"""Meterless H-MEM engine: conformance tests (store, consolidate, recall ranking, decay, conflict, approval)."""
from app.engines.hmem import HMem

KEY_A = "route:A|cause:weather_congestion|cond:rain"
KEY_B = "route:B|cause:weather_congestion|cond:rain"


def lesson(effect, fix="extra_bus_early"):
    return {"route": "A", "effect_min": effect, "fix": fix, "summary": f"Route A ~{effect} min late in rain"}


def test_episode_becomes_lesson_and_is_recalled():
    m = HMem(":memory:")
    m.store_episode(key=KEY_A, content=lesson(14), on_date="2025-04-28", evidence=["ev-1"])
    changes = m.consolidate("2025-04-28")
    assert changes[0]["change"] == "created" and changes[0]["status"] == "active"
    got = m.recall(filters={"route": "A", "cause": "weather_congestion", "cond": "rain"}, on_date="2025-05-19")
    assert got and got[0]["key"] == KEY_A and got[0]["evidence"] == ["ev-1"]


def test_lessons_never_cross_routes():
    m = HMem(":memory:")
    m.store_episode(key=KEY_A, content=lesson(14), on_date="2025-04-28", evidence=["ev-1"])
    m.consolidate("2025-04-28")
    assert m.recall(filters={"route": "B", "cause": "weather_congestion", "cond": "rain"}, on_date="2025-05-19") == []


def test_strength_decays_over_time():
    m = HMem(":memory:")
    m.store_episode(key=KEY_A, content=lesson(14), on_date="2025-04-28", evidence=["ev-1"])
    m.consolidate("2025-04-28")
    lesson_row = m.all("semantic")[0]
    soon = HMem.current_strength(lesson_row, "2025-04-29")
    later = HMem.current_strength(lesson_row, "2025-07-28")
    assert later < soon / 4


def test_repeat_reinforces_and_contradiction_supersedes():
    m = HMem(":memory:")
    m.store_episode(key=KEY_A, content=lesson(14), on_date="2025-04-28", evidence=["ev-1"])
    m.consolidate("2025-04-28")
    m.store_episode(key=KEY_A, content=lesson(12), on_date="2025-05-19", evidence=["ev-2"])
    assert m.consolidate("2025-05-19")[0]["change"] == "reinforced"
    m.store_episode(key=KEY_A, content=lesson(13, fix="reroute_via_city_west_link"), on_date="2025-06-02", evidence=["ev-3"])
    change = m.consolidate("2025-06-02")[0]
    assert change["change"] == "superseded"
    statuses = sorted(r["status"] for r in m.all("semantic"))
    assert statuses == ["active", "superseded"]          # old lesson kept for history


def test_approval_boundary_blocks_unproven_lessons():
    m = HMem(":memory:")
    m.store_episode(key=KEY_B, content={"effect_min": 0, "summary": "no measured effect"}, on_date="2025-04-28", evidence=[])
    assert m.consolidate("2025-04-28")[0]["status"] == "pending_approval"
    assert m.recall(filters={"route": "B"}, on_date="2025-04-29") == []
