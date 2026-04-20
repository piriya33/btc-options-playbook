"""
Tests for AIRSAnalyzer directive rules and portfolio invariants.

Time is frozen to 2026-04-20 so instrument DTE is deterministic:
  - BTC-20MAY26-*  →  30 DTE
  - BTC-25APR26-*  →   5 DTE
"""

import pytest
from freezegun import freeze_time
from analyzer.logic import AIRSAnalyzer
from tests.conftest import make_pos, make_account

# ── Helpers ────────────────────────────────────────────────────────────────────

SPOT = 75_000.0
FROZEN_DATE = "2026-04-20"

# Instrument stubs: 30 DTE
CALL_30 = "BTC-20MAY26-92000-C"    # short call, strike 92000
PUT_30  = "BTC-20MAY26-62000-P"    # short put,  strike 62000

# Instrument stubs: 5 DTE (near expiry)
CALL_5  = "BTC-25APR26-92000-C"
PUT_5   = "BTC-25APR26-62000-P"


def _analyzer(positions, spot=SPOT, equity=10.0, margin=0.5):
    return AIRSAnalyzer(
        spot_price=spot,
        positions=positions,
        account_summary=make_account(equity=equity, initial_margin=margin),
    )


def _directive(positions, spot=SPOT, equity=10.0, margin=0.5):
    return _analyzer(positions, spot, equity, margin).analyze_positions()


# ── Short call rules ───────────────────────────────────────────────────────────

@freeze_time(FROZEN_DATE)
def test_short_call_spot_above_strike_rolls():
    """Spot >= strike on a short call must trigger ROLL."""
    pos = make_pos(CALL_30, size=-2, delta=-1.0)   # spot=75000 < 92000, so let's use a lower strike
    call_itm = "BTC-20MAY26-70000-C"               # strike 70000 < spot 75000
    pos = make_pos(call_itm, size=-2, delta=-1.0)
    directives = _directive([pos], spot=75_000)
    assert len(directives) == 1
    assert directives[0]["status"] == "ROLL"
    assert "Roll" in directives[0]["directive"]


@freeze_time(FROZEN_DATE)
def test_short_call_high_delta_rolls():
    """Short call with |contract_delta| >= 0.50 triggers ROLL regardless of spot vs strike."""
    # size=-2, delta=-1.5  →  contract_delta = -1.5/-2 = 0.75  ≥ 0.50
    pos = make_pos(CALL_30, size=-2, delta=-1.5)
    directives = _directive([pos])
    assert directives[0]["status"] == "ROLL"


@freeze_time(FROZEN_DATE)
def test_short_call_normal_hold():
    """Short call with low delta and spot < strike stays HOLD."""
    # size=-2, delta=-0.20  →  contract_delta = 0.10  <  0.50; spot 75k < 92k strike
    pos = make_pos(CALL_30, size=-2, delta=-0.20)
    directives = _directive([pos])
    assert directives[0]["status"] == "HOLD"


@freeze_time(FROZEN_DATE)
def test_short_call_near_expiry_directive():
    """Short call with DTE <= 7 gets expiry directive but not ROLL status."""
    pos = make_pos(CALL_5, size=-2, delta=-0.20)
    directives = _directive([pos])
    assert directives[0]["status"] == "HOLD"
    assert "7 days" in directives[0]["directive"]


# ── Short put rules (antifragile) ─────────────────────────────────────────────

@freeze_time(FROZEN_DATE)
def test_short_put_below_strike_is_hold():
    """
    Antifragile rule: spot <= strike is NOT a roll trigger for short puts.
    Leg C (crash hedge) covers it.  Status must remain HOLD.
    """
    # strike 62000, spot 75000 > strike — actually for a put we want spot < strike
    put_itm = "BTC-20MAY26-90000-P"   # strike 90000, spot 75000 → spot < strike (ITM)
    # delta is positive for short put (collected premium decays as spot falls)
    # contract_delta = 0.30 / -2 = -0.15, abs = 0.15 < 0.50 → HOLD
    pos = make_pos(put_itm, size=-2, delta=0.30)
    directives = _directive([pos], spot=75_000)
    assert directives[0]["status"] == "HOLD", (
        "Spot below strike must NOT trigger ROLL on short put (antifragile rule)"
    )


@freeze_time(FROZEN_DATE)
def test_short_put_deep_itm_rolls():
    """Short put with |contract_delta| >= 0.50 must ROLL (deep ITM breach)."""
    # size=-2, delta=1.2  →  contract_delta = 1.2/-2 = -0.60  → abs 0.60 ≥ 0.50
    pos = make_pos(PUT_30, size=-2, delta=1.2)
    directives = _directive([pos])
    assert directives[0]["status"] == "ROLL"
    assert "deep ITM" in directives[0]["directive"]


