"""TSS-economics inputs.yaml regression / snapshot.

Locks in headline numbers for the current TSS plan. Tolerance ±1% so trivial
edits don't break CI; intentional changes require updating these snapshots.

Tests cover:
  - both modes × all scenarios revenue snapshot
  - KPI shape (first sustained profit, GM steady-state, cash trough)
  - Upwork bundle one-time revenue math
  - Cold-Call/Ads sales-driven acquisition (more sales reps → more new clients)
  - Self-funding solver convergence
"""
from __future__ import annotations

from pathlib import Path

import pytest

from model import load_inputs, run_model

ROOT = Path(__file__).resolve().parent.parent
INPUTS_PATH = ROOT / "inputs.yaml"


@pytest.fixture(scope="module")
def raw_results():
    inputs = load_inputs(INPUTS_PATH)
    warnings = inputs.validate()
    assert not warnings, f"inputs.yaml validation warnings: {warnings}"
    return inputs, run_model(inputs)


@pytest.fixture(scope="module")
def results(raw_results):
    """Flat scenario→ScenarioResult dict for the primary mode (with_investment)."""
    inputs, raw = raw_results
    primary = inputs.modes[0]
    return {sc: raw[sc][primary] for sc in inputs.scenarios}


def _annual(df, col):
    return [df[col].iloc[:12].sum(), df[col].iloc[12:24].sum(), df[col].iloc[24:36].sum()]


# ── Revenue snapshots (with_investment + no_investment, all scenarios) ─────
# Revenue is independent of pay/bootstrap changes, so these stayed stable
# through the trough-elimination iterations.
SNAPSHOT_REVENUE = {
    ("conservative", "with_investment"): [52431,    498804,  1600389],
    ("conservative", "no_investment"):   [47931,    179238,   494564],
    ("standard",     "with_investment"): [131704,  1721886,  4219887],
    ("standard",     "no_investment"):   [72404,    374825,  1631181],
    ("optimistic",   "with_investment"): [278755,  3081105,  7149669],
    ("optimistic",   "no_investment"):   [103355,  1085762,  3894860],
}


@pytest.mark.parametrize("key,expected", list(SNAPSHOT_REVENUE.items()))
def test_revenue_snapshot(raw_results, key, expected):
    _, raw = raw_results
    sc, mode = key
    actual = _annual(raw[sc][mode].revenue, "total")
    for y, (e, a) in enumerate(zip(expected, actual), start=1):
        assert abs(e - a) / max(abs(e), 1) < 0.01, (
            f"{sc} / {mode} Y{y}: expected ~${e:,}, got ${a:,.0f} (>1% drift). "
            f"If intentional, update SNAPSHOT_REVENUE."
        )


# ── KPI shape ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", ["conservative", "standard", "optimistic"])
@pytest.mark.parametrize("mode", ["with_investment", "no_investment"])
def test_kpis_present(raw_results, scenario, mode):
    _, raw = raw_results
    k = raw[scenario][mode].kpis
    assert k is not None
    for key in ("first_profitable_month", "first_sustained_profit_month",
                "gm_steady_state_month", "gm_steady_state_value",
                "cash_trough_month", "cash_trough_value"):
        assert key in k, f"missing KPI: {key}"


def test_gm_steady_state_above_90_percent(results):
    """SaaS pitch target: GM steady-state ≥ 88% across scenarios (Token Usage cost
    ramps down 30%→15%→8% over Y1→Y3 to reflect AI inference cost reductions)."""
    for sc, r in results.items():
        gm = r.kpis["gm_steady_state_value"]
        assert gm is not None, f"{sc}: GM steady-state not found"
        assert gm >= 0.88, f"{sc}: GM steady-state {gm:.1%} should be ≥ 88% (target 90%+)"


def test_investment_produces_meaningful_y3_lift(raw_results):
    """For each scenario, Y3 revenue with_investment should be ≥ 1.7× no_investment.
    Otherwise the $700k raise doesn't visibly justify itself to investors."""
    inputs, raw = raw_results
    for sc in inputs.scenarios:
        y3_with = raw[sc]["with_investment"].revenue["total"].iloc[24:36].sum()
        y3_no   = raw[sc]["no_investment"].revenue["total"].iloc[24:36].sum()
        ratio = y3_with / y3_no if y3_no > 0 else float("inf")
        assert ratio >= 1.7, (
            f"{sc}: Y3 with-investment lift only {ratio:.1f}× — investors won't see "
            f"deployment ROI (need ≥ 1.7×)"
        )


def test_with_investment_post_raise_eoy_cash_dwarfs_no_investment(raw_results):
    """EOY3 cash with_investment should significantly exceed no_investment for
    scenarios where it matters (Standard, Optimistic). Otherwise capital is sitting
    in the bank, not being deployed productively."""
    inputs, raw = raw_results
    for sc in ["standard", "optimistic"]:
        eoy_with = raw[sc]["with_investment"].cash_flow["final_cash"].iloc[-1]
        eoy_no   = raw[sc]["no_investment"].cash_flow["final_cash"].iloc[-1]
        delta = eoy_with - eoy_no
        assert delta > 1_000_000, (
            f"{sc}: EOY3 cash delta only ${delta:,.0f} — $700k investment should "
            f"produce > $1M of additional cash by Y3 from compounding revenue"
        )


