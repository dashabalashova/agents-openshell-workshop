"""Agent radar: a small team of two LLMs that finds fresh articles and projects about AI agents.

    lead    the smart model: plans the searches, then reviews the candidates and writes the final answer
    workers the small model, one sub-agent per topic: runs the planned Tavily searches
    code    dates, URL checks, memory, report: fixed code, never a model

Nobody generates or runs code. Runs in the notebook and, copied as it is, inside the sandbox.
Files go to RADAR_WORK (default /workspace/radar; /sandbox/runs inside the sandbox).
"""
import json, os, re, time, datetime as dt
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from openai import APIError, OpenAI
from tavily import TavilyClient

# ---- settings ----------------------------------------------------------------------------------------------
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
LEAD_MODEL   = os.environ.get("LEAD_MODEL", "moonshotai/Kimi-K3")   # the smart model: planning and final decisions
WORKER_MODEL = os.environ.get("WORKER_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507")                     # the small model: simple sub-agents
# any pair of models with function calling works (check_model lists them)

MAX_SEARCHES          = 6     # search calls per run, all workers together
SEARCHES_PER_WORKER   = 3     # upper limit for one worker (the real limit is MAX_SEARCHES // number of topics)
CANDIDATES_PER_WORKER = 4     # candidates one worker may hand to the lead
MAX_FINDINGS          = 8     # findings in the report
TIMEOUT_S             = 120   # total time budget of one run, seconds
LEAD_RESERVE_S        = 30    # kept for the lead's final review: workers stop earlier when time is short
CALL_TIMEOUT_S        = 60    # one model request; the client retries a dropped or timed-out request twice (max_retries)
MAX_QUERY_WORDS       = 10    # a search query is plain words; long queries with operators return few or no results

KEY_NAMES = ("NEBIUS_API_KEY", "TAVILY_API_KEY")

WORK = Path(os.environ.get("RADAR_WORK", "/workspace/radar"))
SEEN_FILE   = WORK / "seen_urls.json"
TRACES_FILE = WORK / "traces.jsonl"

KINDS = ("paper", "project", "release", "post")

TOPICS = {
    "RAG":              "retrieval-augmented generation: indexes, chunking, reranking, grounding",
    "tool use":         "tool and function calling, MCP, tool schemas, tool errors",
    "frameworks":       "agent frameworks and SDKs (LangGraph, OpenAI Agents SDK, etc.)",
    "design patterns":  "agent patterns: ReAct, planner-executor, reflection, routing",
    "memory":           "agent memory: short-term, long-term, context compression",
    "multi-agent":      "multiple agents: orchestration, roles, communication, handoff",
    "optimization":     "cost, latency, caching, small models, prompt optimization",
    "data":             "data for agents: datasets, synthetic data, labeling, traces",
    "evaluation":       "agent evaluation: benchmarks, LLM-as-judge, regression tests",
    "safety":           "safety: prompt injection, sandboxing, permissions, guardrails",
    "coding agents":    "coding agents: SWE-bench, IDE agents, CLI agents",
    "personal agents":  "personal assistants: calendar, email, privacy, autonomy",
}


def show_settings():
    print(f"lead   {LEAD_MODEL}\nworker {WORKER_MODEL}")
    print(f"limits: {MAX_SEARCHES} searches, {TIMEOUT_S} s, {MAX_FINDINGS} findings, {CANDIDATES_PER_WORKER} candidates per worker")
    print(f"{len(TOPICS)} topics: {', '.join(TOPICS)}\nfiles: {WORK}")


def load_keys():
    """Check the two API keys (environment variables); prints only "set / not set", never a value."""
    for name in KEY_NAMES:
        print(f"{name}: {'set' if os.environ.get(name) else 'not set'}")
    return all(os.environ.get(n) for n in KEY_NAMES)


# ---- tools, helpers --------------------------------------------------------------------------------------
TOOLS = [{
    "type": "function",
    "function": {
        "name": "tavily_search",
        "description": "Search the web for fresh articles and projects. Returns title, url, content, date.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "search query"},
                                      "topic": {"type": "string", "enum": ["news", "general"],
                                                "description": "news: posts, news, releases (results have dates); general: papers and GitHub projects (usually no date)"}},
                       "required": ["query"]},
    },
}]


