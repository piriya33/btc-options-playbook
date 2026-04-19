# AIRY/AIRS Implementation Plan

This plan is self-contained. Sonnet (or any successor) should be able to execute it without the prior chat transcript.

## Confirmed decisions (from user)

1. **Short-put rule**: Hybrid. Default to HOLD per blueprint when spot breaches the short put strike. Only ROLL if `margin_util > 20%` OR `DTE <= 7` OR `abs(contract_delta) >= 0.50`.
2. **Grouping**: Scrap string-timestamp "trade_id". Replace with **role tags + campaigns + derived spreads**:
   - Each leg has a `role` ∈ {YIELD_CALL (A), YIELD_PUT (B), CRASH_HEDGE (C), MOON_HEDGE (D)}.
   - Each leg belongs to a `Campaign` (user-named, default `AIRS-YYYY-MM`).
   - A campaign has exactly two derived spreads:
     - `put_spread` = legs B + C
     - `call_spread` = legs A + D
   - PnL rolls up: leg → spread (derived) → campaign → portfolio.
3. **Sizing**: Option A. One AIRS structure per BTC of equity, time-staggered across ~3 monthly vintages. Per-campaign scale = `equity / N_CAMPAIGNS` (default N=3). Blueprint ratios applied on top: A=0.5×scale, B=0.2×scale, C=0.6×scale, D=1.0×scale.
4. **Testnet only, price already confirmed BTC-denominated.**
5. **DVOL**: bot must ingest and maintain going forward. User provides the seed CSV once.

## Invariants the analyzer must expose

From the strategy blueprint:
- **Put ratio (long/short)**: `C_size / B_size ≥ 3.0`
- **Short call floor**: `A_size <= 1.0 × equity_btc` (Pillar 3; makes upside liquidation impossible)
- **Margin utilization**: `IM / equity <= 25%` (Pillar 3)
- **Convexity score**: `long_gamma / short_gamma` (higher = more antifragile)

These are **portfolio-wide**, not per-campaign. Surface them prominently in the morning briefing.

---

## Phase 1 — Correctness pass (no strategy decisions)

### 1.1 Call `init_db()` at bot startup
**File**: `src/bot/main.py`, `main()` function (around line 480).

Add near the top of `main()`:
```python
from database.session import init_db
init_db()
```
Without this the bot crashes on first run on a new machine.

### 1.2 Fix `/close` using market orders
**File**: `src/bot/main.py:277-299`.

The slash-command `close_cmd` uses `order_type="market"` but Deribit rejects market orders on options (the README even says so). The button handler at `main.py:440-461` already does it correctly with limit orders. Replace the body of `close_cmd` with equivalent logic: fetch `ticker`, pick best_bid for selling longs or best_ask for covering shorts, submit `order_type="limit"`.

### 1.3 Remove blocking `requests` from async handlers
**File**: `src/bot/main.py:164-166` (iv_cmd, Coingecko) and `main.py:194` (fear_greed_cmd, alternative.me).

Option A (minimal): wrap with `await asyncio.to_thread(requests.get, url, timeout=8)`.
Option B (cleaner): use `httpx.AsyncClient` directly.

Prefer Option B; `httpx` is already a dep.

### 1.4 Share a single `httpx.AsyncClient` in `DeribitClient`
**File**: `src/deribit/client.py`.

Currently every method does `async with httpx.AsyncClient() as client:` — fresh pool per request.

Changes:
- `__init__`: `self._client = httpx.AsyncClient(timeout=10.0)`
- Add `async def aclose(self): await self._client.aclose()`
- Replace all `async with httpx.AsyncClient() as client:` with direct use of `self._client`.
- Register shutdown in bot: use `application.post_shutdown` hook to call `await deribit_client.aclose()`.

### 1.5 Align margin thresholds
**File**: `src/bot/main.py:37`.

Currently:
```python
DELTA_DRIFT_LIMIT = 0.15
MARGIN_ALERT_PCT  = 70.0    # 45pts looser than the 25% blueprint rule
```

