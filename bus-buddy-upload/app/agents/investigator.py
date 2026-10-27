
from __future__ import annotations

import json

CAUSES = ["breakdown", "weather_congestion", "late_train", "event_crowding", "crowding_dwell",
          "gps_glitch", "road_closure", "driver_absent", "unknown"]

SYSTEM = """You are the Investigator in a bus network control room in Sydney.
Explain WHY an incident happened using evidence from tools. Each tool runs a query through Zetaris.
Tools (call with JSON args):
- get_maintenance {"bus_id": str}
- get_driver_shift {"bus_id": str}
- get_weather {}
- get_road_closures {"route_id": str}
- get_events {"stop_ids": [str]}
- get_train_delays {"station_stop_id": str}
- get_passenger_counts {"stop_ids": [str]}
- get_bus_track {"bus_id": str}
Either request tools: {"tool_calls": [{"tool": "...", "args": {...}}]}
or answer: {"final": {"cause": one of %s, "confidence": 0-1, "evidence_ids": ["ev-..."],
"explanation": "plain English, 1-2 sentences", "uncertain": "what you are not sure about"}}
Only cite evidence_ids returned by tools. Use memory lessons if relevant. Max 3 rounds of tools.
Guidance: rain clearly slows Sydney peak-hour traffic. If a route is running late while there is any
measurable rain (precip_mm > 0) in the last two hours, and there is no fault, road closure or event,
the cause is weather_congestion with confidence 0.7 or more. For crowds at a stop, check get_events and
get_passenger_counts: an event at those stops means event_crowding. If get_road_closures shows a closure on
the incident's own route that is active at the time of the delay, the cause is road_closure (closures on other
routes do not count). Only say something was ruled out if you checked it with a tool.
Required checks before answering: stopped -> get_maintenance, get_road_closures, get_bus_track;
missing -> get_bus_track, get_maintenance; late -> get_weather, get_road_closures, get_events;
crowding -> get_events, get_passenger_counts; connection_risk -> get_train_delays, get_passenger_counts.
Usually 1-2 rounds of tools are enough.""" % CAUSES


REQUIRED = {"stopped": ["get_maintenance", "get_road_closures", "get_bus_track"],
            "missing": ["get_bus_track", "get_maintenance"],
            "late": ["get_weather", "get_road_closures", "get_events"],
            "crowding": ["get_events", "get_passenger_counts"],
            "bunching": ["get_events"],
            "connection_risk": ["get_train_delays", "get_passenger_counts"]}


