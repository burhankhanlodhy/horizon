from __future__ import annotations

from decimal import Decimal

import pytest

from horizon.desktop_mcp.evidence_stats import SAVINGS_RULE
from horizon.desktop_mcp.factorial import ARMS, FactorialCell, FactorialError, analyze_factorial

GATE = {
    "min_units": 3,
    "min_clusters": 2,
    "evidence_class": "consented_deidentified",
    "provenance_verified": True,
}


def _unit(
    unit_id: str,
    cluster_id: str,
    costs: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None],
    successes: tuple[bool, bool, bool, bool] = (True, True, True, True),
) -> list[FactorialCell]:
    return [
        FactorialCell(unit_id, cluster_id, arm, cost, success)
        for arm, cost, success in zip(ARMS, costs, successes, strict=True)
    ]


def _block(unit_id: str, cluster_id: str, f00, f10, f01, f11) -> list[FactorialCell]:
    return _unit(
        unit_id,
        cluster_id,
        (Decimal(f00), Decimal(f10), Decimal(f01), Decimal(f11)),
    )


def _analyse(cells, **overrides):
    kwargs = {"seed": 9, "resamples": 1000, **GATE, **overrides}
    return analyze_factorial(cells, **kwargs)


def _estimate(result: dict, effect: str) -> Decimal:
    return Decimal(result["effects"][effect]["interval"]["estimate"])


def _cells_from(blocks: list[tuple[str, str, int, int, int, int]]) -> list[FactorialCell]:
    cells: list[FactorialCell] = []
    for unit_id, cluster_id, f00, f10, f01, f11 in blocks:
        cells.extend(_block(unit_id, cluster_id, f00, f10, f01, f11))
    return cells


# --- known answers --------------------------------------------------------


def test_known_answer_effects() -> None:
    # F00=10, F10=7, F01=9, F11=6 gives input -3, output -1, interaction 0.
    cells = _cells_from(
        [
            ("u1", "p1", 10, 7, 9, 6),
            ("u2", "p2", 10, 7, 9, 6),
            ("u3", "p3", 10, 7, 9, 6),
        ]
    )
    result = _analyse(cells)
    assert result["status"] == "analysed"
    assert result["n_units_total"] == 3
    assert result["n_units_analysed"] == 3
    assert result["n_clusters"] == 3
    assert result["excluded_units"] == []
    assert _estimate(result, "input_reduction") == Decimal("-3")
    assert _estimate(result, "output_contract") == Decimal("-1")
    assert _estimate(result, "interaction") == Decimal("0")


def test_interaction_is_nonzero_when_effects_do_not_add() -> None:
    # F11 - F10 - F01 + F00 = 3 - 7 - 9 + 10 = -3
    cells = _cells_from([("u1", "p1", 10, 7, 9, 3), ("u2", "p2", 10, 7, 9, 3)])
    result = _analyse(cells)
    assert _estimate(result, "interaction") == Decimal("-3")
    assert result["effects"]["interaction"]["decision"]["claim"] == "none"


def test_savings_claim_for_cheaper_input_reduction() -> None:
    cells = _cells_from(
        [("u1", "p1", 10, 7, 9, 6), ("u2", "p2", 10, 7, 9, 6), ("u3", "p3", 10, 7, 9, 6)]
    )
    result = _analyse(cells)
    decision = result["effects"]["input_reduction"]["decision"]
    assert decision["claim"] == "savings_supported"
    assert decision["rule"] == SAVINGS_RULE
    assert result["effects"]["output_contract"]["decision"]["claim"] == "savings_supported"


def test_cost_increase_claim_when_candidate_is_dearer() -> None:
    cells = _cells_from(
        [("u1", "p1", 10, 13, 9, 12), ("u2", "p2", 10, 13, 9, 12), ("u3", "p3", 10, 13, 9, 12)]
    )
    result = _analyse(cells)
    assert result["effects"]["input_reduction"]["decision"]["claim"] == "cost_increase_supported"


def test_synthetic_evidence_blocks_claims_but_not_estimates() -> None:
    cells = _cells_from(
        [("u1", "p1", 10, 7, 9, 6), ("u2", "p2", 10, 7, 9, 6), ("u3", "p3", 10, 7, 9, 6)]
    )
    result = _analyse(cells, evidence_class="synthetic")
    decision = result["effects"]["input_reduction"]["decision"]
    assert decision["claim"] == "none"
    assert "synthetic_evidence" in decision["reasons"]
    assert _estimate(result, "input_reduction") == Decimal("-3")


def test_unverified_provenance_blocks_claim() -> None:
    cells = _cells_from(
        [("u1", "p1", 10, 7, 9, 6), ("u2", "p2", 10, 7, 9, 6), ("u3", "p3", 10, 7, 9, 6)]
    )
    result = _analyse(cells, provenance_verified=False)
    assert result["effects"]["output_contract"]["decision"]["claim"] == "none"
    assert "provenance_unverified" in result["effects"]["output_contract"]["decision"]["reasons"]


def test_below_minimum_units_blocks_claim() -> None:
    cells = _cells_from([("u1", "p1", 10, 7, 9, 6), ("u2", "p2", 10, 7, 9, 6)])
    result = _analyse(cells, min_units=3)
    decision = result["effects"]["input_reduction"]["decision"]
    assert decision["claim"] == "none"
    assert "below_min_units" in decision["reasons"]


def test_interaction_claim_is_always_none() -> None:
    cells = _cells_from(
        [("u1", "p1", 10, 6, 6, 2), ("u2", "p2", 10, 6, 6, 2), ("u3", "p3", 10, 6, 6, 2)]
    )
    result = _analyse(cells)
    interaction = result["effects"]["interaction"]
    assert interaction["decision"]["claim"] == "none"
    assert interaction["decision"]["reasons"]
    assert "interval" in interaction