Replace with two tiers:
```python
MARGIN_WARN_PCT   = 25.0    # blueprint hard limit
MARGIN_ALERT_PCT  = 40.0    # critical — something went wrong
```

Update `_check_alerts` to send a yellow warning at WARN and a red alert at ALERT.

Update `AIRSAnalyzer.analyze_margin` to produce `"status"` values: `"Safe"` (<20), `"Warning"` (20-25), `"Breach"` (25-40), `"Critical"` (>40).

### 1.6 Remove unreachable branch in analyzer
**File**: `src/analyzer/logic.py:112-113`.

The `elif self.spot_price <= strike:` is dead code (caught by the earlier `elif` on line 109). Will be removed in Phase 3 rewrite anyway, but flag for completeness.

### 1.7 `datetime.utcnow()` → `datetime.now(UTC)`
**Files**: all (client.py, logic.py, models.py, queries.py, ingest_dvol.py, main.py).

Import `from datetime import datetime, UTC` and replace `datetime.utcnow()` with `datetime.now(UTC)`. For SQLAlchemy `default=`, use `lambda: datetime.now(UTC)`.

Deprecated in Python 3.12+; emits warnings at runtime.

### 1.8 Remove duplicate `load_dotenv`
**File**: `src/bot/main.py:10` and `:481`. Keep only the one in `main()`.

### 1.9 Proper package layout
Remove the `sys.path.append(...)` hack at `src/bot/main.py:15`.

- Add `src/__init__.py`, `src/bot/__init__.py`, `src/deribit/__init__.py`, etc.
- Add a minimal `pyproject.toml` at repo root:
  ```toml
  [project]
  name = "airy"
  version = "0.1.0"
  dependencies = ["httpx", "python-telegram-bot", "sqlalchemy", "pydantic",
                  "python-dotenv", "matplotlib", "numpy"]

  [tool.setuptools.packages.find]
  where = ["src"]
  ```
- Run as `python -m bot.main` from the `src/` directory, OR install the package with `pip install -e .` and run `python -m bot.main` from anywhere.
- Update README run command accordingly.

**Commit**: `fix: correctness pass (init_db, limit orders, async http, margin tiers, UTC)`.

---

## Phase 2 — Data model migration

### 2.1 New SQLAlchemy models

**File**: `src/database/models.py` — replace entirely.

```python
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, UTC

Base = declarative_base()

def _now(): return datetime.now(UTC)

class AppSettings(Base):
    __tablename__ = "app_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(Float, nullable=False)

class DVOLHistory(Base):
    __tablename__ = "dvol_history"
    id = Column(Integer, primary_key=True)
    date = Column(DateTime, unique=True, nullable=False)
    dvol = Column(Float, nullable=False)

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)   # "AIRS-2026-05"
    status = Column(String, default="OPEN")              # OPEN | CLOSED
    created_at = Column(DateTime, default=_now)
    closed_at = Column(DateTime, nullable=True)
    realized_pnl_btc = Column(Float, default=0.0)       # rolled up from legs
    legs = relationship("Leg", back_populates="campaign", cascade="all, delete-orphan")

class Leg(Base):
    __tablename__ = "legs"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    instrument_name = Column(String, unique=True, nullable=False)
    role = Column(String, nullable=False)                # yield_call | yield_put | crash_hedge | moon_hedge
    opened_at = Column(DateTime, default=_now)
    closed_at = Column(DateTime, nullable=True)
    realized_pnl_btc = Column(Float, default=0.0)       # written on close
    campaign = relationship("Campaign", back_populates="legs")
```

**Role constants** live in a small module:

