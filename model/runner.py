"""Orchestrator: load inputs → run each (scenario, mode) → return a results dict.

Output shape:
    {
        "conservative": {"with_investment": ScenarioResult, "no_investment": ScenarioResult},
        "standard":     {"with_investment": ScenarioResult, "no_investment": ScenarioResult},
        ...
    }

For modes with self-funding hires (any role with hire_trigger_by_mode[mode] or
pay_triggers_by_mode[mode]), `run_scenario` runs a small fixed-point iteration:

    Pass 0: revenue is computed with all trigger-driven roles at headcount=0.
    Pass i: walk forward through revenue trajectory; trigger each hire / pay
            step on the first month its conditions are met. Recompute revenue.
    Stop when consecutive trajectories are within 0.1%.

Hires are monotonic (once triggered, stay triggered), so the fixed point
converges in 1–3 iterations.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from model.config import Inputs
from model import revenue, costs, receivables, payables, salaries as sal_mod, opex as opex_mod
from model import capex as capex_mod, debt as debt_mod, equity as eq_mod
from model import statements as st_mod
from model import kpis as kpis_mod


@dataclass
class ScenarioResult:
    scenario: str
    mode: str
    horizon: int

    revenue: pd.DataFrame
    revenue_breakdown: pd.DataFrame
    active_clients: np.ndarray
    sales_headcount: np.ndarray         # NEW
    costs: pd.DataFrame
    receivables: pd.DataFrame
    payables: pd.DataFrame
    salaries: pd.DataFrame
    role_headcount: dict[str, np.ndarray]   # NEW: resolved per-role per-month headcount
    role_base: dict[str, np.ndarray]        # NEW: resolved per-role per-month base salary
    labor_obligations: pd.DataFrame
    opex: pd.DataFrame
    capex: pd.DataFrame
    tech: pd.DataFrame
    debt: pd.DataFrame
    equity: pd.DataFrame
    grants: pd.DataFrame
    other_income: np.ndarray

    pnl: pd.DataFrame
    balance_sheet: pd.DataFrame
    cash_flow: pd.DataFrame

    kpis: dict = field(default_factory=dict)


# ── Self-funding solver ──────────────────────────────────────────────────────

def _has_self_funding(inputs: Inputs, mode: str) -> bool:
    for r in inputs.salaries.roles:
        if r.hire_trigger_by_mode and mode in r.hire_trigger_by_mode:
            return True
        if r.pay_triggers_by_mode and mode in r.pay_triggers_by_mode:
            return True
    return False


def _resolve_static_headcount(inputs: Inputs, mode: str) -> dict[str, np.ndarray]:
    H = inputs.horizon_months
    out: dict[str, np.ndarray] = {}
    for r in inputs.salaries.roles:
        if r.headcount_by_mode and mode in r.headcount_by_mode:
            out[r.name] = np.array(r.headcount_by_mode[mode][:H], dtype=float)
        elif r.headcount_by_month is not None:
            out[r.name] = np.array(r.headcount_by_month[:H], dtype=float)
        else:
            out[r.name] = np.zeros(H, dtype=float)
    return out


def _aggregate(grid: dict[str, np.ndarray], keyword: str) -> np.ndarray:
    matches = [v for name, v in grid.items() if keyword.lower() in name.lower()]
    if not matches:
        first = next(iter(grid.values()), None)
        return np.zeros(len(first)) if first is not None else np.zeros(0)
    return np.sum(matches, axis=0)


def _evaluate_triggers(inputs: Inputs, mode: str, rev: np.ndarray,
                       prev_hc: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Given a revenue trajectory and previous-iteration headcount, walk forward
    and resolve headcount + pay for every role."""
    H = inputs.horizon_months
    role_hc: dict[str, np.ndarray] = {}
    role_pay: dict[str, np.ndarray] = {}

    running_sales = np.zeros(H)
    running_support = np.zeros(H)

    for r in inputs.salaries.roles:
        # Headcount resolution
        if r.hire_trigger_by_mode and mode in r.hire_trigger_by_mode:
            t = r.hire_trigger_by_mode[mode]
            hc = np.zeros(H)
            triggered = False
            for m in range(H):
                if not triggered:
                    ok_rev   = (t.monthly_revenue_at_least is None or rev[m] >= t.monthly_revenue_at_least)
                    ok_sales = (t.sales_headcount_at_least is None or running_sales[m] >= t.sales_headcount_at_least)
                    ok_supp  = (t.support_headcount_at_least is None or running_support[m] >= t.support_headcount_at_least)
                    ok_month = (m + 1 >= t.earliest_month)
                    if ok_rev and ok_sales and ok_supp and ok_month:
                        triggered = True
                if triggered:
                    hc[m] = float(t.headcount)
            role_hc[r.name] = hc
        elif r.headcount_by_mode and mode in r.headcount_by_mode:
            role_hc[r.name] = np.array(r.headcount_by_mode[mode][:H], dtype=float)
        elif r.headcount_by_month is not None:
            role_hc[r.name] = np.array(r.headcount_by_month[:H], dtype=float)
        else:
            role_hc[r.name] = np.zeros(H, dtype=float)

        # Update running aggregates so subsequent roles' triggers see this role's hires
        if "sales" in r.name.lower():
            running_sales = running_sales + role_hc[r.name]
        elif "support" in r.name.lower():
            running_support = running_support + role_hc[r.name]

        # Pay resolution
        if r.pay_triggers_by_mode and mode in r.pay_triggers_by_mode:
            triggers = r.pay_triggers_by_mode[mode]
            pay = np.zeros(H)
            for m in range(H):
                active = 0.0
                for t in triggers:
                    ok_rev   = (t.monthly_revenue_at_least is None or rev[m] >= t.monthly_revenue_at_least)
                    ok_month = (m + 1 >= t.earliest_month)
                    if ok_rev and ok_month:
                        active = max(active, float(t.monthly_amount))
                pay[m] = active
            role_pay[r.name] = pay
        # else: leave role_pay untouched; salaries.resolve_role_base will fall back

    return role_hc, role_pay


