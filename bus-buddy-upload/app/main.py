"""HTTP API.

POST /run     {"date": "2025-04-29", "memory": "on"|"off", "sample_mode": true|false}  -> run JSON
GET  /health  -> status, mode, which backends are configured
GET  /scenarios -> the five demo mornings and their dates
GET  /, /control-room -> replay of the demo mornings (ui/control-room.html, built by scripts/build_control_room.py)
GET  /classic  -> the first screen (ui/index.html): runs one morning live via POST /run
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.config import ROOT, SETTINGS
from app.orchestrator import run_morning

app = FastAPI(title="Bus Buddy", version="0.1.0")


class RunRequest(BaseModel):
    date: str
    memory: str = "on"
    sample_mode: Optional[bool] = None
    run_id: Optional[str] = None


@app.post("/run")
def run(req: RunRequest):
    try:
        return run_morning(req.date, memory=req.memory, sample_mode=req.sample_mode, run_id=req.run_id)
    except Exception as e:  # structured error instead of a stack trace
        return JSONResponse(status_code=500, content={
            "run_id": req.run_id, "status": "error",
            "error": {"type": e.__class__.__name__, "message": str(e)[:500],
                      "recoverable": True, "hint": "Try sample_mode=true, or check Zetaris/Nightshift settings in .env"}})


@app.get("/health")
def health():
    return {"status": "ok", "sample_mode": SETTINGS.sample_mode,
            "zetaris_configured": _real(SETTINGS.zetaris_key), "nightshift_configured": _real(SETTINGS.nightshift_token),
            "models": {"reasoning": SETTINGS.model_reasoning, "fast": SETTINGS.model_fast}}


def _real(v: str) -> bool:
    return bool(v) and not v.startswith("your-")


@app.get("/scenarios")
def scenarios():
    return json.loads((SETTINGS.sample_dir / "scenarios.json").read_text())


@app.get("/")
@app.get("/control-room")
def control_room():
    return FileResponse(ROOT / "ui" / "control-room.html")


@app.get("/classic")
def classic_ui():
    return FileResponse(ROOT / "ui" / "index.html")
