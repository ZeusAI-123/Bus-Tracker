from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Optional
import uuid

try:   
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

from app.config import SETTINGS
LOGIN_MAX_AGE_S = 8 * 60   # log in again every 8 minutes; Zetaris tokens can expire during a long run
LOGICAL = ["gtfs_routes", "gtfs_stops", "gtfs_trips", "gtfs_stop_times", "gtfs_shapes", "buses",
           "driver_shifts", "maintenance_reports", "road_closures", "events", "train_delays",
           "passenger_counts", "weather_hourly", "vehicle_positions"]
TIME_COLS = ("arrival_time", "departure_time", "shift_start", "shift_end", "reported_at",
             "scheduled_arr", "actual_arr", "window_start")
_TIME_AS_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})\.\d+$")

class ZetarisError(RuntimeError):
    pass


class Zetaris:
    def __init__(self, sample_mode: Optional[bool] = None, trace=None):
        self.sample_mode = SETTINGS.sample_mode if sample_mode is None else sample_mode
        self.trace = trace
        self._duck = None
        self._token = None
        self._login_at = 0.0
        if self.sample_mode:
            self._open_duckdb()

    # ---------------------------------------------------------------- public
    def query(self, sql: str, params: Optional[dict] = None, *, agent: str = "system",
              purpose: str = "", sim_time: Optional[str] = None) -> tuple[list[dict], str]:
        """Run SQL; returns (rows, evidence_id). evidence_id identifies this exact result in the trace."""
        sql_final = self._bind(sql, params or {})
        started = time.time()
        if self.sample_mode:
            rows = self._duck_rows(sql_final)
            backend = "duckdb-sample"
        else:
            rows = self._post_sql(self._to_physical(sql_final))
            backend = "zetaris"
        ev = "ev-" + hashlib.sha1((sql_final + json.dumps(rows, default=str)).encode()).hexdigest()[:10]
        if self.trace:
            self.trace.log(agent, "zetaris_query", sim_time=sim_time, input_summary=purpose,
                           output_summary=f"{len(rows)} rows", sql=sql_final, backend=backend,
                           evidence_id=ev, latency_ms=int((time.time() - started) * 1000))
        return rows, ev

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _bind(sql: str, params: dict) -> str:
        """Tiny, safe parameter binding: :name -> quoted literal (values are our own, never user text)."""
        def lit(v: Any) -> str:
            if v is None:
                return "NULL"
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, (list, tuple)):
                return "(" + ", ".join(lit(x) for x in v) + ")"
            return "'" + str(v).replace("'", "''") + "'"
        return re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", lambda m: lit(params[m.group(1)]) if m.group(1) in params else m.group(0), sql)

    @staticmethod
    def physical(name: str) -> str:
        if name == "vehicle_positions":
            return SETTINGS.zetaris_positions
        if name == "weather_hourly":
            return SETTINGS.zetaris_weather
        return SETTINGS.zetaris_pg_prefix + name

    def _to_physical(self, sql: str) -> str:
        for name in sorted(LOGICAL, key=len, reverse=True):
            sql = re.sub(rf"\b{name}\b", self.physical(name), sql)
        return _zetaris_dialect(sql)

    def _post_sql(self, sql: str) -> list[dict]:
                """Zetaris managed cluster: log in once, then POST the SQL to run-stream and read the rows."""
                base = SETTINGS.zetaris_url.rstrip("/")
                last = None
                for attempt in range(3):
                    try:
                        if not self._token or time.time() - self._login_at > LOGIN_MAX_AGE_S:
                            self._login(base)
                        r = requests.post(
                            base + "/api/proxy/sql-editor/sqls/run-stream",
                            json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor",
                                "chunkSize": 10000, "limit": SETTINGS.zetaris_row_limit},
                            headers={"Authorization": f"Bearer {self._token}", "X-Org-ID": SETTINGS.zetaris_org,
                                    "X-Request-ID": str(uuid.uuid4()), "Accept": "text/plain",
                                    "Origin": base, "Referer": base + "/"},
                            timeout=SETTINGS.zetaris_timeout)
                        if r.status_code in (400, 401, 403):
                            self._token = None          # Zetaris answers an expired login with 400 or 401: log in again and retry
                            raise ZetarisError(f"{r.status_code} from Zetaris: {r.text[:200]}")
                        r.raise_for_status()
                        return _parse_stream(r.text)
                    except Exception as e:  # retry with backoff, then surface a clear error
                        last = e
                        time.sleep(1.5 * (attempt + 1))
                raise ZetarisError(f"Zetaris did not answer after 3 attempts: {last}")

    def _login(self, base: str) -> None:
                r = requests.post(base + "/api/auth/login",
                                json={"username": SETTINGS.zetaris_user, "password": SETTINGS.zetaris_password},
                                timeout=SETTINGS.zetaris_timeout)
                r.raise_for_status()
                self._token = _find_token(r.json())
                self._login_at = time.time()
                if not self._token:
                    raise ZetarisError("Zetaris login answered without a token")

    def _open_duckdb(self) -> None:
            import duckdb
            d = SETTINGS.sample_dir
            con = duckdb.connect(":memory:")
            for name in LOGICAL:
                if name == "vehicle_positions":
                    con.execute(f"CREATE VIEW vehicle_positions AS SELECT * FROM read_parquet('{d / 'vehicle_positions'}/*.parquet')")
                else:
                    con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_csv_auto('{d / (name + '.csv')}', header=true, all_varchar=false)")
            self._duck = con

    def _duck_rows(self, sql: str) -> list[dict]:
            cur = self._duck.execute(sql)
            cols = [c[0] for c in cur.description]
            out = []
            for row in cur.fetchall():
                out.append({c: (v.isoformat() if hasattr(v, "isoformat") else v) for c, v in zip(cols, row)})
            return out

def _parse_stream(text: str) -> list[dict]:
    """run-stream answer: first line = column names, then one JSON list per row (all values as text)."""
    lines = [json.loads(l) for l in text.splitlines() if l.strip()]
    lists = [l for l in lines if isinstance(l, list)]
    if not lists:
        return []
    cols = lists[0]
    return [{c: _value(v) for c, v in zip(cols, row)} for row in lists[1:]]


def _value(v):
    """'3.73' -> 3.73, '42' -> 42; times like '07:20' and ids like 'C1' stay text."""
    if not isinstance(v, str):
        return v

    m = _TIME_AS_DATETIME.match(v)          # '2026-10-26 07:13:46.0' -> '07:13:46'
    if m:
        return m.group(1)
    
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def _find_token(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "token" in k.lower() and isinstance(v, str) and len(v) > 20:
                return v
            found = _find_token(v)
            if found:
                return found
    return None

def _zetaris_dialect(sql: str) -> str:
    """Adjust our SQL for Zetaris: quote the start/end columns the way it expects, and compare
    time-of-day columns by their time only (it stores them as today's date + the time)."""
    sql = sql.replace('"start"', "`start`").replace('"end"', "`end`")
    m = re.search(r"\bWHERE\b", sql, re.I)
    if not m:
        return sql
    head, tail = sql[:m.end()], sql[m.end():]
    o = re.search(r"\bORDER BY\b", tail, re.I)
    where, rest = (tail[:o.start()], tail[o.start():]) if o else (tail, "")
    for col in TIME_COLS:
        where = re.sub(rf"\b{col}\b", f"date_format({col}, 'HH:mm')", where)
    return head + where + rest
