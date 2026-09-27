"""Shared pytest fixtures for the Digital Workflow Builder test suite.

Sitting at the project root, this file also guarantees that the project
root is on sys.path, so `from src... import ...` works no matter how
pytest is invoked (README §7.4).
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.workflow_engine import load_leads, process_leads  # noqa: E402

SAMPLE_CSV = PROJECT_ROOT / "data" / "sample_leads.csv"


@pytest.fixture(scope="session")
def sample_results():
    """The full pipeline over the bundled 20-lead sample — runs once."""
    return process_leads(load_leads(str(SAMPLE_CSV)))
