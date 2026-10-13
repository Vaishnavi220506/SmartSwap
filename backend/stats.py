"""Small, dependency-free statistics used by the API and the research analysis.

SciPy is deliberately avoided: Windows Smart App Control blocks its compiled
extension modules on some hosts, and these few tests are easy to compute exactly.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter

# Two-sided 95% Student t critical values t_{0.975, df}.
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
         11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
         21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
         40: 2.021, 60: 2.000, 120: 1.980}


def t975(df: int) -> float:
    """Critical value of Student's t for a two-sided 95% interval."""
    if df < 1: raise ValueError("df must be >= 1")
    if df in _T975: return _T975[df]
    keys = sorted(_T975)
    if df > keys[-1]: return 1.960
    lo = max(k for k in keys if k < df); hi = min(k for k in keys if k > df)
    # interpolate linearly in 1/df, which is close to exact in this range
    w = (1 / df - 1 / hi) / (1 / lo - 1 / hi)
    return _T975[hi] + w * (_T975[lo] - _T975[hi])


def rank(values: list[float]) -> list[float]:
    """Average ranks (1-based), ties share the mean rank."""
    order = sorted(range(len(values)), key=lambda i: values[i]); ranks = [0.0] * len(values); i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]: j += 1
        for k in range(i, j + 1): ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def wilcoxon_signed_rank(differences: list[float]) -> float:
    """Exact two-sided p-value of the Wilcoxon signed-rank test (zeros dropped).

    The null distribution of W+ is enumerated exactly by dynamic programming over
    doubled ranks, which keeps tied (half-integer) ranks exact.
    """
    d = [x for x in differences if x != 0]
    n = len(d)
    if n == 0: return 1.0
    ranks2 = [int(round(2 * r)) for r in rank([abs(x) for x in d])]
    w_plus2 = sum(r for r, x in zip(ranks2, d) if x > 0)
    counts = Counter({0: 1})
    for r in ranks2:
        nxt: Counter = Counter()
        for s, c in counts.items(): nxt[s] += c; nxt[s + r] += c
        counts = nxt
    denom = 2 ** n
    lower = sum(c for s, c in counts.items() if s <= w_plus2) / denom
    upper = sum(c for s, c in counts.items() if s >= w_plus2) / denom
    return min(1.0, 2 * min(lower, upper))


def holm(pvalues: list[float]) -> list[float]:
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i]); adjusted = [0.0] * len(pvalues); running = 0.0
    for position, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - position) * pvalues[i])); adjusted[i] = running
    return adjusted


def bootstrap_median_ci(values: list[float], resamples: int = 10000, seed: int = 7) -> tuple[float, float]:
    """95% percentile-bootstrap interval for the median."""
    if len(values) < 2: return (math.nan, math.nan)
    rng = random.Random(seed); n = len(values); medians = []
    for _ in range(resamples):
        sample = sorted(values[rng.randrange(n)] for _ in range(n))
        medians.append(sample[n // 2] if n % 2 else (sample[n // 2 - 1] + sample[n // 2]) / 2)
    medians.sort()
    return medians[int(0.025 * resamples)], medians[int(0.975 * resamples) - 1]


def spearman(x: list[float], y: list[float], permutations: int = 5000, seed: int = 11) -> tuple[float, float]:
    """Spearman's rho with a two-sided permutation p-value."""
    def pearson(a: list[float], b: list[float]) -> float:
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        num = sum((p - ma) * (q - mb) for p, q in zip(a, b))
        den = math.sqrt(sum((p - ma) ** 2 for p in a) * sum((q - mb) ** 2 for q in b))
        return num / den if den else 0.0
    rx, ry = rank(x), rank(y); rho = pearson(rx, ry)
    rng = random.Random(seed); shuffled = list(ry); hits = 0
    for _ in range(permutations):
        rng.shuffle(shuffled)
        if abs(pearson(rx, shuffled)) >= abs(rho) - 1e-12: hits += 1
    return rho, (hits + 1) / (permutations + 1)


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided binomial sign test (ties dropped). For paired binary
    outcomes, wins/losses are the discordant pairs, so this is the exact McNemar test."""
    n = wins + losses
    if n == 0: return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def blocks_for_power(log_ratios: list[float], alpha: float = 0.05, power: float = 0.8) -> int | None:
    """Blocks a paired Wilcoxon test would need to detect the observed effect.

    Normal approximation on the per-block log ratio (effect = mean / sd), inflated
    by the Wilcoxon test's asymptotic relative efficiency (3/pi) under normality.
    None when there is no effect to detect or too few blocks to estimate it.
    """
    if len(log_ratios) < 3: return None
    mean, sd = statistics.fmean(log_ratios), statistics.stdev(log_ratios)
    if mean == 0: return None
    if sd == 0: return 3
    z = statistics.NormalDist()
    n = ((z.inv_cdf(1 - alpha / 2) + z.inv_cdf(power)) * sd / abs(mean)) ** 2 / (3 / math.pi)
    return max(3, math.ceil(n))


def hierarchical_bootstrap_median(groups: list[list[float]], resamples: int = 10000, seed: int = 13) -> tuple[float, float, float]:
    """Median and 95% CI when values are nested in groups (blocks within hosts).

    Each resample draws hosts with replacement, then blocks with replacement
    within each drawn host, so between-host variation widens the interval
    instead of being mistaken for more independent data.
    """
    groups = [g for g in groups if g]
    if not groups: return (math.nan, math.nan, math.nan)
    rng = random.Random(seed); stats = []
    for _ in range(resamples):
        sample = []
        for _ in groups:
            g = groups[rng.randrange(len(groups))]
            sample += [g[rng.randrange(len(g))] for _ in g]
        stats.append(statistics.median(sample))
    stats.sort()
    return statistics.median(v for g in groups for v in g), stats[int(0.025 * resamples)], stats[int(0.975 * resamples) - 1]
