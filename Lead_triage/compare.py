#!/usr/bin/env python3
"""Compare the agent's decisions with YOUR OWN manual pass (Tier 4 evidence).
Fill manual_pass.csv yourself BEFORE looking at out/decisions.csv, then run:
    python3 compare.py
"""
import csv

agent = {r["lead_id"]: r["decision"] for r in csv.DictReader(open("out/decisions.csv", encoding="utf-8"))}
manual = {r["lead_id"]: r["my_decision"].strip().upper() for r in csv.DictReader(open("manual_pass.csv", encoding="utf-8"))}

done = {k: v for k, v in manual.items() if v}
if not done:
    raise SystemExit("manual_pass.csv is empty. Fill in my_decision for every lead first.")
agree = [k for k, v in done.items() if agent.get(k) == v]
print(f"compared {len(done)} rows, agreement {len(agree)}/{len(done)} (observed)")
for k, v in done.items():
    if agent.get(k) != v:
        print(f"DISAGREE {k}: agent={agent.get(k)} me={v}  -> write down who was right and why")
