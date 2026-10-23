"""Monitor: pure code, runs every tick. Turns raw positions and feeds into incidents.

Raises one incident per problem (later ticks update it, they don't duplicate it):
  late, stopped, missing, bunching (per bus/route) and connection_risk, crowding (per stop).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.config import LIMITS


def _t(date: str, hhmmss: str) -> datetime:
    return datetime.fromisoformat(f"{date}T{hhmmss if len(hhmmss) == 8 else hhmmss + ':00'}")


class Monitor:
    def __init__(self, date: str, network: dict, zetaris, trace):
        self.date = date
        self.net = network          # trips, stop_times by trip, buses, stops
        self.z = zetaris
        self.trace = trace
        self.history: dict[str, list[dict]] = {}   # bus -> pings
        self.open: dict[str, dict] = {}            # incident key -> incident
        self.counter = 0

    # ---------------------------------------------------------------- ingest
    def add_pings(self, pings: list[dict]) -> None:
        for p in pings:
            self.history.setdefault(p["bus_id"], []).append(p)

    # ---------------------------------------------------------------- checks
    def check(self, now: datetime) -> tuple[list[dict], list[dict]]:
        """Returns (new_incidents, resolved_incidents)."""
        new, resolved = [], []
        hm = now.strftime("%H:%M")
        for bus, info in self.net["buses"].items():
            if not info.get("route_id"):
                continue
            pings = self.history.get(bus, [])
            trip = self._active_trip(bus, now)
            last = pings[-1] if pings else None
            # missing: in service but silent
            if trip and (last is None or (now - _ts(last)).total_seconds() / 60 >= LIMITS["missing_threshold_min"]):
                silent = (now - _ts(last)).total_seconds() / 60 if last else None
                inc = self._raise(f"missing:{bus}", "missing", now, bus=bus, route_id=info["route_id"],
                                  detail=f"no position from {bus} for {silent:.0f} min" if silent else f"{bus} has not reported",
                                  last_seen=last)
                if inc:
                    new.append(inc)
            elif f"missing:{bus}" in self.open and last and (now - _ts(last)).total_seconds() < 120:
                inc = self.open.pop(f"missing:{bus}")
                inc["resolved_at"] = hm
                inc["resolution"] = f"{bus} reporting again"
                resolved.append(inc)
            if not last or not trip:
                continue
            # late: compare actual distance along trip with the timetable
            own_trip = self.net["trips"].get(last["trip_id"])
            already = any(k in self.open for k in (f"stopped:{bus}", f"missing:{bus}"))
            if own_trip and not already:
                late = self._lateness_min(own_trip, last)
                if late is not None and late >= LIMITS["late_threshold_min"]:
                    inc = self._raise(f"late:{bus}", "late", now, bus=bus, route_id=info["route_id"],
                                      detail=f"{bus} running {late:.0f} min late", late_min=round(late, 1),
                                      trip_id=last["trip_id"], position=_pos(last))
                    if inc:
                        new.append(inc)
                    else:
                        self.open[f"late:{bus}"]["late_min"] = max(self.open[f"late:{bus}"].get("late_min", 0), round(late, 1))
            # stopped: not moving, away from any stop
            recent = [p for p in pings if (now - _ts(p)).total_seconds() <= LIMITS["stopped_threshold_min"] * 60]
            if len(recent) >= LIMITS["stopped_threshold_min"] * 2 and all(p["speed_kmh"] < 2 for p in recent) \
                    and not self._near_stop(last):
                since = next((p for p in reversed(pings) if p["speed_kmh"] >= 2), recent[0])
                inc = self._raise(f"stopped:{bus}", "stopped", now, bus=bus, route_id=info["route_id"],
                                  detail=f"{bus} stationary between stops since {str(since['ts'])[11:16]}",
                                  stationary_since=str(since["ts"])[11:16],
                                  trip_id=last["trip_id"], position=_pos(last), dist_along_m=last["dist_along_m"])
                if inc:
                    new.append(inc)
        new += self._bunching(now)
        return new, resolved

    def check_feeds(self, now: datetime) -> list[dict]:
        """Station connections and crowding, from Zetaris feeds (train_delays, passenger_counts)."""
        new = []
        hm = now.strftime("%H:%M")
        horizon = (now + timedelta(minutes=20)).strftime("%H:%M")
        trains, ev = self.z.query(
            "SELECT train, station_stop_id, scheduled_arr, actual_arr, transfer_pax, connecting_route FROM train_delays "
            "WHERE service_date = :d AND scheduled_arr BETWEEN :a AND :b AND actual_arr > scheduled_arr",
            {"d": self.date, "a": hm, "b": horizon}, agent="Monitor", purpose="late trains in the next 20 min", sim_time=hm)
        for tr in trains:
            sched, actual = str(tr["scheduled_arr"])[:5], str(tr["actual_arr"])[:5]
            ready = (_t(self.date, actual) + timedelta(minutes=LIMITS["walk_train_to_bus_min"])).strftime("%H:%M")
            planned_ready = (_t(self.date, sched) + timedelta(minutes=LIMITS["walk_train_to_bus_min"])).strftime("%H:%M")
            dep = self._next_departure(tr["connecting_route"], tr["station_stop_id"], planned_ready)
            if dep and dep < ready:
                after = self._next_departure(tr["connecting_route"], tr["station_stop_id"], ready)
                inc = self._raise(f"conn:{tr['train']}:{sched}", "connection_risk", now, route_id=tr["connecting_route"],
                                  stop_id=tr["station_stop_id"],
                                  detail=(f"{tr['train']} due {sched} now arriving {actual}: {tr['transfer_pax']} passengers "
                                          f"will miss the {dep} Route {tr['connecting_route']} bus"),
                                  train=tr, connecting_departure=dep, next_departure=after, evidence=[ev])
                if inc:
                    new.append(inc)
        crowds, ev2 = self.z.query(
            "SELECT stop_id, window_start, waiting FROM passenger_counts "
            "WHERE service_date = :d AND window_start BETWEEN :a AND :b AND waiting > :cap",
            {"d": self.date, "a": hm, "b": horizon, "cap": LIMITS["bus_capacity_default"]},
            agent="Monitor", purpose="stops with more people than a bus holds", sim_time=hm)
        by_route: dict[str, dict] = {}
        for c in crowds:
            routes = [r for r, rs in self.net["route_stops"].items() if c["stop_id"] in rs]
            rid = routes[0] if routes else "?"
            g = by_route.setdefault(rid, {"stops": {}, "from": str(c["window_start"])[:5]})
            g["stops"][c["stop_id"]] = max(g["stops"].get(c["stop_id"], 0), int(c["waiting"]))
        for rid, g in by_route.items():
            stops = sorted(g["stops"], key=lambda s: -g["stops"][s])
            total = sum(g["stops"].values())
            names = ", ".join(f"{self.net['stops'][s]['name']} ({g['stops'][s]})" for s in stops)
            inc = self._raise(f"crowd:{rid}", "crowding", now, stop_id=stops[0], stops=stops, route_id=rid,
                              detail=f"{total} people expected from {g['from']} at {names}", waiting=total,
                              waiting_by_stop=g["stops"], evidence=[ev2])
            if inc:
                new.append(inc)
        return new

    # ---------------------------------------------------------------- helpers
    def _raise(self, key: str, itype: str, now: datetime, **fields) -> Optional[dict]:
        if key in self.open:
            return None
        self.counter += 1
        inc = {"id": f"inc-{self.counter}", "key": key, "type": itype, "detected_at": now.strftime("%H:%M"), **fields}
        self.open[key] = inc
        self.trace.log("Monitor", "raise_incident", sim_time=inc["detected_at"], output_summary=inc.get("detail", itype),
                       incident_id=inc["id"], incident_type=itype)
        return inc

    def _active_trip(self, bus: str, now: datetime) -> Optional[dict]:
        hm = now.strftime("%H:%M:%S")
        cur = None
        for trip in self.net["trips_by_bus"].get(bus, []):
            sts = self.net["stop_times"][trip["trip_id"]]
            if sts[0]["departure_time"] <= hm:
                cur = trip
        return cur

    def _lateness_min(self, trip: dict, ping: dict) -> Optional[float]:
        sts = self.net["stop_times"][trip["trip_id"]]
        d = ping["dist_along_m"]
        for a, b in zip(sts, sts[1:]):
            if a["shape_dist_traveled"] <= d <= b["shape_dist_traveled"]:
                ta, tb = _t(self.date, a["departure_time"]), _t(self.date, b["arrival_time"])
                span = b["shape_dist_traveled"] - a["shape_dist_traveled"] or 1
                planned = ta + (tb - ta) * ((d - a["shape_dist_traveled"]) / span)
                return (_ts(ping) - planned).total_seconds() / 60
        return None

    def _near_stop(self, ping: dict) -> bool:
        sts = self.net["stop_times"].get(ping["trip_id"], [])
        return any(abs(ping["dist_along_m"] - s["shape_dist_traveled"]) < 80 for s in sts)

    def _bunching(self, now: datetime) -> list[dict]:
        new = []
        by_route: dict[str, list] = {}
        for bus, info in self.net["buses"].items():
            if info.get("route_id") and self.history.get(bus):
                by_route.setdefault(info["route_id"], []).append(self.history[bus][-1])
        for route, lasts in by_route.items():
            if len(lasts) < 2:
                continue
            for i in range(len(lasts)):
                for j in range(i + 1, len(lasts)):
                    a, b = lasts[i], lasts[j]
                    ta, tb = self.net["trip_dir"].get(a["trip_id"]), self.net["trip_dir"].get(b["trip_id"])
                    if ta is None or ta != tb or a["speed_kmh"] < 2 and b["speed_kmh"] < 2:
                        continue
                    if abs(a["dist_along_m"] - b["dist_along_m"]) < LIMITS["bunching_threshold_m"] \
                            and (now - _ts(a)).total_seconds() < 120 and (now - _ts(b)).total_seconds() < 120:
                        inc = self._raise(f"bunch:{route}", "bunching", now, route_id=route,
                                          detail=f"{a['bus_id']} and {b['bus_id']} running together on Route {route}",
                                          buses=[a["bus_id"], b["bus_id"]], position=_pos(a))
                        if inc:
                            new.append(inc)
        return new

    def _next_departure(self, route: str, stop_id: str, after_hm: str) -> Optional[str]:
        best = None
        for trip in self.net["trips_by_route"].get(route, []):
            for st in self.net["stop_times"][trip["trip_id"]]:
                if st["stop_id"] == stop_id and st["stop_sequence"] == 1 and st["departure_time"][:5] >= after_hm:
                    if best is None or st["departure_time"][:5] < best:
                        best = st["departure_time"][:5]
        return best


def _ts(p: dict) -> datetime:
    return datetime.fromisoformat(str(p["ts"]).replace(" ", "T"))


def _pos(p: dict) -> dict:
    return {"lat": p["lat"], "lon": p["lon"], "at": str(p["ts"])[11:16]}
