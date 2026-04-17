# Track 4: Trade Execution & Management (Testnet)

## Objective
Enable the bot to execute trades directly on the Deribit Testnet, allowing for seamless transition from "Directive" to "Action".

## Phase 1: Basic Execution
1. **Task:** Implement `private/buy` and `private/sell` in `DeribitClient`.
2. **Task:** Add bot commands: `/trade buy <instrument> <amount> [price]` and `/trade sell <instrument> <amount> [price]`. If price is omitted, use Market (or best Bid/Ask).
3. **Task:** Implement `/close <instrument>` to simplify closing out specific legs.

## Phase 2: Spread/Combo Execution
1. **Task:** Research Deribit's "Combo" API. Implement a way to execute multi-leg AIRS structures in a single transaction if possible, to reduce slippage and leg-risk.
2. **Task:** Implement `/airs_init` which suggests the 4-leg structure and asks for confirmation to execute all 4 legs as a set.

## Phase 3: Risk Management & Validation
1. **Task:** Add "Pre-Flight Checks" to execution (e.g., checking if the trade would push Margin Utilization > 50% before allowing it).
2. **Task:** Implement a "One-Click Roll" command for the analyzer's "ROLL" directive.

## Verification
Track completion verified when the user can successfully open and close multi-leg positions via the Telegram bot on the Testnet.
