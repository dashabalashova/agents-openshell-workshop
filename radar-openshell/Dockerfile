# Sandbox image: python + the two client libraries + the radar agent (agent.py, copied in by case.py up,
# the same file the notebook radar.ipynb imports) + main.py (entry point) + probe.py (used by the checks).
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl iproute2 nftables \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir openai==3.51.0 tavily-python==0.8.5
# OpenShell runs the workload as this user and requires a WORKDIR it owns (the policy still makes it read-only).
RUN groupadd --gid 1500 sandbox && useradd --uid 1500 --gid sandbox --create-home sandbox \
    && install -d -o sandbox -g sandbox /sandbox /sandbox/runs
COPY app/ /sandbox/app/
RUN chmod -R a+rX /sandbox/app
ENV RADAR_WORK=/sandbox/runs
WORKDIR /sandbox
