from datetime import datetime, timedelta, UTC
from database.session import SessionLocal
from database.models import (
    DVOLHistory, Campaign, Spread, TradeLeg, AppSettings,
    ROLE_ALIASES, ROLE_TO_SPREAD,
)


# ── DVOL helpers ───────────────────────────────────────────────────────────────

def get_latest_dvol_date():
    """Return the most recent DVOLHistory.date, or None if the table is empty."""
    db = SessionLocal()
    try:
        record = db.query(DVOLHistory).order_by(DVOLHistory.date.desc()).first()
        return record.date if record else None
    finally:
        db.close()


def upsert_dvol_candles(candles: list):
    """
    Upsert a list of [timestamp_ms, open, high, low, close] candles into DVOLHistory.
    Uses the close value (index 4). Idempotent — safe to call repeatedly.
    """
    db = SessionLocal()
    try:
        for candle in candles:
            ts_ms  = candle[0]
            close  = candle[4]
            dt = datetime.fromtimestamp(ts_ms / 1000, UTC).replace(
                hour=0, minute=0, second=0, microsecond=0, tzinfo=None
            )
            existing = db.query(DVOLHistory).filter(DVOLHistory.date == dt).first()
            if existing:
                existing.dvol = close
            else:
                db.add(DVOLHistory(date=dt, dvol=close))
        db.commit()
    finally:
        db.close()


# ── DVOL / IV Rank ─────────────────────────────────────────────────────────────

def get_dvol_row_count() -> int:
    db = SessionLocal()
    try:
        return db.query(DVOLHistory).count()
    finally:
        db.close()


def _get_iv_rank_for_window(window_days: int, current_dvol: float = None) -> dict:
    """Internal: compute IV rank over an arbitrary lookback window."""
    db = SessionLocal()
    try:
        cutoff  = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=window_days)
        records = db.query(DVOLHistory).filter(DVOLHistory.date >= cutoff).all()
        dvols   = [r.dvol for r in records]
        if current_dvol is not None:
            dvols.append(current_dvol)
        if not dvols:
            return {"rank": 0.0, "min": 0.0, "max": 0.0}
        lo, hi = min(dvols), max(dvols)
        latest = current_dvol if current_dvol is not None else dvols[-1]
        rank   = ((latest - lo) / (hi - lo) * 100) if hi != lo else 50.0
        return {"rank": round(rank, 1), "min": round(lo, 2), "max": round(hi, 2)}
    finally:
        db.close()


def get_iv_ranks(current_dvol: float = None) -> dict:
    """
    Return IV rank for both 30d and 252d windows in a single dict.
    'rank' is aliased to rank_252d (the primary strategic rank).
    """
    r30  = _get_iv_rank_for_window(30,  current_dvol)
    r252 = _get_iv_rank_for_window(252, current_dvol)
    current = current_dvol if current_dvol is not None else 0.0
    return {
        "current":   round(current, 2),
        "rank_30d":  r30["rank"],
        "min_30d":   r30["min"],
        "max_30d":   r30["max"],
        "rank_252d": r252["rank"],
        "min_252d":  r252["min"],
        "max_252d":  r252["max"],
        "rank":      r252["rank"],   # primary rank = 1-year window
    }


# ── App Settings ───────────────────────────────────────────────────────────────

def get_initial_btc_equity() -> float:
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == 'initial_btc_equity').first()
        return setting.value if setting else 1.0
    finally:
        db.close()


def set_initial_btc_equity(value: float):
    """Upsert the Satoshi Growth baseline (BTC-denominated equity at t0)."""
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == 'initial_btc_equity').first()
        if setting:
            setting.value = float(value)
        else:
            db.add(AppSettings(key='initial_btc_equity', value=float(value)))
        db.commit()
    finally:
        db.close()


def get_morning_push_chat_id() -> int:
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == 'morning_push_chat_id').first()
        return int(setting.value) if setting and setting.value != 0 else None
    finally:
        db.close()


def set_morning_push_chat_id(chat_id: int):
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == 'morning_push_chat_id').first()
        if setting:
            setting.value = float(chat_id)
        else:
            db.add(AppSettings(key='morning_push_chat_id', value=float(chat_id)))
        db.commit()
    finally:
        db.close()


# ── Campaign / Spread / Leg tagging ───────────────────────────────────────────

def tag_instrument(instrument_name: str, role_input: str, campaign_name: str) -> tuple:
    """
    Assign an instrument to a Campaign under the correct Spread, with a role tag.

    role_input accepts: A/B/C/D (shorthand) or the full role name.
    Returns (success: bool, message: str).
    """
    role = ROLE_ALIASES.get(role_input)
    if role is None:
        valid = "A/B/C/D or yield_call/yield_put/crash_hedge/moon_hedge"
        return False, f"Unknown role '{role_input}'. Use {valid}."

    spread_type = ROLE_TO_SPREAD[role]

    db = SessionLocal()
    try:
        # Find or create Campaign
        campaign = db.query(Campaign).filter(Campaign.name == campaign_name).first()
        if not campaign:
            campaign = Campaign(name=campaign_name)
            db.add(campaign)
            db.flush()

        # Find or create the Spread of the correct type within this Campaign
        spread = db.query(Spread).filter(
            Spread.campaign_id == campaign.id,
            Spread.spread_type == spread_type,
        ).first()
        if not spread:
            spread = Spread(campaign_id=campaign.id, spread_type=spread_type)
            db.add(spread)
            db.flush()

        # Upsert the TradeLeg
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if leg:
            leg.spread_id = spread.id
            leg.role = role
        else:
            leg = TradeLeg(spread_id=spread.id, instrument_name=instrument_name, role=role)
            db.add(leg)

        db.commit()
        return True, f"Tagged {instrument_name} as {role} in campaign {campaign_name} ({spread_type})."
    except Exception as e:
        db.rollback()
        return False, f"DB error: {e}"
    finally:
        db.close()


