"""Professional email draft generators for the Digital Workflow Builder.

Every outbound touch in this workflow starts as a *draft* that a human
reviews before sending. Automation prepares the communication; people
approve it (human-in-the-loop by design).

Design principles
-----------------
* Templates, not free text — every email renders from a template with
  {{variables}}, so wording stays consistent, reviewable and on-brand.
* Fail visible, not silent — a variable that cannot be filled stays
  visible in the draft as {{variable}} (and logs a warning), so a
  reviewer catches it before anything is sent.
* Config over code — sender identity, product name and asset links live
  in EMAIL_CONFIG; nobody edits email wording just to change a signature.
* Defensive derivation — every personalization (greeting, timeline
  phrasing, quoted message) has a graceful fallback, so even a lead with
  sparse data gets a natural-sounding draft.
"""

import logging
import re
from dataclasses import dataclass

from src.workflow_engine import (
    SCORING_CONFIG,
    QualificationResult,
    RoutingInfo,
    ScoreResult,
)

logger = logging.getLogger("email_templates")


# ===========================================================================
# 1. CONFIGURATION — identities, product and links (edit here, not in text)
# ===========================================================================
EMAIL_CONFIG: dict = {
    # Sender identity used in every external draft. Replace with the real
    # rep, or load from environment variables / CRM in production.
    "sender": {
        "name": "Alex Morgan",
        "title": "Account Executive",
        "contact": "alex.morgan@ourcompany.com | +1 (555) 012-3456",
    },
    # What we sell. Keep generic, or set to the real product name.
    "product_name": "our platform",
    # Links used by the nurture email (marketing decides these).
    "assets": {
        "getting_started_url": "https://ourcompany.com/getting-started",
        "overview_video_url": "https://ourcompany.com/overview-video",
    },
    # Internal distribution lists that receive tier alerts.
    "internal_recipients": {
        "HOT": "sales-hotline@ourcompany.com",
        "WARM": "ae-standard@ourcompany.com",
        "NURTURE": "marketing-nurture@ourcompany.com",
    },
    # Max characters of the lead's message quoted in a first-touch email.
    "message_snippet_max": 160,
}


# ===========================================================================
# 2. TEMPLATES — {{variable}} placeholders only; wording lives here
# ===========================================================================
TEMPLATES: dict[str, dict[str, str]] = {
    "urgent_first_touch": {
        "subject": "Next steps for {{company}} — quick question",
        "body": """\
Hi {{greeting_name}},

Thank you for reaching out about {{company}} — based on what you shared,
it sounds like we could help as you {{timeline_phrase}}.

{{message_line}}

I have flagged your inquiry for priority handling, so here is what I
would suggest to move quickly:

  1. A focused 25-minute walkthrough of {{product_name}}, with examples
     relevant to {{industry_examples}}
  2. Pricing and rollout options, with numbers you can take internally
  3. An introduction to a customer in a comparable situation, if useful

Would tomorrow or the day after suit for a quick call? Just reply with a
time that works and I will send an invitation.

Looking forward to helping {{company}} move quickly,

{{sender_name}}
{{sender_title}}
{{sender_contact}}
""",
    },
    "standard_first_touch": {
        "subject": "Following up on your inquiry about {{company}}",
        "body": """\
Hi {{greeting_name}},

Thank you for reaching out about {{company}} — it sounds like we could
help as you {{timeline_phrase}}.

{{message_line}}

To make evaluating us easy, I would suggest a short 25-minute call
where I can:

  1. Walk you through {{product_name}}, with examples relevant to
     {{industry_examples}}
  2. Share pricing and rollout options you can take back internally

Would sometime this week or next suit? Reply with a couple of times
that work for you and I will set it up.

Best regards,

{{sender_name}}
{{sender_title}}
{{sender_contact}}
""",
    },
    "nurture": {
        "subject": "A few resources for {{company}} — whenever you are ready",
        "body": """\
Hi {{greeting_name}},

Thank you for your interest — it is great to connect with {{company}}.

Whenever it is useful, here are two things that might help as you
{{timeline_phrase}}:

  * A short getting-started guide: {{getting_started_url}}
  * A 3-minute overview of {{product_name}}: {{overview_video_url}}

There is no rush at all. If now is not the right time, I will check
back in a month — and if something comes up sooner, simply reply to
this email and I will pick it up from there.

Best regards,

{{sender_name}}
{{sender_title}}
{{sender_contact}}
""",
    },
    "internal_alert": {
        "subject": "[{{tier}} LEAD — {{score}}/100] {{company}} — respond within {{sla}}",
        "body": """\
Hi team,

A new inbound lead has qualified as {{tier}} and needs a response
within its {{sla}} SLA window.

  Lead:       {{lead_line}}
  Company:    {{company}}
  Score:      {{score}}/100 ({{tier}})
  Breakdown:  {{breakdown_line}}
  Flags:      {{flags_line}}
  Routed to:  {{owner}} (queue: {{queue}})
  SLA:        {{sla}}

Next step: claim the lead in the queue and send the drafted first-touch
email (pasted below) within the SLA window.

---------- drafted first-touch email ----------
{{drafted_email}}
------------------------------------------------

This alert was generated automatically by the Digital Workflow Builder.
Please reply to this thread only after the lead has been claimed.
""",
    },
}

