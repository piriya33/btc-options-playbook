# Track 1: Data Engine & Telegram Analyzer

## Objective
Build a stateless Python back-end capable of querying Deribit, grouping complex options spreads via a local SQLite database, and delivering explicit AIRS playbook instructions via a Telegram Bot.

## Phase 1: Deribit Connection & Data Modeling
1. **Task:** Initialize Python virtual environment and install dependencies (`httpx`, `python-telegram-bot`, `sqlalchemy`, `pydantic`).
2. **Task:** Build Deribit REST API client to pull live Spot Price, DVOL, and open account positions.
3. **Task:** Set up SQLite database with SQLAlchemy to track `Trade IDs` (mapping multiple Deribit instruments to a single logical AIRS spread).

## Phase 2: AIRS Logic "The Analyzer"
1. **Task:** Create the `Analyzer` class to evaluate open positions against the AIRS Playbook rules.
2. **Task:** Implement logic to calculate exact Margin Utilization bounds and Liquidation distance dynamically.
3. **Task:** Output explicit action directives: `[Hold/Action Required] -> Strike: X, Delta: Y, DTE: Z`.

## Phase 3: Telegram Bot Integration
1. **Task:** Register a new Bot via BotFather and secure API keys locally.
2. **Task:** Build the `/morning` command handler to return the formatted Markdown status table.
3. **Task:** Build the `/status` command to return live metric checks on demand.

## Verification
Track completion verified when the user can type `/morning` in Telegram and receive a fully populated response table pulling live, grouped data from their Deribit test account.
