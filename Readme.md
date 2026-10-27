# Bus Buddy – an AI control-room agent for Sydney buses

**Open Agent Hackathon 2026 · Team Zeus AI · Track 3: Reasoning Architecture**

Bus Buddy watches a bus network through the morning peak, spots problems early, explains **why** with
evidence, decides what to do, has a **Safety Critic** check the plan is safe and fair, alerts passengers,
and **remembers** patterns so it can act before the same delay happens again.

- **Users:** bus network controllers (operator control room) and passengers
- **Data layer:** Zetaris (all data is queried through it)
- **Cognition:** Meterless engines – **World Model** (shared live state) and **H-MEM** (long-term memory)
- **AI provider:** Meterless Nightshift, running **NVIDIA Nemotron** models
- **Built with:** Cursor

---

## Quick start (sample mode – no Zetaris or Nightshift needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # SAMPLE_MODE=true by default
python run.py --week               # runs all 7 demo mornings, writes output_examples/
python scripts/score.py            # checks results against the answer key
python scripts/build_control_room.py  # rebuilds the control-room screen from output_examples/
python run.py --serve              # API + control-room screen on http://localhost:8000
```

Or with Docker: `docker compose up --build` then open http://localhost:8000.

Run one morning through the API:

```bash
curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d @input_examples/example_1_breakdown.json
```

Tests (includes the Meterless engine conformance tests): `python -m pytest -q`

---

## What happens in a run

```
POST /run {"date": "...", "memory": "on"}
  Pre-check (06:30): today's weather + H-MEM lessons -> act BEFORE known delays
  every 2 min, 06:30-08:30:
     new bus positions via Zetaris -> World Model -> Monitor (code) raises incidents
     Investigator (Nemotron) picks Zetaris tools, explains WHY, cites evidence ids (checked in code)
     Planner: code computes options (ETAs, seats, accessibility, driver hours); Nemotron picks one
     Safety Critic: hard rules in code + Nemotron review -> reject -> drop similar options -> retry (max 3)
     Notifier (Nemotron, small): GTFS-Realtime style service alerts + controller decision card
  end of morning: Learner writes lessons to H-MEM
  -> JSON (incidents, explanations, plans considered, decisions, alerts, memory, metrics) + logs/trace-<run_id>.jsonl
