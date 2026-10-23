"""One JSONL trace per run (logs/trace-<run_id>.jsonl) plus readable stdout.

Every agent step calls trace.log(...). Judges read this file to see how the
agents reached their output, so log decisions, queries, models and retries.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET = re.compile(r"(api[_-]?key|token|password|secret)(\"?\s*[:=]\s*\"?)([^\s\",]+)", re.I)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
_log = logging.getLogger("busbuddy")


def mask(text: str) -> str:
    return _SECRET.sub(lambda m: m.group(1) + m.group(2) + "...redacted", text)


class Trace:
    def __init__(self, run_id: str, logs_dir: Path):
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.path = logs_dir / f"trace-{run_id}.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")
        self.started = time.time()
        self.counts: dict[str, int] = {}

    def log(self, agent: str, action: str, *, sim_time: str | None = None, status: str = "success",
            input_summary: str = "", output_summary: str = "", **extra: Any) -> None:
        rec = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "sim_time": sim_time,
            "agent_name": agent,
            "action": action,
            "status": status,
            "input_summary": input_summary,
            "output_summary": output_summary,
        }
        rec.update(extra)
        line = mask(json.dumps(rec, default=str))
        self._fh.write(line + "\n")
        self._fh.flush()
        self.counts[agent] = self.counts.get(agent, 0) + 1
        msg = f"run_id={self.run_id} {sim_time or ''} {agent}: {action}"
        if output_summary:
            msg += f" -> {output_summary[:160]}"
        _log.info(mask(msg))

    def close(self) -> None:
        self._fh.close()
