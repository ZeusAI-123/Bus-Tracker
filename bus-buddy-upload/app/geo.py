
from __future__ import annotations

import math
from datetime import datetime, timedelta


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def road_eta_min(lat1: float, lon1: float, lat2: float, lon2: float, speed_kmh: float = 22.0) -> float:
    
    return haversine_m(lat1, lon1, lat2, lon2) * 1.35 / (speed_kmh * 1000 / 60)


def hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def parse_hm(date: str, hhmm: str) -> datetime:
    return datetime.strptime(f"{date} {hhmm}", "%Y-%m-%d %H:%M")


def add_min(t: datetime, m: float) -> datetime:
    return t + timedelta(minutes=m)
