# Switchboard: a greenfield outcome

The factory's second live test, and the first on a repository that does not exist yet: `target_repositories[0].create` is `true`, so `factory run` creates `willtech3/factory-switchboard` (private), seeds the platform-contract skill and the preview workflow, and the agents scaffold a new FastAPI feature-flag service from nothing.

`outcome.json` was produced by a small design workflow (three independent product proposals, one judge) and then pinned down so that nothing in it is left for the implementer to invent: the bucketing function is fixed, four worked examples are given with their exact buckets, and the 20% rollout over 10,000 subjects has an exact count. The judge's rationale: the product reads like a real internal tool, every rule is a test or a curl, and release 1 fits one implementer session.

Start it with `factory run examples/switchboard/outcome.json`. The Console has no example loader; to start it there, enter the outcome under Outcomes → **New outcome**.
