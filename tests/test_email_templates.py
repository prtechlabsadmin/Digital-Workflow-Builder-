"""Unit tests for src/email_templates.py (README §4).

Covers: render_template behaviour (including fail-visible substitution),
the template/variable manifest, personalization fallbacks (greeting,
timeline, industry, message quoting), each draft generator, the internal
alert, and fully-rendered drafts for every lead in the golden sample run.

Run:  pytest -v
"""

import logging
import re

import pytest

from src.email_templates import (
    EMAIL_CONFIG,
    EMAIL_VARIABLES,
    TEMPLATES,
    draft_first_touch_email,
    draft_internal_alert,
    draft_nurture_email,
    render_template,
)
from src.workflow_engine import (
    QualificationResult,
    classify_lead,
    normalize_lead,
    route_lead,
    score_lead,
)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------
def make_result(**overrides) -> QualificationResult:
    """A fully processed result; defaults build the perfect HOT lead."""
    raw = {
        "name": "Priya Sharma",
        "email": "priya@acmecorp.com",
        "company": "Acme Corp",
        "budget": "50000+",
        "seniority": "cxo",
        "employees": "500+",
        "timeline": "immediate",
        "industry": "saas",
        "source": "referral",
        "message": "We need to roll this out across three teams this quarter.",
    }
    raw.update(overrides)
    lead = normalize_lead(raw)
    score = score_lead(lead)
    routing = route_lead(classify_lead(score.total))
    return QualificationResult(lead=lead, status="PROCESSED", score=score, routing=routing)


def make_warm_result() -> QualificationResult:
    """A representative WARM lead (58/100)."""
    return make_result(
        email="w@warmco.com", budget="10000-49999", seniority="manager",
        employees="1-49", timeline="quarter", industry="ecommerce", source="website",
    )


def make_nurture_result() -> QualificationResult:
    """A minimal NURTURE lead (0/100, free-mail + incomplete data)."""
    return make_result(
        email="n@gmail.com", budget="", seniority="",
        employees="", timeline="", industry="", source="",
    )


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------
class TestRenderTemplate:
    def test_substitutes_known_variables(self):
        out = render_template("Hi {{name}}, welcome to {{city}}!", {"name": "Priya", "city": "Pune"})
        assert out == "Hi Priya, welcome to Pune!"

    def test_unknown_variable_stays_visible_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = render_template("Hi {{name}} from {{missing}}", {"name": "Priya"})
        assert "{{missing}}" in out  # fail visible, not silent
        assert any("missing" in rec.getMessage() for rec in caplog.records)

    def test_extra_variables_are_ignored(self):
        assert render_template("Hi {{name}}", {"name": "P", "unused": "x"}) == "Hi P"

    def test_whitespace_inside_placeholder_is_fine(self):
        assert render_template("Hi {{ name }}", {"name": "Priya"}) == "Hi Priya"


# ---------------------------------------------------------------------------
# Template / variable manifest
# ---------------------------------------------------------------------------
class TestTemplateManifest:
    def test_every_template_is_documented(self):
        assert set(TEMPLATES) == set(EMAIL_VARIABLES)

    def test_placeholders_match_manifest(self):
        pattern = re.compile(r"\{\{\s*(\w+)\s*\}\}")
        for name, listing in EMAIL_VARIABLES.items():
            used = set(pattern.findall(TEMPLATES[name]["subject"] + TEMPLATES[name]["body"]))
            assert used == set(listing), name


# ---------------------------------------------------------------------------
# First-touch drafts
# ---------------------------------------------------------------------------
class TestFirstTouchEmail:
    def test_hot_lead_gets_urgent_template(self):
        result = make_result()
        draft = draft_first_touch_email(result.lead, result.routing, result.score)
        assert draft.template == "urgent_first_touch"
        assert draft.to == "priya@acmecorp.com"

    def test_warm_lead_gets_standard_template(self):
        result = make_warm_result()
        draft = draft_first_touch_email(result.lead, result.routing, result.score)
        assert draft.template == "standard_first_touch"

    def test_no_unrendered_variables_in_output(self):
        result = make_result()
        draft = draft_first_touch_email(result.lead, result.routing, result.score)
        assert "{{" not in draft.subject + draft.body

    def test_subject_mentions_company(self):
        result = make_result()
        assert "Acme Corp" in draft_first_touch_email(result.lead, result.routing, result.score).subject

    def test_body_references_their_message(self):
        result = make_result()
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        assert "three teams" in body

    def test_sender_identity_comes_from_config(self):
        result = make_result()
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        assert EMAIL_CONFIG["sender"]["name"] in body

    def test_nurture_type_is_rejected(self):
        result = make_nurture_result()
        with pytest.raises(ValueError, match="nurture"):
            draft_first_touch_email(result.lead, result.routing, result.score)


# ---------------------------------------------------------------------------
# Personalization fallbacks
# ---------------------------------------------------------------------------
class TestGreetingFallbacks:
    def test_uses_first_name(self):
        draft = draft_first_touch_email(*(lambda r: (r.lead, r.routing, r.score))(make_result()))
        assert draft.body.startswith("Hi Priya,")

    def test_placeholder_name_falls_back_to_company(self):
        result = make_result(name="N/A", email="ops@vertexcapital.com", company="Vertex Capital")
        draft = draft_first_touch_email(result.lead, result.routing, result.score)
        assert draft.body.startswith("Hi Vertex Capital team,")

    def test_no_name_or_company_falls_back_to_there(self):
        result = make_result(name="", company="")
        draft = draft_first_touch_email(result.lead, result.routing, result.score)
        assert draft.body.startswith("Hi there,")


