
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.geo import haversine_m  

NET_DIR = ROOT / "data" / "network"
OUT = ROOT / "data" / "sample"
SEED = 42


STOPS = {
    "FD":  ("Five Dock, Great North Rd", -33.8670, 151.1290),
    "HAB": ("Haberfield, Parramatta Rd", -33.8800, 151.1390),
    "LEI": ("Leichhardt, Norton St", -33.8835, 151.1570),
    "ANN": ("Annandale, Parramatta Rd", -33.8810, 151.1700),
    "RSQ": ("Railway Square (Central)", -33.8840, 151.2010),
    "DRM": ("Drummoyne, Victoria Rd", -33.8540, 151.1540),
    "ROZ": ("Rozelle, Victoria Rd", -33.8620, 151.1710),
    "GLB": ("Glebe Point Rd", -33.8800, 151.1850),
    "ULT": ("Ultimo, Harris St", -33.8790, 151.1980),
    "TWH": ("Town Hall", -33.8731, 151.2069),
    "MKV": ("Marrickville Metro", -33.9110, 151.1550),
    "PET": ("Petersham", -33.8940, 151.1550),
    "STM": ("Stanmore, Parramatta Rd", -33.8940, 151.1640),
    "NTN": ("Newtown, King St", -33.8980, 151.1790),
    "CEN": ("Central Station, Eddy Ave", -33.8833, 151.2075),
    "ERS": ("Erskineville", -33.9020, 151.1860),
    "ALX": ("Alexandria", -33.9050, 151.1940),
    "RDF": ("Redfern", -33.8920, 151.1985),
    "SRH": ("Surry Hills, Crown St", -33.8860, 151.2110),
    "MP":  ("Moore Park", -33.8916, 151.2246),
    "KEN": ("Kensington, Anzac Pde", -33.9070, 151.2240),
    "UNS": ("UNSW Kensington", -33.9170, 151.2310),
    "KFD": ("Kingsford", -33.9240, 151.2280),
    "BJN": ("Bondi Junction", -33.8915, 151.2480),
    "EDG": ("Edgecliff", -33.8790, 151.2370),
    "PAD": ("Paddington, Oxford St", -33.8840, 151.2290),
    "DLH": ("Darlinghurst, Oxford St", -33.8790, 151.2190),
    "WYN": ("Wynyard", -33.8660, 151.2056),
    "COG": ("Coogee", -33.9200, 151.2560),
    "RWK": ("Randwick", -33.9145, 151.2410),
    "ZET": ("Zetland", -33.9070, 151.2110),
    "WAT": ("Waterloo", -33.9000, 151.2060),
}
DEPOT = ("Inner West depot", -33.8790, 151.1600)

ROUTES = {
    "A": ("Five Dock - Central", ["FD", "HAB", "LEI", "ANN", "RSQ"]),
    "B": ("Drummoyne - Town Hall", ["DRM", "ROZ", "GLB", "ULT", "TWH"]),
    "C": ("Marrickville - Central", ["MKV", "PET", "STM", "NTN", "CEN"]),
    "D": ("Erskineville - Surry Hills", ["ERS", "ALX", "RDF", "WAT", "SRH"]),
    "E": ("Central - Kingsford (UNSW)", ["CEN", "MP", "KEN", "UNS", "KFD"]),
    "F": ("Bondi Junction - Wynyard", ["BJN", "EDG", "PAD", "DLH", "WYN"]),
    "G": ("Coogee - Central", ["COG", "RWK", "ZET", "WAT", "CEN"]),
    "H": ("Randwick - Town Hall", ["RWK", "MP", "PAD", "DLH", "TWH"]),
}
# Bus blocks: one bus per route, two on the busy UNSW route, two spares at the depot.
BLOCKS = {"A1": "A", "B1": "B", "C1": "C", "D1": "D", "E1": "E", "E2": "E", "F1": "F", "G1": "G", "H1": "H"}
FIRST_DEPARTURE = {"A1": "06:35", "B1": "06:38", "C1": "06:40", "D1": "06:36", "E1": "06:40",
                   "E2": "06:55", "F1": "06:37", "G1": "06:39", "H1": "06:42"}
PEAK_FACTOR = 1.35   # OSRM free-flow time -> peak-hour time
DWELL_S = 30
LAYOVER_MIN = 4

