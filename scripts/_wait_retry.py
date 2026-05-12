import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from whatsapp_automation import config
from whatsapp_automation.jobqueue import store as queue_store

txn_id = sys.argv[1]
deadline = time.time() + 40
while time.time() < deadline:
    if queue_store.is_txn_processed(txn_id):
        print("DONE (in processed_payments)")
        break
    conn = sqlite3.connect(config.QUEUE_DB_PATH)
    row = conn.execute(
        "SELECT status, attempts, last_error FROM jobs WHERE txn_id=?",
        (txn_id,),
    ).fetchone()
    conn.close()
    if row is None:
        print("(no job row)")
    else:
        err = (row[2] or "")[:80]
        print(f"  status={row[0]} attempts={row[1]} err={err}")
        if row[0] == "done":
            print("DONE")
            break
        if row[0] == "failed":
            print("FAILED")
            break
    time.sleep(2)
