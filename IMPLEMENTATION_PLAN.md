# AIRY/AIRS Implementation Plan

This plan is self-contained. Any model should be able to execute it from this file alone.

## Confirmed decisions

1. **Short-put rule**: Hybrid. Default HOLD when spot breaches short put strike (antifragile — Leg C covers it). Only ROLL if `margin_util > 20%` OR `DTE <= 7` OR `abs(contract_delta) >= 0.50`.
2. **Data model**: Campaign → Spread → TradeLeg (3-tier role hierarchy). Each leg has role ∈ {yield_call(A), yield_put(B), crash_hedge(C), moon_hedge(D)}. A Campaign has exactly two Spreads: call_spread (A+D) and put_spread (B+C).
3. **Sizing**: `equity / 3` per campaign. Three time-staggered campaigns = ~1.0× exposure. Ratios: A=0.5×scale, B=0.2×scale, C=0.6×scale, D=1.0×scale.
4. **Campaign slots**: Harvest (20-45 DTE, target 35), Core (46-65 DTE, target 55), Far (66-90 DTE, target 75).
5. **Testnet only, BTC-denominated prices.**
6. **DVOL**: bot ingests daily and maintains; user may seed with CSV.

## Invariants the analyzer must expose (portfolio-wide)

- **Put ratio**: `C_size / B_size ≥ 3.0` — if below, crash protection is inadequate
- **Short call floor**: `A_size ≤ 1.0 × equity_btc` — unlimited upside liquidation risk otherwise
- **Margin utilisation**: `IM / equity ≤ 25%`
- **Convexity score**: `long_gamma / short_gamma` — higher is more antifragile

---

## Phase 1 — Correctness pass ✅ DONE

All items committed in `b3ed815`.

- ✅ 1.1 `init_db()` called at bot startup
- ✅ 1.2 `/close` uses limit orders (was market orders, rejected by Deribit)
- ✅ 1.3 Async httpx in `iv_cmd` and `fear_greed_cmd` (was blocking `requests`)
- ✅ 1.4 Shared `httpx.AsyncClient` in `DeribitClient` + `aclose()` on shutdown
- ✅ 1.5 Margin thresholds: WARN=25%, ALERT=40% (was a single 70% threshold)
- ✅ 1.6 Removed unreachable branch in `analyzer/logic.py`
- ✅ 1.7 `datetime.utcnow()` → `datetime.now(UTC)` across all files
- ✅ 1.8 Removed duplicate `load_dotenv`; one call inside `main()`
- ✅ 1.9 Proper package layout: `__init__.py` files, `pyproject.toml`, editable install

**Bonus fixes (not in original plan):**
- ✅ Credential lazy-load: `DeribitClient` reads env vars in `authenticate()` not `__init__()`, so module-level instantiation before `load_dotenv()` no longer causes silent `None` credentials
- ✅ `/status` double-fetch eliminated (was calling `_fetch_data()` twice, sending two error messages on failure)

---

## Phase 2 — Data model migration ✅ DONE

Committed in `b1b034e`.

### What was built (differs slightly from original plan — better)

Original plan had `Campaign → Leg` (flat). We built `Campaign → Spread → TradeLeg` (3-tier), which allows per-spread PnL aggregation.

**`src/database/models.py`** — new models:
- `Campaign`: name, status (OPEN/CLOSED), realized_pnl
- `Spread`: spread_type (call_spread/put_spread), campaign_id, realized_pnl
- `TradeLeg`: instrument_name, role, spread_id, realized_pnl
- Constants: `ROLE_ALIASES` (A/B/C/D → full names), `ROLE_TO_SPREAD`, `ROLE_LABELS`

**`src/database/session.py`** — `init_db()` drops legacy `trade_groups`/`trade_legs` tables on first run (destructive migration, testnet-safe).

**`src/database/queries.py`** — new functions:
- `tag_instrument(instrument, role, campaign)` — upserts leg, creates campaign/spread if needed
- `untag_instrument(instrument)` — removes tag
- `get_leg_info(instrument)` — returns role, spread_type, campaign_name, spread_id
- `get_all_open_campaigns()` — returns plain dicts (safe outside session)
- `get_realized_pnl_for_spread(spread_id)`
- `get_realized_pnl_for_campaign(campaign_name)`

**`src/bot/main.py`** — new commands:
- `/tag <instrument> <role> <campaign>` — roles accept A/B/C/D shorthand
- `/untag <instrument>`
- Removed `/group` and `/ungroup`

