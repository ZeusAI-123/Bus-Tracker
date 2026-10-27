from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    sample_mode: bool = _bool("SAMPLE_MODE", True)

    zetaris_url: str = os.getenv("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0")
    zetaris_org: str = os.getenv("ZETARIS_ORG_ID", "1")
    zetaris_key: str = os.getenv("ZETARIS_API_KEY", "")
    zetaris_pg_prefix: str = os.getenv("ZETARIS_PG_PREFIX", "busbuddy_pg.public.")
    zetaris_positions: str = os.getenv("ZETARIS_POSITIONS_TABLE", "busbuddy_s3.vehicle_positions")
    zetaris_weather: str = os.getenv("ZETARIS_WEATHER_TABLE", "busbuddy_pg.public.weather_hourly")
    zetaris_timeout: int = int(os.getenv("ZETARIS_TIMEOUT_SECONDS", "45"))
    zetaris_user: str = os.getenv("ZETARIS_USERNAME", "")
    zetaris_password: str = os.getenv("ZETARIS_PASSWORD", "")
    zetaris_row_limit: int = int(os.getenv("ZETARIS_ROW_LIMIT", "100000"))

    nightshift_url: str = os.getenv("METERLESS_BASE_URL", "http://127.0.0.1:8088/v1")
    nightshift_token: str = os.getenv("METERLESS_RUNTIME_TOKEN", "")
    model_reasoning: str = os.getenv("MODEL_REASONING", "nvidia/nemotron-3-super")
    model_fast: str = os.getenv("MODEL_FAST", "nvidia/nemotron-3-nano")
    model_timeout: int = int(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
    ai_recording: str = os.getenv("AI_RECORDING", "record")
    ai_live: bool = _bool("AI_LIVE", False)

    timezone: str = os.getenv("TIMEZONE", "Australia/Sydney")
    port: int = int(os.getenv("PORT", "8000"))

    data_dir: Path = ROOT / "data"
    sample_dir: Path = ROOT / "data" / "sample"
    memory_db: Path = ROOT / "data" / "memory" / "hmem.db"
    recordings_dir: Path = ROOT / "data" / "recordings"
    runs_dir: Path = ROOT / "runs"
    logs_dir: Path = ROOT / "logs"


SETTINGS = Settings()


LIMITS = {
    "bus_capacity_default": 60,        
    "max_service_gap_min": 30,          
    "max_hold_min": 5,                   
    "min_dispatch_confidence": 0.6,      
    "late_threshold_min": 5,
    "stopped_threshold_min": 4,
    "missing_threshold_min": 4,
    "offroute_threshold_m": 300,
    "bunching_threshold_m": 500,
    "walk_train_to_bus_min": 3,
}

SERVICE_START = "06:30"
SERVICE_END = "08:30"
TICK_MINUTES = 2
