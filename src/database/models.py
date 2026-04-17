from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()

class AppSettings(Base):
    """Stores global application settings like initial equity baseline."""
    __tablename__ = 'app_settings'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(Float, nullable=False)

class DVOLHistory(Base):
    """Tracks daily DVOL to calculate 30-day IV Rank locally."""
    __tablename__ = 'dvol_history'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, default=datetime.utcnow, unique=True)
    dvol = Column(Float, nullable=False)

class TradeGroup(Base):
    """Groups individual option legs into a logical AIRS spread."""
    __tablename__ = 'trade_groups'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String, unique=True, index=True) # e.g., 'AIRS-2026-04'
    strategy_type = Column(String, default='AIRS')
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default='OPEN') # OPEN, CLOSED
    realized_pnl = Column(Float, default=0.0)
    
    legs = relationship("TradeLeg", back_populates="group")

class TradeLeg(Base):
    """Maps a Deribit instrument to a TradeGroup."""
    __tablename__ = 'trade_legs'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('trade_groups.id'))
    instrument_name = Column(String, nullable=False) # e.g., 'BTC-28JUN24-65000-C'
    direction = Column(String, nullable=False) # 'buy' or 'sell'
    amount = Column(Float, nullable=False)
    
    group = relationship("TradeGroup", back_populates="legs")
