"""Manage this Render deployment without printing or committing its secrets."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
from dotenv import dotenv_values

STATE = Path("private/render-deployment.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "validate", "create", "status", "logs", "deploy"])
    parser.add_argument("--owner")
    parser.add_argument("--commit")
    args = parser.parse_args()
    local = {**dotenv_values(".env"), **os.environ}
    token = local.get("RENDER_API_KEY")
    if not token:
        parser.error("RENDER_API_KEY must be set in .env or the environment")
    secrets = dotenv_values("private/render.env") if Path("private/render.env").exists() else {}
    redactions = [str(v) for k, v in {**local, **secrets}.items()
                  if v and any(label in k for label in ("KEY", "TOKEN", "SECRET", "AUTH", "BUNDLE"))]
    if secrets.get("SWARMBOARD_AUTH_USERS"):
        redactions.extend(json.loads(secrets["SWARMBOARD_AUTH_USERS"]).values())

    def safe(value):
        output = json.dumps(value, ensure_ascii=False)
        for secret in redactions:
            output = output.replace(secret, "[redacted]")
        print(output)

    with httpx.Client(base_url="https://api.render.com/v1", timeout=60,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if not response.is_success:
                safe({"ok": False, "status": response.status_code, "detail": response.text[:2000]})
                raise SystemExit(1)
            return response.json() if response.content else {}

        if args.action == "inspect":
            owners = request("GET", "/owners", params={"limit": 100})
            safe({"workspaces": [{k: item["owner"].get(k) for k in ("id", "name", "type")} for item in owners]})
            services = request("GET", "/services", params={"limit": 100})
            safe({"services": [{k: item["service"].get(k) for k in ("id", "name", "repo", "suspended")} for item in services]})
        elif args.action == "validate":
            if not args.owner:
                parser.error("--owner is required")
            with Path("render.yaml").open("rb") as file:
                result = request("POST", "/blueprints/validate", data={"ownerId": args.owner}, files={"file": ("render.yaml", file, "application/yaml")})
            safe(result)
            if not result.get("valid"):
                raise SystemExit(1)
        elif args.action == "create":
            if not args.owner or STATE.exists():
                parser.error("--owner is required and an existing deployment must not be overwritten")
            required = ("SWARMBOARD_AUTH_USERS", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "SWARMBOARD_PERSONA_BUNDLE_B64")
            if any(not secrets.get(key) for key in required):
                parser.error("Prepare all four secrets in private/render.env first")
            values = {key: secrets[key] for key in required}
            values.update(SWARMBOARD_HOSTED="1", SWARMBOARD_REQUIRE_AUTH="1", SWARMBOARD_CODEX_AUTH="api_key",
                          SWARMBOARD_DB_PATH="/var/data/swarmboard.db", SWARMBOARD_PERSONA_DIR="/var/data/persona")
            body = {"type": "web_service", "name": "swarmboard", "ownerId": args.owner,
                    "repo": "https://github.com/jaycaldwell-hs/swarmboard", "branch": "main", "autoDeploy": "yes",
                    "envVars": [{"key": key, "value": value} for key, value in values.items()],
                    "serviceDetails": {"runtime": "docker", "plan": "1c-2g", "region": "ohio", "numInstances": 1,
                        "healthCheckPath": "/health", "envSpecificDetails": {"dockerfilePath": "./Dockerfile", "dockerContext": "."},
                        "disk": {"name": "swarmboard-data", "mountPath": "/var/data", "sizeGB": 5}}}
            result = request("POST", "/services", json=body)
            service = result.get("service", result)
            state = {"service_id": service["id"], "owner_id": args.owner,
                     "deploy_id": result.get("deployId"), "url": service.get("serviceDetails", {}).get("url")}
            STATE.parent.mkdir(exist_ok=True, mode=0o700)
            fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(state, file, indent=2)
            safe(state)
        else:
            state = json.loads(STATE.read_text())
            service_id = state["service_id"]
            if args.action == "status":
                service = request("GET", f"/services/{service_id}")
                safe({"service_id": service_id, "url": service.get("serviceDetails", {}).get("url"), "suspended": service.get("suspended")})
                deploys = request("GET", f"/services/{service_id}/deploys", params={"limit": 3})
                safe({"deploys": [{k: item.get("deploy", item).get(k) for k in ("id", "status", "commit", "createdAt", "finishedAt")} for item in deploys]})
            elif args.action == "logs":
                result = request("GET", "/logs", params={"ownerId": state["owner_id"], "resource": service_id, "limit": 100, "direction": "backward"})
                safe(result)
            elif args.action == "deploy":
                body = {"clearCache": "do_not_clear"}
                if args.commit:
                    body["commitId"] = args.commit
                result = request("POST", f"/services/{service_id}/deploys", json=body)
                safe({k: result.get(k) for k in ("id", "status", "commit")})


if __name__ == "__main__":
    main()
