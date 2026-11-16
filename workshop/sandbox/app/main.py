"""Entry point inside the sandbox: run the radar and save the findings to /sandbox/runs."""
import json, os, sys

# `openshell sandbox exec` does not pass the image ENV; /sandbox/runs is the only writable project path.
os.environ.setdefault("RADAR_WORK", "/sandbox/runs")

from agent import run_radar

topics = sys.argv[1:] or ["memory", "evaluation", "coding agents"]
findings = run_radar(topics=topics, days=7)
with open("/sandbox/runs/findings.json", "w") as f:
    f.write(json.dumps(findings, ensure_ascii=False, indent=1))
print("findings:", len(findings))
