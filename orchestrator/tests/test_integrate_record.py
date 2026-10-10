"""The run record committed on the implementation branch before the change request is opened.
Uses a local bare 'origin' and a working clone; no network."""
import json
import subprocess
from pathlib import Path

from factory.stages.integrate import commit_run_record, run_record_paths

RUN = "run_rec0000001"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "dev@example.com"); _git(work, "config", "user.name", "dev")
    (work / "app.py").write_text("print('hi')\n")
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "checkout", "-q", "-b", "factory/run_rec0000001/spec_smart-retries")
    (work / "retry.py").write_text("x = 1\n")
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "implement")
    _git(work, "push", "-q", "-u", "origin", "factory/run_rec0000001/spec_smart-retries")
    return origin, work


def test_record_commit_is_deterministic_and_pushed(tmp_path, examples):
    origin, work = _repo(tmp_path)
    spec = examples["spec_smart_retries"]
    judged = _git(work, "rev-parse", "HEAD")
    merge_verdict = {**examples["merge_readiness"], "_head_sha": judged, "_branch": "factory/run_rec0000001/spec_smart-retries", "_base_sha": "x"}
    sha = commit_run_record(work, RUN, spec, "# Spec\n", examples["verdict_round2"], merge_verdict)
    assert sha != judged and _git(work, "rev-parse", "HEAD") == sha
    paths = run_record_paths(RUN, spec["spec_id"])
    for key, rel in paths.items():
        assert (work / rel).exists(), key
    assert json.loads((work / paths["spec_json"]).read_text())["spec_id"] == spec["spec_id"]
    recorded = json.loads((work / paths["merge_readiness"]).read_text())
    assert "_head_sha" not in recorded and recorded["result"] == examples["merge_readiness"]["result"]
    assert json.loads((work / paths["verdict"]).read_text())["round"] == examples["verdict_round2"]["round"]
    # pushed: origin's branch head equals the new sha
    assert _git(origin, "rev-parse", "refs/heads/factory/run_rec0000001/spec_smart-retries") == sha
    # only .factory/ changed, and the commit identity and date are fixed
    assert _git(work, "diff", "--name-only", f"{judged}..{sha}").splitlines() == sorted(paths.values())
    log = _git(work, "log", "-1", "--format=%an|%ae|%cn|%ci", sha)
    assert log.startswith("factory|factory@localhost|factory|")
    assert judged in _git(work, "log", "-1", "--format=%B", sha)
    # idempotent: the same inputs produce no second commit
    assert commit_run_record(work, RUN, spec, "# Spec\n", examples["verdict_round2"], merge_verdict) == sha
    # deterministic: a fresh clone at the judged head produces the same sha for the same inputs
    work2 = tmp_path / "work2"
    subprocess.run(["git", "clone", "-q", "-b", "factory/run_rec0000001/spec_smart-retries", str(origin), str(work2)], check=True)
    _git(work2, "reset", "-q", "--hard", judged)
    sha2 = commit_run_record(work2, RUN, spec, "# Spec\n", examples["verdict_round2"], merge_verdict, push=False)
    assert sha2 == sha
