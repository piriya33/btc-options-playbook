import os
import io
import logging
import asyncio
import httpx
import numpy as np
from datetime import time, datetime, UTC

from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

from deribit.client import DeribitClient
from analyzer.logic import AIRSAnalyzer, detect_campaign_phase, recycle_recommendation, _PHASE_EMOJI
from analyzer.charts import generate_payoff_chart
from database.models import ROLE_LABELS

from database.session import init_db
from database.queries import (
    get_iv_ranks,
    get_initial_btc_equity,
    set_initial_btc_equity,
    tag_instrument,
    untag_instrument,
    close_leg,
    get_leg_info,
    get_morning_push_chat_id,
    set_morning_push_chat_id,
    get_all_open_campaigns,
    list_legs_for_campaign,
    get_legs_for_spread,
    is_harvest_alerted,
    mark_harvest_alerted,
    clear_harvest_alerted,
    get_campaign_scale_pct,
    set_campaign_scale_pct,
)
from database.ingest_dvol import ensure_dvol_history, ingest_yesterday_dvol

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

deribit_client = DeribitClient(testnet=False)

# ── Constants ──────────────────────────────────────────────────────────────────
DELTA_DRIFT_LIMIT  = 0.15
MARGIN_WARN_PCT    = 25.0   # blueprint hard limit — log a warning
MARGIN_ALERT_PCT   = 40.0   # critical — send Telegram alert
SPREAD_WARN_PCT    = 25.0   # bid/ask spread as % of mid — warn user
SPREAD_BLOCK_PCT   = 80.0   # bid/ask spread — block execution (no real market)
HARVEST_TARGET_PCT = 50.0   # alert when short leg profit >= this % of entry credit
YIELD_BLOCK_PCT    = 5.0    # annualised net yield % — block initiation below this
YIELD_CAUTION_PCT  = 10.0   # annualised net yield % — caution below this
IV_PREMIUM_HALF_ENTRY = 2.0 # minimum IV premium (pts) to allow 50% entry when IV rank < 20%



def _assess_spread(ticker: dict) -> dict:
    """Return spread quality info for a ticker. Quality: ✅ good / ⚠️ wide / ❌ no market."""
    bid = ticker.get("best_bid_price") or 0.0
    ask = ticker.get("best_ask_price") or 0.0
    if bid <= 0 or ask <= 0:
        return {"bid": bid, "ask": ask, "mid": 0.0, "spread_pct": float("inf"), "quality": "❌"}
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100
    quality = "✅" if spread_pct < SPREAD_WARN_PCT else ("⚠️" if spread_pct < SPREAD_BLOCK_PCT else "❌")
    return {"bid": bid, "ask": ask, "mid": round(mid, 6), "spread_pct": round(spread_pct, 1), "quality": quality}


def _build_airs_trades(suggestion: dict) -> list:
    """Return the 4-leg trade list from a /suggest suggestion dict."""
    scale = suggestion.get("scale", 1.0)
    def _sz(ratio: float) -> float:
        return max(0.1, round(ratio * scale, 1))
    return [
        {"instr": suggestion["leg_d"], "amount": _sz(1.0), "side": "buy",  "role": "moon_hedge"},
        {"instr": suggestion["leg_c"], "amount": _sz(0.6), "side": "buy",  "role": "crash_hedge"},
        {"instr": suggestion["leg_a"], "amount": _sz(0.5), "side": "sell", "role": "yield_call"},
        {"instr": suggestion["leg_b"], "amount": _sz(0.2), "side": "sell", "role": "yield_put"},
    ]


def _get_airs_pairs(trades: list) -> list[list]:
    """Split the 4 AIRS legs into two spread pairs for sequential execution.

    Pair 1 — call spread: moon_hedge (D) + yield_call (A)
    Pair 2 — put spread:  crash_hedge (C) + yield_put (B)
    """
    by_role = {t["role"]: t for t in trades}
    return [
        [by_role["moon_hedge"], by_role["yield_call"]],
        [by_role["crash_hedge"], by_role["yield_put"]],
    ]


# ── Campaign slot definitions ──────────────────────────────────────────────────
# Three time-staggered slots; together they give ~1.0× equity exposure across 3 campaigns
CAMPAIGN_SLOTS = [
    {"number": 1, "label": "Harvest", "dte_min": 20, "dte_max": 45, "target_dte": 35},
    {"number": 2, "label": "Core",    "dte_min": 46, "dte_max": 65, "target_dte": 55},
    {"number": 3, "label": "Far",     "dte_min": 66, "dte_max": 90, "target_dte": 75},
]


def _instr_dte(instrument_name: str) -> int | None:
    """Parse DTE from a Deribit instrument name, e.g. BTC-29MAY26-92000-C."""
    parts = instrument_name.split("-")
    if len(parts) == 4:
        try:
            expiry = datetime.strptime(parts[1], "%d%b%y")
            return (expiry - datetime.now(UTC).replace(tzinfo=None)).days
        except Exception:
            pass
    return None


def _instr_campaign_name(instrument_name: str) -> str:
    """Derive a campaign name from an instrument expiry, e.g. 'MAY2026'."""
    parts = instrument_name.split("-")
    if len(parts) == 4:
        try:
            expiry = datetime.strptime(parts[1], "%d%b%y")
            return expiry.strftime("%b%Y").upper()
        except Exception:
            pass
    return f"AIRS-{datetime.now(UTC).strftime('%b%Y').upper()}"


def _slot_for_dte(dte: int | None) -> int | None:
    """Return which slot number (1/2/3) a given DTE falls into, or None."""
    if dte is None:
        return None
    for slot in CAMPAIGN_SLOTS:
        if slot["dte_min"] <= dte <= slot["dte_max"]:
            return slot["number"]
    return None


def _parse_slot_arg(args: list[str]) -> dict | None:
    """Resolve `/suggest <slot>` argument to a CAMPAIGN_SLOTS entry.

    Accepts slot label (harvest/core/far, case-insensitive) or slot number (1/2/3).
    Returns None when no arg given. Raises ValueError on unrecognised input.
    """
    if not args:
        return None
    token = args[0].strip().lower()
    for slot in CAMPAIGN_SLOTS:
        if token == slot["label"].lower() or token == str(slot["number"]):
            return slot
    raise ValueError(
        f"Unknown slot '{args[0]}'. Use: harvest | core | far  (or 1 | 2 | 3)."
    )


def _filled_slots(campaigns: list) -> dict:
    """
    Return {slot_number: {"name": ..., "dte": ..., "scale_pct": ...}} for each
    open campaign that maps to a recognised slot.
    scale_pct < 100 means the campaign was initiated at half size and can be topped up.
    """
    filled = {}
    for c in campaigns:
        dte = None
        for spread in c.get("spreads", []):
            for leg in spread.get("legs", []):
                dte = _instr_dte(leg["instrument_name"])
                if dte is not None:
                    break
            if dte is not None:
                break
        slot_num = _slot_for_dte(dte)
        if slot_num and slot_num not in filled:
            filled[slot_num] = {
                "name":      c["name"],
                "dte":       dte,
                "scale_pct": get_campaign_scale_pct(c["name"]),
            }
    return filled


# ── Market Readiness Score ─────────────────────────────────────────────────────

async def _market_readiness_score(
    iv_rank: float,
    margin_pct: float,
    iv_premium: float | None = None,
) -> dict:
    """
    Composite gate for /suggest.
    Levels: GO 🟢 / CAUTION 🟡 / AVOID 🔴

    iv_rank    — 252d (1-year) IV rank, strategic gate
    iv_premium — DVOL minus 30-day realised vol (pts); unlocks 50% entry at low rank

    Block → AVOID:   IV rank < 20 AND iv_premium < IV_PREMIUM_HALF_ENTRY, or margin > limit
    Half  → CAUTION: IV rank < 20 BUT iv_premium >= IV_PREMIUM_HALF_ENTRY (half-size allowed)
    Warn  → CAUTION: IV rank > 80 or spot 24h move > 10%
    """
    level = "GO"
    half_size = False
    reasons: list[str] = []
    spot_move_24h: float | None = None

    # Fetch 24-hour spot move from CoinGecko (non-fatal if unavailable)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
            )
        cg = r.json().get("bitcoin", {})
        spot_move_24h = abs(cg.get("usd_24h_change", 0.0))
    except Exception:
        pass  # non-fatal

    # ── Block / half conditions ───────────────────────────────────────────────
    if iv_rank < 20:
        if iv_premium is not None and iv_premium >= IV_PREMIUM_HALF_ENTRY:
            level = "CAUTION"
            half_size = True
            reasons.append(
                f"IV Rank {iv_rank:.1f}% < 20% but IV premium +{iv_premium:.1f} pts — "
                f"50% half-size entry available (top up when rank improves)"
            )
        else:
            level = "AVOID"
            reasons.append(
                f"IV Rank {iv_rank:.1f}% < 20% and IV premium "
                f"{'+' if (iv_premium or 0) >= 0 else ''}{iv_premium:.1f if iv_premium is not None else 'N/A'} pts "
                f"< {IV_PREMIUM_HALF_ENTRY:.0f} pts — vol too cheap to sell"
                if iv_premium is not None else
                f"IV Rank {iv_rank:.1f}% < 20% — volatility too cheap to sell"
            )

    if margin_pct > MARGIN_WARN_PCT:
        level = "AVOID"
        half_size = False
        reasons.append(f"Margin {margin_pct:.1f}% > {MARGIN_WARN_PCT:.0f}% — no capacity for new positions")

    # ── Warn conditions (only if not already blocked) ─────────────────────────
    if level != "AVOID":
        if iv_rank > 80:
            level = "CAUTION"
            reasons.append(f"IV Rank {iv_rank:.1f}% > 80% — elevated vol, risk of crush after entry")
        if spot_move_24h is not None and spot_move_24h > 10:
            level = "CAUTION"
            reasons.append(f"Spot moved {spot_move_24h:.1f}% in 24h — high intraday volatility")

    emoji = {"GO": "🟢", "CAUTION": "🟡", "AVOID": "🔴"}[level]
    return {
        "level":          level,
        "emoji":          emoji,
        "half_size":      half_size,
        "reasons":        reasons,
        "spot_move_24h":  spot_move_24h,
    }


