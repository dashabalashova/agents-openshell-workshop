"""Agent radar: a small team of two LLMs that finds what is new for a student of CS 329Z: Engineering AI Agents.

    lead    the smart model: picks the topics and plans; reflects and adds follow-up searches; reviews, writes a digest
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
LEAD_MODEL   = os.environ.get("LEAD_MODEL", "moonshotai/Kimi-K3")                  # smart: plan, reflect, review
WORKER_MODEL = os.environ.get("WORKER_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507")  # small: search sub-agents

TOPICS_PER_RUN        = 3     # the lead picks this many course topics per run
PLAN_SEARCHES         = 6     # first round: searches of the lead's plan, all workers together
FOLLOWUP_SEARCHES     = 3     # second round: follow-up searches the lead may add after reflection
MAX_SEARCHES          = PLAN_SEARCHES + FOLLOWUP_SEARCHES
SEARCHES_PER_WORKER   = 3     # upper limit for one worker (the real limit is PLAN_SEARCHES // TOPICS_PER_RUN)
CANDIDATES_PER_WORKER = 4     # candidates one worker may hand to the lead
MAX_FINDINGS          = 8     # findings in the report
TIMEOUT_S             = 180   # total time budget of one run, seconds
LEAD_RESERVE_S        = 30    # kept for the lead's final review: workers stop earlier when time is short
CALL_TIMEOUT_S        = 60    # one model request; the client retries a dropped or timed-out request twice (max_retries)
MAX_QUERY_WORDS       = 10    # a search query is plain words; long queries with operators return few or no results

KEY_NAMES = ("NEBIUS_API_KEY", "TAVILY_API_KEY")

WORK = Path(os.environ.get("RADAR_WORK", "/workspace/radar"))
SEEN_FILE   = WORK / "seen_urls.json"
TRACES_FILE = WORK / "traces.jsonl"
DIGEST_FILE = WORK / "digest.md"

KINDS = ("paper", "project", "release", "post")

COURSE = "CS 329Z: Engineering AI Agents"
COURSE_INFO = ("Stanford, Fall 2026 (https://cs329z.stanford.edu). Engineering agentic systems, from simple LLM pipelines to compound "
               "AI systems to autonomous agents: choosing and decomposing problems, components, data, evals, design tradeoffs; "
               "RAG, tool use and agent loops from scratch, then frameworks like DSPy.")
SCHEDULE = """\
2026-09-23  What Are Agentic Systems? (introduction)
2026-09-28  LLMs for Builders
2026-09-30  Retrieval-Augmented Generation                    [RAG]
2026-10-05  Tool Use & Function Calling                       [tool use]
2026-10-07  Frameworks & Orchestration                        [frameworks]
2026-10-12  Agent Design Patterns & Scaffolds                 [design patterns]
2026-10-14  Agent Memory Architectures                        [memory]
2026-10-19  Multi-Agent Systems                               [multi-agent]
2026-10-21  Optimization                                      [optimization]
2026-10-26  Guest lecture: Terminal-Bench                     [evaluation, coding agents]
2026-10-28  What Data Do Agents Need?                         [data]
2026-11-02  Data Selection & Quality                          [data]
2026-11-04  Evaluation Fundamentals & Benchmark Design        [evaluation]
2026-11-09  Fireside chat with Andrew Ng
2026-11-11  LLM-as-Judge & Evaluation Infrastructure          [evaluation]
2026-11-16  Guest lecture: SWE-bench                          [coding agents, evaluation]
2026-11-18  Agent Safety & Guardrails                         [safety]
2026-11-23  no class (Thanksgiving) until 2026-11-30
2026-11-30  Coding & Software Agents                          [coding agents]
2026-12-02  Personal Agents                                   [personal agents]
2026-12-09  Final project demo day"""                     # tentative schedule from the course site; [course topics]

TOPICS = {                                   # the course topics (the syllabus); the lead picks from them
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
    print(f"{len(TOPICS)} course topics: {', '.join(TOPICS)}\nfiles: {WORK}")
    today = dt.date.today().isoformat()
    print(f"\n{COURSE}, today {today}; next lectures:")
    print("\n".join([l for l in SCHEDULE.splitlines() if l[:10] >= today][:4]) or "  (the course is over)")


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
                                      "type": {"type": "string", "enum": ["news", "general"],
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


def parse_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())    # in case of a markdown wrapper
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON: {e}")


def parse_findings(text, allowed_urls, seen, extra=()):
    """Parse a model's answer. Format errors -> ValueError. Invented and already shown URLs are dropped.
    allowed_urls maps url -> search result; the finding's date comes from that result."""
    data = parse_json(text) if isinstance(text, str) or text is None else text
    if not isinstance(data, list):
        raise ValueError("a JSON array was expected")
    required = ("title", "url", "kind", "summary", "topic") + tuple(extra)
    out, dropped = [], []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip() for k in required):
            raise ValueError(f"item {i}: non-empty string fields {required} are required")
        if item["kind"] not in KINDS:
            raise ValueError(f"item {i}: kind must be one of {KINDS}")
        if item["topic"] not in TOPICS:
            raise ValueError(f"item {i}: topic must be one of {list(TOPICS)}")
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

