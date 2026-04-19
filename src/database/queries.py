from datetime import datetime, timedelta, UTC
from database.session import SessionLocal
from database.models import (
    DVOLHistory, Campaign, Spread, TradeLeg, AppSettings,
    ROLE_ALIASES, ROLE_TO_SPREAD,
)


# ── DVOL / IV Rank ─────────────────────────────────────────────────────────────

def get_iv_rank_30d(current_dvol: float = None) -> dict:
    db = SessionLocal()
    try:
        thirty_days_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
        records = db.query(DVOLHistory).filter(DVOLHistory.date >= thirty_days_ago).all()

        if not records and current_dvol is None:
            return {"rank": 0.0, "current": 0.0, "min": 0.0, "max": 0.0}

        dvols = [r.dvol for r in records]
        if current_dvol is not None:
            dvols.append(current_dvol)

        if not dvols:
            return {"rank": 0.0, "current": 0.0, "min": 0.0, "max": 0.0}

        max_dvol = max(dvols)
        min_dvol = min(dvols)

        if max_dvol == min_dvol:
            return {"rank": 50.0, "current": current_dvol, "min": min_dvol, "max": max_dvol}

        latest_dvol = current_dvol if current_dvol is not None else dvols[-1]
        iv_rank = ((latest_dvol - min_dvol) / (max_dvol - min_dvol)) * 100
        return {
            "rank": round(iv_rank, 2),
            "current": round(latest_dvol, 2),
            "min": round(min_dvol, 2),
            "max": round(max_dvol, 2),
        }
    finally:
        db.close()


# ── App Settings ───────────────────────────────────────────────────────────────

def get_initial_btc_equity() -> float:
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == 'initial_btc_equity').first()
        return setting.value if setting else 1.0
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
        return True, f"Tagged {instrument_name} as *{role}* in campaign *{campaign_name}* ({spread_type})."
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
