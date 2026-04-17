# Systematic Strategy Blueprint: The Antifragile Inverse Ratio-Spread (AIRS)

*   **Strategy Name:** The Antifragile Inverse Ratio-Spread (AIRS)
*   **Target Asset:** BTC-Settled Options (Deribit)

## Pillar 1: Selection & Universe

*   **Instrument:** Inverse BTC Options.
*   **Collateral:** 100% BTC.
*   **Time Horizon:** Monthly cycles (Harvesting the "sweet spot" of Theta decay).

## Pillar 2: Entry Logic (The "Structural Build")

Executed every 30 days (or on the last Friday of the month).

| Leg | Action | Contract Specs | Notional Size | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| **A: Yield Call** | Sell | 30-45 DTE / 0.10 Delta | 0.5 Contracts | Collects BTC premium; caps upside. |
| **B: Yield Put** | Sell | 30-45 DTE / 0.10 Delta | 0.2 Contracts | Collects BTC premium; generates yield in flat markets. |
| **C: Crash Hedge** | Buy | 30-45 DTE / 0.03 Delta | 0.6 Contracts | Geometric Multiplier. Prints BTC during crashes. |
| **D: Moon Hedge** | Buy | 30-45 DTE / 0.02 Delta | 1.0 Contracts | Protects against vertical upside; prevents equity erosion. |

*   **Net Ratio:** 3:1 Long-to-Short on Puts. This ensures that a 90k $\to$ 55k move results in a **Net Gain** of BTC.

## Pillar 3: Risk Management (The "Guardrails")

*   **Margin Utilization:** Initial Margin (IM) must never exceed 25% of the 1 BTC stack.
*   **The Inverse Floor:** Total Short Call contracts must never exceed 1.0 per 1 BTC collateral. This makes upside liquidation mathematically impossible.
*   **Path Independence:** No Stop-Losses. The "Crash Hedge" (Leg C) acts as a structural stop-loss that pays out more the faster the price drops.

## Pillar 4: Management & Adjustments

*   **The "Traveler" Rule:** If BTC price is between the Short Put and Short Call at 7 DTE, let all expire.
*   **Upside Test:** If Spot hits the Short Call strike (0.50 Delta), roll the call **Up and Out** to the next monthly cycle at 0.10 Delta.
*   **Downside Surge:** If Spot hits the Short Put strike, do nothing. Your Long Puts (Leg C) are now in the "Geometric Payout Zone" and will start accumulating BTC for you.

## Pillar 5: Exit & Profit Taking

*   **Standard Exit:** Close the entire "Strangle" (Legs A & B) when they have reached 75% of max profit.
*   **Reinvestment:** Reinvest 100% of collected premiums back into the "Moon Hedge" (Leg D) or add to the 1 BTC base stack.

## Pillar 6: Mindset & Review

*   **The "Landlord" Mantra:** I am renting out my BTC. If the "tenant" (the market) leaves, I still own the "house" (the BTC), and my "insurance" (the long puts) just paid me a massive settlement in more bricks.
*   **Metric of Success:** Monthly "Satoshi Growth Rate." Ignore the USD "Unrealized P&L" during the month; only focus on the Settled BTC Balance at the end of the cycle.

---

### Dashboard Indicators (Web App Requirements)

When building the tracking app, the dashboard should implement these indicators:

1.  **Satoshi Multiplier:** `(Current BTC / Initial 1.0 BTC) - 1`
2.  **Convexity Score:** The ratio of Long Put Gamma to Short Put Gamma.
3.  **Liquidation Distance:** Percentage price drop required before maintenance margin hits 100% (accounting for the nonlinear decay of BTC collateral value).
