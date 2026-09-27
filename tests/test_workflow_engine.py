"""Unit tests for src/workflow_engine.py (README §4).

Covers: config integrity, loading, validation, normalization, duplicate
detection, every scoring factor, tier boundaries (39/40 and 69/70),
routing, the never-crash guarantee, audit trail, summaries, and the
golden end-to-end run on the bundled sample data.

Run:  pytest -v
"""

from pathlib import Path

import pytest

from src.workflow_engine import (
    SCORING_CONFIG,
    RoutingInfo,
    build_audit_trail,
    classify_lead,
    is_duplicate,
    load_leads,
    normalize_lead,
    process_leads,
    route_lead,
    score_lead,
    summarize_results,
    validate_lead,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_leads.csv"


# ---------------------------------------------------------------------------
# Reusable factories
# ---------------------------------------------------------------------------
def make_lead(**overrides) -> dict:
    """A fully-specified, high-quality raw lead; override any field."""
    lead = {
        "name": "Priya Sharma",
        "email": "priya@acmecorp.com",
        "company": "Acme Corp",
        "budget": "50000+",
        "seniority": "cxo",
        "employees": "500+",
        "timeline": "immediate",
        "industry": "saas",
        "source": "referral",
        "message": "Need a demo",
    }
    lead.update(overrides)
    return lead


def scored(**overrides):
    """normalize + score a raw lead in one step."""
    return score_lead(normalize_lead(make_lead(**overrides)))


# ---------------------------------------------------------------------------
# Config integrity — a bad edit fails loudly, and here too
# ---------------------------------------------------------------------------
class TestConfigIntegrity:
    def test_rubric_totals_exactly_100(self):
        max_total = sum(max(p.values()) for p in SCORING_CONFIG["rubric"].values())
        max_total += max(SCORING_CONFIG["domain_points"].values())
        assert max_total == 100

    def test_thresholds_are_ordered(self):
        t = SCORING_CONFIG["tier_thresholds"]
        assert 0 <= t["warm"] < t["hot"] <= 100

    def test_routing_map_matches_routinginfo_fields(self):
        expected = set(RoutingInfo.__dataclass_fields__) - {"tier"}
        for tier, routing in SCORING_CONFIG["routing_map"].items():
            assert set(routing) == expected, tier


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
class TestLoadLeads:
    def test_sample_csv_loads_20_leads(self):
        assert len(load_leads(str(SAMPLE_CSV))) == 20

    def test_headers_lowercased_and_values_stripped(self, tmp_path):
        path = tmp_path / "leads.csv"
        path.write_text("Name,Email,Company\n  Jane , jane@x.com , Xylo \n", encoding="utf-8")
        assert load_leads(str(path)) == [
            {"name": "Jane", "email": "jane@x.com", "company": "Xylo"}
        ]

    def test_missing_file_raises_friendly_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            load_leads(str(tmp_path / "nope.csv"))

    def test_header_only_file_raises_value_error(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("name,email,company\n", encoding="utf-8")
        with pytest.raises(ValueError, match="No lead rows"):
            load_leads(str(path))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class TestValidateLead:
    def test_complete_lead_is_valid(self):
        ok, reasons = validate_lead(make_lead())
        assert ok and reasons == []

    def test_missing_required_field_is_reported(self):
        ok, reasons = validate_lead(make_lead(email=""))
        assert not ok
        assert "missing required field: email" in reasons

    def test_all_missing_fields_reported_at_once(self):
        ok, reasons = validate_lead({"name": "", "email": "", "company": ""})
        assert not ok
        assert len(reasons) == 3

    def test_email_without_at_is_invalid(self):
        ok, reasons = validate_lead(make_lead(email="priya.at.acmecorp.com"))
        assert not ok
        assert any("invalid email format" in r for r in reasons)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
class TestNormalizeLead:
    def test_email_lowercased_and_trimmed(self):
        normalized = normalize_lead(make_lead(email="  Priya@AcmeCorp.COM "))
        assert normalized["email"] == "priya@acmecorp.com"

    def test_derived_fields(self):
        normalized = normalize_lead(make_lead())
        assert normalized["first_name"] == "Priya"
        assert normalized["email_domain"] == "acmecorp.com"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("50K+", "50000+"),
            ("100k+", "50000+"),
            ("$75,000", "50000+"),  # numeric bucketing
            ("10K-50K", "10000-49999"),
            ("under 10k", "under-10000"),
        ],
    )
    def test_budget_aliases(self, raw, expected):
        assert normalize_lead(make_lead(budget=raw))["budget"] == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("CEO", "cxo"),
            ("Founder", "cxo"),
            ("Head of Sales", "vp"),
            ("Director", "manager"),
            ("Individual Contributor", "ic"),
        ],
    )
    def test_seniority_aliases(self, raw, expected):
        assert normalize_lead(make_lead(seniority=raw))["seniority"] == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1200", "500+"),  # numeric bucketing
            ("300", "50-499"),
            ("15", "1-49"),
            ("Mid-Market", "50-499"),
            ("Startup", "1-49"),
        ],
    )
    def test_employees_values(self, raw, expected):
        assert normalize_lead(make_lead(employees=raw))["employees"] == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("ASAP", "immediate"),
            ("Next Quarter", "quarter"),
            ("Just Exploring", "exploring"),
            ("6 Months", "exploring"),
        ],
    )
    def test_timeline_aliases(self, raw, expected):
        assert normalize_lead(make_lead(timeline=raw))["timeline"] == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SaaS", "saas"),
            ("Financial Technology", "fintech"),
            ("E-commerce", "ecommerce"),
            ("Healthcare", "other"),
        ],
    )
    def test_industry_aliases(self, raw, expected):
        assert normalize_lead(make_lead(industry=raw))["industry"] == expected

    def test_unrecognized_value_passes_through_for_scoring_to_flag(self):
        # Unknown values are NOT invented into a category — they pass
        # through (lowercased) and score_lead awards default points + flag.
        assert normalize_lead(make_lead(industry="Hospitality"))["industry"] == "hospitality"


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
class TestIsDuplicate:
    def test_duplicate_detected(self):
        assert is_duplicate({"email": "a@b.com"}, {"a@b.com"})

    def test_new_email_not_duplicate(self):
        assert not is_duplicate({"email": "c@d.com"}, {"a@b.com"})

    def test_duplicate_check_is_case_insensitive(self):
        assert is_duplicate({"email": "A@B.com"}, {"a@b.com"})

    def test_blank_email_never_counts_as_duplicate(self):
        assert not is_duplicate({"email": ""}, {""})