# Self-documenting variable manifest: one entry per template, listing
# every {{variable}} that template expects (checked at import time).
EMAIL_VARIABLES: dict[str, list[str]] = {
    "urgent_first_touch": [
        "greeting_name", "company", "timeline_phrase", "message_line",
        "product_name", "industry_examples", "sender_name", "sender_title",
        "sender_contact",
    ],
    "standard_first_touch": [
        "greeting_name", "company", "timeline_phrase", "message_line",
        "product_name", "industry_examples", "sender_name", "sender_title",
        "sender_contact",
    ],
    "nurture": [
        "greeting_name", "company", "timeline_phrase", "product_name",
        "getting_started_url", "overview_video_url", "sender_name",
        "sender_title", "sender_contact",
    ],
    "internal_alert": [
        "lead_line", "company", "score", "tier", "breakdown_line",
        "flags_line", "owner", "queue", "sla", "drafted_email",
    ],
}


# ===========================================================================
# 3. Result container
# ===========================================================================
@dataclass
class EmailDraft:
    """A reviewable email draft — nothing is sent without human approval."""

    to: str
    subject: str
    body: str
    template: str  # key in TEMPLATES

    def as_text(self) -> str:
        """Render the draft as plain text (CLI output / copy-paste)."""
        return f"To:      {self.to}\nSubject: {self.subject}\n\n{self.body}"


# ===========================================================================
# 4. Template self-check — a template/manifest drift fails HERE, at import
# ===========================================================================
_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _validate_templates() -> None:
    """Fail fast if a template and EMAIL_VARIABLES drift apart."""
    for name, listing in EMAIL_VARIABLES.items():
        if name not in TEMPLATES:
            raise ValueError(f"EMAIL_VARIABLES lists unknown template: {name!r}")
        used = set(_PLACEHOLDER_PATTERN.findall(TEMPLATES[name]["subject"]))
        used |= set(_PLACEHOLDER_PATTERN.findall(TEMPLATES[name]["body"]))
        documented = set(listing)
        if used != documented:
            raise ValueError(
                f"Template {name!r} variable mismatch: "
                f"used-but-undocumented={sorted(used - documented)}, "
                f"documented-but-unused={sorted(documented - used)}"
            )
    for name in TEMPLATES:
        if name not in EMAIL_VARIABLES:
            raise ValueError(f"Template {name!r} is missing from EMAIL_VARIABLES.")


_validate_templates()  # runs once at import


# ===========================================================================
# 5. Rendering
# ===========================================================================
def render_template(template: str, variables: dict[str, str]) -> str:
    """Render a {{variable}} template safely.

    Known variables are substituted. Anything that cannot be filled stays
    visible in the output as {{variable}} and logs a warning, so a human
    reviewer catches it before the email is sent. Unused keys in
    ``variables`` are simply ignored.
    """

    def _substitute(match: re.Match) -> str:
        key = match.group(1)
        if key in variables:
            return str(variables[key])
        logger.warning("Template variable left visible for review: {{%s}}", key)
        return match.group(0)

    return _PLACEHOLDER_PATTERN.sub(_substitute, template)


