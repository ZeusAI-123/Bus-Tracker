"""Hard safety and fairness rules the Safety Critic checks in code (no AI).

Each rule returns a problem string, or None if the plan passes it. Values come
from config.LIMITS so the team can adjust them to real operator rules.
"""
from __future__ import annotations

from typing import Optional

from app.config import LIMITS

DISPATCH_ACTIONS = {"replacement_bus", "extra_bus"}


def check_accessibility(plan: dict) -> Optional[str]:
    if plan["action"] in DISPATCH_ACTIONS and plan.get("wheelchair_waiting") and not plan.get("bus_accessible", True):
        return (f"{plan['bus']} is not wheelchair accessible, but a wheelchair user is waiting at "
                f"{', '.join(plan['wheelchair_waiting'])}")
    return None


def check_capacity(plan: dict) -> Optional[str]:
    load, cap = plan.get("projected_load"), plan.get("capacity")
    if load is not None and cap is not None and load > cap:
        return f"{plan.get('bus', 'the bus')} would carry {load} passengers (capacity {cap})"
    return None


def check_service_gap(plan: dict) -> Optional[str]:
    gap = plan.get("service_gap_min")
    if gap is not None and gap > LIMITS["max_service_gap_min"]:
        return f"stops {', '.join(plan.get('stops_affected', []))} would wait {gap:.0f} min for a bus (limit {LIMITS['max_service_gap_min']})"
    return None


def check_driver_hours(plan: dict) -> Optional[str]:
    if plan["action"] in DISPATCH_ACTIONS and plan.get("driver_shift_end") and plan.get("finish_time"):
        if plan["finish_time"] > plan["driver_shift_end"]:
            return (f"driver {plan['driver']} finishes their shift at {plan['driver_shift_end']} "
                    f"but this work runs until {plan['finish_time']}")
    return None


def check_hold(plan: dict) -> Optional[str]:
    if plan["action"] == "hold" and plan.get("hold_min", 0) > LIMITS["max_hold_min"]:
        return (f"holding Route {plan.get('route')} for {plan['hold_min']} min delays everyone downstream "
                f"(limit {LIMITS['max_hold_min']} min)")
    return None


def check_confidence(plan: dict) -> Optional[str]:
    if plan["action"] in DISPATCH_ACTIONS and plan.get("cause_confidence", 1.0) < LIMITS["min_dispatch_confidence"] \
            and not plan.get("corroborated", False):
        return (f"confidence in the cause is only {plan.get('cause_confidence'):.2f} with nothing backing it up; "
                "sending a bus could be a false alarm")
    return None


def check_resource_free(plan: dict) -> Optional[str]:
    used = set(plan.get("used_today", []))
    for r in (plan.get("bus"), plan.get("driver")):
        if plan["action"] in DISPATCH_ACTIONS and r and r in used:
            return f"{r} is already committed to another incident this morning"
    return None


RULES = [check_accessibility, check_capacity, check_service_gap, check_driver_hours,
         check_hold, check_confidence, check_resource_free]


def check_all(plan: dict) -> list[dict]:
    problems = []
    for rule in RULES:
        msg = rule(plan)
        if msg:
            problems.append({"rule": rule.__name__.replace("check_", ""), "problem": msg})
    return problems
