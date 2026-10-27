# Bus Buddy – an AI control-room agent for Sydney buses

**Open Agent Hackathon 2026 · Team Zeus AI · Track 3: Reasoning Architecture**

Bus Buddy watches a bus network through the morning peak, spots problems early, explains **why** with
evidence, decides what to do, has a **Safety Critic** check the plan is safe and fair, alerts passengers,
and **remembers** patterns so it can act before the same delay happens again.

- **Users:** bus network controllers (operator control room) and passengers waiting at stops
- **Data layer:** Zetaris. Every query goes through it (a managed Zetaris cluster on AWS, or DuckDB over the same tables in sample mode)
- **Cognition:** Meterless engines: **World Model** (shared live state) and **H-MEM** (long-term memory)
- **AI provider:** Meterless Nightshift, running **NVIDIA Nemotron** (`nvidia/nemotron-3.5-lightning-30b-a3b`)
- **Built with:** Cursor

**Results:** 9 of 9 expected behaviours across 7 mornings, with Nemotron making every explanation and decision,
both live and from the recorded replies. A proof run through the Zetaris cluster made 195 Zetaris queries,
got 5 Nemotron answers and needed 0 offline fallbacks.

---

## Quick start (sample mode: no Zetaris, Nightshift or keys needed)

Needs Python 3.11 or newer. Sample mode reads `data/sample/` with DuckDB and replays the recorded Nemotron
answers in `data/recordings/`, so judges see the real agent behaviour without our accounts.

**Windows (PowerShell)**
```powershell
cd bus-buddy-upload
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py --week                    # runs all 7 demo mornings, writes output_examples/
python scripts/score.py                 # checks results against the answer key: expect 9/9
python -m pytest -q                     # 23 tests, including the Meterless engine conformance tests
python run.py --serve                   # control room on http://localhost:8000
```

**macOS / Linux**
```bash
cd bus-buddy-upload
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py --week && python scripts/score.py && python -m pytest -q
python run.py --serve
```