@freeze_time(FROZEN_DATE)
def test_short_put_near_expiry_directive():
    """Short put with DTE <= 7 gets expiry directive, not ROLL."""
    pos = make_pos(PUT_5, size=-2, delta=0.30)
    directives = _directive([pos])
    assert directives[0]["status"] == "HOLD"
    assert "7 days" in directives[0]["directive"]


# ── Long hedge rules ───────────────────────────────────────────────────────────

@freeze_time(FROZEN_DATE)
def test_long_hold():
    """Long hedge with DTE > 7 stays HOLD."""
    pos = make_pos(CALL_30, size=9, delta=0.27)
    directives = _directive([pos])
    assert directives[0]["status"] == "HOLD"
    assert directives[0]["directive"] == "Long hedge active."


@freeze_time(FROZEN_DATE)
def test_long_near_expiry_directive():
    pos = make_pos(CALL_5, size=9, delta=0.27)
    directives = _directive([pos])
    assert "7 days" in directives[0]["directive"]


# ── Zero size filtered ─────────────────────────────────────────────────────────

@freeze_time(FROZEN_DATE)
def test_zero_size_positions_ignored():
    pos = make_pos(CALL_30, size=0, delta=0)
    directives = _directive([pos])
    assert directives == [] or all("action" in d for d in directives)


# ── Margin analysis ────────────────────────────────────────────────────────────

def test_margin_safe():
    a = _analyzer([], equity=10.0, margin=0.5)   # 5% util
    info = a.analyze_margin()
    assert info["margin_status"] == "Safe"
    assert info["margin_utilization_pct"] == pytest.approx(5.0)


def test_margin_warning():
    a = _analyzer([], equity=10.0, margin=2.2)   # 22% util
    assert a.analyze_margin()["margin_status"] == "Warning"


def test_margin_breach():
    a = _analyzer([], equity=10.0, margin=2.6)   # 26% util
    assert a.analyze_margin()["margin_status"] == "Breach"


def test_margin_critical():
    a = _analyzer([], equity=10.0, margin=4.5)   # 45% util
    assert a.analyze_margin()["margin_status"] == "Critical"


# ── Portfolio invariants ───────────────────────────────────────────────────────

def _empty_role():
    return {"size": 0.0, "delta": 0.0, "gamma": 0.0, "pnl": 0.0, "count": 0}


def _by_role(yield_call=0, yield_put=0, crash_hedge=0, moon_hedge=0,
             gc=0.0, gd=0.0, ga=0.0, gb=0.0):
    """Build a by_role dict for _compute_invariants."""
    return {
        "yield_call":  {**_empty_role(), "size": yield_call, "gamma": ga},
        "yield_put":   {**_empty_role(), "size": yield_put,  "gamma": gb},
        "crash_hedge": {**_empty_role(), "size": crash_hedge,"gamma": gc},
        "moon_hedge":  {**_empty_role(), "size": moon_hedge, "gamma": gd},
        "untagged":    _empty_role(),
    }


def _inv(by_role, equity=10.0, margin=5.0):
    a = AIRSAnalyzer(spot_price=75000, positions=[])
    return a._compute_invariants(by_role, equity_btc=equity, margin_pct=margin)


def test_put_ratio_ok():
    inv = _inv(_by_role(yield_put=0.3, crash_hedge=0.9))
    assert inv["put_ratio"]["value"] == pytest.approx(3.0)
    assert inv["put_ratio"]["ok"] is True


def test_put_ratio_fail():
    inv = _inv(_by_role(yield_put=0.3, crash_hedge=0.6))
    assert inv["put_ratio"]["value"] == pytest.approx(2.0)
    assert inv["put_ratio"]["ok"] is False


def test_call_floor_ok():
    inv = _inv(_by_role(yield_call=0.5), equity=10.0)
    assert inv["call_floor"]["value"] == pytest.approx(0.05)
    assert inv["call_floor"]["ok"] is True


def test_call_floor_breached():
    inv = _inv(_by_role(yield_call=15.0), equity=10.0)
    assert inv["call_floor"]["ok"] is False


def test_convexity_ok():
    # long gamma (C+D) > short gamma (A+B)
    inv = _inv(_by_role(crash_hedge=9, moon_hedge=15, yield_call=5, yield_put=3,
                        gc=0.02, gd=0.01, ga=0.01, gb=0.01))
    # long_gamma = 9*0.02 + 15*0.01 = 0.18+0.15 = 0.33
    # short_gamma = 5*0.01 + 3*0.01 = 0.08
    assert inv["convexity"]["ok"] is True


def test_convexity_fail():
    # long gamma < short gamma
    inv = _inv(_by_role(crash_hedge=1, moon_hedge=1, yield_call=10, yield_put=10,
                        gc=0.01, gd=0.01, ga=0.05, gb=0.05))
    assert inv["convexity"]["ok"] is False


def test_margin_invariant_ok():
    inv = _inv(_by_role(), margin=20.0)
    assert inv["margin"]["ok"] is True


def test_margin_invariant_fail():
    inv = _inv(_by_role(), margin=26.0)
    assert inv["margin"]["ok"] is False
