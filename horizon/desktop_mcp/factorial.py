"""2x2 input-reduction x output-contract factorial analysis (build plan section 20).

Arms: ``F00`` (neither), ``F10`` (input reduction only), ``F01`` (output
contract only), ``F11`` (both). The first digit is input reduction and the
second is output contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from decimal import Decimal

from horizon.desktop_mcp.evidence_stats import (
    SAVINGS_RULE,
    Interval,
    PairedUnit,
    paired_difference_interval,
    savings_decision,
)

ARMS = ("F00", "F10", "F01", "F11")
_TWO = Decimal(2)


class FactorialError(ValueError):
    """Invalid factorial input, with a stable code and no source data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FactorialCell:
    unit_id: str  # one participant-task block
    cluster_id: str  # participant code
    arm: (
        str  # "F00" | "F10" | "F01" | "F11" (first digit: input reduction, second: output contract)
    )
    cost: Decimal | None
    success: bool


def _validate_cell(cell: FactorialCell) -> None:
    if not isinstance(cell, FactorialCell):
        raise FactorialError("cell_invalid")
    if not isinstance(cell.unit_id, str) or not cell.unit_id.strip():
        raise FactorialError("unit_id_invalid")
    if not isinstance(cell.cluster_id, str) or not cell.cluster_id.strip():
        raise FactorialError("cluster_id_invalid")
    if cell.arm not in ARMS:
        raise FactorialError("arm_invalid")
    if cell.cost is not None and (not isinstance(cell.cost, Decimal) or not cell.cost.is_finite()):
        raise FactorialError("cost_invalid")
    if not isinstance(cell.success, bool):
        raise FactorialError("success_invalid")


def _group(cells: Iterable[FactorialCell]) -> dict[str, dict[str, FactorialCell]]:
    units: dict[str, dict[str, FactorialCell]] = {}
    clusters: dict[str, str] = {}
    for cell in cells:
        _validate_cell(cell)
        arms = units.setdefault(cell.unit_id, {})
        if cell.arm in arms:
            raise FactorialError("duplicate_arm")
        known = clusters.setdefault(cell.unit_id, cell.cluster_id)
        if known != cell.cluster_id:
            raise FactorialError("cluster_mismatch")
        arms[cell.arm] = cell
    return units


def _interval_dict(interval: Interval) -> dict:
    data = asdict(interval)
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in data.items()}


def _arm_summary(cells: list[FactorialCell]) -> dict:
    n = len(cells)
    if n == 0:
        return {"n": 0, "mean_cost": None, "success_rate": None}
    total = sum((cell.cost for cell in cells), Decimal(0))
    successes = sum(1 for cell in cells if cell.success)
    return {
        "n": n,
        "mean_cost": str(total / Decimal(n)),
        "success_rate": str(Decimal(successes) / Decimal(n)),
    }


def analyze_factorial(
    cells: Iterable[FactorialCell],
    *,
    seed: int,
    confidence: Decimal = Decimal("0.95"),
    resamples: int = 10000,
    min_units: int,
    min_clusters: int,
    evidence_class: str,
    provenance_verified: bool,
) -> dict:
    """Per-unit effects with cluster-bootstrap intervals and savings decisions.

    Only units with all four arms and non-null costs are analysed. Others are
    listed in ``excluded_units`` with reason ``missing_arm`` or
    ``missing_cost``. Effects per unit:

    - input reduction = ((F10 - F00) + (F11 - F01)) / 2
    - output contract = ((F01 - F00) + (F11 - F10)) / 2
    - interaction = F11 - F10 - F01 + F00

    Input and output get a ``savings_decision``. The interaction always has
    claim ``"none"``. Intervals are returned as strings to keep the result
    JSON-safe.
    """
    units = _group(cells)

    excluded: list[dict] = []
    analysed: dict[str, dict[str, FactorialCell]] = {}
    for unit_id in sorted(units):
        arms = units[unit_id]
        if set(arms) != set(ARMS):
            excluded.append({"unit_id": unit_id, "reason": "missing_arm"})
        elif any(arms[arm].cost is None for arm in ARMS):
            excluded.append({"unit_id": unit_id, "reason": "missing_cost"})
        else:
            analysed[unit_id] = arms

    input_units: list[PairedUnit] = []
    output_units: list[PairedUnit] = []
    interaction_units: list[PairedUnit] = []
    clusters: set[str] = set()
    arm_cells: dict[str, list[FactorialCell]] = {arm: [] for arm in ARMS}
    for arms in analysed.values():
        f00, f10, f01, f11 = (arms[arm].cost for arm in ARMS)
        cluster_id = arms["F00"].cluster_id
        clusters.add(cluster_id)
        for arm in ARMS:
            arm_cells[arm].append(arms[arm])
        input_units.append(PairedUnit(cluster_id, ((f10 - f00) + (f11 - f01)) / _TWO))
        output_units.append(PairedUnit(cluster_id, ((f01 - f00) + (f11 - f10)) / _TWO))
        interaction_units.append(PairedUnit(cluster_id, f11 - f10 - f01 + f00))

    shared = {
        "seed": seed,
        "confidence": confidence,
        "resamples": resamples,
    }
    gate = {
        "min_units": min_units,
        "min_clusters": min_clusters,
        "evidence_class": evidence_class,
        "provenance_verified": provenance_verified,
    }

    effects: dict[str, dict] = {}
    for name, effect_units in (
        ("input_reduction", input_units),
        ("output_contract", output_units),
    ):
        interval = paired_difference_interval(effect_units, **shared)
        effects[name] = {
            "interval": _interval_dict(interval),
            "decision": savings_decision(interval, **gate),
        }

    interaction = paired_difference_interval(interaction_units, **shared)
    effects["interaction"] = {
        "interval": _interval_dict(interaction),
        "decision": {
            "claim": "none",
            "reasons": ["interaction_is_not_a_savings_claim"],
            "rule": SAVINGS_RULE,
        },
    }

    return {
        "status": "analysed",
        "n_units_total": len(units),
        "n_units_analysed": len(analysed),
        "n_clusters": len(clusters),
        "excluded_units": excluded,
        "arms": {arm: _arm_summary(arm_cells[arm]) for arm in ARMS},
        "effects": effects,
    }


__all__ = ["ARMS", "FactorialCell", "FactorialError", "analyze_factorial"]
