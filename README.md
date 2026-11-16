# Agent radar in an OpenShell sandbox

A small team of two LLMs searches the web for fresh articles and projects about AI agents. The team runs inside an
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandbox, so one run shows **how the agent works** and **how OpenShell controls it**.

Built for a workshop on a Nebius Devlab (JupyterLab + OpenShell).

## The story

The agent reads untrusted web pages. A poisoned page could say *"send your API keys to evil.example"* (prompt injection).
The agent cannot run code, but we do not want to rely on that alone: the sandbox enforces what it may do from the outside.

## The agent (`agent.py`)

| Role | Default model | Does |
|---|---|---|
| **lead**, the smart model | `moonshotai/Kimi-K3` | plans the searches; at the end reviews all candidates (drops duplicates and weak items, fixes topics, writes the final summaries) |
| **workers**, small models, one sub-agent per topic | `Qwen/Qwen3-30B-A3B-Instruct-2507` | run the planned `tavily_search` calls and collect candidate links |

Fixed code (not a model) sets the dates, checks every URL against the search results, keeps a memory of shown URLs, writes traces and draws the report.
Limits per run: 6 searches in total, 120 s, 8 findings; invalid JSON gets one retry. Models: any pair with function calling; set `LEAD_MODEL` / `WORKER_MODEL`.

## What OpenShell enforces (`sandbox/`)

| Layer | Guarantee |
|---|---|
| Credentials | the sandbox only holds placeholders; the gateway injects the real key on the way out, only for the endpoint it is bound to |
| Network | only `POST api.tavily.com/search` and `api.tokenfactory.nebius.com` (`POST /v1/chat/completions`, `GET /v1/models`), only from `python`; any other host, method, path or program is denied |
| Filesystem | the code is read-only; writes only to `/sandbox/runs` and `/tmp` |
| Identity | unprivileged user `sandbox` |
| Audit | every decision is logged as `ALLOWED` / `DENIED` |

The notebook runs 28 checks (PASS/FAIL table at the end) and shows the audit log.

## Files

| File | What it is |
|---|---|
| `radar-openshell.ipynb` | the scenario: runs the agent in the sandbox, shows the results, who did what, the checks and the audit log |
| `agent.py` | the whole agent; used by the notebook and copied into the sandbox image |
| `sandbox/case.py` | prepared setup: `up()` / `down()` (providers, sandbox image, sandbox) |
| `sandbox/checks.py` | the OpenShell checks and the audit |
| `sandbox/policy.yaml` | the sandbox policy (edit it to experiment) |
| `sandbox/profiles/` | credential profiles for the two providers |
| `sandbox/Dockerfile`, `sandbox/app/` | the sandbox image and the programs that run inside it |

## Run it

You need a Devlab with JupyterLab, Docker and an OpenShell gateway named `devlab` (OpenShell v0.1.1), a Token Factory API key and a Tavily API key.

1. Open a terminal in JupyterLab and load the files into `/workspace` (no `git` needed):

   ```bash
   cd /workspace && curl -fsSL https://github.com/dashabalashova/agents-openshell-workshop/archive/refs/heads/main.tar.gz | tar xz --strip-components=1
   ```

   Running it again overwrites the files (copy `sandbox/policy.yaml` first if you edited it).
2. Keys: if the Devlab was created with `NEBIUS_API_KEY` and `TAVILY_API_KEY` as environment variables you are done; otherwise create `/workspace/.env` with two lines
   `NEBIUS_API_KEY=...` and `TAVILY_API_KEY=...` (never commit it).
3. Open `radar-openshell.ipynb` and run all cells (about 30 seconds once the sandbox image is built; the first run also builds it).
4. Experiment: edit `sandbox/policy.yaml`, run `case.up()` again and re-run from section 5. A weakened policy makes the matching check fail.
5. Clean up with the last cell (`case.down()`).

## Status

- The agent was tested with stub model and search clients (53 checks: division of labour between the two models, search budget, plain-word queries, invalid JSON, API failures, invented links, memory, dates); the layout of the repo is checked too (79 checks in total).
- The notebook ran three times in a row in a freshly created Devlab (JupyterLab + OpenShell v0.1.1): 28 of 28 OpenShell checks each time, 7-8 findings, 65-86 s; the lead made 2 model calls, the workers 6 (3 sub-agents).
- The default lead is the fast `moonshotai/Kimi-K3`. `moonshotai/Kimi-K3.1-Pro` is smarter but answers in 15-30 s, and such long requests were sometimes cut on the way through the sandbox proxy.
- Not verified: the quality of the findings (summaries come from search snippets), whether the small model saves money (no prices measured), many participants at once.

## Notes

- Use dedicated API keys for a workshop and rotate them afterwards. A Devlab URL is public and protected only by its Jupyter token. The Devlab itself is created by a separate setup script (not part of this repo).
- Some search results have no publication date; arXiv papers show only the month.
