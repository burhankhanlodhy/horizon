from __future__ import annotations

from decimal import Decimal

import pytest

from horizon.desktop_mcp.evidence_stats import (
    SAVINGS_RULE,
    Interval,
    PairedUnit,
    StatsError,
    _percentile_indexes,
    paired_difference_interval,
    savings_decision,
)

GATE = {
    "min_units": 10,
    "min_clusters": 5,
    "evidence_class": "consented_deidentified",
    "provenance_verified": True,
}


def _participants(count: int, base: Decimal, spread: Decimal = Decimal("0.2")) -> list[PairedUnit]:
    """Each participant contributes two correlated pairs around ``base``."""
    units: list[PairedUnit] = []
    for i in range(count):
        units.append(PairedUnit(f"p{i:02d}", base + spread * (i % 3)))
        units.append(PairedUnit(f"p{i:02d}", base - spread * (i % 2)))
    return units


def _interval(units: list[PairedUnit], *, seed: int = 7, resamples: int = 1000) -> Interval:
    return paired_difference_interval(units, resamples=resamples, seed=seed)


# --- interval -------------------------------------------------------------


def test_same_seed_gives_identical_interval() -> None:
    units = _participants(12, Decimal("-5"))
    assert _interval(units, seed=42) == _interval(units, seed=42)


def test_input_order_does_not_change_interval() -> None:
    units = _participants(8, Decimal("-1"))
    assert _interval(units, seed=3) == _interval(list(reversed(units)), seed=3)


def test_equal_differences_give_degenerate_interval() -> None:
    units = [PairedUnit(f"p{i}", Decimal("-2")) for i in range(6)]
    result = _interval(units, seed=1)
    assert result.method == "cluster_percentile_bootstrap"
    assert result.estimate == Decimal("-2")
    assert result.lower == Decimal("-2")
    assert result.upper == Decimal("-2")
    assert result.n_units == 6
    assert result.n_clusters == 6


def test_interval_contains_estimate_for_mixed_data() -> None:
    units = _participants(15, Decimal("-3"), spread=Decimal("1"))
    result = _interval(units, seed=11, resamples=2000)
    assert result.lower is not None and result.upper is not None
    assert result.lower <= result.estimate <= result.upper
    assert result.lower < result.upper


def test_estimate_is_exact_decimal_mean() -> None:
    units = [
        PairedUnit("a", Decimal("1")),
        PairedUnit("b", Decimal("2")),
        PairedUnit("c", Decimal("4")),
    ]
    assert _interval(units).estimate == Decimal("7") / Decimal("3")


def test_percentile_index_convention() -> None:
    assert _percentile_indexes(10000, Decimal("0.95")) == (250, 9749)


def test_cluster_resampling_widens_interval_versus_per_pair_resampling() -> None:
    # One participant contributes many large pairs. Clustered resampling must
    # draw that participant as a block, so the interval is wider than when each
    # pair is treated as its own cluster.
    outlier = [PairedUnit("outlier", Decimal("10")) for _ in range(40)]
    others = [PairedUnit(f"p{i:02d}", Decimal("-1") if i % 2 else Decimal("1")) for i in range(10)]
    clustered = _interval(outlier + others, seed=5, resamples=2000)
    per_pair = _interval(
        [PairedUnit(f"u{i:03d}", unit.difference) for i, unit in enumerate(outlier + others)],
        seed=5,
        resamples=2000,
    )
    assert clustered.lower is not None and per_pair.lower is not None
    clustered_width = clustered.upper - clustered.lower
    per_pair_width = per_pair.upper - per_pair.lower
    assert clustered_width > per_pair_width


def test_single_cluster_is_insufficient_data() -> None:
    units = [PairedUnit("only", Decimal("-1")), PairedUnit("only", Decimal("-2"))]
    result = _interval(units)
    assert result.method == "insufficient_data"
    assert result.lower is None and result.upper is None
    assert result.estimate == Decimal("-1.5")
    assert result.n_clusters == 1


def test_no_units_is_insufficient_data() -> None:
    result = _interval([])
    assert result.method == "insufficient_data"
    assert result.estimate is None
    assert result.n_units == 0


