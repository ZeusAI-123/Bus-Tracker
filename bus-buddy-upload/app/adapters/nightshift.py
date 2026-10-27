
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Optional

from app.config import SETTINGS


class Nightshift:
    def __init__(self, trace=None, sample_mode: Optional[bool] = None):
        self.trace = trace
        self.sample_mode = SETTINGS.sample_mode if sample_mode is None else sample_mode
        live = (not self.sample_mode) or SETTINGS.ai_live
        self.mode = SETTINGS.ai_recording if live else "replay"
        self._client = None
        self.calls = 0
        self.models_used: dict[str, int] = {}
        SETTINGS.recordings_dir.mkdir(parents=True, exist_ok=True)
        if live and SETTINGS.nightshift_token:
            from openai import OpenAI
            self._client = OpenAI(api_key=SETTINGS.nightshift_token, base_url=SETTINGS.nightshift_url,
                                  timeout=SETTINGS.model_timeout, max_retries=0,
                                  default_headers={"meterless-app": "bus-buddy", "meterless-scope": "user"})

    def json_call(self, *, agent: str, system: str, user: str, fast: bool = False,
                  sim_time: Optional[str] = None) -> Optional[dict]:
        """Ask for a JSON object. Returns the parsed dict, or None if no model is available."""
        model = SETTINGS.model_fast if fast else SETTINGS.model_reasoning
        key = hashlib.sha1(f"{agent}|{system}|{user}".encode()).hexdigest()[:16]
        rec_path = SETTINGS.recordings_dir / f"{agent.lower()}-{key}.json"

        if self.mode in ("replay", "record") and rec_path.exists():   # record reuses saved replies; delete data/recordings to refresh
            saved = json.loads(rec_path.read_text())
            self.models_used[saved["model"]] = self.models_used.get(saved["model"], 0) + 1
            self._log(agent, sim_time, saved["model"], "replayed recorded reply", saved["reply"], key)
            return saved["reply"]

        if self._client is None:
            if self.trace:
                self.trace.log(agent, "model_unavailable", sim_time=sim_time, status="fallback",
                               output_summary="No Nightshift connection or recording; using offline fallback")
            return None

        last_err = None
        for attempt in range(3):
            try:
                started = time.time()
                resp = self._client.chat.completions.create(
                    model=model, temperature=0.2,
                    messages=[{"role": "system", "content": system + "\nReply with ONE JSON object only."},
                              {"role": "user", "content": user}])
                text = resp.choices[0].message.content or ""
                reply = _parse_json(text)
                used = getattr(resp, "model", None) or model
                self.calls += 1
                self.models_used[used] = self.models_used.get(used, 0) + 1
                if self.mode == "record":
                    rec_path.write_text(json.dumps({"model": used, "reply": reply}, indent=2))
                self._log(agent, sim_time, used, f"answered in {int((time.time()-started)*1000)} ms", reply, key,
                          retry_count=attempt)
                return reply
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
        if self.trace:
            self.trace.log(agent, "model_error", sim_time=sim_time, status="fallback",
                           output_summary=f"Nightshift failed after 3 attempts: {last_err}")
        return None

    def _log(self, agent, sim_time, model, note, reply, key, **extra):
        if self.trace:
            self.trace.log(agent, "model_call", sim_time=sim_time, model=model, provider="meterless-nightshift",
                           output_summary=f"{note}: {json.dumps(reply)[:200]}", prompt_key=key, **extra)


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = text.split("</think>")[-1].strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("model did not return JSON")
    return json.loads(m.group(0))
