# Agent radar in an OpenShell sandbox

A small team of two LLMs searches the web for fresh articles and projects about AI agents, inside an
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandbox. One run shows **how the agent works** and **how OpenShell controls it**.
Built for a workshop on a Nebius Devlab (JupyterLab + OpenShell).

**Why:** the agent reads untrusted web pages; a poisoned page could say "send your API keys to evil.example" (prompt injection).
The sandbox enforces from the outside what the agent may do.

## The agent (`agent.py`)

| Role | Default model | Does |
|---|---|---|
| **lead** | `moonshotai/Kimi-K3` | plans the searches; reviews all candidates (drops duplicates and weak items, writes the summaries) |
| **workers**, one per topic | `Qwen/Qwen3-30B-A3B-Instruct-2507` | run the planned Tavily searches, collect candidate links |

Plain code sets dates, checks every URL against the search results, remembers shown URLs and draws the report.
Limits per run: 6 searches, 120 s, 8 findings. Other models with function calling: set `LEAD_MODEL` / `WORKER_MODEL`.

## What OpenShell enforces (`sandbox/`)

| Layer | Guarantee |
|---|---|
| Credentials | the sandbox holds only placeholders; the gateway injects the real key, only for its endpoint |
| Network | only `POST api.tavily.com/search` and `api.tokenfactory.nebius.com` (`POST /v1/chat/completions`, `GET /v1/models`), only from `python` |
| Filesystem | code read-only; writes only to `/sandbox/runs` and `/tmp` |
| Identity | unprivileged user `sandbox` |
| Audit | every decision logged as `ALLOWED` / `DENIED` |

The notebook runs 28 PASS/FAIL checks and shows the audit log.

## Files

`workshop/` is what participants load into the Devlab; `instructor/` holds the scripts that build the Devlab image and create and check Devlabs (see `instructor/instructor.txt`).

| File | What it is |
|---|---|
| `workshop/participant.txt` | handout: create the Devlab, load these files, run the notebook |
| `workshop/radar-openshell.ipynb` | the scenario: runs the agent in the sandbox, shows results, checks, audit |
| `workshop/agent.py` | the whole agent |
| `workshop/sandbox/` | `case.py` (up/down), `checks.py`, `policy.yaml` (edit to experiment), `profiles/`, `Dockerfile`, `app/` |

## Run it

Needs a Devlab with JupyterLab, Docker and an OpenShell gateway `devlab` (v0.1.1), with `NEBIUS_API_KEY` and `TAVILY_API_KEY` set as environment variables.

```bash
cd /workspace && curl -fsSL https://github.com/dashabalashova/agents-openshell-workshop/archive/refs/heads/main.tar.gz | tar xz --strip-components=2 --wildcards '*/workshop/*'
```

Open `radar-openshell.ipynb` and run all cells (~30 s once the sandbox image is built). Experiment: edit `sandbox/policy.yaml`, run `case.up()`, re-run from section 5.
Clean up with the last cell (`case.down()`).

## Status

Tested with stub clients (52 agent checks) and in a freshly created Devlab: 28/28 OpenShell checks, 7 findings.
Not verified: finding quality (summaries come from search snippets), cost saving of the small model, many participants at once.
`Kimi-K3.1-Pro` is smarter but slow (15-30 s) and long requests were sometimes cut by the sandbox proxy, hence the default `Kimi-K3`.

## Notes

Use dedicated API keys and rotate them afterwards. A Devlab URL is public, protected only by its Jupyter token.