# ── Shared report helper ───────────────────────────────────────────────────────
async def _fetch_data():
    await deribit_client.authenticate()
    spot_price      = await deribit_client.get_btc_spot_price()
    current_dvol    = await deribit_client.get_dvol()
    positions       = await deribit_client.get_open_positions()
    account_summary = await deribit_client.get_account_summary()
    iv_data         = get_iv_ranks(current_dvol)
    initial_equity  = get_initial_btc_equity()
    return spot_price, positions, account_summary, iv_data, initial_equity


async def _get_report():
    try:
        spot_price, positions, account_summary, iv_data, initial_equity = await _fetch_data()
        analyzer = AIRSAnalyzer(spot_price, positions, account_summary, iv_data, initial_equity)
        data = analyzer.get_report_data()

        keyboard = []
        for d in data["directives"]:
            instr = d.get("instrument")
            if not instr:
                continue
            label = instr.split('-')[-2]
            row = [InlineKeyboardButton(f"❌ Close {label}", callback_data=f"close:{instr}")]
            if d.get("status") == "ROLL":
                row.append(InlineKeyboardButton(f"🔄 Roll {label}", callback_data=f"roll:{instr}"))
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("🎯 Take Free Options", callback_data="take_free")])

        return data["text"], InlineKeyboardMarkup(keyboard) if keyboard else None
    except Exception as e:
        logger.error(f"Error generating report: {e}", exc_info=True)
        return f"⚠️ Error generating report: {e}", None


# ── Delta Drift / Margin background monitor ────────────────────────────────────
async def _check_alerts(context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_morning_push_chat_id()
    if not chat_id:
        return
    try:
        spot_price, positions, account_summary, iv_data, initial_equity = await _fetch_data()
        analyzer = AIRSAnalyzer(spot_price, positions, account_summary, iv_data, initial_equity)
        directives = analyzer.analyze_positions()
        margin_info = analyzer.analyze_margin()

        util = margin_info["margin_utilization_pct"]
        if util >= MARGIN_ALERT_PCT:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"🚨 *MARGIN CRITICAL*\n"
                      f"Utilisation: *{util:.1f}%* (limit: {MARGIN_WARN_PCT:.0f}%, critical: {MARGIN_ALERT_PCT:.0f}%)\n"
                      f"Reduce position size or add collateral immediately."),
                parse_mode='Markdown'
            )
        elif util >= MARGIN_WARN_PCT:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"⚠️ *MARGIN WARNING*\n"
                      f"Utilisation: *{util:.1f}%* has breached the blueprint limit of {MARGIN_WARN_PCT:.0f}%.\n"
                      f"Monitor closely."),
                parse_mode='Markdown'
            )

        # ── Delta drift per campaign ───────────────────────────────────────────
        grouped: dict = {}
        for d in directives:
            cam = d.get("campaign_name", "Untagged")
            grouped.setdefault(cam, []).append(d)

        for cam, items in grouped.items():
            total_delta = sum(d.get("raw_delta", 0) for d in items)
            if abs(total_delta) > DELTA_DRIFT_LIMIT:
                keyboard = [[
                    InlineKeyboardButton("⚖️ Adjust Hedge", callback_data=f"alert_hedge:{cam}"),
                    InlineKeyboardButton("❌ Close Yield",  callback_data=f"alert_close_yield:{cam}"),
                ]]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(f"⚠️ *DELTA DRIFT — {cam}*\n"
                          f"Combined Δ: *{round(total_delta, 3)}* (Limit: ±{DELTA_DRIFT_LIMIT})\n"
                          f"Consider adjusting your hedge legs."),
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        # ── Harvest alerts — 50% profit target ────────────────────────────────
        for pos in positions:
            if pos.get("size", 0) >= 0:
                continue  # only short legs
            instr = pos["instrument_name"]
            leg_info = get_leg_info(instr)
            if leg_info.get("role") not in ("yield_call", "yield_put"):
                continue
            if is_harvest_alerted(instr):
                continue  # already alerted (persisted across restarts)

            entry_credit = abs(pos.get("average_price", 0))
            floating_pnl = pos["size"] * (pos.get("mark_price", 0) - pos.get("average_price", 0))
            if entry_credit <= 0:
                continue

            # floating_pnl is positive when short is profitable (option decayed)
            profit_pct = (floating_pnl / (entry_credit * abs(pos["size"]))) * 100 if entry_credit > 0 else 0

            if profit_pct >= HARVEST_TARGET_PCT:
                mark_harvest_alerted(instr)
                role_label = ROLE_LABELS.get(leg_info.get("role", ""), "")
                campaign   = leg_info.get("campaign_name", "unknown")
                spread_id  = leg_info.get("spread_id")
                keyboard = [[
                    InlineKeyboardButton("✂️ Close this leg",    callback_data=f"close:{instr}"),
                    InlineKeyboardButton("✂️ Close whole spread", callback_data=f"close_spread:{spread_id}"),
                    InlineKeyboardButton("🎈 Leave it",           callback_data="noop"),
                ]]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(f"🎯 *Harvest Target Reached*\n"
                          f"*{instr}* [{role_label}] in *{campaign}*\n"
                          f"Profit: *{profit_pct:.0f}%* of entry credit\n"
                          f"  Entry credit: {entry_credit:.5f} BTC/contract\n"
                          f"  Floating PnL: +{round(floating_pnl, 5)} BTC\n"
                          f"  → Close now to lock in profit, or leave it running."),
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

    except Exception as e:
        logger.error(f"Alert check failed: {e}")


# ── Command handlers ───────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! I'm AIRY, your AIRS options playbook bot. "
        "Type /help for the full command list."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>📋 AIRY Command Reference</b>\n"
        "\n"
        "<b>── Market ──</b>\n"
        "/morning — Full AIRS morning briefing (positions + directives)\n"
        "/status — Live portfolio snapshot\n"
        "/suggest [slot] — Find next AIRS slot + pre-trade gates (auto-picks if no slot)\n"
        "  Force a slot: <code>/suggest harvest|core|far</code> (or <code>1|2|3</code>)\n"
        "/iv — Volatility intelligence (DVOL, IV rank 30d/1y, RV, premium)\n"
        "/fear_greed — Bitcoin Fear &amp; Greed index\n"
        "\n"
        "<b>── Trading ──</b>\n"
        "/buy &lt;instrument&gt; &lt;size&gt; — Place a market buy\n"
        "/sell &lt;instrument&gt; &lt;size&gt; — Place a market sell\n"
        "/close &lt;instrument&gt; — Close a position at market\n"
        "\n"
        "<b>── Campaigns ──</b>\n"
        "/campaigns — List all open campaigns with PnL\n"
        "/legs &lt;campaign&gt; — Show all tagged legs for a campaign\n"
        "/recycle &lt;campaign&gt; — Phase check + recycle/roll/coast/close recommendation\n"
        "\n"
        "<b>── Tagging ──</b>\n"
        "/tag &lt;instrument&gt; &lt;role&gt; &lt;campaign&gt; — Assign an instrument to a campaign\n"
        "  Roles: <code>A</code>=yield_call  <code>B</code>=yield_put  "
        "<code>C</code>=crash_hedge  <code>D</code>=moon_hedge\n"
        "  Example: <code>/tag BTC-27JUN25-100000-C A MAY2026</code>\n"
        "/untag &lt;instrument&gt; — Remove a tag\n"
        "\n"
        "<b>── Settings ──</b>\n"
        "/register — Subscribe to daily 08:00 UTC morning briefing\n"
        "/setbaseline [btc_equity] — View or update Satoshi Growth baseline\n"
    )
    await update.message.reply_html(text)


