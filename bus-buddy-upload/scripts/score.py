"""Scores runs against data/sample/answer_key.json.

  python scripts/score.py                 scores output_examples/*.json (made by `python run.py --week`)

Checks per expected incident: found? right cause? acceptable action? any "must not" broken?
Also compares the rainy Monday with memory ON vs OFF.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = json.loads((ROOT / "data" / "sample" / "answer_key.json").read_text())


def score_run(res: dict) -> list[dict]:
    exp = KEY.get(res["date"], {"expected": []})["expected"]
    rows = []
    for e in exp:
        if e.get("needs_memory") and res["mode"]["memory"] == "off":
            continue
        cand = [i for i in res["incidents"] if i["type"] == e["type"] and
                (e.get("bus") in (None, i.get("bus"))) and (e.get("route") in (None, i.get("route")))]
        i = cand[0] if cand else None
        row = {"date": res["date"], "scenario": KEY[res["date"]]["scenario"], "expected": f"{e['type']} {e.get('bus') or e.get('route')}",
               "found": bool(i), "cause_ok": bool(i and i["cause"] == e["cause"]), "action_ok": False, "must_not_ok": True}
        if i:
            d = i["decision"]
            row["action_ok"] = d.get("action") in e["actions_ok"]
            mu = e.get("must_use") or {}
            if mu.get("bus") and d.get("bus") != mu["bus"]:
                row["action_ok"] = False
            if mu.get("driver") and d.get("driver") != mu["driver"]:
                row["action_ok"] = False
            for mn in e.get("must_not", []):
                if mn.get("bus") and d.get("bus") == mn["bus"]: row["must_not_ok"] = False
                if mn.get("driver") and d.get("driver") == mn["driver"]: row["must_not_ok"] = False
                if mn.get("action") == "hold" and d.get("action") == "hold" and (d.get("hold_min") or 0) > mn.get("over_min", 0): row["must_not_ok"] = False
                if mn.get("action") == "replacement_bus" and d.get("action") == "replacement_bus": row["must_not_ok"] = False
            if e.get("before") and i["detected_at"] > e["before"]:
                row["action_ok"] = False
        row["pass"] = row["found"] and row["cause_ok"] and row["action_ok"] and row["must_not_ok"]
        rows.append(row)
    return rows


def main() -> None:
    files = sorted((ROOT / "output_examples").glob("*.json"))
    if not files:
        sys.exit("No runs found. Run: python run.py --week")
    all_rows, mem = [], {}
    for f in files:
        res = json.loads(f.read_text())
        all_rows += score_run(res)
        if KEY.get(res["date"], {}).get("scenario") == "mon2":
            pro = [i for i in res["incidents"] if i["type"] == "proactive"]
            mem[res["mode"]["memory"]] = pro[0]["detected_at"] if pro else None
    print(f"{'scenario':<8} {'expected':<26} found cause action safe  PASS")
    for r in all_rows:
        tick = lambda b: " yes " if b else " NO  "
        print(f"{r['scenario']:<8} {r['expected']:<26}{tick(r['found'])}{tick(r['cause_ok'])}{tick(r['action_ok'])}{tick(r['must_not_ok'])} {'PASS' if r['pass'] else 'FAIL'}")
    passed = sum(r["pass"] for r in all_rows)
    print(f"\n{passed}/{len(all_rows)} expected behaviours correct")
    print(f"Rainy Monday, first action on Route A: memory ON -> {mem.get('on') or 'none before delay'}, "
          f"memory OFF -> {mem.get('off') or 'none before the delay (reacts only once buses are late)'}")


if __name__ == "__main__":
    main()
