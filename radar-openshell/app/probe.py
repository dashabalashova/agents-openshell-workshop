"""One HTTP request from inside the sandbox; prints REACHED or DENIED (used by the checks).
usage: probe.py METHOD URL [ENV_VAR]   the Authorization header carries $ENV_VAR (a placeholder inside the sandbox)."""
import os, sys, urllib.request, urllib.error

method, url = sys.argv[1], sys.argv[2]
var = sys.argv[3] if len(sys.argv) > 3 else "NEBIUS_API_KEY"
req = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None,
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get(var, "")})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print("REACHED", r.status)
except urllib.error.HTTPError as e:
    body = e.read(400).decode("utf-8", "replace")
    if "credential_endpoint_mismatch" in body:
        print("DENIED credential-binding")
    elif "policy_denied" in body:
        print("DENIED policy")
    else:
        print("REACHED", e.code)
except Exception as e:
    print("DENIED connection", type(e).__name__)
