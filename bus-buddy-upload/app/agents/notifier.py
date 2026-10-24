"""Notifier: passenger service alerts (GTFS-Realtime Alert style) + a controller decision card.
Nothing is actually sent; alerts are returned and shown on screen."""
from __future__ import annotations

import json

SYSTEM = """You write public transport service alerts for Sydney bus passengers.
Plain, calm, specific: what happened, what we are doing, and the expected time at the affected stop.
Rules:
- Header starts with the route, e.g. "Route C: bus breakdown".
- Never mention internal codes: no fleet numbers (S1, C1), driver ids (D11) or evidence ids. Say
  "a replacement bus", "an extra bus" or "your Route C bus".
- Give expected times as "expected about 07:36". Never promise that service "will resume" or be
  "back to normal", and never mention things that are not happening (e.g. "no rain").
- Name the affected stops passengers know. Max 240 characters each.
Answer: {"header": "...", "description": "..."}"""

GTFS_CAUSE = {"breakdown": "TECHNICAL_PROBLEM", "weather_congestion": "WEATHER", "late_train": "OTHER_CAUSE",
              "event_crowding": "OTHER_CAUSE", "crowding_dwell": "OTHER_CAUSE", "gps_glitch": "TECHNICAL_PROBLEM",
              "road_closure": "CONSTRUCTION", "unknown": "UNKNOWN_CAUSE"}
GTFS_EFFECT = {"replacement_bus": "MODIFIED_SERVICE", "extra_bus": "ADDITIONAL_SERVICE", "hold": "SIGNIFICANT_DELAYS",
               "alert_only": "SIGNIFICANT_DELAYS", "monitor": "NO_EFFECT"}


class Notifier:
    def __init__(self, ai, network, trace):
        self.ai, self.net, self.trace = ai, network, trace

    def alerts(self, inc: dict, finding: dict, plan: dict, sim_time: str) -> list[dict]:
        if plan["action"] == "monitor":
            return []
        stops = plan.get("stops_affected") or ([inc["stop_id"]] if inc.get("stop_id") else [])
        names = [self.net["stops"][s]["name"] for s in stops if s in self.net["stops"]]
        reply = self.ai.json_call(agent="Notifier", fast=True, system=SYSTEM, sim_time=sim_time,
                                  user=json.dumps({"route": inc.get("route_id"), "cause": finding.get("explanation"),
                                                   "action": plan["summary"], "stops": names, "eta": plan.get("eta")}))
        if reply and reply.get("header"):
            header, desc, method = reply["header"], reply.get("description", ""), "nemotron-via-nightshift"
        else:
            header, desc, method = _template(inc, finding, plan, names), "", "template"
        alert = {"id": f"alert-{inc['id']}", "active_from": sim_time, "cause": GTFS_CAUSE.get(finding.get("cause"), "UNKNOWN_CAUSE"),
                 "effect": GTFS_EFFECT.get(plan["action"], "OTHER_EFFECT"),
                 "informed_entity": [{"route_id": inc.get("route_id"), "stop_id": s} for s in stops],
                 "header_text": header, "description_text": desc, "method": method}
        self.trace.log("Notifier", "service_alert", sim_time=sim_time, output_summary=header, incident_id=inc["id"], method=method)
        return [alert]

    def decision_card(self, inc: dict, finding: dict, attempts: list[dict], final: dict | None) -> dict:
        return {"incident": inc.get("detail"), "why": finding.get("explanation"), "confidence": finding.get("confidence"),
                "decision": final["summary"] if final else "Escalated to the human controller",
                "rejected": [{"plan": a["plan"]["summary"], "why": "; ".join(p["problem"] for p in a["verdict"]["problems"])}
                             for a in attempts if not a["verdict"]["approved"]]}


def _template(inc, finding, plan, names) -> str:
    route = inc.get("route_id")
    where = f" at {', '.join(names[:3])}" if names else ""
    if plan["action"] == "replacement_bus":
        return f"Route {route}: bus breakdown. A replacement bus will reach{where} from about {plan.get('eta')}. Please keep waiting at your stop."
    if plan["action"] == "extra_bus":
        return f"Route {route}: an extra bus will run{where} from about {plan.get('eta')} to help with delays and crowding."
    if plan["action"] == "hold":
        return f"Route {route}: this bus is waiting {plan.get('hold_min')} min for passengers from a late train."
    return f"Route {route}: buses are running late{where}. {finding.get('explanation', '')}"[:240]
