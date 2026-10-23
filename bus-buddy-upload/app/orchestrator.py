"""Runs one morning: load network -> pre-check -> 2-minute ticks -> learn -> report.

Agent loop per incident:
  Investigator (why, with evidence) -> Planner (pick an option) <-> Safety Critic
  (reject -> drop options sharing the problem -> Planner again, max 3 attempts)
  -> Notifier (service alert + decision card). Unresolved -> escalate to a human.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.adapters.nightshift import Nightshift
from app.adapters.zetaris import Zetaris
from app.agents.critic import SafetyCritic, exclude_after_rejection
from app.agents.investigator import Investigator
from app.agents.learner import Learner, PreCheck
from app.agents.monitor import Monitor
from app.agents.notifier import Notifier
from app.agents.planner import Planner
from app.agents.tools import Tools
from app.config import SERVICE_END, SERVICE_START, SETTINGS, TICK_MINUTES
from app.engines.hmem import HMem
from app.engines.world_model import WorldModel
from app.tracing import Trace

MAX_ATTEMPTS = 3

AGENTS = [
    {"name": "Monitor", "role": "Code. Watches positions and feeds every 2 min; raises incidents"},
    {"name": "PreCheck", "role": "Start of morning: today's conditions + memory -> act before delays"},
    {"name": "Investigator", "role": "Nemotron. Chooses Zetaris tools, explains WHY with cited evidence"},
    {"name": "Planner", "role": "Code computes options; Nemotron picks one and explains"},
    {"name": "SafetyCritic", "role": "Hard rules in code + Nemotron review; can block and send back"},
    {"name": "Notifier", "role": "Nemotron (small). Passenger service alerts + controller decision card"},
    {"name": "Learner", "role": "End of morning: writes lessons to H-MEM"},
]


def run_morning(date: str, *, memory: str = "on", sample_mode: Optional[bool] = None,
                run_id: Optional[str] = None, memory_db: Optional[Path] = None) -> dict:
    started = time.time()
    run_id = run_id or f"{date}-{'mem' if memory == 'on' else 'nomem'}-{uuid.uuid4().hex[:6]}"
    trace = Trace(run_id, SETTINGS.logs_dir)
    sample = SETTINGS.sample_mode if sample_mode is None else sample_mode
    trace.log("Orchestrator", "start_run", output_summary=f"date={date} memory={memory} sample_mode={sample}")

    z = Zetaris(sample_mode=sample, trace=trace)
    ai = Nightshift(trace=trace, sample_mode=sample)
    world = WorldModel(SETTINGS.runs_dir / run_id / "world.db", run_id)
    mem = HMem(":memory:" if memory == "off" else (memory_db or SETTINGS.memory_db), trace=trace)

    net = load_network(z, world, date, trace)
    tools = Tools(z, world, date)
    monitor = Monitor(date, net, z, trace)
    investigator = Investigator(ai, tools, mem, trace, date)
    planner = Planner(ai, world, net, z, trace, date)
    critic = SafetyCritic(ai, trace)
    notifier = Notifier(ai, net, trace)

    start = datetime.fromisoformat(f"{date}T{SERVICE_START}:00")
    end = datetime.fromisoformat(f"{date}T{SERVICE_END}:00")
    resume_from = world.last_checkpoint()
    handled: list[dict] = world.get_meta("handled", [])
    alerts: list[dict] = world.get_meta("alerts", [])
    resolved_log: list[dict] = []
    timeline: list[dict] = []

    def handle(inc: dict, now: datetime) -> None:
        t = now.strftime("%H:%M")
        ctx = world.get_context(bus_id=inc.get("bus"), route_id=inc.get("route_id"),
                                stop_ids=[inc["stop_id"]] if inc.get("stop_id") else None, at=t)
        world.upsert_entity(f"incident:{inc['id']}", "incident", inc, source="monitor", agent="Monitor")
        if inc["type"] == "proactive":
            finding = {"cause": "weather_congestion", "confidence": 0.8, "corroborated": True,
                       "evidence_ids": inc.get("evidence", []), "method": "memory (H-MEM) + weather",
                       "explanation": f"It is raining and memory says: {inc['lesson']['summary']}",
                       "memory_used": [inc["lesson"]]}
        else:
            finding = investigator.investigate(inc, ctx)
        lessons = finding.get("memory_used", [])
        options = planner.options(inc, finding, now)
        attempts, final = [], None
        for _ in range(MAX_ATTEMPTS):
            plan = planner.choose(inc, finding, options, [{"plan": a["plan"]["summary"], "problems": a["verdict"]["problems"]} for a in attempts], t)
            if not plan:
                break
            verdict = critic.review(plan, lessons, t)
            attempts.append({"plan": plan, "verdict": verdict})
            if verdict["approved"]:
                final = plan
                break
            options = exclude_after_rejection(options, plan, verdict["problems"])
        if final is None:
            trace.log("Orchestrator", "escalate_to_human", sim_time=t, status="escalated", incident_id=inc["id"],
                      output_summary="No plan passed the Safety Critic within 3 attempts")
        else:
            used = world.get_meta("used_resources", [])
            if final["action"] in ("replacement_bus", "extra_bus"):
                used += [x for x in (final.get("bus"), final.get("driver")) if x]
                world.set_meta("used_resources", used)
            new_alerts = notifier.alerts(inc, finding, final, t)
            alerts.extend(new_alerts)
        world.upsert_entity(f"decision:{inc['id']}", "decision",
                            {"incident": inc["id"], "plan": final["id"] if final else "escalated"}, source="orchestrator", agent="Orchestrator")
        handled.append({"incident": inc, "finding": finding,
                        "attempts": [{"plan": _plan_view(a["plan"]), "verdict": a["verdict"]} for a in attempts],
                        "decision": _plan_view(final) if final else {"action": "escalated", "summary": "Escalated to the human controller"},
                        "decision_card": notifier.decision_card(inc, finding, attempts, final)})
        world.set_meta("handled", handled)
        world.set_meta("alerts", alerts)

    # ---- start of morning
    conditions = {}
    if not resume_from:
        pc = PreCheck(mem, tools, net, trace, date)
        conditions, proactive = pc.run(start)
        world.set_meta("conditions", conditions)
        for p in proactive:
            handle(p, start)
    else:
        conditions = world.get_meta("conditions", {})
        trace.log("Orchestrator", "resume", output_summary=f"resuming after {resume_from}")

    # ---- ticks
    prev = start - timedelta(minutes=TICK_MINUTES)
    now = start
    while now <= end:
        hm = now.strftime("%H:%M")
        pings, _ = z.query("SELECT bus_id, route_id, trip_id, ts, lat, lon, speed_kmh, dist_along_m FROM vehicle_positions "
                           "WHERE service_date = :d AND ts > :a AND ts <= :b ORDER BY ts",
                           {"d": date, "a": prev.strftime("%Y-%m-%d %H:%M:%S"), "b": now.strftime("%Y-%m-%d %H:%M:%S")},
                           agent="Monitor", purpose="new bus positions", sim_time=hm)
        for p in pings:
            p["ts"] = str(p["ts"]).replace("T", " ")
            world.ingest(key=f"pos:{p['bus_id']}:{p['ts']}", entity_id=p["bus_id"], kind="position", value=p,
                         sim_time=p["ts"][11:16], source="zetaris:vehicle_positions", agent="Monitor")
        monitor.add_pings(pings)
        if resume_from and hm <= resume_from:
            prev, now = now, now + timedelta(minutes=TICK_MINUTES)
            continue
        new, resolved = monitor.check(now)
        new += monitor.check_feeds(now)
        for inc in resolved:
            resolved_log.append(inc)
            for h in handled:
                if h["incident"]["id"] == inc["id"]:
                    h["incident"]["resolved_at"] = inc["resolved_at"]
                    h["incident"]["resolution"] = inc["resolution"]
                    if h["finding"].get("cause") == "gps_glitch":
                        h["finding"]["confirmed"] = True
            trace.log("Monitor", "resolved", sim_time=hm, output_summary=f"{inc['id']}: {inc['resolution']}")
        for inc in new:
            handle(inc, now)
        timeline.append({"t": hm, "buses": [_bus_state(b, monitor, net) for b in net["buses"] if net["buses"][b].get("route_id")]})
        world.checkpoint(hm)
        prev, now = now, now + timedelta(minutes=TICK_MINUTES)

    # ---- end of morning
    learner = Learner(mem, trace)
    lesson_changes = learner.learn(date, handled, conditions)
    world.commit()
    result = {
        "run_id": run_id, "date": date, "city": "Sydney", "status": "success",
        "mode": {"sample_mode": sample, "memory": memory, "ai": "replay/offline" if sample else "nightshift"},
        "conditions": conditions,
        "incidents": [_incident_view(h) for h in handled],
        "service_alerts": alerts,
        "memory": {"lesson_changes": lesson_changes,
                   "lessons": [{"key": m["key"], "summary": m["content"].get("summary"), "strength": round(m["strength"], 2),
                                "status": m["status"], "mornings": m["content"].get("based_on_mornings")} for m in mem.all("semantic")]},
        "agents": AGENTS,
        "metrics": metrics(handled),
        "models_used": ai.models_used or {"none (sample mode: recorded/offline reasoning)": 0},
        "world_model": {"facts_ingested": world.stats["ingested"], "duplicates_skipped": world.stats["duplicates_skipped"],
                        "provenance_rows": world.provenance_count()},
        "network": net["public"],
        "timeline": timeline,
        "trace_id": run_id, "log_file": str(trace.path.relative_to(SETTINGS.logs_dir.parent)),
        "execution_time_seconds": round(time.time() - started, 1),
    }
    trace.log("Orchestrator", "finish_run", output_summary=f"{len(handled)} incidents, {len(alerts)} alerts in {result['execution_time_seconds']} s")
    trace.close()
    return result


# ------------------------------------------------------------------ network
def load_network(z: Zetaris, world: WorldModel, date: str, trace) -> dict:
    q = lambda sql, purpose: z.query(sql, {"d": date}, agent="Orchestrator", purpose=purpose)[0]
    routes = q("SELECT * FROM gtfs_routes", "routes")
    stops = q("SELECT * FROM gtfs_stops", "stops")
    trips = q("SELECT * FROM gtfs_trips ORDER BY trip_id", "trips")
    st = q("SELECT * FROM gtfs_stop_times ORDER BY trip_id, stop_sequence", "timetable")
    shapes = q("SELECT shape_id, shape_pt_lat, shape_pt_lon FROM gtfs_shapes ORDER BY shape_id, shape_pt_sequence", "route shapes")
    buses = q("SELECT * FROM buses", "fleet")
    drivers = q("SELECT * FROM driver_shifts WHERE service_date = :d", "driver shifts today")
    depot = json.loads((SETTINGS.sample_dir / "depot.json").read_text())

    net: dict = {"stops": {}, "trips": {}, "stop_times": {}, "trips_by_bus": {}, "trips_by_route": {}, "buses": {},
                 "route_stops": {}, "trip_dir": {}, "depot": depot}
    for s in stops:
        net["stops"][s["stop_id"]] = {"name": s["stop_name"], "lat": s["stop_lat"], "lon": s["stop_lon"]}
        world.upsert_entity(f"stop:{s['stop_id']}", "stop", net["stops"][s["stop_id"]], source="zetaris:gtfs_stops")
    for r in st:
        for k in ("arrival_time", "departure_time"):
            r[k] = str(r[k])[:8] if len(str(r[k])) >= 8 else str(r[k]) + ":00"
        net["stop_times"].setdefault(r["trip_id"], []).append(r)
    for t in trips:
        net["trips"][t["trip_id"]] = t
        net["trips_by_bus"].setdefault(t["block_id"], []).append(t)
        net["trips_by_route"].setdefault(t["route_id"], []).append(t)
        net["trip_dir"][t["trip_id"]] = f"{t['route_id']}_{t['direction_id']}"
    for r in routes:
        first_trip = next(t for t in trips if t["route_id"] == r["route_id"] and int(t["direction_id"]) == 0)
        stop_ids = [x["stop_id"] for x in net["stop_times"][first_trip["trip_id"]]]
        net["route_stops"][r["route_id"]] = stop_ids
        world.upsert_entity(f"route:{r['route_id']}", "route", {"name": r["route_long_name"], "stops": stop_ids}, source="zetaris:gtfs_routes")
        for s in stop_ids:
            world.relate(f"stop:{s}", "served_by", f"route:{r['route_id']}", source="zetaris:gtfs_stop_times")
    for b in buses:
        rid = b["route_id"] if b["route_id"] not in (None, "", "nan") and str(b["route_id"]) != "nan" else None
        info = {"route_id": rid, "capacity": int(b["capacity"]), "wheelchair_accessible": bool(b["wheelchair_accessible"]) and str(b["wheelchair_accessible"]).lower() != "false"}
        net["buses"][b["bus_id"]] = info
        world.upsert_entity(b["bus_id"], "bus", info, source="zetaris:buses")
        if rid:
            world.relate(b["bus_id"], "runs", f"route:{rid}", source="zetaris:gtfs_trips")
    for d in drivers:
        info = {"bus_id": d["bus_id"] if str(d["bus_id"]) not in ("None", "nan", "") else None, "shift_start": str(d["shift_start"])[:5],
                "shift_end": str(d["shift_end"])[:5], "status": d["status"]}
        world.upsert_entity(d["driver_id"], "driver", info, source="zetaris:driver_shifts")
        if info["bus_id"]:
            world.relate(d["driver_id"], "drives", info["bus_id"], source="zetaris:driver_shifts")
    world.commit()
    shape_pts: dict = {}
    for s in shapes:
        shape_pts.setdefault(s["shape_id"], []).append([s["shape_pt_lat"], s["shape_pt_lon"]])
    net["public"] = {"stops": [{"id": k, **v} for k, v in net["stops"].items()],
                     "routes": [{"id": r["route_id"], "name": r["route_long_name"], "stops": net["route_stops"][r["route_id"]],
                                 "shape": shape_pts.get(f"{r['route_id']}_0", [])} for r in routes],
                     "depot": depot}
    trace.log("Orchestrator", "network_loaded", output_summary=f"{len(routes)} routes, {len(stops)} stops, {len(trips)} trips, {len(buses)} buses",
              engine="meterless-world-model")
    return net


# ------------------------------------------------------------------ views
def _plan_view(p: Optional[dict]) -> Optional[dict]:
    if not p:
        return None
    keep = ("id", "action", "summary", "reason", "bus", "driver", "eta", "dispatch_at", "finish_time", "hold_min",
            "stops_affected", "passengers_affected", "wheelchair_waiting", "bus_accessible", "driver_shift_end", "method")
    return {k: p.get(k) for k in keep if p.get(k) is not None}


def _incident_view(h: dict) -> dict:
    inc = h["incident"]
    return {"id": inc["id"], "type": inc["type"], "detected_at": inc["detected_at"], "bus": inc.get("bus"),
            "route": inc.get("route_id"), "stop": inc.get("stop_id"), "detail": inc.get("detail"),
            "position": inc.get("position"), "resolved_at": inc.get("resolved_at"), "resolution": inc.get("resolution"),
            "cause": h["finding"].get("cause"), "confidence": h["finding"].get("confidence"),
            "explanation": h["finding"].get("explanation"), "evidence": h["finding"].get("evidence_ids", []),
            "reasoning_method": h["finding"].get("method"), "memory_used": h["finding"].get("memory_used", []),
            "plans_considered": [{"plan": a["plan"]["summary"], "id": a["plan"]["id"],
                                  "critic": "approved" if a["verdict"]["approved"] else "rejected - " + "; ".join(p["problem"] for p in a["verdict"]["problems"])}
                                 for a in h["attempts"]],
            "decision": h["decision"], "decision_card": h["decision_card"]}


def _bus_state(bus: str, monitor: Monitor, net: dict) -> dict:
    last = (monitor.history.get(bus) or [None])[-1]
    status = "ok"
    for key, inc in monitor.open.items():
        if inc.get("bus") == bus or bus in (inc.get("buses") or []):
            status = inc["type"]
    if not last:
        return {"id": bus, "route": net["buses"][bus]["route_id"], "status": "not_started"}
    return {"id": bus, "route": net["buses"][bus]["route_id"], "lat": last["lat"], "lon": last["lon"],
            "speed": last["speed_kmh"], "status": status}


def metrics(handled: list[dict]) -> dict:
    rejections = sum(1 for h in handled for a in h["attempts"] if not a["verdict"]["approved"])
    approved = [h for h in handled if h["decision"].get("action") not in (None, "escalated")]
    proactive = [h for h in handled if h["incident"]["type"] == "proactive"]
    return {"incidents": len(handled), "decisions_approved": len(approved),
            "escalated": len(handled) - len(approved), "critic_rejections": rejections,
            "proactive_actions": len(proactive),
            "passengers_informed": sum((h["decision"].get("passengers_affected") or 0) for h in approved)}
