"""Demo interface for the Digital Workflow Builder.

One file, two modes (README §7.3):

    python app.py          -> CLI demo on the bundled sample data
    streamlit run app.py   -> interactive web demo

Both modes share run_pipeline(), so the demo always exercises exactly
the same engine code path that production would. The Streamlit import
is deliberately lazy: the CLI works even where Streamlit isn't installed.
"""

from __future__ import annotations

import csv
import io
import logging
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — make `src` importable no matter where this script is launched
# from (python app.py, streamlit run app.py, or an IDE runner).
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.email_templates import (  # noqa: E402  (deliberately after sys.path tweak)
    EmailDraft,
    draft_first_touch_email,
    draft_internal_alert,
    draft_nurture_email,
)
from src.workflow_engine import (  # noqa: E402
    SCORING_CONFIG,
    QualificationResult,
    build_audit_trail,
    load_leads,
    process_leads,
    summarize_results,
)

logger = logging.getLogger("app")

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_leads.csv"
OUTPUT_DIR = PROJECT_ROOT / "output"
AUDIT_CSV = OUTPUT_DIR / "audit_trail.csv"

# Compact tier display for the CLI summary — the exact strings README §7.3
# promises on screen.
CLI_TIER_DISPLAY: dict[str, str] = {
    "HOT": "Senior AE (SLA: 1h)",
    "WARM": "Account Executive (SLA: 24h)",
    "NURTURE": "Marketing nurture",
}


# ===========================================================================
# Shared pipeline wrapper (used by BOTH the CLI and Streamlit modes)
# ===========================================================================
def run_pipeline(csv_path: str | Path) -> tuple[list[QualificationResult], dict]:
    """Run the full workflow on a CSV file: load -> process -> summarize.

    Also writes the audit trail to ``output/audit_trail.csv``. Returns
    (results, summary); the summary additionally carries
    ``elapsed_seconds`` and ``audit_csv`` (display path of the written
    audit trail, or None if it could not be written).
    """
    leads = load_leads(str(csv_path))

    start = time.perf_counter()
    results = process_leads(leads)
    elapsed = time.perf_counter() - start

    summary = summarize_results(results)
    summary["elapsed_seconds"] = round(elapsed, 4)
    summary["audit_csv"] = _write_audit_csv(results)
    logger.info("Pipeline complete: %s", summary)
    return results, summary


def _write_audit_csv(results: list[QualificationResult]) -> str | None:
    """Write the audit trail to output/audit_trail.csv; return display path."""
    rows = build_audit_trail(results)
    if not rows:
        return None
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with AUDIT_CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        try:
            display_path: Path = AUDIT_CSV.relative_to(PROJECT_ROOT)
        except ValueError:  # audit written outside the project (e.g. tests)
            display_path = AUDIT_CSV
        logger.info("Audit trail written to %s", display_path)
        return str(display_path)
    except OSError as exc:
        logger.warning("Audit trail could not be written (%s) — run continues.", exc)
        return None


# ===========================================================================
# CLI report
# ===========================================================================
def render_cli_report(results: list[QualificationResult], summary: dict, source: str) -> str:
    """Build the full terminal report as a string (printed by the CLI).

    Contains: scored table -> exception queue -> tier summary (README §7.3
    exact format) -> one sample email draft -> audit trail location.
    Returned as a string so the output itself is unit-testable.
    """
    lines: list[str] = []
    rule = "=" * 68

    lines.append(rule)
    lines.append(" Digital Workflow Builder — Lead Qualification Demo")
    lines.append(f" Source: {source}")
    lines.append(rule)

    # ---- scored table -------------------------------------------------
    processed = [r for r in results if r.status == "PROCESSED"]
    lines.append("")
    lines.append(f"-- Qualified leads ({len(processed)}) " + "-" * 45)
    lines.append(f"{'Lead':<24}{'Company':<24}{'Score':>5}  {'Tier':<8}Owner / SLA")
    for result in processed:
        lead, routing = result.lead, result.routing
        lines.append(
            f"{str(lead.get('name', ''))[:23]:<24}"
            f"{str(lead.get('company', ''))[:23]:<24}"
            f"{result.score.total:>5}  "
            f"{routing.tier:<8}"
            f"{routing.owner} ({routing.sla})"
        )

    # ---- exception queue ----------------------------------------------
    skipped = [r for r in results if r.status == "SKIPPED"]
    lines.append("")
    lines.append(f"-- Exception queue ({len(skipped)}) " + "-" * 43)
    if skipped:
        for result in skipped:
            who = result.lead.get("name") or result.lead.get("email") or "unknown"
            company = result.lead.get("company", "")
            where = f" ({company})" if company else ""
            lines.append(f"  - {who}{where} — {result.skip_reason}")
    else:
        lines.append("  (empty — every row processed cleanly)")

    # ---- tier summary (README §7.3 exact format) -----------------------
    lines.append("")
    lines.append(f"Processed {summary['total']} leads in {summary['elapsed_seconds']:.2f}s")
    for tier in ("HOT", "WARM", "NURTURE"):
        count = summary["tiers"][tier]
        lines.append(f"  {tier:<9}: {count:>2} leads → {CLI_TIER_DISPLAY[tier]}")
    skip_detail = ", ".join(f"{n} {reason}" for reason, n in summary["skip_reasons"].items())
    skipped_line = f"  {'Skipped':<9}: {summary['skipped']:>2} leads"
    if skip_detail:
        skipped_line += f" ({skip_detail})"
    lines.append(skipped_line)

    # ---- sample email draft ---------------------------------------------
    lines.append("")
    lines.append("-- Sample email draft (first HOT lead) " + "-" * 31)
    sample = _pick_sample_result(processed)
    if sample is not None:
        lines.append(_draft_for_result(sample).as_text())
        lines.append("")
        lines.append(f"(All {len(processed)} drafts are generated — view any of them")
        lines.append(" in the Streamlit demo: streamlit run app.py)")
    else:
        lines.append("  (no processed leads — nothing to draft)")

    # ---- audit trail -----------------------------------------------------
    lines.append("")
    lines.append(f"Audit trail → {summary.get('audit_csv') or 'not written (see logs)'}")
    lines.append(rule)
    return "\n".join(lines)


