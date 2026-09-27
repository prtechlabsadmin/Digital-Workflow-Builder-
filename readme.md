# Digital Workflow Builder — Automated Lead Qualification & Follow-up System

> Automated, explainable lead scoring + routing + first-touch email drafting — so sales teams respond to their best leads in minutes, not hours.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## 1. Description

The Digital Workflow Builder is an automated lead-qualification and follow-up system designed for small and mid-size sales and marketing operations teams. It streamlines inbound lead management by ingesting raw lead submissions (currently via CSV, with webhook-ready architecture for n8n or Make.com).

The system scores every lead from 0 to 100 using a transparent, 7-factor rubric, classifies them into HOT, WARM, or NURTURE tiers, routes them to the appropriate owner with a defined SLA, and instantly generates personalized first-touch email drafts. This replaces hours of manual triage with a deterministic, fully auditable pipeline that keeps a human firmly in the loop before any external communication is sent.

---

## 2. Flow Chart / Architecture

<img src="https://res.cloudinary.com/kyizqhwl/image/upload/f_auto,q_auto/watermark-removed-Gemini_Generated_Image_d7bwned7bwned7bw" alt="Architecture Diagram" width="100%" />

### Stage-by-Stage Breakdown

- **Data source** — Inbound lead form exports as CSV. `data/sample_leads.csv` ships with 20 realistic leads, deliberately including edge cases (missing budget, a duplicate, free-mail domains, unknown industry) so the pipeline's error handling is demonstrable. A form webhook (n8n / Make.com HTTP node) is a drop-in future source.
- **Ingestion** — `load_leads()` reads the CSV and `validate_lead()` checks required fields (name, email, company). Invalid rows are routed to an exception queue with a logged reason — a bad row never crashes the batch.
- **Processing** — `normalize_lead()` trims, lowercases, and maps raw form values to canonical categories (e.g., `"50K+"` → `"50000+"`), so scoring is deterministic and idempotent. `is_duplicate()` deduplicates by email within a batch.
- **Model / retrieval / agent** — A deterministic, explainable **rules engine** (`score_lead()`), not ML — by design. Sales leadership required auditability: every score ships with a per-factor breakdown. Seven weighted factors sum to a 0–100 score:

| # | Factor | Max pts | Scoring detail |
|---|---|---|---|
| 1 | Budget range | 25 | 50k+ → 25 · 10–50k → 15 · <10k → 5 · unknown → 0 |
| 2 | Seniority (authority) | 20 | C-level → 20 · VP → 15 · Manager → 10 · IC → 3 |
| 3 | Company size | 15 | 500+ → 15 · 50–499 → 10 · 1–49 → 5 |
| 4 | Timeline | 15 | Immediate → 15 · 1–3 months → 10 · Exploring → 3 |
| 5 | Industry fit | 10 | SaaS / Fintech → 10 · E-commerce → 8 · Other → 3 |
| 6 | Lead source | 10 | Referral → 10 · Webinar → 7 · Website → 5 · Cold → 2 |
| 7 | Email domain type | 5 | Business domain → 5 · Free-mail → 0 |
| | **Total** | **100** | All weights live in one config dict — tunable without touching logic |

- **Evaluation** — 25+ pytest unit tests covering each scoring factor, tier boundaries (39/40 and 69/70), routing, duplicates/invalids, template rendering, plus a golden end-to-end run on the sample CSV asserting the exact tier distribution. Every lead also carries its own score breakdown as a built-in audit artifact (see `docs/stakeholder_update.md`).
- **Deployment** — One file, two modes: `python app.py` (CLI demo) and `streamlit run app.py` (interactive web demo). Free hosting via Streamlit Community Cloud; production pattern documented in §7.5 (scheduled runs via n8n/cron).
- **Monitoring** — Python `logging` at every stage: INFO for pipeline events, WARNING for exception-queue rows, DEBUG for per-factor score breakdowns. `summarize_results()` produces a run summary (tier counts + exceptions) that feeds the stakeholder update — the weekly report is generated, not hand-written.

### Routing Map

| Tier | Score | Routed to | SLA | Email generated |
|---|---|---|---|---|
| 🔥 HOT | 70–100 | Senior Account Executive | 1 hour | Urgent first-touch + internal alert |
| ⚡ WARM | 40–69 | Account Executive | 24 hours | Standard first-touch |
| 🌱 NURTURE | 0–39 | Marketing nurture list | Weekly digest | Nurture email |

---

## 3. Function List

### `src/workflow_engine.py` — Core qualification & routing logic

