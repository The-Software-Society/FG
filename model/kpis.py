"""Headline KPIs computed from a `ScenarioResult`.

Two metrics:
    1. first_profitable_month — first month where pnl["net_result"] > 0
    2. gm_steady_state — month from which monthly Gross Margin enters and stays
       within ±band of its long-run median for the rest of the horizon, plus
       that long-run value.

The steady-state algorithm walks forward through monthly GM. It returns the
first month from which every subsequent GM observation is within ±`band` of
the long-run median (median of the last 6 months by default). This implements
Jon's heuristic ("from that first 90 it steadies out 90+") with a tolerance for
small dips like the example sequence 89, 90, 91, 89, 90, ...
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _gm_steady_state(gm: np.ndarray, band: float = 0.03, window: int = 6) -> dict | None:
    """Return {month, value} or None.

    `gm` may contain NaN for zero-revenue months — those are skipped from both
    the long-run median and the in-band check (NaN months allow the band test
    to pass through them).
    """
    valid_mask = ~np.isnan(gm)
    valid = gm[valid_mask]
    if len(valid) < window:
        return None
    long_run = float(np.median(valid[-window:]))

    # Walk forward; require every later non-NaN GM to be within band of long_run.
    n = len(gm)
    for m in range(n):
        ok = True
        for i in range(m, n):
            if np.isnan(gm[i]):
                continue
            if abs(gm[i] - long_run) > band:
                ok = False
                break
        if ok:
            # Skip months that are themselves NaN (zero-revenue) — return the
            # first month with actual revenue from which steady-state holds.
            if not np.isnan(gm[m]):
                return {"month": m + 1, "value": long_run}
    return None


def compute_kpis(pnl: pd.DataFrame, cash_flow: pd.DataFrame | None = None) -> dict:
    """Compute headline KPIs from a P&L DataFrame indexed by month, plus
    cash-trough metrics if a cash-flow DataFrame is provided.

    Keys:
        first_profitable_month            — first month Net Result > 0.
        first_sustained_profit_month      — first month after which Net Result
                                             stays > 0 for the rest of the horizon.
        gm_steady_state_month/_value      — long-run gross margin band entry month.
        cash_trough_month/_value          — month where final_cash hits its lowest
                                             (and the dollar value at that point;
                                             negative = unfunded; only set if
                                             cash_flow is supplied).
    """
    revenue = pnl["revenue"].values
    gross   = pnl["gross_result"].values
    net     = pnl["net_result"].values

    with np.errstate(divide="ignore", invalid="ignore"):
        gm = np.where(revenue > 0, gross / revenue, np.nan)

    first_profitable = next(
        (m + 1 for m in range(len(net)) if net[m] > 0),
        None,
    )

    first_sustained = None
    for m in range(len(net)):
        if all(net[i] > 0 for i in range(m, len(net))):
            first_sustained = m + 1
            break

    steady = _gm_steady_state(gm)

    cash_trough_month: int | None = None
    cash_trough_value: float | None = None
    if cash_flow is not None and "final_cash" in cash_flow.columns:
        cash = cash_flow["final_cash"].values
        if len(cash) > 0:
            idx = int(np.argmin(cash))
            cash_trough_month = idx + 1
            cash_trough_value = float(cash[idx])

    return {
        "first_profitable_month":       first_profitable,
        "first_sustained_profit_month": first_sustained,
        "gm_steady_state_month":        steady["month"] if steady else None,
        "gm_steady_state_value":        steady["value"] if steady else None,
        "gross_margin_monthly":         gm,
        "cash_trough_month":            cash_trough_month,
        "cash_trough_value":            cash_trough_value,
    }