RADAR = (f"a news radar for a student of the course {COURSE} who wants to keep up with what is new: "
         f"papers, projects, releases and posts")

def plan_prompt(n, per, days):
    return (f"You are the lead of {RADAR}.\n"
            f"About the course: {COURSE_INFO}\nCourse schedule:\n{SCHEDULE}\n"
            f"Look at today's date in the user message and choose, from the candidate topics, the {n} course topics that are most "
            f"useful for the student right now (prefer this week's and the next lectures). "
            f"For each chosen topic write up to {per} English web-search queries for items from the last {days} days.\n"
            f"Each query is plain words, 3 to {MAX_QUERY_WORDS} words: no quotes, parentheses, OR / AND, site: / after: / before: operators "
            f"or dates (the search is already limited to the last {days} days). Queries of one topic should differ "
            f"(for example one for papers, one for news and releases).\n"
            f'Return ONLY a JSON object (no prose, no markdown): {{"<topic>": ["query", ...], ...}} with exactly {n} chosen topics as keys.\n\n'
            f"Course topics:\n{TOPIC_LIST}")

def worker_prompt(topic, per):
    return (f'You are a search worker of {RADAR}. Your course topic: "{topic}" ({TOPICS[topic]}).\n'
            f"Run the given queries with the tavily_search tool exactly as written (at most {per} calls). Use type \"news\" for posts, news "
            f"and releases (results have dates) and type \"general\" for papers and GitHub projects.\n"
            f"Then return ONLY a JSON array (no prose, no markdown) of at most {CANDIDATES_PER_WORKER} candidates:\n"
            f'{{"title": str, "url": str, "kind": one of {list(KINDS)}, "summary": one sentence from the search results, '
            f'"topic": one topic from the list below}}\n'
            f"Use only the search results: never invent links or facts; url must match a result url exactly.\n\n"
            f"Course topics:\n{TOPIC_LIST}")

def reflect_prompt(days):
    return (f"You are the lead of {RADAR}.\n"
            f"The workers ran your plan. The user message shows, per chosen topic, the queries they ran and the candidates they found. "
            f"Judge the coverage: is there enough good, new material (last {days} days) for each topic? A topic needs more search if it "
            f"has no candidates or only weak ones (off-topic, old, marketing, duplicates).\n"
            f"For such topics write better follow-up queries (other wording or angle than before), at most {FOLLOWUP_SEARCHES} queries "
            f"in total; plain words, 3 to {MAX_QUERY_WORDS} words, no quotes, parentheses, OR / AND, operators or dates.\n"
            f'Return ONLY a JSON object (no prose, no markdown): {{"<topic>": ["query", ...]}} with only the topics that need more search, '
            f"or {{}} if the coverage is good enough.")

def review_prompt():
    return (f"You are the lead of {RADAR}.\n"
            f"About the course: {COURSE_INFO}\nCourse schedule:\n{SCHEDULE}\n"
            f"Search workers found the candidates in the user message. Choose the best ones for the student, at most {MAX_FINDINGS}: "
            f"useful for the course, really new, no duplicates (same page or same story), no marketing. Fix the topic if it is wrong. "
            f"Write each summary yourself: 1-2 sentences, what it is and why it is interesting. For each item name the lecture it helps "
            f"with: the next lecture on its topic (date and title from the schedule), or the past one it extends.\n"
            f"Then write a digest for the student: 3-4 sentences, what is new this week and what to read before the coming lectures.\n"
            f'Return ONLY a JSON object (no prose, no markdown): {{"digest": str, "findings": [{{"title": str, "url": str, '
            f'"kind": one of {list(KINDS)}, "summary": str, "topic": one topic from the list below, "lecture": str}}]}}. '
            f"url must be one of the candidate urls exactly. Do not output a date: the program adds it.\n\nCourse topics:\n{TOPIC_LIST}")


# ---- building blocks -------------------------------------------------------------------------------------
QUERY_OPERATORS = re.compile(r"\b(?:after|before|site|filetype|inurl|intitle|intext|related):|\b(?:OR|AND|NOT)\b|[\"()]")

