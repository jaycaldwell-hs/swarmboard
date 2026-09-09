"""Prepare an empty board snapshot retaining the live service's current agents.

Reads the hosted API without mutating it. The result can be explicitly deployed
with render_admin.py stage-import --snapshot followed by deploy.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3

import httpx

from swarmboard.database import init_db, make_engine, make_session_factory
from swarmboard.models import Agent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, default=Path("private/swarmboard-fresh.db"))
    args = parser.parse_args()
    accounts = json.loads(Path("private/accounts.json").read_text())
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=30) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": accounts["admin"]})
        if login.status_code != 200:
            raise SystemExit(f"Cannot sign in: HTTP {login.status_code}")
        response = client.get("/api/state")
        if response.status_code != 200:
            raise SystemExit(f"Cannot read current agents: HTTP {response.status_code}")
        agents = response.json()["agents"]
        client.post("/api/auth/logout")
    args.output.parent.mkdir(exist_ok=True, mode=0o700)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    engine = make_engine(f"sqlite:///{args.output.resolve()}")
    try:
        init_db(engine)
        with make_session_factory(engine).begin() as session:
            for captured in agents:
                values = dict(captured)
                values["last_spoke_at"] = None
                for field in ("created_at", "updated_at"):
                    values[field] = datetime.fromisoformat(values[field].replace("Z", "+00:00"))
                session.add(Agent(**values))
    finally:
        engine.dispose()
    with closing(sqlite3.connect(args.output)) as database:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        database.execute("PRAGMA journal_mode=DELETE")
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        counts = {table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("agents", "runs", "threads", "posts", "turns", "events", "stimuli", "memories")}
        assert all(counts[table] == 0 for table in counts if table != "agents")
    print(json.dumps({"prepared": str(args.output), "counts": counts, "live_service_unchanged": True}))


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"Preparing fresh board failed: {type(exc).__name__}") from None
