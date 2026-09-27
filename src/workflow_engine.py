"""Core lead-qualification and routing logic.

This module is the brain of the Digital Workflow Builder. It takes raw
inbound leads (as exported from a web form) and runs each one through a
fixed, auditable pipeline:

    validate -> normalize -> deduplicate -> score -> classify -> route

Design principles
-----------------
* Deterministic       — no randomness; the same input always produces the
                        same output, which keeps tests stable and the
                        audit trail trustworthy.
* Explainable         — every score carries a per-factor breakdown, so a
                        sales leader can always answer "why did this lead
                        get 82 points?".
* Never crash a batch — bad rows are quarantined in an exception queue
                        with a logged reason; the rest of the batch runs.
* Config over code    — all business rules (weights, thresholds, routing,
                        owners) live in SCORING_CONFIG, so a stakeholder
                        can tune the workflow without touching logic.
"""

import csv
import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging — "timestamp | level | module | message" (README §4).
# Set WORKFLOW_LOG_LEVEL=DEBUG to see per-factor score breakdowns.
# basicConfig() is a no-op when the root logger is already configured
# (e.g. by Streamlit or pytest), so calling it at import time is safe.
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("WORKFLOW_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("workflow_engine")


# ===========================================================================
# 1. CONFIGURATION — the only block a stakeholder should ever need to edit
# ===========================================================================
SCORING_CONFIG: dict = {
    # Required fields — a lead missing any of these is invalid and goes
    # straight to the exception queue.
    "required_fields": ["name", "email", "company"],

    # Scoring rubric, factors 1-6 of 7. Keys are form fields; inner dicts
    # map canonical values (produced by normalize_lead) to points.
    # Factor maximums: 25 + 20 + 15 + 15 + 10 + 10 + 5 (domain) = 100.
    "rubric": {
        "budget": {"50000+": 25, "10000-49999": 15, "under-10000": 5},
        "seniority": {"cxo": 20, "vp": 15, "manager": 10, "ic": 3},
        "employees": {"500+": 15, "50-499": 10, "1-49": 5},
        "timeline": {"immediate": 15, "quarter": 10, "exploring": 3},
        "industry": {"saas": 10, "fintech": 10, "ecommerce": 8, "other": 3},
        "source": {"referral": 10, "webinar": 7, "website": 5, "cold": 2},
    },

    # Factor 7 of 7 — derived from the email domain, not a form field.
    "domain_points": {"business": 5, "free": 0},

    # Points for a value that is present but not recognized.
    # (A blank value always scores 0 and raises `data_incomplete`.)
    "unknown_value_points": {
        "budget": 0,
        "seniority": 0,
        "employees": 0,
        "timeline": 0,
        "industry": 3,  # any real industry is at least an "other"
        "source": 0,
    },

    # Tier thresholds — classify_lead() reads these.
    "tier_thresholds": {"hot": 70, "warm": 40},

    # Routing map — where each tier goes and how fast someone must act.
    "routing_map": {
        "HOT": {
            "owner": "Senior Account Executive",
            "sla": "1 hour",
            "queue": "ae-senior-priority",
            "email_type": "urgent_first_touch",
            "internal_alert": True,
        },
        "WARM": {
            "owner": "Account Executive",
            "sla": "24 hours",
            "queue": "ae-standard",
            "email_type": "standard_first_touch",
            "internal_alert": False,
        },
        "NURTURE": {
            "owner": "Marketing Nurture List",
            "sla": "Weekly digest",
            "queue": "marketing-nurture",
            "email_type": "nurture",
            "internal_alert": False,
        },
    },

    # Consumer mailbox providers — score 0 on factor 7 and raise a
    # verification flag so a human confirms the address before sending.
    "free_mail_domains": [
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in",
        "hotmail.com", "outlook.com", "live.com", "msn.com", "icloud.com",
        "me.com", "aol.com", "protonmail.com", "proton.me", "zoho.com",
        "mail.com",
    ],
}


# ===========================================================================
# 2. FIELD ALIASES — raw form answers mapped to canonical rubric values.
#    This is data cleaning, not business rules: extend freely, no logic
#    changes needed. Anything NOT listed here simply falls through to the
#    "unknown" path (default points + a flag) — it never crashes anything.
# ===========================================================================
FIELD_ALIASES: dict[str, dict[str, str]] = {
    "budget": {
        "50k+": "50000+",
        "50k": "50000+",
        "100k": "50000+",
        "100k+": "50000+",
        "over 50k": "50000+",
        "10k-50k": "10000-49999",
        "10k-49k": "10000-49999",
        "10-50k": "10000-49999",
        "10k+": "10000-49999",
        "<10k": "under-10000",
        "under 10k": "under-10000",
        "less than 10k": "under-10000",
        "below 10k": "under-10000",
        "0-10k": "under-10000",
    },
    "seniority": {
        "c-level": "cxo",
        "c level": "cxo",
        "c-suite": "cxo",
        "ceo": "cxo",
        "cto": "cxo",
        "cfo": "cxo",
        "coo": "cxo",
        "founder": "cxo",
        "co-founder": "cxo",
        "owner": "cxo",
        "vice president": "vp",
        "svp": "vp",
        "avp": "vp",
        "head of sales": "vp",
        "mgr": "manager",
        "director": "manager",
        "senior manager": "manager",
        "team lead": "manager",
        "individual contributor": "ic",
        "employee": "ic",
        "engineer": "ic",
        "developer": "ic",
        "sales rep": "ic",
    },
    "employees": {
        "1000+": "500+",
        "10000+": "500+",
        "enterprise": "500+",
        "50-500": "50-499",
        "mid-market": "50-499",
        "1-50": "1-49",
        "1-10": "1-49",
        "startup": "1-49",
        "smb": "1-49",
        "small business": "1-49",
    },
    "timeline": {
        "asap": "immediate",
        "now": "immediate",
        "urgent": "immediate",
        "this month": "immediate",
        "this quarter": "quarter",
        "next quarter": "quarter",
        "1-3 months": "quarter",
        "1-3 month": "quarter",
        "next month": "quarter",
        "3 months": "quarter",
        "just exploring": "exploring",
        "researching": "exploring",
        "not sure": "exploring",
        "no timeline": "exploring",
        "6 months": "exploring",
        "6+ months": "exploring",
        "next year": "exploring",
    },
    "industry": {
        # Target verticals
        "software": "saas",
        "software as a service": "saas",
        "b2b saas": "saas",
        "financial technology": "fintech",
        "finance": "fintech",
        "banking": "fintech",
        "insurance": "fintech",
        "e-commerce": "ecommerce",
        "e commerce": "ecommerce",
        "retail": "ecommerce",
        "d2c": "ecommerce",
        # Common non-target verticals -> "other"
        "healthcare": "other",
        "education": "other",
        "manufacturing": "other",
        "consulting": "other",
        "logistics": "other",
        "real estate": "other",
    },
    "source": {
        "referred": "referral",
        "word of mouth": "referral",
        "partner": "referral",
        "customer referral": "referral",
        "event": "webinar",
        "conference": "webinar",
        "workshop": "webinar",
        "web form": "website",
        "organic search": "website",
        "content": "website",
        "blog": "website",
        "demo request": "website",
        "cold outreach": "cold",
        "cold email": "cold",
        "cold call": "cold",
        "outbound": "cold",
        "linkedin": "cold",
    },
}


# ===========================================================================
# 3. Result containers (typed outputs — see README §3)
# ===========================================================================
@dataclass
class ScoreResult:
    """Rubric outcome for one lead: total, per-factor points, review flags."""

    total: int
    breakdown: dict[str, int]  # factor name -> points awarded
    flags: list[str]           # e.g. ["data_incomplete", "free_mail_domain"]


@dataclass
class RoutingInfo:
    """Where a qualified lead goes and how fast someone must act on it."""

    tier: str            # "HOT" | "WARM" | "NURTURE"
    owner: str           # team / role that owns the follow-up
    sla: str             # response deadline
    queue: str           # CRM queue / list name
    email_type: str      # which template to draft
    internal_alert: bool  # HOT only: extra heads-up email to the owner


@dataclass
class QualificationResult:
    """Full pipeline outcome for one lead (processed or skipped)."""

    lead: dict                       # normalized lead (raw, if skipped early)
    status: str                      # "PROCESSED" | "SKIPPED"
    skip_reason: str | None = None   # e.g. "duplicate: a@b.com"
    score: ScoreResult | None = None
    routing: RoutingInfo | None = None


# ===========================================================================
# 4. Config self-check — a bad edit fails HERE, not mid-batch
# ===========================================================================
def _validate_config() -> None:
    """Fail fast if SCORING_CONFIG is edited into an invalid state."""
    rubric_fields = set(SCORING_CONFIG["rubric"])
    default_fields = set(SCORING_CONFIG["unknown_value_points"])
    missing = rubric_fields - default_fields
    if missing:
        raise ValueError(
            f"unknown_value_points is missing entries for: {sorted(missing)}"
        )

    max_total = sum(max(points.values()) for points in SCORING_CONFIG["rubric"].values())
    max_total += max(SCORING_CONFIG["domain_points"].values())
    if max_total != 100:
        raise ValueError(
            f"Scoring rubric must total exactly 100 points (currently {max_total})."
        )

    hot = SCORING_CONFIG["tier_thresholds"]["hot"]
    warm = SCORING_CONFIG["tier_thresholds"]["warm"]
    if hot <= warm:
        raise ValueError("tier_thresholds['hot'] must be greater than ['warm'].")

    routing_fields = set(RoutingInfo.__dataclass_fields__) - {"tier"}
    for tier, routing in SCORING_CONFIG["routing_map"].items():
        if set(routing) != routing_fields:
            raise ValueError(
                f"routing_map['{tier}'] keys {sorted(routing)} do not match "
                f"RoutingInfo fields {sorted(routing_fields)}"
            )


_validate_config()  # runs once at import


# ===========================================================================
# 5. Internal helpers
# ===========================================================================
def _extract_first_name(name: str) -> str:
    """Return the first token of a full name, or "" if the name is blank."""
    return name.strip().split()[0] if name.strip() else ""


def _extract_email_domain(email: str) -> str:
    """Return the lowercase domain after the @, or "" if there is none."""
    return email.strip().lower().split("@")[-1] if "@" in email else ""


def _numeric_fallback(field: str, value: str) -> str | None:
    """Bucket a purely numeric answer into a canonical range.

    Real forms often collect free-text numbers ("1200" employees,
    "75000" budget). Returns None when the value is not purely numeric.
    """
    if not value.isdigit():
        return None
    number = int(value)
    if field == "employees":
        if number >= 500:
            return "500+"
        if number >= 50:
            return "50-499"
        return "1-49"
    if field == "budget":  # assume dollars
        if number >= 50_000:
            return "50000+"
        if number >= 10_000:
            return "10000-49999"
        return "under-10000"
    return None


def _canonicalize(field: str, raw_value: str) -> str:
    """Map one raw form answer to its canonical rubric value.

    Lookup order: already-canonical -> alias table -> numeric bucketing.
    Unrecognized values are returned unchanged so score_lead() can award
    default points and raise a data-quality flag.
    """
    cleaned = raw_value.replace("$", "").replace(",", "").strip()
    if cleaned in SCORING_CONFIG["rubric"].get(field, {}):
        return cleaned
    alias = FIELD_ALIASES.get(field, {}).get(cleaned)
    if alias is not None:
        return alias
    bucketed = _numeric_fallback(field, cleaned)
    if bucketed is not None:
        return bucketed
    return cleaned


# ===========================================================================
# 6. Public API (see README §3 for the function list)
# ===========================================================================
def load_leads(csv_path: str) -> list[dict]:
    """Read leads from a CSV file into a list of dicts.

    Header names are lowercased and values are stripped, so downstream
    code can rely on clean keys. Raises friendly errors for a missing
    file or a file with no lead rows.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Lead file not found: '{csv_path}'. Pass a valid CSV path "
            "(see data/sample_leads.csv for the schema)."
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:  # strips Excel BOM
        reader = csv.DictReader(handle)
        leads = [
            {key.strip().lower(): (value or "").strip() for key, value in row.items() if key}
            for row in reader
        ]
    if not leads:
        raise ValueError(f"No lead rows found in '{csv_path}' (empty file or header only).")
    logger.info("Loaded %d lead(s) from %s", len(leads), csv_path)
    return leads


def validate_lead(lead: dict) -> tuple[bool, list[str]]:
    """Check a raw lead against required-field rules.

    Returns (is_valid, reasons). A lead is invalid when a required field
    is blank or the email has no "@" — such rows are skipped into the
    exception queue, never crashed on.
    """
    reasons: list[str] = []
    for required in SCORING_CONFIG["required_fields"]:
        if not lead.get(required):
            reasons.append(f"missing required field: {required}")
    email = lead.get("email", "")
    if email and "@" not in email:
        reasons.append(f"invalid email format: {email}")
    return (not reasons, reasons)


def normalize_lead(lead: dict) -> dict:
    """Clean and standardize a raw lead into canonical form.

    * text fields are trimmed; email is lowercased
    * categorical fields are mapped through FIELD_ALIASES
    * derived helpers (first_name, email_domain) are computed once here,
      so the scoring and email layers never re-derive them
    """
    normalized: dict = {
        "name": str(lead.get("name", "")).strip(),
        "email": str(lead.get("email", "")).strip().lower(),
        "company": str(lead.get("company", "")).strip(),
        "message": str(lead.get("message", "")).strip(),
    }
    for form_field in SCORING_CONFIG["rubric"]:
        raw_value = str(lead.get(form_field, "")).strip().lower()
        normalized[form_field] = _canonicalize(form_field, raw_value)
    normalized["first_name"] = _extract_first_name(normalized["name"])
    normalized["email_domain"] = _extract_email_domain(normalized["email"])
    return normalized


def is_duplicate(lead: dict, seen_emails: set[str]) -> bool:
    """Return True if this lead's email was already seen in the batch."""
    email = str(lead.get("email", "")).strip().lower()
    return bool(email) and email in seen_emails


def score_lead(lead: dict) -> ScoreResult:
    """Score a normalized lead using the 7-factor rubric."""
    logger.debug("Scoring lead %s", lead.get("email"))

    breakdown: dict[str, int] = {}
    flags: list[str] = []

    # ---- factors 1-6: form-field based ---------------------------------
    for form_field, points_map in SCORING_CONFIG["rubric"].items():
        value = str(lead.get(form_field, "")).strip()
        if not value:
            points, flag = 0, "data_incomplete"
        elif value in points_map:
            points, flag = points_map[value], ""
        else:
            points = SCORING_CONFIG["unknown_value_points"][form_field]
            flag = f"unknown_{form_field}"
        breakdown[form_field] = points
        if flag and flag not in flags:
            flags.append(flag)
        logger.debug(
            "  factor=%s value=%r points=%d flag=%s",
            form_field, value, points, flag or "none",
        )

    # ---- factor 7: email domain type -----------------------------------
    domain = lead.get("email_domain") or _extract_email_domain(str(lead.get("email", "")))
    if domain in SCORING_CONFIG["free_mail_domains"]:
        breakdown["domain"] = SCORING_CONFIG["domain_points"]["free"]
        flags.append("free_mail_domain")
    else:
        breakdown["domain"] = SCORING_CONFIG["domain_points"]["business"]

    total = sum(breakdown.values())
    logger.debug("  total=%d/100 flags=%s", total, flags or "none")
    return ScoreResult(total=total, breakdown=breakdown, flags=flags)


def classify_lead(total: int) -> str:
    """Map a 0-100 score to its tier: HOT, WARM or NURTURE."""
    thresholds = SCORING_CONFIG["tier_thresholds"]
    if total >= thresholds["hot"]:
        return "HOT"
    if total >= thresholds["warm"]:
        return "WARM"
    return "NURTURE"


def route_lead(tier: str) -> RoutingInfo:
    """Return the owner, SLA, queue and email type for a lead's tier."""
    routing = SCORING_CONFIG["routing_map"].get(tier)
    if routing is None:
        raise ValueError(
            f"Unknown tier {tier!r}; expected one of {sorted(SCORING_CONFIG['routing_map'])}"
        )
    # The config keys match the RoutingInfo field names exactly
    # (checked at import time by _validate_config).
    return RoutingInfo(tier=tier, **routing)


def process_leads(leads: list[dict]) -> list[QualificationResult]:
    """Run the full pipeline over a batch of raw leads.

    Pipeline per lead: validate -> normalize -> deduplicate -> score ->
    classify -> route. Invalid rows, duplicates and unexpected errors are
    recorded as SKIPPED results with a reason — the batch never crashes.
    """
    results: list[QualificationResult] = []
    seen_emails: set[str] = set()

    logger.info("Processing %d lead(s)...", len(leads))
    for index, raw_lead in enumerate(leads, start=1):
        label = raw_lead.get("email") or raw_lead.get("name") or f"row {index}"
        try:
            # 1) Validate --------------------------------------------------
            is_valid, reasons = validate_lead(raw_lead)
            if not is_valid:
                reason = "invalid: " + "; ".join(reasons)
                logger.warning("Exception queue — %s (%s)", label, reason)
                results.append(
                    QualificationResult(lead=raw_lead, status="SKIPPED", skip_reason=reason)
                )
                continue

            # 2) Normalize -------------------------------------------------
            lead = normalize_lead(raw_lead)

            # 3) Deduplicate (by email, case-insensitive) ------------------
            if is_duplicate(lead, seen_emails):
                reason = f"duplicate: {lead['email']}"
                logger.warning("Exception queue — %s", reason)
                results.append(
                    QualificationResult(lead=lead, status="SKIPPED", skip_reason=reason)
                )
                continue
            seen_emails.add(lead["email"])

            # 4) Score, 5) classify, 6) route ------------------------------
            score = score_lead(lead)
            tier = classify_lead(score.total)
            routing = route_lead(tier)

            logger.info(
                "Lead %s -> %d/100 -> %s -> owner=%s (SLA %s)",
                lead["email"], score.total, tier, routing.owner, routing.sla,
            )
            results.append(
                QualificationResult(lead=lead, status="PROCESSED", score=score, routing=routing)
            )

        except Exception as exc:
            # Deliberate broad catch (README: "batch never crashes"): a
            # single unexpected row is quarantined with a full traceback
            # in the log instead of killing the whole run.
            logger.exception("Unexpected error while processing %s", label)
            results.append(
                QualificationResult(lead=raw_lead, status="SKIPPED", skip_reason=f"error: {exc}")
            )

    logger.info("Pipeline finished: %d result(s)", len(results))
    return results


def build_audit_trail(results: list[QualificationResult]) -> list[dict]:
    """Flatten pipeline results into audit rows (one dict per lead).

    Every processed lead carries its full per-factor score breakdown, so
    the exported CSV answers "why did this lead get this score?" without
    opening any code. Skipped leads appear with their skip reason.
    """
    # Column order is stable because every row starts from this template.
    template: dict = {
        "name": "",
        "email": "",
        "company": "",
        "status": "",
        "tier": "",
        "score": "",
    }
    for factor in [*SCORING_CONFIG["rubric"], "domain"]:
        template[f"{factor}_pts"] = ""
    template.update({"flags": "", "owner": "", "sla": "", "email_type": "", "skip_reason": ""})

    rows: list[dict] = []
    for result in results:
        row = dict(template)
        row["name"] = str(result.lead.get("name", ""))
        row["email"] = str(result.lead.get("email", ""))
        row["company"] = str(result.lead.get("company", ""))
        row["status"] = result.status
        if result.routing:
            row["tier"] = result.routing.tier
            row["owner"] = result.routing.owner
            row["sla"] = result.routing.sla
            row["email_type"] = result.routing.email_type
        if result.score:
            row["score"] = result.score.total
            row["flags"] = "; ".join(result.score.flags)
            for factor, points in result.score.breakdown.items():
                row[f"{factor}_pts"] = points
        row["skip_reason"] = result.skip_reason or ""
        rows.append(row)
    return rows


def summarize_results(results: list[QualificationResult]) -> dict:
    """Summarize a run for the stakeholder report (README §6 / docs/).

    Returns tier counts, skip-reason counts and the average score.
    """
    processed = [r for r in results if r.status == "PROCESSED"]
    skipped = [r for r in results if r.status == "SKIPPED"]

    tier_counts = {tier: 0 for tier in SCORING_CONFIG["routing_map"]}
    for result in processed:
        tier_counts[result.routing.tier] += 1

    skip_categories = Counter(
        (result.skip_reason or "unknown").split(":", 1)[0].strip() for result in skipped
    )

    average_score = (
        round(sum(r.score.total for r in processed) / len(processed), 1) if processed else 0.0
    )

    summary = {
        "total": len(results),
        "processed": len(processed),
        "skipped": len(skipped),
        "tiers": tier_counts,
        "skip_reasons": dict(skip_categories),
        "average_score": average_score,
    }
    logger.info("Run summary: %s", summary)
    return summary


__all__ = [
    "SCORING_CONFIG",
    "ScoreResult",
    "RoutingInfo",
    "QualificationResult",
    "load_leads",
    "validate_lead",
    "normalize_lead",
    "is_duplicate",
    "score_lead",
    "classify_lead",
    "route_lead",
    "process_leads",
    "build_audit_trail",
    "summarize_results",
]
