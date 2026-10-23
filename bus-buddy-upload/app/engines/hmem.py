"""Meterless H-MEM engine (agent version), implemented for Bus Buddy.

Role: the agent's long-term memory across mornings. Built from the Meterless
"H-MEM Agent Engine" guide:

- Tiers        working (this run) -> episodic (one record per incident per morning)
               -> semantic "lessons" (patterns consolidated from episodes)
- Strength     each memory has a strength that decays with time (half-life in days)
               and is reinforced when the pattern happens again
- Conflicts    a new lesson that contradicts an old one on the same key supersedes it;
               the old lesson is kept for history, never deleted
- Retrieval    recall(...) ranks by match x strength x recency and returns the
               evidence behind each lesson
- Approval     lessons only become "active" (usable for proactive action) once the
               approval policy accepts them (programmable approval boundary)

Storage: one SQLite file (data/memory/hmem.db) shared across runs. Use
HMem(":memory:") for an empty, throwaway memory (e.g. the memory-off comparison).
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tier TEXT NOT NULL,              -- working | episodic | semantic
  key TEXT NOT NULL,               -- e.g. route:A|cause:weather_congestion|cond:rain
  content TEXT NOT NULL,           -- JSON
  strength REAL NOT NULL,
  first_seen TEXT NOT NULL,        -- service date (YYYY-MM-DD)
  last_seen TEXT NOT NULL,
  occurrences INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',   -- active | pending_approval | superseded
  superseded_by INTEGER,
  evidence TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS mem_key ON memories(key, tier, status);
"""

HALF_LIFE_DAYS = 30.0


def default_approval(lesson: dict) -> bool:
    """Programmable approval boundary: only lessons backed by evidence and a measured effect go live."""
    return bool(lesson.get("evidence")) and lesson.get("effect_min", 0) > 0


