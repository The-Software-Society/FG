"""Cost-of-sales / cost streams.

Four kinds, all defined in inputs.yaml:
    - "step":              fixed monthly amount within a [start, end] window
    - "per_active_client": amount × active_client_count, with a per-year amount
                           (3-element list for Y1/Y2/Y3)
    - "per_credit":        amount × total_credits_used (legacy AI-credits model)
    - "fraction_of_addon": cost = fraction × addon_revenue (e.g. OpenAI =
                           30% of Token Usage revenue)

The Excel uses VLOOKUP-with-TRUE for step tables and per-year multiplications
hard-coded by month index. We replicate the semantics; per_active_client and
per_credit pick the year amount by `(month-1) // 12`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs, CostStream
from model import revenue


def _per_year_amount(by_year: list[float], month: int, horizon: int) -> float:
    """Pick the year amount for `month` (1-indexed). Months 1..12 → year_idx 0, etc.

    If `by_year` is shorter than the model horizon, the last entry repeats.
    """
    year_idx = min((month - 1) // 12, len(by_year) - 1) if by_year else 0
    return float(by_year[year_idx]) if by_year else 0.0


def cost_stream_vector(stream: CostStream, inputs: Inputs, scenario: str,
                       sales_headcount: np.ndarray | None = None) -> np.ndarray:
    """Return a horizon-length array of monthly cost for one stream under one scenario."""
    H = inputs.horizon_months
    out = np.zeros(H)

    if stream.kind == "step":
        sched = stream.schedule
        for m in range(1, H + 1):
            out[m - 1] = sched.value_at(m) if sched else 0.0

    elif stream.kind == "per_active_client":
        active = revenue.active_clients_total(inputs, scenario, sales_headcount)
        for m in range(1, H + 1):
            out[m - 1] = active[m - 1] * _per_year_amount(stream.per_active_client_by_year or [], m, H)

    elif stream.kind == "per_credit":
        credits = revenue.total_credits_used(inputs, scenario, sales_headcount)
        for m in range(1, H + 1):
            out[m - 1] = credits[m - 1] * _per_year_amount(stream.per_credit_by_year or [], m, H)

    elif stream.kind == "fraction_of_addon":
        # cost = fraction × revenue from a named add-on (e.g. OpenAI cost = 30% of Token Usage).
        # If `fraction_schedule` is set, use a per-month fraction (e.g. 30% Y1 → 8% Y3
        # to reflect AI inference cost reductions at scale).
        addon_rev = revenue.addon_revenue_for(inputs, scenario, stream.addon_name or "", sales_headcount)
        if stream.fraction_schedule is not None:
            fractions = np.array([stream.fraction_schedule.value_at(m) for m in range(1, H + 1)])
            out = addon_rev * fractions
        else:
            fraction = float(stream.fraction or 0.0)
            out = addon_rev * fraction

    else:
        raise ValueError(f"Unknown cost stream kind: {stream.kind!r}")

    return out


def total_costs(inputs: Inputs, scenario: str,
                sales_headcount: np.ndarray | None = None) -> pd.DataFrame:
    """Per-stream and total cost-of-sales by month."""
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    df = pd.DataFrame(index=months)
    for i, cs in enumerate(inputs.cost_streams):
        df[f"stream{i}:{cs.name}"] = cost_stream_vector(cs, inputs, scenario, sales_headcount)
    df["total"] = df.sum(axis=1) if not df.empty else np.zeros(H)
    return df
