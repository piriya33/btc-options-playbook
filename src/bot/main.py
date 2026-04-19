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
    get_morning_push_chat_id,
    set_morning_push_chat_id,
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
        rf"Hi {user.mention_html()}! I am AIRY, your AIRS playbook bot.<br/>"
        "<b>Market:</b> /morning /status /suggest /iv /fear_greed<br/>"
        "<b>Trading:</b> /buy /sell /close<br/>"
        "<b>Tagging:</b> /tag &lt;instrument&gt; &lt;role&gt; &lt;campaign&gt; | /untag &lt;instrument&gt;<br/>"
        "  Roles: A=yield_call  B=yield_put  C=crash_hedge  D=moon_hedge<br/>"
        "  Example: <code>/tag BTC-27JUN25-100000-C A MAY-2026</code><br/>"
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
    await update.message.reply_text(f"{prefix} {msg}", parse_mode='Markdown')


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
    await update.message.reply_text(f"{prefix} {msg}")


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
        size = pos["size"]
        ticker = await deribit_client.get_ticker(instrument)
        if size > 0:
            price = ticker.get("best_bid_price") or ticker.get("last_price")
            res = await deribit_client.sell(instrument, abs(size), price=price, order_type="limit")
        else:
            price = ticker.get("best_ask_price") or ticker.get("last_price")
            res = await deribit_client.buy(instrument, abs(size), price=price, order_type="limit")
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
                "campaign": f"AIRS-{datetime.now(UTC).strftime('%b%Y').upper()}",
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