def test_single_unit_is_insufficient_data() -> None:
    result = _interval([PairedUnit("p1", Decimal("-1"))])
    assert result.method == "insufficient_data"
    assert result.lower is None


# --- decision -------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence_class", "verified", "expected"),
    [
        ("consented_deidentified", True, "savings_supported"),
        ("consented_deidentified", False, "none"),
        ("synthetic", True, "none"),
        ("synthetic", False, "none"),
        ("unconsented", True, "none"),
    ],
)
def test_savings_supported_only_with_consented_and_verified(
    evidence_class: str, verified: bool, expected: str
) -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    decision = savings_decision(
        interval,
        min_units=10,
        min_clusters=5,
        evidence_class=evidence_class,
        provenance_verified=verified,
    )
    assert decision["claim"] == expected
    assert decision["rule"] == SAVINGS_RULE
    assert set(decision) == {"claim", "reasons", "rule"}
    if expected == "none":
        assert decision["reasons"]


def test_synthetic_evidence_reason() -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    decision = savings_decision(interval, **{**GATE, "evidence_class": "synthetic"})
    assert decision["claim"] == "none"
    assert "synthetic_evidence" in decision["reasons"]


def test_unverified_provenance_reason() -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    decision = savings_decision(interval, **{**GATE, "provenance_verified": False})
    assert decision["claim"] == "none"
    assert decision["reasons"] == ["provenance_unverified"]


def test_below_min_units_reason() -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    decision = savings_decision(interval, **{**GATE, "min_units": 25})
    assert decision["claim"] == "none"
    assert "below_min_units" in decision["reasons"]


def test_below_min_clusters_reason() -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    decision = savings_decision(interval, **{**GATE, "min_clusters": 13})
    assert decision["claim"] == "none"
    assert "below_min_clusters" in decision["reasons"]


def test_insufficient_interval_gives_none() -> None:
    interval = _interval([PairedUnit("p1", Decimal("-1"))])
    decision = savings_decision(interval, **{**GATE, "min_units": 2, "min_clusters": 2})
    assert decision["claim"] == "none"
    assert "insufficient_interval" in decision["reasons"]


def test_interval_spanning_zero_is_inconclusive() -> None:
    units = [PairedUnit(f"p{i:02d}", Decimal("1") if i % 2 else Decimal("-1")) for i in range(12)]
    interval = _interval(units, resamples=2000)
    assert interval.lower is not None and interval.lower < 0 < interval.upper
    decision = savings_decision(interval, **GATE)
    assert decision["claim"] == "inconclusive"
    assert decision["reasons"] == ["interval_includes_zero"]


def test_clearly_positive_is_cost_increase_supported() -> None:
    interval = _interval(_participants(12, Decimal("5")))
    decision = savings_decision(interval, **GATE)
    assert decision["claim"] == "cost_increase_supported"
    assert decision["reasons"] == []


def test_clearly_negative_with_enough_evidence_is_savings_supported() -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    decision = savings_decision(interval, **GATE)
    assert decision["claim"] == "savings_supported"
    assert decision["reasons"] == []


# --- validation -----------------------------------------------------------


def _unit() -> PairedUnit:
    return PairedUnit("p1", Decimal("-1"))


@pytest.mark.parametrize(
    "bad_difference",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.5, 2, "1"],
)
def test_rejects_non_finite_or_non_decimal_difference(bad_difference: object) -> None:
    with pytest.raises(StatsError) as excinfo:
        _interval([PairedUnit("p1", bad_difference)])  # type: ignore[arg-type]
    assert excinfo.value.code == "difference_invalid"


@pytest.mark.parametrize("bad_cluster", ["", "   ", None, 3])
def test_rejects_bad_cluster_id(bad_cluster: object) -> None:
    with pytest.raises(StatsError) as excinfo:
        _interval([PairedUnit(bad_cluster, Decimal("1"))])  # type: ignore[arg-type]
    assert excinfo.value.code == "cluster_id_invalid"


