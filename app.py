"""TSS Financial Model — Streamlit UI.

Run:
    streamlit run app.py

Editing flow:
    1. Tweak inputs in the sidebar (Revenue, Costs, OpEx, Salaries, etc.).
    2. The main panel recomputes automatically.
    3. Click "Save inputs.yaml" to persist edits to disk.
    4. Click "Export to Excel" to write a .xlsx for sharing.

Adding new revenue/cost streams or capital instruments:
    Use the "+ Add" buttons inside each section. They append a new entry to the
    in-memory model; click Save to persist.

Resetting:
    "Reset to baseline" re-extracts from the original .xlsx (overwrites edits).
"""
from __future__ import annotations

import copy
import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from model import load_inputs, save_inputs, run_model
from model.config import Inputs

INPUTS_PATH = ROOT / "inputs.yaml"
SCENARIOS = ("conservative", "standard", "optimistic")

st.set_page_config(page_title="TSS Financial Model", layout="wide", initial_sidebar_state="expanded")


# ── Password gate ─────────────────────────────────────────────────────────────
# Password is read from one of (priority order):
#   1. st.secrets["password"]            ← Streamlit Cloud / Community Cloud secret
#   2. os.environ["TSS_MODEL_PASSWORD"]  ← Render / Railway / Fly.io / Docker env var
#   3. "tss26"                           ← dev fallback (5 chars; change before deploy)
#
# Edit `.streamlit/secrets.toml` (local) or set in your host's secret manager.

def _expected_password() -> str:
    import os
    try:
        pw = st.secrets.get("password")
        if pw:
            return str(pw)
    except (FileNotFoundError, AttributeError, KeyError):
        pass
    return os.environ.get("TSS_MODEL_PASSWORD", "tss26")


def _password_gate() -> bool:
    """Return True when the user is authenticated; render gate + halt otherwise."""
    if st.session_state.get("authed"):
        return True

    st.markdown("# 🔒 The Software Society — Financial Model")
    st.caption("Restricted access. Enter password to continue.")
    with st.form("login", clear_on_submit=False):
        pw = st.text_input("Password", type="password", max_chars=20)
        ok = st.form_submit_button("Unlock", type="primary")
    if ok:
        if pw == _expected_password():
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()


_password_gate()


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_into_state():
    raw = load_inputs(INPUTS_PATH).to_dict()
    st.session_state["inputs_dict"] = raw
    st.session_state["dirty"] = False


def _current_inputs() -> Inputs:
    return Inputs.from_dict(st.session_state["inputs_dict"])


def _mark_dirty():
    st.session_state["dirty"] = True


if "inputs_dict" not in st.session_state:
    _load_into_state()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_currency(x):
    try:
        return f"${x:,.0f}"
    except Exception:
        return str(x)


def _annual_sums(df: pd.DataFrame, col: str, horizon: int) -> list[float]:
    n_years = (horizon + 11) // 12
    return [float(df[col].iloc[y * 12:(y + 1) * 12].sum()) for y in range(n_years)]


def _annual_endings(df: pd.DataFrame, col: str, horizon: int) -> list[float]:
    n_years = (horizon + 11) // 12
    return [float(df[col].iloc[min((y + 1) * 12 - 1, len(df) - 1)]) for y in range(n_years)]


def _show_table(df: pd.DataFrame, money_cols=None, money_fmt="${:,.0f}"):
    df = df.copy()
    if money_cols:
        for c in money_cols:
            if c in df.columns:
                df[c] = df[c].map(lambda v: money_fmt.format(v) if isinstance(v, (int, float)) else v)
    st.dataframe(df, use_container_width=True)


# ── Sidebar — Inputs ──────────────────────────────────────────────────────────

