"""
Tests for campaign phase detection and recycle recommendation logic.
"""

import pytest
from analyzer.logic import detect_campaign_phase, recycle_recommendation
from database.queries import tag_instrument, get_all_open_campaigns


# ── Helpers ────────────────────────────────────────────────────────────────────

def _campaign(instruments: dict) -> dict:
    """
    Build a campaign dict (matching get_all_open_campaigns() shape)
    from {instrument_name: role} mapping.
    """
    from database.models import ROLE_TO_SPREAD

    spread_map: dict = {}
    for instr, role in instruments.items():
        stype = ROLE_TO_SPREAD[role]
        spread_map.setdefault(stype, []).append({"instrument_name": instr, "role": role})

    spreads = [
        {"spread_type": stype, "legs": legs, "realized_pnl": 0.0}
        for stype, legs in spread_map.items()
    ]
    return {"name": "TEST", "status": "OPEN", "realized_pnl": 0.0, "spreads": spreads}


def _live(*instruments) -> list:
    """Build a minimal live positions list from instrument names."""
    return [{"instrument_name": i, "size": 1} for i in instruments]


# ── detect_campaign_phase ──────────────────────────────────────────────────────

INSTR_A = "BTC-29MAY26-92000-C"    # yield_call
INSTR_B = "BTC-29MAY26-62000-P"    # yield_put
INSTR_C = "BTC-29MAY26-54000-P"    # crash_hedge
INSTR_D = "BTC-29MAY26-105000-C"   # moon_hedge

ALL_4 = {INSTR_A: "yield_call", INSTR_B: "yield_put",
         INSTR_C: "crash_hedge", INSTR_D: "moon_hedge"}


def test_phase_initiated_all_four_live():
    campaign = _campaign(ALL_4)
    live = _live(INSTR_A, INSTR_B, INSTR_C, INSTR_D)
    assert detect_campaign_phase(campaign, live) == "INITIATED"


def test_phase_coasting_no_shorts_hedges_remain():
    campaign = _campaign(ALL_4)
    live = _live(INSTR_C, INSTR_D)   # only hedges
    assert detect_campaign_phase(campaign, live) == "COASTING"


def test_phase_coasting_one_hedge_remains():
    campaign = _campaign(ALL_4)
    live = _live(INSTR_C)             # single hedge
    assert detect_campaign_phase(campaign, live) == "COASTING"


def test_phase_harvested_one_short_closed():
    campaign = _campaign(ALL_4)
    live = _live(INSTR_A, INSTR_C, INSTR_D)   # B (short put) closed
    assert detect_campaign_phase(campaign, live) == "HARVESTED"


def test_phase_harvested_one_short_one_hedge_remain():
    campaign = _campaign(ALL_4)
    live = _live(INSTR_A, INSTR_C)   # only A short + C hedge
    assert detect_campaign_phase(campaign, live) == "HARVESTED"


def test_phase_empty_no_live_positions():
    campaign = _campaign(ALL_4)
    assert detect_campaign_phase(campaign, []) == "EMPTY"


def test_phase_empty_all_sizes_zero():
    campaign = _campaign(ALL_4)
    live = [{"instrument_name": i, "size": 0} for i in ALL_4]
    assert detect_campaign_phase(campaign, live) == "EMPTY"


def test_phase_ignores_untagged_positions():
    """Live positions for instruments not in the campaign must not affect phase."""
    campaign = _campaign(ALL_4)
    live = _live("BTC-29MAY26-999999-C")   # unrelated instrument
    assert detect_campaign_phase(campaign, live) == "EMPTY"


# ── recycle_recommendation ────────────────────────────────────────────────────

def test_recycle_high_dte_high_value():
    action, _ = recycle_recommendation(min_dte=25, avg_residual_pct=35.0)
    assert action == "RECYCLE"


def test_recycle_boundary_dte_21_value_21():
    action, _ = recycle_recommendation(min_dte=21, avg_residual_pct=21.0)
    assert action == "RECYCLE"


def test_roll_high_dte_low_value():
    action, _ = recycle_recommendation(min_dte=18, avg_residual_pct=10.0)
    assert action == "ROLL"


def test_roll_exactly_14_dte():
    action, _ = recycle_recommendation(min_dte=14, avg_residual_pct=50.0)
    assert action == "ROLL"


def test_coast_7_to_13_dte():
    for dte in (7, 10, 13):
        action, _ = recycle_recommendation(min_dte=dte, avg_residual_pct=50.0)
        assert action == "COAST", f"Expected COAST at {dte} DTE, got {action}"


def test_close_under_7_dte():
    for dte in (0, 3, 6):
        action, _ = recycle_recommendation(min_dte=dte, avg_residual_pct=50.0)
        assert action == "CLOSE", f"Expected CLOSE at {dte} DTE, got {action}"


def test_recommendation_includes_explanation():
    _, explanation = recycle_recommendation(min_dte=25, avg_residual_pct=35.0)
    assert len(explanation) > 10


# ── Integration: phase detection via real DB ──────────────────────────────────

def test_phase_via_db_campaigns():
    """Tag all 4 legs in DB; verify detect_campaign_phase reads them correctly."""
    tag_instrument(INSTR_A, "A", "MAY2026")
    tag_instrument(INSTR_B, "B", "MAY2026")
    tag_instrument(INSTR_C, "C", "MAY2026")
    tag_instrument(INSTR_D, "D", "MAY2026")

    campaigns = get_all_open_campaigns()
    assert len(campaigns) == 1

    # All 4 live → INITIATED
    live_all = _live(INSTR_A, INSTR_B, INSTR_C, INSTR_D)
    assert detect_campaign_phase(campaigns[0], live_all) == "INITIATED"

    # Shorts gone → COASTING
    live_hedges = _live(INSTR_C, INSTR_D)
    assert detect_campaign_phase(campaigns[0], live_hedges) == "COASTING"
