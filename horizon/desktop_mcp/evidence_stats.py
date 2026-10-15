"""Cluster-bootstrap intervals and the savings claim gate for paired cost evidence.

Pure functions only: no I/O, no clocks, no global random state. A given seed
always produces the same interval.
"""

from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

SAVINGS_RULE = (
    "A savings claim needs consented, de-identified evidence with verified "
    "provenance, at least the predeclared number of paired units and "
    "participants, and a confidence interval for (candidate - reference) cost "
    "entirely below zero (build plan section 13)."
)

CONSENTED_DEIDENTIFIED = "consented_deidentified"
SYNTHETIC = "synthetic"

_MIN_RESAMPLES = 1000
_MAX_RESAMPLES = 100000
_CONFIDENCE_LOW = Decimal("0.5")
_CONFIDENCE_HIGH = Decimal("0.999")
_SIGNIFICANT_DIGITS = 10


class StatsError(ValueError):
    """Invalid statistics input, with a stable code and no source data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PairedUnit:
    cluster_id: str  # participant code; pairs from one participant are correlated
    difference: Decimal  # candidate minus reference cost for one matched pair; negative means the candidate is cheaper


@dataclass(frozen=True)
class Interval:
    estimate: Decimal | None  # exact Decimal mean of all differences
    lower: Decimal | None
    upper: Decimal | None
    confidence: Decimal  # e.g. Decimal("0.95")
    method: str  # "cluster_percentile_bootstrap" or "insufficient_data"
    resamples: int
    seed: int
    n_units: int
    n_clusters: int


def _check_confidence(confidence: object) -> Decimal:
    if not isinstance(confidence, Decimal) or not confidence.is_finite():
        raise StatsError("confidence_invalid")
    if not (_CONFIDENCE_LOW < confidence < _CONFIDENCE_HIGH):
        raise StatsError("confidence_out_of_range")
    return confidence


def _check_resamples(resamples: object) -> int:
    if not isinstance(resamples, int) or isinstance(resamples, bool):
        raise StatsError("resamples_invalid")
    if not (_MIN_RESAMPLES <= resamples <= _MAX_RESAMPLES):
        raise StatsError("resamples_out_of_range")
    return resamples


def _check_seed(seed: object) -> int:
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise StatsError("seed_invalid")
    return seed


def _check_minimum(value: object, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 2:
        raise StatsError(code)
    return value


def _check_units(units: Iterable[PairedUnit]) -> list[PairedUnit]:
    checked: list[PairedUnit] = []
    for unit in units:
        if not isinstance(unit, PairedUnit):
            raise StatsError("unit_invalid")
        if not isinstance(unit.cluster_id, str) or not unit.cluster_id.strip():
            raise StatsError("cluster_id_invalid")
        if not isinstance(unit.difference, Decimal) or not unit.difference.is_finite():
            raise StatsError("difference_invalid")
        checked.append(unit)
    return checked


def _significant(value: float) -> Decimal:
    """Round a float bound to 10 significant digits and return it as a Decimal."""
    return Decimal(format(value, f".{_SIGNIFICANT_DIGITS}g"))


def _percentile_indexes(resamples: int, confidence: Decimal) -> tuple[int, int]:
    """0-based indexes into the sorted resample means.

    alpha = 1 - confidence. lower index = floor(alpha/2 * B) and upper index =
    ceil((1 - alpha/2) * B) - 1, clamped to [0, B - 1]. For B = 10000 and
    confidence 0.95 that is indexes 250 and 9749.
    """
    alpha = Decimal(1) - confidence
    half = alpha / 2
    lower = int((half * resamples).to_integral_value(rounding=ROUND_FLOOR))
    upper = int(((1 - half) * resamples).to_integral_value(rounding=ROUND_CEILING)) - 1
    return max(0, min(lower, resamples - 1)), max(0, min(upper, resamples - 1))


def paired_difference_interval(
    units: Iterable[PairedUnit],
    *,
    confidence: Decimal = Decimal("0.95"),
    resamples: int = 10000,
    seed: int,
) -> Interval:
    """Percentile bootstrap over clusters (participants) for the mean difference.

    Each resample draws ``n_clusters`` clusters with replacement using
    ``random.Random(seed)`` and takes the mean of every unit in the drawn
    clusters (a cluster drawn twice counts twice). Bounds are rounded to 10
    significant digits. Fewer than 2 units or fewer than 2 clusters gives
    ``method == "insufficient_data"`` with ``lower`` and ``upper`` set to None.
    """
    confidence = _check_confidence(confidence)
    resamples = _check_resamples(resamples)
    seed = _check_seed(seed)
    checked = _check_units(units)

    n_units = len(checked)
    by_cluster: dict[str, list[Decimal]] = {}
    for unit in checked:
        by_cluster.setdefault(unit.cluster_id, []).append(unit.difference)
    n_clusters = len(by_cluster)

    if n_units == 0:
        estimate = None
    else:
        estimate = sum((unit.difference for unit in checked), Decimal(0)) / Decimal(n_units)

    if n_units < 2 or n_clusters < 2:
        return Interval(
            estimate=estimate,
            lower=None,
            upper=None,
            confidence=confidence,
            method="insufficient_data",
            resamples=resamples,
            seed=seed,
            n_units=n_units,
            n_clusters=n_clusters,
        )

    # Sorted so the same seed gives the same draws regardless of input order.
    cluster_ids = sorted(by_cluster)
    cluster_sums = [float(sum(by_cluster[c], Decimal(0))) for c in cluster_ids]
    cluster_sizes = [len(by_cluster[c]) for c in cluster_ids]

    rng = random.Random(seed)
    indexes = range(len(cluster_ids))
    means: list[float] = []
    for _ in range(resamples):
        drawn = rng.choices(indexes, k=len(cluster_ids))
        total = 0.0
        count = 0
        for index in drawn:
            total += cluster_sums[index]
            count += cluster_sizes[index]
        means.append(total / count)
    means.sort()

    lower_index, upper_index = _percentile_indexes(resamples, confidence)
    return Interval(
        estimate=estimate,
        lower=_significant(means[lower_index]),
        upper=_significant(means[upper_index]),
        confidence=confidence,
        method="cluster_percentile_bootstrap",
        resamples=resamples,
        seed=seed,
        n_units=n_units,
        n_clusters=n_clusters,
    )


def savings_decision(
    interval: Interval,
    *,
    min_units: int,
    min_clusters: int,
    evidence_class: str,
    provenance_verified: bool,
) -> dict:
    """Turn an interval on (candidate - reference) cost into a claim.

    Returns ``{"claim", "reasons", "rule"}``. The claim is ``"none"`` with
    reasons when any gate fails. Otherwise it is ``"savings_supported"`` when
    ``upper < 0``, ``"cost_increase_supported"`` when ``lower > 0``, and
    ``"inconclusive"`` when the interval includes zero.
    """
    if not isinstance(interval, Interval):
        raise StatsError("interval_invalid")
    _check_minimum(min_units, "min_units_invalid")
    _check_minimum(min_clusters, "min_clusters_invalid")
    if not isinstance(evidence_class, str) or not evidence_class:
        raise StatsError("evidence_class_invalid")
    if not isinstance(provenance_verified, bool):
        raise StatsError("provenance_verified_invalid")

    reasons: list[str] = []
    if evidence_class == SYNTHETIC:
        reasons.append("synthetic_evidence")
    elif evidence_class != CONSENTED_DEIDENTIFIED:
        reasons.append("evidence_not_consented_deidentified")
    if not provenance_verified:
        reasons.append("provenance_unverified")
    if interval.n_units < min_units:
        reasons.append("below_min_units")
    if interval.n_clusters < min_clusters:
        reasons.append("below_min_clusters")
    if interval.method == "insufficient_data" or interval.lower is None or interval.upper is None:
        reasons.append("insufficient_interval")

    if reasons:
        claim = "none"
    elif interval.upper < 0:
        claim = "savings_supported"
    elif interval.lower > 0:
        claim = "cost_increase_supported"
    else:
        claim = "inconclusive"
        reasons.append("interval_includes_zero")

    return {"claim": claim, "reasons": reasons, "rule": SAVINGS_RULE}


__all__ = [
    "CONSENTED_DEIDENTIFIED",
    "Interval",
    "PairedUnit",
    "SAVINGS_RULE",
    "StatsError",
    "SYNTHETIC",
    "paired_difference_interval",
    "savings_decision",
]
