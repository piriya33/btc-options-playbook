from datetime import datetime, timedelta
from sqlalchemy import func
from database.session import SessionLocal
from database.models import DVOLHistory, TradeGroup, TradeLeg, AppSettings

def get_iv_rank_30d(current_dvol: float = None) -> dict:
    db = SessionLocal()
    try:
        # Get data from the last 30 days
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
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
            "max": round(max_dvol, 2)
        }
    finally:
        db.close()

def get_initial_btc_equity() -> float:
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == 'initial_btc_equity').first()
        return setting.value if setting else 1.0
    finally:
        db.close()

def get_realized_pnl_for_group(trade_id: str) -> float:
    db = SessionLocal()
    try:
        group = db.query(TradeGroup).filter(TradeGroup.trade_id == trade_id).first()
        return group.realized_pnl if group else 0.0
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

def get_trade_group_for_instrument(instrument_name: str) -> str:
    db = SessionLocal()
    try:
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if leg and leg.group:
            return leg.group.trade_id
        return "Ungrouped"
    finally:
        db.close()
        
def add_instrument_to_group(trade_id: str, instrument_name: str) -> bool:
    db = SessionLocal()
    try:
        # Find or create group
        group = db.query(TradeGroup).filter(TradeGroup.trade_id == trade_id).first()
        if not group:
            group = TradeGroup(trade_id=trade_id)
            db.add(group)
            db.commit()
            
        # Check if leg already exists
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if leg:
            leg.group_id = group.id
        else:
            # We don't have direction/amount right now from the command, just map it
            leg = TradeLeg(group_id=group.id, instrument_name=instrument_name, direction="unknown", amount=0)
            db.add(leg)
            
        db.commit()
        return True
    except Exception as e:
        print(f"Error grouping: {e}")
        return False
    finally:
        db.close()

def remove_instrument_from_group(instrument_name: str) -> bool:
    db = SessionLocal()
    try:
        leg = db.query(TradeLeg).filter(TradeLeg.instrument_name == instrument_name).first()
        if leg:
            db.delete(leg)
            db.commit()
            return True
        return False
    finally:
        db.close()