def _short(value, n=600):
    return value if not isinstance(value, str) or len(value) <= n else value[:n] + "…"

def log_trace(kind, **data):
    """One JSON line per event. API keys never get here: only call contents are written."""
    rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "kind": kind}
    rec.update({k: _short(v) for k, v in data.items()})
    TRACES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TRACES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def canon(url):
    """Canonical form of a URL for the memory and de-duplication: lower-case host, no fragment,
    no trailing slash, no tracking parameters (utm_*, ref, fbclid, ...)."""
    u = urlsplit((url or "").strip())
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                       if not (k.lower().startswith(("utm_", "mc_")) or k.lower() in ("ref", "fbclid", "gclid", "igshid"))])
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/") or "/", query, ""))

def load_seen():
    try:
        return {canon(u) for u in json.loads(SEEN_FILE.read_text())}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(urls):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(urls), ensure_ascii=False, indent=1))


def normalize_date(published, url):
    """Trusted date of a result: Tavily's date, else a date in the URL, else the arXiv month, else 'unknown'."""
    if published:
        try:
            return parsedate_to_datetime(published).date().isoformat()   # "Mon, 09 Nov 2026 00:00:00 GMT"
        except (TypeError, ValueError):
            m = re.search(r"\d{4}-\d{2}-\d{2}", published)
            if m:
                return m.group(0)
    m = re.search(r"(?<!\d)(20\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?!\d)", url or "")
    if m:
        return "-".join(m.groups())                                       # /2026/11/12/ or 2026-11-12
    m = re.search(r"arxiv\.org/(?:abs|html|pdf)/(\d{2})(0[1-9]|1[0-2])\.\d{4,5}", url or "")
    if m:
        return f"20{m.group(1)}-{m.group(2)}"                             # an arXiv id gives only the month
    return "unknown"


