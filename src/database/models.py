from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, UTC

Base = declarative_base()

def _now():
    return datetime.now(UTC).replace(tzinfo=None)


# ── Valid role/spread constants (single source of truth) ──────────────────────
VALID_ROLES = ('yield_call', 'yield_put', 'crash_hedge', 'moon_hedge')

ROLE_ALIASES = {
    'A': 'yield_call', 'a': 'yield_call',
    'B': 'yield_put',  'b': 'yield_put',
    'C': 'crash_hedge','c': 'crash_hedge',
    'D': 'moon_hedge', 'd': 'moon_hedge',
    'yield_call':  'yield_call',
    'yield_put':   'yield_put',
    'crash_hedge': 'crash_hedge',
    'moon_hedge':  'moon_hedge',
}

# Legs A+D form the call spread; Legs B+C form the put spread
ROLE_TO_SPREAD = {
    'yield_call':  'call_spread',
    'moon_hedge':  'call_spread',
    'yield_put':   'put_spread',
    'crash_hedge': 'put_spread',
}

ROLE_LABELS = {
    'yield_call':  'A – Yield Call',
    'yield_put':   'B – Yield Put',
    'crash_hedge': 'C – Crash Hedge',
    'moon_hedge':  'D – Moon Hedge',
}


class AppSettings(Base):
    __tablename__ = 'app_settings'

    id    = Column(Integer, primary_key=True, autoincrement=True)
    key   = Column(String,  unique=True, nullable=False)
    value = Column(Float,   nullable=False)


class DVOLHistory(Base):
    __tablename__ = 'dvol_history'

    id   = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, default=_now, unique=True)
    dvol = Column(Float, nullable=False)


class Campaign(Base):
    """A named collection of two spreads, e.g. 'MAY-2026'."""
    __tablename__ = 'campaigns'

    id           = Column(Integer, primary_key=True, autoincrement=True)
    name         = Column(String, unique=True, index=True, nullable=False)
    status       = Column(String, default='OPEN')    # OPEN / CLOSED
    created_at   = Column(DateTime, default=_now)
    realized_pnl = Column(Float, default=0.0)

    spreads = relationship("Spread", back_populates="campaign", cascade="all, delete-orphan")


class Spread(Base):
    """One of the two spreads within a Campaign: call_spread (A+D) or put_spread (B+C)."""
    __tablename__ = 'spreads'

    id           = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id  = Column(Integer, ForeignKey('campaigns.id'), nullable=False)
    spread_type  = Column(String, nullable=False)   # 'call_spread' or 'put_spread'
    realized_pnl = Column(Float, default=0.0)

    campaign = relationship("Campaign", back_populates="spreads")
    legs     = relationship("TradeLeg", back_populates="spread", cascade="all, delete-orphan")


class TradeLeg(Base):
    """A single option instrument tagged with its role within a Spread."""
    __tablename__ = 'trade_legs'

    id              = Column(Integer, primary_key=True, autoincrement=True)
    spread_id       = Column(Integer, ForeignKey('spreads.id'), nullable=False)
    instrument_name = Column(String, nullable=False, unique=True)
    role            = Column(String, nullable=False)   # yield_call / yield_put / crash_hedge / moon_hedge
    realized_pnl    = Column(Float, default=0.0)

    spread = relationship("Spread", back_populates="legs")