**File**: `src/database/roles.py` (new).
```python
YIELD_CALL  = "yield_call"   # A
YIELD_PUT   = "yield_put"    # B
CRASH_HEDGE = "crash_hedge"  # C
MOON_HEDGE  = "moon_hedge"   # D

ROLE_LETTERS = {"a": YIELD_CALL, "b": YIELD_PUT, "c": CRASH_HEDGE, "d": MOON_HEDGE}
ROLES = set(ROLE_LETTERS.values())

def is_short_side(role): return role in (YIELD_CALL, YIELD_PUT)
def is_long_side(role):  return role in (CRASH_HEDGE, MOON_HEDGE)
def is_put_spread(role): return role in (YIELD_PUT, CRASH_HEDGE)   # B, C
def is_call_spread(role):return role in (YIELD_CALL, MOON_HEDGE)   # A, D
```

### 2.2 Destructive migration
Testnet only; no need for Alembic. In README, document the one-time step:
```
rm data.db
python -c "from database.session import init_db; init_db()"
python src/database/ingest_dvol.py "DERIBIT_DVOL, 1D_f4084.csv"
```

### 2.3 New query layer
**File**: `src/database/queries.py` — replace entirely.

Required functions (keep the signatures stable so analyzer/bot can import):

Settings/IV (unchanged from current):
- `get_iv_rank_30d(current_dvol: float | None) -> dict`
- `get_initial_btc_equity() -> float`
- `set_initial_btc_equity(value: float)`   **(new — so user can update baseline via command)**
- `get_morning_push_chat_id() -> int | None`
- `set_morning_push_chat_id(chat_id: int)`

Campaign management:
- `create_campaign(name: str) -> Campaign`
- `get_campaign(name: str) -> Campaign | None`
- `list_open_campaigns() -> list[Campaign]`
- `close_campaign(name: str)`  — marks status=CLOSED, closed_at=now

Leg tagging:
- `tag_leg(campaign_name: str, instrument_name: str, role: str) -> Leg` — auto-creates campaign if missing; upserts leg (moves between campaigns if re-tagged).
- `untag_leg(instrument_name: str) -> bool`
- `get_leg(instrument_name: str) -> Leg | None` — returns leg with campaign name + role
- `close_leg(instrument_name: str, realized_pnl_btc: float)` — sets `closed_at`, writes `realized_pnl_btc` to leg AND increments the parent campaign's `realized_pnl_btc`. Does NOT delete the leg (keeps history).
- `list_legs_by_role(role: str) -> list[Leg]`  (across all campaigns; handy for invariants)
- `list_legs_for_campaign(name: str) -> list[Leg]`

All functions use short-lived sessions (`with SessionLocal() as db:`).

### 2.4 Bot commands for the new model

**File**: `src/bot/main.py` — rewrite `group_cmd`, `ungroup_cmd`, and register new handlers.

New commands:

- `/campaign_new <name>` — creates a campaign explicitly.
- `/campaign_list` — lists open campaigns with leg counts and aggregate floating+realized PnL.
- `/tag <role> <campaign_name> <instrument>` — role accepts `a|b|c|d` or full name.
  - Auto-creates the campaign if it doesn't exist (convenience).
  - Example: `/tag c AIRS-2026-05 BTC-30MAY26-60000-P`
- `/untag <instrument>` — removes a leg from its campaign (retains Deribit position; just removes local metadata).
- `/legs` — lists all tagged legs grouped by campaign, with role.

Remove `/group` and `/ungroup` entirely (testnet only, no back-compat burden).

---

## Phase 3 — Analyzer rewrite

**File**: `src/analyzer/logic.py` — substantial rewrite. Keep the class name `AIRSAnalyzer` so other modules still import it.

### 3.1 Constructor
```python
def __init__(self, spot_price, positions, account_summary, iv_data, initial_equity):
    self.spot = spot_price
    self.positions = [p for p in positions if p.get("size", 0) != 0]  # drop ghosts upfront
    self.account = account_summary or {}
    self.iv = iv_data or {...}
    self.initial_equity = initial_equity
    self._leg_meta = {}  # instrument -> (campaign_name, role) from DB, populated lazily
```

### 3.2 Three public methods

```python
def portfolio_summary(self) -> dict
def campaign_summaries(self) -> list[dict]
def position_directives(self) -> list[dict]
```