with st.sidebar:
    st.title("TSS Model")
    st.caption("Edit any field — outputs recompute automatically.")

    col_save, col_reset = st.columns(2)
    with col_save:
        if st.button("💾 Save inputs.yaml", use_container_width=True, type="primary",
                     disabled=not st.session_state.get("dirty", False)):
            save_inputs(_current_inputs(), INPUTS_PATH)
            st.session_state["dirty"] = False
            st.success("Saved.")
    with col_reset:
        if st.button("↺ Reload from disk", use_container_width=True):
            _load_into_state()
            st.rerun()

    if st.session_state.get("dirty", False):
        st.warning("Unsaved changes")

    d = st.session_state["inputs_dict"]

    # — Horizon —
    with st.expander("⏱ Horizon", expanded=False):
        new_h = st.number_input("Horizon (months)", min_value=12, max_value=120,
                                value=int(d["horizon_months"]), step=12)
        if new_h != d["horizon_months"]:
            # extend or truncate per-month arrays
            old_h = d["horizon_months"]
            for rs in d["revenue_streams"]:
                for sc in d["scenarios"]:
                    arr = rs["new_clients_per_month"][sc]
                    if new_h > old_h:
                        arr.extend([arr[-1]] * (new_h - old_h))
                    else:
                        del arr[new_h:]
            for r in d["salaries"]["roles"]:
                if new_h > old_h:
                    r["headcount_by_month"].extend([r["headcount_by_month"][-1]] * (new_h - old_h))
                else:
                    del r["headcount_by_month"][new_h:]
            d["horizon_months"] = int(new_h)
            _mark_dirty()
            st.rerun()

    # — Revenue —
    for i, rs in enumerate(d["revenue_streams"]):
        with st.expander(f"💰 {rs['name']}", expanded=(i == 0)):
            rs["name"] = st.text_input("Name", rs["name"], key=f"rs_name_{i}")
            rs["business_model"] = st.text_input("Business model", rs["business_model"], key=f"rs_bm_{i}")

            # Pricing kind selector (NEW)
            current_kind = rs.get("pricing_kind", "monthly_subscription")
            kind = st.selectbox(
                "Pricing kind",
                ["monthly_subscription", "one_time_bundle"],
                index=["monthly_subscription", "one_time_bundle"].index(current_kind),
                key=f"rs_kind_{i}",
                help=("monthly_subscription: revenue = active customers × monthly price. "
                      "one_time_bundle: revenue = new customers × bundle price (cash basis); customer "
                      "stays platform-active for `bundle_term_months` so they keep contributing to add-ons."),
            )
            rs["pricing_kind"] = kind

            if kind == "one_time_bundle":
                rs["bundle_term_months"] = int(st.number_input(
                    "Bundle term (months)", min_value=1, max_value=120,
                    value=int(rs.get("bundle_term_months") or 24),
                    key=f"rs_bterm_{i}",
                ))
                bp = rs.get("bundle_price") or {sc: 0 for sc in SCENARIOS}
                bp_df = pd.DataFrame({"bundle_price": [float(bp.get(sc, 0)) for sc in SCENARIOS]},
                                     index=list(SCENARIOS))
                bp_ed = st.data_editor(bp_df, key=f"rs_bp_{i}",
                                       column_config={"bundle_price": st.column_config.NumberColumn(format="%.2f")})
                rs["bundle_price"] = {sc: float(bp_ed.loc[sc, "bundle_price"]) for sc in SCENARIOS}

            rs["churn_monthly"] = st.slider("Monthly churn", 0.0, 0.5,
                                            float(rs["churn_monthly"]), step=0.005,
                                            format="%.3f", key=f"rs_churn_{i}")
            rs["invoice_fulfillment_pct"] = st.slider("Invoice fulfillment %", 0.0, 1.0,
                                                       float(rs["invoice_fulfillment_pct"]),
                                                       step=0.01, key=f"rs_fulf_{i}")
            rs["collection_lag_months"] = st.number_input("Collection lag (months)", min_value=0,
                                                          max_value=12,
                                                          value=int(rs["collection_lag_months"]),
                                                          key=f"rs_lag_{i}")

            st.markdown("**New clients per month** (one row per scenario)")
            new_clients_df = pd.DataFrame(rs["new_clients_per_month"])
            new_clients_df.index = [f"M{m+1}" for m in range(len(new_clients_df))]
            edited = st.data_editor(new_clients_df, key=f"rs_nc_{i}", num_rows="fixed")
            for sc in SCENARIOS:
                rs["new_clients_per_month"][sc] = [float(x) for x in edited[sc].tolist()]

            if kind == "monthly_subscription":
                # Detect single-tier streams (basic=1.0, pro=0.0 across all scenarios) — they
                # don't need the full tier × component editor matrix.
                ts = rs.get("tier_split", {})
                is_single_tier = all(
                    abs(float(ts.get(sc, {}).get("basic", 0)) - 1.0) < 1e-6
                    and abs(float(ts.get(sc, {}).get("pro", 0))) < 1e-6
                    for sc in SCENARIOS
                )

                if is_single_tier:
                    # Simple editor: just one subscription price per scenario.
                    sub_basic = rs["pricing"]["subscription"]["basic"]
                    df_p = pd.DataFrame({"$ / month": [float(sub_basic.get(sc, 0)) for sc in SCENARIOS]},
                                        index=list(SCENARIOS))
                    ed_p = st.data_editor(df_p, key=f"rs_price_simple_{i}",
                                           column_config={"$ / month": st.column_config.NumberColumn(format="%.2f")})
                    for sc in SCENARIOS:
                        rs["pricing"]["subscription"]["basic"][sc] = float(ed_p.loc[sc, "$ / month"])
                    st.caption("Subscription price per scenario (single tier)")
                else:
                    # Full tier editor for multi-tier streams (Excel-baseline compatible).
                    st.markdown("**Tier split** (must sum to 1)")
                    ts_df = pd.DataFrame(rs["tier_split"]).T
                    ts_edited = st.data_editor(ts_df, key=f"rs_ts_{i}")
                    for sc in SCENARIOS:
                        rs["tier_split"][sc] = {"basic": float(ts_edited.loc[sc, "basic"]),
                                                 "pro": float(ts_edited.loc[sc, "pro"])}

                    st.markdown("**Pricing (per tier × scenario)**")
                    for cat_name, cat_path in [("Subscription $", ["subscription"]),
                                                ("Seats — count/client", ["seats", "per_client"]),
                                                ("Seats — $/seat", ["seats", "price"]),
                                                ("Credits — count/client/month", ["credits", "per_client_monthly"]),
                                                ("Credits — $/credit", ["credits", "price"])]:
                        node = rs["pricing"]
                        for k in cat_path:
                            node = node[k]
                        df_p = pd.DataFrame({"basic": node["basic"], "pro": node["pro"]})
                        df_p = df_p.reindex(SCENARIOS)
                        ed_p = st.data_editor(df_p, key=f"rs_price_{i}_{'_'.join(cat_path)}",
                                              column_config={"basic": st.column_config.NumberColumn(format="%.4f"),
                                                             "pro":   st.column_config.NumberColumn(format="%.4f")})
                        ed_p.columns.name = None
                        for sc in SCENARIOS:
                            node["basic"][sc] = float(ed_p.loc[sc, "basic"])
                            node["pro"][sc] = float(ed_p.loc[sc, "pro"])
                        st.caption(cat_name)

                # Sales productivity (if configured) — important for sales-driven streams
                csp = rs.get("customers_per_salesperson_per_month")
                if csp is not None:
                    st.markdown("**Sales productivity** (customers per sales rep per month — only fires when ≥1 sales rep is active)")
                    csp_df = pd.DataFrame(
                        {"customers/rep/mo": [float(csp.get(sc, 0)) for sc in SCENARIOS]},
                        index=list(SCENARIOS),
                    )
                    csp_ed = st.data_editor(csp_df, key=f"rs_csp_{i}")
                    rs["customers_per_salesperson_per_month"] = {
                        sc: float(csp_ed.loc[sc, "customers/rep/mo"]) for sc in SCENARIOS
                    }

                # Active / inactive month windows (e.g., Cold Call M4-M12 only)
                w_cols = st.columns(2)
                _h_local = d["horizon_months"]
                cur_after = rs.get("active_after_month")
                cur_inactive = rs.get("inactive_after_month")
                new_after = w_cols[0].number_input(
                    "Active after month (0 = always)", min_value=0, max_value=_h_local,
                    value=int(cur_after or 0), key=f"rs_after_{i}",
                )
                new_inactive = w_cols[1].number_input(
                    "Inactive after month (0 = never)", min_value=0, max_value=_h_local,
                    value=int(cur_inactive or 0), key=f"rs_inactive_{i}",
                )
                rs["active_after_month"] = int(new_after) if new_after > 0 else None
                rs["inactive_after_month"] = int(new_inactive) if new_inactive > 0 else None

    rev_btn_cols = st.columns([2, 1])
    if rev_btn_cols[0].button("➕ Add revenue stream"):
        d["revenue_streams"].append(copy.deepcopy(d["revenue_streams"][0]))
        d["revenue_streams"][-1]["name"] = f"Revenue Stream {len(d['revenue_streams'])}"
        _mark_dirty()
        st.rerun()

    # — Add-ons (Platform Access, Token Usage, etc.) —
    with st.expander("✨ Add-ons (Platform / Tokens)"):
        st.caption("Layered on top of every active customer regardless of channel.")
        add_ons = d.setdefault("add_ons", [])
        for i, ao in enumerate(add_ons):
            cols_h = st.columns([4, 1])
            ao["name"] = cols_h[0].text_input("Name", ao["name"], key=f"ao_name_{i}")
            if cols_h[1].button("✕", key=f"ao_del_{i}"):
                add_ons.pop(i); _mark_dirty(); st.rerun()
            ao["applies_to"] = st.text_input(
                "Applies to (comma-separated stream names, or 'all')",
                "all" if ao.get("applies_to", "all") == "all"
                else ", ".join(ao["applies_to"]),
                key=f"ao_at_{i}",
                help="Use 'all' to apply to every revenue stream, or list stream names like 'Cold Call, Ads' to exclude others.",
            )
            # parse back
            if ao["applies_to"].strip().lower() == "all":
                ao["applies_to"] = "all"
            else:
                ao["applies_to"] = [s.strip() for s in ao["applies_to"].split(",") if s.strip()]

            sched_df = pd.DataFrame([{"start": e["months"][0], "end": e["months"][1], "monthly": e["monthly"]}
                                     for e in ao.get("arpu_schedule", [])])
            ed_sched = st.data_editor(sched_df, key=f"ao_sched_{i}", num_rows="dynamic")
            ao["arpu_schedule"] = [
                {"months": [int(r["start"]), int(r["end"])], "monthly": float(r["monthly"])}
                for _, r in ed_sched.iterrows() if pd.notna(r["start"])
            ]
        if st.button("➕ Add add-on"):
            d["add_ons"].append({
                "name": f"Add-on {len(d['add_ons'])+1}",
                "arpu_schedule": [{"months": [1, d["horizon_months"]], "monthly": 0}],
                "applies_to": "all",
            })
            _mark_dirty(); st.rerun()

    # — Costs —
    with st.expander("💸 Cost streams"):
        for i, cs in enumerate(d["cost_streams"]):
            st.markdown(f"**{cs['name']}** — `{cs['kind']}`")
            cs["name"] = st.text_input("Name", cs["name"], key=f"cs_name_{i}")
            if cs["kind"] == "step":
                sched_df = pd.DataFrame([{"start": e["months"][0], "end": e["months"][1], "monthly": e["monthly"]}
                                         for e in cs["schedule"]])
                ed_sched = st.data_editor(sched_df, key=f"cs_sched_{i}", num_rows="dynamic")
                cs["schedule"] = [{"months": [int(r["start"]), int(r["end"])], "monthly": float(r["monthly"])}
                                  for _, r in ed_sched.iterrows() if pd.notna(r["start"])]
            elif cs["kind"] == "per_active_client":
                vals = cs["per_active_client_by_year"]
                cols = st.columns(len(vals))
                for y, c in enumerate(cols):
                    vals[y] = float(c.number_input(f"Y{y+1} $/client", value=float(vals[y]), key=f"cs_pacy_{i}_{y}"))
            elif cs["kind"] == "per_credit":
                vals = cs["per_credit_by_year"]
                cols = st.columns(len(vals))
                for y, c in enumerate(cols):
                    vals[y] = float(c.number_input(f"Y{y+1} $/credit", value=float(vals[y]),
                                                    step=0.01, format="%.4f", key=f"cs_pcy_{i}_{y}"))
            elif cs["kind"] == "fraction_of_addon":
                addon_names = [a["name"] for a in d.get("add_ons", [])] or [""]
                cs["addon_name"] = st.selectbox(
                    "Add-on (cost = fraction × this add-on's revenue)",
                    addon_names,
                    index=addon_names.index(cs.get("addon_name") or addon_names[0]) if (cs.get("addon_name") or addon_names[0]) in addon_names else 0,
                    key=f"cs_aon_{i}",
                )
                cs["fraction"] = float(st.slider(
                    "Fraction (e.g. 0.30 = 30%)", 0.0, 1.0,
                    float(cs.get("fraction") or 0.0),
                    step=0.01, format="%.2f", key=f"cs_frac_{i}",
                ))

        d["payment_lag_months"] = int(st.number_input("Payment lag (months)", min_value=0, max_value=12,
                                                       value=int(d["payment_lag_months"])))

    # — Salaries —
    with st.expander("👥 Salaries"):
        sal = d["salaries"]
        sal["fringe_benefits_pct"] = float(st.slider("Fringe benefits %", 0.0, 0.5,
                                                      float(sal["fringe_benefits_pct"]),
                                                      step=0.005, format="%.3f"))
        sal["payment_periodicity_months"] = int(st.number_input("Payment periodicity (months)",
                                                                  min_value=1, max_value=12,
                                                                  value=int(sal["payment_periodicity_months"])))

        st.markdown("**Roles**")
        st.caption("Each role has either a static `headcount_by_month` (legacy) "
                   "or `headcount_by_mode[mode]` (per-mode arrays) or `hire_trigger_by_mode[mode]` "
                   "(self-funding: hires when MRR crosses threshold). The active mode picks one.")
        H_local = d["horizon_months"]
        for i, role in enumerate(sal["roles"]):
            with st.container(border=True):
                cols_n = st.columns([3, 2])
                role["name"] = cols_n[0].text_input("Name", role["name"], key=f"role_name_{i}")
                role["base_monthly"] = float(cols_n[1].number_input(
                    "Base $/mo (fallback)", value=float(role.get("base_monthly", 0)), key=f"role_base_{i}"
                ))

                # Static legacy headcount
                if role.get("headcount_by_month") is not None:
                    hc_legacy = pd.DataFrame([role["headcount_by_month"]],
                                              index=["headcount"],
                                              columns=[f"M{m+1}" for m in range(H_local)])
                    ed_legacy = st.data_editor(hc_legacy, key=f"role_hc_legacy_{i}")
                    role["headcount_by_month"] = [int(x) for x in ed_legacy.iloc[0].tolist()]

                # Per-mode static headcount
                hbm = role.get("headcount_by_mode") or {}
                if hbm:
                    st.caption("Static headcount per mode (used unless overridden by hire trigger)")
                    hbm_df = pd.DataFrame({m: hbm.get(m, [0]*H_local) for m in d.get("modes", [])},
                                           index=[f"M{m+1}" for m in range(H_local)]).T
                    ed_hbm = st.data_editor(hbm_df, key=f"role_hbm_{i}")
                    role["headcount_by_mode"] = {
                        m: [int(x) for x in ed_hbm.loc[m].tolist()]
                        for m in d.get("modes", []) if m in ed_hbm.index
                    }

                # Hire trigger per mode
                htr = role.get("hire_trigger_by_mode") or {}
                if htr:
                    st.caption("Hire trigger (self-funding modes)")
                    for mode_name, trig in htr.items():
                        c = st.columns(4)
                        trig["monthly_revenue_at_least"] = float(c[0].number_input(
                            f"[{mode_name}] MRR ≥ $",
                            value=float(trig.get("monthly_revenue_at_least") or 0),
                            step=1000.0, key=f"role_trig_rev_{i}_{mode_name}",
                        ))
                        trig["sales_headcount_at_least"] = int(c[1].number_input(
                            "Sales ≥",
                            value=int(trig.get("sales_headcount_at_least") or 0),
                            min_value=0, step=1, key=f"role_trig_sales_{i}_{mode_name}",
                        ))
                        trig["support_headcount_at_least"] = int(c[2].number_input(
                            "Support ≥",
                            value=int(trig.get("support_headcount_at_least") or 0),
                            min_value=0, step=1, key=f"role_trig_supp_{i}_{mode_name}",
                        ))
                        trig["earliest_month"] = int(c[3].number_input(
                            "Earliest M",
                            value=int(trig.get("earliest_month") or 1),
                            min_value=1, max_value=H_local, step=1,
                            key=f"role_trig_em_{i}_{mode_name}",
                        ))

                # ── Pay schedule per mode (founder calendar ramps) ──
                bms_by_mode = role.get("base_monthly_schedule_by_mode") or {}
                if bms_by_mode:
                    st.caption("Base pay schedule per mode (calendar-based step bands)")
                    for mode_name, steps in list(bms_by_mode.items()):
                        st.markdown(f"  *[{mode_name}]*")
                        sched_df = pd.DataFrame([
                            {"start": s["months"][0], "end": s["months"][1], "monthly": s["monthly"]}
                            for s in steps
                        ])
                        ed = st.data_editor(sched_df, key=f"role_pay_sched_{i}_{mode_name}",
                                            num_rows="dynamic")
                        bms_by_mode[mode_name] = [
                            {"months": [int(r["start"]), int(r["end"])], "monthly": float(r["monthly"])}
                            for _, r in ed.iterrows() if pd.notna(r["start"])
                        ]
                    role["base_monthly_schedule_by_mode"] = bms_by_mode

                # ── Pay triggers per mode (MRR-stacked: pay = max(monthly_amount where conditions met)) ──
                ptr_by_mode = role.get("pay_triggers_by_mode") or {}
                if ptr_by_mode:
                    st.caption("Pay triggers per mode (stacked: active pay = highest monthly_amount whose triggers are met)")
                    for mode_name, triggers in list(ptr_by_mode.items()):
                        st.markdown(f"  *[{mode_name}]*")
                        trig_df = pd.DataFrame([
                            {
                                "monthly_amount":           float(t.get("monthly_amount", 0)),
                                "monthly_revenue_at_least": (float(t["monthly_revenue_at_least"])
                                                             if t.get("monthly_revenue_at_least") is not None else None),
                                "earliest_month":           int(t.get("earliest_month", 1)),
                            }
                            for t in triggers
                        ])
                        ed = st.data_editor(trig_df, key=f"role_pay_trig_{i}_{mode_name}",
                                            num_rows="dynamic")
                        ptr_by_mode[mode_name] = [
                            {
                                "monthly_amount":           float(r["monthly_amount"]),
                                "earliest_month":           int(r["earliest_month"]) if pd.notna(r["earliest_month"]) else 1,
                                **({"monthly_revenue_at_least": float(r["monthly_revenue_at_least"])}
                                    if pd.notna(r["monthly_revenue_at_least"]) else {}),
                            }
                            for _, r in ed.iterrows() if pd.notna(r.get("monthly_amount"))
                        ]
                    role["pay_triggers_by_mode"] = ptr_by_mode


    # — OpEx —
    with st.expander("📋 OpEx"):
        modes_list = list(d.get("modes", []))
        scenarios_list = list(d.get("scenarios", []))
        for i, cat in enumerate(d["opex"]):
            with st.container(border=True):
                cat["name"] = st.text_input("Name", cat["name"], key=f"opex_name_{i}")

                # Mode gating
                current_modes = cat.get("enabled_in_modes")
                sel_modes = st.multiselect(
                    "Enabled in modes (empty = all)",
                    modes_list,
                    default=current_modes if current_modes is not None else modes_list,
                    key=f"opex_modes_{i}",
                )
                cat["enabled_in_modes"] = sel_modes if (sel_modes and set(sel_modes) != set(modes_list)) else None

                # Default schedule
                st.caption("Default schedule (used when no per-scenario override)")
                sched_df = pd.DataFrame([{"start": e["months"][0], "end": e["months"][1], "monthly": e["monthly"]}
                                         for e in cat["schedule"]])
                ed_sched = st.data_editor(sched_df, key=f"opex_sched_{i}", num_rows="dynamic")
                cat["schedule"] = [{"months": [int(r["start"]), int(r["end"])], "monthly": float(r["monthly"])}
                                   for _, r in ed_sched.iterrows() if pd.notna(r["start"])]

                # Per-scenario schedules
                use_per_sc = st.checkbox(
                    "Differ by scenario",
                    value=cat.get("schedule_by_scenario") is not None,
                    key=f"opex_per_sc_{i}",
                )
                if use_per_sc:
                    if cat.get("schedule_by_scenario") is None:
                        cat["schedule_by_scenario"] = {sc: list(cat["schedule"]) for sc in scenarios_list}
                    for sc in scenarios_list:
                        st.markdown(f"  *[{sc}]*")
                        sub = cat["schedule_by_scenario"].get(sc, list(cat["schedule"]))
                        sub_df = pd.DataFrame([{"start": e["months"][0], "end": e["months"][1], "monthly": e["monthly"]}
                                               for e in sub])
                        ed_sub = st.data_editor(sub_df, key=f"opex_sched_sc_{i}_{sc}", num_rows="dynamic")
                        cat["schedule_by_scenario"][sc] = [
                            {"months": [int(r["start"]), int(r["end"])], "monthly": float(r["monthly"])}
                            for _, r in ed_sub.iterrows() if pd.notna(r["start"])
                        ]
                else:
                    cat.pop("schedule_by_scenario", None)

    # — CAPEX / Tech —
    with st.expander("🏭 CAPEX & Tech investments"):
        for label, key, life_field in [("CAPEX", "capex", "depreciation_months"),
                                        ("Tech", "tech_investments", "amortization_months")]:
            st.markdown(f"**{label}**")
            items = d[key]
            for i, it in enumerate(items):
                cols = st.columns([3, 2, 2, 2, 1])
                it["name"] = cols[0].text_input("Name", it["name"], key=f"{key}_name_{i}")
                it["value"] = float(cols[1].number_input("Value $", value=float(it["value"]), key=f"{key}_val_{i}"))
                it[life_field] = int(cols[2].number_input("Life (mo)", value=int(it[life_field]), key=f"{key}_life_{i}"))
                it["acquisition_month"] = int(cols[3].number_input("Acq. mo", value=int(it["acquisition_month"]), key=f"{key}_acq_{i}"))
                if cols[4].button("✕", key=f"{key}_del_{i}"):
                    items.pop(i)
                    _mark_dirty()
                    st.rerun()
            if st.button(f"➕ Add {label}", key=f"{key}_add"):
                items.append({"name": f"{label} #{len(items)+1}", "value": 0.0,
                              life_field: 12, "acquisition_month": 1})
                _mark_dirty()
                st.rerun()

    # — Debt —
    with st.expander("🏦 Debt"):
        for i, dt in enumerate(d["debt"]):
            cols = st.columns([3, 2, 2, 2, 2, 1])
            dt["name"] = cols[0].text_input("Name", dt["name"], key=f"debt_name_{i}")
            dt["principal"] = float(cols[1].number_input("Principal", value=float(dt["principal"]), key=f"debt_p_{i}"))
            dt["term_months"] = int(cols[2].number_input("Term", value=int(dt["term_months"]), key=f"debt_t_{i}"))
            dt["monthly_rate"] = float(cols[3].number_input("Rate/mo", value=float(dt["monthly_rate"]),
                                                              step=0.001, format="%.4f", key=f"debt_r_{i}"))
            dt["disbursement_month"] = int(cols[4].number_input("Disb. mo",
                                                                  value=int(dt["disbursement_month"]),
                                                                  key=f"debt_d_{i}"))
            if cols[5].button("✕", key=f"debt_del_{i}"):
                d["debt"].pop(i); _mark_dirty(); st.rerun()
        if st.button("➕ Add debt"):
            d["debt"].append({"name": f"Debt #{len(d['debt'])+1}", "principal": 0.0,
                              "term_months": 24, "monthly_rate": 0.01, "disbursement_month": 1})
            _mark_dirty(); st.rerun()

    # — Equity / Grants —
    with st.expander("📈 Equity & Grants"):
        st.markdown("**Equity raises**")
        for i, e in enumerate(d["equity_raises"]):
            cols = st.columns([3, 2, 2, 1])
            e["name"] = cols[0].text_input("Name", e["name"], key=f"eq_name_{i}")
            e["amount"] = float(cols[1].number_input("Amount", value=float(e["amount"]), key=f"eq_amt_{i}"))
            e["closing_month"] = int(cols[2].number_input("Close mo", value=int(e["closing_month"]), key=f"eq_mo_{i}"))
            if cols[3].button("✕", key=f"eq_del_{i}"):
                d["equity_raises"].pop(i); _mark_dirty(); st.rerun()
            # Mode gating
            current = e.get("enabled_in_modes")
            modes_list = list(d.get("modes", []))
            sel = st.multiselect(
                "Enabled in modes (empty = all)",
                modes_list,
                default=current if current is not None else modes_list,
                key=f"eq_modes_{i}",
            )
            e["enabled_in_modes"] = sel if (sel and set(sel) != set(modes_list)) else None
        if st.button("➕ Add equity raise"):
            d["equity_raises"].append({"name": f"Fundraise #{len(d['equity_raises'])+1}",
                                        "amount": 0.0, "closing_month": 1,
                                        "enabled_in_modes": ["with_investment"] if "with_investment" in d.get("modes", []) else None})
            _mark_dirty(); st.rerun()

        st.markdown("**Grants**")
        for i, g in enumerate(d["grants"]):
            cols = st.columns([3, 2, 2, 1])
            g["name"] = cols[0].text_input("Name", g["name"], key=f"gr_name_{i}")
            g["amount"] = float(cols[1].number_input("Amount", value=float(g["amount"]), key=f"gr_amt_{i}"))
            g["receiving_month"] = int(cols[2].number_input("Recv mo", value=int(g["receiving_month"]), key=f"gr_mo_{i}"))
            if cols[3].button("✕", key=f"gr_del_{i}"):
                d["grants"].pop(i); _mark_dirty(); st.rerun()
        if st.button("➕ Add grant"):
            d["grants"].append({"name": f"Grant #{len(d['grants'])+1}",
                                 "amount": 0.0, "receiving_month": 1})
            _mark_dirty(); st.rerun()

    # — Other Income & Tax —
    with st.expander("💵 Other income & tax"):
        sched_df = pd.DataFrame([{"start": e["months"][0], "end": e["months"][1], "monthly": e["monthly"]}
                                  for e in d["other_income"]["schedule"]])
        ed_sched = st.data_editor(sched_df, key="oi_sched", num_rows="dynamic")
        d["other_income"]["schedule"] = [
            {"months": [int(r["start"]), int(r["end"])], "monthly": float(r["monthly"])}
            for _, r in ed_sched.iterrows() if pd.notna(r["start"])
        ]
        d["tax"]["effective_rate"] = float(
            st.slider("Effective tax rate", 0.0, 0.5,
                      float(d["tax"]["effective_rate"]), step=0.005, format="%.3f")
        )

