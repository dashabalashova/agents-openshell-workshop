# Sandbox image: python + client libraries + agent.py (copied in by case.up()) + main.py (entry) + probe.py (used by the checks).
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl iproute2 nftables \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir openai==3.51.0 tavily-python==0.8.5
# OpenShell runs the workload as this user and requires a WORKDIR it owns (the policy still makes it read-only).
RUN groupadd --gid 1500 sandbox && useradd --uid 1500 --gid sandbox --create-home sandbox \
    && install -d -o sandbox -g sandbox /sandbox /sandbox/runs
COPY app/ /sandbox/app/
RUN chmod -R a+rX /sandbox/app
WORKDIR /sandbox
