"""Meterless World Model engine: conformance tests (idempotent ingest, provenance, resume, context)."""
from app.engines.world_model import WorldModel


def make(tmp_path, run="r1"):
    return WorldModel(tmp_path / "world.db", run)


def test_ingest_is_idempotent(tmp_path):
    w = make(tmp_path)
    fact = dict(key="pos:C1:07:20:00", entity_id="C1", kind="position", value={"lat": 1, "lon": 2},
                sim_time="07:20", source="zetaris:vehicle_positions", agent="Monitor")
    assert w.ingest(**fact) is True
    assert w.ingest(**fact) is False          # same fact twice -> no duplicate
    assert w.stats == {"ingested": 1, "duplicates_skipped": 1}
    assert len(w.facts("C1", "position")) == 1


def test_every_fact_has_provenance(tmp_path):
    w = make(tmp_path)
    w.upsert_entity("C1", "bus", {"route_id": "C"}, source="zetaris:buses")
    w.ingest(key="k1", entity_id="C1", kind="position", value={"lat": 1}, sim_time="07:00",
             source="zetaris:vehicle_positions", agent="Monitor")
    assert w.provenance_count() == 2
    assert w.latest("C1", "position")["_source"] == "zetaris:vehicle_positions"


def test_resume_after_crash(tmp_path):
    w = make(tmp_path)
    w.ingest(key="k1", entity_id="C1", kind="position", value={"lat": 1}, sim_time="07:00", source="s", agent="a")
    w.checkpoint("07:00")
    del w                                     # simulate a crash
    w2 = make(tmp_path)                       # reopen the same run
    assert w2.last_checkpoint() == "07:00"
    assert w2.ingest(key="k1", entity_id="C1", kind="position", value={"lat": 1}, sim_time="07:00", source="s", agent="a") is False


def test_context_builder(tmp_path):
    w = make(tmp_path)
    w.upsert_entity("route:C", "route", {"name": "Marrickville - Central", "stops": ["MKV", "NTN"]}, source="t")
    w.upsert_entity("stop:MKV", "stop", {"name": "Marrickville"}, source="t")
    w.upsert_entity("stop:NTN", "stop", {"name": "Newtown"}, source="t")
    w.upsert_entity("C1", "bus", {"route_id": "C", "wheelchair_accessible": True}, source="t")
    w.upsert_entity("S1", "bus", {"route_id": None, "wheelchair_accessible": True}, source="t")
    w.upsert_entity("D11", "driver", {"status": "spare", "shift_end": "14:15"}, source="t")
    w.ingest(key="p", entity_id="C1", kind="position", value={"lat": 1, "lon": 2}, sim_time="07:20", source="s", agent="a")
    ctx = w.get_context(bus_id="C1", at="07:30")
    assert ctx["route"]["name"] == "Marrickville - Central"
    assert [s["name"] for s in ctx["route_stops"]] == ["Marrickville", "Newtown"]
    assert [b["id"] for b in ctx["spare_buses"]] == ["S1"]
    assert ctx["spare_drivers"][0]["id"] == "D11"
    assert ctx["bus_position"]["lat"] == 1
