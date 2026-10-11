# Keep integration records outside application branches

Integration must open the PR at the exact head verified and approved by the merge judge. It retains the approved specification and verdicts in Factory’s artifact store and gives the integrator explicit artifact links. `FACTORY_ARTIFACT_BASE_URL` defaults to the owner’s local Console at `http://127.0.0.1:8772`; those links require that service. Hosted session links provide independent provenance. A hosted artifact service can supply a durable base URL.

This supersedes the implementation run-record commit in ADR-0007. Historical records and the optional legacy forge design-gate branch are not rewritten by this change.

An owner may explicitly evaluate a run whose implementation was already merged, against a comparison branch pinned to the original approved baseline. The controller records this instruction without changing approved outcome/spec bytes, checks the pin on resume, binds reviews and judgement to both base and candidate, and forces a labeled draft evaluation PR. An ordinary resume continues to target its recorded repository default branch.