def _pick_sample_result(processed: list[QualificationResult]) -> QualificationResult | None:
    """First HOT lead if any, else the first processed lead."""
    if not processed:
        return None
    for result in processed:
        if result.routing.tier == "HOT":
            return result
    return processed[0]


def _draft_for_result(result: QualificationResult) -> EmailDraft:
    """Draft the right email for a processed result (template from routing)."""
    if result.routing.email_type == "nurture":
        return draft_nurture_email(result.lead)
    return draft_first_touch_email(result.lead, result.routing, result.score)


# ===========================================================================
# Streamlit web demo
# ===========================================================================
def _is_running_under_streamlit() -> bool:
    """True when this file is executed by `streamlit run app.py`."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx  # type: ignore[attr-defined]

        return get_script_run_ctx() is not None
    except Exception:  # streamlit not installed, or internal API moved
        return False


def main() -> None:
    """Streamlit demo UI (README §5): data source -> run -> four tabs."""
    import streamlit as st

    st.set_page_config(page_title="Digital Workflow Builder", page_icon="⚙️", layout="wide")
    st.title("⚙️ Digital Workflow Builder")
    st.caption(
        "Automated Lead Qualification & Follow-up — validate → score (7 factors, "
        "0–100) → route (HOT / WARM / NURTURE) → draft follow-up emails. "
        "Human-in-the-loop: drafts are reviewed by people, never auto-sent."
    )

    # ---- sidebar: data source + live view of the scoring rules ----------
    with st.sidebar:
        st.header("Data source")
        use_sample = st.button("Use sample data (20 leads)", type="primary")
        upload = st.file_uploader("…or upload your own leads CSV", type=["csv"])

        with st.expander("How scoring works (live from config)"):
            table = "| Factor | Max pts |\n|---|---|"
            for factor, points in SCORING_CONFIG["rubric"].items():
                table += f"\n| {factor} | {max(points.values())} |"
            table += f"\n| email domain | {max(SCORING_CONFIG['domain_points'].values())} |"
            st.markdown(table)
            thresholds = SCORING_CONFIG["tier_thresholds"]
            st.markdown(
                f"**Tiers:** HOT ≥ {thresholds['hot']} · WARM ≥ {thresholds['warm']} · "
                "otherwise NURTURE"
            )

    # ---- run the pipeline, and remember it across widget reruns ---------
    if "run" not in st.session_state:
        st.session_state["run"] = None

    def _execute(csv_path: Path, label: str) -> None:
        try:
            results, summary = run_pipeline(csv_path)
        except (FileNotFoundError, ValueError) as exc:
            st.error(f"Could not load leads: {exc}")
            return
        st.session_state["run"] = {"results": results, "summary": summary, "label": label}

    if use_sample:
        _execute(SAMPLE_CSV, "sample data (data/sample_leads.csv)")
    elif upload is not None:
        signature = (upload.name, upload.size)
        if st.session_state.get("upload_signature") != signature:
            st.session_state["upload_signature"] = signature
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            upload_path = OUTPUT_DIR / "uploaded_leads.csv"
            upload_path.write_bytes(upload.getvalue())
            _execute(upload_path, f"upload ({upload.name})")

    run = st.session_state["run"]
    if run is None:
        st.info(
            "Click **Use sample data** in the sidebar (or upload a CSV matching the "
            "schema in README §7.1) to run the workflow on realistic leads — "
            "including a duplicate, missing fields and free-mail addresses."
        )
        return

    results: list[QualificationResult] = run["results"]
    summary: dict = run["summary"]
    processed = [r for r in results if r.status == "PROCESSED"]
    skipped = [r for r in results if r.status == "SKIPPED"]

    st.success(
        f"Processed **{summary['processed']} of {summary['total']}** leads from "
        f"{run['label']} in **{summary['elapsed_seconds']:.2f}s** · "
        f"average score **{summary['average_score']}**"
    )

    cols = st.columns(5)
    cols[0].metric("Processed", summary["processed"])
    cols[1].metric("🔥 HOT", summary["tiers"]["HOT"], "respond within 1h", delta_color="off")
    cols[2].metric("⚡ WARM", summary["tiers"]["WARM"], "respond within 24h", delta_color="off")
    cols[3].metric("🌱 NURTURE", summary["tiers"]["NURTURE"], "weekly digest", delta_color="off")
    cols[4].metric("Skipped", summary["skipped"], "exception queue", delta_color="off")

    tab_results, tab_drafts, tab_audit, tab_exceptions = st.tabs(
        ["Results", "Email drafts", "Audit trail", "Exceptions"]
    )

    # ---- tab 1: results ---------------------------------------------------
    with tab_results:
        import pandas as pd  # pandas ships with streamlit — no extra dep

        tiers = summary["tiers"]
        st.bar_chart(
            pd.DataFrame(
                {"Tier": ["HOT", "WARM", "NURTURE"],
                 "Leads": [tiers["HOT"], tiers["WARM"], tiers["NURTURE"]]}
            ),
            x="Tier",
            y="Leads",
        )
        table = [
            {
                "Lead": r.lead.get("name", ""),
                "Company": r.lead.get("company", ""),
                "Email": r.lead.get("email", ""),
                "Score": r.score.total,
                "Tier": {"HOT": "🔥 HOT", "WARM": "⚡ WARM", "NURTURE": "🌱 NURTURE"}[r.routing.tier],
                "Owner": r.routing.owner,
                "SLA": r.routing.sla,
                "Flags": "; ".join(r.score.flags) or "—",
            }
            for r in processed
        ]
        st.dataframe(table, hide_index=True)
        st.caption("The per-factor score breakdown for every lead lives in the Audit trail tab.")

    # ---- tab 2: email drafts ----------------------------------------------
    with tab_drafts:
        if not processed:
            st.warning("No processed leads — nothing to draft.")
        else:
            labels = [
                f"{r.lead.get('name', 'unknown')} — {r.lead.get('company', '')} "
                f"({r.score.total} · {r.routing.tier})"
                for r in processed
            ]
            default = next((i for i, r in enumerate(processed) if r.routing.tier == "HOT"), 0)
            choice = st.selectbox("Choose a lead to preview its drafted email", labels, index=default)
            result = processed[labels.index(choice)]

            draft = _draft_for_result(result)
            st.caption(
                f"Template: `{draft.template}` — this is a DRAFT for human review; "
                "nothing is sent automatically."
            )
            st.code(draft.as_text(), language="text")

            if result.routing.internal_alert:
                with st.expander("Internal alert (also drafted for HOT leads)"):
                    st.code(draft_internal_alert(result.lead, result).as_text(), language="text")

    # ---- tab 3: audit trail ------------------------------------------------
    with tab_audit:
        rows = build_audit_trail(results)
        st.dataframe(rows, hide_index=True)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        st.download_button(
            "Download audit trail (CSV)",
            data=buffer.getvalue(),
            file_name="audit_trail.csv",
            mime="text/csv",
        )
        if summary.get("audit_csv"):
            st.caption(f"Also written to disk on every run: `{summary['audit_csv']}`")

    # ---- tab 4: exceptions -------------------------------------------------
    with tab_exceptions:
        if not skipped:
            st.success("No exceptions — every row processed cleanly.")
        else:
            st.warning(
                f"{len(skipped)} row(s) quarantined. Bad data never crashes the batch — "
                "each skip is logged (WARNING) with its reason."
            )
            st.dataframe(
                [
                    {
                        "Lead": r.lead.get("name", "") or r.lead.get("email", "") or "unknown",
                        "Company": r.lead.get("company", ""),
                        "Reason": r.skip_reason,
                    }
                    for r in skipped
                ],
                hide_index=True,
            )

    st.divider()
    st.caption(
        "Drafts are generated for human review — nothing is sent automatically. "
        "See docs/stakeholder_update.md for the full communication pack."
    )


# ===========================================================================
# Entry point — one file, two modes
# ===========================================================================
if __name__ == "__main__":
    if _is_running_under_streamlit():
        main()  # launched via `streamlit run app.py`
    else:
        # CLI demo on the bundled sample data (README §7.3)
        try:
            cli_results, cli_summary = run_pipeline(SAMPLE_CSV)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Could not run demo: {exc}")
            sys.exit(1)
        source = str(SAMPLE_CSV.relative_to(PROJECT_ROOT))
        print(render_cli_report(cli_results, cli_summary, source))
