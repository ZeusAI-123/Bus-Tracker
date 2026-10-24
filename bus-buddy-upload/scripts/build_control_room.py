"""Builds ui/control-room.html: a self-contained replay of the five demo mornings.

  python scripts/build_control_room.py      after `python run.py --week` has written output_examples/

Reads output_examples/*.json, keeps what the screen needs (network, bus positions per tick,
incidents, plans, alerts, memory) and embeds it in ui/control-room.template.html.
The page opens straight from disk or at http://localhost:8000/control-room.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "output_examples"
TEMPLATE = ROOT / "ui" / "control-room.template.html"
PAGE = ROOT / "ui" / "control-room.html"

MORNINGS = [("mon1", "mon1_2025-04-28", "Mon 28 Apr", "Rainy Monday: Route A crawls"),
            ("tue", "tue_2025-04-29", "Tue 29 Apr", "Bus C1 breaks down"),
            ("wed", "wed_2025-05-07", "Wed 7 May", "Late train + GPS trap"),
            ("thu", "thu_2025-05-08", "Thu 8 May", "UNSW exam crowds"),
            ("mon2", "mon2_2025-05-19", "Mon 19 May", "Rain again: memory acts early"),
            ("mon2off", "mon2_2025-05-19_memory_off", "Mon 19 May", "Same morning, memory off")]

INCIDENT_KEYS = ("id", "type", "detected_at", "bus", "route", "stop", "detail", "position", "resolved_at", "resolution",
                 "cause", "confidence", "explanation", "evidence", "reasoning_method", "memory_used", "plans_considered")
DECISION_KEYS = ("action", "summary", "reason", "bus", "driver", "eta", "dispatch_at", "stops_affected",
                 "passengers_affected", "wheelchair_waiting", "method", "hold_min")
ALERT_KEYS = ("id", "active_from", "cause", "effect", "header_text", "description_text", "method")


def _r(x):
    return round(x, 5) if isinstance(x, float) else x


def _network(n: dict) -> dict:
    return {"stops": [{"id": s["id"], "name": s["name"], "lat": _r(s["lat"]), "lon": _r(s["lon"])} for s in n["stops"]],
            "routes": [{"id": r["id"], "name": r["name"], "stops": r["stops"],
                        "shape": [[_r(p[0]), _r(p[1])] for p in r["shape"]]} for r in n["routes"]],
            "depot": n["depot"]}


def _morning(key: str, day: str, label: str, res: dict) -> dict:
    incidents = []
    for i in res["incidents"]:
        d = i["decision"]
        incidents.append({k: i.get(k) for k in INCIDENT_KEYS}
                         | {"decision": {k: d.get(k) for k in DECISION_KEYS if d.get(k) is not None}})
    alerts = [{k: a[k] for k in ALERT_KEYS}
              | {"routes": sorted({e["route_id"] for e in a["informed_entity"] if e.get("route_id")}),
                 "stops": [e["stop_id"] for e in a["informed_entity"] if e.get("stop_id")]} for a in res["service_alerts"]]
    timeline = [{"t": t["t"], "b": [[b["id"], _r(b.get("lat")), _r(b.get("lon")), b.get("speed"), b["status"]] for b in t["buses"]]}
                for t in res["timeline"]]
    return {"key": key, "date": res["date"], "day": day, "label": label, "memory": res["mode"]["memory"],
            "conditions": res["conditions"], "incidents": incidents, "alerts": alerts, "metrics": res["metrics"],
            "lessons": res["memory"]["lessons"], "lesson_changes": res["memory"]["lesson_changes"],
            "models": res["models_used"], "timeline": timeline}


def main() -> None:
    data: dict = {"mornings": {}}
    for key, name, day, label in MORNINGS:
        f = OUT_DIR / f"{name}.json"
        if not f.exists():
            raise SystemExit(f"Missing {f.relative_to(ROOT)}. Run: python run.py --week")
        res = json.loads(f.read_text(encoding="utf-8"))
        data.setdefault("network", _network(res["network"]))
        data["mornings"][key] = _morning(key, day, label, res)
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    PAGE.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")
    print(f"wrote {PAGE.relative_to(ROOT)} ({PAGE.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