# ---------------------------------------------------------------------------
# Scoring — every factor, plus edge behaviour
# ---------------------------------------------------------------------------
class TestScoreLead:
    def test_perfect_lead_scores_100_with_no_flags(self):
        result = scored()
        assert result.total == 100
        assert result.flags == []
        assert result.breakdown == {
            "budget": 25, "seniority": 20, "employees": 15,
            "timeline": 15, "industry": 10, "source": 10, "domain": 5,
        }

    @pytest.mark.parametrize("value,points", [("50000+", 25), ("10000-49999", 15), ("under-10000", 5)])
    def test_budget_factor(self, value, points):
        assert scored(budget=value).breakdown["budget"] == points

    def test_blank_budget_scores_zero_and_flags(self):
        result = scored(budget="")
        assert result.breakdown["budget"] == 0
        assert "data_incomplete" in result.flags

    @pytest.mark.parametrize("value,points", [("cxo", 20), ("vp", 15), ("manager", 10), ("ic", 3)])
    def test_seniority_factor(self, value, points):
        assert scored(seniority=value).breakdown["seniority"] == points

    @pytest.mark.parametrize("value,points", [("500+", 15), ("50-499", 10), ("1-49", 5)])
    def test_employees_factor(self, value, points):
        assert scored(employees=value).breakdown["employees"] == points

    @pytest.mark.parametrize("value,points", [("immediate", 15), ("quarter", 10), ("exploring", 3)])
    def test_timeline_factor(self, value, points):
        assert scored(timeline=value).breakdown["timeline"] == points

    @pytest.mark.parametrize(
        "value,points", [("saas", 10), ("fintech", 10), ("ecommerce", 8), ("other", 3)]
    )
    def test_industry_factor(self, value, points):
        assert scored(industry=value).breakdown["industry"] == points

    @pytest.mark.parametrize(
        "value,points", [("referral", 10), ("webinar", 7), ("website", 5), ("cold", 2)]
    )
    def test_source_factor(self, value, points):
        assert scored(source=value).breakdown["source"] == points

    def test_business_domain_scores_5(self):
        assert scored().breakdown["domain"] == 5

    def test_free_mail_domain_scores_zero_and_flags(self):
        result = scored(email="rohan@gmail.com")
        assert result.breakdown["domain"] == 0
        assert "free_mail_domain" in result.flags

    def test_unknown_value_gets_default_points_and_flag(self):
        result = scored(industry="Hospitality", source="Billboard")
        assert result.breakdown["industry"] == 3  # any real industry >= "other"
        assert result.breakdown["source"] == 0
        assert "unknown_industry" in result.flags
        assert "unknown_source" in result.flags

    def test_all_minimum_lead_scores_zero(self):
        lead = {
            "name": "Sam Rivera",
            "email": "sam.rivera@gmail.com",
            "company": "Freelance",
            "message": "just browsing",
        }
        result = score_lead(normalize_lead(lead))
        assert result.total == 0
        assert "data_incomplete" in result.flags
        assert "free_mail_domain" in result.flags