```

| Agent | What it does | Where |
|---|---|---|
| Monitor | Late, stopped, missing, bunching, late-train connections, crowding (code only) | `app/agents/monitor.py` |
| Pre-check | Today's conditions + memory → proactive action | `app/agents/learner.py` |
| Investigator | Chooses Zetaris tools, explains the cause with evidence | `app/agents/investigator.py` |
| Planner | Options with facts computed in code; model picks | `app/agents/planner.py` |
| Safety Critic | Hard rules (`app/rules.py`) + model review; can block | `app/agents/critic.py` |
| Notifier | Passenger alerts + decision card | `app/agents/notifier.py` |
| Learner | Turns incidents into H-MEM lessons | `app/agents/learner.py` |

### Demo mornings (planted in the data, checked by `scripts/score.py`)

| Morning | What happens | What the agent should do |
|---|---|---|
| Mon (real rainy date) | Route A crawls on Parramatta Rd | Explain (rain), update passengers, **learn** the pattern |
| Tue | Bus C1 breaks down; wheelchair user waiting at Newtown | Critic rejects the non-accessible spare (S2) and a driver whose shift ends; send S1 |
| Wed | Train into Central 9 min late; GPS gap on G1 (**trap**) | Critic rejects a 7-min hold; tell passengers about the next bus. Do **not** send a bus for the GPS gap |
| Thu | UNSW exam crowds at Kensington/UNSW | Extra shuttle; Critic rejects driver D10 (shift ends 07:45); use D11 |
| Fri | One lane of Glebe Point Rd closed (water main repair); Route B crawls past it | Find the closure as the cause (not rain or traffic); update Route B passengers |
| Wed (second week) | **Two problems at once**: C1 breaks down (wheelchair user at Newtown), then UNSW graduation crowds | Accessible S1 replaces C1; the shuttle must not reuse S1 or its driver; Critic rejects D10 (shift ends 07:45) |
| Mon again (real rainy date) | Same rain pattern | **Memory acts at 06:30, before the delay**. Memory-off run only reacts once buses are late |

`python scripts/score.py` checks 9 expected behaviours across the 7 mornings.

**Control room screen:** `ui/control-room.html` (also served at `/` by `python run.py --serve`) replays every morning:
buses moving on the route map, the incident feed, a decision card with each plan the Safety Critic approved or
rejected, the passenger alerts, and the memory on/off comparison. Click a route for its buses and stops. It opens
straight from disk; rebuild it with `python scripts/build_control_room.py` after `python run.py --week`.
The first screen, which runs one morning live through `POST /run`, is at `/classic`.

---

## Sponsor technology

### Zetaris – data layer (`app/adapters/zetaris.py`)
Every query goes through `Zetaris.query()`. Agents use logical table names; `physical()` maps them to our
Zetaris sources. In sample mode the same SQL runs on `data/sample/` with DuckDB.

Sources to register in Zetaris:
| Logical tables | Source | How |
|---|---|---|
| `gtfs_*`, `buses`, `driver_shifts`, `maintenance_reports`, `road_closures`, `events`, `train_delays`, `passenger_counts`, `weather_hourly` | Postgres | `docker compose --profile db up` loads them; register as `busbuddy_pg` |
| `vehicle_positions` | Parquet in S3 | `python scripts/upload_s3.py --bucket <bucket>`; register as `busbuddy_s3.vehicle_positions` |
| live weather (optional) | Open-Meteo REST | register as a REST source |

**TODO (team):** confirm the Zetaris REST query endpoint/payload from the Zetaris getting-started repo
(`docs/connections/client-setup.md`) and adjust `_post_sql()`; set `ZETARIS_*` names in `.env`.

### Meterless – engines + Nightshift
- **World Model** (`app/engines/world_model.py`): entities, relationships and observations with provenance;
  idempotent, resumable ingest; `get_context()` for agents. Tests: `tests/test_world_model.py`.
- **H-MEM** (`app/engines/hmem.py`): working → episodic → semantic tiers, decay, reinforcement, conflict
  supersession, approval boundary, ranked recall. Tests: `tests/test_hmem.py`. Powers the rainy-Monday
  proactive action.
- **Nightshift** (`app/adapters/nightshift.py`): the only model endpoint the app calls (OpenAI format).
  Every call logs the model that answered. `AI_RECORDING=record` saves replies so sample mode replays real
  agent behaviour.

### NVIDIA
Set `MODEL_REASONING` / `MODEL_FAST` to the Nemotron model ids Nightshift lists. The NVIDIA API key goes in
Nightshift's Providers tab, never in this app. The trace logs `model` on every call.

### Cursor
Built in Cursor; project rules in `.cursor/rules/`.

---

## Going live with real data
| Data | Demo | Production source |
|---|---|---|
| Bus positions | replayed Parquet | TfNSW GTFS-Realtime Vehicle Positions (Open Data Hub) |
| Timetable | generated GTFS on real Sydney roads | TfNSW GTFS timetable (trim to our 8 routes) |
| Train delays | table | Sydney Trains realtime feed |
| Weather | real Open-Meteo history | forecast feed |
| Crowds, faults, rosters | simulated | operator systems, connected in place through Zetaris |

A small collector writes real-time feeds to S3/Postgres every 15–30 s; the agent code does not change.

---

## Data and credits
- `scripts/build_data.py` builds everything in `data/sample/` (fixed seed). Road paths: OSRM over
  © OpenStreetMap contributors (ODbL). Weather: Open-Meteo historical API (CC BY 4.0).
- Stop locations are approximate real Sydney places; route letters A–H are ours, not TfNSW route numbers.
  When using Transport for NSW open data, credit TfNSW per its licence.
- No personal data: passengers are counts; drivers are codes.

## Limitations (honest)
- Bus positions are replayed, so agent decisions don't change what buses do afterwards; impact numbers are projected.
- Without Nightshift (or recordings) the agents use labelled offline rules (`reasoning_method: offline-rules`).
- Operating limits in `app/config.py` are placeholders; check real operator rules.

## Repo map
```
app/            orchestrator, agents/, adapters/ (zetaris, nightshift), engines/ (world_model, hmem), rules, tracing, main (API)
scripts/        build_data.py, score.py, upload_s3.py, postgres_init.sql
data/sample/    small demo data (sample mode + Postgres load)
input_examples/ output_examples/ logs/ tests/ ui/
```