def test_inconclusive_when_interval_spans_zero() -> None:
    blocks = []
    for i in range(12):
        flip = 1 if i % 2 else -1
        blocks.append((f"u{i}", f"p{i}", 10, 10 + flip, 10, 10))
    result = _analyse(_cells_from(blocks), min_units=10, min_clusters=5, resamples=2000)
    decision = result["effects"]["input_reduction"]["decision"]
    assert decision["claim"] == "inconclusive"


# --- exclusions -----------------------------------------------------------


def test_incomplete_block_is_excluded_as_missing_arm() -> None:
    cells = _cells_from([("u1", "p1", 10, 7, 9, 6), ("u2", "p2", 10, 7, 9, 6)])
    partial = _block("u3", "p3", 10, 7, 9, 6)[:3]
    result = _analyse(cells + partial)
    assert result["n_units_total"] == 3
    assert result["n_units_analysed"] == 2
    assert result["excluded_units"] == [{"unit_id": "u3", "reason": "missing_arm"}]


def test_null_cost_is_excluded_as_missing_cost() -> None:
    cells = _cells_from([("u1", "p1", 10, 7, 9, 6), ("u2", "p2", 10, 7, 9, 6)])
    nulled = _unit("u3", "p3", (Decimal(10), None, Decimal(9), Decimal(6)))
    result = _analyse(cells + nulled)
    assert result["excluded_units"] == [{"unit_id": "u3", "reason": "missing_cost"}]
    assert result["n_units_analysed"] == 2


def test_missing_arm_takes_precedence_over_null_cost() -> None:
    cells = _unit("u1", "p1", (None, Decimal(7), Decimal(9), Decimal(6)))[1:]
    result = _analyse(cells)
    assert result["excluded_units"] == [{"unit_id": "u1", "reason": "missing_arm"}]


def test_no_cells_gives_empty_analysis() -> None:
    result = _analyse([])
    assert result["n_units_total"] == 0
    assert result["n_units_analysed"] == 0
    assert result["effects"]["input_reduction"]["decision"]["claim"] == "none"
    assert result["effects"]["input_reduction"]["interval"]["estimate"] is None


# --- per-arm summaries ----------------------------------------------------


def test_per_arm_summaries() -> None:
    cells = _unit(
        "u1",
        "p1",
        (Decimal(10), Decimal(7), Decimal(9), Decimal(6)),
        successes=(False, True, True, True),
    ) + _unit(
        "u2",
        "p2",
        (Decimal(12), Decimal(9), Decimal(11), Decimal(8)),
        successes=(True, True, False, True),
    )
    result = _analyse(cells, min_units=2)
    assert result["arms"]["F00"] == {
        "n": 2,
        "mean_cost": "11",
        "success_rate": "0.5",
    }
    assert result["arms"]["F11"] == {
        "n": 2,
        "mean_cost": "7",
        "success_rate": "1",
    }
    assert result["arms"]["F01"]["success_rate"] == "0.5"


def test_arm_summaries_count_only_analysed_units() -> None:
    cells = _cells_from([("u1", "p1", 10, 7, 9, 6), ("u2", "p2", 10, 7, 9, 6)])
    cells += _unit("u3", "p3", (Decimal(10), None, Decimal(9), Decimal(6)))
    result = _analyse(cells, min_units=2)
    assert result["arms"]["F10"]["n"] == 2


# --- validation -----------------------------------------------------------


def test_duplicate_arm_is_error() -> None:
    cells = _block("u1", "p1", 10, 7, 9, 6) + [FactorialCell("u1", "p1", "F10", Decimal(7), True)]
    with pytest.raises(FactorialError) as excinfo:
        _analyse(cells)
    assert excinfo.value.code == "duplicate_arm"


def test_cluster_mismatch_within_unit_is_error() -> None:
    cells = _block("u1", "p1", 10, 7, 9, 6)
    cells[1] = FactorialCell("u1", "p2", "F10", Decimal(7), True)
    with pytest.raises(FactorialError) as excinfo:
        _analyse(cells)
    assert excinfo.value.code == "cluster_mismatch"


def test_unknown_arm_is_error() -> None:
    with pytest.raises(FactorialError) as excinfo:
        _analyse([FactorialCell("u1", "p1", "F20", Decimal(1), True)])
    assert excinfo.value.code == "arm_invalid"


@pytest.mark.parametrize(
    ("cell", "code"),
    [
        (FactorialCell("", "p1", "F00", Decimal(1), True), "unit_id_invalid"),
        (FactorialCell("u1", "", "F00", Decimal(1), True), "cluster_id_invalid"),
        (FactorialCell("u1", "p1", "F00", Decimal("NaN"), True), "cost_invalid"),
        (FactorialCell("u1", "p1", "F00", 1.0, True), "cost_invalid"),  # type: ignore[arg-type]
        (FactorialCell("u1", "p1", "F00", Decimal(1), 1), "success_invalid"),  # type: ignore[arg-type]
    ],
)
def test_invalid_cell_fields_are_errors(cell: FactorialCell, code: str) -> None:
    with pytest.raises(FactorialError) as excinfo:
        _analyse([cell])
    assert excinfo.value.code == code


def test_invalid_stats_inputs_propagate_as_stats_errors() -> None:
    from horizon.desktop_mcp.evidence_stats import StatsError

    cells = _cells_from([("u1", "p1", 10, 7, 9, 6)])
    with pytest.raises(StatsError):
        _analyse(cells, seed=True)  # type: ignore[arg-type]
    with pytest.raises(StatsError):
        _analyse(cells, min_clusters=1)


def test_factorial_error_is_value_error() -> None:
    assert issubclass(FactorialError, ValueError)