### 🔲 Still missing from Phase 2 (carry into Phase 3)

- 🔲 `/campaign_list` — list open campaigns with leg counts + PnL summary
- 🔲 `/legs` — list all tagged legs grouped by campaign
- 🔲 `close_leg(instrument, realized_pnl)` query — needed for Phase 5
- 🔲 `list_legs_for_campaign(name)` query

---

## Phase 3 — Analyzer + report rewrite ✅ PARTIAL

Committed in `b1b034e` (partial), `b419b90` (additional fixes).

### ✅ Done

- Role-based grouping in `generate_report()`: Campaign → Spread → Leg with per-spread PnL
- Antifragile short-put rule: spot ≤ strike is NOT a roll trigger; only `|Δ| ≥ 0.50` or `DTE ≤ 7`
- `get_leg_info()` called per-position to get campaign/spread/role metadata
- `ROLE_LABELS` display in report (A – Yield Call, etc.)

### 🔲 Not yet done

**3.1 Portfolio invariants section in report**

Add a `*Portfolio Invariants*` block to `generate_report()`:

```
*Portfolio Invariants*
  Put ratio C:B     = 3.2× ✅ (target ≥ 3.0)
  Call floor A/Eq   = 0.49 ✅ (cap 1.0)
  Convexity Lγ/Sγ   = 0.58× ⚠️ (below 1.0 — short gamma dominates)
  Margin util       = 1.9%  ✅ (cap 25%)
```

Compute from `analyze_positions()` directives: sum sizes/gamma by role across all campaigns.

**3.2 By-role summary block**

```
*By Role (Portfolio-wide)*
  A Yield Call   Short 0.5 | Δ -0.06 | Net credit +0.014 BTC | PnL +0.003
  B Yield Put    Short 0.2 | Δ +0.03 | Net credit +0.006 BTC | PnL -0.001
  C Crash Hedge  Long  0.6 | Δ -0.02 | Cost       -0.009 BTC | PnL -0.002
  D Moon Hedge   Long  1.0 | Δ +0.02 | Cost       -0.007 BTC | PnL -0.001
  ⚠️ Untagged: 2 positions — use /tag to include in campaign PnL
```

**3.3 `/campaign_list` and `/legs` commands**

```
/campaign_list
  MAY2026  [Slot 1 – Harvest, 39 DTE]  Floating: +0.002 BTC | Realized: 0.000 BTC
  JUN2026  [Slot 2 – Core,    59 DTE]  Floating: -0.001 BTC | Realized: 0.000 BTC

/legs
  MAY2026
    A – Yield Call:  BTC-29MAY26-92000-C  [HOLD]
    B – Yield Put:   BTC-29MAY26-62000-P  [HOLD]
    C – Crash Hedge: BTC-29MAY26-54000-P  [HOLD]
    D – Moon Hedge:  BTC-29MAY26-105000-C [HOLD]
```

---

## Phase 4 — Sizing, slot detection & market readiness ✅ PARTIAL

### ✅ Done

- ✅ Sizing: `equity / 3` per campaign (was `equity × 0.5`)
- ✅ Campaign slot detection: Harvest/Core/Far with DTE ranges; /suggest targets the first open slot
- ✅ Net premium breakdown per spread + total shown in /suggest before initiation
- ✅ Initiate button hidden if total structure is net debit
- ✅ Pre-flight spread check: bid/ask spread quality (✅/⚠️/❌) shown before any orders
- ✅ Sequential execution with rollback: if a leg fails, cancel/close prior legs
- ✅ Campaign name derived from instrument expiry (e.g. `MAY2026`), not current date

### 🔲 Not yet done — Market Readiness Score

**4.1 Gate function**

Replace the current bare IV check with a composite readiness assessment. Add to `main.py`:

```python
READINESS_RULES = [
    # (label, severity, condition_fn)
    ("IV Rank < 20% — options too cheap",    "block", lambda d: d["iv_rank"] < 20),
    ("IV Rank > 80% — extreme vol regime",   "warn",  lambda d: d["iv_rank"] > 80),
    ("IV < Realised Vol — edge inverted",    "warn",  lambda d: d.get("iv_premium", 1) < 0),
    ("Margin already > 20%",                 "block", lambda d: d["margin_pct"] > 20),
    ("Spot moved >10% in 24h",               "warn",  lambda d: abs(d.get("spot_change_24h", 0)) > 10),
    ("Fear & Greed < 15 or > 85",            "warn",  lambda d: not (15 <= d.get("fear_greed", 50) <= 85)),
]

def assess_readiness(ctx: dict) -> dict:
    blocks  = [r[0] for r in READINESS_RULES if r[1]=="block" and r[2](ctx)]
    warns   = [r[0] for r in READINESS_RULES if r[1]=="warn"  and r[2](ctx)]
    if blocks:
        verdict = "🛑 AVOID"
    elif len(warns) >= 2:
        verdict = "⏸️ WAIT"
    elif warns:
        verdict = "⚠️ CAUTION"
    else:
        verdict = "✅ GO"
    return {"verdict": verdict, "blocks": blocks, "warns": warns}
```

