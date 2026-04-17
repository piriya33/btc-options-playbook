import csv
import os
import sys
from datetime import datetime

# Add src to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from database.session import init_db, SessionLocal
from database.models import DVOLHistory

def ingest_csv(filepath: str):
    init_db()
    db = SessionLocal()
    
    try:
        with open(filepath, 'r') as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                try:
                    # time is unix timestamp in seconds
                    timestamp = int(row['time'])
                    dt = datetime.utcfromtimestamp(timestamp)
                    dvol_val = float(row['close'])
                    
                    # check if exists
                    existing = db.query(DVOLHistory).filter(DVOLHistory.date == dt).first()
                    if not existing:
                        record = DVOLHistory(date=dt, dvol=dvol_val)
                        db.add(record)
                        count += 1
                except Exception as e:
                    print(f"Skipping row error: {e}")
            
            db.commit()
            print(f"Successfully ingested {count} new DVOL records.")
    except Exception as e:
        print(f"Error reading file: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        ingest_csv(sys.argv[1])
    else:
        print("Please provide the path to the CSV file.")