| Function | Signature | Purpose |
|---|---|---|
| `load_leads` | `(csv_path: str) -> list[dict]` | Read lead CSV; friendly error on missing/empty file |
| `validate_lead` | `(lead: dict) -> tuple[bool, list[str]]` | Check required fields; return list of reasons |
| `normalize_lead` | `(lead: dict) -> dict` | Clean + standardize fields to canonical categories |
| `is_duplicate` | `(lead: dict, seen_emails: set[str]) -> bool` | Detect repeat emails within a batch |
| `score_lead` | `(lead: dict) -> ScoreResult` | Apply 7-factor rubric; return total + per-factor breakdown |
| `classify_lead` | `(total: int) -> str` | Map score → `HOT` / `WARM` / `NURTURE` via thresholds |
| `route_lead` | `(tier: str) -> RoutingInfo` | Return owner, SLA, queue, and email type per routing map |
| `process_leads` | `(leads: list[dict]) -> list[QualificationResult]` | Orchestrator: validate → normalize → dedupe → score → classify → route; batch never crashes |
| `build_audit_trail` | `(results: list[QualificationResult]) -> list[dict]` | Flatten results into audit rows for CSV export |
| `summarize_results` | `(results: list[QualificationResult]) -> dict` | Tier counts + exception counts for the stakeholder report |

### `src/email_templates.py` — Professional email generators

| Function | Signature | Purpose |
|---|---|---|
| `render_template` | `(template: str, variables: dict[str, str]) -> str` | Safe `{{variable}}` substitution; unknown variables stay visible for review |
| `draft_first_touch_email` | `(lead: dict, routing: RoutingInfo, score: ScoreResult) -> EmailDraft` | Personalized urgent/standard first-touch (subject + body) |
| `draft_nurture_email` | `(lead: dict) -> EmailDraft` | Softer nurture-sequence email for low-score leads |
| `draft_internal_alert` | `(lead: dict, result: QualificationResult) -> EmailDraft` | Internal heads-up to the owner for HOT leads |
| `EMAIL_VARIABLES` | `dict[str, list[str]]` | Self-documenting list of variables each template expects |

### `app.py` — Demo interface (CLI + Streamlit)

| Function | Purpose |
|---|---|
| `run_pipeline` | Wraps the engine for both CLI and Streamlit modes; returns results + summary |
| `render_cli_report` | Terminal output: scored table, tier summary, one sample email draft |
| `main` | Streamlit UI: sample data / CSV upload → run → tabs for Results, Email drafts, Audit trail, Exceptions |

---

## 4. Code Hygiene

- **Type hints** — every public function is fully annotated (`list[dict]`, `tuple[bool, list[str]]`, dataclasses `ScoreResult`, `RoutingInfo`, `EmailDraft`, `QualificationResult`). Example:
  ```python
  def score_lead(lead: dict) -> ScoreResult:
      """Score a normalized lead using the 7-factor rubric."""
      logger.debug("Scoring lead %s", lead.get("email"))
      ...
  ```
- **Unit tests** — pytest suite in `tests/`: per-factor scoring math, tier boundary values (39/40, 69/70), routing map correctness, duplicate + invalid handling, template variable substitution, and a golden end-to-end run on `data/sample_leads.csv`.
- **Linting** — `ruff check src app.py` + `black --check .` (single-command, zero-config defaults).
- **Config files** — the entire business logic configuration (rubric weights, tier thresholds, routing map, owner names) lives in one clearly-commented `SCORING_CONFIG` block at the top of `src/workflow_engine.py`. A stakeholder can tune the rules without touching any logic.
- **Environment variables** — zero secrets required: the project runs fully offline and only *drafts* emails (human-in-the-loop by design). Optional `WORKFLOW_LOG_LEVEL` controls verbosity; SMTP variables are documented but reserved for the future "send" extension.
- **Logging** — stdlib `logging`, module-level loggers, consistent format (`timestamp | level | module | message`). Exception rows are logged with a reason and skipped — never crash the batch.
- **Reproducible setup** — deterministic scoring (no randomness anywhere), pinned `requirements.txt`, bundled sample data, venv instructions. The same input always produces byte-identical output, so the golden test and CI runs are stable.

---

## 5. UI 

image 1,2,3,4,5

---

## 6. Results

Measured on the bundled 20-lead sample (Python 3.11, laptop). Baseline was estimated by manually triaging the same 20 leads once — the intended "before" state of the workflow.

### Metric comparison

