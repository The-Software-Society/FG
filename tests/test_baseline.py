"""Regression: rebuilt model must reproduce Excel baseline totals.

The reference values come from the original spreadsheet's Landing & Statements
sheets (cached calculation values). Any drift here means the Python engine has
diverged from Excel — investigate before changing the test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from model import load_inputs, run_model

ROOT = Path(__file__).resolve().parent.parent
# Pinned to a fixture so the Excel-match guarantee can't be silently broken
# when the live inputs.yaml is edited. Re-extract via:
#   python3 scripts/extract_baseline_from_xlsx.py --out tests/fixtures/inputs.baseline.yaml
INPUTS_PATH = ROOT / "tests" / "fixtures" / "inputs.baseline.yaml"

# From Landing sheet C9:E11 (Total Revenue per scenario per year)
EXCEL_ANNUAL_REVENUE = {
    "conservative": [30412.80,  305791.20,  991029.60],
    "standard":     [64240.80,  689268.00,  1998280.80],
    "optimistic":   [90304.80,  815580.00,  2940343.20],
}

# From Statements P/AC/AP columns for the Standard scenario
EXCEL_STANDARD_PNL = {
    "cost_of_sales":      [10855.30,    62259.00,    155615.40],
    "gross_result":       [53385.50,   627009.00,   1842665.40],
    "operational_result": [-133151.77,  55676.68,    487891.96],
    "net_result":         [-120452.43,  34187.94,    384165.36],
    "ebitda":             [-133151.77,  80676.68,    762891.96],
}

# Final cash at end of each year (Statements!O97, AB97, AO97) for Standard
EXCEL_STANDARD_FINAL_CASH = [100587.09, 611656.15, 1273721.59]
EXCEL_STANDARD_FREE_OCF   = [-134412.91, -238930.94, 662065.44]


@pytest.fixture(scope="module")
def results():
    """Returns a flat dict[scenario, ScenarioResult] for the legacy baseline
    (single mode 'base'). Hides the new nested results[scenario][mode] shape so
    these tests remain readable."""
    inputs = load_inputs(INPUTS_PATH)
    warnings = inputs.validate()
    assert not warnings, f"inputs.yaml validation warnings: {warnings}"
    raw = run_model(inputs)
    mode = inputs.modes[0]
    return {sc: raw[sc][mode] for sc in inputs.scenarios}


def _annual(df, col):
    return [df[col].iloc[:12].sum(), df[col].iloc[12:24].sum(), df[col].iloc[24:36].sum()]


@pytest.mark.parametrize("scenario", ["conservative", "standard", "optimistic"])
def test_annual_total_revenue_matches_excel(results, scenario):
    expected = EXCEL_ANNUAL_REVENUE[scenario]
    actual = _annual(results[scenario].revenue, "total")
    for y, (e, a) in enumerate(zip(expected, actual), start=1):
        assert abs(e - a) < 0.01, f"{scenario} Year {y}: expected ${e:,.2f}, got ${a:,.2f}"


@pytest.mark.parametrize("metric,expected", list(EXCEL_STANDARD_PNL.items()))
def test_standard_pnl_annual(results, metric, expected):
    pnl = results["standard"].pnl
    actual = _annual(pnl, metric)
    for y, (e, a) in enumerate(zip(expected, actual), start=1):
        assert abs(e - a) < 0.01, f"Standard {metric} Y{y}: expected ${e:,.2f}, got ${a:,.2f}"


def test_standard_final_cash_by_year(results):
    cf = results["standard"].cash_flow
    actual = [cf["final_cash"].iloc[11], cf["final_cash"].iloc[23], cf["final_cash"].iloc[35]]
    for y, (e, a) in enumerate(zip(EXCEL_STANDARD_FINAL_CASH, actual), start=1):
        assert abs(e - a) < 0.01, f"Standard final cash Y{y}: expected ${e:,.2f}, got ${a:,.2f}"


def test_standard_free_op_cf_annual(results):
    cf = results["standard"].cash_flow
    actual = _annual(cf, "free_op_cf")
    for y, (e, a) in enumerate(zip(EXCEL_STANDARD_FREE_OCF, actual), start=1):
        assert abs(e - a) < 0.01, f"Standard free OCF Y{y}: expected ${e:,.2f}, got ${a:,.2f}"


def test_monthly_revenue_sums_to_annual(results):
    """Sum-of-monthly-revenue must equal the annual sum to floating-point tolerance."""
    for sc, r in results.items():
        rev = r.revenue["total"]
        for y in range(3):
            slice_sum = rev.iloc[y * 12:(y + 1) * 12].sum()
            expected = EXCEL_ANNUAL_REVENUE[sc][y]
            assert abs(slice_sum - expected) < 0.01
