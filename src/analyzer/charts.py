import io
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for server use
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from typing import List, Dict, Any


def _option_payoff_at_expiry(opt_type: str, strike: float, size: float, avg_price: float, spot: float) -> float:
    """
    Calculate the P&L of an option position at expiry for a given spot price.
    
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
    # P&L per contract = intrinsic_btc - avg_price (premium paid/received)
    return size * (intrinsic_btc - avg_price)


def generate_payoff_chart(positions: List[Dict[str, Any]], spot_price: float) -> io.BytesIO:
    """
    Generate a P&L at expiry payoff chart for all open option positions.
    Returns a BytesIO PNG image buffer.
    """
    # Build price range: -50% to +100% of spot
    price_range = np.linspace(spot_price * 0.40, spot_price * 2.0, 500)
    
    # Aggregate payoff across all positions
    total_pnl = np.zeros(len(price_range))
    leg_lines = []

    for pos in positions:
        name = pos.get("instrument_name", "")
        size = pos.get("size", 0)
        if size == 0:
            continue  # Skip ghost positions
        parts = name.split('-')
        if len(parts) != 4:
            continue
        try:
            strike = float(parts[2])
            opt_type = parts[3]
            size = pos.get("size", 0)
            avg_price = pos.get("average_price", 0)
            
            leg_pnl = np.array([_option_payoff_at_expiry(opt_type, strike, size, avg_price, s) for s in price_range])
            total_pnl += leg_pnl
            
            label = f"{'Long' if size > 0 else 'Short'} {name.split('-')[-2]}{opt_type}"
            leg_lines.append((price_range, leg_pnl, label, size > 0))
        except Exception:
            continue

    # --- Plot ---
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor('#0f0f1a')
    ax.set_facecolor('#0f0f1a')

    # Individual legs (faint)
    for x, y, label, is_long in leg_lines:
        color = '#3b82f6' if is_long else '#ef4444'
        ax.plot(x, y, color=color, alpha=0.25, linewidth=1, linestyle='--')

    # Total P&L (bold)
    ax.fill_between(price_range, total_pnl, 0,
                    where=(total_pnl >= 0), color='#22c55e', alpha=0.25)
    ax.fill_between(price_range, total_pnl, 0,
                    where=(total_pnl < 0), color='#ef4444', alpha=0.25)
    ax.plot(price_range, total_pnl, color='#f0f0f0', linewidth=2.5, label='Total P&L at Expiry')

    # Zero line
    ax.axhline(0, color='#6b7280', linewidth=0.8, linestyle='-')

    # Current spot marker
    ax.axvline(spot_price, color='#facc15', linewidth=1.5, linestyle=':', label=f'Spot ${spot_price:,.0f}')

    # Formatting
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'${x/1000:.0f}k'))
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f'{y:.3f} BTC'))
    ax.set_xlabel('BTC Price at Expiry', color='#9ca3af', fontsize=11)
    ax.set_ylabel('P&L (BTC)', color='#9ca3af', fontsize=11)
    ax.set_title('📊 AIRS Portfolio — P&L at Expiry', color='#f0f0f0', fontsize=14, fontweight='bold', pad=16)
    ax.tick_params(colors='#9ca3af')
    for spine in ax.spines.values():
        spine.set_color('#374151')

    ax.legend(loc='upper left', facecolor='#1f2937', edgecolor='#374151', labelcolor='#f0f0f0', fontsize=9)
    ax.grid(True, color='#1f2937', linewidth=0.8)

    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