**4.2 Integration in `/suggest`**

- Run readiness check before searching for legs
- If `AVOID`: return early with explanation, no suggestion
- If `WAIT`: show suggestion but hide the Initiate button (user must use `/buy`/`/sell` manually)
- If `CAUTION`: show suggestion + warning block, button visible
- If `GO`: normal flow

Show slot status + readiness verdict at top of every `/suggest` response:

```
📊 Market Readiness: ⚠️ CAUTION
  • IV Rank > 80% — vol regime is extreme
  Slot 1 – Harvest: 👉 Suggesting now
```

**4.3 24h spot change and IV premium data**

- 24h spot change: compare current `get_btc_spot_price()` vs a stored value in `AppSettings` (updated at each bot run). Or fetch from CoinGecko `/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true`.
- IV premium: already computed in `/iv` — refactor into a shared helper `get_iv_premium()` so both `/iv` and `/suggest` can use it.

**4.4 Optional: calendar blackout dates**

Store as JSON in `AppSettings` (key: `blackout_dates`, value serialised separately). If today ±2 days of a blackout date, add "📅 Event risk window" to warns. Add `/blackout_add <YYYY-MM-DD> <label>` and `/blackout_list` commands.

---

## Phase 5 — Realized PnL + Harvest monitoring 🔲 NOT DONE

### 5.1 `close_leg()` query

**File**: `src/database/queries.py` — add:

```python
def close_leg(instrument_name: str, realized_pnl_btc: float) -> bool:
    """
    Record that a leg was closed. Writes realized PnL to the leg and propagates
    it up to the parent Spread and Campaign. Does NOT delete the leg (keeps history).
    """
    db = SessionLocal()
    try:
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if not leg:
            return False
        leg.realized_pnl += realized_pnl_btc
        leg.spread.realized_pnl += realized_pnl_btc
        leg.spread.campaign.realized_pnl += realized_pnl_btc
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        return False
    finally:
        db.close()
```

### 5.2 PnL capture on every close path

Three call sites in `main.py` close positions. Each must read floating PnL before closing and call `close_leg()`:

1. `/close <instrument>` — `close_cmd`
2. Button `close:<instrument>` — `button_handler`
3. Button `take_free` — all short legs in loop

Pattern:
```python
pos = next((p for p in positions if p["instrument_name"] == instr), None)
pnl_before = pos.get("floating_profit_loss", 0.0) if pos else 0.0
# ... execute close ...
close_leg(instr, pnl_before)
```

*Note*: This uses floating PnL at close time as an approximation. For exact realized PnL, fetch fills via `private/get_user_trades_by_instrument`. Accept approximation on testnet; document as TODO for production.

### 5.3 🆕 Harvest alerts — 50% profit target monitoring

Add to `_check_alerts()` background job (runs every 15 min):

```python
HARVEST_TARGET_PCT = 50.0  # close short when it has lost 50% of its initial credit value

for pos in positions:
    if pos["size"] >= 0:
        continue
    leg_info = get_leg_info(pos["instrument_name"])
    if leg_info.get("role") not in ("yield_call", "yield_put"):
        continue

    entry_credit = abs(pos.get("average_price", 0))      # BTC per contract at entry
    current_cost = abs(ticker_ask)                        # cost to buy back now
    if entry_credit <= 0:
        continue
    profit_pct = (entry_credit - current_cost) / entry_credit * 100

    if profit_pct >= HARVEST_TARGET_PCT:
        campaign = leg_info.get("campaign_name", "unknown")
        keyboard = [[
            InlineKeyboardButton("✂️ Close this leg",   callback_data=f"close:{instr}"),
            InlineKeyboardButton("✂️ Close whole spread", callback_data=f"close_spread:{spread_id}"),
            InlineKeyboardButton("🎈 Leave it",          callback_data="noop"),
        ]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"🎯 *Harvest Target Reached*\n"
                  f"{instr} [{role_label}] in *{campaign}*\n"
                  f"Profit: *{profit_pct:.0f}%* of credit collected\n"
                  f"Entry: {entry_credit:.5f} | Buyback: {current_cost:.5f} BTC"),
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
```