# Auto-mark dirty on any sidebar change (Streamlit re-runs on every interaction).
# Simpler heuristic: compare against on-disk version.
_on_disk = load_inputs(INPUTS_PATH).to_dict()
if d != _on_disk:
    st.session_state["dirty"] = True


# ── Compute ───────────────────────────────────────────────────────────────────

inputs = _current_inputs()
warnings = inputs.validate()
if warnings:
    for w in warnings:
        st.warning(w)

results = run_model(inputs)
H = inputs.horizon_months


# ── Main panel ────────────────────────────────────────────────────────────────

st.title("The Software Society — Financial Model")

# Top bar: scenario + mode pickers
sel_cols = st.columns([3, 3])
with sel_cols[0]:
    default_sc_idx = inputs.scenarios.index("standard") if "standard" in inputs.scenarios else 0
    scenario = st.radio("Scenario", inputs.scenarios, index=default_sc_idx,
                        horizontal=True, key="scenario_radio")
with sel_cols[1]:
    default_mode_idx = 0
    mode = st.radio("Mode", inputs.modes, index=default_mode_idx,
                    horizontal=True, key="mode_radio",
                    help="with_investment = $700k raise lands M6 + scheduled hires; "
                         "no_investment = self-funding hires gated by MRR thresholds")

r = results[scenario][mode]

