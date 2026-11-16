#!/usr/bin/env python3
"""Check a Devlab the way a participant uses it (called by 3-check-devlab.sh).
    JUPYTER_TOKEN=... python verify.py <devlab url> <github owner/repo> <notebook>
Exit code 0 only if all checks pass. The token is read from the environment, never printed."""
import ast, json, os, re, sys, time, urllib.error, urllib.request, uuid

import websocket

URL, REPO, NOTEBOOK = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
TOKEN = os.environ["JUPYTER_TOKEN"]
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  -> {detail}" if detail else ""), flush=True)
    return ok


def api(path, method="GET", data=None, auth=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "token " + TOKEN
    req = urllib.request.Request(URL + path, method=method, headers=headers, data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else {}


def status(auth):
    try:
        req = urllib.request.Request(URL + "/api/status", headers={"Authorization": "token " + TOKEN} if auth else {})
        return urllib.request.urlopen(req, timeout=20).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ---- 1. reachable, and the token is required
for _ in range(36):
    if status(True) == 200:
        break
    time.sleep(5)
check("Jupyter answers with the token", status(True) == 200)
check("Jupyter refuses a request without the token", status(False) == 403)
if not all(results):
    sys.exit(1)

# ---- a kernel to run commands inside the Devlab
kernel = api("/api/kernels", "POST", {"name": "python3"})
ws = websocket.create_connection(URL.replace("https", "wss", 1) + f"/api/kernels/{kernel['id']}/channels?token={TOKEN}", timeout=600)


def run(code):
    msg_id = uuid.uuid4().hex
    ws.send(json.dumps({"header": {"msg_id": msg_id, "username": "", "session": uuid.uuid4().hex, "msg_type": "execute_request", "version": "5.3"},
                        "parent_header": {}, "metadata": {}, "content": {"code": code, "silent": False}, "channel": "shell"}))
    out = ""
    while True:
        m = json.loads(ws.recv())
        if m["parent_header"].get("msg_id") != msg_id:
            continue
        kind, c = m["msg_type"], m["content"]
        if kind == "stream":
            out += c["text"]
        elif kind == "error":
            out += f"ERROR {c['ename']}: {c['evalue']}\n"
        elif kind == "status" and c["execution_state"] == "idle":
            return out


try:
    # ---- 2. what a participant does first: load the content from GitHub
    out = run(f'''
import subprocess
r = subprocess.run("cd /workspace && curl -fsSL https://github.com/{REPO}/archive/refs/heads/main.tar.gz | tar xz --strip-components=2 --wildcards '*/workshop/*' && echo LOADED", shell=True, capture_output=True, text=True)
print(r.stdout.strip(), r.stderr.strip()[:200])
import os
print("FILES", sorted(f for f in ("agent.py", "{NOTEBOOK}", "sandbox/case.py", "sandbox/checks.py", "sandbox/policy.yaml") if os.path.exists("/workspace/" + f)))
''')
    check("the content loads from GitHub with the participants' one-liner", "LOADED" in out)
    files = re.search(r"FILES (\[.*\])", out)
    check("agent.py, the notebook and the sandbox/ folder are in /workspace", files is not None and len(ast.literal_eval(files.group(1))) == 5, files.group(1) if files else out[:150])

    # ---- 3. keys (names only) and the gateway
    out = run('''
import os, subprocess
print("ENV", {k: bool(os.environ.get(k)) for k in ("NEBIUS_API_KEY", "TAVILY_API_KEY")})
r = subprocess.run("openshell status | grep -E 'Status'; docker info --format '{{.ServerVersion}}'", shell=True, capture_output=True, text=True)
print("GATEWAY", r.stdout.replace(chr(10), " | "))
''')
    check("NEBIUS_API_KEY and TAVILY_API_KEY are set in the Devlab (values not shown)", "ENV {'NEBIUS_API_KEY': True, 'TAVILY_API_KEY': True}" in out, out.strip().splitlines()[0] if out.strip() else "")
    check("the OpenShell gateway is connected and Docker runs", "Connected" in out and re.search(r"\d+\.\d+\.\d+", out) is not None)

    # ---- 4. run the notebook inside the Devlab (detached; the result is read from the saved notebook)
    run(f'''
import subprocess
subprocess.Popen("cd /workspace && rm -f verify-run.ipynb verify-run.done && jupyter nbconvert --to notebook --execute --allow-errors "
                 "--ExecutePreprocessor.timeout=1500 --output verify-run.ipynb {NOTEBOOK} > verify-run.log 2>&1; touch verify-run.done",
                 shell=True, start_new_session=True)
print("started")
''')
    print("     running the notebook inside the Devlab (about 1-3 minutes) ...", flush=True)
    done = False
    for _ in range(240):
        try:
            api("/api/contents/verify-run.done")
            done = True
            break
        except urllib.error.HTTPError:
            time.sleep(5)
    check("the notebook finished", done)
    if done:
        nb = api("/api/contents/verify-run.ipynb?content=1")["content"]
        errors = [(o["ename"], o["evalue"][:100]) for c in nb["cells"] for o in c.get("outputs", []) if o.get("output_type") == "error"]
        text = "\n".join("".join(o.get("text", "")) if isinstance(o.get("text", ""), str) else "".join(o.get("text", []))
                         for c in nb["cells"] if c["cell_type"] == "code" for o in c.get("outputs", []))
        total = re.search(r"TOTAL: (\d+)/(\d+) checks passed", text)
        lead = re.search(r"^lead .*: (\d+) calls", text, re.M)
        worker = re.search(r"^worker .*: (\d+) calls in (\d+) sub-agents -> (\d+) candidates", text, re.M)
        findings = re.search(r"^findings: (\d+)", text, re.M)
        check("the notebook ran without errors", not errors, str(errors) if errors else "")
        check("all OpenShell checks passed", bool(total) and total.group(1) == total.group(2), total.group(0) if total else "no TOTAL line")
        check("the two-model agent ran in the sandbox", bool(lead and worker and findings) and int(findings.group(1)) > 0,
              f"lead {lead.group(1)} calls, workers {worker.group(1)} calls in {worker.group(2)} sub-agents, {worker.group(3)} candidates, {findings.group(1)} findings" if lead and worker and findings else "agent summary missing")
        if not all(results):                                    # what went wrong: the FAIL lines and the output of the agent cell
            print("\n--- details of the failed run")
            for line in [l for l in text.splitlines() if l.startswith("FAIL")][:8]:
                print("   ", line[:200])
            for c in nb["cells"]:
                if c["cell_type"] == "code" and "run_agent(" in "".join(c["source"]):
                    agent_out = "".join("".join(o.get("text", [])) if not isinstance(o.get("text", ""), str) else o["text"] for o in c.get("outputs", []))
                    print("    output of checks.run_agent():")
                    for line in agent_out.strip().splitlines()[-12:]:
                        print("      " + line[:200])
finally:
    try:
        run("import os\nfor f in ('verify-run.ipynb', 'verify-run.log', 'verify-run.done'):\n    os.path.exists('/workspace/' + f) and os.remove('/workspace/' + f)\n")
    except Exception:
        pass
    try:
        ws.close()
        api(f"/api/kernels/{kernel['id']}", "DELETE")
    except Exception:
        pass

print(f"\n{sum(results)}/{len(results)} verification checks passed")
sys.exit(0 if all(results) else 1)
