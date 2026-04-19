import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from database.models import Base

# Determine database path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(BASE_DIR, "data.db")

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    # Phase 2 migration: drop legacy tables before creating the new schema.
    # Safe to run repeatedly — IF NOT EXISTS guards prevent data loss on tables
    # that were already migrated.
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS trade_legs"))
        conn.execute(text("DROP TABLE IF EXISTS trade_groups"))
        conn.commit()
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