class HMem:
    def __init__(self, path: Path | str, approval: Callable[[dict], bool] = default_approval, trace=None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.approval = approval
        self.trace = trace

    # ------------------------------------------------------------ store
    def store_episode(self, *, key: str, content: dict, on_date: str, evidence: list[str]) -> int:
        cur = self.db.execute(
            "INSERT INTO memories(tier,key,content,strength,first_seen,last_seen,evidence) VALUES('episodic',?,?,?,?,?,?)",
            (key, json.dumps(content), 1.0, on_date, on_date, json.dumps(evidence)))
        self.db.commit()
        self._log("store_episode", f"{key} on {on_date}")
        return cur.lastrowid

    def consolidate(self, on_date: str) -> list[dict]:
        """Turn episodes into semantic lessons. Reinforce, create, or supersede."""
        changed = []
        keys = [r["key"] for r in self.db.execute("SELECT DISTINCT key FROM memories WHERE tier='episodic'").fetchall()]
        for key in keys:
            eps = [self._row(r) for r in self.db.execute(
                "SELECT * FROM memories WHERE tier='episodic' AND key=? ORDER BY last_seen", (key,)).fetchall()]
            effects = [e["content"].get("effect_min", 0) for e in eps]
            lesson_content = dict(eps[-1]["content"])
            lesson_content["effect_min"] = round(sum(effects) / len(effects), 1)
            lesson_content["based_on_mornings"] = sorted({e["last_seen"] for e in eps})
            lesson_content["evidence"] = [ev for e in eps for ev in e["evidence"]][-6:]
            current = self._active_lesson(key)
            if current is None:
                status = "active" if self.approval(lesson_content) else "pending_approval"
                lid = self._insert_lesson(key, lesson_content, on_date, len(eps), status)
                changed.append({"id": lid, "key": key, "change": "created", "status": status})
            elif _contradicts(current["content"], lesson_content):
                status = "active" if self.approval(lesson_content) else "pending_approval"
                lid = self._insert_lesson(key, lesson_content, on_date, len(eps), status)
                self.db.execute("UPDATE memories SET status='superseded', superseded_by=? WHERE id=?", (lid, current["id"]))
                changed.append({"id": lid, "key": key, "change": "superseded", "old": current["id"]})
            elif current["occurrences"] < len(eps):
                self.db.execute(
                    "UPDATE memories SET content=?, occurrences=?, last_seen=?, strength=MIN(1.0, strength+0.3) WHERE id=?",
                    (json.dumps(lesson_content), len(eps), on_date, current["id"]))
                changed.append({"id": current["id"], "key": key, "change": "reinforced"})
        self.db.commit()
        for c in changed:
            self._log("consolidate", json.dumps(c))
        return changed

    # ------------------------------------------------------------ recall
    def recall(self, *, filters: dict, on_date: str, top_n: int = 3, min_score: float = 0.2) -> list[dict]:
        """Rank active lessons by how well they match, how strong they are, and how recent."""
        rows = [self._row(r) for r in self.db.execute(
            "SELECT * FROM memories WHERE tier='semantic' AND status='active'").fetchall()]
        scored = []
        for m in rows:
            parts = dict(p.split(":", 1) for p in m["key"].split("|"))
            wanted = {k: str(v) for k, v in filters.items() if v is not None}
            if "route" in wanted and parts.get("route") != wanted["route"]:
                continue   # never apply one route's lesson to another route
            hits = sum(1 for k, v in wanted.items() if parts.get(k) == v)
            if wanted and hits == 0:
                continue
            match = hits / max(1, len(wanted))
            strength = self.current_strength(m, on_date)
            days = max(0, (date.fromisoformat(on_date) - date.fromisoformat(m["last_seen"])).days)
            recency = 1.0 / (1.0 + days / 60.0)
            score = round(match * strength * recency, 3)
            if score >= min_score:
                scored.append({**m, "score": score, "strength_now": round(strength, 3)})
        scored.sort(key=lambda x: -x["score"])
        out = scored[:top_n]
        self._log("recall", f"filters={filters} -> {[(x['key'], x['score']) for x in out]}")
        return out

    @staticmethod
    def current_strength(m: dict, on_date: str) -> float:
        days = max(0, (date.fromisoformat(on_date) - date.fromisoformat(m["last_seen"])).days)
        return m["strength"] * math.pow(0.5, days / HALF_LIFE_DAYS)

    # ------------------------------------------------------------ helpers
    def all(self, tier: Optional[str] = None) -> list[dict]:
        q, a = "SELECT * FROM memories", ()
        if tier:
            q, a = q + " WHERE tier=?", (tier,)
        return [self._row(r) for r in self.db.execute(q, a).fetchall()]

    def reset(self) -> None:
        self.db.execute("DELETE FROM memories")
        self.db.commit()

    def _active_lesson(self, key: str) -> Optional[dict]:
        r = self.db.execute("SELECT * FROM memories WHERE tier='semantic' AND key=? AND status IN ('active','pending_approval') "
                            "ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        return self._row(r) if r else None

    def _insert_lesson(self, key, content, on_date, occurrences, status) -> int:
        cur = self.db.execute(
            "INSERT INTO memories(tier,key,content,strength,first_seen,last_seen,occurrences,status,evidence) "
            "VALUES('semantic',?,?,?,?,?,?,?,?)",
            (key, json.dumps(content), 0.7, on_date, on_date, occurrences, status, json.dumps(content.get("evidence", []))))
        return cur.lastrowid

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["content"] = json.loads(d["content"])
        d["evidence"] = json.loads(d["evidence"])
        return d

    def _log(self, action: str, summary: str) -> None:
        if self.trace:
            self.trace.log("H-MEM", action, output_summary=summary, engine="meterless-h-mem")


def _contradicts(old: dict, new: dict) -> bool:
    """Same pattern, but the measured effect or the fix that worked has clearly changed."""
    a, b = old.get("effect_min", 0), new.get("effect_min", 0)
    if old.get("fix") and new.get("fix") and old["fix"] != new["fix"]:
        return True
    return a > 0 and abs(a - b) / a > 0.5
