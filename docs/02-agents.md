# Agents

The roster, what each agent reads and writes, and why the boundaries sit where they do. The definitions themselves are in [`agents/`](../agents/) (one `ant apply` file per agent, frontmatter plus system prompt). This page is the design rationale.

## Roster at a glance

| Agent | Reads | Writes | Must not | Independent of |
|---|---|---|---|---|
| **Codebase researcher** | one factual question; the repo | a short evidence-backed report | be told the intended change | the goal |
| **Decomposer** | outcome, repo (via researchers), prior feedback, platform-contract skill | `decomposition.json/.md`; `ask_human` | write specs or code; exceed 25 items; make reserved platform work a work item | - |
| **Spec writer** | decomposition, its work item, sibling specs, judge issues, human comments, repo (via researchers), platform-contract skill | `spec.json/.md` (with `user_visible` flags and `test_plan.e2e`); `ask_human` | write code; grade itself; specify what the contract reserves | - |
| **Spec judge** | all specs (JSON only), rubric, outcome, decomposition, previous verdict, repo, platform-contract skill and service manifest | `verdict.json` (S1 to S12) | edit specs; infer intent charitably; reward length | the writer's session and reasoning |
| **Implementer** | spec, dependency specs, repo on its branch, `preview.json`, findings, platform-contract skill | code, tests, commits, `implementation_report.json` (with `e2e_results`), `review_response.json`, `blocked.md` | touch paths outside `allowed_paths`; edit existing tests; expand scope; touch IAM, networking, CI templates, or the platform repo; deploy anything | - |
| **Adversarial reviewer** | spec, diff, implementation report (as hypotheses), `preview.json`, repo read-only, previous findings and response | `findings.json` (with preview e2e runs in `checks_performed`), probe scripts under outputs | edit source; ask the implementer anything; mark a `user_visible` criterion `met` without exercising the preview | the implementer's session and reasoning |
| **Security reviewer** | spec, diff, implementation report (as hypotheses), `preview.json`, `security_prechecks.json` (secret scan, dependency audit, SAST when the host has the tools), repo read-only, previous findings and response | `findings.json` with `category: security` (or concurrency / data_integrity), one `cwe` per finding, `attack_path`, `exploitability`, `impact`, probe scripts under outputs | edit source; report a vulnerability without an attack path or a reproduction; treat a scanner's output as a verdict | the implementer's session and reasoning; the general reviewers' findings |
| **Merge judge** | spec, diff, findings, responses, orchestrator test results, deterministic checks | `merge_readiness.json` | override a deterministic check; edit code | both author sessions |
| **Integrator** | the rendered description, its lint result, PR facts, spec, verdict | `cr.json` via `open_change_request` | change code or history; add evidence or commentary to the description | - |

## Model and effort choices

| Agent | Model | Effort | Reasoning |
|---|---|---|---|
| Codebase researcher | Sonnet 5.5 | medium | Reading-heavy, little judgement; keeps the leads' context small. Haiku 5.5 is a cost option once its reports are calibrated. |
| Decomposer | Opus 5.5 | high | Highest leverage per token in the run: a bad work item becomes several bad specs. |
| Spec writer | Sonnet 5.5 | high | Specs are structured and rubric-graded; the judge above it is stronger, which is the point. |
| Spec judge | Opus 5.5 | high | Different tier from the writer (self-preference mitigation). At xhigh about 80% of its output was hidden reasoning that changed no verdict (post-mortem 2026-10-10, issue 9); the orchestrator can override to xhigh per session for round 2 or sets over three specs. |
| Implementer | Opus 5.5 | xhigh | Long-horizon agentic coding; xhigh is the recommended setting for this class of work. |
| Reviewer | Opus 5.5 | high | Same tier as the implementer. Mitigation is independence plus execution grounding, not model choice; at xhigh, 65-73% of its output was hidden reasoning (post-mortem 2026-10-10, issue 10). For high-stakes repos, pin a different model per session with `agent_with_overrides` (Claude Fable 5.1 is the natural upgrade). |
| Merge judge | Opus 5.5 | high | Judges an exchange with authoritative test results in hand; xhigh rarely changes the verdict. |
| Integrator | Sonnet 5.5 | medium | Mechanical rendering and one tool call. |

Effort is set in the agent file's `model` object and can be overridden per session. Expect to re-tune per model release; one practitioner's whole orchestration collapsed on a single model change ([practitioners.md §7](research/practitioners.md)).

## Why these boundaries

**Researcher is goal-blind.** HumanLayer's revised workflow hides the implementation goal from the research step so it records facts rather than justification ([practitioners.md §1c](research/practitioners.md)). The researcher's prompt says so explicitly and the lead agents are told not to reveal intent.