**`portfolio_summary()`** returns:
```python
{
  "spot": ..., "equity_btc": ..., "initial_margin": ..., "maint_margin": ...,
  "margin_utilization_pct": ..., "margin_status": "Safe|Warning|Breach|Critical",
  "satoshi_growth_pct": ...,
  "iv_rank": ..., "dvol": ..., "iv_range": (min, max),
  "by_role": {
      "yield_call":  {"abs_size": 0.75, "net_delta": -0.08, "net_gamma": ...,
                      "avg_credit_btc": 0.045, "floating_pnl": ...},
      "yield_put":   {...},
      "crash_hedge": {...},
      "moon_hedge":  {...},
      "untagged":    {...}      # positions not tagged — always surface so user tags them
  },
  "invariants": {
      "put_ratio_long_short":  {"value": 3.2, "target": 3.0, "ok": True},
      "call_floor_vs_equity":  {"value": 0.75, "cap": 1.0,   "ok": True},
      "convexity_score":       {"value": 1.8, "min_healthy": 1.0, "ok": True},
      "margin_utilization":    {"value": 22.1, "cap": 25.0, "ok": True},
  },
  "net_delta": ..., "net_gamma": ..., "net_credit_btc": ...
}
```

**`campaign_summaries()`** returns one dict per open campaign:
```python
{
  "name": "AIRS-2026-05",
  "status": "OPEN",
  "legs": [
     {"instrument": ..., "role": "yield_put", "size": -0.10, "delta": ...,
      "floating_pnl_btc": ..., "realized_pnl_btc": ..., "closed": False}, ...
  ],
  "put_spread":  {"legs": [B, C], "floating_pnl_btc": ..., "realized_pnl_btc": ..., "net_debit_btc": ...},
  "call_spread": {"legs": [A, D], "floating_pnl_btc": ..., "realized_pnl_btc": ..., "net_debit_btc": ...},
  "total_floating_pnl_btc": ...,
  "total_realized_pnl_btc": ...,
  "total_pnl_btc": ...,
}
```

**`position_directives()`** — per open position:
```python
{
  "instrument": ..., "role": ..., "campaign": ...,
  "status": "HOLD" | "ROLL" | "CLOSE",
  "directive": "<human text>",
  "metrics": "Δ: ... | DTE: ... | PnL: ... BTC",
  # raw fields for keyboard builder in bot:
  "size": ..., "delta": ..., "gamma": ..., "avg_price": ..., "pnl": ...
}
```

### 3.3 Hybrid short-put directive

```python
def _directive(self, pos, margin_util_pct):
    parsed = self._parse_instrument(pos["instrument_name"])
    strike, opt_type, dte = parsed["strike"], parsed["type"], parsed["dte"]
    size = pos["size"]
    raw_delta = pos.get("delta", 0)
    contract_delta = raw_delta / size if size else 0

    # Universal expiry rule
    if dte <= 7:
        return "HOLD", "Expiring ≤ 7 days. Let expire or close if profitable."

    # Long hedges
    if size > 0:
        return "HOLD", "Long hedge active."

    # Short call
    if size < 0 and opt_type == "C":
        if self.spot >= strike or abs(contract_delta) >= 0.50:
            return "ROLL", f"Short call breached (Δ={abs(contract_delta):.2f}). Roll up and out."
        return "HOLD", "Short call OTM. Harvesting theta."

    # Short put — hybrid rule
    if size < 0 and opt_type == "P":
        breached = self.spot <= strike
        deep_itm = abs(contract_delta) >= 0.50
        stressed = margin_util_pct > 20.0

        if breached and stressed:
            return "ROLL", "Short put breached AND margin > 20%. Defensive roll down-and-out."
        if breached:
            return "HOLD", "Blueprint: spot below short put — long C in payout zone. Do nothing."
        if deep_itm:
            return "ROLL", f"Short put Δ={abs(contract_delta):.2f} ≥ 0.50. Roll down and out."
        return "HOLD", "Short put OTM. Harvesting theta."

    return "HOLD", "Monitoring."
```

### 3.4 Report renderer