# ---------------------------------------------------------------------------
# Classification — the promised boundary values
# ---------------------------------------------------------------------------
class TestClassifyLead:
    @pytest.mark.parametrize(
        "total,tier",
        [
            (100, "HOT"),
            (70, "HOT"),   # upper boundary (inclusive)
            (69, "WARM"),  # just below HOT
            (40, "WARM"),  # lower boundary (inclusive)
            (39, "NURTURE"),
            (0, "NURTURE"),
        ],
    )
    def test_tier_boundaries(self, total, tier):
        assert classify_lead(total) == tier


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
class TestRouteLead:
    def test_hot_routing(self):
        r = route_lead("HOT")
        assert (r.owner, r.sla, r.queue, r.email_type, r.internal_alert) == (
            "Senior Account Executive", "1 hour", "ae-senior-priority",
            "urgent_first_touch", True,
        )

    def test_warm_routing(self):
        r = route_lead("WARM")
        assert (r.owner, r.sla, r.queue, r.email_type, r.internal_alert) == (
            "Account Executive", "24 hours", "ae-standard", "standard_first_touch", False,
        )

    def test_nurture_routing(self):
        r = route_lead("NURTURE")
        assert (r.owner, r.sla, r.email_type) == ("Marketing Nurture List", "Weekly digest", "nurture")

    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError, match="Unknown tier"):
            route_lead("SUPERHOT")


# ---------------------------------------------------------------------------
# Orchestration — including the never-crash guarantee
# ---------------------------------------------------------------------------
class TestProcessLeads:
    def test_returns_one_result_per_input(self):
        results = process_leads([make_lead(), make_lead(email="b@c.com")])
        assert len(results) == 2
        assert all(r.status == "PROCESSED" for r in results)

    def test_invalid_lead_is_skipped_with_reason(self):
        results = process_leads([make_lead(email="")])
        assert results[0].status == "SKIPPED"
        assert "missing required field: email" in results[0].skip_reason

    def test_duplicate_is_skipped_with_reason(self):
        results = process_leads([make_lead(), make_lead()])
        assert results[0].status == "PROCESSED"
        assert results[1].status == "SKIPPED"
        assert results[1].skip_reason == "duplicate: priya@acmecorp.com"

    def test_order_is_preserved(self):
        leads = [make_lead(email=f"u{i}@x.com") for i in range(3)]
        results = process_leads(leads)
        assert [r.lead["email"] for r in results] == [f"u{i}@x.com" for i in range(3)]

    def test_batch_never_crashes_on_malformed_row(self):
        # A row that is not even a dict must be quarantined, not fatal.
        results = process_leads(["not-a-dict", make_lead()])
        assert results[0].status == "SKIPPED"
        assert results[0].skip_reason.startswith("error:")
        assert results[1].status == "PROCESSED"


