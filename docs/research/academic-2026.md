# Research notes: 2026 academic work relevant to the factory

Compiled 2026-10-09. Every item was checked against its arXiv abstract page unless marked *unverified*. Numbers are taken from abstracts. Where an arXiv ID implies a later month than the page's submitted date, the page date is reported. This file is the evidence base for the principles in [design-principles.md](../design-principles.md); each principle there cites one or more entries here.

Gaps worth knowing about before trusting this: no 2026 paper directly benchmarks spec-judge reliability for software specifications; there is no head-to-head Self-Refine versus independent-critic study on code (the closest are Parmar et al. in data cleaning and Rakasi et al. on execution-based correction); there is no empirical study of approval fatigue inside a real coding pipeline (Turan is a model).

---

## 1. LLM-as-judge reliability (code, specs, trajectories)

**Reliability without Validity: A Systematic, Large-Scale Evaluation of LLM-as-a-Judge Models Across Agreement, Consistency, and Bias.** Norman, Rivera, Hughes. arXiv:2606.19544, Jun 2026. https://arxiv.org/abs/2606.19544
- 21 judges, 9 providers, 3 benchmarks, 118 runs, about 541k judgments.
- Exact-match agreement overstates chance-corrected discrimination (Cohen's kappa) by 33 to 41 percentage points on MT-Bench, universally.
- Judge rankings shift up to 14 positions across benchmarks.
- "Consistency-bias paradox": test-retest reliability above 0.95 coexists with position bias above 0.10 in two production judges. Verbosity bias small under a single pairwise rubric.
- Relevance: a stable spec or code judge can still be systematically biased. Validate with kappa and position-swap, not raw agreement.

**The Coin Flip Judge? Reliability and Bias in LLM-as-a-Judge Evaluation.** Yagubyan. arXiv:2606.13685, Apr/Jun 2026. https://arxiv.org/abs/2606.13685
- 29 tasks, 50 pairwise plus 50 pointwise trials each, two judges.
- Pairwise verdicts flipped 13.6% on average; 28% of questions had a flip rate over 20%; one reached 56%.
- First-position bias observed; cross-judge agreement 76% (kappa 0.51); equivalent prompt templates flipped the majority outcome in 25% of cases.
- 11 repeated trials needed for majority vote to recover the 50-trial verdict with 95% probability (15 for high-variance items).
- Relevance: single-call judging at a merge gate is close to a coin flip on hard cases. Budget for repeated votes or treat a single verdict as a soft signal.

**Bias in the Loop: Auditing LLM-as-a-Judge for Software Engineering.** Zhao, Esmaeili, Fard. arXiv:2604.16790, Apr 2026. https://arxiv.org/abs/2604.16790
- Code generation, repair, and test generation; one presentation cue varied at a time with code held fixed.
- Prompt-induced biases shift preferences enough to change task-level conclusions and model rankings.
- Recommends reporting bias sensitivity alongside accuracy, with explicit controls.
- Relevance: the spec judge and reviewer prompts must be audited with perturbation controls before being trusted in the pipeline.

**Quantifying and Mitigating Self-Preference Bias of LLM Judges.** Yang et al. arXiv:2604.22891, Apr 2026 (v4 Jun). https://arxiv.org/abs/2604.22891
- Automated equal-quality pair construction across 20 models. Capability is uncorrelated or negatively correlated with low self-preference bias.
- Structured multi-dimensional evaluation (cognitive-load decomposition) cuts self-preference bias by 31.5% on average.
- Relevance: do not let the implementer model judge its own spec or code without mitigation. Use a different model or a decomposed rubric, and never show the judge the author's self-assessment.

**Judging the Judges: A Systematic Evaluation of Bias Mitigation Strategies in LLM-as-a-Judge Pipelines.** Soumik. arXiv:2604.23178, Apr 2026; TMLR 2026. https://arxiv.org/abs/2604.23178
- 9 debiasing strategies, 5 judges, 3 benchmarks. Style bias (markdown over prose) is dominant (0.10 to 0.76); position bias at most 0.04.
- A cheap judge with combined debiasing reached 71.0% agreement (kappa 0.549) at roughly 15 times lower cost than the best frontier setup (69.5%).
- Relevance: normalise the formatting of candidate specs and diffs before judging. A cheap debiased judge can match a frontier one.

**Can LLM-as-a-Judge Reliably Verify Rubrics in Agentic Scenarios? (RuVerBench).** Peng et al. arXiv:2606.29920, Jun 2026; EMNLP 2026. https://arxiv.org/abs/2606.29920
- 2,458 instances (deep research and agentic coding) with human-labelled rubric satisfaction.
- Frontier judges strong but with substantial noise; weaker models more prompt-sensitive; batched verification trades accuracy for cost; majority voting helps with diminishing returns.
- Relevance: rubric-item verification of a spec is viable but should be per item, unbatched, with voting.

**Counsel: A Meta-Evaluation Dataset for Agentic Tasks.** Pisupati et al. arXiv:2606.21627, Jun 2026. https://arxiv.org/abs/2606.21627
- Human meta-evaluation of step-level judge critiques on tau-bench and DA-Code.
- Best judge: about 88% agreement on error location, only about 65% on reasoning.
- Relevance: trust a reviewer's pointer to where a problem is more than its explanation of why.

**trajectory-judge: What Outcome-Only LLM Judges Miss on Agent Trajectories.** Mohammadi. arXiv:2609.00038, Aug/Sep 2026. https://arxiv.org/abs/2609.00038
- Proposes paired discrimination: flag rate on a faulty run minus flag rate on its clean parent. 400 exactly-labelled trajectories.
- An outcome-only judge shows 34 to 76% apparent recall on reply-unchanged faults, but paired discrimination of zero (it is just the false-positive rate).
- Step-by-step instruction flags all such faults with zero false positives on clean runs.
- Relevance: review the diff and trace step by step, not just the final artifact. Evaluate reviewers with paired clean and faulty controls.

**Are LLMs Reliable Code Reviewers? Systematic Overcorrection in Requirement Conformance Judgement.** Jin, Chen. arXiv:2603.00539, Feb 2026. https://arxiv.org/abs/2603.00539
- LLMs frequently label correct code as non-conformant; asking for explanations and fixes increases the misjudgment rate.
- Proposes a fix-guided verification filter: treat the proposed fix as a counterfactual and run both versions against spec-derived tests.
- Relevance: an adversarial reviewer will over-reject. Gate its findings through execution.

---

## 2. Adversarial, debate, and critic approaches

**Adversarial Review: Structured Disagreement for Grounded Agentic Code Review.** Qiu, Gill. arXiv:2608.18167, Aug 2026. https://arxiv.org/abs/2608.18167
- Three agents: coder, reviewer, critic (the critic audits the review before edits are made).
- Highest pass rate on LiveCodeBench among tested methods, beating a 5-agent baseline; also improves SWE-bench Verified.
- A naive version showed false consensus on SWE-PRBench; one prompt change mandating explicit disagreement produced the highest F1.
- Relevance: minimal, evidence-grounded, mandated disagreement beats bigger teams. A direct template for the review stage.

**When Helping Hurts and How to Fix It: Multi-Agent Debate for Data Cleaning.** Parmar et al. arXiv:2606.02866, Jun 2026. https://arxiv.org/abs/2606.02866
- 3 benchmarks, 4 model families, over 6,000 task-conditions. Debate hurts generation by 1.6 to 15.5 pp (the generator accepts hallucinated critiques) but improves detection by 27.4 pp F1.
- Self-verification with identical tools matches baseline. A separate critic with code-execution grounding and evidence-gated generation is the only configuration that beats single-agent generation (+5.3 pp, p<0.05).
- Relevance: the strongest available evidence that an independent, execution-grounded critic beats self-critique, and that critiques must be evidence-gated before the implementer acts on them.

**The Cost of Consensus: Isolated Self-Correction Prevails Over Unguided Homogeneous Multi-Agent Debate.** Bertalanic, Fortuna. arXiv:2605.00914, Apr 2026. https://arxiv.org/abs/2605.00914
- Ten homogeneous small agents, three rounds. Sycophantic conformity up to 85.5%; correct answers destabilised up to 70%.
- Debate costs 2.1 to 3.4 times the tokens for equal or worse accuracy.
- Relevance: unstructured same-model debate is a cost sink. Roles and heterogeneity matter.

**More Rounds, More Noise: Why Multi-Turn Review Fails to Improve Cross-Context Verification.** Song. arXiv:2603.16244, Mar 2026. https://arxiv.org/abs/2603.16244
- 30 artifacts, 150 injected errors. Single-pass isolated review (F1 0.376) beats all multi-turn variants (best 0.303, p<0.001).
- Extra rounds add 0.08 recall and 62% more false positives; review-target drift toward critiquing the conversation.
- Relevance: one independent review pass per round, fresh context. Do not run the reviewer and author as a continuing chat.

**When Uncertainty Isn't Enough: An Empirical Study of Self-Correction in Code Generation.** Rakasi et al. arXiv:2608.14659, Jul 2026. https://arxiv.org/abs/2608.14659
- Uncertainty-triggered self-correction degrades Pass@1 in 5 of 6 configurations. Execution-based regeneration is the only policy that reliably helps (+6 to +26 pp HumanEval).
- Relevance: self-critique without execution is net negative. Route corrections through tests.

**Self-Authored Verification Is Unreliable in Heuristic Self-Improving Agents (SEAL).** Guo et al. arXiv:2607.24300, Jul 2026. https://arxiv.org/abs/2607.24300
- "Verifier-deployment gap": agents pass their own tests while regressing on a sealed audit. A harness-side acceptance check the agent cannot see beats baselines across 6 models.
- Relevance: keep at least one acceptance oracle outside the implementer's control.

**Mitigating Legibility Tax with Decoupled Prover-Verifier Games.** Kim, Lee. arXiv:2602.23248, Feb 2026 (ICLR 2026 workshop). https://arxiv.org/abs/2602.23248
- Training for checkability reduces accuracy; decoupling a fixed solver from a trained translator recovers it. Math domain.
- Relevance: separate the agent that makes the change correct from the step that makes it legible (the MR description and evidence bundle).

---

## 3. Requirements and specification generation

**Clarity Is Not Assumed (Orchid).** Yang et al. arXiv:2604.21505, Apr 2026 (v3 Sep). https://arxiv.org/abs/2604.21505
- 1,304 function-level tasks, four ambiguity types. Ambiguity degrades all models, most for the strongest; models produce functionally different implementations for the same ambiguous requirement and cannot reliably detect or resolve ambiguity.
- Relevance: the spec stage must surface and resolve ambiguity explicitly before implementation. Stronger implementers do not compensate.

**ClarifyCodeBench.** Fang et al. arXiv:2607.00711, Jul 2026. https://arxiv.org/abs/2607.00711
- Interactive elicitation benchmark; metrics for key-question rate and optimal round adherence. Strong code generation does not imply good clarification; performance collapses as ambiguity density rises.
- Relevance: measure the decomposer and spec writer on question quality, not only on output.

**Can LLMs Decompose User Stories into Tasks? (DELTA).** Ly, Aydemir, Dalpiaz. ASE 2026. https://conf.researchr.org/details/ase-2026/ase-2026-research-track/262/
- No single prompting or model configuration was consistently best by text similarity; criteria-based LLM-judge ranking aligned with experts better than similarity metrics; experts preferred the top-3 LLM decompositions over human ones.
- Relevance: judge decompositions with criteria rubrics, not similarity to a reference.

**ReqGenX.** Ayon et al. arXiv:2607.16564, Jul 2026. https://arxiv.org/abs/2607.16564
- Atomic decomposition of requirements documents into traceable statements; under strict grading pass rates range 54.8 to 97.1%.
- Relevance: atomic, source-grounded requirement units with traceability are feasible and measurable; lenient judges inflate pass rates.

**Intent-aligned Formal Specification Synthesis via Traceable Refinement (VeriSpecGen).** Ye et al. arXiv:2604.10392, Apr 2026. https://arxiv.org/abs/2604.10392
- Decomposes natural language into atomic requirements, generates requirement-targeted tests with traceability maps; failures route to clause-level repair. Up to +31.8 points over baselines.
- Relevance: a concrete pattern for spec-to-test traceability and targeted spec repair.

**CodeSpec: Dual Executable Specifications for Agentic Long-Horizon Feature Development.** Wang et al. arXiv:2607.26777, Jul 2026. https://arxiv.org/abs/2607.26777
- Compiles sub-requirements into paired architecture and behaviour specs that are executable, preventing design/implementation drift. Beats agent baselines on FeatureBench.
- Relevance: make the spec executable (interface and behaviour checks) so implementer and reviewer share an oracle.

**Inferring Code Correctness from Specification (TRAILS).** Tambon, Papadakis. arXiv:2605.29822, May 2026. https://arxiv.org/abs/2605.29822
- Partition the spec's input space, execute the candidate, ask the model only whether I/O pairs conform. Up to +39% MCC over zero-shot chain-of-thought; more stable across seeds.
- Relevance: spec-conformance checking is more reliable when the judge sees behaviour, not code.

---

## 4. Multi-agent software engineering: decomposition, error propagation, coordination

**Towards a Science of Scaling Agent Systems.** Kim et al. arXiv:2512.08296, Dec 2025 (v3 Apr 2026). https://arxiv.org/abs/2512.08296
- 260 configurations, 6 benchmarks, 5 architectures. Range against single agent: +80.8% (decomposable work) to -70.0% (sequential planning).
- Architectures without centralized verification propagate errors more; diminishing returns once single-agent accuracy is roughly 45% or higher.
- Relevance: a centralized verifier between stages is the main protection against cascades. Decomposition helps only on genuinely decomposable work.

**Effective Strategies for Asynchronous Software Engineering Agents (CAID).** Geng, Neubig. arXiv:2603.21489, Mar 2026. https://arxiv.org/abs/2603.21489
- Centralized dependency-aware delegation, isolated worktrees, and test-based integration: +25.6 pp (PaperBench), +14.7 pp (Commit0) over single agent. Higher API cost; wall-clock not meaningfully reduced; coarse or misaligned splits produce locally correct but expensive-to-integrate outputs.
- Relevance: decomposition granularity must follow the dependency graph. Budget for integration cost.

**From Spark to Fire: Modeling and Mitigating Error Cascades in LLM-Based Multi-Agent Collaboration.** Xie et al. arXiv:2603.04474, Mar 2026. https://arxiv.org/abs/2603.04474
- A single seeded error causes system-wide failure; a genealogy-graph governance layer prevents final infection in 89% or more of runs.
- Relevance: track provenance of every claim across stages so a wrong spec clause can be traced and quarantined.

**When Agents Coordinate: Measuring Coordination in Multi-Agent AI Coding.** Destefanis, Aste. arXiv:2608.16801, Aug 2026. https://arxiv.org/abs/2608.16801
- 1,902 runs. Shared files cut output tokens by about 42% at 8 agents; a designated coordinator gives no reliable success gain; agents sought hidden grading material in about 80% of 244 sealed runs.
- Relevance: coordinate through shared artifacts, not chat; keep test oracles away from implementers.

**TheBotCompany.** Lyu et al. arXiv:2603.25928, Mar 2026; ASE 2026. https://arxiv.org/abs/2603.25928
- Strategy, execution, verification as an explicit state machine with asynchronous human oversight over multi-day projects.
- Relevance: verification as a state, not a step inside implementation.

**ChatDev 2.0.** arXiv:2609.00714, Sep 2026 (*details unverified*). The MAST failure taxonomy (Cemri et al., 2025) remains the reference: roughly 42% specification and design failures, 37% inter-agent misalignment, 21% verification and termination (*figures from secondary sources, unverified*).

---

## 5. Benchmarks and evaluation methodology

**FeatureBench.** Zhou et al. arXiv:2602.10975, Feb 2026; ICLR 2026. https://arxiv.org/abs/2602.10975
- 200 feature-level tasks, 3,825 executable environments, 24 repos; tasks span multiple commits. A frontier model at 74.4% on SWE-bench scored 11.0% here. Often over 1M input tokens per task.
- Relevance: the closest public proxy for "deliver a feature end to end".

**RoadmapBench.** Xu et al. arXiv:2605.15846, May 2026. https://arxiv.org/abs/2605.15846
- 115 version-upgrade tasks, 17 repos, 5 languages; median 3,700 lines across 51 files. Best model 39.1%.

**SWE-Marathon.** Desai et al. arXiv:2606.07682, Jun 2026. https://arxiv.org/abs/2606.07682
- 20 ultra-long tasks, average 27.2M tokens per attempt; frontier agents under 30%; reward hacking in 13.8% of rollouts; adversarially reviewed test suites.

**Long-Horizon-Terminal-Bench.** Li et al. arXiv:2607.08964, Jul 2026. https://arxiv.org/abs/2607.08964
- 46 tasks with dense subtask rewards; best 15.2% pass@1 at reward 0.95 or above.

**Terminal-Bench 2.0.** Merrill et al. arXiv:2601.11868, Jan 2026; ICLR 2026. https://arxiv.org/abs/2601.11868
- 89 tasks; frontier agents under 65%; 32,155 trials.

**SWE-Cycle.** Guan et al. arXiv:2605.13139, May 2026. https://arxiv.org/abs/2605.13139
- 489 instances covering environment reconstruction, implementation, test generation, and a full cycle; solve rates drop sharply in the full cycle. Introduces an execution-capable grader.

**SlopCodeBench.** Orlanski et al. arXiv:2603.24755, Mar 2026. https://arxiv.org/abs/2603.24755
- 36 problems, 196 checkpoints of evolving specs; best agent passes 14.8% of checkpoints; erosion rises in 77% of trajectories; agent code is 2.3 times more verbose than open-source baselines. Quality guidance cuts initial slop but not the degradation rate.

**SWE-Bench Pro Verified.** Zheng et al. arXiv:2609.08149, Sep 2026. https://arxiv.org/abs/2609.08149
- The original suffered reward hacking via leaked gold solutions and flawed tasks; the patched version lowers some scores substantially.

**Does SWE-Bench-Verified Test Agent Ability or Model Memory?** Prathifkumar, Mathews, Nagappan. arXiv:2512.10218, Dec 2025. https://arxiv.org/abs/2512.10218
- Given only issue text, models localise files 3 times better on SWE-bench Verified than on fresher sets, and 6 times better at finding edited files.
- An OpenAI audit (Feb 2026, *figures from search results*) attributed 59.4% of 138 audited failures to test flaws.

**Keep Evaluation Fair (CGMIA).** Zhao et al. arXiv:2609.09865, Sep 2026. https://arxiv.org/abs/2609.09865
- Membership-inference leak detector for code benchmarks.

**Position: Coding Benchmarks Are Misaligned with Agentic Software Engineering.** Gorinova et al. arXiv:2606.17799, Jun 2026. https://arxiv.org/abs/2606.17799
- Benchmarks collapse model, harness, and environment into one score; any harness component can shift scores by a model-generation's margin; single-reference grading penalises valid alternatives.

**SWE-Touch.** Tan et al. arXiv:2608.02499, Aug 2026. https://arxiv.org/abs/2608.02499
- Concurrent user edits cut resolve rate by 7.7 pp; agents fail to re-inspect and re-test.

**Test-as-oracle validity.**
- Liang et al., *Auditing and Decomposing Feedback-Driven Evolution in LLM Test Generation under the Oracle Problem*, arXiv:2608.19626, Aug 2026. https://arxiv.org/abs/2608.19626 Generated outputs agree with a 3-implementation panel only 27.79 to 50.12% of the time; a single-reference oracle inflates measured gains by 9.46 to 14.85 points.
- Bodicoat, Jahangirova, Terragni, *Understanding LLM-Driven Test Oracle Generation*, arXiv:2601.05542, Jan 2026. Generators mostly produce regression oracles (implemented behaviour), not intended-behaviour oracles.
- Ma, Eisty, *From Business Requirements to Test Assertions*, arXiv:2607.10277, Jul 2026. Requirement-derived oracles agree more with the requirement than with the system under test; pilot scale.

**METR Time Horizon 1.1.** Jan 2026. https://metr.org/blog/2026-1-29-time-horizon-1-1/ 228 tasks; the leading model's 50% horizon is 16 hours or more (95% CI 8.5 to 55 hours), at the ceiling of the suite.

---

## 6. Human-in-the-loop placement and trust

**Oversight Has a Capacity.** Turan. arXiv:2606.08919, Jun 2026. https://arxiv.org/abs/2606.08919
- Reviewer agreement on risk only kappa 0.52; with fatigue, safety follows an inverted U in escalation rate. More oversight can reduce safety. Load-aware gating resists flooding.

**When to Screen, When to Bypass: LLM-Judges in Resource-Scarce AI-Human Workflow.** Lin, Zhang. arXiv:2603.13870, Mar 2026. https://arxiv.org/abs/2603.13870
- Queueing model: LLM pre-screening helps when human reviewers are the bottleneck but creates a rework trap when implementers are overloaded. The optimal routing flips by regime.

**Human oversight of agentic systems in practice.** Dhanorkar, Passi, Vorvoreanu. arXiv:2606.05391, Jun 2026. https://arxiv.org/abs/2606.05391
- 17 developer interviews; four oversight forms (a priori control, co-planning, real-time monitoring, post hoc review); the heuristic of treating test results as proof of correctness is common and dangerous.

**You Shall Not Pass! Where and Why Developers Draw The Line on AI Autonomy.** Choudhuri, Bird, Badea, Gerosa, Sarma. arXiv:2607.00533, Jul 2026. https://arxiv.org/abs/2607.00533
- 448 developers; acceptance lowest for identity-defining, human-facing, design-oriented work; accountability lowers delegation of action; high task demand raises delegation.

**Evaluating the Impact of Explainable AI on Trust in AI-Assisted Code Review.** Gao et al. arXiv:2607.24601, Jul 2026. https://arxiv.org/abs/2607.24601
- Full explanations give highest trust but moderate explanations give highest agreement (89.22%); no effect on review time.

**How Coding Agents Fail Their Users.** Tang et al. arXiv:2605.29442, May 2026. https://arxiv.org/abs/2605.29442
- 20,574 sessions; 90.50% of misalignments cost effort and trust rather than cause damage; 91.49% of resolutions need explicit user correction; inaccurate self-reporting is a rising share.

**The End of Code Review.** Monperrus. arXiv:2606.13175, Jun 2026 (position). https://arxiv.org/abs/2606.13175
- Argues the human as mandatory reviewer of agent code is a dead end and proposes agent-in-the-loop merge gates. Included as a counterpoint; this design keeps the human gate at the MR but makes the agent-side review strong enough that the human's job is to decide, not to find bugs.

---

## 7. Statistics for non-deterministic pipelines

**Don't Pass@k: A Bayesian Framework for LLM Evaluation.** Hariri et al. arXiv:2510.04265; ICLR 2026. https://arxiv.org/abs/2510.04265
- Pass@k is unstable at small N; a Dirichlet posterior over categorical outcomes gives credible intervals and supports rubric scores. Non-overlapping intervals as the decision rule.

**Beyond pass@1: A Reliability Science Framework for Long-Horizon LLM Agents.** Khanal, Tao, Zhou. arXiv:2603.29231, Mar 2026. https://arxiv.org/abs/2603.29231
- 10 models, 23,392 episodes, 396 tasks. Graceful degradation drops from 0.90 to 0.44 with task duration; capability and reliability rankings invert at long horizons; memory scaffolds hurt all 10 models.

**Beyond Static Leaderboards: Predictive Validity for the Evaluation of LLM Agents.** Patel et al. arXiv:2606.19704, Jun 2026. https://arxiv.org/abs/2606.19704
- Rank configurations by in-sample to out-of-sample rank correlation; require multi-run variance, hardware disclosure, raw trajectories.

**Engineering Reliable Coding Agents.** Jarmak. arXiv:2608.13867, Aug 2026. https://arxiv.org/abs/2608.13867
- Multivocal review (164 papers, 100 practitioner records); many "model failures" originate in the harness or environment; treat evaluation as a dependency chain.

---

## Design implications (each tagged to its source)

1. Never let the implementer model judge its own spec or code without mitigation; use a different model or a decomposed rubric. Yang et al. 2604.22891.
2. Make the reviewer independent and execution-grounded; gate its critiques on evidence before the implementer acts. Parmar et al. 2606.02866.
3. Mandate explicit disagreement in the reviewer prompt; naive review converges on false consensus. Qiu and Gill 2608.18167.
4. One fresh-context review pass per round; no reviewer-author chat loops. Song 2603.16244.
5. Run every reviewer finding through a fix-as-counterfactual test to filter over-rejection. Jin and Chen 2603.00539.
6. Keep a harness-side acceptance oracle the implementer cannot see or edit. Guo et al. 2607.24300; Destefanis and Aste 2608.16801.
7. Self-correction only via execution feedback. Rakasi et al. 2608.14659.
8. The spec stage must detect and resolve ambiguity explicitly, with clarification scored on question quality. Yang et al. 2604.21505; Fang et al. 2607.00711.
9. Decompose into atomic, source-grounded requirement units with a traceability map to tests. Ye et al. 2604.10392; Ayon et al. 2607.16564.
10. Compile the spec into executable interface and behaviour checks shared by implementer and reviewer. Wang et al. 2607.26777.
11. Spec-conformance judging should see I/O behaviour, not only code. Tambon and Papadakis 2605.29822.
12. Put a centralized verifier between every stage. Kim et al. 2512.08296; Xie et al. 2603.04474.
13. Cut tasks along the dependency graph, isolate in worktrees, integrate via tests; expect higher token cost, not lower wall-clock. Geng and Neubig 2603.21489.
14. Coordinate through shared artifacts, not chat; a dedicated coordinator agent adds little. Destefanis and Aste 2608.16801.
15. Review the trajectory and diff step by step; trust error location more than rationale. Mohammadi 2609.00038; Pisupati et al. 2606.21627.
16. Normalise formatting of candidates before judging; randomise position in pairwise judging. Soumik 2604.23178; Norman et al. 2606.19544.
17. Place human review where humans are the scarce resource and let a judge screen beforehand; when implementers are the bottleneck, screening causes rework. Lin and Zhang 2603.13870.
18. Escalate selectively; oversight capacity follows an inverted U. Turan 2606.08919. Reserve human decisions for design-defining choices. Choudhuri et al. 2607.00533.
19. Give human reviewers moderate, focused explanations, and never present a passing test as proof. Gao et al. 2607.24601; Dhanorkar et al. 2606.05391.
20. Add a quality and erosion check at each iteration; passing checkpoints can hide structural decay. Orlanski et al. 2603.24755.

## Eval implications

1. Validate every judge with Cohen's kappa, position swap, and single-cue perturbation controls; never report raw agreement. Norman et al. 2606.19544; Zhao et al. 2604.16790.
2. Use 11 or more judge votes (15 for high-variance items) or treat a single verdict as noise. Yagubyan 2606.13685.
3. Evaluate reviewers with paired clean and faulty trajectories and report paired discrimination. Mohammadi 2609.00038.
4. Report posterior success probability with credible intervals instead of pass@k; compare configurations by non-overlapping intervals. Hariri et al. 2510.04265.
5. Report pass^k and a reliability-decay curve across task-duration buckets. Khanal et al. 2603.29231.
6. Use feature-level, multi-file benchmarks as the primary end-to-end metric; SWE-bench Verified is contaminated and a large share of audited failures were test flaws. Zhou et al. 2602.10975; Xu et al. 2605.15846; Guan et al. 2605.13139; Prathifkumar et al. 2512.10218.
7. Decontaminate with post-cutoff tasks and membership-inference checks; audit for reward hacking and leaked solutions. Zhao et al. 2609.09865; Zheng et al. 2609.08149; Desai et al. 2606.07682.
8. Do not use LLM-generated tests as the sole oracle; validate against independent implementations or human audit, and separate intended from implemented-behaviour oracles. Liang et al. 2608.19626; Bodicoat et al. 2601.05542.
9. Attribute scores to pipeline components (ablate harness, judge, reviewer) rather than one end-to-end number. Gorinova et al. 2606.17799; Jarmak 2608.13867.
10. Use dense subtask rewards and partial credit for long tasks. Li et al. 2607.08964.
11. Rank pipeline configurations by predictive validity on a holdout; publish multi-run variance and raw trajectories. Patel et al. 2606.19704.
12. Measure decomposition quality with criteria-based rubrics calibrated against expert rankings, not text similarity. Ly et al. ASE 2026. Include an erosion metric across iterations. Orlanski et al. 2603.24755.
