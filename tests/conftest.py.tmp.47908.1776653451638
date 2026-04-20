"""
Shared fixtures for the AIRY test suite.

The key fixture is `patch_session`, which redirects every SessionLocal()
call in database.queries to a fresh in-memory SQLite DB.  Tests never
touch the real data.db file.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base


@pytest.fixture(scope="function")
def db_engine():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def patch_session(db_engine, monkeypatch):
    """Redirect all SessionLocal() calls to the per-test in-memory DB."""
    TestSession = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    monkeypatch.setattr("database.queries.SessionLocal", TestSession)


# ── Shared position / account builders ────────────────────────────────────────

def make_pos(instrument: str, size: float, delta: float,
             gamma: float = 0.01, pnl: float = 0.0,
             avg_price: float = 0.05) -> dict:
    """Build a minimal Deribit position dict for analyzer tests."""
    return {
        "instrument_name":    instrument,
        "size":               size,
        "delta":              delta,
        "gamma":              gamma,
        "floating_profit_loss": pnl,
        "average_price":      avg_price,
    }


def make_account(equity: float = 10.0, initial_margin: float = 0.5,
                 maint_margin: float = 0.2) -> dict:
    return {
        "equity":               equity,
        "initial_margin":       initial_margin,
        "maintenance_margin":   maint_margin,
    }