SLOW_SEGMENT = ("A", "LEI", "ANN")  
SHARED_STREAM = {"mon1", "mon2", "tue", "wed", "thu"}



def get_json(url: str, timeout: int = 60) -> dict:
    """requests first; fall back to curl (older macOS Python can't do the TLS handshake some hosts need)."""
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        import subprocess
        out = subprocess.run(["curl", "-sS", "-m", str(timeout), url], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)


def fetch_shapes(refresh: bool) -> dict:
    cache = NET_DIR / "shapes.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    shapes = {}
    for rid, (_, stops) in ROUTES.items():
        coords = ";".join(f"{STOPS[s][2]},{STOPS[s][1]}" for s in stops)
        url = f"https://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson"
        try:
            route = get_json(url, 30)["routes"][0]
            pts = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
            legs = [leg["duration"] for leg in route["legs"]]
            shapes[rid] = {"points": pts, "leg_durations_s": legs, "source": "osrm"}
            print(f"  route {rid}: {len(pts)} points from OSRM")
        except Exception as e:  # offline fallback: straight lines at 20 km/h
            pts = [(STOPS[s][1], STOPS[s][2]) for s in stops]
            legs = [haversine_m(*pts[i], *pts[i + 1]) * 1.3 / (20000 / 3600) for i in range(len(pts) - 1)]
            shapes[rid] = {"points": pts, "leg_durations_s": legs, "source": f"straight-line ({e.__class__.__name__})"}
            print(f"  route {rid}: OSRM unavailable, straight-line fallback")
    NET_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(shapes))
    return shapes


def fetch_weather(refresh: bool) -> dict:
    """Hourly Sydney rain for a winter season; cached."""
    cache = NET_DIR / "weather.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    url = ("https://archive-api.open-meteo.com/v1/archive?latitude=-33.87&longitude=151.21"
           "&start_date=2025-04-01&end_date=2025-09-30&hourly=precipitation,temperature_2m"
           "&timezone=Australia%2FSydney")
    try:
        h = get_json(url, 60)["hourly"]
        data = {"source": "open-meteo archive", "time": h["time"], "precip": h["precipitation"],
                "temp": h["temperature_2m"]}
    except Exception as e:
        print(f"  weather unavailable ({e.__class__.__name__}); using synthetic weather")
        data = {"source": "synthetic", "time": [], "precip": [], "temp": []}
    NET_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data))
    return data


def morning_rain(weather: dict, d: date) -> float:
    total = 0.0
    for t, p in zip(weather["time"], weather["precip"]):
        if t.startswith(d.isoformat()) and 6 <= int(t[11:13]) <= 8:
            total += p or 0.0
    return total


def pick_dates(weather: dict) -> dict:
    """Two real rainy Mondays (memory scenario) + dry Tue/Wed/Thu for the other scenarios."""
    if not weather["time"]:
        return {"mon1": "2025-06-16", "tue": "2025-06-17", "wed": "2025-06-18", "thu": "2025-06-19",
                "mon2": "2025-07-07", "closure": "2025-06-20", "busy": "2025-06-25", "_note": "synthetic weather fallback"}
    days = sorted({t[:10] for t in weather["time"]})
    ds = [date.fromisoformat(x) for x in days]
    rainy_mondays = [d for d in ds if d.weekday() == 0 and morning_rain(weather, d) >= 1.0]
    if len(rainy_mondays) < 2:
        rainy_mondays = sorted([d for d in ds if d.weekday() == 0], key=lambda d: -morning_rain(weather, d))[:2]
        rainy_mondays.sort()
    mon1 = rainy_mondays[0]
    mon2 = next((d for d in rainy_mondays if (d - mon1).days >= 14), rainy_mondays[-1])
    picks = {}
    for key, wd in (("tue", 1), ("wed", 2), ("thu", 3)):
        cands = [d for d in ds if d.weekday() == wd and d > mon1 and d < mon2 and morning_rain(weather, d) == 0]
        if not cands:
            cands = [d for d in ds if d.weekday() == wd and morning_rain(weather, d) == 0]
        picks[key] = min(cands, key=lambda d: (d - mon1).days).isoformat()
    
    after = date.fromisoformat(picks["thu"])
    for key, wd in (("closure", 4), ("busy", 2)):
        cands = [d for d in ds if d.weekday() == wd and d > after and morning_rain(weather, d) == 0
                 and d.isoformat() not in picks.values()]
        picks[key] = cands[0].isoformat()
        after = cands[0]
    return {"mon1": mon1.isoformat(), "mon2": mon2.isoformat(), **picks}