Add ticker fetch inside the loop (or batch-fetch all tickers at start of `_check_alerts`).

### 5.4 🆕 `close_spread` button callback

When user clicks "Close whole spread", close both legs in the spread (e.g. both B and C in the put spread). Re-uses close logic from button `close:` handler. Tags both with `close_leg()`.

---

## Phase 6 — Campaign lifecycle (post-harvest) 🆕 NEW

This phase handles the full lifecycle after shorts are harvested.

### 6.1 Campaign phase detection

Compute `phase` on the fly from live positions + DB tags (no `phase` column needed — avoids drift):

```python
def detect_campaign_phase(campaign: dict, live_positions: list) -> str:
    """
    INITIATED  — all 4 roles have live positions
    HARVESTED  — hedges active, at least one short role has no live position
    COASTING   — only hedges remain (both shorts closed)
    RECYCLED   — shorts re-opened against existing hedges (same campaign, new leg rows)
    EMPTY      — no live positions remain in this campaign
    """
    live = {p["instrument_name"] for p in live_positions if p.get("size", 0) != 0}
    tagged = {leg["instrument_name"]: leg["role"]
              for spread in campaign["spreads"]
              for leg in spread["legs"]}

    active_roles = {role for instr, role in tagged.items() if instr in live}
    short_roles  = {"yield_call", "yield_put"}
    hedge_roles  = {"crash_hedge", "moon_hedge"}

    if not active_roles:
        return "EMPTY"
    if short_roles <= active_roles and hedge_roles <= active_roles:
        return "INITIATED"
    if active_roles & short_roles and not (active_roles >= short_roles):
        return "HARVESTED"   # some shorts still open
    if not (active_roles & short_roles) and active_roles & hedge_roles:
        return "COASTING"    # all shorts gone, hedges remain
    return "INITIATED"       # fallback
```

Show phase in `/campaign_list` and `/status`:

```
*Campaign: MAY2026*  [Slot 1 – Harvest, 39 DTE] — 🎈 COASTING
  └ Call Spread: D (Moon Hedge) active — A closed at +50%
  └ Put Spread:  C (Crash Hedge) active — B closed at +52%
  └ Realized: +0.0043 BTC | Hedges floating: -0.0003 BTC
```

### 6.2 🆕 `/recycle <campaign>` command

When a campaign is COASTING (hedges remain, shorts gone), offer to re-short within the same campaign:

```
/recycle MAY2026

Campaign MAY2026 — COASTING
Remaining hedges:
  C (Crash Hedge): BTC-29MAY26-54000-P — 21 DTE, Δ -0.03, 0.0008 BTC (14% of entry)
  D (Moon Hedge):  BTC-29MAY26-105000-C — 21 DTE, Δ 0.02, 0.0005 BTC (11% of entry)

Recommendation: RECYCLE
  → New A' at 0.10Δ call for same expiry
  → New B' at 0.10Δ put for same expiry
  → Hedges already paid for — yield is near-pure edge

[🔄 Find new shorts]  [🎈 Keep coasting]  [❌ Close hedges]
```

Decision rules:

| Hedge DTE | Hedge residual value | Recommendation |
|-----------|---------------------|----------------|
| ≥ 21 days | > 20% of entry      | RECYCLE — add new shorts |
| ≥ 14 days | < 20% of entry      | ROLL — hedges decayed, start fresh campaign |
| 7–14 days | any                 | COAST — too close to expiry to add risk |
| < 7 days  | any                 | CLOSE — let expire or close for scraps |

### 6.3 🆕 Recycle execution flow

"Find new shorts" button triggers the same `/suggest` → pre-flight → confirm flow, but:
- Targets the existing campaign's expiry (not the slot's target DTE)
- Adds new `TradeLeg` rows to the **existing** Campaign (same `Campaign` row, new leg rows in existing Spread rows)
- Pre-flight checks that new shorts are safe relative to the hedge strikes (short call strike > hedge call strike; short put strike < hedge put strike)

### 6.4 🆕 Post-harvest decision helper in `/status` and morning push

When any campaign is COASTING, append to the morning report:

```
*⚠️ Action Required — COASTING campaigns:*
  MAY2026: hedges have 21 DTE. Consider /recycle MAY2026 or let expire.
```