async def morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching Deribit data... ⏳")
    report, markup = await _get_report()
    await update.message.reply_text(report, parse_mode='Markdown', reply_markup=markup)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Generating live status... ⏳")
    try:
        spot_price, positions, account_summary, iv_data, initial_equity = await _fetch_data()
        analyzer = AIRSAnalyzer(spot_price, positions, account_summary, iv_data, initial_equity)
        data = analyzer.get_report_data()

        keyboard = []
        for d in data["directives"]:
            instr = d.get("instrument")
            if not instr:
                continue
            label = instr.split('-')[-2]
            row = [InlineKeyboardButton(f"❌ Close {label}", callback_data=f"close:{instr}")]
            if d.get("status") == "ROLL":
                row.append(InlineKeyboardButton(f"🔄 Roll {label}", callback_data=f"roll:{instr}"))
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("🎯 Take Free Options", callback_data="take_free")])
        markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await update.message.reply_text(data["text"], parse_mode='Markdown', reply_markup=markup)

        if positions:
            buf = generate_payoff_chart(positions, spot_price)
            await update.message.reply_photo(photo=buf, caption="📈 P&L at Expiry — Current Portfolio")
    except Exception as e:
        logger.error(f"Status error: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Error generating status: {e}")


async def iv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching volatility data... ⏳")
    try:
        dvol = await deribit_client.get_dvol()
        iv_data = get_iv_ranks(dvol)

        rv_url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=30&interval=daily"
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(rv_url)
        prices = [p[1] for p in r.json().get("prices", [])]

        realised_vol = None
        if len(prices) > 2:
            returns = np.diff(np.log(prices))
            realised_vol = float(np.std(returns) * np.sqrt(365) * 100)

        lines = [
            "📐 *Volatility Intelligence*",
            f"• DVOL (Implied): *{dvol:.2f}*",
            f"• IV Rank 30d:  *{iv_data['rank_30d']}%*  (range {iv_data['min_30d']} – {iv_data['max_30d']})",
            f"• IV Rank 1yr:  *{iv_data['rank_252d']}%*  (range {iv_data['min_252d']} – {iv_data['max_252d']})",
        ]
        if realised_vol:
            vol_premium = dvol - realised_vol
            if vol_premium < -3:
                verdict = "🟢 *Options CHEAP* — IV well below RV. Good time to BUY hedges."
            elif vol_premium < 3:
                verdict = f"⚪ *Options FAIR* — IV/RV spread within noise ({vol_premium:+.1f} pts). No strong vol signal."
            elif vol_premium < 8:
                verdict = f"🔴 *Options EXPENSIVE* — IV premium: +{vol_premium:.1f} pts. Good time to SELL yield."
            else:
                verdict = f"🔴 *Options RICH* — IV premium: +{vol_premium:.1f} pts. Strong sell signal, but watch for vol crush."
            lines.append(f"• 30d Realised Vol: *{realised_vol:.2f}*")
            lines.append(f"• IV Premium: *{vol_premium:+.2f}* pts")
            lines.append(f"\n{verdict}")

        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching IV data: {e}")


