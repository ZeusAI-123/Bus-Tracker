"""Safety Critic: hard rules in code, then a model review. Can block any plan.

A rejection returns feedback; the orchestrator removes options that share the
rejected property and asks the Planner again (max 3 attempts), then escalates.
"""
from __future__ import annotations

import json

from app import rules

SYSTEM = """You are the Safety Critic in a Sydney bus control room. You review ONE proposed plan.
Code has already checked hard rules (capacity, accessibility, driver hours, holding limits, service gaps,
false-alarm dispatch). Your job: spot anything else unsafe or unfair, or anything that contradicts the
memory lessons provided. Only block for a concrete reason.
Answer: {"block": true|false, "concerns": ["..."], "reason": "one sentence"}"""


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
        elif r == "confidence":
            out = [o for o in out if o["action"] not in rules.DISPATCH_ACTIONS]
        elif r in ("capacity", "resource_free"):
            out = [o for o in out if o.get("bus") != plan.get("bus")]
        elif r == "service_gap":
            out = [o for o in out if o["action"] not in ("alert_only", "monitor")]
    return out