n_years = (H + 11) // 12
year_labels = [f"Year {y+1}" for y in range(n_years)]

# Summary KPIs (one row per scenario, mode-filtered)
st.subheader(f"Summary KPIs — {mode.replace('_', ' ').title()} (annual)")
summary_rows = []
for sc in inputs.scenarios:
    rev_y = _annual_sums(results[sc][mode].revenue, "total", H)
    summary_rows.append([sc.title()] + [_fmt_currency(v) for v in rev_y])
summary_df = pd.DataFrame(summary_rows, columns=["Scenario"] + year_labels).set_index("Scenario")
st.dataframe(summary_df, use_container_width=True)

st.markdown(f"**Selected:** `{scenario}` × `{mode}`")

# ── Headline KPIs ──
k = r.kpis or {}
fp = k.get("first_profitable_month")
fsp = k.get("first_sustained_profit_month")
gm_m = k.get("gm_steady_state_month")
gm_v = k.get("gm_steady_state_value")
ct_m = k.get("cash_trough_month")
ct_v = k.get("cash_trough_value")
hcols = st.columns(5)
hcols[0].metric(
    "First Profitable Month",
    f"M{fp}" if fp else "—",
    help="First single month where Net Result > 0 (may be one-off)",
)
hcols[1].metric(
    "Sustained Profit From",
    f"M{fsp}" if fsp else "—",
    help="First month from which Net Result stays > 0 for the rest of the horizon",
)
hcols[2].metric(
    "Gross Margin Steady-State",
    f"{gm_v*100:.0f}%" if gm_v is not None else "—",
    f"from M{gm_m}" if gm_m else "—",
    help="Long-run GM (median of last 6 months); first month it enters and stays within ±3pp",
)
trough_label = f"${ct_v:,.0f}" if ct_v is not None else "—"
trough_delta = f"M{ct_m}" if ct_m else None
trough_help = "Lowest end-of-month cash. Negative ⇒ shortfall (need bridge / raise / cost cuts)."
hcols[3].metric(
    "Cash Trough",
    trough_label,
    trough_delta,
    delta_color="inverse" if (ct_v is not None and ct_v < 0) else "normal",
    help=trough_help,
)
hcols[4].metric(
    "Active Customers @ M36",
    f"{int(r.active_clients[-1]):,}" if len(r.active_clients) else "—",
    help="Total platform-active customers at end of horizon",
)