def cumulative(points):
    dist = [0.0]
    for i in range(1, len(points)):
        dist.append(dist[-1] + haversine_m(*points[i - 1], *points[i]))
    return dist


def stop_dists(points, cum, stop_ids):
    """Distance along the shape nearest to each stop (monotonic)."""
    out, start = [], 0
    for sid in stop_ids:
        _, lat, lon = STOPS[sid]
        best, best_i = 1e18, start
        for i in range(start, len(points)):
            d = haversine_m(lat, lon, *points[i])
            if d < best:
                best, best_i = d, i
        out.append(cum[best_i])
        start = best_i
    out[0], out[-1] = 0.0, cum[-1]
    return out


def point_at(points, cum, dist):
    if dist <= 0:
        return points[0]
    if dist >= cum[-1]:
        return points[-1]
    lo, hi = 0, len(cum) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cum[mid] <= dist:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo] or 1.0
    f = (dist - cum[lo]) / seg
    return (points[lo][0] + f * (points[hi][0] - points[lo][0]), points[lo][1] + f * (points[hi][1] - points[lo][1]))



def scenarios(dates: dict) -> dict:
    """Planted problems per morning. Times are local Sydney time."""
    return {
        dates["mon1"]: {"key": "mon1", "label": "Rainy Monday: Route A crawls on Parramatta Rd",
                        "rain_slowdown": True},
        dates["tue"]: {"key": "tue", "label": "Bus C1 breaks down; wheelchair user waiting at Newtown",
                       "breakdown": {"bus": "C1", "at": "07:20"},
                       "maintenance": [{"bus": "C1", "time": "06:50", "issue": "Engine warning light reported by driver at depot check", "severity": "medium"}],
                       "wheelchair": {"stop": "NTN", "from": "07:15", "to": "07:50"}},
        dates["wed"]: {"key": "wed", "label": "Late train at Central threatens Route E connection; GPS glitch on G1 (trap)",
                       "train_delay": {"station_stop": "CEN", "train": "T1 Western Line", "scheduled": "07:15", "actual": "07:24",
                                       "transfer_pax": 42, "route": "E"},
                       "gps_gap": {"bus": "G1", "from": "07:10", "to": "07:16"}},
        dates["thu"]: {"key": "thu", "label": "UNSW exam day crowds; Route E buses bunch",
                       "event": {"stop": "UNS", "name": "UNSW morning exam session", "from": "07:30", "to": "08:15", "extra_pax": 170,
                                 "also_stop": "KEN"},
                       "crowd_dwell": {"bus": "E1", "extra_dwell_min": 7, "stops": ["KEN", "UNS"], "after": "07:20"}},
        dates["mon2"]: {"key": "mon2", "label": "Rainy Monday again: memory should act before the delay",
                        "rain_slowdown": True},
        dates["closure"]: {"key": "closure", "label": "Lane closure on Glebe Point Rd slows Route B",
                           "road_closure": {"route": "B", "from_stop": "GLB", "to_stop": "ULT", "slow_factor": 0.3,
                                            "reason": "Emergency water main repair, one lane closed", "from": "06:50", "to": "09:00"}},
        dates["busy"]: {"key": "busy", "label": "Two problems at once: C1 breaks down, then UNSW crowds need a shuttle",
                        "breakdown": {"bus": "C1", "at": "07:20"},
                        "maintenance": [{"bus": "C1", "time": "06:50", "issue": "Engine warning light reported by driver at depot check", "severity": "medium"}],
                        "wheelchair": {"stop": "NTN", "from": "07:15", "to": "07:50"},
                        "event": {"stop": "UNS", "name": "UNSW graduation ceremony", "from": "07:45", "to": "08:30", "extra_pax": 130,
                                  "also_stop": "KEN"},
                        "extra_spare_driver": {"driver": "D12", "from": "07:00", "to": "14:00"}},
    }


