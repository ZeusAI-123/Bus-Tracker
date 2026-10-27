
from __future__ import annotations

from typing import Any, Callable


class Tools:
    def __init__(self, zetaris, world, date: str):
        self.z = zetaris
        self.world = world
        self.date = date
        self.evidence: dict[str, dict] = {}   

    def _q(self, tool: str, sql: str, params: dict, purpose: str, sim_time: str) -> dict:
        rows, ev = self.z.query(sql, {"d": self.date, **params}, agent="Investigator", purpose=purpose, sim_time=sim_time)
        self.evidence[ev] = {"tool": tool, "args": params, "rows": rows}
        self.world.ingest(key=f"{ev}", entity_id=f"evidence:{tool}", kind="evidence",
                          value={"tool": tool, "args": params, "rows": rows[:20]}, sim_time=sim_time,
                          source=f"zetaris:{tool}", agent="Investigator")
        return {"evidence_id": ev, "rows": rows}

    def get_maintenance(self, bus_id: str, sim_time: str) -> dict:
        return self._q("get_maintenance",
                       "SELECT bus_id, reported_at, issue, severity FROM maintenance_reports "
                       "WHERE service_date = :d AND bus_id = :bus AND reported_at <= :t",
                       {"bus": bus_id, "t": sim_time}, f"maintenance reports for {bus_id}", sim_time)

    def get_driver_shift(self, bus_id: str, sim_time: str) -> dict:
        return self._q("get_driver_shift",
                       "SELECT driver_id, bus_id, shift_start, shift_end, status FROM driver_shifts "
                       "WHERE service_date = :d AND (bus_id = :bus OR status = 'spare')",
                       {"bus": bus_id}, f"driver shift for {bus_id} and spare drivers", sim_time)

    def get_weather(self, sim_time: str) -> dict:
        return self._q("get_weather",
                       "SELECT hour, precip_mm, temp_c, source FROM weather_hourly "
                       "WHERE service_date = :d AND hour BETWEEN :h0 AND :h1 ORDER BY hour",
                       {"h0": int(sim_time[:2]) - 1, "h1": int(sim_time[:2])}, "rain around now", sim_time)

    def get_morning_weather(self, sim_time: str) -> dict:
        return self._q("get_morning_weather",
                       "SELECT hour, precip_mm, temp_c, source FROM weather_hourly "
                       "WHERE service_date = :d AND hour BETWEEN 5 AND 8 ORDER BY hour",
                       {}, "rain this morning (05:00-09:00)", sim_time)

    def get_road_closures(self, route_id: str, sim_time: str) -> dict:
        return self._q("get_road_closures",
                       "SELECT * FROM road_closures WHERE service_date = :d",
                       {"route": route_id}, f"road closures (route {route_id})", sim_time)

    def get_events(self, stop_ids: list, sim_time: str) -> dict:
        return self._q("get_events",
                       "SELECT stop_id, name, \"start\", \"end\", expected_extra_pax FROM events "
                       "WHERE service_date = :d AND stop_id IN :stops",
                       {"stops": list(stop_ids) or ["-"]}, "events near these stops", sim_time)

    def get_train_delays(self, station_stop_id: str, sim_time: str) -> dict:
        return self._q("get_train_delays",
                       "SELECT train, scheduled_arr, actual_arr, transfer_pax, connecting_route FROM train_delays "
                       "WHERE service_date = :d AND station_stop_id = :s AND scheduled_arr BETWEEN :t0 AND :t1",
                       {"s": station_stop_id, "t0": _shift(sim_time, -30), "t1": _shift(sim_time, 30)},
                       f"trains into {station_stop_id}", sim_time)

    def get_passenger_counts(self, stop_ids: list, sim_time: str) -> dict:
        return self._q("get_passenger_counts",
                       "SELECT stop_id, window_start, waiting, wheelchair_waiting FROM passenger_counts "
                       "WHERE service_date = :d AND stop_id IN :stops AND window_start BETWEEN :t0 AND :t1 "
                       "ORDER BY stop_id, window_start",
                       {"stops": list(stop_ids) or ["-"], "t0": _shift(sim_time, -15), "t1": _shift(sim_time, 45)},
                       "passengers waiting", sim_time)

    def get_bus_track(self, bus_id: str, sim_time: str) -> dict:
        return self._q("get_bus_track",
                       "SELECT ts, lat, lon, speed_kmh, dist_along_m, trip_id FROM vehicle_positions "
                       "WHERE service_date = :d AND bus_id = :bus AND ts BETWEEN :t0 AND :t1 ORDER BY ts",
                       {"bus": bus_id, "t0": f"{self.date} {_shift(sim_time, -12)}:00", "t1": f"{self.date} {sim_time}:59"},
                       f"recent GPS track for {bus_id}", sim_time)

    def menu(self) -> dict[str, Callable[..., Any]]:
        return {"get_maintenance": self.get_maintenance, "get_driver_shift": self.get_driver_shift,
                "get_weather": self.get_weather, "get_road_closures": self.get_road_closures,
                "get_events": self.get_events, "get_train_delays": self.get_train_delays,
                "get_passenger_counts": self.get_passenger_counts, "get_bus_track": self.get_bus_track}


def _shift(hhmm: str, minutes: int) -> str:
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    total = max(0, min(23 * 60 + 59, h * 60 + m + minutes))
    return f"{total // 60:02d}:{total % 60:02d}"
