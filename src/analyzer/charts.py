import io
import math
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for server use
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from datetime import datetime, UTC
from typing import List, Dict, Any


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _bsm_price_btc(spot: float, strike: float, T: float, iv: float, opt_type: str) -> float:
    """
    BSM theoretical value in BTC at a hypothetical spot price.

    Deribit options are inverse-settled: price_BTC = price_USD / spot.
    iv is annualised decimal (e.g. 0.50 for 50%). r=0 simplification.
    Falls back to intrinsic when T or iv are zero (near-expiry / no vol data).
    """
    if spot <= 0:
        return 0.0
    if T <= 0 or iv <= 0:
        if opt_type == 'C':
            return max(0.0, (spot - strike) / spot)
        else:
            return max(0.0, (strike - spot) / spot)
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    if opt_type == 'C':
        price_usd = spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    else:
        price_usd = strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(0.0, price_usd) / spot


def _years_to_expiry(instrument_name: str) -> float:
    """Parse years to expiry from instrument name (e.g. BTC-26JUN26-95000-C)."""
    parts = instrument_name.split('-')
    if len(parts) == 4:
        try:
            expiry = datetime.strptime(parts[1], '%d%b%y')
            days = (expiry - datetime.now(UTC).replace(tzinfo=None)).days
            return max(days, 0) / 365.0
        except Exception:
            pass
    return 0.0


def _option_payoff_at_expiry(opt_type: str, strike: float, size: float, avg_price: float, spot: float) -> float:
    """
    P&L of an option position at expiry for a given spot price.

    Deribit BTC options are inverse-settled in BTC.
    Intrinsic value in BTC = max(0, spot - strike) / spot  (for calls)
    avg_price is already in BTC (e.g. 0.0009 BTC per contract).
    size is in BTC contracts (negative = short).
    """
    if spot <= 0:
        return 0.0
    if opt_type == 'C':
        intrinsic_btc = max(0.0, (spot - strike) / spot)
    else:
        intrinsic_btc = max(0.0, (strike - spot) / spot)
    return size * (intrinsic_btc - avg_price)


def generate_payoff_chart(positions: List[Dict[str, Any]], spot_price: float) -> io.BytesIO:
    """
    Generate a payoff chart with two curves:
      — Current P&L (BSM theoretical value at today's date, amber solid line)
      — Expiry P&L  (intrinsic value at expiration, white dashed line)

    mark_iv from Deribit positions drives the BSM curve. Falls back to intrinsic
    if mark_iv is missing or zero.
    """
    price_range = np.linspace(spot_price * 0.40, spot_price * 2.0, 500)

    total_expiry  = np.zeros(len(price_range))
    total_current = np.zeros(len(price_range))
    leg_lines = []
    has_current_curve = False

    for pos in positions:
        name = pos.get("instrument_name", "")
        size = pos.get("size", 0)
        if size == 0:
            continue
        parts = name.split('-')
        if len(parts) != 4:
            continue
        try:
            strike    = float(parts[2])
            opt_type  = parts[3]
            avg_price = pos.get("average_price", 0)
            mark_iv   = pos.get("mark_iv", 0) or 0        # Deribit: percentage, e.g. 47.5
            T         = _years_to_expiry(name)
            iv        = mark_iv / 100.0

            # Expiry payoff
            leg_expiry = np.array([
                _option_payoff_at_expiry(opt_type, strike, size, avg_price, s)
                for s in price_range
            ])
            total_expiry += leg_expiry

            # Current theoretical value (BSM)
            leg_current = np.array([
                size * (_bsm_price_btc(s, strike, T, iv, opt_type) - avg_price)
                for s in price_range
            ])
            total_current += leg_current
            if iv > 0 and T > 0:
                has_current_curve = True

            label = f"{'Long' if size > 0 else 'Short'} {parts[2]}{opt_type}"
            leg_lines.append((price_range, leg_expiry, label, size > 0))
        except Exception:
            continue

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor('#0f0f1a')
    ax.set_facecolor('#0f0f1a')

    # Individual legs (faint)
    for x, y, label, is_long in leg_lines:
        color = '#3b82f6' if is_long else '#ef4444'
        ax.plot(x, y, color=color, alpha=0.20, linewidth=1, linestyle='--')

    # Expiry P&L — dashed white, green/red fill
    ax.fill_between(price_range, total_expiry, 0,
                    where=(total_expiry >= 0), color='#22c55e', alpha=0.15)
    ax.fill_between(price_range, total_expiry, 0,
                    where=(total_expiry < 0),  color='#ef4444', alpha=0.15)
    ax.plot(price_range, total_expiry,
            color='#9ca3af', linewidth=1.8, linestyle='--', label='P&L at Expiry')

    # Current value P&L — solid amber, this is the main focus
    if has_current_curve:
        ax.fill_between(price_range, total_current, 0,
                        where=(total_current >= 0), color='#f59e0b', alpha=0.12)
        ax.fill_between(price_range, total_current, 0,
                        where=(total_current < 0),  color='#ef4444', alpha=0.20)
        ax.plot(price_range, total_current,
                color='#f59e0b', linewidth=2.5, label='Current P&L (today)')

    # Zero line
    ax.axhline(0, color='#6b7280', linewidth=0.8)

    # Current spot marker
    ax.axvline(spot_price, color='#facc15', linewidth=1.5, linestyle=':',
               label=f'Spot ${spot_price:,.0f}')

    # ── Dynamic y-axis: focus on ±40% of spot across both curves ─────────────
    central_mask = (price_range >= spot_price * 0.60) & (price_range <= spot_price * 1.40)
    curves = [total_expiry]
    if has_current_curve:
        curves.append(total_current)
    if central_mask.any():
        central_vals = np.concatenate([c[central_mask] for c in curves])
        y_lo = min(central_vals.min(), 0)
        y_hi = max(central_vals.max(), 0)
    else:
        all_vals = np.concatenate(curves)
        y_lo, y_hi = all_vals.min(), all_vals.max()
    padding = max((y_hi - y_lo) * 0.15, abs(y_lo) * 0.05, 1e-4)
    ax.set_ylim(y_lo - padding, y_hi + padding)

    # Formatting
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'${x/1000:.0f}k'))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f'{y:.3f} BTC'))
    ax.set_xlabel('BTC Price', color='#9ca3af', fontsize=11)
    ax.set_ylabel('P&L (BTC)', color='#9ca3af', fontsize=11)
    ax.set_title('📊 AIRS Portfolio — P&L', color='#f0f0f0', fontsize=14, fontweight='bold', pad=16)
    ax.tick_params(colors='#9ca3af')
    for spine in ax.spines.values():
        spine.set_color('#374151')

    ax.legend(loc='upper left', facecolor='#1f2937', edgecolor='#374151',
              labelcolor='#f0f0f0', fontsize=9)
    ax.grid(True, color='#1f2937', linewidth=0.8)

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