def _solve_self_funding(inputs: Inputs, scenario: str, mode: str,
                        max_iters: int = 6) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    H = inputs.horizon_months
    # Pass 0: zero out trigger-driven roles, static for others; compute revenue.
    role_hc = _resolve_static_headcount(inputs, mode)
    for r in inputs.salaries.roles:
        if r.hire_trigger_by_mode and mode in r.hire_trigger_by_mode:
            role_hc[r.name] = np.zeros(H)
    role_pay: dict[str, np.ndarray] = {}

    prev_rev: np.ndarray | None = None
    for _ in range(max_iters):
        sales_hc = _aggregate(role_hc, "sales")
        rev_df = revenue.total_revenue(inputs, scenario, sales_hc)
        rev = rev_df["total"].values

        new_hc, new_pay = _evaluate_triggers(inputs, mode, rev, role_hc)

        # Convergence check (compare revenue trajectories)
        if prev_rev is not None and np.allclose(rev, prev_rev, rtol=0.001, atol=1.0):
            return new_hc, new_pay
        prev_rev = rev
        role_hc = new_hc
        role_pay = new_pay

    return role_hc, role_pay


# ── Single-pass compute (with optional resolved overrides) ──────────────────

def _run_one_pass(inputs: Inputs, scenario: str, mode: str,
                  headcount_overrides: dict[str, np.ndarray] | None = None,
                  pay_overrides: dict[str, np.ndarray] | None = None) -> ScenarioResult:
    H = inputs.horizon_months

    # Resolve final headcount/pay grid
    role_hc = sal_mod.role_headcount_grid(inputs, mode, headcount_overrides)
    sales_hc = _aggregate(role_hc, "sales")

    # Revenue & costs (sales-driven where configured)
    rev_df = revenue.total_revenue(inputs, scenario, sales_hc)
    rev_break = revenue.revenue_breakdown(inputs, scenario, sales_hc)
    active = revenue.active_clients_total(inputs, scenario, sales_hc)
    cost_df = costs.total_costs(inputs, scenario, sales_hc)

    # Working capital
    ar = receivables.receivables(inputs, scenario, rev_df["total"].values)
    ap = payables.payables(inputs, cost_df["total"].values)

    # Salaries with overrides
    sal_df = sal_mod.salaries(inputs, mode, headcount_overrides=headcount_overrides,
                              pay_overrides=pay_overrides)
    lo_df = sal_mod.labor_obligations(inputs, sal_df["fringe"].values)

    # Build resolved role_base dict for reporting
    role_base: dict[str, np.ndarray] = {}
    for r in inputs.salaries.roles:
        po = (pay_overrides or {}).get(r.name)
        role_base[r.name] = sal_mod.resolve_role_base(r, mode, H, po)

    # OpEx (mode + scenario aware: per-scenario marketing schedules differ)
    opex_df = opex_mod.opex(inputs, mode, scenario)

    # Capital structure
    capex_df = capex_mod.capex(inputs)
    tech_df = capex_mod.tech(inputs)
    debt_df = debt_mod.debt(inputs)
    equity_df = eq_mod.equity(inputs, mode)
    grants_df = eq_mod.grants(inputs, mode)

    # Other income
    other_inc = np.array([
        next((float(e["monthly"]) for e in inputs.other_income.get("schedule", [])
              if e["months"][0] <= m <= e["months"][1]), 0.0)
        for m in range(1, H + 1)
    ])

    # Statements
    pnl = st_mod.build_pnl(inputs, rev_df, cost_df, sal_df, opex_df,
                           capex_df, tech_df, debt_df, other_inc)
    bs = st_mod.build_bs(inputs, pnl, ar, ap, lo_df, capex_df, tech_df,
                         debt_df, equity_df, grants_df)
    cf = st_mod.build_cf(inputs, pnl, bs, ar, ap, lo_df, capex_df, tech_df,
                         debt_df, equity_df, grants_df, other_inc)
    bs.loc[:, "cash"] = cf["final_cash"].values
    bs.loc[:, "total_assets"] = bs["cash"] + bs["accounts_receivable"] + bs["capex_net"] + bs["tech_net"]

    headline = kpis_mod.compute_kpis(pnl, cf)

    return ScenarioResult(
        scenario=scenario, mode=mode, horizon=H,
        revenue=rev_df, revenue_breakdown=rev_break,
        active_clients=active, sales_headcount=sales_hc,
        costs=cost_df, receivables=ar, payables=ap,
        salaries=sal_df, role_headcount=role_hc, role_base=role_base,
        labor_obligations=lo_df, opex=opex_df,
        capex=capex_df, tech=tech_df, debt=debt_df,
        equity=equity_df, grants=grants_df, other_income=other_inc,
        pnl=pnl, balance_sheet=bs, cash_flow=cf,
        kpis=headline,
    )


def run_scenario(inputs: Inputs, scenario: str, mode: str = "base") -> ScenarioResult:
    if _has_self_funding(inputs, mode):
        hc_over, pay_over = _solve_self_funding(inputs, scenario, mode)
    else:
        hc_over, pay_over = None, None
    return _run_one_pass(inputs, scenario, mode, hc_over, pay_over)


def run_model(inputs: Inputs) -> dict[str, dict[str, ScenarioResult]]:
    """Returns nested dict: results[scenario][mode]."""
    return {
        sc: {m: run_scenario(inputs, sc, m) for m in inputs.modes}
        for sc in inputs.scenarios
    }