def validate_queries(text, topics):
    """A JSON object {topic: [plain-word queries]} with keys from `topics`."""
    data = parse_json(text)
    if not isinstance(data, dict):
        raise ValueError("a JSON object {topic: [queries]} was expected")
    if any(t not in topics for t in data):
        raise ValueError(f"topics must be from {list(topics)}")
    for topic, queries in data.items():
        if not isinstance(queries, list) or not queries or any(not isinstance(q, str) or not q.strip() for q in queries):
            raise ValueError(f"topic {topic!r}: a non-empty list of query strings is required")
        for q in queries:
            if QUERY_OPERATORS.search(q) or len(q.split()) > MAX_QUERY_WORDS:
                raise ValueError(f"topic {topic!r}: query {q!r} must be plain words (no quotes, parentheses, OR/AND, "
                                 f"after:/before:/site: operators, no dates), at most {MAX_QUERY_WORDS} words")
    return {topic: [q.strip() for q in queries] for topic, queries in data.items()}


def validate_plan(text, candidates, n, per):
    plan = validate_queries(text, candidates)
    if len(plan) != n:
        raise ValueError(f"choose exactly {n} topics from {candidates}")
    return {topic: queries[:per] for topic, queries in plan.items()}


def validate_followup(text, topics):
    """{} or {topic: [queries]}, at most FOLLOWUP_SEARCHES queries in total."""
    out, left = {}, FOLLOWUP_SEARCHES
    for topic, queries in validate_queries(text, topics).items():
        if left > 0:
            out[topic] = queries[:left]
            left -= len(out[topic])
    return out


