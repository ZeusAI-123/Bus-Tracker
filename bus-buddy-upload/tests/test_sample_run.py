"""End to end in sample mode (no Zetaris, no Nightshift): the five planted mornings behave as expected."""
import json

from app.config import SETTINGS
from app.orchestrator import run_morning

DATES = json.loads((SETTINGS.sample_dir / "scenarios.json").read_text())["dates"]


def run(key, memory="on", mem_db=None):
    return run_morning(DATES[key], memory=memory, sample_mode=True, memory_db=mem_db)


def test_breakdown_needs_accessible_bus(tmp_path):
    res = run("tue", mem_db=tmp_path / "m.db")
    inc = next(i for i in res["incidents"] if i["type"] == "stopped")
    assert inc["cause"] == "breakdown"
    assert inc["decision"]["bus"] == "S1"                 # the wheelchair-accessible spare
    assert inc["decision"]["bus_accessible"] is True
    # S2 (not accessible) must never be approved, whoever proposed it
    assert all(p["critic"] != "approved" for p in inc["plans_considered"] if ":S2:" in p["id"])


def test_gps_glitch_does_not_send_a_bus(tmp_path):
    res = run("wed", mem_db=tmp_path / "m.db")
    inc = next(i for i in res["incidents"] if i["type"] == "missing")
    assert inc["decision"]["action"] == "monitor"
    assert inc["resolution"]


def test_memory_acts_before_the_delay(tmp_path):
    db = tmp_path / "m.db"
    run("mon1", mem_db=db)                         # learn from the first rainy Monday
    with_mem = run("mon2", mem_db=db)
    without = run("mon2", memory="off")
    assert any(i["type"] == "proactive" and i["route"] == "A" for i in with_mem["incidents"])
    assert not any(i["type"] == "proactive" for i in without["incidents"])


def test_trace_written(tmp_path):
    res = run("thu", mem_db=tmp_path / "m.db")
    lines = (SETTINGS.logs_dir.parent / res["log_file"]).read_text().splitlines()
    agents = {json.loads(l)["agent_name"] for l in lines}
    assert {"Monitor", "Investigator", "Planner", "SafetyCritic", "Notifier"} <= agents