# ===========================================================================
# 6. Personalization helpers — every one has a graceful fallback
# ===========================================================================
# Names people type into forms when they do not want to give a real name.
_PLACEHOLDER_NAMES = {"n/a", "na", "-", "--", "unknown", "none", "test", "anonymous", "x"}

_TIMELINE_PHRASES = {
    "immediate": "move this forward this month",
    "quarter": "plan for the coming quarter",
    "exploring": "explore your options",
}
_DEFAULT_TIMELINE_PHRASE = "evaluate your options"

_INDUSTRY_EXAMPLES = {
    "saas": "product and growth teams at SaaS companies",
    "fintech": "operations and finance teams in fintech",
    "ecommerce": "e-commerce and retail teams",
    "other": "teams across a range of industries",
}
_DEFAULT_INDUSTRY_EXAMPLES = "teams across a range of industries"


def _first_name(lead: dict) -> str:
    """Best-effort first name; tolerates raw (un-normalized) lead dicts."""
    first = str(lead.get("first_name", "")).strip()
    if first:
        return first
    tokens = str(lead.get("name", "")).strip().split()
    return tokens[0] if tokens else ""


def _greeting_name(lead: dict) -> str:
    """Personal greeting name with graceful fallbacks.

    Priority: real first name -> "<Company> team" -> "there". Placeholder
    names typed into forms ("N/A", "Test", ...) are treated as missing, so
    the email never starts with "Hi N/A,".
    """
    first = _first_name(lead)
    if first and first.lower() not in _PLACEHOLDER_NAMES:
        return first
    company = str(lead.get("company", "")).strip()
    if company:
        return f"{company} team"
    return "there"


def _timeline_phrase(lead: dict) -> str:
    """Natural-language phrasing for the lead's buying timeline."""
    value = str(lead.get("timeline", "")).strip()
    return _TIMELINE_PHRASES.get(value, _DEFAULT_TIMELINE_PHRASE)


def _industry_examples(lead: dict) -> str:
    """Audience-relevant example phrasing for the lead's industry."""
    value = str(lead.get("industry", "")).strip()
    return _INDUSTRY_EXAMPLES.get(value, _DEFAULT_INDUSTRY_EXAMPLES)


def _message_line(lead: dict) -> str:
    """One line referencing the lead's message, or a neutral fallback."""
    message = str(lead.get("message", "")).strip()
    if not message:
        return "It is great to connect with you."
    limit = EMAIL_CONFIG["message_snippet_max"]
    snippet = message if len(message) <= limit else message[: limit - 3].rstrip() + "..."
    return f'In your message you mentioned: "{snippet}"'


def _sender_variables() -> dict[str, str]:
    """Variables that come from EMAIL_CONFIG rather than the lead."""
    sender = EMAIL_CONFIG["sender"]
    return {
        "sender_name": sender["name"],
        "sender_title": sender["title"],
        "sender_contact": sender["contact"],
        "product_name": EMAIL_CONFIG["product_name"],
        "getting_started_url": EMAIL_CONFIG["assets"]["getting_started_url"],
        "overview_video_url": EMAIL_CONFIG["assets"]["overview_video_url"],
    }


def _first_touch_variables(lead: dict) -> dict[str, str]:
    """All variables needed by either first-touch template."""
    return {
        "greeting_name": _greeting_name(lead),
        "company": str(lead.get("company", "")).strip() or "your team",
        "timeline_phrase": _timeline_phrase(lead),
        "message_line": _message_line(lead),
        "industry_examples": _industry_examples(lead),
        **_sender_variables(),
    }


