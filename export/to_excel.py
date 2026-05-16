"""Write a computed model to a .xlsx workbook.

Layout (mirrors original Excel where reasonable):
    - Summary       — Y1/Y2/Y3 KPIs per scenario (replaces Landing).
    - Inputs        — flat dump of inputs.yaml (round-trip aid).
    - Projections   — every monthly intermediate vector, all scenarios.
    - Statements_<scenario> — P&L + BS + CF for each scenario, with
                               Y1/Y2/Y3 columns interleaved like the original.

Use:
    from model import load_inputs, run_model
    from export.to_excel import build_workbook
    inputs = load_inputs("inputs.yaml")
    results = run_model(inputs)
    build_workbook(inputs, results, "out.xlsx")
"""
from __future__ import annotations

from pathlib import Path
from typing import IO

import pandas as pd
import yaml

from model.config import Inputs


def _annual_sum(values, horizon: int) -> list[float]:
    n_years = (horizon + 11) // 12
    return [float(sum(values[y * 12:(y + 1) * 12])) for y in range(n_years)]


def _annual_eoy(values, horizon: int) -> list[float]:
    n_years = (horizon + 11) // 12
    out = []
    for y in range(n_years):
        idx = min((y + 1) * 12 - 1, horizon - 1)
        out.append(float(values[idx]))
    return out


def _interleave(df: pd.DataFrame, horizon: int, year_kind: str = "sum") -> pd.DataFrame:
    """Insert Y1/Y2/Y3 columns after each block of 12 months.

    year_kind="sum" → year col = sum of the 12 monthly cols (P&L, CF flows)
    year_kind="eoy" → year col = end-of-year value (BS stocks)
    """
    n_years = (horizon + 11) // 12
    blocks = []
    for y in range(n_years):
        m_lo = y * 12
        m_hi = min((y + 1) * 12, horizon)
        block = df.iloc[:, m_lo:m_hi].copy()
        if year_kind == "sum":
            block[f"Year {y+1}"] = block.sum(axis=1)
        else:
            block[f"Year {y+1}"] = block.iloc[:, -1]
        blocks.append(block)
    return pd.concat(blocks, axis=1)


def build_workbook(inputs: Inputs, results: dict, dest: str | Path | IO) -> None:
    H = inputs.horizon_months
    n_years = (H + 11) // 12
    months = [f"Month {i+1}" for i in range(H)]
    year_labels = [f"Year {y+1}" for y in range(n_years)]

    if hasattr(dest, "write"):
        engine_target = dest
    else:
        engine_target = str(dest)

    with pd.ExcelWriter(engine_target, engine="openpyxl") as xl:
        # ── Summary ────────────────────────────────────────────────────
        rows = []
        for sc in inputs.scenarios:
            rev_y = _annual_sum(results[sc].revenue["total"].values, H)
            net_y = _annual_sum(results[sc].pnl["net_result"].values, H)
            cash_eoy = _annual_eoy(results[sc].cash_flow["final_cash"].values, H)
            rows.append([sc.title(), "Total Revenue", *rev_y])
            rows.append([sc.title(), "Net Result",    *net_y])
            rows.append([sc.title(), "EOY Cash",      *cash_eoy])
        summary = pd.DataFrame(rows, columns=["Scenario", "Metric", *year_labels])
        summary.to_excel(xl, sheet_name="Summary", index=False)

        # ── Inputs (flat YAML dump) ────────────────────────────────────
        inputs_text = yaml.dump(inputs.to_dict(), sort_keys=False, allow_unicode=True,
                                 default_flow_style=False, width=120)
        pd.DataFrame({"inputs.yaml": inputs_text.split("\n")}).to_excel(
            xl, sheet_name="Inputs", index=False)

        # ── Statements per scenario ─────────────────────────────────────
        for sc in inputs.scenarios:
            r = results[sc]
            blocks: list[pd.DataFrame] = []

            # P&L
            pnl_rows = ["revenue", "cost_of_sales", "gross_result",
                        "salaries", "depreciation_amortization", "other_opex",
                        "operational_expenditures", "operational_result",
                        "other_income", "interest_expense", "net_result_pretax",
                        "tax", "net_result", "ebitda",
                        "gross_margin", "operational_margin", "net_margin", "ebitda_margin"]
            pnl = r.pnl[pnl_rows].T
            pnl.columns = months
            pnl_block = _interleave(pnl, H, "sum")
            pnl_block.insert(0, "Section", "P&L")
            blocks.append(pnl_block)

            # Spacer
            blocks.append(pd.DataFrame([[""] * (1 + len(pnl_block.columns) - 1)], columns=pnl_block.columns))

            # BS
            bs_rows = ["cash", "accounts_receivable", "current_assets",
                       "capex_net", "tech_net", "non_current_assets", "total_assets",
                       "accounts_payable", "labor_obligations", "debt", "total_liabilities",
                       "equity_investment", "grants", "net_result", "accumulated_result", "total_equity"]
            bs = r.balance_sheet[bs_rows].T
            bs.columns = months
            bs_block = _interleave(bs, H, "eoy")
            bs_block.insert(0, "Section", "Balance Sheet")
            blocks.append(bs_block)

            blocks.append(pd.DataFrame([[""] * (1 + len(bs_block.columns) - 1)], columns=bs_block.columns))

            # CF
            cf_rows = ["ebitda", "owk_movement", "capex_investment", "tech_investment",
                       "taxes", "free_op_cf", "equity_in", "grants_in",
                       "debt_disbursements", "debt_principal_paid", "debt_interest_paid",
                       "other_income", "non_op_cf", "cash_variation",
                       "initial_cash", "final_cash"]
            cf = r.cash_flow[cf_rows].T
            cf.columns = months
            cf_block = _interleave(cf, H, "sum")
            # final_cash and initial_cash should use EOY/beginning, not sum
            for y in range(n_years):
                end_idx = min((y + 1) * 12 - 1, H - 1)
                cf_block.loc["final_cash", f"Year {y+1}"] = float(r.cash_flow["final_cash"].iloc[end_idx])
                cf_block.loc["initial_cash", f"Year {y+1}"] = float(r.cash_flow["initial_cash"].iloc[y * 12])
            cf_block.insert(0, "Section", "Cash Flow")
            blocks.append(cf_block)

            sheet = pd.concat(blocks, axis=0)
            sheet.to_excel(xl, sheet_name=f"Statements_{sc[:8]}", index=True, index_label="Line")

        # ── Projections (one sheet per scenario) ──────────────────────
        for sc in inputs.scenarios:
            r = results[sc]
            proj_blocks = {
                "Revenue":    r.revenue,
                "Costs":      r.costs,
                "Salaries":   r.salaries,
                "OpEx":       r.opex,
                "CAPEX":      r.capex,
                "Tech":       r.tech,
                "Debt":       r.debt,
                "Equity":     r.equity,
                "Grants":     r.grants,
                "Receivables": r.receivables,
                "Payables":   r.payables,
            }
            tables = []
            for name, df in proj_blocks.items():
                t = df.T
                t.columns = months
                t.insert(0, "Block", name)
                tables.append(t)
                tables.append(pd.DataFrame([[""] * (1 + len(months))], columns=t.columns))
            full = pd.concat(tables, axis=0)
            full.to_excel(xl, sheet_name=f"Projections_{sc[:6]}", index=True, index_label="Line")
