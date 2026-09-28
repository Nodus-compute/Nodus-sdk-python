"""Customer-owned queue and scheduled submission using existing Nodus APIs."""
import argparse
import json
import math
import sqlite3
import uuid
from pathlib import Path

import nodus


def connect(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, value REAL NOT NULL, batch TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            workload TEXT, status TEXT NOT NULL DEFAULT 'pending')""")
    db.execute("CREATE TABLE IF NOT EXISTS completions (workload TEXT PRIMARY KEY, event_type TEXT NOT NULL)")
    return db


def enqueue(db, request_id, value):
    if not request_id or not math.isfinite(value):
        raise ValueError("A stable request ID and finite value are required")
    with db:
        previous = db.execute("SELECT value FROM requests WHERE id=?", (request_id,)).fetchone()
        if previous and previous[0] != value:
            raise ValueError("Request ID already used with different input")
        db.execute("INSERT OR IGNORE INTO requests(id,value) VALUES (?,?)", (request_id, value))


def prepare(db, budget, image, limit=64):
    db.execute("BEGIN IMMEDIATE")
    try:
        pending = db.execute("SELECT id,payload FROM batches WHERE workload IS NULL ORDER BY rowid LIMIT 1").fetchone()
        if pending:
            db.commit()
            return pending[0], json.loads(pending[1])
        rows = db.execute("SELECT id,value FROM requests WHERE batch IS NULL ORDER BY rowid LIMIT ?", (limit,)).fetchall()
        if not rows:
            db.commit()
            return None
        batch_id = "microbatch-" + uuid.uuid4().hex
        items = [{"id": item[0], "value": item[1]} for item in rows]
        payload = {"image": image, "command": ["python", "-c", Path(__file__).with_name("worker.py").read_text(), json.dumps(items)],
                   "outputs": {"results": "results.json"}, "budget": budget, "idempotency_key": batch_id}
        db.execute("INSERT INTO batches(id,payload) VALUES (?,?)", (batch_id, json.dumps(payload)))
        db.executemany("UPDATE requests SET batch=? WHERE id=?", [(batch_id, item[0]) for item in rows])
        db.commit()
        return batch_id, payload
    except BaseException:
        db.rollback()
        raise


def submit(db, client, budget, image):
    prepared = prepare(db, budget, image)
    if prepared is None:
        return None
    batch_id, payload = prepared
    workload = client.run(**payload)
    with db:
        db.execute("UPDATE batches SET workload=?,status=COALESCE((SELECT event_type FROM completions WHERE workload=?),'submitted') WHERE id=?", (workload.id, workload.id, batch_id))
    return workload.id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("enqueue")
    add.add_argument("id")
    add.add_argument("value", type=float)
    drain = commands.add_parser("submit")
    drain.add_argument("--budget", type=float, default=None, help="Deprecated compatibility option")
    drain.add_argument("--image", required=True)
    args = parser.parse_args()
    with connect(args.db) as db:
        if args.command == "enqueue":
            enqueue(db, args.id, args.value)
        else:
            with nodus.Client() as client:
                workload_id = submit(db, client, args.budget, args.image)
                if workload_id:
                    print(workload_id)


if __name__ == "__main__":
    main()
