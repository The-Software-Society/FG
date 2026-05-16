"""Accounts payable (AP) rolling balance, mirror of receivables but for cost-of-sales.

For each month:
    initial_AP[m] = final_AP[m-1]
    paid[m]       = total_costs[m - lag]   (0 if month <= lag)
    final_AP[m]   = initial_AP[m] + total_costs[m] - paid[m]

Excel rows 117–120 / 129–132 / 141–144.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs


def payables(inputs: Inputs, total_costs: np.ndarray) -> pd.DataFrame:
    H = inputs.horizon_months
    lag = int(inputs.payment_lag_months)

    paid = np.zeros(H)
    for m in range(1, H + 1):
        if m > lag:
            paid[m - 1] = total_costs[m - 1 - lag]

    initial_ap = np.zeros(H)
    final_ap   = np.zeros(H)
    for m in range(H):
        initial_ap[m] = final_ap[m - 1] if m > 0 else 0.0
        final_ap[m]   = initial_ap[m] + total_costs[m] - paid[m]

    months = pd.RangeIndex(1, H + 1, name="month")
    return pd.DataFrame({
        "to_be_paid": total_costs,
        "paid": paid,
        "initial_ap": initial_ap,
        "final_ap": final_ap,
    }, index=months)
