"""Salary computation: mode-aware base × headcount + fringe benefits.

Headcount resolution per role per month (most-specific wins):
    1. hire_trigger_by_mode[mode]   — self-funding (resolved by runner via solver)
    2. headcount_by_mode[mode]      — static per-mode 36-month array
    3. headcount_by_month           — legacy single array (used for every mode)

Base salary resolution (per role per month):
    1. pay_triggers_by_mode[mode]         — MRR-stacked steps (max amount whose triggers met)
    2. base_monthly_schedule_by_mode[mode] — per-mode step schedule
    3. base_monthly_schedule              — schedule applied to every mode
    4. base_monthly                        — flat fallback

For self-funding modes (where hire_trigger_by_mode or pay_triggers_by_mode is set),
the runner pre-resolves headcount and pay arrays and passes them in via
`headcount_overrides` / `pay_overrides` so this module stays vectorized.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs, SalaryRole


def _static_headcount(role: SalaryRole, mode: str, H: int) -> np.ndarray:
    """Resolve a static headcount array for `role` under `mode`. Returns
    zeros if no source is configured (the caller should be using a trigger
    instead in that case)."""
    if role.headcount_by_mode is not None and mode in role.headcount_by_mode:
        return np.array(role.headcount_by_mode[mode][:H], dtype=float)
    if role.headcount_by_month is not None:
        return np.array(role.headcount_by_month[:H], dtype=float)
    return np.zeros(H, dtype=float)


def _static_base(role: SalaryRole, mode: str, H: int) -> np.ndarray:
    """Resolve a static per-month base salary array for `role` under `mode`."""
    sched = None
    if role.base_monthly_schedule_by_mode is not None and mode in role.base_monthly_schedule_by_mode:
        sched = role.base_monthly_schedule_by_mode[mode]
    elif role.base_monthly_schedule is not None:
        sched = role.base_monthly_schedule
    if sched is not None:
        return np.array([sched.value_at(m) for m in range(1, H + 1)], dtype=float)
    return np.full(H, float(role.base_monthly))


def resolve_role_headcount(role: SalaryRole, mode: str, H: int,
                           override: np.ndarray | None = None) -> np.ndarray:
    if override is not None:
        return override
    return _static_headcount(role, mode, H)


def resolve_role_base(role: SalaryRole, mode: str, H: int,
                      override: np.ndarray | None = None) -> np.ndarray:
    if override is not None:
        return override
    return _static_base(role, mode, H)


def salaries(inputs: Inputs, mode: str = "base",
             headcount_overrides: dict[str, np.ndarray] | None = None,
             pay_overrides: dict[str, np.ndarray] | None = None) -> pd.DataFrame:
    """Per-month per-role compensation. Returns DataFrame with one column per
    role (containing the role's monthly salary line, headcount × base_monthly),
    plus total_base, fringe, total_salaries.
    """
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    sal = inputs.salaries

    base_total = np.zeros(H)
    role_breakdown: dict[str, np.ndarray] = {}
    for r in sal.roles:
        hc_override = (headcount_overrides or {}).get(r.name)
        pay_override = (pay_overrides or {}).get(r.name)
        hc = resolve_role_headcount(r, mode, H, hc_override)
        pay = resolve_role_base(r, mode, H, pay_override)
        v = hc * pay
        role_breakdown[r.name] = v
        base_total += v

    fringe = base_total * float(sal.fringe_benefits_pct)
    total = base_total + fringe

    df = pd.DataFrame(role_breakdown, index=months)
    df["total_base"] = base_total
    df["fringe"] = fringe
    df["total_salaries"] = total
    return df


def labor_obligations(inputs: Inputs, fringe: np.ndarray) -> pd.DataFrame:
    """Balance-sheet rolling labor obligation.

    For periodicity = 1: paid every month, final balance always 0.
    For periodicity > 1: accrues until a payment month divides cleanly.
    """
    H = inputs.horizon_months
    period = int(inputs.salaries.payment_periodicity_months)

    initial = np.zeros(H)
    paid = np.zeros(H)
    final = np.zeros(H)

    for m in range(H):
        initial[m] = final[m - 1] if m > 0 else 0.0
        if period == 1:
            paid[m] = fringe[m]
        else:
            month_no = m + 1
            if month_no % period == 0:
                paid[m] = initial[m] + fringe[m]
        final[m] = initial[m] + fringe[m] - paid[m]

    months = pd.RangeIndex(1, H + 1, name="month")
    return pd.DataFrame({
        "initial": initial, "to_be_paid": fringe, "paid": paid, "final": final,
    }, index=months)


# ── Headcount aggregation helpers (used by sales-driven revenue + hire trigger gates) ──

def sales_headcount(salaries_df: pd.DataFrame) -> np.ndarray:
    """Sum of monthly headcount across any role whose name contains 'Sales'."""
    cols = [c for c in salaries_df.columns
            if "Sales" in c and c not in ("total_base", "fringe", "total_salaries")]
    if not cols:
        return np.zeros(len(salaries_df))
    # Each column holds headcount × pay; we need headcount.
    # Caller should use `role_headcount_grid` instead for raw headcount.
    raise RuntimeError("sales_headcount called on $-valued salaries_df; use role_headcount_grid")


def role_headcount_grid(inputs: Inputs, mode: str,
                        headcount_overrides: dict[str, np.ndarray] | None = None) -> dict[str, np.ndarray]:
    """Resolve raw headcount per role per month under `mode`. Used by the runner
    to build sales/support aggregates for sales-driven acquisition + trigger gates.
    """
    H = inputs.horizon_months
    out: dict[str, np.ndarray] = {}
    for r in inputs.salaries.roles:
        override = (headcount_overrides or {}).get(r.name)
        out[r.name] = resolve_role_headcount(r, mode, H, override)
    return out


def aggregate_sales_headcount(grid: dict[str, np.ndarray]) -> np.ndarray:
    return _aggregate_by_keyword(grid, "Sales")


def aggregate_support_headcount(grid: dict[str, np.ndarray]) -> np.ndarray:
    return _aggregate_by_keyword(grid, "Support")


def _aggregate_by_keyword(grid: dict[str, np.ndarray], keyword: str) -> np.ndarray:
    matched = [v for name, v in grid.items() if keyword.lower() in name.lower()]
    if not matched:
        # H unknown without an iter — caller should handle empty
        return np.zeros(len(next(iter(grid.values())))) if grid else np.zeros(0)
    return np.sum(matched, axis=0)
