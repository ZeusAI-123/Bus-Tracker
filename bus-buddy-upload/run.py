"""Entry point.

  python run.py --serve                 start the API + screen on http://localhost:8000
  python run.py --date 2025-04-29       run one morning, print a summary, save JSON to runs/
  python run.py --week                  run all 5 scenario mornings in order (memory builds up),
                                        plus the rainy Monday again with memory OFF for comparison,
                                        and write output_examples/
"""
from __future__ import annotations

import argparse
import json
import sys

from app.config import SETTINGS


def _summary(res: dict) -> None:
    print(f"\n=== {res['date']}  ({res['mode']})  {res['execution_time_seconds']} s")
    for i in res["incidents"]:
        print(f"- [{i['detected_at']}] {i['type']:<15} {i.get('bus') or i.get('route') or ''}: {i['detail']}")
        print(f"    why: {i['cause']} ({i['confidence']}) - {i['explanation']}")
        for p in i["plans_considered"]:
            print(f"    plan: {p['plan']}  => {p['critic']}")
        if i.get("resolution"):
            print(f"    resolved {i['resolved_at']}: {i['resolution']}")
    print(f"  alerts: {len(res['service_alerts'])}  lessons: {[l['summary'] for l in res['memory']['lessons']]}")
    print(f"  metrics: {res['metrics']}\n  trace: {res['log_file']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--memory", default="on", choices=["on", "off"])
    ap.add_argument("--week", action="store_true")
    a = ap.parse_args()

    if a.serve:
        import uvicorn
        uvicorn.run("app.main:app", host="0.0.0.0", port=SETTINGS.port)
        return

    from app.orchestrator import run_morning
    if a.week:
        from app.engines.hmem import HMem
        HMem(SETTINGS.memory_db).reset()
        dates = json.loads((SETTINGS.sample_dir / "scenarios.json").read_text())["dates"]
        order = ["mon1", "tue", "wed", "thu", "mon2"]
        SETTINGS.runs_dir.mkdir(exist_ok=True)
        out_dir = SETTINGS.data_dir.parent / "output_examples"
        out_dir.mkdir(exist_ok=True)
        for key in order:
            res = run_morning(dates[key], memory="on", run_id=f"{dates[key]}-{key}")
            _summary(res)
            (out_dir / f"{key}_{dates[key]}.json").write_text(json.dumps(res, indent=2, default=str))
        res = run_morning(dates["mon2"], memory="off", run_id=f"{dates['mon2']}-mon2-nomemory")
        _summary(res)
        (out_dir / f"mon2_{dates['mon2']}_memory_off.json").write_text(json.dumps(res, indent=2, default=str))
        return

    if not a.date:
        ap.error("give --date, --week or --serve")
    res = run_morning(a.date, memory=a.memory)
    _summary(res)
    SETTINGS.runs_dir.mkdir(exist_ok=True)
    (SETTINGS.runs_dir / f"{res['run_id']}.json").write_text(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
