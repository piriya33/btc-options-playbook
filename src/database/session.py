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
    # create_all is a no-op for tables that already exist — tags/campaigns are preserved.
    # The old Phase 2 DROP TABLE trade_legs has been removed; do not add it back.
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
