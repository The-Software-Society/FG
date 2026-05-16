# TSS Financial Model

Code-based recreation of `The Software Society - Financial Model (1).xlsx`.
Identical baseline numbers (every Y1/Y2/Y3 total reproduced to the cent), but
designed to be edited and extended without spreadsheet pain.

## Quick start

```bash
cd /Users/jon/Downloads/software-society/financial-model
python3 -m pip install -r requirements.txt

# (Re-)seed inputs.yaml from the original .xlsx — already done once on setup
python3 scripts/extract_baseline_from_xlsx.py

# Run the regression suite — must be 14/14 passing before edits
python3 -m pytest

# Launch the editing UI
streamlit run app.py
```

The Streamlit app opens at <http://localhost:8501>. It's gated behind a 5-character
password (local dev fallback: `tss26` — change before deploying). Edit any input
on the left, the right panel recomputes immediately. **Save inputs.yaml** persists
the edits; **Export to Excel** generates a workbook for sharing.

## Hosting

See [`DEPLOY.md`](./DEPLOY.md) for Streamlit Community Cloud / Render setup
instructions, password configuration, and an honest assessment of Vercel
compatibility (spoiler: it requires a Next.js rewrite).

## File map

| Path | Purpose |
|---|---|
| `inputs.yaml` | Source of truth — every assumption. Hand-edit or use the UI. |
| `app.py` | Streamlit editing UI. |
| `model/` | Pure-Python compute engine (no UI imports here). |
| `model/config.py` | Typed dataclass schema for `inputs.yaml`. |
| `model/runner.py` | `run_model(inputs)` orchestrator. |
| `model/revenue.py` | Active clients + tier split + sub/seat/credit revenue. |
| `model/costs.py` | 3 cost-stream kinds: step / per-active-client / per-credit. |
| `model/receivables.py`, `payables.py` | AR/AP rolling balances. |
| `model/salaries.py`, `opex.py` | Headcount × base + fringe; step-table OpEx. |
| `model/capex.py`, `debt.py`, `equity.py` | Capital structure schedules. |
| `model/statements.py` | P&L / BS / CF rollup. |
| `scripts/extract_baseline_from_xlsx.py` | One-time bootstrap from the .xlsx. |
| `export/to_excel.py` | Write computed model back to `.xlsx`. |
| `tests/` | pytest regression suite (baseline match + extensibility). |

## Verified baseline (matches the original Excel exactly)

| Year | Conservative | Standard | Optimistic |
|---|---|---|---|
| Y1 Revenue | $30,412.80 | $64,240.80 | $90,304.80 |
| Y2 Revenue | $305,791.20 | $689,268.00 | $1,998,280.80 |
| Y3 Revenue | $991,029.60 | $1,998,280.80 | $2,940,343.20 |
| Y1 Net (Std) | — | $-120,452.43 | — |
| Y3 EOY Cash (Std) | — | $1,273,721.59 | — |

`pytest` enforces these.

## Editing patterns

### Tweak a single assumption
1. Open the UI sidebar, change the value, click **Save inputs.yaml**.
2. Or hand-edit `inputs.yaml` and re-run.

### Change time horizon (e.g., 36 → 48 months)
- UI: expand "⏱ Horizon", change the value. Existing per-month arrays auto-extend by repeating the last value.
- YAML: bump `horizon_months`. Make sure each `new_clients_per_month[scenario]` and each role's `headcount_by_month` has the new length.

### Add a revenue stream
- UI: click **➕ Add revenue stream** in the sidebar (clones the first stream).
- YAML: append a new entry to the `revenue_streams` list.

### Add a cost stream / CAPEX / debt / equity / grant
- UI: each section has an **➕ Add** button.
- YAML: append to the relevant list. Cost streams support three `kind`s:
  - `step` — `schedule: [{months: [start, end], monthly: amount}, ...]`
  - `per_active_client` — `per_active_client_by_year: [Y1, Y2, Y3]`
  - `per_credit` — `per_credit_by_year: [Y1, Y2, Y3]`

### Restructure scenarios (rename, add a 4th)
- Edit the top-level `scenarios:` list in `inputs.yaml`.
- Update every nested scenario-keyed dict (`new_clients_per_month`, `tier_split`,
  `subscription`, `seats`, `credits`) to include the new key.
- The compute engine and Streamlit UI both pick up the new scenarios automatically.

## Notes / known divergences from the Excel original

- Excel applies fringe-benefit `1+pct` to monthly base when displayed (e.g.
  `D109 = C109 * (1 + C120) = 7215`). The compute engine here computes
  `salaries.total = base + base*pct` which is mathematically equivalent.
- Excel computes "Cost Stream 2" as `active_clients × per_year_amount` where the
  per-year amount is identical across Y1/Y2/Y3 ($5/mo). The engine indexes by
  `(month-1) // 12`, which behaves the same.
- Excel uses `ROUNDDOWN` for the active-client churn step. The Python engine uses
  `math.floor`, which is identical for positive numbers.

## Adding a feature to the UI

If you add a new field to `inputs.yaml` and want a widget for it, add a block to
the relevant `with st.expander(...)` section in `app.py`. The widget should
mutate `st.session_state["inputs_dict"]` directly — the compute engine reads
that on every Streamlit re-run. No state synchronization needed.

## Re-seeding from the original .xlsx

```bash
python3 scripts/extract_baseline_from_xlsx.py
```

This **overwrites** `inputs.yaml`. If you've made unsaved edits in the UI, save
them first (or they're lost).

## Why YAML and not JSON?

YAML preserves comments and is friendlier to hand-edit. The downside (significant
indentation) doesn't matter much for a flat-ish config like this.
