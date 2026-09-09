"""Exercise the actual Linux image, login gate and mounted storage without model calls."""
from __future__ import annotations

import base64
import json
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    name = "swarmboard-container-smoke"
    volume = run("docker", "volume", "create")
    bundle = base64.b64encode(json.dumps({"AGENTS.md": "Read memory.md.\r\n", "memory.md": "A test persona.\n"}).encode()).decode()
    environment = ["-e", 'SWARMBOARD_AUTH_USERS={"tester":"fixture-password"}',
                   "-e", "OPENAI_API_KEY=fixture-not-a-real-key",
                   "-e", "SWARMBOARD_PERSONA_BUNDLE_B64=" + bundle]
    try:
        run("docker", "run", "--detach", "--name", name, "--publish", "127.0.0.1:18080:10000",
            "--mount", f"type=volume,source={volume},target=/var/data", *environment, "swarmboard:test")
        for attempt in range(60):
            try:
                with urlopen("http://127.0.0.1:18080/health", timeout=2) as response:
                    assert json.load(response) == {"status": "ok"}
                break
            except (URLError, HTTPError):
                if attempt == 59:
                    raise RuntimeError("Container did not become healthy")
                time.sleep(1)
        try:
            urlopen("http://127.0.0.1:18080/api/state", timeout=5)
            raise AssertionError("Unauthenticated API was exposed")
        except HTTPError as response:
            assert response.code == 401
        auth = "Basic " + base64.b64encode(b"tester:fixture-password").decode()
        with urlopen(Request("http://127.0.0.1:18080/api/state", headers={"Authorization": auth})) as response:
            state = json.load(response)
        ada = next(agent for agent in state["agents"] if agent["handle"] == "ada")
        assert ada["provider"] == "codex" and ada["model"] == "gpt-6-astra"
        assert ada["settings"]["persona_harness"]["instructions"] == "Read memory.md.\r\n"
        # Run under exactly the service account; --version alone as root would
        # miss a wrong home directory or unwritable Codex state directory.
        run("docker", "exec", "--user", "10001", name, "python", "-c",
            "import os,pathlib; assert os.getuid()==10001; p=pathlib.Path.home()/'.codex'; p.mkdir(exist_ok=True); assert os.access(p,os.W_OK)")
        version = run("docker", "exec", "--user", "10001", name, "codex", "--version")
        assert "0.153.4" in version
        print("Container passed: authentication, Ada initialization, persistent volume, unprivileged Codex.")
    except BaseException:
        subprocess.run(["docker", "logs", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "--force", name], stdout=subprocess.DEVNULL, check=False)
        subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
