"""Investigator: explains WHY an incident happened, with evidence.

Live mode: Nemotron (via Nightshift) chooses which Zetaris tools to call, up to
3 rounds, then answers with a cause, a confidence and the evidence ids it relied on.
Code then checks every cited evidence id really exists (no made-up evidence).

Offline fallback (no model and no recording): a documented rule-based reasoner
that calls the same tools. It is labelled "offline-rules" in the trace/output.
"""
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
Only cite evidence_ids returned by tools. Use memory lessons if relevant. Max 3 rounds of tools.""" % CAUSES


class Investigator:
    def __init__(self, ai, tools, memory, trace, date: str):
        self.ai, self.tools, self.memory, self.trace, self.date = ai, tools, memory, trace, date

    def investigate(self, inc: dict, context: dict) -> dict:
        t = inc["detected_at"]
        lessons = self.memory.recall(filters={"route": inc.get("route_id")}, on_date=self.date) if inc.get("route_id") else []
        lesson_text = [{"key": l["key"], "lesson": l["content"].get("summary"), "score": l["score"]} for l in lessons]
        transcript = [{"incident": _brief(inc), "context": _ctx_brief(context), "memory": lesson_text}]
        for round_no in range(4):
            reply = self.ai.json_call(agent="Investigator", system=SYSTEM, user=json.dumps(transcript, default=str), sim_time=t)
            if reply is None:
                return self._offline(inc, lessons)
            if "final" in reply:
                final = reply["final"]
                bad = [e for e in final.get("evidence_ids", []) if e not in self.tools.evidence]
                if bad and round_no < 3:
                    transcript.append({"error": f"These evidence ids do not exist: {bad}. Cite only ids returned by tools."})
                    self.trace.log("Investigator", "rejected_uncited_evidence", sim_time=t, status="retry", output_summary=str(bad))
                    continue
                final["evidence_ids"] = [e for e in final.get("evidence_ids", []) if e in self.tools.evidence]
                if final.get("cause") not in CAUSES:
                    final["cause"] = "unknown"
                final["method"] = "nemotron-via-nightshift"
                final["memory_used"] = lesson_text
                self.trace.log("Investigator", "explained", sim_time=t, output_summary=f"{final['cause']} ({final.get('confidence')}): {final.get('explanation','')}", incident_id=inc["id"])
                return final
            results = []
            for call in (reply.get("tool_calls") or [])[:4]:
                fn = self.tools.menu().get(call.get("tool"))
                if not fn:
                    results.append({"tool": call.get("tool"), "error": "unknown tool"})
                    continue
                try:
                    out = fn(**(call.get("args") or {}), sim_time=t)
                    results.append({"tool": call["tool"], "evidence_id": out["evidence_id"], "rows": out["rows"][:12]})
                except Exception as e:
                    results.append({"tool": call.get("tool"), "error": str(e)})
            transcript.append({"tool_results": results})
        return self._offline(inc, lessons)

    # ---------------------------------------------------------------- fallback
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
        return self.tools.world.entity(f"route:{inc['route_id']}").get("stops", []) if inc.get("route_id") else []


def _brief(inc: dict) -> dict:
    return {k: v for k, v in inc.items() if k not in ("last_seen",)}


def _ctx_brief(ctx: dict) -> dict:
    return {"bus": ctx.get("bus"), "bus_position": ctx.get("bus_position"),
            "route": (ctx.get("route") or {}).get("name"), "spare_buses": [b["id"] for b in ctx.get("spare_buses", [])]}
