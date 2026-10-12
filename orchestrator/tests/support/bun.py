"""Bun test evidence in managed verification, shared by the tests that drive it: Bun 1.3.14's own JUnit reports (tests/fixtures), a fake bun that copies one to the path the harness asked for, and a driver whose implementer maps a criterion to a Bun test."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import support.hosted as hosted



FIXTURES = Path(__file__).parent.parent / "fixtures"

REPORT = FIXTURES / "bun-1.3.14-junit.xml"

FILTERED = FIXTURES / "bun-1.3.14-junit-filtered.xml"

MODELS = "src/catalog/models.test.ts"

PASSING_BUN = f"{MODELS}::model catalog > frontier entries > lists the new model with a display name"

FAILING_BUN = f"{MODELS}::model catalog > frontier entries > fails on purpose"

FAKE_BUN = """#!/usr/bin/env python3
import os, shutil, sys
out = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--reporter-outfile=")), None)
with open(os.environ["FAKE_BUN_ARGV"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if out and os.environ.get("FAKE_BUN_REPORT"):
    shutil.copyfile(os.environ["FAKE_BUN_REPORT"], out)
sys.exit(int(os.environ.get("FAKE_BUN_EXIT", "0")))
"""

@pytest.fixture
def fake_bun(tmp_path):
    """A `bun` that records its argv and writes $FAKE_BUN_REPORT to --reporter-outfile; PATH for the harness env."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "bun"
    exe.write_text(FAKE_BUN)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    argv = tmp_path / "bun-argv.log"
    path = f"{bindir}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    return {"PATH": path, "FAKE_BUN_ARGV": str(argv), "FAKE_BUN_REPORT": str(REPORT), "argv": argv}

def env_of(fake, **extra):
    return {"PATH": fake["PATH"], "FAKE_BUN_ARGV": fake["FAKE_BUN_ARGV"], "FAKE_BUN_REPORT": fake["FAKE_BUN_REPORT"], **extra}

BUN_SPEC_COMMANDS = ["python -m pytest -q tests/test_a.py", "bun test src/catalog"]

def bun_spec(commands=None):
    return {"spec_id": "spec_hosted", "work_item_id": "wi-hosted", "title": "catalog", "allowed_paths": ["app/**", "tests/**"],
            "acceptance_criteria": [
                {"ac_id": "AC-1", "user_visible": False, "verification": "automated_test", "given": "g", "when": "w", "then": "t"},
                {"ac_id": "AC-3", "user_visible": False, "verification": "automated_test", "given": "g", "when": "w", "then": "t"}],
            "_markdown": "# spec", "test_plan": {"commands": list(commands or BUN_SPEC_COMMANDS), "e2e": []}}

class BunDriver(hosted.Driver):
    def __init__(self, *args, bun_tests, **kwargs):
        super().__init__(*args, **kwargs)
        self.report = {"criteria": [{"ac_id": "AC-1", "tests": ["tests/test_a.py::test_a"]}, {"ac_id": "AC-3", "tests": bun_tests}],
                       "head_commit": "x"}

    def run(self, **kw):
        if kw.get("stage") == "verify" or kw["agent_key"] != "implementer":
            return super().run(**kw)
        self.calls.append(kw)
        hosted.push(self.origin, self.scratch, f"factory/{hosted.RUN_ID}/spec_hosted", self.change_files, len(self.calls))
        outputs = {"implementation_report.json": json.dumps(self.report).encode()}
        if "findings.json" in kw["uploads"]:
            answered = json.loads(kw["uploads"]["findings.json"])["findings"]
            outputs["review_response.json"] = json.dumps({"responses": [{"finding_id": f["finding_id"], "disposition": "fixed",
                                                                        "explanation": "done"} for f in answered]}).encode()
        return hosted.result(outputs)
