"""Straight-line depreciation/amortization for CAPEX and Tech investments.

For each item:
    investment[m] = value if month == acquisition_month else 0
    period_dep[m] = value / life if acquisition <= month < acquisition + life else 0
    final_value[m] = initial[m] + investment[m] - period_dep[m]
    accumulated[m] = Σ period_dep up to and including m

Excel rows 184–208 (CAPEX) and 213–237 (Tech).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs


def _schedule(value: float, life: int, acq_month: int, horizon: int) -> dict[str, np.ndarray]:
    investment = np.zeros(horizon)
    period_dep = np.zeros(horizon)
    if 1 <= acq_month <= horizon:
        investment[acq_month - 1] = value
    for m in range(1, horizon + 1):
        if life > 0 and acq_month <= m < acq_month + life:
            period_dep[m - 1] = value / life
    final_value = np.zeros(horizon)
    accumulated = np.zeros(horizon)
    for m in range(horizon):
        prev_final = final_value[m - 1] if m > 0 else 0.0
        prev_acc   = accumulated[m - 1] if m > 0 else 0.0
        final_value[m] = prev_final + investment[m] - period_dep[m]
        accumulated[m] = prev_acc + period_dep[m]
    return {
        "investment": investment,
        "period_dep": period_dep,
        "final_value": final_value,
        "accumulated": accumulated,
    }


def capex(inputs: Inputs) -> pd.DataFrame:
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    df = pd.DataFrame(index=months, data={
        "investment": np.zeros(H),
        "period_dep": np.zeros(H),
        "final_value": np.zeros(H),
        "accumulated": np.zeros(H),
    })
    for it in inputs.capex:
        s = _schedule(it.value, it.depreciation_months, it.acquisition_month, H)
        for k, v in s.items():
            df[k] += v
    return df


def tech(inputs: Inputs) -> pd.DataFrame:
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    df = pd.DataFrame(index=months, data={
        "investment": np.zeros(H),
        "period_dep": np.zeros(H),
        "final_value": np.zeros(H),
        "accumulated": np.zeros(H),
    })
    for it in inputs.tech_investments:
        s = _schedule(it.value, it.amortization_months, it.acquisition_month, H)
        for k, v in s.items():
            df[k] += v
    return df
