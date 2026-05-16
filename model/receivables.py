"""Accounts receivable (AR) rolling balance with a fixed collection lag.

For each month:
    paid[m]            = total_revenue[m] * invoice_fulfillment_pct
    to_be_paid[m]      = total_revenue[m] - paid[m]
    initial_AR[m]      = final_AR[m-1]   (0 for m=1)
    collected[m]       = to_be_paid[m - lag]   (0 if month <= lag)
    final_AR[m]        = initial_AR[m] + to_be_paid[m] - collected[m]

Mirrors Excel rows 63–69 / 72–78 / 81–87 (one block per scenario).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs


def receivables(inputs: Inputs, scenario: str, total_revenue: np.ndarray) -> pd.DataFrame:
    H = inputs.horizon_months
    # Pull invoice_fulfillment_pct + collection_lag_months from the FIRST revenue stream.
    # Multi-stream models could weight this; for now the existing TSS model has 1 stream.
    rs = inputs.revenue_streams[0] if inputs.revenue_streams else None
    fulfillment = rs.invoice_fulfillment_pct if rs else 1.0
    lag = rs.collection_lag_months if rs else 0

    paid       = total_revenue * fulfillment
    to_be_paid = total_revenue - paid

    collected = np.zeros(H)
    for m in range(1, H + 1):
        if m > lag:
            collected[m - 1] = to_be_paid[m - 1 - lag]

    initial_ar = np.zeros(H)
    final_ar   = np.zeros(H)
    for m in range(H):
        initial_ar[m] = final_ar[m - 1] if m > 0 else 0.0
        final_ar[m]   = initial_ar[m] + to_be_paid[m] - collected[m]

    months = pd.RangeIndex(1, H + 1, name="month")
    return pd.DataFrame({
        "paid": paid,
        "to_be_paid": to_be_paid,
        "initial_ar": initial_ar,
        "collected": collected,
        "final_ar": final_ar,
    }, index=months)
