#!/usr/bin/env python3
"""Prepared setup of the radar-openshell case: providers, sandbox image, policy and the sandbox itself.

    python case.py up      (re)create everything from the files in this folder; idempotent
    python case.py down    delete the sandbox, the providers and their profiles
    python case.py status  show what exists

Files used: policy.yaml, profiles/*.yaml, Dockerfile, app/main.py, app/probe.py and ../agent.py (the radar agent,
copied into the image as it is). The real keys are read from the environment (or /workspace/.env) and handed to
the OpenShell gateway as provider credentials (--credential NAME, no value on any command line); the sandbox
only gets placeholders. The notebook radar-openshell.ipynb imports this module and calls up() / down().
"""
import argparse, os, re, shlex, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT = HERE.parent / "agent.py"               # the same file the notebook radar.ipynb imports
SANDBOX = "radar-case"
IMAGE = "radar-sandbox:local"
KEYS = ("NEBIUS_API_KEY", "TAVILY_API_KEY")
PROFILES = ("nebius-token-factory", "tavily-search")
PROVIDERS = (("radar-nebius", "nebius-token-factory", "NEBIUS_API_KEY"),      # (provider, profile, variable)
             ("radar-tavily", "tavily-search", "TAVILY_API_KEY"))


def clean(text):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def sh(cmd, timeout=900):
    """Run a shell command; returns (exit code, output without colors)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, executable="/bin/bash")
    return r.returncode, clean(r.stdout + r.stderr).strip()


def run_in(*args, timeout=300):
    """Run a command inside the sandbox (no shell: the arguments are passed as they are)."""
    return sh("openshell sandbox exec --name %s --no-tty --no-login-shell -- %s" % (SANDBOX, shlex.join(args)), timeout)


def step(message):
    print(f"==> {message}", flush=True)


def fail(message):
    raise SystemExit(f"ERROR: {message}")


def load_env_files():
    """Optional fallback: /workspace/.env or ./.env. Real environment variables always win."""
    for env_file in (Path("/workspace/.env"), Path(".env")):
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = (s.strip() for s in line.split("=", 1))
                    key, value = key.removeprefix("export ").strip(), value.strip("\"'")
                    if key in KEYS and value and not os.environ.get(key):
                        os.environ[key] = value


def delete_everything():
    """Sandbox first (it keeps its providers in use), then providers, then profiles."""
    sh(f"openshell sandbox delete {SANDBOX}")
    for _ in range(30):
        if SANDBOX not in sh("openshell sandbox list")[1]:
            break
        time.sleep(2)
    for name, _, _ in PROVIDERS:
        sh(f"openshell provider delete {name}")
    for profile in PROFILES:
        sh(f"openshell profile delete {profile}")


def up():
    load_env_files()
    missing = [k for k in KEYS if not os.environ.get(k)]
    if missing:
        fail(f"missing {missing}: set them as environment variables (create-devlab.sh) or in /workspace/.env")
    for path in (HERE / "policy.yaml", HERE / "Dockerfile", HERE / "app" / "main.py", HERE / "app" / "probe.py", AGENT):
        if not path.is_file():
            fail(f"{path} is missing: upload the whole notebooks folder (agent.py and radar-openshell/)")

    step("gateway")
    rc, out = sh("openshell status")
    if rc != 0 or "Connected" not in out:
        fail("the OpenShell gateway is not connected:\n" + out)

    step("clean up an earlier run")
    delete_everything()

    step("provider profiles")
    for profile in PROFILES:
        path = HERE / "profiles" / f"{profile}.yaml"
        rc, out = sh(f"openshell profile lint --file {path} && openshell profile import --file {path}")
        if rc != 0:
            fail(f"profile {profile}:\n{out}")
        print("   ", out.splitlines()[-1])

    step("providers (the keys are read from the environment, not from the command line)")
    for name, profile, variable in PROVIDERS:
        rc, out = sh(f"openshell provider create --name {name} --type {profile} --credential {variable}")
        if rc != 0:
            fail(f"provider {name}:\n{out}")
        print("   ", out.splitlines()[-1])

    step("sandbox image " + IMAGE)
    with tempfile.TemporaryDirectory() as tmp:
        context = Path(tmp)
        shutil.copy(HERE / "Dockerfile", context / "Dockerfile")
        shutil.copytree(HERE / "app", context / "app")
        shutil.copy(AGENT, context / "app" / "agent.py")
        rc, out = sh(f"docker build -q -t {IMAGE} {context}")
    if rc != 0:
        fail("docker build failed:\n" + out[-1500:])
    print("   ", out.splitlines()[-1])

    step("sandbox " + SANDBOX + " with " + str(HERE / "policy.yaml"))
    rc, out = sh(f"openshell sandbox create --name {SANDBOX} --from {IMAGE} --policy {HERE / 'policy.yaml'} "
                 "--provider radar-nebius --provider radar-tavily --no-auto-providers "
                 "--detach --no-tty -- /usr/bin/sleep infinity")
    if rc != 0:
        fail("sandbox was not created:\n" + out[-1500:])
    rc, out = run_in("id")
    print("   ", out)
    if rc != 0:
        fail("the sandbox does not run commands: " + out)
    print("ready: sandbox", SANDBOX)


def down():
    step("deleting the sandbox, the providers and their profiles")
    delete_everything()
    print(status_text())


def status_text():
    return "\n".join(f"{title}:\n{sh(cmd)[1]}" for title, cmd in
                     (("sandboxes", "openshell sandbox list"), ("providers", "openshell provider list"),
                      ("profiles", "openshell profile list")))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("up", "down", "status"))
    args = parser.parse_args()
    {"up": up, "down": down, "status": lambda: print(status_text())}[args.command]()


if __name__ == "__main__":
    main()
