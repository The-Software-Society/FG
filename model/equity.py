"""Equity raises + grants → cumulative balance-sheet equity component.

Each raise / grant carries an optional `enabled_in_modes: list[str]`. When the
active mode isn't in that list, the raise contributes zero (skipped). If
`enabled_in_modes` is None (legacy), the raise applies in every mode.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs


def _enabled(item, mode: str) -> bool:
    """Item passes mode filter if enabled_in_modes is None or contains mode."""
    enabled = getattr(item, "enabled_in_modes", None)
    return enabled is None or mode in enabled


def _cumulative(amounts_by_month: dict[int, float], horizon: int) -> dict[str, np.ndarray]:
    investment = np.zeros(horizon)
    for m, amt in amounts_by_month.items():
        if 1 <= m <= horizon:
            investment[m - 1] += amt
    final = np.zeros(horizon)
    initial = np.zeros(horizon)
    for m in range(horizon):
        initial[m] = final[m - 1] if m > 0 else 0.0
        final[m] = initial[m] + investment[m]
    return {"initial": initial, "investment": investment, "final": final}


def equity(inputs: Inputs, mode: str = "base") -> pd.DataFrame:
    H = inputs.horizon_months
    by_month: dict[int, float] = {}
    for e in inputs.equity_raises:
        if not _enabled(e, mode):
            continue
        by_month[e.closing_month] = by_month.get(e.closing_month, 0.0) + e.amount
    s = _cumulative(by_month, H)
    months = pd.RangeIndex(1, H + 1, name="month")
    return pd.DataFrame(s, index=months)


def grants(inputs: Inputs, mode: str = "base") -> pd.DataFrame:
    H = inputs.horizon_months
    by_month: dict[int, float] = {}
    for g in inputs.grants:
        if not _enabled(g, mode):
            continue
        by_month[g.receiving_month] = by_month.get(g.receiving_month, 0.0) + g.amount
    s = _cumulative(by_month, H)
    months = pd.RangeIndex(1, H + 1, name="month")
    return pd.DataFrame(s, index=months)
