# Track 2: Persistence & Grouping

## Objective
Persist historical DVOL data to calculate local 30-day IV Rank, and implement manual trade grouping via Telegram so the Analyzer can track spreads by Trade ID.

## Phase 1: DVOL Historical Ingestion & IV Rank
1. **Task:** Create a database connection utility and ensure all SQLAlchemy models from Track 1 are created locally (`sqlite:///data.db`).
2. **Task:** Write an ingestion script to read `DERIBIT_DVOL, 1D_f4084.csv` and populate the `dvol_history` table.
3. **Task:** Implement IV Rank calculation logic: `(Current DVOL - 30d Min) / (30d Max - 30d Min) * 100`.

## Phase 2: Manual Trade Grouping
1. **Task:** Add Telegram command `/group <TradeID> <instrument>` (e.g., `/group AIRS-01 BTC-28JUN24-65000-C`) to map a specific instrument to a `TradeGroup` in the database.
2. **Task:** Add Telegram command `/ungroup <instrument>` to remove an instrument from a group.
3. **Task:** Update the Analyzer to query the database and visually group open positions by `Trade ID` in the Morning Briefing report.

## Verification
Track completion verified when the user can ingest the CSV, check the current IV Rank via the bot, and manually group/ungroup their open testnet positions, seeing the grouped structure reflected in the `/morning` report.