def parse_findings(text, allowed_urls, seen):
    """Parse a model's answer. Format errors -> ValueError. Invented and already shown URLs are dropped.
    allowed_urls maps url -> search result; the finding's date comes from that result."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)          # in case of a markdown wrapper
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON: {e}")
    if not isinstance(data, list):
        raise ValueError("a JSON array was expected")
    required = ("title", "url", "kind", "summary", "course_topic")
    out, dropped = [], []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() for k in required):
            raise ValueError(f"item {i}: non-empty string fields {required} are required")
        if item["kind"] not in KINDS:
            raise ValueError(f"item {i}: kind must be one of {KINDS}")
        if item["course_topic"] not in TOPICS:
            raise ValueError(f"item {i}: course_topic must be one of {list(TOPICS)}")
        if item["url"] not in allowed_urls:
            dropped.append((item["url"], "not in the Tavily results")); continue
        if canon(item["url"]) in seen or any(canon(o["url"]) == canon(item["url"]) for o in out):
            dropped.append((item["url"], "already shown")); continue
        finding = {k: item[k].strip() for k in required}
        finding["date"] = allowed_urls[item["url"]].get("date", "unknown")   # trusted date, not the model's
        out.append(finding)
    return out[:MAX_FINDINGS], dropped


# ---- prompts ---------------------------------------------------------------------------------------------
TOPIC_LIST = "\n".join(f"- {name}: {desc}" for name, desc in TOPICS.items())

def plan_prompt(per, days):
    return (f"You are the lead of a research radar for a student of the course CS 329Z: Engineering AI Agents.\n"
            f"For each requested course topic write up to {per} English web-search queries that find NEW items (last {days} days): "
            f"articles, papers, projects or releases about AI agents on that topic.\n"
            f"Each query is plain natural-language words, 3 to {MAX_QUERY_WORDS} words. No quotes, no parentheses, no OR / AND, "
            f"no site: / after: / before: operators, no dates: the search is already limited to the last {days} days. "
            f"Different queries of one topic should look at different angles (for example one for papers, one for news and releases).\n"
            f'Return ONLY a JSON object (no prose, no markdown): {{"<topic>": ["query", ...], ...}} with exactly the requested topics as keys.\n\n'
            f"Course topics:\n{TOPIC_LIST}")

def worker_prompt(topic, per):
    return (f'You are a search worker, a sub-agent of a research radar for the course CS 329Z. Your topic: "{topic}" ({TOPICS[topic]}).\n'
            f"Run the given queries with the tavily_search tool exactly as they are written (at most {per} calls). Use topic \"news\" for blog posts, news and releases "
            f"(results then have dates) and topic \"general\" for papers and GitHub projects.\n"
            f"Then return ONLY a JSON array (no prose, no markdown) of at most {CANDIDATES_PER_WORKER} candidates:\n"
            f'{{"title": str, "url": str, "kind": one of {list(KINDS)}, "summary": one sentence taken from the search results, '
            f'"course_topic": one topic from the list below}}\n'
            f"Rules: use only the search results; never invent links or facts; url must match a result url exactly; "
            f"you never generate or run code.\n\nCourse topics:\n{TOPIC_LIST}")

def review_prompt():
    return (f"You are the lead of a research radar for a student of the course CS 329Z: Engineering AI Agents.\n"
            f"Sub-agents found the candidates below. Choose the best ones, at most {MAX_FINDINGS}: relevant to AI agents, genuinely new, "
            f"not duplicates of each other (same page or same story), no marketing fluff. Fix course_topic if it is wrong. "
            f"Write the summary yourself: 1-2 sentences in English, what it is and why it is interesting.\n"
            f'Return ONLY a JSON array (no prose, no markdown) of {{"title": str, "url": str, "kind": one of {list(KINDS)}, '
            f'"summary": str, "course_topic": one topic from the list below}}. url must be one of the candidate urls exactly. '
            f"Do not output a date: the program adds it.\n\nCourse topics:\n{TOPIC_LIST}")


# ---- building blocks -------------------------------------------------------------------------------------
QUERY_OPERATORS = re.compile(r"\b(?:after|before|site|filetype|inurl|intitle|intext|related):|\b(?:OR|AND|NOT)\b|[\"()]")

def validate_plan(text, topics, per):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("a JSON object {topic: [queries]} was expected")
    plan = {}
    for topic in topics:
        queries = data.get(topic)
        if not isinstance(queries, list) or not queries or any(not isinstance(q, str) or not q.strip() for q in queries):
            raise ValueError(f"topic {topic!r}: a non-empty list of query strings is required")
        for q in queries:
            if QUERY_OPERATORS.search(q) or len(q.split()) > MAX_QUERY_WORDS:
                raise ValueError(f"topic {topic!r}: query {q!r} must be plain words (no quotes, parentheses, OR/AND, "
                                 f"after:/before:/site: operators, no dates), at most {MAX_QUERY_WORDS} words")
        plan[topic] = [q.strip() for q in queries][:per]
    return plan


def model_call(ctx, role, messages, tools=None):
    """One chat completion by the lead or by a worker; role decides the model. Every call is traced."""
    if ctx["left"]() <= 0:
        raise TimeoutError(f"timeout of {TIMEOUT_S} s exceeded")
    model = ctx[role + "_model"]
    kwargs = {"tools": tools} if tools else {}
    resp = ctx["client"].chat.completions.create(model=model, messages=messages, temperature=0.2,
                                                 timeout=min(CALL_TIMEOUT_S, max(5, ctx["left"]())), **kwargs)
    msg = resp.choices[0].message
    ctx["calls"][role] += 1
    log_trace("model_call", role=role, model=model, tools_enabled=bool(tools),
              tool_calls=[c.function.name for c in (msg.tool_calls or [])],
              finish=resp.choices[0].finish_reason, content=msg.content or "")
    return msg


def answer_json(ctx, role, messages, text, validate):
    """Validate a model answer. One retry with the error message; a second failure raises ValueError."""
    try:
        return validate(text)
    except ValueError as err:
        log_trace("invalid_output", role=role, error=str(err))
        messages += [{"role": "assistant", "content": text or ""},
                     {"role": "user", "content": f"Your answer is invalid: {err}. Return ONLY valid JSON in the required schema."}]
        return validate(model_call(ctx, role, messages).content)


def run_search(ctx, w, call):
    """Execute one tool call of a worker. Always returns a dict for the model; the loop never crashes here."""
    if call.function.name != "tavily_search":
        return {"error": f"unknown tool {call.function.name}"}
    if w["used"] >= w["per"] or ctx["searches"] >= MAX_SEARCHES:
        return {"error": "search limit reached, return the final JSON array now"}
    w["used"] += 1
    ctx["searches"] += 1
    try:
        args = json.loads(call.function.arguments)
        query = args["query"]
        topic = args.get("topic") if args.get("topic") in ("news", "general") else "news"
        raw = ctx["tavily"].search(query=query, topic=topic, search_depth="basic", time_range=ctx["time_range"],
                                   max_results=6, timeout=min(30, max(5, ctx["left"]())))
        items = [{"title": r.get("title"), "url": r.get("url"), "content": _short(r.get("content", ""), 500),
                  "date": normalize_date(r.get("published_date"), r.get("url"))} for r in raw.get("results", [])]
        w["found"].update({it["url"]: it for it in items if it["url"]})
        log_trace("tool_call", role="worker", worker=w["topic"], tool="tavily_search", query=query, topic=topic,
                  n_results=len(items), n_dated=sum(1 for it in items if it["date"] != "unknown"),
                  urls=" ".join(it["url"] for it in items if it["url"]))
        return {"results": items}
    except Exception as e:
        log_trace("tool_error", role="worker", worker=w["topic"], tool="tavily_search", error=str(e))
        return {"error": f"{type(e).__name__}: {e}"}


def run_worker(ctx, topic, queries):
    """A sub-agent (small model): runs the planned searches for one topic and returns candidates."""
    w = {"topic": topic, "per": len(queries), "used": 0, "found": {}}
    messages = [{"role": "system", "content": worker_prompt(topic, w["per"])},
                {"role": "user", "content": "Queries to run:\n" + "\n".join(f"- {q}" for q in queries)}]
    log_trace("worker_start", role="worker", worker=topic, queries=" | ".join(queries))
    text = None
    for _ in range(w["per"] + 3):
        force_final = w["used"] >= w["per"] or ctx["searches"] >= MAX_SEARCHES or ctx["left"]() < LEAD_RESERVE_S
        msg = model_call(ctx, "worker", messages, tools=None if force_final else TOOLS)
        if not msg.tool_calls:
            text = msg.content
            break
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in msg.tool_calls]})
        for call in msg.tool_calls:
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(run_search(ctx, w, call), ensure_ascii=False)})
    else:
        messages.append({"role": "user", "content": "Step limit reached. Return the final JSON array now."})
        text = model_call(ctx, "worker", messages).content
    candidates = answer_json(ctx, "worker", messages, text,
                             lambda t: parse_findings(t, w["found"], ctx["seen"])[0][:CANDIDATES_PER_WORKER])
    ctx["allowed"].update(w["found"])
    log_trace("worker_done", role="worker", worker=topic, searches=w["used"], candidates=len(candidates))
    return candidates


# ---- the team --------------------------------------------------------------------------------------------
def run_radar(topics=None, days=7, client=None, tavily=None, lead_model=None, worker_model=None):
    """One radar run: the lead plans, one small worker per topic searches, the lead reviews.
    Returns the new findings (at most MAX_FINDINGS); prints who did what."""
    topics = list(topics or TOPICS)
    bad = [t for t in topics if t not in TOPICS]
    if bad:
        raise ValueError(f"unknown topics: {bad}")
    if len(topics) > MAX_SEARCHES:
        print(f"note: at most {MAX_SEARCHES} topics per run (one search each); using the first {MAX_SEARCHES}")
        topics = topics[:MAX_SEARCHES]
    per = min(SEARCHES_PER_WORKER, MAX_SEARCHES // len(topics))
    started = time.monotonic()
    ctx = {"client": client or OpenAI(base_url=BASE_URL, api_key=os.environ["NEBIUS_API_KEY"], max_retries=2),
           "tavily": tavily or TavilyClient(api_key=os.environ["TAVILY_API_KEY"]),
           "lead_model": lead_model or LEAD_MODEL, "worker_model": worker_model or WORKER_MODEL,
           "time_range": "day" if days <= 1 else "week" if days <= 7 else "month",
           "seen": load_seen(), "allowed": {}, "searches": 0, "calls": Counter(),
           "left": lambda: TIMEOUT_S - (time.monotonic() - started)}
    log_trace("run_start", role="code", topics=", ".join(topics), lead_model=ctx["lead_model"], worker_model=ctx["worker_model"])

    # 1. the lead (smart model) plans the searches
    messages = [{"role": "system", "content": plan_prompt(per, days)},
                {"role": "user", "content": f"Topics: {json.dumps(topics)}. Today is {dt.date.today().isoformat()}."}]
    plan = answer_json(ctx, "lead", messages, model_call(ctx, "lead", messages).content,
                       lambda t: validate_plan(t, topics, per))
    log_trace("plan", role="lead", plan=json.dumps(plan, ensure_ascii=False))

    # 2. one small worker (sub-agent) per topic; a failing worker is skipped, the run goes on
    candidates, workers_run = [], 0
    for topic in topics:
        if ctx["left"]() < LEAD_RESERVE_S + 10 or ctx["searches"] >= MAX_SEARCHES:
            log_trace("worker_skipped", role="code", worker=topic, reason="time or search budget")
            continue
        workers_run += 1
        try:
            candidates += run_worker(ctx, topic, plan[topic])
        except (ValueError, APIError) as err:                # invalid JSON twice, or the model API failed after its retries
            log_trace("worker_failed", role="worker", worker=topic, error=f"{type(err).__name__}: {err}")
    unique = {}
    for c in candidates:
        unique.setdefault(canon(c["url"]), c)               # the same page found by two workers counts once
    candidates = list(unique.values())

    # 3. the lead (smart model) reviews the candidates and makes the final decisions
    findings, dropped = [], []
    if candidates:
        allowed = {c["url"]: ctx["allowed"][c["url"]] for c in candidates}     # a finding must be one of the candidates
        payload = [{k: c[k] for k in ("title", "url", "date", "kind", "summary", "course_topic")} for c in candidates]
        messages = [{"role": "system", "content": review_prompt()},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        findings, dropped = answer_json(ctx, "lead", messages, model_call(ctx, "lead", messages).content,
                                        lambda t: parse_findings(t, allowed, ctx["seen"]))
    for url, why in dropped:
        log_trace("dropped", role="code", url=url, reason=why)
    save_seen(ctx["seen"] | {canon(f["url"]) for f in findings})

    seconds = round(time.monotonic() - started)
    print(f"searches: {ctx['searches']}/{MAX_SEARCHES}, time: {seconds} s, findings: {len(findings)}, dropped: {len(dropped)}")
    print(f"lead   {ctx['lead_model']}: {ctx['calls']['lead']} calls (plan, review)")
    print(f"worker {ctx['worker_model']}: {ctx['calls']['worker']} calls in {workers_run} sub-agents -> {len(candidates)} candidates")
    return findings


def check_model(client=None):
    """Check that both models are in GET /v1/models. See the Token Factory console for the models you can use."""
    client = client or OpenAI(base_url=BASE_URL, api_key=os.environ["NEBIUS_API_KEY"])
    ids = sorted(m.id for m in client.models.list())
    print(f"models available: {len(ids)}")
    for role, model in (("lead", LEAD_MODEL), ("worker", WORKER_MODEL)):
        print(f"  {role:6} {model!r}: {'available' if model in ids else 'NOT in the list, set ' + role.upper() + '_MODEL'}")
    return ids


# ---- report and traces (notebook helpers) ----------------------------------------------------------------
def show_report(findings):
    """A table of findings and a chart 'findings by course topic', drawn by fixed code."""
    if not findings:
        print("No new findings.")
        return
    import pandas as pd
    import matplotlib.pyplot as plt
    from IPython.display import display, HTML
    df = pd.DataFrame(findings)[["title", "kind", "course_topic", "date", "summary", "url"]]
    display(HTML(df.to_html(index=False, escape=True, render_links=True)))
    counts = df["course_topic"].value_counts().reindex(list(TOPICS), fill_value=0)
    counts = counts[counts > 0].sort_values()
    ax = counts.plot.barh(figsize=(7, max(2, 0.45 * len(counts) + 1)), color="#4C78A8")
    ax.set_xlabel("findings"); ax.set_ylabel(""); ax.set_title("Findings by course topic")
    ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
    plt.tight_layout(); plt.show()


def show_traces(n=14, path=None):
    """The latest events of traces.jsonl (or of another trace file): role (lead / worker / code), model and tool of every call."""
    path = Path(path) if path else TRACES_FILE
    if not path.exists() or not path.read_text().strip():
        print("No traces yet.")
        return
    for line in path.read_text().splitlines()[-n:]:
        rec = json.loads(line)
        print(rec["ts"], rec["kind"], {k: v for k, v in rec.items() if k not in ("ts", "kind", "content", "urls")})