# ===========================================================================
# 7. Public API (see README §3 for the function list)
# ===========================================================================
def draft_first_touch_email(lead: dict, routing: RoutingInfo, score: ScoreResult) -> EmailDraft:
    """Draft the personalized first-touch email for a qualified lead.

    Uses ``routing.email_type`` to pick the template: ``urgent_first_touch``
    for HOT leads (1-hour SLA) and ``standard_first_touch`` for WARM. The
    ``score`` is accepted so every draft stays traceable to the
    qualification decision that produced it.
    """
    template_key = routing.email_type
    if template_key not in ("urgent_first_touch", "standard_first_touch"):
        raise ValueError(
            f"draft_first_touch_email does not handle email_type {template_key!r}; "
            "use draft_nurture_email for nurture leads."
        )
    template = TEMPLATES[template_key]
    variables = _first_touch_variables(lead)
    logger.debug(
        "Drafting %s for %s (score %d)", template_key, lead.get("email"), score.total
    )
    return EmailDraft(
        to=str(lead.get("email", "")).strip(),
        subject=render_template(template["subject"], variables),
        body=render_template(template["body"], variables),
        template=template_key,
    )


def draft_nurture_email(lead: dict) -> EmailDraft:
    """Draft the softer nurture email for a NURTURE-tier lead."""
    template = TEMPLATES["nurture"]
    variables = {
        "greeting_name": _greeting_name(lead),
        "company": str(lead.get("company", "")).strip() or "your team",
        "timeline_phrase": _timeline_phrase(lead),
        **_sender_variables(),
    }
    logger.debug("Drafting nurture email for %s", lead.get("email"))
    return EmailDraft(
        to=str(lead.get("email", "")).strip(),
        subject=render_template(template["subject"], variables),
        body=render_template(template["body"], variables),
        template="nurture",
    )


def draft_internal_alert(lead: dict, result: QualificationResult) -> EmailDraft:
    """Draft the internal heads-up email for a HOT lead.

    Bundles the full qualification decision (score, per-factor breakdown,
    flags, routing, SLA) together with the ready-to-review first-touch
    draft, so the owner can act from a single message.
    """
    if result.routing is None or result.score is None:
        raise ValueError("draft_internal_alert requires a fully processed result.")
    routing, score = result.routing, result.score
    if not routing.internal_alert:
        raise ValueError(
            f"Internal alerts are only drafted for tiers with internal_alert=True "
            f"(tier {routing.tier!r} does not qualify)."
        )

    first_touch = draft_first_touch_email(lead, routing, score)

    # Breakdown in stable rubric order: budget ... source, then domain.
    breakdown_order = [*SCORING_CONFIG["rubric"], "domain"]
    breakdown_line = " · ".join(
        f"{factor} {score.breakdown[factor]}" for factor in breakdown_order
    )
    flags_line = "; ".join(score.flags) if score.flags else "none"

    name = str(lead.get("name", "")).strip()
    if name.lower() in _PLACEHOLDER_NAMES:
        name = ""  # never display "N/A" in an internal alert either
    email = str(lead.get("email", "")).strip()
    lead_line = f"{name} <{email}>" if name and email else (email or name or "unknown lead")

    variables = {
        "lead_line": lead_line,
        "company": str(lead.get("company", "")).strip() or "-",
        "score": str(score.total),
        "tier": routing.tier,
        "breakdown_line": breakdown_line,
        "flags_line": flags_line,
        "owner": routing.owner,
        "queue": routing.queue,
        "sla": routing.sla,
        "drafted_email": first_touch.as_text(),
    }
    template = TEMPLATES["internal_alert"]
    recipient = EMAIL_CONFIG["internal_recipients"].get(
        routing.tier, EMAIL_CONFIG["internal_recipients"]["HOT"]
    )
    logger.debug("Drafting internal alert for %s (%s)", lead.get("email"), routing.tier)
    return EmailDraft(
        to=recipient,
        subject=render_template(template["subject"], variables),
        body=render_template(template["body"], variables),
        template="internal_alert",
    )


__all__ = [
    "EMAIL_CONFIG",
    "EMAIL_VARIABLES",
    "TEMPLATES",
    "EmailDraft",
    "render_template",
    "draft_first_touch_email",
    "draft_nurture_email",
    "draft_internal_alert",
]