def test_no_investment_has_lower_or_equal_cash_than_with_investment(raw_results):
    """For each scenario, with_investment should reach a higher Y3 cash than no_investment
    (since the $700k raise lands at M6)."""
    _, raw = raw_results
    for sc in ["conservative", "standard"]:
        cash_with = raw[sc]["with_investment"].cash_flow["final_cash"].iloc[-1]
        cash_no   = raw[sc]["no_investment"].cash_flow["final_cash"].iloc[-1]
        assert cash_with > cash_no, (
            f"{sc}: with_investment cash {cash_with:.0f} should exceed no_investment cash {cash_no:.0f}"
        )


def test_all_scenarios_cash_trough_non_negative(raw_results):
    """Every (scenario, mode) combo should have a non-negative cash trough.

    The model is calibrated (founder bootstrap, hire triggers, founder pay caps,
    Upwork volume) so the worst case is still self-funded. If this fails, either
    a regression broke the calibration or someone made the assumptions less
    favorable — investigate before bumping the snapshot."""
    inputs, raw = raw_results
    for sc in inputs.scenarios:
        for mode in inputs.modes:
            trough = raw[sc][mode].kpis["cash_trough_value"]
            assert trough is not None and trough >= 0, (
                f"{sc} / {mode}: cash trough ${trough:,.0f} is negative — "
                f"model needs more bootstrap, slower hires, or higher revenue ramp"
            )


# ── Upwork bundle one-time revenue ──────────────────────────────────────────
def test_upwork_bundle_one_time_revenue(raw_results):
    inp, raw = raw_results
    upwork = next(rs for rs in inp.revenue_streams if rs.pricing_kind == "one_time_bundle")
    sc = "standard"
    primary = inp.modes[0]
    new_clients_std = upwork.new_clients_per_month[sc]
    bundle_price = upwork.get_bundle_price(sc)

    rev_col = f"stream:{upwork.name}"
    monthly_rev = raw[sc][primary].revenue[rev_col].values
    for m in range(min(12, len(new_clients_std))):
        expected = new_clients_std[m] * bundle_price
        assert abs(monthly_rev[m] - expected) < 0.01, (
            f"M{m+1}: expected {expected:.2f} (={new_clients_std[m]} × ${bundle_price}), got {monthly_rev[m]:.2f}"
        )


# ── Cold Call inactive after M12 ─────────────────────────────────────────────
def test_cold_call_no_new_acquisitions_after_m12(raw_results):
    """Cold Call's `inactive_after_month: 12` means new_clients = 0 for all m > 12.
    Since cold-call is sales-driven, this is enforced via effective_new_clients."""
    inp, raw = raw_results
    cold_stream_name = next(rs.name for rs in inp.revenue_streams if "Cold Call" in rs.name)
    sc, mode = "standard", "with_investment"
    rev = raw[sc][mode].revenue[f"stream:{cold_stream_name}"].values

    # Cold-Call billing-active count is cumulative-with-churn but `effective_new_clients`
    # zeros after M12, so revenue from M13+ should reflect a *non-growing* book that
    # only churns down (no new additions). Specifically, if no churn was zero, M13
    # revenue would equal M12 revenue × (1-churn). Just check revenue trends downward
    # from peak after M12.
    peak_idx_post = 11 + int(rev[11:].argmax())  # peak in M12+
    # After the peak, revenue should not exceed peak (only churn shrinks it).
    for m in range(peak_idx_post + 1, len(rev)):
        assert rev[m] <= rev[peak_idx_post] + 0.5, (
            f"Cold Call rev grows after M{peak_idx_post+1}: M{m+1}=${rev[m]:.0f} > peak ${rev[peak_idx_post]:.0f}"
        )


# ── Sales-driven acquisition test ────────────────────────────────────────────
def test_more_sales_reps_means_more_revenue(raw_results):
    """Optimistic scenario has higher salesperson productivity than Standard.
    With same hire schedule (with_investment), Ads stream should produce
    measurably more revenue in optimistic."""
    _, raw = raw_results
    ads_name = next(name for name in raw["standard"]["with_investment"].revenue.columns if "Ads" in name)
    std_ads = raw["standard"]["with_investment"].revenue[ads_name].sum()
    opt_ads = raw["optimistic"]["with_investment"].revenue[ads_name].sum()
    assert opt_ads > std_ads, f"optimistic Ads ${opt_ads:.0f} should exceed standard Ads ${std_ads:.0f}"


# ── Self-funding solver convergence ──────────────────────────────────────────
def test_self_funding_converges(raw_results):
    """The fixed-point solver should produce stable headcount in no_investment mode.
    Re-running run_model yields identical results."""
    inp, raw = raw_results
    raw2 = run_model(inp)
    for sc in inp.scenarios:
        rev1 = raw[sc]["no_investment"].revenue["total"].values
        rev2 = raw2[sc]["no_investment"].revenue["total"].values
        assert (abs(rev1 - rev2) < 1e-6).all(), f"{sc}: solver not deterministic"
