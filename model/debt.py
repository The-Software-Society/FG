"""Debt amortization with PMT-based principal/interest split.

Excel rows 242–263. Each debt instrument:
    initial_balance[m] = principal at disbursement_month, else previous final
    interest[m]        = initial_balance[m] * monthly_rate (when in active window)
    principal[m]       = -PMT(rate, term, principal) - interest[m]    (active window only)
    final_balance[m]   = initial_balance[m] - principal[m]

Active window = [disbursement_month, disbursement_month + term_months).
"""
from __future__ import annotations

import numpy as np
import numpy_financial as npf
import pandas as pd

from model.config import Inputs


def _instrument(principal: float, term: int, rate: float, disbursement: int, horizon: int) -> dict[str, np.ndarray]:
    pmt = -npf.pmt(rate, term, principal) if (rate > 0 and term > 0) else (principal / term if term > 0 else 0.0)

    initial = np.zeros(horizon)
    interest = np.zeros(horizon)
    principal_paid = np.zeros(horizon)
    final = np.zeros(horizon)

    for m in range(horizon):
        month_no = m + 1
        prev_final = final[m - 1] if m > 0 else 0.0
        if month_no == disbursement:
            initial[m] = principal
        else:
            initial[m] = prev_final

        in_window = disbursement <= month_no < disbursement + term
        if in_window and initial[m] > 1e-9:
            interest[m] = initial[m] * rate
            principal_paid[m] = max(0.0, pmt - interest[m])
            principal_paid[m] = min(principal_paid[m], initial[m])  # clamp final period

        final[m] = initial[m] - principal_paid[m]

    return {
        "initial": initial,
        "interest": interest,
        "principal": principal_paid,
        "final": final,
    }


def debt(inputs: Inputs) -> pd.DataFrame:
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    df = pd.DataFrame(index=months, data={
        "initial": np.zeros(H),
        "interest": np.zeros(H),
        "principal": np.zeros(H),
        "final": np.zeros(H),
    })
    for d in inputs.debt:
        s = _instrument(d.principal, d.term_months, d.monthly_rate, d.disbursement_month, H)
        for k, v in s.items():
            df[k] += v
    return df
