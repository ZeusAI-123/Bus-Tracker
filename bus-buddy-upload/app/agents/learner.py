"""Learner (end of morning) and Pre-check (start of morning).

Learner: turns this morning's explained incidents into H-MEM episodes, then
consolidates them into lessons ("Route A runs ~12 min late on rainy mornings").
Pre-check: at 06:30, looks at today's conditions, recalls lessons and, if one
applies, raises a 'proactive' incident so the agent acts BEFORE the delay.
"""
from __future__ import annotations

from datetime import datetime


class Learner:
    def __init__(self, memory, trace):
        self.memory, self.trace = memory, trace

    def learn(self, date: str, handled: list[dict], conditions: dict) -> list[dict]:
        stored = []
        for h in handled:
            inc, f = h["incident"], h["finding"]
            if f.get("cause") == "weather_congestion" and inc.get("route_id") and inc["type"] == "late" \
                    and float(inc.get("late_min", 0)) >= 8:   # only clear, measured delays become memories
                key = f"route:{inc['route_id']}|cause:weather_congestion|cond:rain"
                effect = float(inc.get("late_min", 0))
                content = {"route": inc["route_id"], "cause": "weather_congestion", "condition": "rain",
                           "effect_min": effect, "fix": "extra_bus_early",
                           "summary": f"Route {inc['route_id']} runs about {effect:.0f} min late on rainy mornings; "
                                      f"an early extra bus keeps the gap down."}
                self.memory.store_episode(key=key, content=content, on_date=date, evidence=f.get("evidence_ids", []))
                stored.append(key)
        changes = self.memory.consolidate(date)
        self.trace.log("Learner", "learned", output_summary=f"stored {len(stored)} episode(s); lesson changes: {changes}",
                       engine="meterless-h-mem")
        return changes


class PreCheck:
    def __init__(self, memory, tools, network, trace, date: str):
        self.memory, self.tools, self.net, self.trace, self.date = memory, tools, network, trace, date

    def run(self, now: datetime) -> tuple[dict, list[dict]]:
        t = now.strftime("%H:%M")
        w = self.tools.get_morning_weather(t)   # this morning's conditions (a forecast feed in a live system)
        rain = sum((r.get("precip_mm") or 0) for r in w["rows"])
        conditions = {"rain_mm_morning": round(rain, 1), "raining": rain >= 0.5}
        proactive = []
        if conditions["raining"]:
            for rid in sorted(self.net["route_stops"]):
                lessons = self.memory.recall(filters={"route": rid, "cause": "weather_congestion", "cond": "rain"}, on_date=self.date)
                if not lessons:
                    continue
                l = lessons[0]
                stops = self.net["route_stops"][rid]
                proactive.append({"id": f"pro-{rid}", "type": "proactive", "detected_at": t, "route_id": rid,
                                  "stop_id": stops[0], "stops": stops,
                                  "expected_delay_min": l["content"].get("effect_min", 10),
                                  "detail": f"Rain this morning; memory says Route {rid} runs ~{l['content'].get('effect_min', 10):.0f} min late",
                                  "lesson": {"key": l["key"], "summary": l["content"].get("summary"), "score": l["score"],
                                             "based_on": l["content"].get("based_on_mornings")},
                                  "evidence": [w["evidence_id"]] + l["evidence"]})
        self.trace.log("PreCheck", "morning_check", sim_time=t,
                       output_summary=f"rain {conditions['rain_mm_morning']} mm; proactive actions: {[p['route_id'] for p in proactive]}")
        return conditions, proactive
