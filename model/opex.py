"""OpEx categories — each a step schedule (start_month, end_month, monthly amount).

Scenario + mode resolution per category, in priority order:
    1. enabled_in_modes        — if set and active mode not listed, contributes 0.
    2. schedule_by_scenario     — if set and contains active scenario, use it.
    3. schedule                  — default fallback.

Lets investors see how Marketing differs across (scenario, mode):
e.g. Optimistic+with_investment scales ad spend to $40k/mo Y3 while
Conservative+no_investment stays at $1k/mo (because growth doesn't justify it).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs


def opex(inputs: Inputs, mode: str = "base", scenario: str | None = None) -> pd.DataFrame:
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    df = pd.DataFrame(index=months)
    for cat in inputs.opex:
        if cat.enabled_in_modes is not None and mode not in cat.enabled_in_modes:
            df[cat.name] = np.zeros(H)
            continue
        # Resolve schedule: prefer scenario-specific, fall back to default
        sched = cat.schedule
        if scenario is not None and cat.schedule_by_scenario and scenario in cat.schedule_by_scenario:
            sched = cat.schedule_by_scenario[scenario]
        v = np.array([sched.value_at(m) for m in range(1, H + 1)])
        df[cat.name] = v
    df["total"] = df.sum(axis=1) if not df.empty else np.zeros(H)
    return df