| Metric | Baseline — manual | After — automated |
|---|---|---|
| Triage time per lead | ~12–15 min | < 5 ms |
| First-touch draft availability | 6–8 business hours | Instant — ready for review on arrival |
| Routing decisions | Person-dependent, inconsistent | 100% deterministic, rule-based |
| Leads with full audit trail | 0% | 100% (per-factor score breakdown + decision log) |
| Pipeline hygiene | ~30% of AE time spent on low-fit leads (est.) | Low-fit leads auto-routed to nurture |

- **Latency** (20 leads): load + validate ≈ 15 ms · score + route all 20 ≈ 10 ms · drafts for all 20 ≈ 15 ms · **end-to-end incl. audit CSV < 0.5 s**
- **Cost** — $0 recurring: pure Python, no external APIs, no LLM tokens (template-based drafts are free and safe to review). Optional hosting on the free Streamlit Community Cloud tier.

### Failure cases handled (each covered by a unit test)

| Failure case | Behavior |
|---|---|
| Missing budget / timeline fields | Factor scored 0 + `data_incomplete` flag; lead still processed |
| Duplicate lead (same email) | Skipped, logged to exception queue with reason |
| Free-mail domain (gmail/yahoo/…) | 0 domain points + verification flag |
| Unknown industry value | Falls to lowest weight + flag — never crashes |
| Malformed / empty CSV row | Row rejected with logged reason; batch continues |
| All-minimum lead (score 0) | Cleanly routed to nurture |
| HOT lead with missing first name | Template falls back to company greeting |

---

## 7. How to Run

### 7.1 Setup

Requires **Python 3.10+** and git.

```bash
git clone https://github.com/<your-username>/digital-workflow-builder.git
cd digital-workflow-builder
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
```

#### Project Structure

```text
.
├── data/
│   └── sample_leads.csv        # 20 realistic leads (incl. edge cases)
├── src/
│   ├── __init__.py             # Python package marker
│   ├── workflow_engine.py      # Core rules engine, scoring, and pipeline orchestration
│   └── email_templates.py      # Template rendering and email draft generators
├── tests/
│   ├── test_workflow_engine.py # Unit tests for validation, scoring, and routing
│   └── test_email_templates.py # Unit tests for variable substitution and drafts
├── docs/
│   ├── process_map.md          # Detailed Mermaid architecture and workflow guide
│   └── stakeholder_update.md   # Hand-over template and weekly report pack
├── screenshots/                # UI captures for documentation (§5)
├── output/                     # Generated audit trail CSVs (created at runtime)
├── app.py                      # Dual-mode interface (Streamlit web app + CLI runner)
├── requirements.txt            # Pinned Python package dependencies
└── README.md                   # Complete specification and documentation
```

#### Input CSV schema (for your own leads)

| Column | Example | Required |
|---|---|---|
| `name` | `Priya Sharma` | ✅ |
| `email` | `priya@acmecorp.com` | ✅ |
| `company` | `Acme Corp` | ✅ |
| `budget` | `50000+` / `10000-49999` / `under-10000` / *(blank)* | — |
| `employees` | `500+` / `50-499` / `1-49` | — |
| `industry` | `saas` / `fintech` / `ecommerce` / `other` | — |
| `seniority` | `cxo` / `vp` / `manager` / `ic` | — |
| `timeline` | `immediate` / `quarter` / `exploring` | — |
| `source` | `referral` / `webinar` / `website` / `cold` | — |
| `message` | free text | — |

### 7.2 Install

```bash
pip install -r requirements.txt
```

### 7.3 Run locally

```bash
# Option A — interactive web demo (upload your own CSV or use the sample)
streamlit run app.py

# Option B — CLI demo on the bundled sample data
python app.py
```

Expected CLI output:

```text
Processed 20 leads in 0.04s
  HOT     :  4 leads → Senior AE (SLA: 1h)
  WARM    :  9 leads → Account Executive (SLA: 24h)
  NURTURE :  5 leads → Marketing nurture
  Skipped :  2 leads (1 duplicate, 1 invalid)
Audit trail → output/audit_trail.csv
```

### 7.4 Run tests

```bash
pytest -v
ruff check src app.py
black --check .
```

### 7.5 Deploy

1. **Free hosted demo (Streamlit Community Cloud):** push to GitHub → [share.streamlit.io](https://share.streamlit.io) → connect the repo → set main file to `app.py` → share the public URL.
2. **Production pattern (n8n / Make.com):** lead form webhook → HTTP/schedule trigger invokes the pipeline → audit CSV written to a shared drive → email drafts queued in a review step → approved drafts sent via an SMTP node. Sending stays human-approved by design.