class TestPhrasing:
    @pytest.mark.parametrize(
        "timeline,phrase",
        [
            ("immediate", "move this forward this month"),
            ("quarter", "plan for the coming quarter"),
            ("exploring", "explore your options"),
        ],
    )
    def test_timeline_phrase(self, timeline, phrase):
        result = make_result(timeline=timeline)
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        assert phrase in body

    def test_unknown_timeline_uses_default_phrase(self):
        result = make_result(timeline="whenever")
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        assert "evaluate your options" in body

    def test_industry_examples_are_relevant(self):
        result = make_result()
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        assert "product and growth teams at SaaS companies" in body


class TestMessageLine:
    def test_short_message_is_quoted(self):
        result = make_result()
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        assert '"We need to roll this out across three teams this quarter."' in body

    def test_long_message_is_truncated(self):
        result = make_result(message="x" * 300)
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        limit = EMAIL_CONFIG["message_snippet_max"]
        assert "x" * (limit - 3) in body      # kept up to the limit …
        assert "x" * (limit - 2) not in body  # … and not a character more

    def test_blank_message_uses_neutral_line(self):
        result = make_result(message="")
        body = draft_first_touch_email(result.lead, result.routing, result.score).body
        assert "It is great to connect with you." in body


# ---------------------------------------------------------------------------
# Nurture drafts
# ---------------------------------------------------------------------------
class TestNurtureEmail:
    def test_contains_both_asset_links(self):
        draft = draft_nurture_email(make_nurture_result().lead)
        assert EMAIL_CONFIG["assets"]["getting_started_url"] in draft.body
        assert EMAIL_CONFIG["assets"]["overview_video_url"] in draft.body

    def test_subject_mentions_company(self):
        draft = draft_nurture_email(make_result(company="SlowCo").lead)
        assert "SlowCo" in draft.subject


# ---------------------------------------------------------------------------
# Internal alerts (HOT leads)
# ---------------------------------------------------------------------------
class TestInternalAlert:
    def test_subject_carries_tier_score_company_and_sla(self):
        result = make_result()
        alert = draft_internal_alert(result.lead, result)
        assert alert.subject == "[HOT LEAD — 100/100] Acme Corp — respond within 1 hour"

    def test_body_contains_score_breakdown(self):
        result = make_result()
        alert = draft_internal_alert(result.lead, result)
        assert "budget 25" in alert.body
        assert "domain 5" in alert.body

    def test_body_contains_review_flags(self):
        result = make_result(email="rohan@gmail.com")  # free-mail flag, still HOT (95)
        alert = draft_internal_alert(result.lead, result)
        assert "free_mail_domain" in alert.body

    def test_alert_embeds_the_first_touch_draft(self):
        result = make_result()
        alert = draft_internal_alert(result.lead, result)
        first = draft_first_touch_email(result.lead, result.routing, result.score)
        assert first.subject in alert.body
        assert "drafted first-touch email" in alert.body

    def test_warm_lead_does_not_get_an_internal_alert(self):
        warm = make_warm_result()
        with pytest.raises(ValueError, match="internal_alert"):
            draft_internal_alert(warm.lead, warm)

    def test_requires_a_fully_processed_result(self):
        partial = QualificationResult(lead={}, status="PROCESSED")  # no score/routing
        with pytest.raises(ValueError, match="fully processed"):
            draft_internal_alert({}, partial)


# ---------------------------------------------------------------------------
# EmailDraft formatting
# ---------------------------------------------------------------------------
class TestEmailDraftText:
    def test_as_text_layout(self):
        text = draft_nurture_email(make_nurture_result().lead).as_text()
        assert text.startswith("To:")
        assert "\nSubject: " in text


# ---------------------------------------------------------------------------
# Golden drafts — every sample lead renders completely
# ---------------------------------------------------------------------------
class TestGoldenDrafts:
    def test_every_sample_lead_gets_a_fully_rendered_draft(self, sample_results):
        for result in sample_results:
            if result.status != "PROCESSED":
                continue
            if result.routing.email_type == "nurture":
                draft = draft_nurture_email(result.lead)
            else:
                draft = draft_first_touch_email(result.lead, result.routing, result.score)
            assert "{{" not in draft.subject + draft.body, result.lead["email"]

    def test_internal_alerts_render_for_every_hot_lead(self, sample_results):
        hot = [r for r in sample_results if r.status == "PROCESSED" and r.routing.tier == "HOT"]
        assert len(hot) == 4
        for result in hot:
            alert = draft_internal_alert(result.lead, result)
            assert "{{" not in alert.subject + alert.body

    def test_placeholder_name_lead_greets_the_company(self, sample_results):
        by_email = {r.lead["email"]: r for r in sample_results if r.status == "PROCESSED"}
        result = by_email["ops@vertexcapital.com"]
        draft = draft_first_touch_email(result.lead, result.routing, result.score)
        assert draft.body.startswith("Hi Vertex Capital team,")