class Investigator:
    def __init__(self, ai, tools, memory, trace, date: str):
        self.ai, self.tools, self.memory, self.trace, self.date = ai, tools, memory, trace, date

    def investigate(self, inc: dict, context: dict) -> dict:
        t = inc["detected_at"]
        lessons = self.memory.recall(filters={"route": inc.get("route_id")}, on_date=self.date) if inc.get("route_id") else []
        lesson_text = [{"key": l["key"], "lesson": l["content"].get("summary"), "score": l["score"]} for l in lessons]
        transcript = [{"incident": _brief(inc), "context": _ctx_brief(context), "memory": lesson_text}]
        called: set[str] = set()
        for round_no in range(4):
            if round_no == 3:
                transcript.append({"instruction": "No more tools. Give your final answer now."})
            reply = self.ai.json_call(agent="Investigator", system=SYSTEM, user=json.dumps(transcript, default=str), sim_time=t)
            if reply is None:
                return self._offline(inc, lessons)
            if "final" in reply or "cause" in reply:
                final = reply.get("final", reply)
                bad = [e for e in final.get("evidence_ids", []) if e not in self.tools.evidence]
                if bad and round_no < 3:
                    transcript.append({"error": f"These evidence ids do not exist: {bad}. Cite only ids returned by tools."})
                    self.trace.log("Investigator", "rejected_uncited_evidence", sim_time=t, status="retry", output_summary=str(bad))
                    continue
                skipped = [n for n in REQUIRED.get(inc["type"], []) if n not in called]
                if skipped and round_no < 3:
                    results = [self._run(n, self._default_args(n, inc), t) for n in skipped]
                    called.update(skipped)
                    transcript.append({"tool_results": results, "note": f"You answered without these required checks: {skipped}. "
                                       "They were run for you. Reconsider and give your final answer."})
                    self.trace.log("Investigator", "required_checks_added", sim_time=t, status="retry", output_summary=str(skipped),
                                   incident_id=inc["id"])
                    continue
                final["evidence_ids"] = [e for e in final.get("evidence_ids", []) if e in self.tools.evidence]
                if final.get("cause") not in CAUSES:
                    final["cause"] = "unknown"
                final["method"] = "nemotron-via-nightshift"
                final["memory_used"] = lesson_text
                self.trace.log("Investigator", "explained", sim_time=t, output_summary=f"{final['cause']} ({final.get('confidence')}): {final.get('explanation','')}", incident_id=inc["id"])
                return final
            calls = (reply.get("tool_calls") or [])[:4]
            called.update(c.get("tool") for c in calls)
            transcript.append({"tool_results": [self._run(c.get("tool"), c.get("args") or {}, t) for c in calls]})
        return self._offline(inc, lessons)

    def _run(self, name: str, args: dict, t: str) -> dict:
        fn = self.tools.menu().get(name)
        if not fn:
            return {"tool": name, "error": "unknown tool"}
        try:
            out = fn(**args, sim_time=t)
            return {"tool": name, "evidence_id": out["evidence_id"], "rows": out["rows"][:12]}
        except Exception as e:
            return {"tool": name, "error": str(e)}

    def _default_args(self, name: str, inc: dict) -> dict:
        stops = inc.get("stops") or ([inc["stop_id"]] if inc.get("stop_id") else self._route_stops(inc))
        return {"get_maintenance": {"bus_id": inc.get("bus")}, "get_bus_track": {"bus_id": inc.get("bus")},
                "get_driver_shift": {"bus_id": inc.get("bus")}, "get_weather": {},
                "get_road_closures": {"route_id": inc.get("route_id")}, "get_events": {"stop_ids": stops},
                "get_passenger_counts": {"stop_ids": stops}, "get_train_delays": {"station_stop_id": inc.get("stop_id")}}[name]

    
    def _offline(self, inc: dict, lessons: list) -> dict:
        t, T = inc["detected_at"], self.tools
        kind = inc["type"]
        ev, cause, conf, why, unsure, corroborated = [], "unknown", 0.4, "", "", False
        if kind == "stopped":
            m = T.get_maintenance(inc["bus"], t); ev.append(m["evidence_id"])
            if m["rows"]:
                r = m["rows"][-1]
                cause, conf, corroborated = "breakdown", 0.9, True
                why = f"{inc['bus']} has been stationary between stops since {inc.get('stationary_since', inc['position']['at'])}, and the driver reported '{r['issue']}' at {str(r['reported_at'])[:5]}."
            else:
                c = T.get_road_closures(inc["route_id"], t); ev.append(c["evidence_id"])
                cause, conf = ("road_closure", 0.7) if c["rows"] else ("unknown", 0.4)
                why = "Bus stationary between stops; " + ("a road closure is reported." if c["rows"] else "no fault or closure reported.")
        elif kind == "missing":
            tr = T.get_bus_track(inc["bus"], t); ev.append(tr["evidence_id"])
            m = T.get_maintenance(inc["bus"], t); ev.append(m["evidence_id"])
            moving = [r for r in tr["rows"] if (r.get("speed_kmh") or 0) > 5]
            if moving and not m["rows"]:
                cause, conf = "gps_glitch", 0.5
                why = f"{inc['bus']} stopped reporting while moving normally and on schedule; no fault reported. Most likely a GPS/communications dropout."
                unsure = "Cannot rule out a breakdown until positions resume."
            else:
                cause, conf = ("breakdown", 0.6) if m["rows"] else ("unknown", 0.4)
                why = f"{inc['bus']} stopped reporting." + (" A fault was reported earlier." if m["rows"] else "")
        elif kind == "late":
            w = T.get_weather(t); ev.append(w["evidence_id"])
            rain = sum((r.get("precip_mm") or 0) for r in w["rows"])
            if rain > 0:
                cause, conf, corroborated = "weather_congestion", 0.8, True
                why = f"Route {inc['route_id']} is {inc.get('late_min', 0):.0f} min late; {rain:.1f} mm of rain in the last two hours is slowing traffic."
                if lessons:
                    why += f" Memory agrees: {lessons[0]['content'].get('summary')}"
            else:
                e = T.get_events(self._route_stops(inc), t); ev.append(e["evidence_id"])
                c = T.get_road_closures(inc["route_id"], t); ev.append(c["evidence_id"])
                if e["rows"]:
                    cause, conf, corroborated = "crowding_dwell", 0.75, True
                    why = f"Route {inc['route_id']} is {inc.get('late_min', 0):.0f} min late: buses are taking longer to load {e['rows'][0]['name']} crowds."
                elif c["rows"]:
                    cause, conf = "road_closure", 0.7
                    why = f"Route {inc['route_id']} is running late; a road closure is reported."
                else:
                    cause, conf = "unknown", 0.5
                    why = f"Route {inc['route_id']} is running late; no clear cause found."
        elif kind == "connection_risk":
            tdl = T.get_train_delays(inc["stop_id"], t); ev.append(tdl["evidence_id"])
            pc = T.get_passenger_counts([inc["stop_id"]], t); ev.append(pc["evidence_id"])
            tr = inc["train"]
            cause, conf, corroborated = "late_train", 0.95, True
            why = (f"{tr['train']} due {str(tr['scheduled_arr'])[:5]} is arriving {str(tr['actual_arr'])[:5]}; "
                   f"{tr['transfer_pax']} passengers transferring to Route {inc['route_id']} will miss the {inc['connecting_departure']} bus.")
        elif kind == "crowding":
            stops = inc.get("stops") or [inc["stop_id"]]
            e = T.get_events(stops, t); ev.append(e["evidence_id"])
            pc = T.get_passenger_counts(stops, t); ev.append(pc["evidence_id"])
            if e["rows"]:
                cause, conf, corroborated = "event_crowding", 0.9, True
                why = (f"{e['rows'][0]['name']} is bringing about {inc.get('waiting')} people to "
                       f"{', '.join(stops)} on Route {inc.get('route_id')}, more than the regular buses can carry.")
            else:
                cause, conf = "unknown", 0.5
                why = f"More people than usual at {inc['stop_id']}; no event found."
        elif kind == "bunching":
            e = T.get_events(self._route_stops(inc), t); ev.append(e["evidence_id"])
            cause, conf = ("crowding_dwell", 0.75) if e["rows"] else ("unknown", 0.5)
            why = (f"{' and '.join(inc['buses'])} are running together; " +
                   ("the first bus lost time loading event crowds, so the next one caught up." if e["rows"] else "cause unclear."))
        out = {"cause": cause, "confidence": conf, "evidence_ids": ev, "explanation": why, "uncertain": unsure,
               "corroborated": corroborated, "method": "offline-rules",
               "memory_used": [{"key": l["key"], "lesson": l["content"].get("summary"), "score": l["score"]} for l in lessons]}
        self.trace.log("Investigator", "explained", sim_time=t, status="fallback",
                       output_summary=f"{cause} ({conf}): {why}", incident_id=inc["id"], method="offline-rules")
        return out

    def _route_stops(self, inc: dict) -> list:
        return (self.tools.world.entity(f"route:{inc['route_id']}") or {}).get("stops", []) if inc.get("route_id") else []


def _brief(inc: dict) -> dict:
    return {k: v for k, v in inc.items() if k not in ("last_seen",)}


def _ctx_brief(ctx: dict) -> dict:
    return {"bus": ctx.get("bus"), "bus_position": ctx.get("bus_position"),
            "route": (ctx.get("route") or {}).get("name"), "spare_buses": [b["id"] for b in ctx.get("spare_buses", [])]}
