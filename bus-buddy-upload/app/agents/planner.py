"""Planner: suggests fixes. CODE works out the facts for each option (arrival times,
seats, accessibility, driver hours, service gaps); the MODEL picks which one to try
and explains why. After a Safety Critic rejection, options that share the rejected
property are removed and the Planner chooses again (the critic feedback loop).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from app.config import LIMITS
from app.geo import road_eta_min

SYSTEM = """You are the Planner in a Sydney bus control room. You get an incident, its explained cause,
and a list of possible actions with facts already calculated by code. Pick the ONE option that best
serves passengers safely and fairly. If earlier attempts were rejected by the Safety Critic, learn from
the feedback. Answer: {"choice": "<option id>", "reason": "1-2 plain sentences"}"""


class Planner:
    def __init__(self, ai, world, network, zetaris, trace, date: str):
        self.ai, self.world, self.net, self.z, self.trace, self.date = ai, world, network, zetaris, trace, date

    # ------------------------------------------------------------- options
    def options(self, inc: dict, finding: dict, now: datetime) -> list[dict]:
        cause, kind = finding["cause"], inc["type"]
        base = {"incident_id": inc["id"], "route": inc.get("route_id"), "cause": cause,
                "cause_confidence": finding.get("confidence", 0.5), "corroborated": finding.get("corroborated", False),
                "used_today": self.world.get_meta("used_resources", [])}
        stops = self._affected_stops(inc)
        waiting, wheelchair, waiting_by_stop = self._waiting(stops, now)
        opts: list[dict] = []

        def dispatch(action: str, label: str, start_stop: str, work_until: str, extra: dict) -> None:
            for bus in self._spares():
                for drv in self._spare_drivers():
                    dispatch_at = max(now + timedelta(minutes=2), _at(self.date, drv["shift_start"]))
                    s = self.net["stops"][start_stop]
                    eta = road_eta_min(self.net["depot"]["lat"], self.net["depot"]["lon"], s["lat"], s["lon"])
                    arrive = dispatch_at + timedelta(minutes=eta)
                    opts.append({**base, **extra, "id": f"{action}:{bus['id']}:{drv['id']}", "action": action,
                                 "summary": f"{label}: spare {bus['id']} with driver {drv['id']} from the depot, "
                                            f"at {self.net['stops'][start_stop]['name']} by {arrive:%H:%M}",
                                 "bus": bus["id"], "driver": drv["id"], "bus_accessible": bus["wheelchair_accessible"],
                                 "capacity": bus["capacity"], "driver_shift_end": drv["shift_end"][:5],
                                 "dispatch_at": f"{dispatch_at:%H:%M}", "eta": f"{arrive:%H:%M}", "finish_time": work_until,
                                 "stops_affected": stops, "wheelchair_waiting": wheelchair, "passengers_affected": waiting,
                                 # people board and alight along the way: peak on board ~ the busiest stop + half the rest
                                 "projected_load": _projected_load(action, waiting_by_stop, inc, arrive, work_until, self.date),
                                 # only a replacement leaves stops with no bus until it arrives
                                 "service_gap_min": max(0.0, (arrive - now).total_seconds() / 60) if action == "replacement_bus" else None})

        if kind in ("stopped",) or cause == "breakdown":
            first = stops[0] if stops else inc.get("stop_id")
            dispatch("replacement_bus", "Replace broken-down bus", first, self._trip_end(inc, extra_min=10), {})
            opts.append({**base, "id": "alert_only", "action": "alert_only", "summary": "Alert passengers only; no replacement",
                         "stops_affected": stops, "passengers_affected": waiting, "service_gap_min": 60.0})
        elif kind == "missing":
            opts.append({**base, "id": "replacement_bus:precaution", "action": "replacement_bus",
                         "summary": f"Send a spare bus to cover {inc['bus']} as a precaution", "bus": (self._spares() or [{}])[0].get("id"),
                         "driver": None, "stops_affected": stops})
            opts.append({**base, "id": "monitor", "action": "monitor",
                         "summary": f"Keep watching {inc['bus']} for 4 more minutes; act if it does not reappear", "stops_affected": stops})
        elif kind == "connection_risk":
            tr = inc["train"]
            ready = _at(self.date, str(tr["actual_arr"])[:5]) + timedelta(minutes=LIMITS["walk_train_to_bus_min"])
            hold = max(0, int((ready - _at(self.date, inc["connecting_departure"])).total_seconds() // 60) + 1)
            opts.append({**base, "id": f"hold:{hold}", "action": "hold", "hold_min": hold,
                         "summary": f"Hold the {inc['connecting_departure']} Route {inc['route_id']} bus for {hold} min",
                         "stops_affected": [inc["stop_id"]], "passengers_affected": tr["transfer_pax"]})
            dispatch("extra_bus", "Extra Route " + inc["route_id"] + " departure for the late train", inc["stop_id"],
                     (ready + timedelta(minutes=35)).strftime("%H:%M"), {"passengers_affected": tr["transfer_pax"]})
            wait = (_at(self.date, inc["next_departure"]) - ready).total_seconds() / 60 if inc.get("next_departure") else 60
            opts.append({**base, "id": "alert_only", "action": "alert_only",
                         "summary": f"Tell transferring passengers the next bus is at {inc.get('next_departure') or 'unknown'}",
                         "stops_affected": [inc["stop_id"]], "passengers_affected": tr["transfer_pax"], "service_gap_min": wait})
        elif kind in ("crowding", "bunching") or cause in ("event_crowding", "crowding_dwell"):
            stop = inc.get("stop_id") or (stops[0] if stops else None)
            if inc.get("waiting_by_stop"):
                waiting_by_stop = inc["waiting_by_stop"]
            dispatch("extra_bus", f"Extra shuttle runs for crowds on Route {inc.get('route_id')}", stop, "08:30",
                     {"passengers_affected": inc.get("waiting", waiting)})
            opts.append({**base, "id": "alert_only", "action": "alert_only", "summary": "Warn passengers to expect full buses",
                         "stops_affected": inc.get("stops") or [stop], "passengers_affected": inc.get("waiting", waiting),
                         "service_gap_min": None, "projected_load": inc.get("waiting"), "capacity": 120,
                         "bus": f"Route {inc.get('route_id')} regular buses"})
        elif kind == "proactive":
            first = inc["stop_id"]
            dispatch("extra_bus", f"Run an early extra Route {inc['route_id']} bus through the morning peak", first, "07:45",
                     {"stops_affected": inc.get("stops", []), "passengers_affected": None})
            opts.append({**base, "id": "alert_only", "action": "alert_only", "summary": "Only warn passengers early",
                         "stops_affected": inc.get("stops", []), "service_gap_min": None})
        elif kind == "late" or cause == "weather_congestion":
            opts.append({**base, "id": "alert_only", "action": "alert_only",
                         "summary": f"Update arrival times for Route {inc.get('route_id')} passengers",
                         "stops_affected": stops, "passengers_affected": waiting,
                         "service_gap_min": float(inc.get("late_min", 10))})
            first = stops[0] if stops else None
            if first:
                dispatch("extra_bus", f"Fill the gap on Route {inc.get('route_id')}", first, "08:30", {})
        elif kind == "proactive":
            first = inc["stop_id"]
            dispatch("extra_bus", f"Run an early extra Route {inc['route_id']} bus through the morning peak", first, "07:45",
                     {"stops_affected": inc.get("stops", []), "passengers_affected": None})
            opts.append({**base, "id": "alert_only", "action": "alert_only", "summary": "Only warn passengers early",
                         "stops_affected": inc.get("stops", []), "service_gap_min": float(inc.get("expected_delay_min", 12))})
        return opts

    # ------------------------------------------------------------- choose
    def choose(self, inc: dict, finding: dict, options: list[dict], feedback: list[dict], sim_time: str) -> Optional[dict]:
        if not options:
            return None
        payload = {"incident": {k: inc.get(k) for k in ("id", "type", "detail", "route_id", "stop_id", "bus")},
                   "cause": finding, "options": [_public(o) for o in options], "rejected_so_far": feedback}
        reply = self.ai.json_call(agent="Planner", system=SYSTEM, user=json.dumps(payload, default=str), sim_time=sim_time)
        if reply and reply.get("choice") in {o["id"] for o in options}:
            pick = next(o for o in options if o["id"] == reply["choice"])
            pick = {**pick, "reason": reply.get("reason", ""), "method": "nemotron-via-nightshift"}
        else:
            pick = {**_heuristic(options, inc), "method": "offline-rules"}
            pick["reason"] = _heuristic_reason(pick)
        self.trace.log("Planner", "proposed", sim_time=sim_time, output_summary=f"{pick['id']}: {pick['summary']}",
                       incident_id=inc["id"], method=pick["method"], options=[o["id"] for o in options])
        return pick

    # ------------------------------------------------------------- helpers
    def _spares(self) -> list[dict]:
        return [b for b in self.world.entities("bus") if not b.get("route_id")]

    def _spare_drivers(self) -> list[dict]:
        ds = [d for d in self.world.entities("driver") if d.get("status") == "spare"]
        return sorted(ds, key=lambda d: d["shift_end"])   # use up the shortest remaining shift first

    def _affected_stops(self, inc: dict) -> list[str]:
        if inc.get("trip_id") and inc.get("dist_along_m") is not None:
            return [s["stop_id"] for s in self.net["stop_times"][inc["trip_id"]] if s["shape_dist_traveled"] > inc["dist_along_m"]]
        if inc.get("trip_id"):
            return [s["stop_id"] for s in self.net["stop_times"][inc["trip_id"]]][1:]
        if inc.get("stop_id"):
            return [inc["stop_id"]]
        r = self.world.entity(f"route:{inc.get('route_id')}") if inc.get("route_id") else None
        return (r or {}).get("stops", [])

    def _waiting(self, stops: list[str], now: datetime) -> tuple[int, list[str], dict]:
        if not stops:
            return 0, [], {}
        rows, _ = self.z.query(
            "SELECT stop_id, waiting, wheelchair_waiting FROM passenger_counts WHERE service_date = :d AND stop_id IN :s "
            "AND window_start BETWEEN :a AND :b",
            {"d": self.date, "s": stops, "a": (now - timedelta(minutes=15)).strftime("%H:%M"), "b": (now + timedelta(minutes=30)).strftime("%H:%M")},
            agent="Planner", purpose="who is waiting at affected stops", sim_time=now.strftime("%H:%M"))
        per_stop: dict[str, int] = {}
        wc = set()
        for r in rows:
            per_stop[r["stop_id"]] = max(per_stop.get(r["stop_id"], 0), int(r["waiting"]))
            if int(r["wheelchair_waiting"] or 0) > 0:
                wc.add(r["stop_id"])
        return sum(per_stop.values()), sorted(wc), per_stop

    def _trip_end(self, inc: dict, extra_min: int = 0) -> str:
        sts = self.net["stop_times"].get(inc.get("trip_id"), [])
        if not sts:
            return "08:30"
        end = _at(self.date, sts[-1]["arrival_time"][:5]) + timedelta(minutes=extra_min + 15)
        return end.strftime("%H:%M")


def _projected_load(action: str, per_stop: dict, inc: dict, arrive, work_until: str, date: str):
    """Peak passengers on the dispatched bus.
    Replacement: carries everyone at the stranded stops (busiest stop + half the rest, as people alight).
    Extra bus for crowds: carries what the regular buses can't, spread over the shuttle runs it can make."""
    if not per_stop:
        return None
    if action == "replacement_bus":
        return _peak_load(per_stop, inc)
    if inc["type"] in ("crowding", "bunching"):
        regular = 2 * 60                                    # two scheduled buses in the window
        minutes = max(10.0, (_at(date, work_until) - arrive).total_seconds() / 60)
        runs = max(1, int(minutes // 15))                   # ~15 min per short Kensington-UNSW shuttle loop
        return int(max(0, sum(per_stop.values()) - regular) / runs)
    return None


def _peak_load(per_stop: dict, inc: dict) -> int:
    if not per_stop:
        return 0
    if inc["type"] in ("crowding", "bunching") or len(per_stop) == 1:
        return max(per_stop.values())          # one crowded stop: everyone boards at once
    vals = sorted(per_stop.values(), reverse=True)
    return int(vals[0] + 0.5 * sum(vals[1:]))


def _heuristic(options: list[dict], inc: dict) -> dict:
    """Offline choice: what a hurried controller might try first, which the Critic then checks.
    Cheapest/fastest action first; for spares, keep the accessible bus in reserve and use the
    driver with the shortest remaining shift first."""
    order = {"hold": 0, "alert_only": 1, "monitor": 2, "replacement_bus": 0, "extra_bus": 1}
    if inc["type"] in ("late",):
        order = {"alert_only": 0, "extra_bus": 1}
    if inc["type"] == "missing":
        order = {"replacement_bus": 0, "monitor": 1}
    if inc["type"] in ("crowding", "bunching", "proactive"):
        order = {"extra_bus": 0, "alert_only": 1}
    return sorted(options, key=lambda o: (order.get(o["action"], 9), o.get("bus_accessible", False), o.get("driver_shift_end") or ""))[0]


def _heuristic_reason(o: dict) -> str:
    return {"hold": "Holding the bus is the cheapest way to keep the connection.",
            "alert_only": "Least disruptive option; passengers get updated times.",
            "monitor": "No evidence of a real fault yet, so keep watching before committing a bus.",
            "replacement_bus": "Restores service to the stranded stops fastest.",
            "extra_bus": "Adds capacity where passengers are waiting."}.get(o["action"], "")


def _public(o: dict) -> dict:
    return {k: v for k, v in o.items() if k not in ("used_today",)}


def _at(date: str, hm: str) -> datetime:
    return datetime.fromisoformat(f"{date}T{hm[:5]}:00")