`generate_report() -> str` produces the Markdown text. Structure:

```
📊 *AIRY Morning Briefing*
Spot: $XX,XXX | IV Rank: XX% (DVOL YY.Y, 30d range A–B)
Equity: X.XXXX BTC | Margin: XX.X% [Safe]
Satoshi Growth: +X.XX% (Baseline: 1.0 BTC)

*Portfolio Invariants*
  Put ratio C:B       = 3.2x ✓ (target ≥ 3.0)
  Call floor A/Eq     = 0.75 ✓ (cap 1.0)
  Convexity Lγ/Sγ     = 1.8x ✓
  Margin utilisation  = 22.1% ✓ (cap 25%)

*By Role*
  A Yield Call   Short 0.75 | Δ -0.08 | Credit 0.030 BTC | PnL +0.012
  B Yield Put    Short 0.30 | Δ +0.06 | Credit 0.015 BTC | PnL +0.005
  C Crash Hedge  Long  0.90 | Δ -0.04 | Debit  0.012 BTC | PnL -0.003
  D Moon Hedge   Long  1.50 | Δ +0.05 | Debit  0.008 BTC | PnL -0.002
  (Untagged 2 positions — /tag them to include in campaign PnL)

*Open Campaigns*
  AIRS-2026-05    Total PnL: +0.0023 BTC
    Put spread (B+C):  +0.0015
    Call spread (A+D): +0.0008
  AIRS-2026-06    Total PnL: -0.0004 BTC
    ...

*Actions*
  BTC-...-C  [HOLD]  Short call OTM. Harvesting theta.
  BTC-...-P  [ROLL]  Short put Δ=0.62 ≥ 0.50. Roll down and out.
```

Keep the `get_report_data()` convenience method returning `{"text": ..., "directives": ...}` so the bot's keyboard builder still works.

**Commit**: `refactor: role-based analyzer with campaign/spread aggregation`.

---

## Phase 4 — Sizing & IV gate

### 4.1 Sizing Option A
**File**: `src/bot/main.py:301-343` (`suggest_cmd`).

Constants (top of file):
```python
N_CAMPAIGNS   = 3
DERIBIT_MIN   = 0.1    # minimum option amount on Deribit
LEG_RATIOS    = {"a": 0.5, "b": 0.2, "c": 0.6, "d": 1.0}
```

Sizing logic:
```python
equity = summary.get("equity", 0)
scale  = round(equity / N_CAMPAIGNS, 2)

sizes = {k: max(DERIBIT_MIN, round(scale * r, 1)) for k, r in LEG_RATIOS.items()}

# Warn if equity too small to run blueprint ratios cleanly
min_viable_equity = DERIBIT_MIN * N_CAMPAIGNS / min(LEG_RATIOS.values())  # B is smallest
if equity < min_viable_equity:
    warning = (f"⚠️ Equity {equity:.4f} BTC is below the minimum to run "
               f"{N_CAMPAIGNS} concurrent campaigns at blueprint ratios. "
               f"Sizes have been floored at Deribit's 0.1 BTC minimum.")
```

### 4.2 IV gate on `/suggest`
Before computing suggestions, check IV rank:
```python
iv = get_iv_rank_30d(await deribit_client.get_dvol())
if iv["rank"] < 20:
    await update.message.reply_text(
        f"⛔ *IV Rank {iv['rank']}% — too low to sell yield.*\n"
        "Blueprint Pillar 4: 'Only sell options when IV is high.'\n"
        "Wait for IV > 30, or buy hedges only (Legs C/D).",
        parse_mode="Markdown"
    )
    return
low_iv_warning = None
if iv["rank"] < 30:
    low_iv_warning = (f"⚠️ IV Rank {iv['rank']}% is below 30 — yield legs may be "
                      "underpriced. Proceed with caution.")
```

Prepend `low_iv_warning` to the report if set.

### 4.3 AIRS init button — auto-assign campaign + roles
**File**: `src/bot/main.py:352-395` (`button_handler`, `init_airs` branch).