**Writer and judge are different agents, different sessions, different tiers.** Anthropic's own harness work found generators "reliably skew positive when grading their own work" and that a separate evaluator is "far more tractable" to tune ([practitioners.md §3](research/practitioners.md)). Academic work adds that capability does not reduce self-preference bias and that decomposed per-criterion rubrics do ([academic-2026.md §1](research/academic-2026.md)).

**The judge never authors.** If the judge could rewrite specs it would start grading its own rewrites. It reports issues with suggested resolutions; the writer decides.

**Reviewer gets a fresh context each round and is told to disagree.** Clean-context review is the pattern Cognition reports working in production ([practitioners.md §2](research/practitioners.md)); mandated disagreement is what turned a naive adversarial reviewer into the best-performing one in Qiu and Gill's study; and more rounds in a shared context raise false positives 62% for 8 points of recall (Song). So: new session per round, previous findings passed as a file, explicit instruction to find fault.

**Findings carry refutation conditions.** This is the refutation protocol the brief asked for, made concrete: a finding states what evidence would make the reviewer withdraw it, the implementer either produces that evidence or fixes the code, and a new reviewer instance judges the response against the condition literally. "I believe this is fine" is not admissible on either side. Jin and Chen found reviewers over-reject and that asking for fixes makes it worse; the counter is to route every disputed finding through execution.

**Merge judge is a critic over the review.** Qiu and Gill's best configuration added a critic that audits the review before edits are made. The merge judge does that after the exchange settles: re-runs reproductions, checks refutations against conditions, spot-checks withdrawals, and looks for what both missed. It also receives the orchestrator's authoritative test results so that neither author's claims about tests matter.

**Implementer works in slices and cannot touch existing tests.** Both come straight from practitioner reports: models default to horizontal layers, and "cheating on tests" is the first failure mode several authors list ([practitioners.md §1f, §6](research/practitioners.md); Anthropic's "it is unacceptable to remove or edit tests"). The slice rule is in the prompt and the rubric; the test rule is a deterministic check (`protected_tests`). The one exception is an existing test file the approved spec names by exact path in `allowed_paths`. Such a file may be extended: lines added, none removed, not deleted or renamed, and this only when the diff proves no line was removed. A glob such as `tests/**` never unlocks an existing test, and `test_weakening` still reads every added line.

**Implementer and reviewer both run the end-to-end checks against the preview, and neither owns the URL.** Anthropic's harness work observed agents marking features done "without verifying them end to end" ([practitioners.md §3](research/practitioners.md)), and SWE-Cycle measured the drop when the cycle includes running the result ([academic-2026.md §5](research/academic-2026.md)). So `test_plan.e2e` is required in the spec, the implementer runs it, the reviewer runs it again, and the orchestrator runs it a third time against a URL it read from GitLab's environment record. The agent never supplies the evidence of its own deployment.

**Nobody deploys and nobody touches the platform.** The platform contract (a skill the platform team owns) names what agents must never touch: IAM, networking, CI templates, the platform repository, promotion beyond dev. Boundary-crossing actions are routed to humans or to the platform's own automation, not taken by the working agent ([practitioners.md §1b Factor 7, §4](research/practitioners.md)); the reviewer treats any such touch as `blocking`. Deployment after merge is the platform's pipeline, observed by the orchestrator ([05-platform-and-deployment.md](05-platform-and-deployment.md)).

**Integrator makes no decisions.** The legibility step (writing the change request so a human can review it in five minutes) is separated from the correctness step. The orchestrator renders the description and keeps the evidence in the run record; the integrator only rephrases what the lint refuses ([post-mortem 2026-10-11](postmortems/2026-10-11-evergreen-frontier-models-pr.md) §4). The split follows the prover-verifier decoupling result (Kim and Lee) and the observation that the generator and the explainer want different things.

## In-stage fan-out (multiagent rosters)

Three agents declare a `multiagent` roster:
- **Decomposer** and **Spec writer** roster the researcher, so reading happens in parallel threads with fresh contexts and only short reports return.
- **Implementer** rosters the researcher and `self`, so a large spec can be split into independent slices across copies while the coordinator keeps design decisions and the final report.
- **Reviewer** rosters `self`, so a large diff can be reviewed in parallel slices and the findings merged. Separately, the orchestrator runs two whole reviewer sessions and merges those too.

One level of delegation is a platform limit and also the right design: the research literature on agent coordination finds designated coordinators add little and shared artifacts beat chat ([academic-2026.md §4](research/academic-2026.md)).

## Instruction budget

Every system prompt is under 80 lines and each rule traces to a failure in `docs/research/`. Repository-specific guidance (build commands, conventions, where tests live) belongs in the target repository's `.claude/skills/`, which Managed Agents loads at session start, not in these prompts. HumanLayer's finding that the model "silently skipped alignment steps" once a planning prompt passed 85 instructions is the reason.