# ---------------------------------------------------------------------------
# Audit trail & summary
# ---------------------------------------------------------------------------
class TestBuildAuditTrail:
    def test_processed_row_carries_full_breakdown(self):
        row = build_audit_trail(process_leads([make_lead()]))[0]
        assert row["status"] == "PROCESSED"
        assert row["score"] == 100
        assert row["budget_pts"] == 25
        assert row["domain_pts"] == 5
        assert row["tier"] == "HOT"
        assert row["owner"] == "Senior Account Executive"

    def test_skipped_row_has_reason_and_blank_score(self):
        row = build_audit_trail(process_leads([make_lead(email="")]))[0]
        assert row["status"] == "SKIPPED"
        assert row["score"] == ""
        assert "missing required field" in row["skip_reason"]


class TestSummarizeResults:
    def test_counts_tiers_skips_and_average(self):
        leads = [
            make_lead(),                                                              # 100 -> HOT
            make_result_warm := make_lead(                                            # 58 -> WARM
                email="w@x.com", budget="10000-49999", seniority="manager",
                employees="1-49", timeline="quarter", industry="ecommerce", source="website",
            ),
            make_lead(email="n@gmail.com", budget="", seniority="",                   # 0 -> NURTURE
                      employees="", timeline="", industry="", source=""),
            make_lead(email=""),                                                      # invalid
        ]
        summary = summarize_results(process_leads(leads))
        assert summary["total"] == 4
        assert summary["processed"] == 3
        assert summary["skipped"] == 1
        assert summary["tiers"] == {"HOT": 1, "WARM": 1, "NURTURE": 1}
        assert summary["skip_reasons"] == {"invalid": 1}
        assert summary["average_score"] == 52.7  # (100 + 58 + 0) / 3

    def test_empty_input_summarizes_to_zero(self):
        assert summarize_results([]) == {
            "total": 0, "processed": 0, "skipped": 0,
            "tiers": {"HOT": 0, "WARM": 0, "NURTURE": 0},
            "skip_reasons": {}, "average_score": 0.0,
        }


# ---------------------------------------------------------------------------
# Golden end-to-end run — the README's promised numbers
# ---------------------------------------------------------------------------
class TestGoldenSampleRun:
    def test_summary_matches_documented_run(self, sample_results):
        assert summarize_results(sample_results) == {
            "total": 20,
            "processed": 18,
            "skipped": 2,
            "tiers": {"HOT": 4, "WARM": 9, "NURTURE": 5},
            "skip_reasons": {"duplicate": 1, "invalid": 1},
            "average_score": 52.4,
        }

    def test_known_lead_scores(self, sample_results):
        by_email = {r.lead["email"]: r for r in sample_results if r.status == "PROCESSED"}
        assert by_email["priya.sharma@acmecorp.com"].score.total == 100  # perfect lead
        assert by_email["david.okafor@gmail.com"].score.total == 40      # WARM boundary
        assert by_email["sam.rivera@gmail.com"].score.total == 0         # all-minimum
        assert by_email["neha.kulkarni@paybridge.in"].score.total == 67  # see Correction 2

    def test_skipped_rows_are_duplicate_and_invalid(self, sample_results):
        skipped = [r for r in sample_results if r.status == "SKIPPED"]
        reasons = sorted(r.skip_reason.split(":", 1)[0] for r in skipped)
        assert reasons == ["duplicate", "invalid"]