Replace the timestamp-based trade_id:
```python
# Infer campaign name from the DTE target (35 days ahead)
target_expiry = datetime.now(UTC) + timedelta(days=35)
campaign_name = f"AIRS-{target_expiry.strftime('%Y-%m')}"
create_campaign(campaign_name)  # idempotent: returns existing if present
```

After each successful leg order, tag with the right role:
```python
ROLE_BY_LEG_KEY = {
    "leg_a": YIELD_CALL, "leg_b": YIELD_PUT,
    "leg_c": CRASH_HEDGE, "leg_d": MOON_HEDGE,
}
# in the results loop:
if r["status"] == "✅":
    tag_leg(campaign_name, r["instr"], ROLE_BY_LEG_KEY[leg_key])
```

**Commit**: `feat: sizing Option A, IV gate, auto-tagging on AIRS init`.

---

## Phase 5 — Realized PnL write-through

### 5.1 Every close path must capture PnL

Three call sites close positions:
1. `/close <instrument>` — `main.py:277-299` (post-Phase-1)
2. Button `close:<instrument>` — `main.py:440-462`
3. Button `take_free` — `main.py:398-427` (loops over all short legs)

For each, the flow is:
```python
# read floating PnL before close
positions = await deribit_client.get_open_positions()
pos = next((p for p in positions if p["instrument_name"] == instr), None)
pnl_before = pos.get("floating_profit_loss", 0.0) if pos else 0.0

# execute close
res = await deribit_client.buy(...) or .sell(...)

# write through
close_leg(instr, realized_pnl_btc=pnl_before)
```

**Approximation caveat**: This uses floating PnL at the moment before the close. For exact realized PnL we'd need to fetch the fill trade via `private/get_user_trades_by_instrument` and compare against the leg's average entry price. Accept the approximation on testnet; document as TODO for production.

### 5.2 `close_campaign` auto-close
Inside `close_leg`, after writing the leg:
```python
if all(l.closed_at is not None for l in campaign.legs):
    campaign.status = "CLOSED"
    campaign.closed_at = _now()
```

**Commit**: `feat: realized PnL write-through on close`.

---

## Phase 6 — Daily DVOL ingestion

### 6.1 Job registration
**File**: `src/bot/main.py`, `main()`:
```python
from datetime import time as dtime
job_queue.run_daily(ingest_yesterday_dvol, time=dtime(0, 30), name="dvol_ingest")
```

### 6.2 Handler
**File**: `src/database/ingest_dvol.py` — add an async handler alongside the existing CSV importer.

```python
async def ingest_yesterday_dvol(context):
    """Runs daily at 00:30 UTC. Fetches Deribit mainnet DVOL for the last 24h
    and upserts the close value keyed by date."""
    import httpx
    from datetime import datetime, UTC, timedelta
    from database.session import SessionLocal
    from database.models import DVOLHistory

    end = int(datetime.now(UTC).timestamp() * 1000)
    start = end - 86400000
    url = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, params={
            "currency": "BTC", "resolution": "1D",
            "start_timestamp": start, "end_timestamp": end,
        })
        r.raise_for_status()
        data = r.json()["result"]["data"]
    if not data: return
    ts_ms, *_rest, close = data[-1][0], *data[-1][1:4], data[-1][4]
    dt = datetime.fromtimestamp(ts_ms/1000, UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    with SessionLocal() as db:
        existing = db.query(DVOLHistory).filter(DVOLHistory.date == dt).first()
        if existing:
            existing.dvol = close
        else:
            db.add(DVOLHistory(date=dt, dvol=close))
        db.commit()
```

### 6.3 Startup backfill
In `main()` before starting polling:
```python
from database.session import SessionLocal
from database.models import DVOLHistory
with SessionLocal() as db:
    latest = db.query(DVOLHistory).order_by(DVOLHistory.date.desc()).first()
    stale = latest is None or (datetime.now(UTC) - latest.date).days > 2
if stale:
    logger.info("DVOL stale or missing — running backfill")
    # Reuse ingest_yesterday_dvol in a loop for 30 days, or fetch 30d in one call
    await backfill_last_30d()
```

