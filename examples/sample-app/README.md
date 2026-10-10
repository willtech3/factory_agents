# Sample app: a small todo service

A deliberately small FastAPI service the factory can change end to end: list, create, complete, and delete
todos, in memory, with a test suite that runs in under two seconds. It exists so the first real run has a
real repository with real tests, a manifest, and a preview workflow, and an outcome worth decomposing.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/uvicorn app.main:app --reload     # http://127.0.0.1:8000/docs
```

## Use it as the factory's target

1. Create an empty repository on your GitHub (for example `factory-sample-app`) and push this directory to it:
   ```bash
   cp -r examples/sample-app ~/factory-sample-app && cd ~/factory-sample-app
   git init -b main && git add -A && git commit -m "Sample todo service" && gh repo create factory-sample-app --private --source . --push
   ```
2. In the Factory Console (`factory web`), open Outcomes and choose **Load the sample app outcome**: it fills
   the form from this directory's `outcome.json`. Set the repository URL and the owner handle to yours and save.
   From a terminal the equivalent is `factory run examples/sample-app/outcome.json --design-only` after editing
   those two fields in the file.
3. Start a **design run** first (needs only `ANTHROPIC_API_KEY`): the decomposer, spec writers, and judge run
   as real sessions and you decide Gates 1 and 2. Then a **full run** with `FORGE_REPO` and `FORGE_TOKEN`:
   agents implement on branches, the preview workflow records a deployment, and pull requests open.

`.github/workflows/preview.yml` is the factory's preview template with a placeholder deploy step; the
Deployment it records is what the orchestrator reads for the preview URL. Replace the step with a real
deploy when you want the end-to-end checks to hit a running instance.
