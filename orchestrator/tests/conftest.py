import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "ledgerline"
SCHEMAS = ROOT / "schemas"


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def schemas_dir() -> Path:
    return SCHEMAS


@pytest.fixture(scope="session")
def examples() -> dict:
    return {p.stem: json.loads(p.read_text()) for p in EXAMPLES.glob("*.json")}
