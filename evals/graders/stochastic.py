"""Stochastic graders: LLM judging done carefully.

Every LLM measurement here follows the same discipline:
- per-criterion, not holistic (self-preference drops ~31% with decomposed rubrics; Yang et al. 2604.22891);
- N independent votes with the majority and the vote entropy reported (Yagubyan 2606.13685: ~11 votes);
- position swap for pairwise comparisons (Norman et al. 2606.19544);
- formatting normalised before judging (style bias dominates; Soumik 2604.23178);
- calibration against human labels reported as Cohen's kappa, never raw agreement;
- bootstrap or Bayesian intervals, not point estimates (Hariri et al. 2510.04265).

The judge model is Claude Opus 5.5 by default. It is the same family as the agents being judged,
so the kappa-against-humans number is what makes any of this trustworthy; run it.
"""
from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

import anthropic

JUDGE_MODEL = "claude-opus-5-5"


def normalise(text: str) -> str:
    """Strip formatting cues that bias judges: markdown emphasis, heading levels, trailing whitespace."""
    text = re.sub(r"[*_`]{1,3}", "", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


@dataclass
class CriterionScore:
    criterion: str
    votes: list[int]

    @property
    def majority(self) -> int:
        return Counter(self.votes).most_common(1)[0][0]

    @property
    def entropy(self) -> float:
        n = len(self.votes)
        return -sum((c / n) * math.log2(c / n) for c in Counter(self.votes).values())


class RubricJudge:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = JUDGE_MODEL, votes: int = 11):
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.votes = votes

    def score_criterion(self, artifact_text: str, criterion_md: str, anchors_md: str | None = None) -> CriterionScore:
        """Independent votes for ONE criterion, 0-3, each in a fresh context."""
        system = ("You score one artifact against one rubric criterion. Reply with JSON only: "
                  '{"score": 0|1|2|3, "evidence": "<quote from the artifact>"}. Score the contract, not the confidence; '
                  "do not reward length or formatting.")
        prompt = f"<criterion>\n{criterion_md}\n</criterion>\n"
        if anchors_md:
            prompt += f"<calibration_examples>\n{anchors_md}\n</calibration_examples>\n"
        prompt += f"<artifact>\n{normalise(artifact_text)}\n</artifact>"
        votes: list[int] = []
        for _ in range(self.votes):
            msg = self.client.messages.create(
                model=self.model, max_tokens=400, system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": {
                    "type": "object", "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 3},
                                                     "evidence": {"type": "string"}},
                    "required": ["score", "evidence"], "additionalProperties": False}}},
            )
            if msg.stop_reason == "refusal":
                continue
            votes.append(int(json.loads(msg.content[0].text)["score"]))
        name = criterion_md.strip().splitlines()[0].lstrip("# ").strip()
        return CriterionScore(criterion=name, votes=votes)

    def pairwise(self, a: str, b: str, question: str, swaps: int = 6) -> dict[str, Any]:
        """Which of two artifacts better satisfies `question`. Half the votes see B first."""
        system = ('Compare two artifacts. Reply JSON only: {"winner": "A"|"B"|"tie", "reason": "<short>"}.')
        outcomes = []
        for i in range(swaps):
            first, second, flipped = (a, b, False) if i % 2 == 0 else (b, a, True)
            prompt = f"<question>{question}</question>\n<A>\n{normalise(first)}\n</A>\n<B>\n{normalise(second)}\n</B>"
            msg = self.client.messages.create(model=self.model, max_tokens=300, system=system,
                                              messages=[{"role": "user", "content": prompt}])
            if msg.stop_reason == "refusal":
                continue
            w = json.loads(msg.content[0].text)["winner"]
            if flipped and w in ("A", "B"):
                w = "B" if w == "A" else "A"
            outcomes.append(w)
        c = Counter(outcomes)
        return {"votes": outcomes, "a_wins": c["A"], "b_wins": c["B"], "ties": c["tie"],
                "position_consistency": _position_consistency(outcomes)}


def _position_consistency(outcomes: Sequence[str]) -> float:
    """Share of (normal, swapped) vote pairs that agree after un-flipping. 1.0 = no position bias observed."""
    pairs = list(zip(outcomes[0::2], outcomes[1::2]))
    if not pairs:
        return float("nan")
    return sum(x == y for x, y in pairs) / len(pairs)


# ----------------------------------------------------------------------------- statistics

def cohens_kappa(a: Sequence[int], b: Sequence[int]) -> float:
    assert len(a) == len(b) and a
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    cats = set(a) | set(b)
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def bootstrap_ci(values: Sequence[float], iters: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float, float]:
    rng = random.Random(seed)
    vals = list(values)
    means = sorted(sum(rng.choices(vals, k=len(vals))) / len(vals) for _ in range(iters))
    return (sum(vals) / len(vals), means[int(alpha / 2 * iters)], means[int((1 - alpha / 2) * iters) - 1])


def beta_credible_interval(successes: int, trials: int, mass: float = 0.95) -> tuple[float, float, float]:
    """Posterior mean and equal-tailed interval for a success probability under a Beta(1,1) prior.

    Preferred over pass@k for small N (Hariri et al. 2510.04265). Uses a simple numeric inversion to avoid scipy.
    """
    a, b = successes + 1, trials - successes + 1
    mean = a / (a + b)
    # Numeric CDF inversion on a fine grid; adequate for reporting.
    grid = [i / 4000 for i in range(4001)]
    from math import lgamma, exp, log
    lbeta = lgamma(a) + lgamma(b) - lgamma(a + b)
    pdf = [exp((a - 1) * log(x) + (b - 1) * log(1 - x) - lbeta) if 0 < x < 1 else 0.0 for x in grid]
    total = sum(pdf)
    cdf, acc = [], 0.0
    for p in pdf:
        acc += p / total
        cdf.append(acc)
    lo = next(x for x, c in zip(grid, cdf) if c >= (1 - mass) / 2)
    hi = next(x for x, c in zip(grid, cdf) if c >= 1 - (1 - mass) / 2)
    return mean, lo, hi


def pass_power_k(per_item_success_runs: dict[str, Sequence[bool]], k: int) -> float:
    """pass^k: probability all k runs of an item succeed, averaged over items (reliability, not luck)."""
    vals = []
    for runs in per_item_success_runs.values():
        p = sum(runs) / len(runs)
        vals.append(p ** k)
    return sum(vals) / len(vals) if vals else float("nan")


def intervals_non_overlapping(ci_a: tuple[float, float, float], ci_b: tuple[float, float, float]) -> bool:
    """Decision rule for 'configuration B is better than A': credible intervals do not overlap."""
    return ci_a[2] < ci_b[1] or ci_b[2] < ci_a[1]
