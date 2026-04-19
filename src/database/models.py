from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, UTC

Base = declarative_base()

def _now():
    return datetime.now(UTC).replace(tzinfo=None)


class AppSettings(Base):
    __tablename__ = 'app_settings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(Float, nullable=False)


class DVOLHistory(Base):
    __tablename__ = 'dvol_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, default=_now, unique=True)
    dvol = Column(Float, nullable=False)


class TradeGroup(Base):
    __tablename__ = 'trade_groups'

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String, unique=True, index=True)
    strategy_type = Column(String, default='AIRS')
    created_at = Column(DateTime, default=_now)
    status = Column(String, default='OPEN')
    realized_pnl = Column(Float, default=0.0)

    legs = relationship("TradeLeg", back_populates="group")


class TradeLeg(Base):
    __tablename__ = 'trade_legs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('trade_groups.id'))
    instrument_name = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    amount = Column(Float, nullable=False)

    group = relationship("TradeGroup", back_populates="legs")
