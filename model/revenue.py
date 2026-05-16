"""Revenue computation: active clients with churn → tier split → revenue components.

Two pricing kinds per stream:
    - "monthly_subscription" (legacy):
          billing_active[m] = ROUNDDOWN(billing_active[m-1] * (1 - churn)) + new[m]
          revenue[m] = (Σ tiers) qty[tier][m] × (subscription + seats + credits price)
          platform_active[m] = billing_active[m]
    - "one_time_bundle" (NEW for prepaid bundles like Upwork $790/24mo):
          revenue[m] = new[m] × bundle_price        ← cash basis, single hit at sale
          platform_active[m] = sum over cohorts c in [m - bundle_term + 1, m] of
                                 floor(new[c] × (1-churn)^(m-c))
          billing_active[m] = new[m]                ← only the just-acquired customers
                              (only used by the legacy `tier_quantities` path,
                               which `revenue_components()` skips for bundles)

Add-ons (Platform Access, Token Usage) layer on top: their revenue =
platform_active × arpu_schedule.value_at(month). See `addon_revenue()`.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from model.config import Inputs, RevenueStream

TIERS = ("basic", "pro")
COMPONENTS = ("subscription", "seats", "credits")


def effective_new_clients(stream: RevenueStream, scenario: str, horizon: int,
                          sales_headcount: np.ndarray | None = None) -> np.ndarray:
    """Resolve the new_clients ramp for a stream, applying:
       - sales-driven override (new = sales_headcount × per_rep_rate) if configured,
       - the active_after_month / inactive_after_month windows.
    """
    rate = (stream.customers_per_salesperson_per_month or {}).get(scenario)
    if rate is not None and sales_headcount is not None:
        out = (sales_headcount[:horizon] * float(rate)).astype(float)
    else:
        out = np.array([float(x) for x in stream.new_clients_per_month[scenario][:horizon]], dtype=float)

    # Apply active / inactive windows
    for m_idx in range(horizon):
        month = m_idx + 1
        if stream.active_after_month is not None and month < stream.active_after_month:
            out[m_idx] = 0.0
        if stream.inactive_after_month is not None and month > stream.inactive_after_month:
            out[m_idx] = 0.0
    return out


# ── Active client counts ──────────────────────────────────────────────────────

def active_clients(stream: RevenueStream, scenario: str, horizon: int,
                   sales_headcount: np.ndarray | None = None) -> np.ndarray:
    """Billing-active client count per month.

    For monthly_subscription: cumulative with churn (legacy semantics, preserved).
    For one_time_bundle: == new clients acquired this month (revenue is recognized
    once at sale; no ongoing monthly subscription billing).
    """
    new_clients = effective_new_clients(stream, scenario, horizon, sales_headcount)
    if stream.pricing_kind == "one_time_bundle":
        return new_clients

    out = np.zeros(horizon, dtype=float)
    prev = 0.0
    for m in range(horizon):
        retained = math.floor(prev * (1.0 - stream.churn_monthly))
        out[m] = retained + float(new_clients[m])
        prev = out[m]
    return out


def platform_active_clients(stream: RevenueStream, scenario: str, horizon: int,
                            sales_headcount: np.ndarray | None = None) -> np.ndarray:
    """Platform-active client count per month — used for add-on quantity.

    For monthly_subscription: same as billing-active.
    For one_time_bundle: cohort-survival sum over a `bundle_term_months` rolling
    window; each cohort decays by churn until the term expires.
    """
    new_clients = effective_new_clients(stream, scenario, horizon, sales_headcount)
    if stream.pricing_kind == "one_time_bundle":
        term = stream.bundle_term_months or horizon
        churn = stream.churn_monthly
        out = np.zeros(horizon, dtype=float)
        for m in range(horizon):
            total = 0.0
            for c in range(max(0, m - term + 1), m + 1):
                tenure = m - c
                survivors = math.floor(float(new_clients[c]) * (1.0 - churn) ** tenure)
                total += survivors
            out[m] = total
        return out

    return active_clients(stream, scenario, horizon, sales_headcount)


# ── Revenue per stream ────────────────────────────────────────────────────────

def tier_quantities(stream: RevenueStream, scenario: str, horizon: int,
                    sales_headcount: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Active client count split by tier (basic/pro). Only meaningful for
    monthly_subscription kind."""
    active = active_clients(stream, scenario, horizon, sales_headcount)
    return {
        tier: active * float(stream.tier_split[scenario][tier])
        for tier in TIERS
    }