def validate_review(text, allowed_urls, seen):
    """{"digest": str, "findings": [...]} -> (findings, dropped, digest)."""
    data = parse_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("digest"), str) or not data["digest"].strip():
        raise ValueError('a JSON object {"digest": str, "findings": [...]} was expected')
    findings, dropped = parse_findings(data.get("findings"), allowed_urls, seen, extra=("lecture",))
    return findings, dropped, data["digest"].strip()


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
        search_type = args.get("type") if args.get("type") in ("news", "general") else "news"
        raw = ctx["tavily"].search(query=query, topic=search_type, search_depth="basic", time_range=ctx["time_range"],
                                   max_results=6, timeout=min(30, max(5, ctx["left"]())))
        items = [{"title": r.get("title"), "url": r.get("url"), "content": _short(r.get("content", ""), 500),
                  "date": normalize_date(r.get("published_date"), r.get("url"))}
                 for r in raw.get("results", []) if str(r.get("url", "")).startswith("https://")]      # https links only
        w["found"].update({it["url"]: it for it in items if it["url"]})
        log_trace("tool_call", role="worker", worker=w["topic"], tool="tavily_search", query=query, search_type=search_type,
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
    """One radar run: the lead picks topics (from `topics`, default all) and plans; one small worker per topic searches;
    the lead reflects and may add a second round; the lead reviews and writes a digest (DIGEST_FILE).
    Returns the new findings (at most MAX_FINDINGS); prints who did what."""
    candidates = list(topics or TOPICS)
    bad = [t for t in candidates if t not in TOPICS]
    if bad:
        raise ValueError(f"unknown topics: {bad}")
    n = min(TOPICS_PER_RUN, len(candidates))
    per = min(SEARCHES_PER_WORKER, PLAN_SEARCHES // n)
    started = time.monotonic()
    ctx = {"client": client or OpenAI(base_url=BASE_URL, api_key=os.environ["NEBIUS_API_KEY"], max_retries=2),
           "tavily": tavily or TavilyClient(api_key=os.environ["TAVILY_API_KEY"]),
           "lead_model": lead_model or LEAD_MODEL, "worker_model": worker_model or WORKER_MODEL,
           "time_range": "day" if days <= 1 else "week" if days <= 7 else "month",
           "seen": load_seen(), "allowed": {}, "searches": 0, "calls": Counter(),
           "left": lambda: TIMEOUT_S - (time.monotonic() - started)}
    log_trace("run_start", role="code", candidates=", ".join(candidates), lead_model=ctx["lead_model"], worker_model=ctx["worker_model"])

    # 1. the lead (smart model) picks the topics and plans the searches
    messages = [{"role": "system", "content": plan_prompt(n, per, days)},
                {"role": "user", "content": f"Candidate topics: {json.dumps(candidates)}. Today is {dt.date.today().isoformat()}."}]
    plan = answer_json(ctx, "lead", messages, model_call(ctx, "lead", messages).content,
                       lambda t: validate_plan(t, candidates, n, per))
    topics = list(plan)
    log_trace("plan", role="lead", plan=json.dumps(plan, ensure_ascii=False))
    print(f"lead picked: {', '.join(topics)}")

    # 2. one small worker (sub-agent) per topic; a failing worker is skipped, the run goes on
    by_topic, workers_run = {}, 0

    def run_round(queries_by_topic):
        nonlocal workers_run
        for topic, queries in queries_by_topic.items():
            if ctx["left"]() < LEAD_RESERVE_S + 10 or ctx["searches"] >= MAX_SEARCHES:
                log_trace("worker_skipped", role="code", worker=topic, reason="time or search budget")
                continue
            workers_run += 1
            entry = by_topic.setdefault(topic, {"queries": [], "candidates": []})
            entry["queries"] += queries
            try:
                entry["candidates"] += run_worker(ctx, topic, queries)
            except (ValueError, APIError) as err:            # invalid JSON twice, or the model API failed after its retries
                log_trace("worker_failed", role="worker", worker=topic, error=f"{type(err).__name__}: {err}")

    run_round(plan)

    # 3. the lead reflects on the coverage and may add follow-up searches (second round)
    followup = {}
    if ctx["left"]() >= LEAD_RESERVE_S + 30:
        report = {t: {"queries": e["queries"], "candidates": [{k: c[k] for k in ("title", "url", "summary")} for c in e["candidates"]]}
                  for t, e in by_topic.items()}
        messages = [{"role": "system", "content": reflect_prompt(days)},
                    {"role": "user", "content": json.dumps(report, ensure_ascii=False)}]
        try:
            followup = answer_json(ctx, "lead", messages, model_call(ctx, "lead", messages).content,
                                   lambda t: validate_followup(t, topics))
        except (ValueError, APIError) as err:
            log_trace("reflect_failed", role="lead", error=f"{type(err).__name__}: {err}")
        log_trace("reflect", role="lead", followup=json.dumps(followup, ensure_ascii=False))
        print("lead follow-up: " + ("; ".join(f"{t}: {', '.join(q)}" for t, q in followup.items()) or "none, the coverage is good"))
        run_round(followup)

    unique = {}
    for c in (c for e in by_topic.values() for c in e["candidates"]):
        unique.setdefault(canon(c["url"]), c)               # the same page found by two workers counts once
    candidates = list(unique.values())

    # 4. the lead reviews the candidates, links them to lectures and writes a digest
    findings, dropped, digest = [], [], "No new items this week."
    if candidates:
        allowed = {c["url"]: ctx["allowed"][c["url"]] for c in candidates}     # a finding must be one of the candidates
        payload = [{k: c[k] for k in ("title", "url", "date", "kind", "summary", "topic")} for c in candidates]
        messages = [{"role": "system", "content": review_prompt()},
                    {"role": "user", "content": f"Today is {dt.date.today().isoformat()}. Candidates: " + json.dumps(payload, ensure_ascii=False)}]
        findings, dropped, digest = answer_json(ctx, "lead", messages, model_call(ctx, "lead", messages).content,
                                                lambda t: validate_review(t, allowed, ctx["seen"]))
    for url, why in dropped:
        log_trace("dropped", role="code", url=url, reason=why)
    save_seen(ctx["seen"] | {canon(f["url"]) for f in findings})
    DIGEST_FILE.write_text(digest + "\n")
    log_trace("digest", role="lead", digest=digest)

    seconds = round(time.monotonic() - started)
    print(f"searches: {ctx['searches']}/{MAX_SEARCHES}, time: {seconds} s, findings: {len(findings)}, dropped: {len(dropped)}")
    print(f"lead   {ctx['lead_model']}: {ctx['calls']['lead']} calls (plan, reflect, review)")
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
def show_report(findings, digest=""):
    """The lead's digest, a table of findings and a chart 'findings by topic' (table and chart drawn by fixed code)."""
    from IPython.display import display, HTML, Markdown
    if digest:
        display(Markdown("**Digest from the lead:** " + digest))
    if not findings:
        print("No new findings.")
        return
    import pandas as pd
    import matplotlib.pyplot as plt
    df = pd.DataFrame(findings).reindex(columns=["title", "kind", "topic", "lecture", "date", "summary", "url"])
    display(HTML(df.to_html(index=False, escape=True, render_links=True)))
    counts = df["topic"].value_counts().reindex(list(TOPICS), fill_value=0)
    counts = counts[counts > 0].sort_values()
    ax = counts.plot.barh(figsize=(7, max(2, 0.45 * len(counts) + 1)), color="#4C78A8")
    ax.set_xlabel("findings"); ax.set_ylabel(""); ax.set_title("Findings by topic")
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