Implement `backfill_last_30d()` symmetric to the daily job, but pulling 30 daily candles in one call and upserting each.

**Commit**: `feat: daily DVOL ingestion with startup backfill`.

---

## Phase 7 — Tests

### 7.1 Setup
Create `tests/` at repo root. Add to `pyproject.toml`:
```toml
[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "freezegun"]
```

`tests/conftest.py`:
```python
import pytest
from database.session import engine, Base, SessionLocal

@pytest.fixture(autouse=True)
def _reset_db(monkeypatch, tmp_path):
    # Point engine to a tmp sqlite, create tables, tear down
    ...
```

### 7.2 Priority tests

**`tests/test_analyzer_directives.py`** — `AIRSAnalyzer._directive`:
- Short put below strike + margin 15% → `(HOLD, "…blueprint…")`
- Short put below strike + margin 22% → `(ROLL, "…Defensive…")`
- Short put above strike + Δ 0.55 → `(ROLL, "…0.55…")`
- Short put above strike + Δ 0.10 → `(HOLD, "Harvesting theta")`
- Short call spot > strike → `(ROLL, …)`
- Long hedge DTE 20 → `(HOLD, "Long hedge active")`
- Any position DTE 5 → `(HOLD, "Expiring ≤ 7")`

**`tests/test_analyzer_invariants.py`**:
- Portfolio with B=0.3, C=0.9 → `put_ratio = 3.0 ✓`
- Portfolio with B=0.3, C=0.6 → `put_ratio = 2.0 ✗`
- A=0.5, equity=1.0 → `call_floor_ok = True`; A=1.5, equity=1.0 → False
- Convexity with known long/short γ

**`tests/test_payoff.py`**:
- Long 1 BTC put, strike 50k, avg 0.05 BTC, spot 25k → payoff = `1.0 × ((50000-25000)/25000 - 0.05) = 0.95 BTC`.
- Short 0.5 call, strike 100k, avg 0.03, spot 150k → payoff = `-0.5 × ((150000-100000)/150000 - 0.03) = -0.5 × 0.303 = -0.152 BTC`.

**`tests/test_queries.py`**:
- `tag_leg` auto-creates campaign.
- `tag_leg` twice for same instrument → moves it (not duplicate).
- `close_leg` increments campaign realized.
- `close_leg` on last leg → closes campaign.

No integration tests against live Deribit. Keep tests offline.

**Commit**: `test: analyzer directives, invariants, payoff, and query layer`.

---

## Summary: commit order

| # | Commit                                                       | Depends on |
|---|--------------------------------------------------------------|------------|
| 1 | `fix: correctness pass`                                      | —          |
| 2 | `refactor: role-based data model + tag/untag commands`       | 1          |
| 3 | `refactor: analyzer aggregates by role, spread, campaign`    | 2          |
| 4 | `feat: sizing Option A, IV gate, auto-tag on AIRS init`      | 3          |
| 5 | `feat: realized PnL write-through on close`                  | 2          |
| 6 | `feat: daily DVOL ingestion + startup backfill`              | 1          |
| 7 | `test: analyzer + payoff + queries`                          | 3          |

After each commit, the bot should start cleanly (`python -m bot.main`) and at least `/status` should return a report without crashing on an empty portfolio.

## Notes for executor

- Don't preserve backward compatibility with the old `TradeGroup`/`TradeLeg` tables. Testnet only; destructive migration is fine.
- Keep functions small; prefer dataclasses or TypedDicts for analyzer return shapes if helpful.
- Don't introduce new deps beyond what's listed unless blocked.
- If a commit's scope balloons, split it — do not bundle.
- When in doubt about Deribit API field names, check the existing working code in `client.py` first; the field names there are correct for testnet.
- The `average_price` on Deribit positions for inverse options is BTC-denominated (user confirmed). Payoff math in `charts.py` is already correct.