def revenue_components(stream: RevenueStream, scenario: str, horizon: int,
                       sales_headcount: np.ndarray | None = None) -> pd.DataFrame:
    """Per-month revenue components for a single stream.

    For monthly_subscription: 6 lines (subscription/seats/credits × basic/pro) + total.
    For one_time_bundle: a single 'bundle' column with new[m] × bundle_price + total.
    """
    months = pd.RangeIndex(1, horizon + 1, name="month")
    df = pd.DataFrame(index=months)

    if stream.pricing_kind == "one_time_bundle":
        new_clients = effective_new_clients(stream, scenario, horizon, sales_headcount)
        df["bundle"] = new_clients * stream.get_bundle_price(scenario)
        df["total"] = df["bundle"]
        return df

    qty = tier_quantities(stream, scenario, horizon, sales_headcount)
    for tier in TIERS:
        df[f"{tier}_subscription"] = qty[tier] * stream.subscription_price(tier, scenario)
        df[f"{tier}_seats"]        = qty[tier] * stream.seats_per_client(tier, scenario) * stream.seat_price(tier, scenario)
        df[f"{tier}_credits"]      = qty[tier] * stream.credits_per_client_monthly(tier, scenario) * stream.credit_price(tier, scenario)
    df["total"] = df.sum(axis=1)
    return df


# ── Add-on revenue (Platform Access, Token Usage, etc.) ───────────────────────

def addon_quantities(inputs: Inputs, scenario: str,
                     sales_headcount: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Platform-active count contributing to each add-on, indexed by add-on name."""
    H = inputs.horizon_months
    out: dict[str, np.ndarray] = {}
    for ao in inputs.add_ons:
        total = np.zeros(H)
        for rs in inputs.revenue_streams:
            if not ao.applies_to_stream(rs.name):
                continue
            total += platform_active_clients(rs, scenario, H, sales_headcount)
        out[ao.name] = total
    return out


def addon_revenue(inputs: Inputs, scenario: str,
                  sales_headcount: np.ndarray | None = None) -> pd.DataFrame:
    """Per-month revenue from each add-on. Columns: <add-on name> + 'total'."""
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    df = pd.DataFrame(index=months)
    qtys = addon_quantities(inputs, scenario, sales_headcount)
    for ao in inputs.add_ons:
        arpu = np.array([ao.arpu_schedule.value_at(m) for m in range(1, H + 1)])
        df[ao.name] = qtys[ao.name] * arpu
    df["total"] = df.sum(axis=1) if not df.empty else np.zeros(H)
    return df


# ── Aggregations ──────────────────────────────────────────────────────────────

def total_revenue(inputs: Inputs, scenario: str,
                  sales_headcount: np.ndarray | None = None) -> pd.DataFrame:
    """Sum of channel-stream revenue + add-on revenue.

    Columns: total, plus one column per stream and one per add-on.
    """
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    out = pd.DataFrame(index=months, data={"total": np.zeros(H)})

    for rs in inputs.revenue_streams:
        comps = revenue_components(rs, scenario, H, sales_headcount)
        col = f"stream:{rs.name}"
        out[col] = comps["total"].values
        out["total"] += comps["total"].values

    addons = addon_revenue(inputs, scenario, sales_headcount)
    for ao in inputs.add_ons:
        col = f"addon:{ao.name}"
        out[col] = addons[ao.name].values
        out["total"] += addons[ao.name].values

    return out


def revenue_breakdown(inputs: Inputs, scenario: str,
                      sales_headcount: np.ndarray | None = None) -> pd.DataFrame:
    """Fine-grained breakdown for the Projections tab — per-stream per-tier per-component
    plus per-addon."""
    H = inputs.horizon_months
    months = pd.RangeIndex(1, H + 1, name="month")
    df = pd.DataFrame(index=months)
    for i, rs in enumerate(inputs.revenue_streams):
        comps = revenue_components(rs, scenario, H, sales_headcount)
        for col in comps.columns:
            if col == "total":
                continue
            df[f"s{i}:{rs.name}:{col}"] = comps[col].values
    addons = addon_revenue(inputs, scenario, sales_headcount)
    for ao in inputs.add_ons:
        df[f"addon:{ao.name}"] = addons[ao.name].values
    return df


def active_clients_total(inputs: Inputs, scenario: str,
                         sales_headcount: np.ndarray | None = None) -> np.ndarray:
    """Total platform-active clients across all streams."""
    H = inputs.horizon_months
    out = np.zeros(H)
    for rs in inputs.revenue_streams:
        out += platform_active_clients(rs, scenario, H, sales_headcount)
    return out


def total_credits_used(inputs: Inputs, scenario: str,
                       sales_headcount: np.ndarray | None = None) -> np.ndarray:
    """Legacy — total monthly AI credits consumed (basic + pro tiers, monthly_subscription
    streams only). Kept for backward compatibility with the Excel-baseline cost streams.
    """
    H = inputs.horizon_months
    out = np.zeros(H)
    for rs in inputs.revenue_streams:
        if rs.pricing_kind != "monthly_subscription":
            continue
        qty = tier_quantities(rs, scenario, H, sales_headcount)
        for tier in TIERS:
            out += qty[tier] * rs.credits_per_client_monthly(tier, scenario)
    return out


def addon_revenue_for(inputs: Inputs, scenario: str, addon_name: str,
                      sales_headcount: np.ndarray | None = None) -> np.ndarray:
    """Convenience: return one add-on's monthly revenue as a numpy array."""
    df = addon_revenue(inputs, scenario, sales_headcount)
    if addon_name in df.columns:
        return df[addon_name].values
    return np.zeros(inputs.horizon_months)
