"""Safety Critic: hard rules in code, then a model review. Can block any plan.

A rejection returns feedback; the orchestrator removes options that share the
rejected property and asks the Planner again (max 3 attempts), then escalates.
"""
from __future__ import annotations

import json

from app import rules

SYSTEM = """You are the Safety Critic in a Sydney bus control room. You review ONE proposed plan.
Code has ALREADY checked these hard rules and the plan PASSED them: bus capacity (projected_load is the
peak number of people on board at one time, and it is within capacity), wheelchair accessibility, driver
shift hours, holding limits, maximum service gap, false-alarm dispatch (including GPS dropouts), a driver
assigned to every dispatched bus, and buses or drivers already committed today. Do NOT re-check these and
never block because of them.
Note: passengers_affected is everyone helped over the whole trip, not the number on board at once.
Block ONLY for a concrete safety or fairness problem the code could not check, or a clear contradiction
with the memory lessons provided. If in doubt, approve.
Answer: {"block": true|false, "concerns": ["..."], "reason": "<one sentence>"}"""

class SafetyCritic:
    def __init__(self, ai, trace):
        self.ai, self.trace = ai, trace

    def review(self, plan: dict, lessons: list, sim_time: str) -> dict:
        problems = rules.check_all(plan)
        review = None
        if not problems:
            review = self.ai.json_call(agent="SafetyCritic", system=SYSTEM,
                                       user=json.dumps({"plan": {k: v for k, v in plan.items() if k != "used_today"},
                                                        "memory": lessons}, default=str), sim_time=sim_time)
            if review and review.get("block"):
                problems.append({"rule": "model_review", "problem": review.get("reason") or "; ".join(review.get("concerns", []))})
        verdict = {"approved": not problems, "problems": problems,
                   "model_review": review if review is not None else ("not needed: hard rules failed" if problems else "unavailable (offline)")}
        self.trace.log("SafetyCritic", "approved" if verdict["approved"] else "rejected", sim_time=sim_time,
                       status="success" if verdict["approved"] else "rejected",
                       output_summary=f"{plan['id']}: " + ("OK" if verdict["approved"] else "; ".join(p["problem"] for p in problems)),
                       incident_id=plan.get("incident_id"), plan_id=plan["id"])
        return verdict


def exclude_after_rejection(options: list[dict], plan: dict, problems: list[dict]) -> list[dict]:
    """Remove options that share whatever the critic rejected."""
    out = [o for o in options if o["id"] != plan["id"]]
    for p in problems:
        r = p["rule"]
        if r == "accessibility":
            out = [o for o in out if o.get("bus_accessible", True) or o["action"] not in rules.DISPATCH_ACTIONS]
        elif r == "driver_hours":
            out = [o for o in out if o.get("driver") != plan.get("driver")]
        elif r == "hold":
            out = [o for o in out if o["action"] != "hold"]
        elif r in ("confidence", "gps_dropout"):
            out = [o for o in out if o["action"] not in rules.DISPATCH_ACTIONS]
        elif r == "crew":
            out = [o for o in out if o["action"] not in rules.DISPATCH_ACTIONS or o.get("driver")]
        elif r == "capacity":
            out = [o for o in out if o.get("bus") != plan.get("bus")]
        elif r == "resource_free":   # drop every option that uses a bus or driver already committed today
            used = set(plan.get("used_today", []))
            out = [o for o in out if o.get("bus") not in used and o.get("driver") not in used]
        elif r == "service_gap":
            out = [o for o in out if o["action"] not in ("alert_only", "monitor")]
    return out