def test_rejects_non_unit_item() -> None:
    with pytest.raises(StatsError) as excinfo:
        _interval([("p1", Decimal("1"))])  # type: ignore[list-item]
    assert excinfo.value.code == "unit_invalid"


@pytest.mark.parametrize(
    "confidence",
    [Decimal("0.5"), Decimal("0.4"), Decimal("0.999"), Decimal("1"), 0.95, Decimal("NaN")],
)
def test_rejects_confidence_outside_open_range(confidence: object) -> None:
    with pytest.raises(StatsError) as excinfo:
        paired_difference_interval([_unit()], confidence=confidence, seed=1)  # type: ignore[arg-type]
    assert excinfo.value.code.startswith("confidence")


@pytest.mark.parametrize("resamples", [999, 100001, 0, True, 1000.0])
def test_rejects_resamples_outside_range(resamples: object) -> None:
    with pytest.raises(StatsError) as excinfo:
        paired_difference_interval([_unit()], resamples=resamples, seed=1)  # type: ignore[arg-type]
    assert excinfo.value.code.startswith("resamples")


@pytest.mark.parametrize("seed", [True, False, "1", 1.0, None])
def test_rejects_non_int_seed(seed: object) -> None:
    with pytest.raises(StatsError) as excinfo:
        paired_difference_interval([_unit()], seed=seed)  # type: ignore[arg-type]
    assert excinfo.value.code == "seed_invalid"


@pytest.mark.parametrize("key", ["min_units", "min_clusters"])
@pytest.mark.parametrize("value", [1, 0, True, 2.0])
def test_rejects_minimums_below_two(key: str, value: object) -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    kwargs = {**GATE, key: value}
    with pytest.raises(StatsError):
        savings_decision(interval, **kwargs)  # type: ignore[arg-type]


def test_rejects_non_interval_and_bad_gate_inputs() -> None:
    interval = _interval(_participants(12, Decimal("-5")))
    with pytest.raises(StatsError) as excinfo:
        savings_decision({"lower": 1}, **GATE)  # type: ignore[arg-type]
    assert excinfo.value.code == "interval_invalid"
    with pytest.raises(StatsError):
        savings_decision(interval, **{**GATE, "evidence_class": ""})
    with pytest.raises(StatsError):
        savings_decision(interval, **{**GATE, "provenance_verified": 1})  # type: ignore[arg-type]


def test_stats_error_is_value_error_with_code() -> None:
    with pytest.raises(ValueError) as excinfo:
        _interval([PairedUnit("p1", Decimal("NaN"))])
    assert isinstance(excinfo.value, StatsError)
    assert excinfo.value.code == "difference_invalid"


def test_self_metered_evidence_supports_claims_only_when_made_eligible() -> None:
    from horizon.desktop_mcp.evidence_stats import SELF_METERED

    units = [PairedUnit(f"g{index % 8}", Decimal("-0.05")) for index in range(16)]
    interval = paired_difference_interval(units, seed=3)
    default = savings_decision(
        interval,
        min_units=12,
        min_clusters=6,
        evidence_class=SELF_METERED,
        provenance_verified=True,
    )
    assert default["claim"] == "none"
    assert default["reasons"] == ["evidence_not_consented_deidentified"]
    scoped = savings_decision(
        interval,
        min_units=12,
        min_clusters=6,
        evidence_class=SELF_METERED,
        provenance_verified=True,
        eligible_classes=frozenset({SELF_METERED}),
    )
    assert scoped["claim"] == "savings_supported"
    other = savings_decision(
        interval,
        min_units=12,
        min_clusters=6,
        evidence_class="consented_deidentified",
        provenance_verified=True,
        eligible_classes=frozenset({SELF_METERED}),
    )
    assert other["reasons"] == ["evidence_class_not_eligible"]


def test_synthetic_can_never_be_made_eligible() -> None:
    interval = paired_difference_interval(
        [PairedUnit(f"g{index}", Decimal("-1")) for index in range(4)], seed=1
    )
    with pytest.raises(StatsError):
        savings_decision(
            interval,
            min_units=2,
            min_clusters=2,
            evidence_class="synthetic",
            provenance_verified=True,
            eligible_classes=frozenset({"synthetic"}),
        )
