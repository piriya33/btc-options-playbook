import os
import io
import logging
import asyncio
import httpx
from datetime import time, datetime, UTC

from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

from deribit.client import DeribitClient
from analyzer.logic import AIRSAnalyzer
from analyzer.charts import generate_payoff_chart
from database.models import ROLE_LABELS

from database.session import init_db
from database.queries import (
    get_iv_rank_30d,
    get_initial_btc_equity,
    tag_instrument,
    untag_instrument,
    close_leg,
    get_leg_info,
    get_morning_push_chat_id,
    set_morning_push_chat_id,
    get_all_open_campaigns,
    list_legs_for_campaign,
    get_legs_for_spread,
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

deribit_client = DeribitClient(testnet=True)

# ── Constants ──────────────────────────────────────────────────────────────────
DELTA_DRIFT_LIMIT  = 0.15
MARGIN_WARN_PCT    = 25.0   # blueprint hard limit — log a warning
MARGIN_ALERT_PCT   = 40.0   # critical — send Telegram alert
SPREAD_WARN_PCT    = 25.0   # bid/ask spread as % of mid — warn user
SPREAD_BLOCK_PCT   = 80.0   # bid/ask spread — block execution (no real market)
HARVEST_TARGET_PCT = 50.0   # alert when short leg profit >= this % of entry credit

# Track harvest alerts sent this session — prevents re-alerting every 15 min
_harvest_alerted: set = set()


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
    return [
        {"instr": suggestion["leg_d"], "amount": round(1.0 * scale, 1), "side": "buy",  "role": "moon_hedge"},
        {"instr": suggestion["leg_c"], "amount": round(0.6 * scale, 1), "side": "buy",  "role": "crash_hedge"},
        {"instr": suggestion["leg_a"], "amount": round(0.5 * scale, 1), "side": "sell", "role": "yield_call"},
        {"instr": suggestion["leg_b"], "amount": round(0.2 * scale, 1), "side": "sell", "role": "yield_put"},
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


def _filled_slots(campaigns: list) -> dict:
    """
    Return {slot_number: {"name": ..., "dte": ...}} for each open campaign
    that maps to a recognised slot.
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
            filled[slot_num] = {"name": c["name"], "dte": dte}
    return filled


# ── Market Readiness Score ─────────────────────────────────────────────────────

async def _market_readiness_score(iv_rank: float, margin_pct: float) -> dict:
    """
    Composite gate for /suggest.
    Levels: GO 🟢 / CAUTION 🟡 / AVOID 🔴
    Block triggers  → AVOID:   IV rank < 20  or margin > MARGIN_WARN_PCT
    Warn triggers   → CAUTION: IV rank > 80  or spot 24h move > 10%
    """
    level = "GO"
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
        pass  # non-fatal — we just won't check this signal

    # ── Block conditions ──────────────────────────────────────────────────────
    if iv_rank < 20:
        level = "AVOID"
        reasons.append(f"IV Rank {iv_rank:.1f}% < 20% — volatility too cheap to sell")
    if margin_pct > MARGIN_WARN_PCT:
        level = "AVOID"
        reasons.append(f"Margin {margin_pct:.1f}% > {MARGIN_WARN_PCT:.0f}% — no capacity for new positions")

    # ── Warn conditions (only upgrade to CAUTION if not already blocked) ──────
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
    iv_data         = get_iv_rank_30d(current_dvol)
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
            if instr in _harvest_alerted:
                continue  # already alerted this session

            entry_credit = abs(pos.get("average_price", 0))
            floating_pnl = pos.get("floating_profit_loss", 0)
            if entry_credit <= 0:
                continue

            # floating_pnl is positive when short is profitable (option decayed)
            profit_pct = (floating_pnl / (entry_credit * abs(pos["size"]))) * 100 if entry_credit > 0 else 0

            if profit_pct >= HARVEST_TARGET_PCT:
                _harvest_alerted.add(instr)
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
        rf"Hi {user.mention_html()}! I am AIRY, your AIRS playbook bot.<br/>"
        "<b>Market:</b> /morning /status /suggest /iv /fear_greed<br/>"
        "<b>Trading:</b> /buy /sell /close<br/>"
        "<b>Campaigns:</b> /campaigns /legs<br/>"
        "<b>Tagging:</b> /tag &lt;instrument&gt; &lt;role&gt; &lt;campaign&gt; | /untag &lt;instrument&gt;<br/>"
        "  Roles: A=yield_call  B=yield_put  C=crash_hedge  D=moon_hedge<br/>"
        "  Example: <code>/tag BTC-27JUN25-100000-C A MAY2026</code><br/>"
        "<b>Settings:</b> /register"
    )


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
        iv_data = get_iv_rank_30d(dvol)

        rv_url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=30&interval=daily"
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(rv_url)
        prices = [p[1] for p in r.json().get("prices", [])]

        realised_vol = None
        if len(prices) > 2:
            import numpy as np
            returns = np.diff(np.log(prices))
            realised_vol = float(np.std(returns) * np.sqrt(365) * 100)

        lines = [
            "📐 *Volatility Intelligence*",
            f"• DVOL (Implied): *{dvol:.2f}*",
            f"• 30d IV Rank: *{iv_data['rank']}%* (Range: {iv_data['min']} – {iv_data['max']})",
        ]
        if realised_vol:
            vol_premium = dvol - realised_vol
            verdict = ("🟢 *Options CHEAP* — IV < RV. Good time to BUY hedges." if vol_premium < 0
                       else f"🔴 *Options EXPENSIVE* — IV premium: +{vol_premium:.1f} pts. Good time to SELL yield.")
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


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_morning_push_chat_id(update.effective_chat.id)
    await update.message.reply_text("✅ Registered! Daily Morning Briefing at 08:00 UTC.")


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
        realized_pnl = pos.get("floating_profit_loss", 0)
        size = pos["size"]
        ticker = await deribit_client.get_ticker(instrument)
        if size > 0:
            price = ticker.get("best_bid_price") or ticker.get("last_price")
            res = await deribit_client.sell(instrument, abs(size), price=price, order_type="limit")
        else:
            price = ticker.get("best_ask_price") or ticker.get("last_price")
            res = await deribit_client.buy(instrument, abs(size), price=price, order_type="limit")
        order = res.get("order", {})

        # Record realized PnL if this leg is tagged
        pnl_msg = ""
        ok, msg = close_leg(instrument, realized_pnl)
        if ok:
            pnl_msg = f"\nRealized PnL: {realized_pnl:+.5f} BTC (recorded)"
        elif "not tagged" not in msg:
            pnl_msg = f"\n⚠️ PnL not recorded: {msg}"

        await update.message.reply_text(f"✅ Closed! ID: {order.get('order_id')}{pnl_msg}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def suggest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Analysing campaign slots and finding best instruments... 🔍")
    try:
        await deribit_client.authenticate()
        summary    = await deribit_client.get_account_summary()
        equity     = summary.get("equity", 0)
        margin_pct = (summary.get("initial_margin", 0) / equity * 100) if equity > 0 else 0

        # ── Market Readiness Score ────────────────────────────────────────────
        current_dvol = await deribit_client.get_dvol()
        iv_data      = get_iv_rank_30d(current_dvol)
        readiness    = await _market_readiness_score(iv_data["rank"], margin_pct)

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

        # ── Determine which slot to fill ──────────────────────────────────────
        existing  = get_all_open_campaigns()
        filled    = _filled_slots(existing)

        open_slot = next((s for s in CAMPAIGN_SLOTS if s["number"] not in filled), None)

        if open_slot is None:
            slot_lines = "\n".join(
                f"  Slot {s['number']} – {s['label']}: ✅ {filled[s['number']]['name']} ({filled[s['number']]['dte']} DTE)"
                for s in CAMPAIGN_SLOTS
            )
            await update.message.reply_text(
                f"✅ *All 3 campaign slots are active:*\n{slot_lines}\n\n"
                "_No new campaign needed. Monitor existing positions with /status._",
                parse_mode='Markdown'
            )
            return

        target_dte = open_slot["target_dte"]

        # ── Find legs at the right DTE ────────────────────────────────────────
        leg_a = await deribit_client.find_instruments_by_delta(-0.10, target_dte, 'C')
        leg_b = await deribit_client.find_instruments_by_delta(-0.10, target_dte, 'P')
        leg_c = await deribit_client.find_instruments_by_delta(0.03,  target_dte, 'P')
        leg_d = await deribit_client.find_instruments_by_delta(0.02,  target_dte, 'C')

        # ── Sizing: equity / 3 per campaign ──────────────────────────────────
        scale = round(equity / 3, 1)

        # Leg sizes at the AIRS ratios
        sz_a = round(0.5 * scale, 1)
        sz_b = round(0.2 * scale, 1)
        sz_c = round(0.6 * scale, 1)
        sz_d = round(1.0 * scale, 1)

        # ── Net premium breakdown ─────────────────────────────────────────────
        call_credit = sz_a * leg_a[0]['bid'] - sz_d * leg_d[0]['ask']
        put_credit  = sz_b * leg_b[0]['bid'] - sz_c * leg_c[0]['ask']
        total_credit = call_credit + put_credit

        def _fmt_credit(val: float) -> str:
            sign = "+" if val >= 0 else ""
            emoji = "✅" if val >= 0 else "⚠️"
            return f"{sign}{round(val, 5)} BTC {emoji}"

        # ── Slot status lines ─────────────────────────────────────────────────
        slot_lines = []
        for s in CAMPAIGN_SLOTS:
            if s["number"] in filled:
                info = filled[s["number"]]
                slot_lines.append(f"  Slot {s['number']} – {s['label']}: ✅ {info['name']} ({info['dte']} DTE)")
            elif s["number"] == open_slot["number"]:
                slot_lines.append(f"  Slot {s['number']} – {s['label']}: 👉 *Suggesting now*")
            else:
                slot_lines.append(f"  Slot {s['number']} – {s['label']}: ⬜ Empty")

        report = [
            f"🚀 *AIRS Suggestion — Slot {open_slot['number']}: {open_slot['label']}*",
            f"Target: ~{target_dte} DTE\n",
            "*Campaign Slots:*",
            *slot_lines,
            f"\n💰 Equity: {round(equity, 4)} BTC",
            f"⚖️ Campaign allocation: {scale} BTC (equity ÷ 3)",
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

        # ── Readiness block ───────────────────────────────────────────────────
        report.append(f"\n*Market Readiness: {readiness['emoji']} {readiness['level']}*")
        if readiness["reasons"]:
            for r in readiness["reasons"]:
                report.append(f"  ⚠️ {r}")
        if readiness["spot_move_24h"] is not None:
            report.append(f"  24h spot move: {readiness['spot_move_24h']:.1f}%")

        if total_credit < 0:
            report.append("\n❌ *Net DEBIT — do not initiate. Adjust strikes or sizing.*")
        else:
            if readiness["level"] == "CAUTION":
                report.append("\n⚠️ *Proceed with caution.* Pre-flight check runs before any order is placed.")
            else:
                report.append("\n_Pre-flight check runs before any order is placed._")

        context.user_data["last_suggestion"] = {
            "leg_a": leg_a[0]['instrument'],
            "leg_b": leg_b[0]['instrument'],
            "leg_c": leg_c[0]['instrument'],
            "leg_d": leg_d[0]['instrument'],
            "scale": scale,
            "slot":  open_slot,
        }

        keyboard = [[InlineKeyboardButton(
            f"🚀 Initiate Slot {open_slot['number']} – {open_slot['label']}",
            callback_data="init_airs"
        )]]
        await update.message.reply_text(
            "\n".join(report), parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard) if total_credit >= 0 else None
        )
    except Exception as e:
        logger.error(f"Error finding suggestions: {e}")
        await update.message.reply_text(f"❌ Error finding suggestions: {e}")


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
                "trades": trades,
                "exec_prices": exec_prices,
                "campaign": _instr_campaign_name(trades[0]["instr"]),
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
                lines.append("_Legs execute sequentially. If any leg fails, filled legs are rolled back._")
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
        # ── Step 2: Sequential execution with rollback ─────────────────────────
        pending = context.user_data.get("pending_airs")
        if not pending:
            await query.edit_message_text("❌ Session expired. Run /suggest again.")
            return

        trades       = pending["trades"]
        exec_prices  = pending["exec_prices"]
        campaign_name = pending["campaign"]
        await query.edit_message_text("⏳ Executing legs sequentially...")

        filled = []  # legs that placed successfully — needed for rollback
        failed_leg = None

        try:
            await deribit_client.authenticate()
            for t in trades:
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
                except Exception as e:
                    failed_leg = {"instr": t["instr"], "error": str(e)}
                    break

            if failed_leg:
                # ── Rollback ───────────────────────────────────────────────────
                rb_lines = [
                    f"❌ *Leg failed:* {failed_leg['instr']}\n  └ {failed_leg['error']}\n",
                    f"⏪ Rolling back {len(filled)} filled leg(s)...",
                ]
                for f in filled:
                    try:
                        if f["state"] in ("open", "untriggered"):
                            await deribit_client.cancel_order(f["order_id"])
                            rb_lines.append(f"✅ Cancelled order for {f['instr']}")
                        else:
                            # Position filled — reverse it
                            ticker = await deribit_client.get_ticker(f["instr"])
                            if f["side"] == "buy":
                                price = ticker.get("best_bid_price") or ticker.get("last_price")
                                await deribit_client.sell(f["instr"], f["amount"], price=price, order_type="limit")
                            else:
                                price = ticker.get("best_ask_price") or ticker.get("last_price")
                                await deribit_client.buy(f["instr"], f["amount"], price=price, order_type="limit")
                            rb_lines.append(f"✅ Closed position in {f['instr']}")
                    except Exception as re:
                        rb_lines.append(f"⚠️ Rollback failed for {f['instr']}: {re}\n  → Close manually with /close {f['instr']}")
                await query.edit_message_text("\n".join(rb_lines), parse_mode='Markdown')
                return

            # ── All legs filled — tag and report ──────────────────────────────
            summary = [f"🚀 *AIRS Campaign ({campaign_name})*\n"]
            for f in filled:
                role_label = ROLE_LABELS.get(f["role"], f["role"])
                summary.append(f"✅ *[{role_label}]* {f['instr']}\n  └ Price: {f['price']} | ID: {f['order_id']}")
                tag_instrument(f["instr"], f["role"], campaign_name)
            summary.append("\n_Use /tag to re-assign legs or add to a different campaign._")
            await query.edit_message_text("\n".join(summary), parse_mode='Markdown')

        except Exception as e:
            await query.edit_message_text(f"❌ Critical error during execution: {e}")
        return

    if data == "init_airs_cancel":
        context.user_data.pop("pending_airs", None)
        await query.edit_message_text("❌ Execution cancelled.")
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
                realized_pnl = pos.get("floating_profit_loss", 0)
                try:
                    ticker = await deribit_client.get_ticker(instr)
                    price  = ticker.get("best_ask_price") or ticker.get("last_price")
                    res    = await deribit_client.buy(instr, abs(size), price=price, order_type="limit")
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
            realized_pnl = pos.get("floating_profit_loss", 0)
            size = pos["size"]
            ticker = await deribit_client.get_ticker(instrument)
            if size > 0:
                price = ticker.get("best_bid_price") or ticker.get("last_price")
                res = await deribit_client.sell(instrument, abs(size), price=price, order_type="limit")
            else:
                price = ticker.get("best_ask_price") or ticker.get("last_price")
                res = await deribit_client.buy(instrument, abs(size), price=price, order_type="limit")
            order = res.get("order", {})

            pnl_msg = ""
            ok, msg = close_leg(instrument, realized_pnl)
            if ok:
                pnl_msg = f"\nRealized PnL: {realized_pnl:+.5f} BTC (recorded)"

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
                realized_pnl = pos.get("floating_profit_loss", 0)
                size = pos["size"]
                try:
                    ticker = await deribit_client.get_ticker(instr)
                    if size > 0:
                        price = ticker.get("best_bid_price") or ticker.get("last_price")
                        await deribit_client.sell(instr, abs(size), price=price, order_type="limit")
                    else:
                        price = ticker.get("best_ask_price") or ticker.get("last_price")
                        await deribit_client.buy(instr, abs(size), price=price, order_type="limit")
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


# ── Shutdown ───────────────────────────────────────────────────────────────────
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
        .post_shutdown(_post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start",      start))
    application.add_handler(CommandHandler("morning",    morning))
    application.add_handler(CommandHandler("status",     status))
    application.add_handler(CommandHandler("tag",        tag_cmd))
    application.add_handler(CommandHandler("untag",      untag_cmd))
    application.add_handler(CommandHandler("campaigns",  campaigns_cmd))
    application.add_handler(CommandHandler("legs",       legs_cmd))
    application.add_handler(CommandHandler("register",   register))
    application.add_handler(CommandHandler("buy",        trade_cmd))
    application.add_handler(CommandHandler("sell",       trade_cmd))
    application.add_handler(CommandHandler("close",      close_cmd))
    application.add_handler(CommandHandler("suggest",    suggest_cmd))
    application.add_handler(CommandHandler("iv",         iv_cmd))
    application.add_handler(CommandHandler("fear_greed", fear_greed_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))

    job_queue = application.job_queue
    job_queue.run_daily(scheduled_morning_push, time=time(8, 0))
    job_queue.run_repeating(_check_alerts, interval=900, first=60)

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