# ── Annual Y1/Y2/Y3 grid (existing) ──
cols = st.columns(n_years)
rev_y = _annual_sums(r.revenue, "total", H)
net_y = _annual_sums(r.pnl, "net_result", H)
cash_eoy = _annual_endings(r.cash_flow, "final_cash", H)
for i, c in enumerate(cols):
    c.metric(f"Y{i+1} Revenue", _fmt_currency(rev_y[i]))
    c.metric(f"Y{i+1} Net Result", _fmt_currency(net_y[i]))
    c.metric(f"Y{i+1} End-of-Year Cash", _fmt_currency(cash_eoy[i]))

# Tabs
tab_pnl, tab_bs, tab_cf, tab_charts, tab_proj, tab_export = st.tabs([
    "P&L", "Balance Sheet", "Cash Flow", "Charts", "Projections", "Export"])

# ── P&L tab ───────
with tab_pnl:
    pnl_money_rows = ["revenue", "cost_of_sales", "gross_result",
                      "salaries", "depreciation_amortization", "other_opex",
                      "operational_expenditures", "operational_result",
                      "other_income", "interest_expense", "net_result_pretax",
                      "tax", "net_result", "ebitda"]
    pnl_pct_rows = ["gross_margin", "operational_margin", "net_margin", "ebitda_margin"]

    # Build a wide table: rows = lines, cols = months M1..MH and Year totals
    lines = pnl_money_rows + pnl_pct_rows
    table = {}
    for ln in lines:
        vals = list(r.pnl[ln].values)
        table[ln] = vals
    wide = pd.DataFrame(table).T
    wide.columns = [f"M{i+1}" for i in range(H)]
    # Insert year-total columns
    for y in range(n_years):
        slice_ = wide.iloc[:, y * 12:(y + 1) * 12]
        wide[f"Y{y+1}"] = slice_.sum(axis=1)
    # Reorder so Y_i sits after the corresponding 12 months
    ordered = []
    for y in range(n_years):
        ordered.extend([f"M{y*12+i+1}" for i in range(12) if y*12+i < H])
        ordered.append(f"Y{y+1}")
    wide = wide[ordered]
    # Replace pct year sums with weighted-average margins
    margin_numerators = {
        "gross_margin": "gross_result",
        "operational_margin": "operational_result",
        "net_margin": "net_result",
        "ebitda_margin": "ebitda",
    }
    for ln in pnl_pct_rows:
        for y in range(n_years):
            r_y = r.pnl["revenue"].iloc[y*12:(y+1)*12].sum()
            metric_y = r.pnl[margin_numerators[ln]].iloc[y*12:(y+1)*12].sum()
            wide.loc[ln, f"Y{y+1}"] = (metric_y / r_y) if r_y else 0.0

    # Format
    fmt = {col: "${:,.0f}" for col in wide.columns}
    pct_fmt = {col: "{:.1%}" for col in wide.columns}
    st.markdown("**P&L (rows = lines, columns = months + Year totals)**")
    money_view = wide.loc[pnl_money_rows].map(lambda v: f"${v:,.0f}")
    st.dataframe(money_view, use_container_width=True)
    st.markdown("**Margins**")
    pct_view = wide.loc[pnl_pct_rows].map(lambda v: f"{v:.1%}")
    st.dataframe(pct_view, use_container_width=True)