def untag_instrument(instrument_name: str) -> tuple:
    """Remove an instrument's tag. Returns (success, message)."""
    db = SessionLocal()
    try:
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if not leg:
            return False, f"{instrument_name} is not tagged."
        db.delete(leg)
        db.commit()
        return True, f"Untagged {instrument_name}."
    except Exception as e:
        db.rollback()
        return False, f"DB error: {e}"
    finally:
        db.close()


def get_leg_info(instrument_name: str) -> dict:
    """
    Returns tag metadata for an instrument, or {} if untagged.
    Keys: role, spread_type, campaign_name, spread_id, campaign_id, spread_realized_pnl
    """
    db = SessionLocal()
    try:
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if leg and leg.spread and leg.spread.campaign:
            return {
                "role":                leg.role,
                "spread_type":         leg.spread.spread_type,
                "campaign_name":       leg.spread.campaign.name,
                "spread_id":           leg.spread.id,
                "campaign_id":         leg.spread.campaign.id,
                "spread_realized_pnl": leg.spread.realized_pnl,
            }
        return {}
    finally:
        db.close()


def close_leg(instrument_name: str, realized_pnl_btc: float) -> tuple:
    """
    Record that a leg was closed. Propagates realized PnL to the parent Spread
    and Campaign. Does NOT delete the leg — keeps history.
    Returns (success: bool, message: str).
    """
    db = SessionLocal()
    try:
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if not leg:
            return False, f"{instrument_name} is not tagged — PnL not recorded."
        leg.realized_pnl          += realized_pnl_btc
        leg.spread.realized_pnl   += realized_pnl_btc
        leg.spread.campaign.realized_pnl += realized_pnl_btc
        db.commit()
        return True, f"Recorded {realized_pnl_btc:+.5f} BTC realized PnL for {instrument_name}."
    except Exception as e:
        db.rollback()
        return False, f"DB error recording PnL: {e}"
    finally:
        db.close()


def list_legs_for_campaign(campaign_name: str) -> list:
    """Returns all legs for a campaign as plain dicts, ordered by spread then role."""
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.name == campaign_name).first()
        if not campaign:
            return []
        result = []
        for spread in campaign.spreads:
            for leg in spread.legs:
                result.append({
                    "instrument_name": leg.instrument_name,
                    "role":            leg.role,
                    "spread_type":     spread.spread_type,
                    "realized_pnl":    leg.realized_pnl,
                })
        return result
    finally:
        db.close()


def get_all_open_campaigns() -> list:
    """Returns all open campaigns as plain dicts (safe outside DB session)."""
    db = SessionLocal()
    try:
        campaigns = db.query(Campaign).filter(Campaign.status == 'OPEN').all()
        result = []
        for c in campaigns:
            spreads = []
            for s in c.spreads:
                legs = [
                    {
                        "instrument_name": l.instrument_name,
                        "role": l.role,
                        "realized_pnl": l.realized_pnl,
                    }
                    for l in s.legs
                ]
                spreads.append({
                    "id": s.id,
                    "spread_type": s.spread_type,
                    "realized_pnl": s.realized_pnl,
                    "legs": legs,
                })
            result.append({
                "name": c.name,
                "status": c.status,
                "realized_pnl": c.realized_pnl,
                "spreads": spreads,
            })
        return result
    finally:
        db.close()


def get_realized_pnl_for_spread(spread_id: int) -> float:
    db = SessionLocal()
    try:
        spread = db.query(Spread).filter(Spread.id == spread_id).first()
        return spread.realized_pnl if spread else 0.0
    finally:
        db.close()


def get_realized_pnl_for_campaign(campaign_name: str) -> float:
    db = SessionLocal()
    try:
        campaign = db.query(Campaign).filter(Campaign.name == campaign_name).first()
        return campaign.realized_pnl if campaign else 0.0
    finally:
        db.close()


def get_legs_for_spread(spread_id: int) -> list:
    """Returns all legs for a given spread as plain dicts (safe outside DB session)."""
    db = SessionLocal()
    try:
        spread = db.query(Spread).filter(Spread.id == spread_id).first()
        if not spread:
            return []
        return [
            {"instrument_name": leg.instrument_name, "role": leg.role}
            for leg in spread.legs
        ]
    finally:
        db.close()


# ── Harvest alert persistence ──────────────────────────────────────────────────
# Stored as AppSettings rows with key "harvest_alerted:<instrument>" → value 1.0
# This survives bot restarts so we don't re-alert on the same leg.

def is_harvest_alerted(instrument_name: str) -> bool:
    db = SessionLocal()
    try:
        key = f"harvest_alerted:{instrument_name}"
        return db.query(AppSettings).filter(AppSettings.key == key).first() is not None
    finally:
        db.close()


def mark_harvest_alerted(instrument_name: str):
    db = SessionLocal()
    try:
        key = f"harvest_alerted:{instrument_name}"
        if not db.query(AppSettings).filter(AppSettings.key == key).first():
            db.add(AppSettings(key=key, value=1.0))
            db.commit()
    finally:
        db.close()


def clear_harvest_alerted(instrument_name: str):
    """Call when a leg is closed so it can be re-alerted if re-opened."""
    db = SessionLocal()
    try:
        key = f"harvest_alerted:{instrument_name}"
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()
