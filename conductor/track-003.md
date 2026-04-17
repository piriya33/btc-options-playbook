# Track 3: Advanced Metrics & Scheduling

## Objective
Implement advanced strategy metrics (Convexity Score, Satoshi Multiplier) into the Analyzer and create a scheduled cron job to automatically push the Morning Briefing to the user daily.

## Phase 1: Advanced Metrics
1. **Task:** Implement **Satoshi Multiplier**: Requires saving an "Initial BTC Equity" baseline (e.g. 1.0 BTC) to the database, so the bot can calculate `(Current BTC / Initial BTC) - 1`.
2. **Task:** Implement **Convexity Score**: In the Analyzer, loop through the open positions' Gamma values. Calculate the ratio of total Long Gamma vs total Short Gamma to assess mathematical protection.
3. **Task:** Implement **Realized PnL Tracking**: Update data engine to poll trade history and store realized PnL for closed legs in the database, allowing groups to show true total performance.

## Phase 2: Automated Morning Push
1. **Task:** Integrate `APScheduler` into the Telegram bot.
2. **Task:** Schedule a daily task (e.g., at 08:00 AM UTC) that calls the `_get_report` function and uses `context.bot.send_message` to push the briefing to a registered Telegram Chat ID.
3. **Task:** Add a `/register` command to the bot to let the user save their Chat ID so the bot knows where to send the daily push.

## Verification
Track completion verified when the Morning Briefing displays the Convexity Score and Satoshi Multiplier, and the user successfully registers their chat for automated daily messages.