def answer_key(dates: dict) -> dict:
    return {
        dates["mon1"]: {"scenario": "mon1", "expected": [
            {"route": "A", "type": "late", "cause": "weather_congestion", "actions_ok": ["extra_bus", "alert_only"],
             "must_not": []}]},
        dates["tue"]: {"scenario": "tue", "expected": [
            {"bus": "C1", "type": "stopped", "cause": "breakdown", "actions_ok": ["replacement_bus"],
             "must_use": {"bus": "S1"}, "must_not": [{"bus": "S2", "why": "not wheelchair accessible"}]}]},
        dates["wed"]: {"scenario": "wed", "expected": [
            {"route": "E", "type": "connection_risk", "cause": "late_train", "actions_ok": ["extra_bus", "alert_only"],
             "must_not": [{"action": "hold", "over_min": 5}]},
            {"bus": "G1", "type": "missing", "cause": "gps_glitch", "actions_ok": ["monitor"],
             "must_not": [{"action": "replacement_bus"}], "trap": True}]},
        dates["thu"]: {"scenario": "thu", "expected": [
            {"route": "E", "type": "crowding", "cause": "event_crowding", "actions_ok": ["extra_bus"],
             "must_use": {"driver": "D11"}, "must_not": [{"driver": "D10", "why": "shift ends 07:45"}]}]},
        dates["mon2"]: {"scenario": "mon2", "expected": [
            {"route": "A", "type": "proactive", "cause": "weather_congestion", "actions_ok": ["extra_bus"],
             "before": "07:05", "needs_memory": True}]},
        dates["closure"]: {"scenario": "closure", "expected": [
            {"route": "B", "type": "late", "cause": "road_closure", "actions_ok": ["alert_only", "extra_bus"],
             "must_not": []}]},
        dates["busy"]: {"scenario": "busy", "expected": [
            {"bus": "C1", "type": "stopped", "cause": "breakdown", "actions_ok": ["replacement_bus"],
             "must_use": {"bus": "S1"}, "must_not": [{"bus": "S2", "why": "not wheelchair accessible"}]},
            {"route": "E", "type": "crowding", "cause": "event_crowding", "actions_ok": ["extra_bus"],
             "must_not": [{"bus": "S1", "why": "already replacing C1"}, {"driver": "D10", "why": "shift ends 07:45"}]}]},
    }