# ── Balance Sheet tab ───────
with tab_bs:
    bs_rows = ["cash", "accounts_receivable", "current_assets",
               "capex_net", "tech_net", "non_current_assets", "total_assets",
               "accounts_payable", "labor_obligations", "debt", "total_liabilities",
               "equity_investment", "grants", "net_result", "accumulated_result", "total_equity"]
    table = {ln: list(r.balance_sheet[ln].values) for ln in bs_rows}
    wide = pd.DataFrame(table).T
    wide.columns = [f"M{i+1}" for i in range(H)]
    for y in range(n_years):
        wide[f"Y{y+1} (EOY)"] = wide.iloc[:, (y+1)*12 - 1] if (y+1)*12 - 1 < H else wide.iloc[:, -1]
    money_view = wide.map(lambda v: f"${v:,.0f}")
    st.dataframe(money_view, use_container_width=True)

# ── Cash Flow tab ───────
with tab_cf:
    cf_rows = ["ebitda", "delta_ar", "delta_ap", "delta_labor", "owk_movement",
               "capex_investment", "tech_investment", "taxes", "free_op_cf",
               "equity_in", "grants_in", "debt_disbursements", "debt_principal_paid",
               "debt_interest_paid", "other_income", "non_op_cf",
               "cash_variation", "initial_cash", "final_cash"]
    table = {ln: list(r.cash_flow[ln].values) for ln in cf_rows}
    wide = pd.DataFrame(table).T
    wide.columns = [f"M{i+1}" for i in range(H)]
    for y in range(n_years):
        slice_ = wide.iloc[:, y*12:(y+1)*12]
        wide[f"Y{y+1}"] = slice_.sum(axis=1)
    # final_cash should be EOY, not sum
    for y in range(n_years):
        end_idx = min((y+1)*12 - 1, H - 1)
        wide.loc["final_cash", f"Y{y+1}"] = r.cash_flow["final_cash"].iloc[end_idx]
        wide.loc["initial_cash", f"Y{y+1}"] = r.cash_flow["initial_cash"].iloc[y*12]
    money_view = wide.map(lambda v: f"${v:,.0f}")
    st.dataframe(money_view, use_container_width=True)

