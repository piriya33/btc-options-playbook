# AIRY — BTC Options Playbook 🤖₿

An institutional-grade BTC options trading assistant powered by a Telegram bot, using the **Antifragile Inverse Ratio-Spread (AIRS)** strategy on Deribit.

## What is AIRS?

The **Antifragile Inverse Ratio-Spread (AIRS)** is a BTC-denominated options strategy designed to:
- **Collect premium** from short OTM options (yield legs)
- **Hold free tail exposure** via long OTM hedges (moon/crash legs)
- **Benefit from volatility events** (crashes or moonshots) while staying net-positive in calm markets

## Features

| Feature | Command |
|---|---|
| 📊 Live Portfolio Briefing | `/morning` or `/status` |
| 📈 Payoff Diagram at Expiry | Auto-attached to `/status` |
| 🔍 Smart Instrument Discovery | `/suggest` |
| 🚀 One-Click AIRS Initiation | Button after `/suggest` |
| 📐 IV vs Realised Vol | `/iv` |
| 😱 Fear & Greed Bias | `/fear_greed` |
| ⚖️ Manual Trade Execution | `/buy <instr> <amount>` / `/sell <instr> <amount>` |
| ❌ Close Position | `/close <instrument>` |
| 🎯 Take Free Options | Button in `/status` |
| 🏷️ Group Positions | `/group <TradeID> <instrument>` |
| 🔔 Delta Drift Alerts | Automatic (every 15 min) |
| 🚨 Margin Alerts | Automatic (>70% utilisation) |

## Setup

### 1. Clone & install
```bash
git clone https://github.com/piriya33/btc-options-playbook.git
cd btc-options-playbook
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 2. Configure credentials
Create a `credentials.env` file (never commit this):
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
DERIBIT_API_KEY=your_deribit_client_id
DERIBIT_SECRET_KEY=your_deribit_client_secret
```

### 3. Run
```bash
python src/bot/main.py
```

## Project Structure

```
src/
├── bot/
│   └── main.py          # Telegram bot — all commands & handlers
├── deribit/
│   └── client.py        # Deribit REST/JSON-RPC client (auto-auth, retry)
├── analyzer/
│   ├── logic.py         # AIRS strategy analyzer & directive engine
│   └── charts.py        # Payoff diagram generator (matplotlib)
└── database/
    └── queries.py       # SQLite persistence (groups, IV history, equity)
```

## Strategy Sizing

The `/suggest` command auto-sizes legs based on your live portfolio equity:
- **Target:** 3 concurrent campaigns
- **Per-campaign scale:** 0.5× equity
- **Leg ratios:** D(1.0x long) : C(0.6x long) : A(0.5x short) : B(0.2x short)

## Notes

- Tested on **Deribit Testnet** (`test.deribit.com`)
- Switch to mainnet by changing `testnet=True` → `testnet=False` in `main.py`
- All orders use **limit orders at best bid/ask** (Deribit rejects market orders on options)
