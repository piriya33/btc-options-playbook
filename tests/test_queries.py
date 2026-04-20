"""
Tests for the database query layer.

All tests run against an in-memory SQLite DB via the `patch_session`
autouse fixture in conftest.py.
"""

import pytest
from database.queries import (
    tag_instrument,
    untag_instrument,
    get_leg_info,
    close_leg,
    get_all_open_campaigns,
    get_realized_pnl_for_spread,
    get_realized_pnl_for_campaign,
    get_legs_for_spread,
    list_legs_for_campaign,
    is_harvest_alerted,
    mark_harvest_alerted,
    clear_harvest_alerted,
    upsert_dvol_candles,
    get_latest_dvol_date,
)


# ── tag_instrument ─────────────────────────────────────────────────────────────

def test_tag_creates_campaign_spread_leg():
    ok, msg = tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    assert ok
    info = get_leg_info("BTC-29MAY26-92000-C")
    assert info["role"] == "yield_call"
    assert info["spread_type"] == "call_spread"
    assert info["campaign_name"] == "MAY2026"


def test_tag_unknown_role_fails():
    ok, msg = tag_instrument("BTC-29MAY26-92000-C", "Z", "MAY2026")
    assert not ok
    assert "Unknown role" in msg


def test_tag_same_instrument_upserts_not_duplicates():
    tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    ok, msg = tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    assert ok
    # Should still be exactly one leg
    legs = list_legs_for_campaign("MAY2026")
    assert len([l for l in legs if l["instrument_name"] == "BTC-29MAY26-92000-C"]) == 1


def test_tag_shorthand_aliases():
    """A/B/C/D shorthands must resolve to full role names."""
    for shorthand, expected_role in [("A", "yield_call"), ("B", "yield_put"),
                                      ("C", "crash_hedge"), ("D", "moon_hedge")]:
        instr = f"BTC-29MAY26-{shorthand}0000-C"
        tag_instrument(instr, shorthand, "TEST")
        assert get_leg_info(instr)["role"] == expected_role


def test_tag_all_four_roles_same_campaign():
    instruments = {
        "BTC-29MAY26-92000-C": "A",
        "BTC-29MAY26-62000-P": "B",
        "BTC-29MAY26-54000-P": "C",
        "BTC-29MAY26-105000-C": "D",
    }
    for instr, role in instruments.items():
        ok, _ = tag_instrument(instr, role, "MAY2026")
        assert ok

    campaigns = get_all_open_campaigns()
    assert len(campaigns) == 1
    assert campaigns[0]["name"] == "MAY2026"
    total_legs = sum(len(s["legs"]) for s in campaigns[0]["spreads"])
    assert total_legs == 4


def test_tag_creates_two_spreads():
    tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")   # call_spread
    tag_instrument("BTC-29MAY26-105000-C", "D", "MAY2026")  # call_spread
    tag_instrument("BTC-29MAY26-62000-P", "B", "MAY2026")   # put_spread
    tag_instrument("BTC-29MAY26-54000-P", "C", "MAY2026")   # put_spread

    camps = get_all_open_campaigns()
    spread_types = {s["spread_type"] for s in camps[0]["spreads"]}
    assert spread_types == {"call_spread", "put_spread"}


# ── untag_instrument ───────────────────────────────────────────────────────────

def test_untag_removes_leg():
    tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    ok, _ = untag_instrument("BTC-29MAY26-92000-C")
    assert ok
    assert get_leg_info("BTC-29MAY26-92000-C") == {}


def test_untag_nonexistent_fails():
    ok, msg = untag_instrument("BTC-29MAY26-99999-C")
    assert not ok
    assert "not tagged" in msg


# ── close_leg + PnL propagation ────────────────────────────────────────────────

def test_close_leg_propagates_pnl():
    tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    info = get_leg_info("BTC-29MAY26-92000-C")
    spread_id = info["spread_id"]

    ok, msg = close_leg("BTC-29MAY26-92000-C", 0.005)
    assert ok

    assert get_realized_pnl_for_spread(spread_id) == pytest.approx(0.005)
    assert get_realized_pnl_for_campaign("MAY2026") == pytest.approx(0.005)


def test_close_leg_accumulates_multiple():
    tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    close_leg("BTC-29MAY26-92000-C", 0.003)
    close_leg("BTC-29MAY26-92000-C", 0.002)
    assert get_realized_pnl_for_campaign("MAY2026") == pytest.approx(0.005)


def test_close_leg_untagged_fails():
    ok, msg = close_leg("BTC-29MAY26-NOPE-C", 0.001)
    assert not ok
    assert "not tagged" in msg


def test_close_leg_negative_pnl():
    """Losses must also propagate correctly."""
    tag_instrument("BTC-29MAY26-62000-P", "B", "MAY2026")
    close_leg("BTC-29MAY26-62000-P", -0.002)
    assert get_realized_pnl_for_campaign("MAY2026") == pytest.approx(-0.002)


def test_pnl_propagates_across_two_legs_same_campaign():
    tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    tag_instrument("BTC-29MAY26-62000-P", "B", "MAY2026")
    close_leg("BTC-29MAY26-92000-C", 0.004)
    close_leg("BTC-29MAY26-62000-P", 0.002)
    assert get_realized_pnl_for_campaign("MAY2026") == pytest.approx(0.006)


# ── get_legs_for_spread ────────────────────────────────────────────────────────

def test_get_legs_for_spread():
    tag_instrument("BTC-29MAY26-92000-C", "A", "MAY2026")
    tag_instrument("BTC-29MAY26-105000-C", "D", "MAY2026")
    info = get_leg_info("BTC-29MAY26-92000-C")
    legs = get_legs_for_spread(info["spread_id"])
    instruments = {l["instrument_name"] for l in legs}
    assert instruments == {"BTC-29MAY26-92000-C", "BTC-29MAY26-105000-C"}


def test_get_legs_for_nonexistent_spread():
    assert get_legs_for_spread(9999) == []


# ── Harvest alert persistence ──────────────────────────────────────────────────

def test_harvest_alert_lifecycle():
    instr = "BTC-29MAY26-92000-C"
    assert not is_harvest_alerted(instr)
    mark_harvest_alerted(instr)
    assert is_harvest_alerted(instr)
    clear_harvest_alerted(instr)
    assert not is_harvest_alerted(instr)


def test_mark_harvest_idempotent():
    instr = "BTC-29MAY26-92000-C"
    mark_harvest_alerted(instr)
    mark_harvest_alerted(instr)   # should not raise or duplicate
    assert is_harvest_alerted(instr)


# ── DVOL upsert ───────────────────────────────────────────────────────────────

def test_upsert_dvol_candles_stores_close():
    from datetime import datetime, UTC
    # Use an explicit UTC timestamp so upsert_dvol_candles (which reads UTC) lands
    # on the expected date regardless of the local machine timezone.
    dt_utc = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    ts_ms  = int(dt_utc.timestamp() * 1000)
    upsert_dvol_candles([[ts_ms, 45.0, 46.0, 44.0, 45.5]])
    latest = get_latest_dvol_date()
    assert latest is not None
    assert latest.date() == datetime(2026, 4, 19).date()


def test_upsert_dvol_candles_idempotent():
    from datetime import datetime, UTC
    dt_utc = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    ts_ms  = int(dt_utc.timestamp() * 1000)
    upsert_dvol_candles([[ts_ms, 45.0, 46.0, 44.0, 45.5]])
    upsert_dvol_candles([[ts_ms, 45.0, 46.0, 44.0, 46.0]])   # updated close
    assert get_latest_dvol_date() is not None
