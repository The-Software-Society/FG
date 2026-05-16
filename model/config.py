"""Typed schema for inputs.yaml.

Loading a YAML file goes through `load_inputs()` which returns a fully-validated
`Inputs` dataclass. Saving back uses `save_inputs()` and round-trips cleanly.

Adding a new input field:
    1. Add a field to the relevant dataclass below.
    2. Update `Inputs.from_dict()` / `to_dict()` if non-trivial.
    3. The Streamlit UI auto-renders new primitive fields; complex fields need
       a widget block in app.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scenario_keyed(d: dict) -> dict[str, float]:
    """{conservative,standard,optimistic} → dict of floats. Tolerates either short or long keys."""
    return {k: float(v) for k, v in d.items()}


# ── Revenue ───────────────────────────────────────────────────────────────────

@dataclass
class Schedule:
    """Step table: list of {months: [start, end], monthly: amount}."""
    entries: list[dict[str, Any]] = field(default_factory=list)

    def value_at(self, month: int) -> float:
        for e in self.entries:
            lo, hi = e["months"]
            if lo <= month <= hi:
                return float(e["monthly"])
        return 0.0

    @classmethod
    def from_list(cls, raw: list[dict] | None) -> "Schedule":
        return cls(entries=list(raw or []))

    def to_list(self) -> list[dict]:
        return list(self.entries)


@dataclass
class TierScenarioPrice:
    """Per-tier per-scenario price/quantity."""
    basic: dict[str, float]
    pro:   dict[str, float]

    @classmethod
    def from_dict(cls, d: dict) -> "TierScenarioPrice":
        return cls(basic=_scenario_keyed(d["basic"]), pro=_scenario_keyed(d["pro"]))

    def to_dict(self) -> dict:
        return {"basic": dict(self.basic), "pro": dict(self.pro)}

    def get(self, tier: str, scenario: str) -> float:
        return float(getattr(self, tier)[scenario])


@dataclass
class RevenueStream:
    name: str
    business_model: str
    new_clients_per_month: dict[str, list[float]]   # scenario → 36-month list
    churn_monthly: float
    tier_split: dict[str, dict[str, float]]          # scenario → {basic, pro}
    pricing: dict                                     # nested: subscription, seats, credits
    invoice_fulfillment_pct: float
    collection_lag_months: int
    # New for TSS: how the stream bills
    #   monthly_subscription → revenue/mo = active_clients × subscription_price (legacy)
    #   one_time_bundle      → revenue/mo = new_clients_this_month × bundle_price; customer
    #                          stays "platform-active" for bundle_term_months
    pricing_kind: str = "monthly_subscription"
    bundle_term_months: int | None = None
    bundle_price: dict[str, float] | None = None    # scenario → price (only for one_time_bundle)
    # If set, new_clients[m] = sales_headcount[m] × per_rep_rate[scenario] (overrides
    # the manual `new_clients_per_month` ramp). Sales headcount is the sum of
    # heads of any role whose name starts with "Sales" in the active mode.
    customers_per_salesperson_per_month: dict[str, float] | None = None
    active_after_month: int | None = None    # if set, force new_clients = 0 before this month
    inactive_after_month: int | None = None  # if set, force new_clients = 0 after this month (cohort price grandfathered)

    @classmethod
    def from_dict(cls, d: dict) -> "RevenueStream":
        return cls(
            name=d["name"],
            business_model=d.get("business_model", ""),
            new_clients_per_month={k: [float(x) for x in v] for k, v in d["new_clients_per_month"].items()},
            churn_monthly=float(d["churn_monthly"]),
            tier_split={k: {"basic": float(v["basic"]), "pro": float(v["pro"])} for k, v in d["tier_split"].items()},
            pricing=d["pricing"],
            invoice_fulfillment_pct=float(d["invoice_fulfillment_pct"]),
            collection_lag_months=int(d["collection_lag_months"]),
            pricing_kind=str(d.get("pricing_kind", "monthly_subscription")),
            bundle_term_months=(int(d["bundle_term_months"]) if d.get("bundle_term_months") is not None else None),
            bundle_price=({k: float(v) for k, v in d["bundle_price"].items()} if d.get("bundle_price") else None),
            customers_per_salesperson_per_month=(
                {k: float(v) for k, v in d["customers_per_salesperson_per_month"].items()}
                if d.get("customers_per_salesperson_per_month") else None
            ),
            active_after_month=(int(d["active_after_month"]) if d.get("active_after_month") is not None else None),
            inactive_after_month=(int(d["inactive_after_month"]) if d.get("inactive_after_month") is not None else None),
        )

    def to_dict(self) -> dict:
        out = asdict(self)
        if self.pricing_kind == "monthly_subscription":
            out.pop("bundle_term_months", None)
            out.pop("bundle_price", None)
        if self.customers_per_salesperson_per_month is None:
            out.pop("customers_per_salesperson_per_month", None)
        if self.active_after_month is None:
            out.pop("active_after_month", None)
        if self.inactive_after_month is None:
            out.pop("inactive_after_month", None)
        return out

    # — convenience accessors used by the compute engine —

    def subscription_price(self, tier: str, scenario: str) -> float:
        return float(self.pricing["subscription"][tier][scenario])

    def seats_per_client(self, tier: str, scenario: str) -> float:
        return float(self.pricing["seats"]["per_client"][tier][scenario])

    def seat_price(self, tier: str, scenario: str) -> float:
        return float(self.pricing["seats"]["price"][tier][scenario])

    def credits_per_client_monthly(self, tier: str, scenario: str) -> float:
        return float(self.pricing["credits"]["per_client_monthly"][tier][scenario])

    def credit_price(self, tier: str, scenario: str) -> float:
        return float(self.pricing["credits"]["price"][tier][scenario])

    def get_bundle_price(self, scenario: str) -> float:
        if self.bundle_price is None:
            return 0.0
        return float(self.bundle_price.get(scenario, 0.0))


# ── Add-ons ───────────────────────────────────────────────────────────────────

@dataclass
class AddOn:
    """Platform-wide add-on revenue layered on top of channel subscriptions.

    Quantity = sum of platform-active customers from `applies_to` streams.
    Revenue/mo = quantity × arpu_schedule.value_at(month).
    """
    name: str
    arpu_schedule: Schedule
    applies_to: Any = "all"   # "all" or list[str] of stream names

    @classmethod
    def from_dict(cls, d: dict) -> "AddOn":
        return cls(
            name=d["name"],
            arpu_schedule=Schedule.from_list(d.get("arpu_schedule", [])),
            applies_to=d.get("applies_to", "all"),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "arpu_schedule": self.arpu_schedule.to_list(),
            "applies_to": self.applies_to,
        }

    def applies_to_stream(self, stream_name: str) -> bool:
        if self.applies_to == "all":
            return True
        return stream_name in self.applies_to


# ── Costs ─────────────────────────────────────────────────────────────────────

@dataclass
class CostStream:
    name: str
    kind: str                               # "step" | "per_active_client" | "per_credit" | "fraction_of_addon"
    schedule: Schedule | None = None        # for kind=step
    per_active_client_by_year: list[float] | None = None    # for kind=per_active_client
    per_credit_by_year: list[float] | None = None           # for kind=per_credit
    addon_name: str | None = None                           # for kind=fraction_of_addon
    fraction: float | None = None                           # for kind=fraction_of_addon (flat)
    fraction_schedule: Schedule | None = None               # for kind=fraction_of_addon (per-month %)

    @classmethod
    def from_dict(cls, d: dict) -> "CostStream":
        return cls(
            name=d["name"],
            kind=d["kind"],
            schedule=Schedule.from_list(d.get("schedule")) if d.get("kind") == "step" else None,
            per_active_client_by_year=[float(x) for x in d["per_active_client_by_year"]] if d.get("kind") == "per_active_client" else None,
            per_credit_by_year=[float(x) for x in d["per_credit_by_year"]] if d.get("kind") == "per_credit" else None,
            addon_name=d.get("addon_name") if d.get("kind") == "fraction_of_addon" else None,
            fraction=float(d["fraction"]) if d.get("kind") == "fraction_of_addon" and d.get("fraction") is not None else None,
            fraction_schedule=(Schedule.from_list(d["fraction_schedule"])
                                if d.get("kind") == "fraction_of_addon" and d.get("fraction_schedule") is not None
                                else None),
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.kind == "step" and self.schedule:
            out["schedule"] = self.schedule.to_list()
        elif self.kind == "per_active_client":
            out["per_active_client_by_year"] = list(self.per_active_client_by_year or [])
        elif self.kind == "per_credit":
            out["per_credit_by_year"] = list(self.per_credit_by_year or [])
        elif self.kind == "fraction_of_addon":
            out["addon_name"] = self.addon_name
            if self.fraction_schedule is not None:
                out["fraction_schedule"] = self.fraction_schedule.to_list()
            else:
                out["fraction"] = self.fraction
        return out


# ── Salaries / OpEx ───────────────────────────────────────────────────────────

@dataclass
class HireTrigger:
    """Self-funding hire condition. The first month where ALL conditions are met
    is the activation month; from that month onward, headcount = 1.

    earliest_month is a hard floor (won't trigger before that calendar month).
    """
    monthly_revenue_at_least: float | None = None
    sales_headcount_at_least: int | None = None
    support_headcount_at_least: int | None = None
    earliest_month: int = 1
    headcount: int = 1   # value once triggered

    @classmethod
    def from_dict(cls, d: dict) -> "HireTrigger":
        return cls(
            monthly_revenue_at_least=(float(d["monthly_revenue_at_least"]) if d.get("monthly_revenue_at_least") is not None else None),
            sales_headcount_at_least=(int(d["sales_headcount_at_least"]) if d.get("sales_headcount_at_least") is not None else None),
            support_headcount_at_least=(int(d["support_headcount_at_least"]) if d.get("support_headcount_at_least") is not None else None),
            earliest_month=int(d.get("earliest_month", 1)),
            headcount=int(d.get("headcount", 1)),
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"earliest_month": self.earliest_month, "headcount": self.headcount}
        if self.monthly_revenue_at_least is not None:
            out["monthly_revenue_at_least"] = self.monthly_revenue_at_least
        if self.sales_headcount_at_least is not None:
            out["sales_headcount_at_least"] = self.sales_headcount_at_least
        if self.support_headcount_at_least is not None:
            out["support_headcount_at_least"] = self.support_headcount_at_least
        return out


@dataclass
class PayTrigger:
    """A single pay step: when conditions are met, the role's pay becomes
    `monthly_amount`. In any given month, the role's active pay is the MAX
    monthly_amount across all triggers whose conditions are satisfied for that
    month — so triggers stack monotonically.

    earliest_month is a calendar floor; monthly_revenue_at_least is an MRR floor.
    A trigger with both None and earliest_month=1 is always-on.
    """
    monthly_amount: float
    monthly_revenue_at_least: float | None = None
    earliest_month: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "PayTrigger":
        return cls(
            monthly_amount=float(d["monthly_amount"]),
            monthly_revenue_at_least=(float(d["monthly_revenue_at_least"]) if d.get("monthly_revenue_at_least") is not None else None),
            earliest_month=int(d.get("earliest_month", 1)),
        )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"monthly_amount": self.monthly_amount, "earliest_month": self.earliest_month}
        if self.monthly_revenue_at_least is not None:
            out["monthly_revenue_at_least"] = self.monthly_revenue_at_least
        return out


@dataclass
class SalaryRole:
    """A staffing role.

    Three layered ways to specify headcount (most-specific wins per mode):
        1. hire_trigger_by_mode[mode]   — self-funding: first month MRR ≥ threshold
        2. headcount_by_mode[mode]      — static per-mode 36-month array
        3. headcount_by_month           — legacy: single array, used for every mode

    Three layered ways to specify base monthly salary:
        1. pay_triggers_by_mode[mode]         — self-funding: MRR-stacked pay steps
        2. base_monthly_schedule_by_mode[mode] — static per-mode step schedule
        3. base_monthly_schedule              — schedule applied to every mode
        4. base_monthly                        — flat single value (legacy)
    """
    name: str
    base_monthly: float                                                 # legacy fallback
    headcount_by_month: list[int] | None = None                         # legacy
    headcount_by_mode: dict[str, list[int]] | None = None
    hire_trigger_by_mode: dict[str, HireTrigger] | None = None
    base_monthly_schedule: Schedule | None = None
    base_monthly_schedule_by_mode: dict[str, Schedule] | None = None
    pay_triggers_by_mode: dict[str, list[PayTrigger]] | None = None


@dataclass
class Salaries:
    fringe_benefits_pct: float
    payment_periodicity_months: int
    roles: list[SalaryRole]

    @classmethod
    def from_dict(cls, d: dict) -> "Salaries":
        roles = []
        for r in d["roles"]:
            roles.append(SalaryRole(
                name=r["name"],
                base_monthly=float(r.get("base_monthly", 0.0)),
                headcount_by_month=([int(x) for x in r["headcount_by_month"]] if r.get("headcount_by_month") is not None else None),
                headcount_by_mode=(
                    {m: [int(x) for x in arr] for m, arr in r["headcount_by_mode"].items()}
                    if r.get("headcount_by_mode") is not None else None
                ),
                hire_trigger_by_mode=(
                    {m: HireTrigger.from_dict(t) for m, t in r["hire_trigger_by_mode"].items()}
                    if r.get("hire_trigger_by_mode") is not None else None
                ),
                base_monthly_schedule=(
                    Schedule.from_list(r["base_monthly_schedule"]) if r.get("base_monthly_schedule") is not None else None
                ),
                base_monthly_schedule_by_mode=(
                    {m: Schedule.from_list(s) for m, s in r["base_monthly_schedule_by_mode"].items()}
                    if r.get("base_monthly_schedule_by_mode") is not None else None
                ),
                pay_triggers_by_mode=(
                    {m: [PayTrigger.from_dict(t) for t in lst] for m, lst in r["pay_triggers_by_mode"].items()}
                    if r.get("pay_triggers_by_mode") is not None else None
                ),
            ))
        return cls(
            fringe_benefits_pct=float(d["fringe_benefits_pct"]),
            payment_periodicity_months=int(d["payment_periodicity_months"]),
            roles=roles,
        )

    def to_dict(self) -> dict:
        out_roles = []
        for r in self.roles:
            d: dict[str, Any] = {"name": r.name, "base_monthly": r.base_monthly}
            if r.headcount_by_month is not None:
                d["headcount_by_month"] = list(r.headcount_by_month)
            if r.headcount_by_mode is not None:
                d["headcount_by_mode"] = {m: list(arr) for m, arr in r.headcount_by_mode.items()}
            if r.hire_trigger_by_mode is not None:
                d["hire_trigger_by_mode"] = {m: t.to_dict() for m, t in r.hire_trigger_by_mode.items()}
            if r.base_monthly_schedule is not None:
                d["base_monthly_schedule"] = r.base_monthly_schedule.to_list()
            if r.base_monthly_schedule_by_mode is not None:
                d["base_monthly_schedule_by_mode"] = {m: s.to_list() for m, s in r.base_monthly_schedule_by_mode.items()}
            if r.pay_triggers_by_mode is not None:
                d["pay_triggers_by_mode"] = {m: [t.to_dict() for t in lst] for m, lst in r.pay_triggers_by_mode.items()}
            out_roles.append(d)
        return {
            "fringe_benefits_pct": self.fringe_benefits_pct,
            "payment_periodicity_months": self.payment_periodicity_months,
            "roles": out_roles,
        }


@dataclass
class OpExCategory:
    name: str
    schedule: Schedule                                                   # default for any scenario
    enabled_in_modes: list[str] | None = None                            # None ⇒ always-on; else only those modes
    schedule_by_scenario: dict[str, Schedule] | None = None              # per-scenario override; falls back to `schedule`


# ── Capital structure ─────────────────────────────────────────────────────────

@dataclass
class CapexItem:
    name: str
    value: float
    depreciation_months: int
    acquisition_month: int


@dataclass
class TechInvestment:
    name: str
    value: float
    amortization_months: int
    acquisition_month: int


@dataclass
class DebtInstrument:
    name: str
    principal: float
    term_months: int
    monthly_rate: float
    disbursement_month: int


@dataclass
class EquityRaise:
    name: str
    amount: float
    closing_month: int
    # Modes in which this raise actually happens. Default (None) ⇒ happens in every mode
    # (legacy behavior). Typical: ["with_investment"].
    enabled_in_modes: list[str] | None = None


@dataclass
class Grant:
    name: str
    amount: float
    receiving_month: int
    enabled_in_modes: list[str] | None = None


# ── Top-level Inputs ──────────────────────────────────────────────────────────

@dataclass
class Inputs:
    horizon_months: int
    scenarios: list[str]
    modes: list[str]                 # e.g. ["with_investment", "no_investment"]; default just ["base"]
    revenue_streams: list[RevenueStream]
    add_ons: list[AddOn]
    payment_lag_months: int
    cost_streams: list[CostStream]
    salaries: Salaries
    opex: list[OpExCategory]
    capex: list[CapexItem]
    tech_investments: list[TechInvestment]
    debt: list[DebtInstrument]
    equity_raises: list[EquityRaise]
    grants: list[Grant]
    other_income: dict[str, Any]   # {"schedule": [...]}
    tax: dict[str, float]          # {"effective_rate": 0.21}

    @classmethod
    def from_dict(cls, d: dict) -> "Inputs":
        # Equity / Grant accept both legacy (no enabled_in_modes) and new shape
        def _equity(e: dict) -> EquityRaise:
            return EquityRaise(
                name=e["name"], amount=float(e["amount"]), closing_month=int(e["closing_month"]),
                enabled_in_modes=(list(e["enabled_in_modes"]) if e.get("enabled_in_modes") is not None else None),
            )
        def _grant(g: dict) -> Grant:
            return Grant(
                name=g["name"], amount=float(g["amount"]), receiving_month=int(g["receiving_month"]),
                enabled_in_modes=(list(g["enabled_in_modes"]) if g.get("enabled_in_modes") is not None else None),
            )
        return cls(
            horizon_months=int(d["horizon_months"]),
            scenarios=list(d["scenarios"]),
            modes=list(d.get("modes", ["base"])),
            revenue_streams=[RevenueStream.from_dict(rs) for rs in d.get("revenue_streams", [])],
            add_ons=[AddOn.from_dict(a) for a in d.get("add_ons", [])],
            payment_lag_months=int(d.get("payment_lag_months", 1)),
            cost_streams=[CostStream.from_dict(cs) for cs in d.get("cost_streams", [])],
            salaries=Salaries.from_dict(d["salaries"]),
            opex=[OpExCategory(
                name=o["name"],
                schedule=Schedule.from_list(o["schedule"]),
                enabled_in_modes=(list(o["enabled_in_modes"]) if o.get("enabled_in_modes") is not None else None),
                schedule_by_scenario=(
                    {sc: Schedule.from_list(s) for sc, s in o["schedule_by_scenario"].items()}
                    if o.get("schedule_by_scenario") is not None else None
                ),
            ) for o in d.get("opex", [])],
            capex=[CapexItem(**c) for c in d.get("capex", [])],
            tech_investments=[TechInvestment(**t) for t in d.get("tech_investments", [])],
            debt=[DebtInstrument(**x) for x in d.get("debt", [])],
            equity_raises=[_equity(e) for e in d.get("equity_raises", [])],
            grants=[_grant(g) for g in d.get("grants", [])],
            other_income=d.get("other_income", {"schedule": []}),
            tax=d.get("tax", {"effective_rate": 0.21}),
        )

    def to_dict(self) -> dict:
        def _eq(e):
            base = {"name": e.name, "amount": e.amount, "closing_month": e.closing_month}
            if e.enabled_in_modes is not None:
                base["enabled_in_modes"] = list(e.enabled_in_modes)
            return base
        def _gr(g):
            base = {"name": g.name, "amount": g.amount, "receiving_month": g.receiving_month}
            if g.enabled_in_modes is not None:
                base["enabled_in_modes"] = list(g.enabled_in_modes)
            return base
        return {
            "horizon_months": self.horizon_months,
            "scenarios": list(self.scenarios),
            "modes": list(self.modes),
            "revenue_streams": [rs.to_dict() for rs in self.revenue_streams],
            "add_ons": [a.to_dict() for a in self.add_ons],
            "payment_lag_months": self.payment_lag_months,
            "cost_streams": [cs.to_dict() for cs in self.cost_streams],
            "salaries": self.salaries.to_dict(),
            "opex": [
                ({"name": o.name, "schedule": o.schedule.to_list()}
                 | ({"enabled_in_modes": list(o.enabled_in_modes)} if o.enabled_in_modes is not None else {})
                 | ({"schedule_by_scenario": {sc: s.to_list() for sc, s in o.schedule_by_scenario.items()}}
                     if o.schedule_by_scenario is not None else {}))
                for o in self.opex
            ],
            "capex": [asdict(c) for c in self.capex],
            "tech_investments": [asdict(t) for t in self.tech_investments],
            "debt": [asdict(d) for d in self.debt],
            "equity_raises": [_eq(e) for e in self.equity_raises],
            "grants": [_gr(g) for g in self.grants],
            "other_income": self.other_income,
            "tax": self.tax,
        }

    def validate(self) -> list[str]:
        """Returns a list of human-readable warnings (empty = OK)."""
        warnings: list[str] = []
        H = self.horizon_months
        for rs in self.revenue_streams:
            for sc in self.scenarios:
                arr = rs.new_clients_per_month.get(sc)
                if arr is None or len(arr) != H:
                    warnings.append(f"{rs.name}: new_clients_per_month[{sc}] must have {H} values (has {0 if arr is None else len(arr)})")
                t = rs.tier_split.get(sc, {})
                total = float(t.get("basic", 0)) + float(t.get("pro", 0))
                if abs(total - 1.0) > 1e-6:
                    warnings.append(f"{rs.name}: tier_split[{sc}] sums to {total:.4f}, expected 1.0")
        for r in self.salaries.roles:
            if r.headcount_by_month is not None and len(r.headcount_by_month) != H:
                warnings.append(f"Salary role '{r.name}': headcount_by_month must have {H} values (has {len(r.headcount_by_month)})")
            if r.headcount_by_mode is not None:
                for m, arr in r.headcount_by_mode.items():
                    if len(arr) != H:
                        warnings.append(f"Salary role '{r.name}': headcount_by_mode[{m}] must have {H} values (has {len(arr)})")
            # Sanity: every mode should be resolvable
            for mode in self.modes:
                has_static = (r.headcount_by_mode is not None and mode in r.headcount_by_mode)
                has_trigger = (r.hire_trigger_by_mode is not None and mode in r.hire_trigger_by_mode)
                has_legacy = (r.headcount_by_month is not None)
                if not (has_static or has_trigger or has_legacy):
                    warnings.append(f"Salary role '{r.name}': no headcount source for mode '{mode}'")
        return warnings


# ── Public API ────────────────────────────────────────────────────────────────

def load_inputs(path: Path | str) -> Inputs:
    with Path(path).open("r") as f:
        raw = yaml.safe_load(f)
    return Inputs.from_dict(raw)


def save_inputs(inputs: Inputs, path: Path | str) -> None:
    with Path(path).open("w") as f:
        yaml.dump(inputs.to_dict(), f, sort_keys=False, allow_unicode=True,
                  default_flow_style=False, width=120)