async def fear_greed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get("https://api.alternative.me/fng/?limit=1")
        data = r.json()["data"][0]
        value = int(data["value"])
        label = data["value_classification"]

        if value <= 25:
            emoji, bias = "😱", "Strong CRASH bias → lean heavier on Put hedges (Leg C)."
        elif value <= 45:
            emoji, bias = "😟", "Fear environment → options typically expensive, yield legs attractive."
        elif value <= 55:
            emoji, bias = "😐", "Neutral market → balanced AIRS sizing is appropriate."
        elif value <= 75:
            emoji, bias = "😄", "Greed building → lean heavier on Call hedges (Leg D)."
        else:
            emoji, bias = "🤑", "Extreme Greed → strong MOON bias. Protect upside with Call hedges."

        msg = (
            f"{emoji} *Fear & Greed Index: {value} — {label}*\n\n"
            f"📌 *AIRS Bias:* {bias}"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching Fear & Greed: {e}")


async def tag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /tag <instrument> <role> <campaign>
    role: A/B/C/D  or  yield_call/yield_put/crash_hedge/moon_hedge
    campaign: e.g. MAY-2026
    """
    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "Usage: /tag <instrument> <role> <campaign>\n"
            "Roles: A=yield_call  B=yield_put  C=crash_hedge  D=moon_hedge\n"
            "Example: /tag BTC-27JUN25-100000-C A MAY-2026"
        )
        return
    instrument, role_input, campaign = args
    ok, msg = tag_instrument(instrument, role_input, campaign)
    prefix = "✅" if ok else "❌"
    await update.message.reply_text(f"{prefix} {msg}")


async def untag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /untag <instrument>
    Removes the instrument from its campaign/spread.
    """
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /untag <instrument>")
        return
    ok, msg = untag_instrument(args[0])
    prefix = "✅" if ok else "❌"
    await update.message.reply_text(f"{prefix} {msg}")  # plain text — no parse_mode


async def campaigns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all open campaigns with spread structure and realized PnL."""
    campaigns = get_all_open_campaigns()
    if not campaigns:
        await update.message.reply_text(
            "No open campaigns. Use /suggest to find instruments, then /tag to assign them."
        )
        return
    lines = ["📋 *Open Campaigns*\n"]
    for c in campaigns:
        total_legs = sum(len(s["legs"]) for s in c["spreads"])
        lines.append(f"*{c['name']}*  ({total_legs} legs tagged)")
        lines.append(f"  Realized PnL: {c['realized_pnl']:+.5f} BTC")
        for s in c["spreads"]:
            label = "📞 Call Spread" if s["spread_type"] == "call_spread" else "📉 Put Spread"
            strikes = ", ".join(l["instrument_name"].split("-")[2] for l in s["legs"])
            lines.append(f"  {label}: {strikes or '—'}")
        lines.append("")
    lines.append("_Use /legs for full instrument details._")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def legs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all tagged legs grouped by campaign and spread."""
    campaigns = get_all_open_campaigns()
    if not campaigns:
        await update.message.reply_text(
            "No tagged legs. Use /tag <instrument> <role> <campaign> to assign positions."
        )
        return
    lines = ["🏷️ *Tagged Legs*\n"]
    for c in campaigns:
        lines.append(f"*{c['name']}*")
        for s in c["spreads"]:
            label = "📞 Call Spread (A+D)" if s["spread_type"] == "call_spread" else "📉 Put Spread (B+C)"
            lines.append(f"  {label}")
            for leg in s["legs"]:
                role_label = ROLE_LABELS.get(leg["role"], leg["role"])
                lines.append(f"    [{role_label}]  {leg['instrument_name']}")
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def recycle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /recycle <campaign>
    Shows hedge status for a COASTING campaign and recommends next action.
    """
    if not context.args:
        await update.message.reply_text("Usage: /recycle <campaign>\nExample: /recycle AIRS-260529-01")
        return

    campaign_name = context.args[0]
    campaigns = get_all_open_campaigns()
    campaign  = next((c for c in campaigns if c["name"] == campaign_name), None)
    if not campaign:
        await update.message.reply_text(f"❌ Campaign '{campaign_name}' not found. Use /campaigns to list open campaigns.")
        return

    await update.message.reply_text("Checking campaign status... ⏳")
    try:
        await deribit_client.authenticate()
        positions = await deribit_client.get_open_positions()
        pos_map   = {p["instrument_name"]: p for p in positions}

        phase = detect_campaign_phase(campaign, positions)
        ph_emoji = _PHASE_EMOJI.get(phase, "")

        if phase != "COASTING":
            await update.message.reply_text(
                f"{ph_emoji} *{campaign_name}* is *{phase}*, not COASTING.\n\n"
                "_Recycle is only available once all yield shorts have been harvested "
                "and at least one hedge leg remains._",
                parse_mode='Markdown'
            )
            return

        # ── Gather active hedge legs ──────────────────────────────────────────
        hedge_legs = []
        for spread in campaign["spreads"]:
            for leg in spread["legs"]:
                instr = leg["instrument_name"]
                pos   = pos_map.get(instr)
                if not pos or pos.get("size", 0) == 0:
                    continue
                if leg["role"] not in ("crash_hedge", "moon_hedge"):
                    continue

                parsed = instr.split("-")
                dte = None
                entry_cost = abs(pos.get("average_price", 0))
                if len(parsed) == 4:
                    try:
                        from datetime import datetime, UTC
                        expiry = datetime.strptime(parsed[1], "%d%b%y")
                        dte    = (expiry - datetime.now(UTC).replace(tzinfo=None)).days
                    except Exception:
                        pass

                # Current mid price
                try:
                    ticker  = await deribit_client.get_ticker(instr)
                    bid     = ticker.get("best_bid_price", 0) or 0
                    ask     = ticker.get("best_ask_price", 0) or 0
                    mid     = (bid + ask) / 2 if bid and ask else 0
                except Exception:
                    mid = 0

                residual_pct = (mid / entry_cost * 100) if entry_cost > 0 else 0
                hedge_legs.append({
                    "instr":        instr,
                    "role":         leg["role"],
                    "dte":          dte,
                    "entry_cost":   entry_cost,
                    "current_mid":  round(mid, 6),
                    "residual_pct": round(residual_pct, 1),
                    "expiry_str":   parsed[1] if len(parsed) == 4 else "?",
                })

        if not hedge_legs:
            await update.message.reply_text(
                f"🎈 *{campaign_name}* — no active hedge positions found.\n"
                "_Positions may have expired. Campaign can be considered complete._",
                parse_mode='Markdown'
            )
            return

        # ── Recommendation ────────────────────────────────────────────────────
        min_dte      = min((h["dte"] or 0) for h in hedge_legs)
        avg_residual = sum(h["residual_pct"] for h in hedge_legs) / len(hedge_legs)
        action, explanation = recycle_recommendation(min_dte, avg_residual)

        _ROLE_LETTER = {"crash_hedge": "C", "moon_hedge": "D"}
        action_emoji = {"RECYCLE": "🔄", "ROLL": "↩️", "COAST": "🎈", "CLOSE": "❌"}

        lines = [f"🎈 *{campaign_name}* — COASTING\n", "*Remaining hedges:*"]
        for h in hedge_legs:
            letter = _ROLE_LETTER.get(h["role"], "?")
            lines.append(
                f"  [{letter}] {h['instr'].split('-')[2]}-{h['instr'].split('-')[3]}  "
                f"DTE {h['dte']}  "
                f"Mid {h['current_mid']:.5f}  "
                f"({h['residual_pct']:.0f}% of entry)"
            )

        lines.append(
            f"\n*Recommendation: {action_emoji.get(action, '')} {action}*\n"
            f"  {explanation}"
        )

        # ── Buttons based on recommendation ───────────────────────────────────
        expiry_str = hedge_legs[0]["expiry_str"]
        context.user_data["recycle_campaign"] = {
            "name":       campaign_name,
            "expiry_str": expiry_str,
            "min_dte":    min_dte,
        }

        keyboard = []
        if action == "RECYCLE":
            lines.append("\n_New shorts will target the same expiry as your existing hedges._")
            keyboard.append([InlineKeyboardButton("🔄 Find new shorts", callback_data="recycle_find")])
        elif action == "ROLL":
            lines.append("\n_Use /suggest to open a fresh campaign in the next available slot._")
        elif action == "CLOSE":
            keyboard.append([InlineKeyboardButton("❌ Close all hedges", callback_data=f"recycle_close:{campaign_name}")])

        keyboard.append([
            InlineKeyboardButton("🎈 Keep coasting", callback_data="noop"),
        ])

        await update.message.reply_text(
            "\n".join(lines), parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
        )
    except Exception as e:
        logger.error(f"Recycle error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error: {e}")


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_morning_push_chat_id(update.effective_chat.id)
    await update.message.reply_text("✅ Registered! Daily Morning Briefing at 08:00 UTC.")


async def setbaseline_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /setbaseline <btc_equity>   — overwrite the Satoshi Growth baseline.
    /setbaseline                 — show current baseline + live equity, no write.
    """
    current = get_initial_btc_equity()

    if not context.args:
        try:
            _, _, account_summary, _, _ = await _fetch_data()
            live_equity = float(account_summary.get("equity", 0.0))
            msg = (
                f"Current baseline: `{current:.6f}` BTC\n"
                f"Live equity:      `{live_equity:.6f}` BTC\n\n"
                f"To update: `/setbaseline {live_equity:.6f}`"
            )
        except Exception as e:
            msg = (
                f"Current baseline: `{current:.6f}` BTC\n\n"
                f"Usage: `/setbaseline <btc_equity>`\n"
                f"(couldn't fetch live equity: {e})"
            )
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    try:
        new_value = float(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Value must be a number, e.g. `/setbaseline 0.12345`",
            parse_mode='Markdown',
        )
        return

    if new_value <= 0:
        await update.message.reply_text("❌ Baseline must be positive.")
        return

    set_initial_btc_equity(new_value)
    await update.message.reply_text(
        f"✅ Baseline updated: `{current:.6f}` → `{new_value:.6f}` BTC",
        parse_mode='Markdown',
    )


async def scheduled_morning_push(context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_morning_push_chat_id()
    if chat_id:
        report, markup = await _get_report()
        await context.bot.send_message(chat_id=chat_id, text=report, parse_mode='Markdown', reply_markup=markup)


async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    command = update.message.text.split()[0][1:]
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(f"Usage: /{command} <instrument> <amount> [price]")
        return
    try:
        instr  = args[0]
        amount = float(args[1])
        price  = float(args[2]) if len(args) > 2 else None

        await deribit_client.authenticate()
        if not price:
            ticker = await deribit_client.get_ticker(instr)
            price = ticker.get("best_ask_price" if command == "buy" else "best_bid_price") or ticker.get("last_price")

        await update.message.reply_text(f"Executing {command} {amount} {instr} @ {price}...")
        if command == 'buy':
            res = await deribit_client.buy(instr, amount, price, order_type="limit")
        else:
            res = await deribit_client.sell(instr, amount, price, order_type="limit")

        order = res.get("order", {})
        await update.message.reply_text(f"✅ Order placed! ID: {order.get('order_id')} | Status: {order.get('order_state')}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error executing trade: {e}")


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /close <instrument>")
        return
    instrument = args[0]
    await update.message.reply_text(f"Closing {instrument}...")
    try:
        await deribit_client.authenticate()
        positions = await deribit_client.get_open_positions()
        pos = next((p for p in positions if p["instrument_name"] == instrument), None)
        if not pos:
            await update.message.reply_text(f"❌ No open position for {instrument}")
            return
        size = pos["size"]
        average_price = pos.get("average_price", 0)
        ticker = await deribit_client.get_ticker(instrument)
        if size > 0:
            price = ticker.get("best_bid_price") or ticker.get("last_price")
            res = await deribit_client.sell(instrument, abs(size), price=price, order_type="limit")
        else:
            price = ticker.get("best_ask_price") or ticker.get("last_price")
            res = await deribit_client.buy(instrument, abs(size), price=price, order_type="limit")
        realized_pnl = size * ((price or 0) - average_price)
        order = res.get("order", {})

        # Record realized PnL and clear harvest alert flag
        pnl_msg = ""
        ok, msg = close_leg(instrument, realized_pnl)
        if ok:
            pnl_msg = f"\nRealized PnL: {realized_pnl:+.5f} BTC (recorded)"
            clear_harvest_alerted(instrument)
        elif "not tagged" not in msg:
            pnl_msg = f"\n⚠️ PnL not recorded: {msg}"

        await update.message.reply_text(f"✅ Closed! ID: {order.get('order_id')}{pnl_msg}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def suggest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        forced_slot = _parse_slot_arg(context.args)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    if forced_slot:
        await update.message.reply_text(
            f"Analysing Slot {forced_slot['number']} – {forced_slot['label']}... 🎯"
        )
    else:
        await update.message.reply_text("Analysing campaign slots and finding best instruments... 🔍")
    try:
        await deribit_client.authenticate()
        summary    = await deribit_client.get_account_summary()
        equity     = summary.get("equity", 0)
        margin_pct = (summary.get("initial_margin", 0) / equity * 100) if equity > 0 else 0

        # ── IV premium (DVOL − 30d realised vol) ─────────────────────────────
        current_dvol = await deribit_client.get_dvol()
        iv_data      = get_iv_ranks(current_dvol)
        iv_premium: float | None = None
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
                    params={"vs_currency": "usd", "days": 30, "interval": "daily"},
                )
            prices = [p[1] for p in r.json().get("prices", [])]
            if len(prices) > 2:
                returns = np.diff(np.log(prices))
                realised_vol = float(np.std(returns) * np.sqrt(365) * 100)
                iv_premium   = current_dvol - realised_vol
        except Exception:
            pass  # non-fatal — readiness will fall back to rank-only gate

        # ── Market Readiness Score ────────────────────────────────────────────
        readiness = await _market_readiness_score(iv_data["rank_252d"], margin_pct, iv_premium)

        if readiness["level"] == "AVOID":
            lines = [
                f"{readiness['emoji']} *Market Readiness: AVOID*",
                "",
                "New AIRS campaign blocked — fix the following:",
            ]
            for r in readiness["reasons"]:
                lines.append(f"  ❌ {r}")
            lines.append("\n_Check again with /suggest once conditions improve._")
            await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
            return

        # ── Determine which slot to fill (or top up) ──────────────────────────
        existing  = get_all_open_campaigns()
        filled    = _filled_slots(existing)

        if forced_slot:
            forced_info = filled.get(forced_slot["number"])
            if forced_info and forced_info["scale_pct"] >= 100:
                await update.message.reply_text(
                    f"✅ Slot {forced_slot['number']} ({forced_slot['label']}) is already at 100% "
                    f"— {forced_info['name']} ({forced_info['dte']} DTE). Nothing to do here."
                )
                return
            target_slot = forced_slot
            half_slot   = forced_slot if forced_info else None
        else:
            # Half-filled slot takes priority (top up before opening a new one)
            half_slot = next(
                (s for s in CAMPAIGN_SLOTS if s["number"] in filled and filled[s["number"]]["scale_pct"] < 100),
                None,
            )
            open_slot = next((s for s in CAMPAIGN_SLOTS if s["number"] not in filled), None)
            target_slot = half_slot or open_slot

        if target_slot is None:
            slot_lines = "\n".join(
                f"  Slot {s['number']} – {s['label']}: ✅ {filled[s['number']]['name']} "
                f"({filled[s['number']]['dte']} DTE)"
                for s in CAMPAIGN_SLOTS
            )
            await update.message.reply_text(
                f"✅ *All 3 campaign slots are fully active:*\n{slot_lines}\n\n"
                "_No new campaign needed. Monitor existing positions with /status._",
                parse_mode='Markdown'
            )
            return

        is_topup    = half_slot is not None and half_slot == target_slot
        scale_factor = 0.5 if readiness["half_size"] or is_topup else 1.0
        target_dte  = target_slot["target_dte"]

        def _fmt_credit(val: float) -> str:
            sign  = "+" if val >= 0 else ""
            emoji = "✅" if val >= 0 else "⚠️"
            return f"{sign}{round(val, 5)} BTC {emoji}"

        # ── Find (or load) legs ───────────────────────────────────────────────
        skipped_slots: list[str] = []
        if is_topup:
            # Re-use the existing instruments — just add the remaining 50%
            cam_info = filled[half_slot["number"]]
            cam_name = cam_info["name"]
            existing_cam = next((c for c in existing if c["name"] == cam_name), None)
            role_to_instr: dict[str, str] = {}
            for spread in existing_cam["spreads"]:
                for leg in spread["legs"]:
                    role_to_instr[leg["role"]] = leg["instrument_name"]

            async def _ticker_leg(instr: str) -> dict:
                t = await deribit_client.get_ticker(instr)
                g = t.get("greeks", {})
                return {
                    "instrument": instr,
                    "delta":  g.get("delta", 0),
                    "gamma":  g.get("gamma", 0),
                    "bid":    t.get("best_bid_price", 0) or 0,
                    "ask":    t.get("best_ask_price", 0) or 0,
                }

            leg_a = [await _ticker_leg(role_to_instr["yield_call"])]
            leg_b = [await _ticker_leg(role_to_instr["yield_put"])]
            leg_c = [await _ticker_leg(role_to_instr["crash_hedge"])]
            leg_d = [await _ticker_leg(role_to_instr["moon_hedge"])]
        else:
            # Try open slots in order; skip any that have no liquid expiry right now.
            # When a slot is forced, the candidate list is just that one slot.
            skipped_slots: list[str] = []
            if forced_slot:
                open_candidates = [forced_slot]
            else:
                open_candidates = [s for s in CAMPAIGN_SLOTS if s["number"] not in filled]
            leg_a = leg_b = leg_c = leg_d = []
            for candidate in open_candidates:
                la = await deribit_client.find_instruments_by_delta(
                    -0.10, candidate["target_dte"], 'C', candidate["dte_min"], candidate["dte_max"])
                lb = await deribit_client.find_instruments_by_delta(
                    -0.10, candidate["target_dte"], 'P', candidate["dte_min"], candidate["dte_max"])
                lc = await deribit_client.find_instruments_by_delta(
                    0.03,  candidate["target_dte"], 'P', candidate["dte_min"], candidate["dte_max"])
                ld = await deribit_client.find_instruments_by_delta(
                    0.02,  candidate["target_dte"], 'C', candidate["dte_min"], candidate["dte_max"])
                if la and lb and lc and ld:
                    leg_a, leg_b, leg_c, leg_d = la, lb, lc, ld
                    target_slot = candidate
                    target_dte  = candidate["target_dte"]
                    break
                skipped_slots.append(
                    f"Slot {candidate['number']} ({candidate['dte_min']}–{candidate['dte_max']} DTE)"
                )

            if not leg_a:
                if forced_slot:
                    await update.message.reply_text(
                        f"❌ No liquid instruments at Slot {forced_slot['number']} "
                        f"({forced_slot['label']}, {forced_slot['dte_min']}–{forced_slot['dte_max']} DTE).\n"
                        f"Deribit may have no active expiries in that range right now."
                    )
                else:
                    skipped = ", ".join(skipped_slots) or "all slots"
                    await update.message.reply_text(
                        f"❌ No liquid instruments found across any open slot ({skipped}).\n"
                        f"Deribit may have no active expiries in range right now — try again tomorrow.",
                    )
                return

        # ── Sizing ────────────────────────────────────────────────────────────
        # Goal: deploy each campaign so that running all 3 simultaneously stays
        # near but under the MARGIN_WARN_PCT (25%) initial-margin threshold.
        #
        # Deribit initial margin for short OTM options ≈ 15% of notional.
        # The two short legs per campaign are A (0.5× scale) and B (0.2× scale),
        # so their combined notional = 0.7 × scale BTC.
        #
        # Solve for scale that fills the margin budget:
        #   margin_per_campaign = 0.7 × scale × 0.15
        #   total_margin (3 campaigns) = 3 × 0.7 × scale × 0.15 = 0.315 × scale × equity⁻¹ ... wait:
        #
        #   3 × 0.7 × scale × 0.15 = 0.25 × equity
        #   scale = 0.25 × equity / (3 × 0.7 × 0.15)
        #   scale = 0.25 / 0.315 × equity
        #   scale ≈ 0.794 × equity   →   rounded to 0.79
        #
        # Worked example at 0.85 BTC equity, full-size:
        #   scale = round(0.85 × 0.79, 1) = 0.7
        #   D 1.0× = 0.7 BTC  C 0.6× = 0.4 BTC  A 0.5× = 0.4 BTC  B 0.2× = 0.1 BTC (min)
        #   margin used: (0.4 + 0.1) × 0.15 × 3 = 0.225 BTC = 26% ✅
        #
        # Worked example at 0.85 BTC equity, half-size (scale_factor = 0.5):
        #   scale = round(0.85 × 0.79 × 0.5, 1) = 0.3
        #   D=0.3  C=0.2  A=0.2  B=0.1 (min)
        #   margin used: (0.2 + 0.1) × 0.15 × 3 = 0.135 BTC = 16% ✅
        #
        # Note: 0.15 is the approximate Deribit margin rate for ~10-delta OTM options.
        # Actual margin will be slightly lower for deeply OTM strikes; monitor the
        # margin % in /status after deployment and adjust the 0.79 factor if needed.
        #
        # scale_factor = 0.5 for half entries or top-ups; 1.0 for full entries
        # min 0.1 per leg — Deribit's minimum contract size
        scale = round(equity * 0.79 * scale_factor, 1)
        sz_a  = max(0.1, round(0.5 * scale, 1))
        sz_b  = max(0.1, round(0.2 * scale, 1))
        sz_c  = max(0.1, round(0.6 * scale, 1))
        sz_d  = max(0.1, round(1.0 * scale, 1))

        # ── Net premium breakdown ─────────────────────────────────────────────
        call_credit  = sz_a * leg_a[0]['bid'] - sz_d * leg_d[0]['ask']
        put_credit   = sz_b * leg_b[0]['bid'] - sz_c * leg_c[0]['ask']
        total_credit = call_credit + put_credit

        # ── Slot status lines ─────────────────────────────────────────────────
        skipped_slot_nums = {int(s.split()[1]) for s in skipped_slots} if not is_topup else set()
        slot_lines = []
        for s in CAMPAIGN_SLOTS:
            if s["number"] in filled:
                info = filled[s["number"]]
                scale_tag = f" ⚡ {info['scale_pct']:.0f}%" if info["scale_pct"] < 100 else ""
                if s["number"] == target_slot["number"] and is_topup:
                    slot_lines.append(
                        f"  Slot {s['number']} – {s['label']}: 🔼 {info['name']} "
                        f"({info['dte']} DTE){scale_tag} → *topping up to 100%*"
                    )
                else:
                    slot_lines.append(
                        f"  Slot {s['number']} – {s['label']}: ✅ {info['name']} "
                        f"({info['dte']} DTE){scale_tag}"
                    )
            elif s["number"] == target_slot["number"]:
                slot_lines.append(f"  Slot {s['number']} – {s['label']}: 👉 *Suggesting now*")
            elif s["number"] in skipped_slot_nums:
                slot_lines.append(f"  Slot {s['number']} – {s['label']}: ⏭ _(skipped — no liquid expiry)_")
            else:
                slot_lines.append(f"  Slot {s['number']} – {s['label']}: ⬜ Empty")

        forced_tag = "  🎯 _forced_" if forced_slot else ""
        if is_topup:
            size_note = (
                f"⚖️ Top-up allocation: {scale} BTC  "
                f"(adding remaining 50% to {cam_info['name']})"
            )
            header = (
                f"🔼 *AIRS Top-Up — Slot {target_slot['number']}: {target_slot['label']}*{forced_tag}\n"
                f"Campaign: {cam_info['name']}  ({cam_info['dte']} DTE remaining)\n"
            )
        else:
            size_note = (
                f"⚖️ Campaign allocation: {scale} BTC  "
                f"({'50% half-size' if scale_factor == 0.5 else 'full size'}  equity ÷ 3"
                f"{' × 0.5' if scale_factor == 0.5 else ''})"
            )
            header = (
                f"🚀 *AIRS Suggestion — Slot {target_slot['number']}: {target_slot['label']}*{forced_tag}\n"
                f"Target: ~{target_dte} DTE\n"
            )

        report = [
            header,
            "*Campaign Slots:*",
            *slot_lines,
            f"\n💰 Equity: {round(equity, 4)} BTC",
            size_note,
            "\n*Yield Legs (Short):*",
            f"• A (Call): {leg_a[0]['instrument']} (Δ: {round(leg_a[0]['delta'], 2)})",
            f"  └ Size: {sz_a} | Bid: {leg_a[0]['bid']} | Ask: {leg_a[0]['ask']}",
            f"• B (Put):  {leg_b[0]['instrument']} (Δ: {round(leg_b[0]['delta'], 2)})",
            f"  └ Size: {sz_b} | Bid: {leg_b[0]['bid']} | Ask: {leg_b[0]['ask']}",
            "\n*Hedge Legs (Long):*",
            f"• C (Crash): {leg_c[0]['instrument']} (Δ: {round(leg_c[0]['delta'], 2)})",
            f"  └ Size: {sz_c} | Bid: {leg_c[0]['bid']} | Ask: {leg_c[0]['ask']}",
            f"• D (Moon):  {leg_d[0]['instrument']} (Δ: {round(leg_d[0]['delta'], 2)})",
            f"  └ Size: {sz_d} | Bid: {leg_d[0]['bid']} | Ask: {leg_d[0]['ask']}",
            "\n*Net Premium at Mid:*",
            f"  📞 Call spread (A−D): {_fmt_credit(call_credit)}",
            f"  📉 Put spread  (B−C): {_fmt_credit(put_credit)}",
            f"  ──────────────────────",
            f"  Total: {_fmt_credit(total_credit)}",
        ]

        # ── Structure checks ──────────────────────────────────────────────────
        ann_yield     = (total_credit / scale) * (365 / target_dte) * 100 if scale > 0 and total_credit > 0 else 0.0
        yield_hard_ok = ann_yield >= YIELD_BLOCK_PCT
        yield_caution = yield_hard_ok and ann_yield < YIELD_CAUTION_PCT
        yield_icon    = "✅" if ann_yield >= YIELD_CAUTION_PCT else ("⚠️" if yield_hard_ok else "❌")

        report.append("\n*Structure Checks:*")
        report.append(
            f"  Annual yield = {ann_yield:.1f}%  {yield_icon}  "
            f"(block < {YIELD_BLOCK_PCT:.0f}%  caution < {YIELD_CAUTION_PCT:.0f}%)"
        )

        # ── Readiness block ───────────────────────────────────────────────────
        report.append(f"\n*Market Readiness: {readiness['emoji']} {readiness['level']}*")
        if readiness["reasons"]:
            for r in readiness["reasons"]:
                report.append(f"  ⚠️ {r}")
        if readiness["spot_move_24h"] is not None:
            report.append(f"  24h spot move: {readiness['spot_move_24h']:.1f}%")
        if iv_premium is not None:
            report.append(f"  IV premium: {iv_premium:+.1f} pts (DVOL − 30d RV)")

        # ── Gate summary ──────────────────────────────────────────────────────
        hard_blocks = []
        if total_credit < 0:
            hard_blocks.append("Net debit — adjust strikes or sizing")
        if not yield_hard_ok:
            hard_blocks.append(
                f"Annualised yield {ann_yield:.1f}% < {YIELD_BLOCK_PCT:.0f}% — premium too thin for the risk"
            )

        can_initiate = len(hard_blocks) == 0

        if hard_blocks:
            report.append("\n❌ *Cannot initiate — fix the following:*")
            for b in hard_blocks:
                report.append(f"  • {b}")
        elif is_topup:
            report.append("\n🔼 *Top-up ready.* Adds remaining 50% to existing campaign legs.")
        elif readiness["half_size"]:
            report.append(
                f"\n⚡ *Half-size entry (50%).* IV rank is low but premium is attractive.\n"
                f"  Run /suggest again when IV rank > 20% to top up to full size."
            )
        elif yield_caution or readiness["level"] == "CAUTION":
            report.append("\n⚠️ *Proceed with caution.* Pre-flight check runs before any order is placed.")
        else:
            report.append("\n✅ *Structure looks good.* Pre-flight check runs before any order is placed.")

        # Store suggestion for the confirm step
        context.user_data["last_suggestion"] = {
            "leg_a":          leg_a[0]['instrument'],
            "leg_b":          leg_b[0]['instrument'],
            "leg_c":          leg_c[0]['instrument'],
            "leg_d":          leg_d[0]['instrument'],
            "scale":          scale,
            "scale_pct":      50 if scale_factor == 0.5 else 100,
            "slot":           target_slot,
            "topup":          is_topup,
            "topup_campaign": cam_name if is_topup else None,
        }

        btn_label = (
            f"🔼 Top-Up Slot {target_slot['number']} – {target_slot['label']}"
            if is_topup else
            f"🚀 Initiate Slot {target_slot['number']} – {target_slot['label']}"
        )
        keyboard = [[InlineKeyboardButton(btn_label, callback_data="init_airs")]] if can_initiate else None
        await update.message.reply_text(
            "\n".join(report), parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
        )
    except Exception as e:
        logger.error(f"Error finding suggestions: {e}")
        await update.message.reply_text(f"❌ Error finding suggestions: {e}")


async def _run_airs_execution(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute pending AIRS legs in two spread pairs.

    Pair 1 (call spread D+A) runs first, then pair 2 (put spread C+B).
    On any leg failure the user is offered Retry (fresh price) or Open Manually
    (skip auto-execution; bot shows the /tag command to record it later).
    No rollback of already-filled legs.
    """
    pending = context.user_data.get("pending_airs")
    if not pending:
        await query.edit_message_text("❌ Session expired. Run /suggest again.")
        return

    trades        = pending["trades"]
    exec_prices   = pending["exec_prices"]
    campaign_name = pending["campaign"]
    filled        = pending.setdefault("filled", [])
    skipped       = pending.setdefault("skipped", [])
    done_set      = {f["instr"] for f in filled} | {s["instr"] for s in skipped}

    try:
        await deribit_client.authenticate()
    except Exception as e:
        await query.edit_message_text(f"❌ Authentication failed: {e}")
        return

    pair_labels = ["📞 Call spread (D + A)", "📉 Put spread (C + B)"]
    for pair_idx, pair_trades in enumerate(_get_airs_pairs(trades)):
        for t in pair_trades:
            if t["instr"] in done_set:
                continue

            price = exec_prices.get(t["instr"])
            try:
                if t["side"] == "buy":
                    res = await deribit_client.buy(t["instr"], t["amount"], price=price, order_type="limit")
                else:
                    res = await deribit_client.sell(t["instr"], t["amount"], price=price, order_type="limit")

                order = res.get("order", {})
                filled.append({
                    "instr":    t["instr"],
                    "role":     t["role"],
                    "side":     t["side"],
                    "amount":   t["amount"],
                    "order_id": order.get("order_id"),
                    "state":    order.get("order_state", "open"),
                    "price":    order.get("average_price", price),
                })
                done_set.add(t["instr"])

            except Exception as e:
                pending["failed_leg"] = {
                    "instr":  t["instr"],
                    "role":   t["role"],
                    "side":   t["side"],
                    "amount": t["amount"],
                    "error":  str(e),
                }
                context.user_data["pending_airs"] = pending

                role_label = ROLE_LABELS.get(t["role"], t["role"])
                lines = [
                    f"❌ *Leg failed:* [{role_label}] `{t['instr']}`\n  └ {e}\n",
                    f"*{pair_labels[pair_idx]}* — in progress",
                ]
                if filled:
                    lines.append("\n*Already filled:*")
                    for f in filled:
                        rl = ROLE_LABELS.get(f["role"], f["role"])
                        lines.append(f"  ✅ [{rl}] {f['instr']}  (order {f['order_id']})")
                lines.append(
                    "\n_Retry fetches a fresh market price. "
                    "'Open Manually' skips auto-execution — open it yourself, then use /tag to record it._"
                )
                keyboard = [[
                    InlineKeyboardButton("🔄 Retry",         callback_data="airs_retry_leg"),
                    InlineKeyboardButton("✋ Open Manually", callback_data="airs_skip_leg"),
                    InlineKeyboardButton("❌ Abort",          callback_data="init_airs_cancel"),
                ]]
                await query.edit_message_text(
                    "\n".join(lines), parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return

    # ── All legs handled — tag and report ─────────────────────────────────────
    is_topup       = pending.get("topup", False)
    topup_campaign = pending.get("topup_campaign")
    scale_pct      = pending.get("scale_pct", 100)
    final_campaign = topup_campaign if is_topup else campaign_name

    summary = [
        f"🔼 *AIRS Top-Up — {topup_campaign}*\n" if is_topup else f"🚀 *AIRS Campaign ({campaign_name})*\n"
    ]
    for f in filled:
        role_label = ROLE_LABELS.get(f["role"], f["role"])
        summary.append(f"✅ *[{role_label}]* {f['instr']}\n  └ Price: {f['price']} | ID: {f['order_id']}")
        if not is_topup:
            tag_instrument(f["instr"], f["role"], campaign_name)

    set_campaign_scale_pct(final_campaign, 100.0 if is_topup else float(scale_pct))

    if skipped:
        summary.append("\n*Open manually (not auto-tagged):*")
        for s in skipped:
            rl = ROLE_LABELS.get(s["role"], s["role"])
            summary.append(
                f"✋ *[{rl}]* {s['instr']}\n"
                f"  └ `/tag {s['instr']} {s['role']} {final_campaign}`"
            )

    if is_topup:
        summary.append(f"\n✅ *{topup_campaign}* topped up to 100% — full campaign now active.")
    elif scale_pct < 100:
        summary.append(
            f"\n⚡ *Half-size campaign initiated ({scale_pct:.0f}%).*\n"
            f"  Run /suggest when IV rank > 20% to top up to full size."
        )
    summary.append("\n_Use /tag to re-assign legs or add to a different campaign._")
    await query.edit_message_text("\n".join(summary), parse_mode='Markdown')


# ── Button handler ─────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "init_airs":
        # ── Step 1: Pre-flight spread check ───────────────────────────────────
        suggestion = context.user_data.get("last_suggestion")
        if not suggestion:
            await query.edit_message_text("❌ Suggestion expired. Run /suggest again.")
            return

        await query.edit_message_text("🔍 Running pre-flight spread check...")
        try:
            await deribit_client.authenticate()
            trades = _build_airs_trades(suggestion)

            lines = ["📋 *Pre-flight Check*\n"]
            has_no_market = False
            has_wide = False
            tickers = {}
            exec_prices = {}

            for t in trades:
                ticker = await deribit_client.get_ticker(t["instr"])
                tickers[t["instr"]] = ticker
                s = _assess_spread(ticker)
                exec_price = (ticker.get("best_ask_price") if t["side"] == "buy"
                              else ticker.get("best_bid_price")) or ticker.get("last_price", 0)
                exec_prices[t["instr"]] = exec_price
                role_label = ROLE_LABELS.get(t["role"], t["role"])

                lines.append(
                    f"{s['quality']} *[{role_label}]* {t['instr']}\n"
                    f"  Bid: {s['bid']} | Ask: {s['ask']} | Spread: {s['spread_pct']}%\n"
                    f"  Exec: {exec_price} BTC × {t['amount']} contracts"
                )
                if s["quality"] == "❌":
                    has_no_market = True
                elif s["quality"] == "⚠️":
                    has_wide = True

            # Store the resolved trade plan for the confirm step
            context.user_data["pending_airs"] = {
                "trades":          trades,
                "exec_prices":     exec_prices,
                "campaign":        _instr_campaign_name(trades[0]["instr"]),
                "scale_pct":       suggestion.get("scale_pct", 100),
                "topup":           suggestion.get("topup", False),
                "topup_campaign":  suggestion.get("topup_campaign"),
            }

            if has_no_market:
                lines.append(
                    "\n❌ *One or more legs have no active market.*\n"
                    "_This is common on testnet. Try placing orders manually via /buy and /sell._"
                )
                keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="init_airs_cancel")]]
            else:
                if has_wide:
                    lines.append("\n⚠️ *Wide spreads detected — fills may be poor.*")
                else:
                    lines.append("\n✅ *Spreads look good.*")
                lines.append("_Legs execute in two pairs (call spread D+A first, then put spread C+B). If a leg fails you can retry or open it manually — no rollback._")
                keyboard = [[
                    InlineKeyboardButton("🚀 Confirm", callback_data="init_airs_confirm"),
                    InlineKeyboardButton("❌ Cancel",  callback_data="init_airs_cancel"),
                ]]

            await query.edit_message_text(
                "\n".join(lines), parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Pre-flight failed: {e}")
        return

    if data == "init_airs_confirm":
        # ── Step 2: Kick off pair-by-pair execution ────────────────────────────
        pending = context.user_data.get("pending_airs")
        if not pending:
            await query.edit_message_text("❌ Session expired. Run /suggest again.")
            return
        pending["filled"]  = []
        pending["skipped"] = []
        context.user_data["pending_airs"] = pending
        await query.edit_message_text("⏳ Executing call spread (D + A)...")
        await _run_airs_execution(query, context)
        return

    if data == "init_airs_cancel":
        context.user_data.pop("pending_airs", None)
        await query.edit_message_text("❌ Execution cancelled.")
        return

    if data == "airs_retry_leg":
        # ── Retry failed leg at a fresh market price ───────────────────────────
        pending = context.user_data.get("pending_airs")
        if not pending or "failed_leg" not in pending:
            await query.edit_message_text("❌ No pending retry. Run /suggest again.")
            return
        failed = pending["failed_leg"]
        try:
            await deribit_client.authenticate()
            ticker = await deribit_client.get_ticker(failed["instr"])
            fresh_price = (
                ticker.get("best_ask_price") if failed["side"] == "buy"
                else ticker.get("best_bid_price")
            ) or ticker.get("last_price", 0)
            pending["exec_prices"][failed["instr"]] = fresh_price
            del pending["failed_leg"]
            context.user_data["pending_airs"] = pending
        except Exception as e:
            await query.edit_message_text(f"❌ Could not fetch fresh price: {e}")
            return
        await query.edit_message_text("🔄 Retrying leg at fresh market price...")
        await _run_airs_execution(query, context)
        return

    if data == "airs_skip_leg":
        # ── Mark failed leg as manually opened; continue execution ────────────
        pending = context.user_data.get("pending_airs")
        if not pending or "failed_leg" not in pending:
            await query.edit_message_text("❌ No pending leg to skip. Run /suggest again.")
            return
        failed = pending["failed_leg"]
        pending.setdefault("skipped", []).append({
            "instr":  failed["instr"],
            "role":   failed["role"],
            "side":   failed["side"],
            "amount": failed["amount"],
        })
        del pending["failed_leg"]
        context.user_data["pending_airs"] = pending
        await query.edit_message_text("✋ Leg skipped — open it yourself, then /tag it. Continuing...")
        await _run_airs_execution(query, context)
        return

    if data == "take_free":
        await query.edit_message_text("⏳ Closing all short (yield) legs...")
        try:
            await deribit_client.authenticate()
            positions = await deribit_client.get_open_positions()
            short_legs = [p for p in positions if p.get("size", 0) < 0]
            if not short_legs:
                await query.edit_message_text("ℹ️ No short legs found to close.")
                return

            results = []
            for pos in short_legs:
                instr        = pos["instrument_name"]
                size         = pos["size"]
                average_price = pos.get("average_price", 0)
                try:
                    ticker = await deribit_client.get_ticker(instr)
                    price  = ticker.get("best_ask_price") or ticker.get("last_price")
                    res    = await deribit_client.buy(instr, abs(size), price=price, order_type="limit")
                    realized_pnl = size * ((price or 0) - average_price)
                    close_leg(instr, realized_pnl)
                    results.append(f"✅ Closed {instr}  PnL: {realized_pnl:+.5f} BTC")
                except Exception as e:
                    results.append(f"❌ {instr}: {e}")

            await query.edit_message_text(
                "🎯 *Take Free Options — Complete*\n\n" + "\n".join(results) +
                "\n\n_Long hedges retained as free options._",
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("roll:"):
        instrument = data.split(":", 1)[1]
        await query.edit_message_text(
            f"🔄 *Roll {instrument}*\n\nTo roll, close the existing leg then open a new one at the target 0.10 delta:\n"
            f"1. /close {instrument}\n2. /suggest → find new leg → initiate",
            parse_mode='Markdown'
        )
        return

    if data == "recycle_find":
        # ── Find new shorts for same expiry as existing hedges ────────────────
        recycle = context.user_data.get("recycle_campaign")
        if not recycle:
            await query.edit_message_text("❌ Session expired. Run /recycle again.")
            return

        expiry_str = recycle["expiry_str"]   # e.g. "29MAY26"
        min_dte    = recycle["min_dte"]
        cam_name   = recycle["name"]

        await query.edit_message_text(f"🔍 Finding 0.10Δ shorts for {expiry_str} expiry...")
        try:
            await deribit_client.authenticate()
            # Use ±5 DTE tolerance around the hedge expiry
            leg_a = await deribit_client.find_instruments_by_delta(-0.10, min_dte, 'C')
            leg_b = await deribit_client.find_instruments_by_delta(-0.10, min_dte, 'P')

            # Filter to same expiry
            leg_a = [l for l in leg_a if expiry_str in l["instrument"]] or leg_a
            leg_b = [l for l in leg_b if expiry_str in l["instrument"]] or leg_b

            if not leg_a or not leg_b:
                await query.edit_message_text(
                    f"❌ Could not find 0.10Δ instruments for {expiry_str}.\n"
                    "Try /buy and /tag manually."
                )
                return

            lines = [
                f"🔄 *New Shorts for {cam_name}* (expiry {expiry_str})\n",
                f"• A (Call): {leg_a[0]['instrument']}  Δ {leg_a[0]['delta']:+.3f}",
                f"  Bid {leg_a[0]['bid']}  Ask {leg_a[0]['ask']}",
                f"• B (Put):  {leg_b[0]['instrument']}  Δ {leg_b[0]['delta']:+.3f}",
                f"  Bid {leg_b[0]['bid']}  Ask {leg_b[0]['ask']}",
                "\n_Hedges already paid for — new premium is near-pure edge._",
                "_Use /buy + /tag to open these legs and add them to the campaign._",
            ]
            await query.edit_message_text("\n".join(lines), parse_mode='Markdown')
        except Exception as e:
            await query.edit_message_text(f"❌ Error finding shorts: {e}")
        return

    if data.startswith("recycle_close:"):
        cam_name = data.split(":", 1)[1]
        await query.edit_message_text(f"⏳ Closing all hedge legs in {cam_name}...")
        try:
            await deribit_client.authenticate()
            campaigns = get_all_open_campaigns()
            campaign  = next((c for c in campaigns if c["name"] == cam_name), None)
            if not campaign:
                await query.edit_message_text(f"❌ Campaign {cam_name} not found.")
                return

            positions = await deribit_client.get_open_positions()
            pos_map   = {p["instrument_name"]: p for p in positions}
            results   = []

            for spread in campaign["spreads"]:
                for leg in spread["legs"]:
                    instr = leg["instrument_name"]
                    pos   = pos_map.get(instr)
                    if not pos or pos.get("size", 0) == 0:
                        continue
                    size = pos["size"]
                    average_price = pos.get("average_price", 0)
                    try:
                        ticker = await deribit_client.get_ticker(instr)
                        if size > 0:
                            price = ticker.get("best_bid_price") or ticker.get("last_price")
                            await deribit_client.sell(instr, abs(size), price=price, order_type="limit")
                        else:
                            price = ticker.get("best_ask_price") or ticker.get("last_price")
                            await deribit_client.buy(instr, abs(size), price=price, order_type="limit")
                        realized_pnl = size * ((price or 0) - average_price)
                        close_leg(instr, realized_pnl)
                        results.append(f"✅ {instr}  PnL {realized_pnl:+.5f} BTC")
                    except Exception as e:
                        results.append(f"❌ {instr}: {e}")

            await query.edit_message_text(
                f"❌ *Hedges Closed — {cam_name}*\n\n" + "\n".join(results),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("close:"):
        instrument = data.split(":", 1)[1]
        await query.edit_message_text(f"⏳ Closing {instrument}...")
        try:
            await deribit_client.authenticate()
            positions = await deribit_client.get_open_positions()
            pos = next((p for p in positions if p["instrument_name"] == instrument), None)
            if not pos:
                await query.edit_message_text(f"❌ No open position for {instrument}")
                return
            size = pos["size"]
            average_price = pos.get("average_price", 0)
            ticker = await deribit_client.get_ticker(instrument)
            if size > 0:
                price = ticker.get("best_bid_price") or ticker.get("last_price")
                res = await deribit_client.sell(instrument, abs(size), price=price, order_type="limit")
            else:
                price = ticker.get("best_ask_price") or ticker.get("last_price")
                res = await deribit_client.buy(instrument, abs(size), price=price, order_type="limit")
            realized_pnl = size * ((price or 0) - average_price)
            order = res.get("order", {})

            pnl_msg = ""
            ok, msg = close_leg(instrument, realized_pnl)
            if ok:
                pnl_msg = f"\nRealized PnL: {realized_pnl:+.5f} BTC (recorded)"
                clear_harvest_alerted(instrument)

            await query.edit_message_text(
                f"✅ Closed {instrument}!\nOrder ID: {order.get('order_id')}{pnl_msg}"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error closing {instrument}: {e}")
        return

    if data.startswith("close_spread:"):
        spread_id = int(data.split(":", 1)[1])
        await query.edit_message_text(f"⏳ Closing all legs in spread {spread_id}...")
        try:
            await deribit_client.authenticate()
            legs     = get_legs_for_spread(spread_id)
            positions = await deribit_client.get_open_positions()
            pos_map  = {p["instrument_name"]: p for p in positions}

            results = []
            for leg in legs:
                instr = leg["instrument_name"]
                pos   = pos_map.get(instr)
                if not pos:
                    results.append(f"ℹ️ {instr}: no open position")
                    continue
                size = pos["size"]
                average_price = pos.get("average_price", 0)
                try:
                    ticker = await deribit_client.get_ticker(instr)
                    if size > 0:
                        price = ticker.get("best_bid_price") or ticker.get("last_price")
                        await deribit_client.sell(instr, abs(size), price=price, order_type="limit")
                    else:
                        price = ticker.get("best_ask_price") or ticker.get("last_price")
                        await deribit_client.buy(instr, abs(size), price=price, order_type="limit")
                    realized_pnl = size * ((price or 0) - average_price)
                    close_leg(instr, realized_pnl)
                    role_label = ROLE_LABELS.get(leg["role"], leg["role"])
                    results.append(f"✅ [{role_label}] {instr}  PnL: {realized_pnl:+.5f} BTC")
                except Exception as e:
                    results.append(f"❌ {instr}: {e}")

            await query.edit_message_text(
                "✂️ *Spread Closed*\n\n" + "\n".join(results),
                parse_mode='Markdown'
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error closing spread: {e}")
        return

    if data == "noop":
        # Harvest alert acknowledged — do nothing (alert already sent)
        await query.edit_message_text(
            query.message.text + "\n\n_Acknowledged — leaving it to run._",
            parse_mode='Markdown'
        )
        return

    if data.startswith("alert_hedge:") or data.startswith("alert_close_yield:"):
        action, gid = data.split(":", 1)
        if action == "alert_hedge":
            await query.edit_message_text(
                f"⚖️ To adjust the hedge for *{gid}*, run `/suggest` and add a new Crash or Moon leg.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ To close yield legs in *{gid}*, use the `/close <instrument>` command for each short leg.",
                parse_mode='Markdown'
            )
        return


# ── Startup / Shutdown ────────────────────────────────────────────────────────
async def _post_init(application):
    """Run once after the Application is built but before polling starts."""
    status = await ensure_dvol_history()
    logger.info(status)


async def _post_shutdown(application):
    await deribit_client.aclose()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    load_dotenv("credentials.env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("No TELEGRAM_BOT_TOKEN found.")
        return

    init_db()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start",      start))
    application.add_handler(CommandHandler("help",       help_cmd))
    application.add_handler(CommandHandler("morning",    morning))
    application.add_handler(CommandHandler("status",     status))
    application.add_handler(CommandHandler("tag",        tag_cmd))
    application.add_handler(CommandHandler("untag",      untag_cmd))
    application.add_handler(CommandHandler("campaigns",  campaigns_cmd))
    application.add_handler(CommandHandler("legs",       legs_cmd))
    application.add_handler(CommandHandler("recycle",    recycle_cmd))
    application.add_handler(CommandHandler("register",   register))
    application.add_handler(CommandHandler("setbaseline",setbaseline_cmd))
    application.add_handler(CommandHandler("buy",        trade_cmd))
    application.add_handler(CommandHandler("sell",       trade_cmd))
    application.add_handler(CommandHandler("close",      close_cmd))
    application.add_handler(CommandHandler("suggest",    suggest_cmd))
    application.add_handler(CommandHandler("iv",         iv_cmd))
    application.add_handler(CommandHandler("fear_greed", fear_greed_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))

    job_queue = application.job_queue
    job_queue.run_daily(scheduled_morning_push,  time=time(8,  0))
    job_queue.run_daily(ingest_yesterday_dvol,   time=time(0, 30))
    job_queue.run_repeating(_check_alerts, interval=900, first=60)

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