When a campaign goes EMPTY (all positions closed), auto-mark it CLOSED in the DB via `close_campaign()`.

---

## Phase 7 — Daily DVOL ingestion 🔲 NOT DONE

*(was Phase 6 in original plan)*

### 7.1 Job registration
**File**: `src/bot/main.py`, `main()`:
```python
job_queue.run_daily(ingest_yesterday_dvol, time=time(0, 30), name="dvol_ingest")
```

### 7.2 Daily handler
**File**: `src/database/ingest_dvol.py`:

```python
async def ingest_yesterday_dvol(context):
    end   = int(datetime.now(UTC).timestamp() * 1000)
    start = end - 86400000
    url   = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, params={"currency":"BTC","resolution":"1D",
                                          "start_timestamp":start,"end_timestamp":end})
        r.raise_for_status()
        data = r.json()["result"]["data"]
    if not data: return
    ts_ms, close = data[-1][0], data[-1][4]
    dt = datetime.fromtimestamp(ts_ms/1000, UTC).replace(hour=0,minute=0,second=0,microsecond=0,tzinfo=None)
    db = SessionLocal()
    try:
        existing = db.query(DVOLHistory).filter(DVOLHistory.date == dt).first()
        if existing: existing.dvol = close
        else: db.add(DVOLHistory(date=dt, dvol=close))
        db.commit()
    finally:
        db.close()
```

### 7.3 Startup backfill

In `main()` before polling starts:
```python
latest = get_latest_dvol_date()
if latest is None or (datetime.now(UTC).replace(tzinfo=None) - latest).days > 2:
    await backfill_last_30d()
```

`backfill_last_30d()` fetches 30 daily candles in one call and upserts each.

---

## Phase 8 — Tests 🔲 NOT DONE

*(was Phase 7)*

### 8.1 Setup
```toml
[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "freezegun"]
```

### 8.2 Priority test cases

**Analyzer directives:**
- Short put below strike, margin 15% → HOLD (antifragile: do nothing)
- Short put below strike, margin 22% → ROLL (defensive)
- Short put above strike, Δ 0.55 → ROLL (deep ITM)
- Short put above strike, Δ 0.10 → HOLD (harvesting theta)
- Short call, spot > strike → ROLL
- Any position, DTE 5 → HOLD (expiring)

**Portfolio invariants:**
- B=0.3, C=0.9 → put_ratio=3.0 ✅; B=0.3, C=0.6 → put_ratio=2.0 ❌
- A=0.5, equity=1.0 → call_floor ok; A=1.5, equity=1.0 → breached
- Convexity with known long/short γ values

**Payoff (inverse options math):**
- Long 1 BTC put, strike 50k, avg 0.05, spot 25k → payoff = (50k-25k)/25k − 0.05 = 0.95 BTC
- Short 0.5 call, strike 100k, avg 0.03, spot 150k → payoff = −0.5 × ((150k-100k)/150k − 0.03) = −0.152 BTC

**Query layer:**
- `tag_instrument` auto-creates campaign + spread
- `tag_instrument` twice → moves leg (no duplicate)
- `close_leg` propagates PnL to spread and campaign
- Campaign auto-closes when all legs closed

**Campaign lifecycle:**
- COASTING detected when shorts closed, hedges remain
- EMPTY detected when all positions gone

---

## Current status

| Phase | Status | Notes |
|-------|--------|-------|
| 1 – Correctness | ✅ DONE | All items + 2 bonus fixes |
| 2 – Data model | ✅ DONE | `/tag`, `/untag`, Campaign/Spread/TradeLeg — missing `/campaign_list`, `/legs` |
| 3 – Analyzer | 🔶 PARTIAL | Role-based grouping done; invariants block + by-role summary not yet |
| 4 – Sizing & readiness | 🔶 PARTIAL | Sizing, slots, net premium, pre-flight, rollback done; Market Readiness Score not yet |
| 5 – Realized PnL + Harvest | 🔲 NOT DONE | New: harvest alerts at 50% profit |
| 6 – Campaign lifecycle | 🔲 NOT DONE | 🆕 New phase: phase detection, /recycle, COASTING handling |
| 7 – DVOL ingestion | 🔲 NOT DONE | |
| 8 – Tests | 🔲 NOT DONE | |

**Next up**: Phase 3 remaining (invariants block + `/campaign_list`) → Phase 4 remaining (Market Readiness Score) → Phase 5 (realized PnL + harvest alerts).
