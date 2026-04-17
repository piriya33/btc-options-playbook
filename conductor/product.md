# Product Definition

**Project Name**: BTC Options Playbook
**Description**: An institutional-grade toolkit for managing BTC collateral through automated ratio spreads and convex hedging.

## Problem Statement
Existing portfolio trackers are USD-centric and fail to properly model BTC-denominated collateral constraints and inverse payouts. Furthermore, managing complex ratio spreads like AIRS manually introduces emotional friction, requires constant monitoring, and leads to calculation errors during extreme volatility.

## Target Users
Solo algorithmic/systematic traders running inverse options strategies on Deribit.

## Key Goals
1. Grow BTC stack count regardless of USD price.
2. Generate sustainable premium rent.
3. Ensure zero liquidation risk (survive 50% crash or 100% surge).
4. Provide daily clear, exact execution instructions (Strike, Delta, DTE, Reason) based on the AIRS playbook logic.
5. Group multiple legs (spreads) logically under a unique 'Trade ID' for accurate portfolio tracking and P&L monitoring.