# ── Charts tab ───────
with tab_charts:
    months = list(range(1, H + 1))

    chart_view = st.radio(
        "Chart view",
        ["Active mode only", "Compare modes (selected scenario)"],
        index=1, horizontal=True, key="chart_view_mode",
    )

    fig = go.Figure()
    fig2 = go.Figure()
    fig3 = go.Figure()

    if chart_view == "Compare modes (selected scenario)":
        # All modes for the selected scenario
        for m in inputs.modes:
            res = results[scenario][m]
            label = f"{scenario.title()} / {m}"
            fig.add_trace(go.Scatter(x=months, y=res.revenue["total"].values, mode="lines", name=label))
            fig2.add_trace(go.Scatter(x=months, y=res.cash_flow["final_cash"].values, mode="lines", name=label))
            fig3.add_trace(go.Scatter(x=months, y=res.active_clients, mode="lines", name=label))
    else:
        # All scenarios for the active mode
        for sc in inputs.scenarios:
            res = results[sc][mode]
            fig.add_trace(go.Scatter(x=months, y=res.revenue["total"].values, mode="lines", name=sc.title()))
            fig2.add_trace(go.Scatter(x=months, y=res.cash_flow["final_cash"].values, mode="lines", name=sc.title()))
            fig3.add_trace(go.Scatter(x=months, y=res.active_clients, mode="lines", name=sc.title()))

    fig.update_layout(title="Monthly Revenue", xaxis_title="Month", yaxis_title="Revenue ($)")
    fig2.update_layout(title="Final Cash", xaxis_title="Month", yaxis_title="Cash ($)")
    fig3.update_layout(title="Active Customers", xaxis_title="Month", yaxis_title="Clients")
    # Cash chart: zero line for visual reference
    fig2.add_hline(y=0, line_dash="dot", line_color="grey")
    st.plotly_chart(fig, use_container_width=True)
    st.plotly_chart(fig2, use_container_width=True)
    st.plotly_chart(fig3, use_container_width=True)

    # P&L stack for selected scenario
    fig4 = go.Figure()
    fig4.add_trace(go.Bar(name="Revenue", x=months, y=r.pnl["revenue"]))
    fig4.add_trace(go.Bar(name="Cost of Sales", x=months, y=-r.pnl["cost_of_sales"]))
    fig4.add_trace(go.Bar(name="Salaries", x=months, y=-r.pnl["salaries"]))
    fig4.add_trace(go.Bar(name="Other OpEx", x=months, y=-r.pnl["other_opex"]))
    fig4.add_trace(go.Bar(name="D&A", x=months, y=-r.pnl["depreciation_amortization"]))
    fig4.add_trace(go.Scatter(name="Net Result", x=months, y=r.pnl["net_result"], mode="lines+markers", line=dict(color="black", width=3)))
    fig4.update_layout(title=f"P&L Composition — {scenario.title()}", barmode="relative")
    st.plotly_chart(fig4, use_container_width=True)

# ── Projections tab ───────
with tab_proj:
    st.markdown("**Revenue breakdown** (per-tier per-component)")
    st.dataframe(r.revenue_breakdown.map(lambda v: f"${v:,.0f}"), use_container_width=True)

    st.markdown("**Cost streams**")
    st.dataframe(r.costs.map(lambda v: f"${v:,.0f}"), use_container_width=True)

    st.markdown("**Salaries by role**")
    st.dataframe(r.salaries.map(lambda v: f"${v:,.0f}"), use_container_width=True)

    st.markdown("**OpEx by category**")
    st.dataframe(r.opex.map(lambda v: f"${v:,.0f}"), use_container_width=True)

# ── Export tab ───────
with tab_export:
    st.markdown("Generate a `.xlsx` workbook of the current model state.")
    if st.button("📥 Build Excel export"):
        from export.to_excel import build_workbook  # local import keeps cold-start fast
        buf = io.BytesIO()
        # Excel export takes a flat scenario→ScenarioResult mapping for the active mode.
        flat_results = {sc: results[sc][mode] for sc in inputs.scenarios}
        build_workbook(inputs, flat_results, buf)
        buf.seek(0)
        st.download_button("Download .xlsx", buf,
                           file_name="tss-financial-model-export.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