No `.env` file is needed for sample mode (`SAMPLE_MODE` defaults to `true`). The full list of settings is in
[Configuration](#configuration).

Other ways in:
- Open `ui/control-room.html` straight from disk, with no server needed.
- Run one morning: `python run.py --date 2025-04-29` (prints a summary, saves JSON to `runs/`).
- Run one morning through the API:
  ```bash
  curl -X POST http://localhost:8000/run -H "Content-Type: application/json" -d @input_examples/example_1_breakdown.json
  ```
- Docker (optional, not tested by the team): `docker compose up --build`, then open http://localhost:8000.

---

## The control room screen

`http://localhost:8000/` (or `ui/control-room.html`) replays every morning from `output_examples/`:

- **Map:** the 8 routes on real Sydney roads, with buses moving and coloured by status (on time, late, stopped,
  no GPS, crowding). Spare buses travel from the depot when one is sent. Click a route to see its buses,
  stops and problems.
- **Time bar:** play, pause, 1×/2×/4× speed. Diamonds mark each incident; click one to jump to it.
- **Incident feed and decision card:** what happened, Nemotron's explanation with confidence and evidence ids,
  every plan considered (rejected in red with the Safety Critic's reason, approved in green), and the
  Planner's reason.
- **What passengers see:** the service alerts Nemotron wrote, shown as phone notifications.
- **Memory:** H-MEM lessons, and the rainy-Monday comparison of memory on (first action 06:30) and memory off (06:58).

Rebuild it after a new run with `python scripts/build_control_room.py`. The first, simpler screen, which runs one
morning live through `POST /run`, is at `/classic`.

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
     Notifier (Nemotron): GTFS-Realtime style service alerts + controller decision card
  end of morning: Learner writes lessons to H-MEM
  -> JSON (incidents, explanations, plans considered, decisions, alerts, memory, metrics) + logs/trace-<run_id>.jsonl
```

| Agent | What it does | Where |
|---|---|---|
| Monitor | Late, stopped, missing, bunching, late-train connections, crowding (code only) | `app/agents/monitor.py` |
| Pre-check | Today's conditions + memory → proactive action | `app/agents/learner.py` |
| Investigator | Chooses Zetaris tools, explains the cause with evidence | `app/agents/investigator.py` |
| Planner | Options with facts computed in code; Nemotron picks | `app/agents/planner.py` |
| Safety Critic | Hard rules (`app/rules.py`) + Nemotron review; can block and send back | `app/agents/critic.py` |
| Notifier | Passenger alerts + decision card | `app/agents/notifier.py` |
| Learner | Turns incidents into H-MEM lessons | `app/agents/learner.py` |

### Guardrails that make the reasoning reliable
- **Evidence check:** every evidence id the Investigator cites must come from a Zetaris query it really ran;
  otherwise the answer is sent back.
- **Required checks:** each incident type has checks the Investigator must make (for a stopped bus:
  maintenance reports, road closures, GPS track). If Nemotron answers without them, code runs them, hands back
  the rows and asks it to reconsider, so a skipped check cannot hide the real cause.
- **Hard safety and fairness rules (code, not AI):** wheelchair-accessible bus wherever a wheelchair user waits;
  no bus over capacity; no stop without service longer than 30 min; driver within shift hours; hold a bus at most
  5 min; no spare sent on a low-confidence guess or for a GPS dropout; every dispatched bus needs a driver;
  no bus or driver double-booked across incidents. Limits are in `app/config.py`.
- **Critic loop:** a rejected plan goes back to the Planner with the reason, and options sharing the problem are
  dropped. After 3 failed attempts the incident is escalated to the human controller.

### Demo mornings (planted in the data, checked by `scripts/score.py`)

| Morning | What happens | What the agent does |
|---|---|---|
| Mon 28 Apr (real rainy date) | Route A crawls on Parramatta Rd | Explains it as rain, updates passengers, **learns** the pattern |
| Tue 29 Apr | Bus C1 breaks down; wheelchair user waiting at Newtown | Finds the engine-warning report; sends the accessible spare S1; never S2 (not accessible) |
| Wed 7 May | Train into Central 9 min late; GPS gap on G1 (**trap**) | Critic rejects a 7-min hold; extra Route E bus for the 42 passengers. For G1, Critic blocks the spare Nemotron proposed (a GPS dropout, the bus is still running); the agent keeps watching and G1 reappears |
| Thu 8 May | UNSW exam crowds at Kensington/UNSW, then bus E1 runs late | Extra shuttle (S1, driver D11). For E1 the cause is unclear, so Critic blocks a spare on a 35% guess; passengers get updated times |
| Fri 9 May | One lane of Glebe Point Rd closed (water main repair); Route B crawls past it | Finds the closure as the cause (not rain or traffic); Critic rejects driver D10 (shift ends 07:45); extra bus with D11 |
| Wed 14 May | **Two problems at once**: C1 breaks down, then UNSW graduation crowds | S1 replaces C1. For the shuttle, Critic rejects "warn only" (210 people, regular buses hold 120) and S1 (already replacing C1); sends S2 with driver D12 |
| Mon 19 May (real rainy date) | Same rain pattern | **Memory acts at 06:30, before the delay**. The memory-off run only reacts at 06:58, once buses are late |

---

## Sponsor technology

### Zetaris: data layer (`app/adapters/zetaris.py`)
Every query any agent makes goes through `Zetaris.query()`. Agents write SQL against logical table names
(`maintenance_reports`, `vehicle_positions`, …); the adapter maps them to the Zetaris tables and sends the SQL
to the cluster. Each result gets an **evidence id** that the agents must cite, so every explanation traces
back to governed data. In sample mode the same SQL runs on `data/sample/` with DuckDB, so the project also
runs without an account.

We ran the project on a **Zetaris managed cluster on AWS** with our tables registered under `bustracker`.
See [Running with Zetaris](#running-with-zetaris).

### Meterless: engines and Nightshift
- **World Model** (`app/engines/world_model.py`): entities, relationships and observations with provenance;
  idempotent, resumable ingest; `get_context()` for agents. Tests: `tests/test_world_model.py`.
- **H-MEM** (`app/engines/hmem.py`): working → episodic → semantic tiers, decay, reinforcement, conflict
  supersession, approval boundary, ranked recall. Tests: `tests/test_hmem.py`. Powers the rainy-Monday
  proactive action.
- **Nightshift** (`app/adapters/nightshift.py`): the only model endpoint the app calls (OpenAI format,
  `http://127.0.0.1:8088/v1`). Every call logs the model that answered.

### NVIDIA Nemotron
All reasoning runs on `nvidia/nemotron-3.5-lightning-30b-a3b` through Meterless Nightshift: the Investigator,
Planner, Safety Critic review and Notifier. The NVIDIA API key lives only in Nightshift's provider settings,
never in this app. The output JSON (`models_used`) and every `model_call` line in the trace name the model.

### Cursor
The project was built in Cursor.

---

## Running with Zetaris

By default the app runs in sample mode. With `SAMPLE_MODE=false`, every query goes to the Zetaris managed
cluster instead. The agent code is the same in both modes; only `app/adapters/zetaris.py` changes where the
SQL is sent.

### 1. Zetaris cluster and data
1. Create a managed cluster from the Zetaris cloud portal (Compute → Credentials → AWS Quickstart, then
   Compute → Clusters → Create Cluster). The sign-in URL is on the cluster's **Overview → Access** page; the
   login is under **Organisations**.
2. Upload the files in `data/sample/` to S3: every `.csv`, and the `vehicle_positions/*.parquet` files.
3. Register them in Zetaris under one source name. We use `bustracker`, so tables are `bustracker.buses`,
   `bustracker.gtfs_stop_times`, `bustracker.vehicle_positions`, and so on (same names as the CSV files).
4. Check in the Zetaris SQL Editor: `SELECT * FROM bustracker.maintenance_reports` returns the C1
   "Engine warning light" row.

### 2. Settings (`.env`, never committed)
```
SAMPLE_MODE=false
ZETARIS_REST_URL=https://<your-cluster>.cloud.zetaris.com     # site address only, no /login
ZETARIS_ORG_ID=1
ZETARIS_USERNAME=<Zetaris login email>
ZETARIS_PASSWORD=<Zetaris password>
ZETARIS_PG_PREFIX=bustracker.                                 # keep the dot
ZETARIS_POSITIONS_TABLE=bustracker.vehicle_positions
ZETARIS_WEATHER_TABLE=bustracker.weather_hourly
ZETARIS_TIMEOUT_SECONDS=180
```
`requirements.txt` includes `truststore`, so Python uses the computer's trusted certificates; this fixes
`CERTIFICATE_VERIFY_FAILED` on many Windows machines.

### 3. Run one morning through Zetaris
```
python run.py --date 2025-04-29
```
With Nightshift open and `AI_LIVE=true`, the data comes through Zetaris and the reasoning through Nemotron.
A full morning makes about 195 Zetaris queries and takes 10–15 minutes on the Mini cluster.

Check the trace afterwards (PowerShell):
```
$f = "logs\" + (Get-ChildItem logs\trace-2025-04-29-mem-*.jsonl | Sort-Object LastWriteTime | Select-Object -Last 1).Name
"Zetaris queries : " + (Select-String -Path $f -Pattern '"backend": "zetaris"').Count
"Nemotron answers: " + (Select-String -Path $f -Pattern '"model_call"').Count
"AI failures     : " + (Select-String -Path $f -Pattern 'model_error|model_unavailable').Count
```
Our proof run is in `output_examples/traces/tue_2025-04-29_zetaris_live.jsonl`: 195 Zetaris queries,
5 Nemotron answers, 0 fallbacks, and the same decision as sample mode (C1 breakdown, accessible spare S1 sent).

Set `SAMPLE_MODE=true` again afterwards. The saved outputs and the replay use sample mode.

### How the adapter talks to Zetaris
- **Login:** `POST /api/auth/login` with `username` and `password`. The token from the answer is kept in
  memory (never logged) and renewed every 8 minutes, or after a 400/401/403.
- **Query:** `POST /api/proxy/sql-editor/sqls/run-stream` with headers `Authorization: Bearer <token>`,
  `X-Org-ID`, `X-Request-ID` and `Accept: text/plain`, and body `queryId`, `sql`, `source`, `chunkSize`, `limit`.
- **Answer:** plain text, one JSON list per line. The first line holds the column names, each further line
  one row. Values arrive as text and are turned back into numbers.
- **Table names:** agents write logical names (`maintenance_reports`); the adapter adds `ZETARIS_PG_PREFIX`.
- **Time-of-day columns** (`departure_time`, `reported_at`, `window_start`, …) were registered by Zetaris as
  timestamps with the registration date. The adapter compares only their time part in `WHERE` clauses
  (`date_format(col, 'HH:mm')`) and cuts returned values back to `HH:MM:SS`. It also writes the events
  table's `start`/`end` columns in backticks, as Zetaris expects.

### Testing the connection on its own
`notebooks/zetaris_connection_test.ipynb` logs in and runs `SELECT * FROM NY_TAXI.taxi_zones LIMIT 5`
step by step, outside the app. Clear the notebook's outputs before committing it.

### Troubleshooting
| Message | Fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` | `pip install truststore`, then restart Python (or the notebook kernel) |
| `404` on `/login/api/auth/login` | `ZETARIS_REST_URL` must be the site address without `/login` |
| `Invalid value for: header X-Org-ID` | Set `ZETARIS_ORG_ID` |
| `400 Bad Request` partway through a run | The login expired; the adapter logs in again by itself |
| `Read timed out` | Raise `ZETARIS_TIMEOUT_SECONDS` (first queries on a cold cluster are slow) |
| A morning with 0 incidents | Check that times come back as `07:13:46`, not `2026-10-26 07:13:46.0` |

Delete the Zetaris cluster when you are finished (Compute → Clusters), so it stops using AWS credits.

---

## Recording and replaying Nemotron

Nightshift runs on the developer's machine, so judges can't call it. Instead, a live run saves every Nemotron
reply in `data/recordings/`, and sample mode replays them exactly.

| Mode | Settings | What happens |
|---|---|---|
| Replay (default, what judges run) | `SAMPLE_MODE=true`, `AI_LIVE=false` | Recorded Nemotron replies, local data, a full week in under a minute |
| Record | `SAMPLE_MODE=true`, `AI_LIVE=true`, `AI_RECORDING=record`, Nightshift open | Asks Nemotron for any prompt with no saved reply and saves the answer |
| Fully live | `SAMPLE_MODE=false`, `AI_LIVE=true` | Zetaris data and live Nemotron |

To record from scratch, delete `data/recordings/`, then run `python run.py --week` with recording on. Re-running
only fills in replies that are missing. If no recording or model is available, the agents fall back to labelled
offline rules (`reasoning_method: offline-rules`), which also score 9/9.

To check who made each decision:
```
python -c "import json,glob,os; [print(os.path.basename(f), [(i['type'], i['reasoning_method'], i['decision'].get('method')) for i in json.load(open(f))['incidents']]) for f in sorted(glob.glob('output_examples/*.json'))]"
```

---

## Configuration

All settings are read from `.env` in `bus-buddy-upload/` (see `app/config.py`). Never commit `.env`.

| Setting | Default | Meaning |
|---|---|---|
| `SAMPLE_MODE` | `true` | `true`: local data with DuckDB. `false`: Zetaris |
| `AI_LIVE` | `false` | Call Nightshift even in sample mode (used for recording) |
| `AI_RECORDING` | `record` | `record` saves new replies and reuses saved ones |
| `METERLESS_BASE_URL` | `http://127.0.0.1:8088/v1` | Nightshift's local address |
| `METERLESS_RUNTIME_TOKEN` | – | API key from Nightshift's Console |
| `MODEL_REASONING` | `nvidia/nemotron-3-super` | We use `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `MODEL_FAST` | `nvidia/nemotron-3-nano` | We use `nvidia/nemotron-3.5-lightning-30b-a3b` |
| `ZETARIS_*` | – | See [Running with Zetaris](#running-with-zetaris) |
| `PORT` | `8000` | Web server port |

---

## Going live with real data
| Data | Demo | Production source |
|---|---|---|
| Bus positions | replayed Parquet | TfNSW GTFS-Realtime Vehicle Positions (Open Data Hub) |
| Timetable | generated GTFS on real Sydney roads | TfNSW GTFS timetable (trim to our 8 routes) |
| Train delays | table | Sydney Trains realtime feed |
| Weather | real Open-Meteo history | forecast feed |
| Crowds, faults, rosters | simulated | operator systems, connected in place through Zetaris |

A small collector would write the real-time feeds to S3 every 15–30 s and register them in Zetaris; the agent
code does not change.

---

## Data and credits
- `scripts/build_data.py` builds everything in `data/sample/` (fixed seed). Road paths: OSRM over
  © OpenStreetMap contributors (ODbL). Weather: Open-Meteo historical API (CC BY 4.0).
- Stop locations are approximate real Sydney places; route letters A–H are ours, not TfNSW route numbers.
  When using Transport for NSW open data, credit TfNSW per its licence.
- No personal data: passengers are counts; drivers and buses are codes.

## Limitations
- Bus positions are replayed, so agent decisions don't change what buses do afterwards; spare-bus positions
  on the screen after dispatch are projected.
- Recorded Nemotron replies match this data exactly. Changing the data or a prompt means recording again.
- A full morning through the Zetaris cluster takes 10–15 minutes, because the app queries every 2 simulated minutes.
- Operating limits in `app/config.py` are placeholders; check real operator rules.

## Pre-existing work
<!-- TEAM: confirm before submitting. The rules require declaring any code written before 22 Oct 2026 00:00 UTC. -->
None. All code, data and prompts in this repository were written during the build window (22–27 October 2026).

## Repo map
```
app/              orchestrator, agents/, adapters/ (zetaris, nightshift), engines/ (world_model, hmem), rules, tracing, main (API)
scripts/          build_data.py, score.py, build_control_room.py, upload_s3.py, postgres_init.sql
data/sample/      demo data for the 7 mornings (also uploaded to Zetaris)
data/recordings/  recorded Nemotron replies used by the replay
ui/               control-room.html (main screen), control-room.template.html, index.html (/classic)
notebooks/        zetaris_connection_test.ipynb
input_examples/   sample POST /run requests
output_examples/  results of the 7 mornings + memory-off run; traces/ holds the Zetaris proof run
tests/            rules, World Model, H-MEM, end-to-end sample runs
```

