"""Smoke tests for the CLI demo (app.py) — pins README §7.3's exact output."""

import pytest

import app  # noqa: E402  (importable thanks to conftest.py at the project root)


@pytest.fixture()
def cli_run(tmp_path, monkeypatch):
    """Run the full demo pipeline with the audit file redirected to tmp."""
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "AUDIT_CSV", tmp_path / "audit_trail.csv")
    results, summary = app.run_pipeline(app.SAMPLE_CSV)
    report = app.render_cli_report(results, summary, "data/sample_leads.csv")
    return results, summary, report


class TestCliReport:
    def test_summary_lines_match_readme_exactly(self, cli_run):
        _, _, report = cli_run
        assert "Processed 20 leads in" in report
        assert "  HOT      :  4 leads → Senior AE (SLA: 1h)" in report
        assert "  WARM     :  9 leads → Account Executive (SLA: 24h)" in report
        assert "  NURTURE  :  5 leads → Marketing nurture" in report
        assert "  Skipped  :  2 leads (1 duplicate, 1 invalid)" in report

    def test_report_includes_table_sample_draft_and_exceptions(self, cli_run):
        _, _, report = cli_run
        assert "Priya Sharma" in report                      # scored table
        assert "Subject: Next steps for Acme Corp — quick question" in report
        assert "duplicate: sarah.lim@zenithretail.com" in report
        assert "missing required field: email" in report

    def test_audit_trail_line_present(self, cli_run):
        _, _, report = cli_run
        assert "Audit trail → " in report


class TestRunPipeline:
    def test_end_to_end_counts_and_audit_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(app, "AUDIT_CSV", tmp_path / "audit_trail.csv")
        results, summary = app.run_pipeline(app.SAMPLE_CSV)
        assert len(results) == 20
        assert summary["tiers"] == {"HOT": 4, "WARM": 9, "NURTURE": 5}
        assert summary["audit_csv"] == str(tmp_path / "audit_trail.csv")
        assert (tmp_path / "audit_trail.csv").exists()
        assert summary["elapsed_seconds"] >= 0

    def test_missing_file_raises_friendly_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            app.run_pipeline(tmp_path / "nope.csv")
