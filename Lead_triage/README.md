# Lead Triage Agent

Rule-based first-pass triage for inbound leads. Standard library only, no API key.

## Run
    python3 triage.py fixtures/inbound_leads.csv
    shasum -a 256 fixtures/inbound_leads.csv

Outputs go to `out/` (`decisions.csv`, `run_log.jsonl`). Save the terminal output as the run log:

    python3 triage.py fixtures/inbound_leads.csv | tee out/run_stdout.txt

## Compare with your own judgement
Fill `manual_pass.csv` first, then `python3 compare.py`.

## Stress test
    cd stress && python3 ../triage.py stress_leads.csv

Lead text is treated as data. No URL in it is fetched, and embedded instructions are ignored and flagged.
