"""Safety Critic hard rules."""
from app import rules
from app.agents.critic import exclude_after_rejection


def plan(**kw):
    base = {"id": "p", "action": "replacement_bus", "bus": "S2", "driver": "D11", "bus_accessible": True,
            "capacity": 60, "projected_load": 40, "driver_shift_end": "14:15", "finish_time": "08:10",
            "cause_confidence": 0.9, "corroborated": True, "wheelchair_waiting": []}
    base.update(kw)
    return base


def test_clean_plan_passes():
    assert rules.check_all(plan()) == []


def test_accessibility():
    p = plan(bus_accessible=False, wheelchair_waiting=["NTN"])
    assert [x["rule"] for x in rules.check_all(p)] == ["accessibility"]


def test_driver_hours_and_capacity():
    p = plan(driver="D10", driver_shift_end="07:45", projected_load=80)
    assert {x["rule"] for x in rules.check_all(p)} == {"capacity", "driver_hours"}


def test_hold_limit():
    p = plan(action="hold", hold_min=7, route="E")
    assert [x["rule"] for x in rules.check_all(p)] == ["hold"]


def test_false_alarm_guard():
    p = plan(cause_confidence=0.5, corroborated=False)
    assert [x["rule"] for x in rules.check_all(p)] == ["confidence"]


def test_exclusion_after_rejection():
    opts = [plan(id="a", bus="S2", bus_accessible=False), plan(id="b", bus="S1"), {"id": "c", "action": "alert_only"}]
    left = exclude_after_rejection(opts, opts[0], [{"rule": "accessibility", "problem": "x"}])
    assert [o["id"] for o in left] == ["b", "c"]
