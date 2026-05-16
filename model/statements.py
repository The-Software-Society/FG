"""P&L, Balance Sheet, and Cash Flow rollup per scenario.

These mirror the Excel "Statements" sheet (rows 6–97) but keep monthly granularity
in DataFrames indexed by month. Year totals are computed in helpers below.

Sign conventions:
    P&L: revenue +, costs +, expenses +, net = revenue - costs - opex - tax
    CF:  inflows +, outflows -
    BS:  all balances are signed positively (assets+, liabilities+, equity+)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from model.config import Inputs


# ── P&L ───────────────────────────────────────────────────────────────────────

def build_pnl(inputs: Inputs,
              revenue_df: pd.DataFrame, cost_df: pd.DataFrame,
              salaries_df: pd.DataFrame, opex_df: pd.DataFrame,
              capex_df: pd.DataFrame, tech_df: pd.DataFrame, debt_df: pd.DataFrame,
              other_income: np.ndarray) -> pd.DataFrame:
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")

    revenue = revenue_df["total"].values
    cost_of_sales = cost_df["total"].values
    gross = revenue - cost_of_sales

    salaries_total = salaries_df["total_salaries"].values
    da = capex_df["period_dep"].values + tech_df["period_dep"].values
    other_opex = opex_df["total"].values
    op_expenditures = salaries_total + da + other_opex
    op_result = gross - op_expenditures

    interest_expense = debt_df["interest"].values
    pre_tax = op_result + other_income - interest_expense

    tax_rate = float(inputs.tax.get("effective_rate", 0.0))
    tax = np.where(pre_tax > 0, pre_tax * tax_rate, 0.0)
    net = pre_tax - tax

    df = pd.DataFrame({
        "revenue": revenue,
        "cost_of_sales": cost_of_sales,
        "gross_result": gross,
        "salaries": salaries_total,
        "depreciation_amortization": da,
        "other_opex": other_opex,
        "operational_expenditures": op_expenditures,
        "operational_result": op_result,
        "other_income": other_income,
        "interest_expense": interest_expense,
        "net_result_pretax": pre_tax,
        "tax": tax,
        "net_result": net,
        # margins
        "gross_margin":      np.where(revenue != 0, gross / revenue, 0.0),
        "operational_margin": np.where(revenue != 0, op_result / revenue, 0.0),
        "net_margin":        np.where(revenue != 0, net / revenue, 0.0),
        "ebitda":            op_result + da,
        "ebitda_margin":     np.where(revenue != 0, (op_result + da) / revenue, 0.0),
    }, index=months)
    return df


# ── Balance Sheet ─────────────────────────────────────────────────────────────

def build_bs(inputs: Inputs, pnl: pd.DataFrame,
             ar: pd.DataFrame, ap: pd.DataFrame, labor: pd.DataFrame,
             capex_df: pd.DataFrame, tech_df: pd.DataFrame, debt_df: pd.DataFrame,
             equity_df: pd.DataFrame, grants_df: pd.DataFrame) -> pd.DataFrame:
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")

    accumulated_result = pnl["net_result"].cumsum().shift(1).fillna(0.0).values

    # Cash will be filled in by runner after CF builds. Initialize to zeros.
    cash = np.zeros(H)

    df = pd.DataFrame({
        "cash": cash,
        "accounts_receivable": ar["final_ar"].values,
        "current_assets": cash + ar["final_ar"].values,
        "capex_net": capex_df["final_value"].values,
        "tech_net": tech_df["final_value"].values,
        "non_current_assets": capex_df["final_value"].values + tech_df["final_value"].values,
        "total_assets": cash + ar["final_ar"].values + capex_df["final_value"].values + tech_df["final_value"].values,
        "accounts_payable": ap["final_ap"].values,
        "labor_obligations": labor["final"].values,
        "debt": debt_df["final"].values,
        "total_liabilities": ap["final_ap"].values + labor["final"].values + debt_df["final"].values,
        "equity_investment": equity_df["final"].values,
        "grants": grants_df["final"].values,
        "net_result": pnl["net_result"].values,
        "accumulated_result": accumulated_result,
        "total_equity": equity_df["final"].values + grants_df["final"].values + pnl["net_result"].values + accumulated_result,
    }, index=months)
    return df


# ── Cash Flow ─────────────────────────────────────────────────────────────────

def build_cf(inputs: Inputs, pnl: pd.DataFrame, bs: pd.DataFrame,
             ar: pd.DataFrame, ap: pd.DataFrame, labor: pd.DataFrame,
             capex_df: pd.DataFrame, tech_df: pd.DataFrame, debt_df: pd.DataFrame,
             equity_df: pd.DataFrame, grants_df: pd.DataFrame,
             other_income: np.ndarray) -> pd.DataFrame:
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")

    op_result = pnl["operational_result"].values
    da = pnl["depreciation_amortization"].values
    ebitda = op_result + da

    # ΔAR (asset increase = cash use), ΔAP and ΔLabor (liability increase = cash source)
    final_ar = ar["final_ar"].values
    initial_ar = np.concatenate([[0.0], final_ar[:-1]])
    delta_ar = -(final_ar - initial_ar)   # increase in AR is a use of cash

    final_ap = ap["final_ap"].values
    initial_ap = np.concatenate([[0.0], final_ap[:-1]])
    delta_ap = (final_ap - initial_ap)    # increase in AP is a source of cash

    final_lo = labor["final"].values
    initial_lo = np.concatenate([[0.0], final_lo[:-1]])
    delta_lo = (final_lo - initial_lo)

    owk_movement = delta_ar + delta_ap + delta_lo

    capex_invest = -capex_df["investment"].values
    tech_invest = -tech_df["investment"].values
    taxes = -pnl["tax"].values

    free_op_cf = ebitda + owk_movement + taxes + capex_invest + tech_invest

    # Debt: initial disbursement (inflow), interest + principal (outflows)
    debt_disbursements = np.zeros(H)
    for d in inputs.debt:
        m = d.disbursement_month
        if 1 <= m <= H:
            debt_disbursements[m - 1] += d.principal
    debt_principal_paid = -debt_df["principal"].values
    debt_interest_paid = -debt_df["interest"].values
    debt_cf = debt_disbursements + debt_principal_paid + debt_interest_paid

    # Equity / grants
    equity_in = equity_df["investment"].values
    grants_in = grants_df["investment"].values
    eq_grants_cf = equity_in + grants_in

    other_income_cf = other_income

    non_op_cf = eq_grants_cf + debt_cf + other_income_cf
    cash_variation = free_op_cf + non_op_cf

    initial_cash = np.zeros(H)
    final_cash = np.zeros(H)
    for m in range(H):
        initial_cash[m] = final_cash[m - 1] if m > 0 else 0.0
        final_cash[m] = initial_cash[m] + cash_variation[m]

    df = pd.DataFrame({
        "ebitda": ebitda,
        "delta_ar": delta_ar,
        "delta_ap": delta_ap,
        "delta_labor": delta_lo,
        "owk_movement": owk_movement,
        "capex_investment": capex_invest,
        "tech_investment": tech_invest,
        "taxes": taxes,
        "free_op_cf": free_op_cf,
        "equity_in": equity_in,
        "grants_in": grants_in,
        "debt_disbursements": debt_disbursements,
        "debt_principal_paid": debt_principal_paid,
        "debt_interest_paid": debt_interest_paid,
        "other_income": other_income_cf,
        "non_op_cf": non_op_cf,
        "cash_variation": cash_variation,
        "initial_cash": initial_cash,
        "final_cash": final_cash,
    }, index=months)
    return df


# ── Year aggregates ───────────────────────────────────────────────────────────

def yearly_sums(df: pd.DataFrame) -> pd.DataFrame:
    """Roll a monthly DataFrame into yearly totals (Year 1, Year 2, …).

    Includes sums (for flow items) — for stocks, take the last-month value via
    `yearly_endings()` instead.
    """
    months = df.index
    n_years = (len(months) + 11) // 12
    out: dict[str, list[float]] = {col: [] for col in df.columns}
    for y in range(n_years):
        slice_ = df.iloc[y * 12:(y + 1) * 12]
        for col in df.columns:
            out[col].append(float(slice_[col].sum()))
    return pd.DataFrame(out, index=[f"Year {y+1}" for y in range(n_years)])


def yearly_endings(df: pd.DataFrame) -> pd.DataFrame:
    """For balance-sheet stocks: end-of-year values."""
    months = df.index
    n_years = (len(months) + 11) // 12
    out: dict[str, list[float]] = {col: [] for col in df.columns}
    for y in range(n_years):
        end_idx = min((y + 1) * 12 - 1, len(months) - 1)
        for col in df.columns:
            out[col].append(float(df.iloc[end_idx][col]))
    return pd.DataFrame(out, index=[f"Year {y+1}" for y in range(n_years)])
