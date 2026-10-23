"""Meterless World Model engine (agent version), implemented for Bus Buddy.

Role: the shared, grounded picture of the morning that every agent reads instead
of re-deriving it. Built from the Meterless "World Model Agent Engine" guide:

- WorldStore      entities (routes, stops, buses, drivers, trips, incidents, decisions),
                  relationships between them, and observations (facts) with provenance
- IngestPipeline  one ingest path; idempotent (same fact key never duplicates) and
                  resumable (a crashed run continues from its last checkpoint)
- ContextBuilder  get_context(...) returns everything relevant to an incident
- Audit           every write appends a provenance row (who/what/when/source)

Storage is a single SQLite file per run (runs/<run_id>/world.db).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, attrs TEXT NOT NULL, source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS relations (
  src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL, source TEXT,
  PRIMARY KEY (src, rel, dst));
CREATE TABLE IF NOT EXISTS facts (
  key TEXT PRIMARY KEY, entity_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
  sim_time TEXT, source TEXT NOT NULL, agent TEXT NOT NULL, run_id TEXT NOT NULL, ingested_at TEXT);
CREATE INDEX IF NOT EXISTS facts_entity ON facts(entity_id, kind, sim_time);
CREATE TABLE IF NOT EXISTS provenance (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, action TEXT, source TEXT, agent TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class WorldModel:
    def __init__(self, path: Path | str, run_id: str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.run_id = run_id
        self.stats = {"ingested": 0, "duplicates_skipped": 0}

    # ------------------------------------------------------------ WorldStore
    def upsert_entity(self, eid: str, etype: str, attrs: dict, *, source: str, agent: str = "system") -> None:
        now = _now()
        self.db.execute(
            "INSERT INTO entities(id,type,attrs,source,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET attrs=excluded.attrs, source=excluded.source, updated_at=excluded.updated_at",
            (eid, etype, json.dumps(attrs, default=str), source, now))
        self._audit(eid, "upsert_entity", source, agent)

    def relate(self, src: str, rel: str, dst: str, *, source: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO relations(src,rel,dst,source) VALUES(?,?,?,?)", (src, rel, dst, source))

    def entity(self, eid: str) -> Optional[dict]:
        r = self.db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
        return None if r is None else {"id": r["id"], "type": r["type"], **json.loads(r["attrs"]), "_source": r["source"]}

    def entities(self, etype: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM entities WHERE type=? ORDER BY id", (etype,)).fetchall()
        return [{"id": r["id"], "type": r["type"], **json.loads(r["attrs"])} for r in rows]

    def related(self, eid: str, rel: Optional[str] = None, reverse: bool = False) -> list[str]:
        col_a, col_b = ("dst", "src") if reverse else ("src", "dst")
        q = f"SELECT {col_b} AS other FROM relations WHERE {col_a}=?" + (" AND rel=?" if rel else "")
        args = (eid, rel) if rel else (eid,)
        return [r["other"] for r in self.db.execute(q, args).fetchall()]

    # -------------------------------------------------------- IngestPipeline
    def ingest(self, *, key: str, entity_id: str, kind: str, value: Any, sim_time: Optional[str],
               source: str, agent: str) -> bool:
        """Add one observation. Returns False if this exact fact was already ingested (idempotent)."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO facts(key,entity_id,kind,value,sim_time,source,agent,run_id,ingested_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, entity_id, kind, json.dumps(value, default=str), sim_time, source, agent, self.run_id, _now()))
        if cur.rowcount == 0:
            self.stats["duplicates_skipped"] += 1
            return False
        self.stats["ingested"] += 1
        self._audit(key, "ingest", source, agent)
        return True

    def latest(self, entity_id: str, kind: str, at_or_before: Optional[str] = None) -> Optional[dict]:
        q = "SELECT * FROM facts WHERE entity_id=? AND kind=?"
        args: list = [entity_id, kind]
        if at_or_before:
            q += " AND sim_time<=?"
            args.append(at_or_before)
        r = self.db.execute(q + " ORDER BY sim_time DESC, rowid DESC LIMIT 1", args).fetchone()
        return None if r is None else {**json.loads(r["value"]), "_source": r["source"], "_sim_time": r["sim_time"], "_key": r["key"]}

    def facts(self, entity_id: str, kind: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM facts WHERE entity_id=? AND kind=? ORDER BY sim_time, rowid", (entity_id, kind)).fetchall()
        return [{**json.loads(r["value"]), "_source": r["source"], "_sim_time": r["sim_time"]} for r in rows]

    def checkpoint(self, tick: str) -> None:
        self.db.execute("INSERT INTO meta(k,v) VALUES('last_tick',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (tick,))
        self.db.commit()

    def last_checkpoint(self) -> Optional[str]:
        r = self.db.execute("SELECT v FROM meta WHERE k='last_tick'").fetchone()
        return r["v"] if r else None

    def set_meta(self, k: str, v: Any) -> None:
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v, default=str)))

    def get_meta(self, k: str, default: Any = None) -> Any:
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def commit(self) -> None:
        self.db.commit()

    # -------------------------------------------------------- ContextBuilder
    def get_context(self, *, bus_id: Optional[str] = None, route_id: Optional[str] = None,
                    stop_ids: Optional[list[str]] = None, at: Optional[str] = None) -> dict:
        """Everything an agent needs about an incident, each piece tagged with where it came from."""
        ctx: dict[str, Any] = {}
        if bus_id:
            ctx["bus"] = self.entity(bus_id)
            ctx["bus_position"] = self.latest(bus_id, "position", at)
            route_id = route_id or (ctx["bus"] or {}).get("route_id")
        if route_id:
            ctx["route"] = self.entity(f"route:{route_id}")
            ctx["route_stops"] = [self.entity(f"stop:{s}") for s in (ctx["route"] or {}).get("stops", [])]
            ctx["route_buses"] = [b for b in self.entities("bus") if b.get("route_id") == route_id]
            ctx["route_bus_positions"] = {b["id"]: self.latest(b["id"], "position", at) for b in ctx["route_buses"]}
        if stop_ids:
            ctx["stops"] = [self.entity(f"stop:{s}") for s in stop_ids]
        ctx["spare_buses"] = [b for b in self.entities("bus") if not b.get("route_id")]
        ctx["spare_drivers"] = [d for d in self.entities("driver") if d.get("status") == "spare"]
        ctx["used_today"] = self.get_meta("used_resources", [])
        return ctx

    # -------------------------------------------------------------- Audit
    def _audit(self, key: str, action: str, source: str, agent: str) -> None:
        self.db.execute("INSERT INTO provenance(key,action,source,agent,at) VALUES(?,?,?,?,?)", (key, action, source, agent, _now()))

    def provenance_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM provenance").fetchone()[0]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