def build(refresh: bool) -> None:
    random.seed(SEED)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vehicle_positions").mkdir(exist_ok=True)

    print("Road paths:")
    shapes = fetch_shapes(refresh)
    print("Weather:")
    weather = fetch_weather(refresh)
    dates = pick_dates(weather)
    print("Scenario dates:", dates)

    
    gtfs_routes = [{"route_id": r, "route_short_name": r, "route_long_name": n, "route_type": 3} for r, (n, _) in ROUTES.items()]
    gtfs_stops = [{"stop_id": s, "stop_name": n, "stop_lat": la, "stop_lon": lo, "wheelchair_boarding": 1} for s, (n, la, lo) in STOPS.items()]
    gtfs_shapes, geo = [], {}
    for rid, (_, stops) in ROUTES.items():
        pts = [tuple(p) for p in shapes[rid]["points"]]
        cum = cumulative(pts)
        sd = stop_dists(pts, cum, stops)
        legs = shapes[rid]["leg_durations_s"]
        for direction, (dpts, dstops) in enumerate([(pts, stops), (pts[::-1], stops[::-1])]):
            dcum = cumulative(dpts)
            dsd = stop_dists(dpts, dcum, dstops)
            dlegs = legs if direction == 0 else legs[::-1]
            shape_id = f"{rid}_{direction}"
            geo[shape_id] = {"points": dpts, "cum": dcum, "stop_dist": dsd, "stops": dstops,
                             "leg_s": [l * PEAK_FACTOR for l in dlegs]}
            for i, (la, lo) in enumerate(dpts):
                if i % 3 == 0 or i == len(dpts) - 1:
                    gtfs_shapes.append({"shape_id": shape_id, "shape_pt_lat": round(la, 6), "shape_pt_lon": round(lo, 6),
                                        "shape_pt_sequence": i, "shape_dist_traveled": round(dcum[i], 1)})

    # --- trips + planned stop_times per bus block (same timetable every weekday)
    trips, stop_times = [], []
    base = datetime(2000, 1, 1)
    end_service = base.replace(hour=8, minute=30)
    for bus, rid in BLOCKS.items():
        hh, mm = map(int, FIRST_DEPARTURE[bus].split(":"))
        t = base.replace(hour=hh, minute=mm)
        direction, n = 0, 0
        while t < end_service:
            shape_id = f"{rid}_{direction}"
            g = geo[shape_id]
            trip_id = f"{bus}_{n:02d}"
            trips.append({"route_id": rid, "service_id": "WKDY", "trip_id": trip_id, "direction_id": direction,
                          "shape_id": shape_id, "block_id": bus, "wheelchair_accessible": 1})
            for k, sid in enumerate(g["stops"]):
                if k > 0:
                    t = t + timedelta(seconds=g["leg_s"][k - 1] + DWELL_S)
                stop_times.append({"trip_id": trip_id, "stop_id": sid, "stop_sequence": k + 1,
                                   "arrival_time": t.strftime("%H:%M:%S"), "departure_time": t.strftime("%H:%M:%S"),
                                   "shape_dist_traveled": round(g["stop_dist"][k], 1)})
            t = t + timedelta(minutes=LAYOVER_MIN)
            direction, n = 1 - direction, n + 1


    buses = [{"bus_id": b, "route_id": r, "capacity": 60, "wheelchair_accessible": True, "home_lat": DEPOT[1], "home_lon": DEPOT[2]} for b, r in BLOCKS.items()]
    buses += [{"bus_id": "S1", "route_id": None, "capacity": 60, "wheelchair_accessible": True, "home_lat": DEPOT[1], "home_lon": DEPOT[2]},
              {"bus_id": "S2", "route_id": None, "capacity": 60, "wheelchair_accessible": False, "home_lat": DEPOT[1], "home_lon": DEPOT[2]}]

    sc = scenarios(dates)
    akey = answer_key(dates)
    tables = {k: [] for k in ("driver_shifts", "maintenance_reports", "road_closures", "events", "train_delays", "passenger_counts", "weather_hourly")}
    for key, d in [(k, v) for k, v in dates.items() if not k.startswith("_")]:
        s = sc[d]
        rng = random if key in SHARED_STREAM else random.Random(f"{SEED}-{d}")
        
        for i, bus in enumerate(BLOCKS):
            tables["driver_shifts"].append({"service_date": d, "driver_id": f"D{i+1:02d}", "bus_id": bus, "shift_start": "05:45", "shift_end": "13:45", "status": "on_shift"})
        tables["driver_shifts"].append({"service_date": d, "driver_id": "D10", "bus_id": None, "shift_start": "06:15", "shift_end": "07:45", "status": "spare"})
        tables["driver_shifts"].append({"service_date": d, "driver_id": "D11", "bus_id": None, "shift_start": "07:15", "shift_end": "14:15", "status": "spare"})
        if s.get("extra_spare_driver"):
            x = s["extra_spare_driver"]
            tables["driver_shifts"].append({"service_date": d, "driver_id": x["driver"], "bus_id": None, "shift_start": x["from"], "shift_end": x["to"], "status": "spare"})
        if s.get("road_closure"):
            rc = s["road_closure"]
            tables["road_closures"].append({"service_date": d, "route_id": rc["route"], "from_stop": rc["from_stop"], "to_stop": rc["to_stop"],
                                            "reason": rc["reason"], "start": rc["from"], "end": rc["to"]})
        
        for t, p, tmp in zip(weather["time"], weather["precip"], weather["temp"]):
            if t.startswith(d) and 5 <= int(t[11:13]) <= 9:
                tables["weather_hourly"].append({"service_date": d, "hour": int(t[11:13]), "precip_mm": p or 0.0, "temp_c": tmp, "source": weather["source"]})
        if weather["source"] == "synthetic":
            for h in range(5, 10):
                tables["weather_hourly"].append({"service_date": d, "hour": h, "precip_mm": 2.0 if s.get("rain_slowdown") else 0.0, "temp_c": 12.0, "source": "synthetic"})
        
        tables["maintenance_reports"].append({"service_date": d, "bus_id": "F1", "reported_at": "06:20", "issue": "Rear door sensor intermittent", "severity": "low"})
        for m in s.get("maintenance", []):
            tables["maintenance_reports"].append({"service_date": d, "bus_id": m["bus"], "reported_at": m["time"], "issue": m["issue"], "severity": m["severity"]})

        e_deps = sorted(st["departure_time"][:5] for st in stop_times if st["stop_id"] == "CEN" and st["stop_sequence"] == 1
                        and st["trip_id"].startswith("E") and "07:15" <= st["departure_time"][:5] <= "08:10")
        td = s.get("train_delay")
        if td and e_deps:
            dep = datetime.strptime(e_deps[0], "%H:%M")
            td["scheduled"] = (dep - timedelta(minutes=6)).strftime("%H:%M")
            td["actual"] = (dep + timedelta(minutes=3)).strftime("%H:%M")
            td["connecting_departure"] = e_deps[0]
        scheds = sorted({"06:45", "07:00", "07:30", "07:45", "08:00"} | ({td["scheduled"]} if td else set()))
        for sched in scheds:
            row = {"service_date": d, "station_stop_id": "CEN", "train": "T1 Western Line", "scheduled_arr": sched, "actual_arr": sched, "transfer_pax": 25, "connecting_route": "E"}
            td = s.get("train_delay")
            if td and td["scheduled"] == sched:
                row.update({"actual_arr": td["actual"], "transfer_pax": td["transfer_pax"]})
            tables["train_delays"].append(row)
        if s.get("event"):
            e = s["event"]
            tables["events"].append({"service_date": d, "stop_id": e["stop"], "name": e["name"], "start": e["from"], "end": e["to"], "expected_extra_pax": e["extra_pax"]})
            if e.get("also_stop"):
                tables["events"].append({"service_date": d, "stop_id": e["also_stop"], "name": e["name"], "start": e["from"], "end": e["to"], "expected_extra_pax": e["extra_pax"] // 2})
       
        for sid in STOPS:
            for q in range(8):  
                t = (base.replace(hour=6, minute=30) + timedelta(minutes=15 * q)).strftime("%H:%M")
                waiting = rng.randint(3, 18) + (12 if sid in ("CEN", "RSQ", "TWH", "WYN") else 0)
                wc = 0
                e = s.get("event")
                if e and sid in (e["stop"], e.get("also_stop")) and e["from"] <= t <= e["to"]:
                    waiting += e["extra_pax"] if sid == e["stop"] else e["extra_pax"] // 2
                w = s.get("wheelchair")
                if w and sid == w["stop"] and w["from"] <= t <= w["to"]:
                    wc = 1
                tables["passenger_counts"].append({"service_date": d, "stop_id": sid, "window_start": t, "waiting": waiting, "wheelchair_waiting": wc})

   
    trip_rows = pd.DataFrame(trips)
    st_rows = pd.DataFrame(stop_times)
    for key, d in [(k, v) for k, v in dates.items() if not k.startswith("_")]:
        s = sc[d]
        rng = random if key in SHARED_STREAM else random.Random(f"{SEED}-pos-{d}")
        rain_hours = {r["hour"] for r in tables["weather_hourly"] if r["service_date"] == d and r["precip_mm"] >= 0.3}
        rows = []
        for bus, rid in BLOCKS.items():
            btrips = trip_rows[trip_rows.block_id == bus].sort_values("trip_id")
            clock = None
            frozen = None
            for _, tr in btrips.iterrows():
                g = geo[tr.shape_id]
                sts = st_rows[st_rows.trip_id == tr.trip_id].sort_values("stop_sequence")
                planned_start = datetime.fromisoformat(f"{d}T{sts.iloc[0].departure_time}")
                t = planned_start if clock is None else max(planned_start, clock + timedelta(minutes=LAYOVER_MIN))
                if clock is not None:  # layover pings at terminal
                    lt = clock
                    while lt < t:
                        lat, lon = g["points"][0]
                        rows.append(_ping(bus, rid, tr.trip_id, lt, lat, lon, 0.0, 0.0))
                        lt += timedelta(seconds=30)
                dist, k, dwell_left = 0.0, 1, 0.0
                total = g["cum"][-1]
                while dist < total and t.hour < 9:
                    if frozen:
                        lat, lon = frozen
                        rows.append(_ping(bus, rid, tr.trip_id, t, lat, lon, 0.0, dist))
                        t += timedelta(seconds=30)
                        if t.strftime("%H:%M") >= "08:30":
                            break
                        continue
                    bd = s.get("breakdown")
                    if bd and bd["bus"] == bus and t.strftime("%H:%M") >= bd["at"]:
                        frozen = point_at(g["points"], g["cum"], dist)
                        continue
                    seg_len = g["stop_dist"][k] - g["stop_dist"][k - 1] or 1.0
                    speed = seg_len / max(g["leg_s"][k - 1], 30)  
                    f = rng.uniform(0.88, 1.12)
                    if t.hour in rain_hours:
                        f *= 0.95
                    
                    if s.get("rain_slowdown") and rid == SLOW_SEGMENT[0] and tr.direction_id == 0 \
                            and g["stops"][k - 1] == SLOW_SEGMENT[1]:
                        f *= 0.4
                    
                    rc = s.get("road_closure")
                    if rc and rid == rc["route"] and {g["stops"][k - 1], g["stops"][k]} == {rc["from_stop"], rc["to_stop"]} \
                            and rc["from"] <= t.strftime("%H:%M") < rc["to"]:
                        f *= rc["slow_factor"]
                    step = 30.0
                    if dwell_left > 0:
                        move = 0.0
                        dwell_left -= step
                    else:
                        move = speed * f * step
                    dist = min(total, dist + move)
                    if k < len(g["stop_dist"]) - 1 and dist >= g["stop_dist"][k]:
                        dwell_left = DWELL_S
                        cd = s.get("crowd_dwell")
                        if cd and cd["bus"] == bus and g["stops"][k] in cd["stops"] and t.strftime("%H:%M") >= cd["after"]:
                            dwell_left += cd["extra_dwell_min"] * 60 / len(cd["stops"])
                        k += 1
                    lat, lon = point_at(g["points"], g["cum"], dist)
                    lat += rng.uniform(-0.00008, 0.00008)
                    lon += rng.uniform(-0.00008, 0.00008)
                    rows.append(_ping(bus, rid, tr.trip_id, t, lat, lon, round(move / step * 3.6, 1), dist))
                    t += timedelta(seconds=30)
                clock = t
                if t.strftime("%H:%M") >= "08:30":
                    break
        df = pd.DataFrame(rows)
        df = df[(df.ts >= f"{d} 06:30:00") & (df.ts <= f"{d} 08:30:00")]
        gap = s.get("gps_gap")
        if gap:
            df = df[~((df.bus_id == gap["bus"]) & (df.ts >= f"{d} {gap['from']}:00") & (df.ts < f"{d} {gap['to']}:00"))]
        df.insert(0, "service_date", d)
        df.to_parquet(OUT / "vehicle_positions" / f"{d}.parquet", index=False)
        print(f"  {key} {d}: {len(df)} positions, rain hours {sorted(rain_hours)}")

    
    empty_cols = {"road_closures": ["service_date", "route_id", "from_stop", "to_stop", "reason", "start", "end"],
                  "events": ["service_date", "stop_id", "name", "start", "end", "expected_extra_pax"]}

    def w(name, rows):
        pd.DataFrame(rows, columns=None if rows else empty_cols.get(name)).to_csv(OUT / f"{name}.csv", index=False)

    w("gtfs_routes", gtfs_routes)
    w("gtfs_stops", gtfs_stops)
    w("gtfs_trips", trips)
    w("gtfs_stop_times", stop_times)
    w("gtfs_shapes", gtfs_shapes)
    w("buses", buses)
    for name, rows in tables.items():
        w(name, rows)
    (OUT / "depot.json").write_text(json.dumps({"name": DEPOT[0], "lat": DEPOT[1], "lon": DEPOT[2]}))
    (OUT / "scenarios.json").write_text(json.dumps({"dates": {k: v for k, v in dates.items()}, "scenarios": sc}, indent=2))
    (OUT / "answer_key.json").write_text(json.dumps(akey, indent=2))
    print(f"Done. Data in {OUT}")


def _ping(bus, rid, trip_id, t, lat, lon, speed, dist):
    return {"bus_id": bus, "route_id": rid, "trip_id": trip_id, "ts": t.strftime("%Y-%m-%d %H:%M:%S"),
            "lat": round(lat, 6), "lon": round(lon, 6), "speed_kmh": speed, "dist_along_m": round(dist, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-fetch road paths and weather")
    build(ap.parse_args().refresh)
