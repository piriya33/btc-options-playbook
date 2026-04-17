import os
import sys
import io
import logging
import asyncio
import requests
from datetime import time, datetime, UTC

from dotenv import load_dotenv
load_dotenv("credentials.env")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from deribit.client import DeribitClient
from analyzer.logic import AIRSAnalyzer
from analyzer.charts import generate_payoff_chart

from database.queries import (
    get_iv_rank_30d,
    get_initial_btc_equity,
    add_instrument_to_group,
    remove_instrument_from_group,
    get_morning_push_chat_id,
    set_morning_push_chat_id
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

deribit_client = DeribitClient(testnet=True)

# ── Constants ──────────────────────────────────────────────────────────────────
DELTA_DRIFT_LIMIT = 0.15   # Alert if group delta exceeds this
MARGIN_ALERT_PCT  = 70.0   # Alert if margin utilisation exceeds this

# ── Shared report helper ───────────────────────────────────────────────────────
async def _fetch_data():
    """Authenticate and return all required data."""
    await deribit_client.authenticate()
    spot_price      = await deribit_client.get_btc_spot_price()
    current_dvol    = await deribit_client.get_dvol()
    positions       = await deribit_client.get_open_positions()
    account_summary = await deribit_client.get_account_summary()
    iv_data         = get_iv_rank_30d(current_dvol)
    initial_equity  = get_initial_btc_equity()
    return spot_price, positions, account_summary, iv_data, initial_equity

async def _get_report():
    """Generate text report + inline keyboard."""
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

        # Group-level action buttons
        keyboard.append([
            InlineKeyboardButton("🎯 Take Free Options", callback_data="take_free"),
        ])

        return data["text"], InlineKeyboardMarkup(keyboard) if keyboard else None
    except Exception as e:
        logger.error(f"Error generating report: {e}", exc_info=True)
        return f"⚠️ Error generating report: {e}", None

# ── Delta Drift / Margin background monitor ────────────────────────────────────
async def _check_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Background job: send alerts if delta drifts or margin spikes."""
    chat_id = get_morning_push_chat_id()
    if not chat_id:
        return
    try:
        spot_price, positions, account_summary, iv_data, initial_equity = await _fetch_data()
        analyzer = AIRSAnalyzer(spot_price, positions, account_summary, iv_data, initial_equity)
        directives = analyzer.analyze_positions()
        margin_info = analyzer.analyze_margin()

        # Margin alert
        util = margin_info["margin_utilization_pct"]
        if util >= MARGIN_ALERT_PCT:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *MARGIN ALERT*\nUtilisation: *{util:.1f}%* (Limit: {MARGIN_ALERT_PCT:.0f}%)\nReduce position size or add collateral immediately.",
                parse_mode='Markdown'
            )

        # Delta drift per group
        grouped = {}
        for d in directives:
            gid = d.get("group_id", "Unassigned")
            grouped.setdefault(gid, []).append(d)

        for gid, items in grouped.items():
            total_delta = sum(d.get("raw_delta", 0) for d in items)
            if abs(total_delta) > DELTA_DRIFT_LIMIT:
                keyboard = [[
                    InlineKeyboardButton("⚖️ Adjust Hedge",  callback_data=f"alert_hedge:{gid}"),
                    InlineKeyboardButton("❌ Close Yield",   callback_data=f"alert_close_yield:{gid}"),
                ]]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(f"⚠️ *DELTA DRIFT — {gid}*\n"
                          f"Combined Δ: *{round(total_delta, 3)}* (Limit: ±{DELTA_DRIFT_LIMIT})\n"
                          f"Consider adjusting your hedge legs."),
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
    except Exception as e:
        logger.error(f"Alert check failed: {e}")

# ── Command handlers ───────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! I am AIRY, your AIRS playbook bot. "
        "Commands: /morning /status /suggest /iv /fear_greed /buy /sell /close /group /ungroup /register"
    )

async def morning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching Deribit data... ⏳")
    report, markup = await _get_report()
    await update.message.reply_text(report, parse_mode='Markdown', reply_markup=markup)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send status + payoff chart."""
    await update.message.reply_text("Generating live status... ⏳")
    report, markup = await _get_report()
    await update.message.reply_text(report, parse_mode='Markdown', reply_markup=markup)

    # Send payoff diagram
    try:
        spot_price, positions, _, _, _ = await _fetch_data()
        if positions:
            buf = generate_payoff_chart(positions, spot_price)
            await update.message.reply_photo(
                photo=buf,
                caption="📈 P&L at Expiry — Current Portfolio"
            )
    except Exception as e:
        logger.error(f"Payoff chart error: {e}")
        await update.message.reply_text(f"⚠️ Could not generate payoff chart: {e}")

async def iv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/iv — Show DVOL vs realised volatility."""
    await update.message.reply_text("Fetching volatility data... ⏳")
    try:
        dvol = await deribit_client.get_dvol()
        iv_data = get_iv_rank_30d(dvol)

        # Rough realised vol proxy via 30d BTC price change (public endpoint)
        rv_url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=30&interval=daily"
        r = requests.get(rv_url, timeout=8)
        prices = [p[1] for p in r.json().get("prices", [])]
        if len(prices) > 2:
            import numpy as np
            returns = np.diff(np.log(prices))
            realised_vol = float(np.std(returns) * np.sqrt(365) * 100)
        else:
            realised_vol = None

        lines = [
            "📐 *Volatility Intelligence*",
            f"• DVOL (Implied): *{dvol:.2f}*",
            f"• 30d IV Rank: *{iv_data['rank']}%* (Range: {iv_data['min']} – {iv_data['max']})",
        ]
        if realised_vol:
            vol_premium = dvol - realised_vol
            verdict = "🟢 *Options CHEAP* — IV < RV. Good time to BUY hedges." if vol_premium < 0 \
                      else f"🔴 *Options EXPENSIVE* — IV premium: +{vol_premium:.1f} pts. Good time to SELL yield."
            lines.append(f"• 30d Realised Vol: *{realised_vol:.2f}*")
            lines.append(f"• IV Premium: *{vol_premium:+.2f}* pts")
            lines.append(f"\n{verdict}")

        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching IV data: {e}")

async def fear_greed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/fear_greed — Show crypto Fear & Greed index."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
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

async def group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage: /group <TradeID> <instrument>")
        return
    trade_id, instrument = args
    if add_instrument_to_group(trade_id, instrument):
        await update.message.reply_text(f"✅ Grouped {instrument} → {trade_id}.")
    else:
        await update.message.reply_text("❌ Failed to group instrument.")

async def ungroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Usage: /ungroup <instrument>")
        return
    if remove_instrument_from_group(args[0]):
        await update.message.reply_text(f"✅ Ungrouped {args[0]}.")
    else:
        await update.message.reply_text("❌ Instrument not found in any group.")

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_morning_push_chat_id(update.effective_chat.id)
    await update.message.reply_text("✅ Registered! Daily Morning Briefing at 08:00 UTC.")

async def scheduled_morning_push(context: ContextTypes.DEFAULT_TYPE):
    chat_id = get_morning_push_chat_id()
    if chat_id:
        report, markup = await _get_report()
        await context.bot.send_message(chat_id=chat_id, text=report, parse_mode='Markdown', reply_markup=markup)

async def trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute a trade: /buy <instrument> <amount> [price] or /sell <instrument> <amount> [price]"""
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
        if size > 0:
            res = await deribit_client.sell(instrument, abs(size), order_type="market")
        else:
            res = await deribit_client.buy(instrument, abs(size), order_type="market")
        order = res.get("order", {})
        await update.message.reply_text(f"✅ Closed! ID: {order.get('order_id')}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def suggest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Searching for the best instruments (30-45 DTE)... 🔍")
    try:
        leg_a = await deribit_client.find_instruments_by_delta(-0.10, 35, 'C')
        leg_b = await deribit_client.find_instruments_by_delta(-0.10, 35, 'P')
        leg_c = await deribit_client.find_instruments_by_delta(0.03, 35, 'P')
        leg_d = await deribit_client.find_instruments_by_delta(0.02, 35, 'C')

        await deribit_client.authenticate()
        summary = await deribit_client.get_account_summary()
        equity  = summary.get("equity", 0)
        scale   = max(0.5, round(equity * 0.5, 1))

        report = [
            "🚀 *New AIRS Suggestion (35 DTE Target)*",
            f"\n💰 *Account Equity:* {round(equity, 4)} BTC",
            f"⚖️ *Suggested Scale:* {scale} (per campaign, target 3 active)",
            "\n*Yield Legs (Short):*",
            f"• A (Call): {leg_a[0]['instrument']} (Δ: {round(leg_a[0]['delta'], 2)})",
            f"  └ Size: {round(0.5 * scale, 1)} BTC | Bid: {leg_a[0]['bid']} | Ask: {leg_a[0]['ask']}",
            f"• B (Put): {leg_b[0]['instrument']} (Δ: {round(leg_b[0]['delta'], 2)})",
            f"  └ Size: {round(0.2 * scale, 1)} BTC | Bid: {leg_b[0]['bid']} | Ask: {leg_b[0]['ask']}",
            "\n*Hedge Legs (Long):*",
            f"• C (Crash): {leg_c[0]['instrument']} (Δ: {round(leg_c[0]['delta'], 2)})",
            f"  └ Size: {round(0.6 * scale, 1)} BTC | Bid: {leg_c[0]['bid']} | Ask: {leg_c[0]['ask']}",
            f"• D (Moon): {leg_d[0]['instrument']} (Δ: {round(leg_d[0]['delta'], 2)})",
            f"  └ Size: {round(1.0 * scale, 1)} BTC | Bid: {leg_d[0]['bid']} | Ask: {leg_d[0]['ask']}",
            "\n_Click below to open all legs at suggested sizes._"
        ]

        context.user_data["last_suggestion"] = {
            "leg_a": leg_a[0]['instrument'],
            "leg_b": leg_b[0]['instrument'],
            "leg_c": leg_c[0]['instrument'],
            "leg_d": leg_d[0]['instrument'],
            "scale": scale
        }

        keyboard = [[InlineKeyboardButton("🚀 Initiate Full AIRS Campaign", callback_data="init_airs")]]
        await update.message.reply_text("\n".join(report), parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error finding suggestions: {e}")
        await update.message.reply_text(f"❌ Error finding suggestions: {e}")

# ── Button handler ─────────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Init AIRS ──────────────────────────────────────────────────────────────
    if data == "init_airs":
        suggestion = context.user_data.get("last_suggestion")
        if not suggestion:
            await query.edit_message_text("❌ Suggestion expired. Run /suggest again.")
            return

        await query.edit_message_text("⏳ Executing 4-Leg AIRS Initiation in Parallel...")
        try:
            scale = suggestion.get("scale", 1.0)
            await deribit_client.authenticate()
            trades = [
                {"instr": suggestion["leg_d"], "amount": round(1.0 * scale, 1), "side": "buy"},
                {"instr": suggestion["leg_c"], "amount": round(0.6 * scale, 1), "side": "buy"},
                {"instr": suggestion["leg_a"], "amount": round(0.5 * scale, 1), "side": "sell"},
                {"instr": suggestion["leg_b"], "amount": round(0.2 * scale, 1), "side": "sell"},
            ]

            async def execute_leg(t):
                try:
                    ticker = await deribit_client.get_ticker(t["instr"])
                    if t["side"] == "buy":
                        price = ticker.get("best_ask_price") or ticker.get("last_price")
                        res = await deribit_client.buy(t["instr"], t["amount"], price=price, order_type="limit")
                    else:
                        price = ticker.get("best_bid_price") or ticker.get("last_price")
                        res = await deribit_client.sell(t["instr"], t["amount"], price=price, order_type="limit")
                    order = res.get("order", {})
                    return {"instr": t["instr"], "status": "✅", "id": order.get("order_id"), "price": order.get("average_price", 0)}
                except Exception as e:
                    return {"instr": t["instr"], "status": "❌", "error": str(e)}

            results = await asyncio.gather(*(execute_leg(t) for t in trades))
            trade_id = f"AIRS-{datetime.now(UTC).strftime('%m%d-%H%M')}"
            summary = [f"🚀 *AIRS Campaign ({trade_id})*\n"]
            for r in results:
                if r["status"] == "✅":
                    summary.append(f"✅ {r['instr']}\n  └ Price: {r['price']} | ID: {r['id']}")
                    add_instrument_to_group(trade_id, r["instr"])
                else:
                    summary.append(f"❌ {r['instr']}\n  └ {r['error']}")
            await query.edit_message_text("\n".join(summary), parse_mode='Markdown')
        except Exception as e:
            await query.edit_message_text(f"❌ Critical error: {e}")
        return

    # ── Take Free Options: close short legs, keep long ────────────────────────
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
                instr = pos["instrument_name"]
                size  = pos["size"]
                try:
                    ticker = await deribit_client.get_ticker(instr)
                    price  = ticker.get("best_ask_price") or ticker.get("last_price")
                    res    = await deribit_client.buy(instr, abs(size), price=price, order_type="limit")
                    results.append(f"✅ Closed {instr}")
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

    # ── Roll a single instrument ───────────────────────────────────────────────
    if data.startswith("roll:"):
        instrument = data.split(":", 1)[1]
        await query.edit_message_text(
            f"🔄 *Roll {instrument}*\n\nTo roll, close the existing leg then open a new one at the target 0.10 delta:\n"
            f"1. /close {instrument}\n2. /suggest → find new leg → initiate",
            parse_mode='Markdown'
        )
        return

    # ── Close a single instrument ──────────────────────────────────────────────
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
            ticker = await deribit_client.get_ticker(instrument)
            if size > 0:
                price = ticker.get("best_bid_price") or ticker.get("last_price")
                res = await deribit_client.sell(instrument, abs(size), price=price, order_type="limit")
            else:
                price = ticker.get("best_ask_price") or ticker.get("last_price")
                res = await deribit_client.buy(instrument, abs(size), price=price, order_type="limit")
            order = res.get("order", {})
            await query.edit_message_text(f"✅ Closed {instrument}!\nOrder ID: {order.get('order_id')}")
        except Exception as e:
            await query.edit_message_text(f"❌ Error closing {instrument}: {e}")
        return

    # ── Alert actions (informational) ─────────────────────────────────────────
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

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    load_dotenv("credentials.env")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("No TELEGRAM_BOT_TOKEN found.")
        return

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start",      start))
    application.add_handler(CommandHandler("morning",    morning))
    application.add_handler(CommandHandler("status",     status))
    application.add_handler(CommandHandler("group",      group_cmd))
    application.add_handler(CommandHandler("ungroup",    ungroup_cmd))
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
    # Check for delta drift + margin alerts every 15 minutes
    job_queue.run_repeating(_check_alerts, interval=900, first=60)

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
