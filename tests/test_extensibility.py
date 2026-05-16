"""Extensibility: confirm the engine handles non-baseline shapes.

These exercise things that would require structural changes in the Excel file:
    - 48-month horizon (instead of 36)
    - 2 revenue streams (instead of 1)
    - A populated debt instrument (Excel slots are empty in baseline)
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from model import load_inputs, run_model
from model.config import Inputs

ROOT = Path(__file__).resolve().parent.parent
# Use the legacy Excel-baseline fixture for extensibility tests because they
# manipulate the simpler, single-mode schema (headcount_by_month, no triggers).
INPUTS_PATH = ROOT / "tests" / "fixtures" / "inputs.baseline.yaml"


@pytest.fixture
def baseline_dict():
    return load_inputs(INPUTS_PATH).to_dict()


def test_48_month_horizon(baseline_dict):
    """Extend horizon to 48 months — final 12 should reflect last-known schedule values."""
    d = copy.deepcopy(baseline_dict)
    d["horizon_months"] = 48
    # extend monthly arrays by repeating last value
    for rs in d["revenue_streams"]:
        for sc in d["scenarios"]:
            arr = rs["new_clients_per_month"][sc]
            arr.extend([arr[-1]] * 12)
    for r in d["salaries"]["roles"]:
        r["headcount_by_month"].extend([r["headcount_by_month"][-1]] * 12)

    inputs = Inputs.from_dict(d)
    raw = run_model(inputs)
    results = {sc: raw[sc][inputs.modes[0]] for sc in inputs.scenarios}

    # Each scenario should now have 48 months of data
    for sc, r in results.items():
        assert len(r.revenue) == 48
        assert len(r.pnl) == 48
        assert len(r.balance_sheet) == 48
        assert len(r.cash_flow) == 48


def test_second_revenue_stream(baseline_dict):
    """Adding a second revenue stream should add to total revenue without breaking anything."""
    d = copy.deepcopy(baseline_dict)
    base = d["revenue_streams"][0]
    extra = copy.deepcopy(base)
    extra["name"] = "Test Stream 2"
    # halve the new-client ramp so the stream contributes incrementally
    for sc in d["scenarios"]:
        extra["new_clients_per_month"][sc] = [x * 0.5 for x in extra["new_clients_per_month"][sc]]
    d["revenue_streams"].append(extra)

    inputs = Inputs.from_dict(d)
    raw = run_model(inputs)
    results = {sc: raw[sc][inputs.modes[0]] for sc in inputs.scenarios}

    base_inp = load_inputs(INPUTS_PATH)
    base_raw = run_model(base_inp)
    base_results = {sc: base_raw[sc][base_inp.modes[0]] for sc in base_inp.scenarios}
    for sc in d["scenarios"]:
        added = results[sc].revenue["total"].sum()
        baseline = base_results[sc].revenue["total"].sum()
        assert added > baseline, f"{sc}: adding a stream should increase total revenue"


def test_debt_instrument(baseline_dict):
    """Populating Debt #1 should add interest expense and a non-zero debt balance."""
    d = copy.deepcopy(baseline_dict)
    d["debt"] = [{
        "name": "Test Debt",
        "principal": 100000.0,
        "term_months": 24,
        "monthly_rate": 0.01,
        "disbursement_month": 6,
    }]

    inputs = Inputs.from_dict(d)
    raw = run_model(inputs)
    standard = raw["standard"][inputs.modes[0]]
    # Interest should be positive in the months following disbursement
    assert standard.pnl["interest_expense"].sum() > 0
    # Final debt should be 0 by month disbursement+term
    assert abs(standard.debt["final"].iloc[5 + 24 - 1]) < 1.0  # within ~$1 due to PMT rounding
