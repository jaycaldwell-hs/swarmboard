"""Manage this Render deployment without printing or committing its secrets."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import re

import httpx
from dotenv import dotenv_values

STATE = Path("private/render-deployment.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "validate", "create", "status", "logs", "deploy", "stage-import", "clear-import", "enable-preflight", "clear-preflight"])
    parser.add_argument("--owner")
    parser.add_argument("--commit")
    parser.add_argument("--snapshot", type=Path, default=Path("private/swarmboard-migration.db"))
    parser.add_argument("--check-id")
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
        def request(method, path, *, private=False, **kwargs):
            response = client.request(method, path, **kwargs)
            if not response.is_success:
                safe({"ok": False, "status": response.status_code,
                      "detail": "Private request failed; response omitted" if private else response.text[:2000]})
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
                logs = result.get("logs", [])
                safe({"logs": [{"timestamp": item.get("timestamp"), "message": item.get("message", "")[:1500]}
                               for item in logs[-40:]]})
            elif args.action == "stage-import":
                # Snapshot must come from SQLite's online backup API, never a
                # copy of a live main file with an uncheckpointed WAL.
                from swarmboard.migration import _MAX_ENCODED_BUNDLE, _MAX_RAW_BUNDLE, _CORE_TABLES
                import sqlite3
                from contextlib import closing
                source = args.snapshot.resolve()
                if not source.is_file() or not 0 < source.stat().st_size <= _MAX_RAW_BUNDLE:
                    parser.error("Snapshot is missing or exceeds the import limit")
                if any(Path(str(source) + suffix).exists() and Path(str(source) + suffix).stat().st_size
                       for suffix in ("-wal", "-journal")):
                    parser.error("Create a standalone SQLite backup before staging")
                with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                    if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        parser.error("Snapshot failed integrity validation")
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not _CORE_TABLES.issubset(tables):
                        parser.error("Snapshot is not a Swarmboard database")
                raw = source.read_bytes()
                encoded = base64.b64encode(lzma.compress(raw, preset=6)).decode("ascii")
                if len(encoded) > _MAX_ENCODED_BUNDLE:
                    parser.error("Compressed snapshot exceeds the import limit")
                # Replace only this helper's previous temporary upload parts.
                existing = request("GET", f"/services/{service_id}/secret-files", private=True, params={"limit": 100})
                for item in existing:
                    name = item["secretFile"]["name"]
                    if re.fullmatch(r"swarmboard-import-(?:[0-9]{3}\.b64)", name) or name == "swarmboard-import.json":
                        request("DELETE", f"/services/{service_id}/secret-files/{name}", private=True)
                parts = []
                chunk_size = 256 * 1024
                for index, offset in enumerate(range(0, len(encoded), chunk_size)):
                    name = f"swarmboard-import-{index:03d}.b64"
                    request("PUT", f"/services/{service_id}/secret-files/{name}", private=True,
                            json={"content": encoded[offset:offset + chunk_size]})
                    parts.append(name)
                    safe({"uploaded_part": index + 1})
                manifest = {"version": 1, "compression": "xz", "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw), "parts": parts}
                request("PUT", f"/services/{service_id}/secret-files/swarmboard-import.json", private=True,
                        json={"content": json.dumps(manifest)})
                request("PUT", f"/services/{service_id}/env-vars/SWARMBOARD_IMPORT_BUNDLE_FILE", private=True,
                        json={"value": "/etc/secrets/swarmboard-import.json"})
                safe({"staged": True, "size_bytes": len(raw), "sha256": manifest["sha256"], "parts": len(parts),
                      "next": "Deploy the import-capable commit to apply this snapshot once"})
            elif args.action == "clear-import":
                request("DELETE", f"/services/{service_id}/env-vars/SWARMBOARD_IMPORT_BUNDLE_FILE", private=True)
                safe({"import_disabled": True, "next": "Redeploy to apply; imported history and backups remain on disk"})
            elif args.action == "enable-preflight":
                if not args.check_id:
                    parser.error("--check-id is required; reuse it to avoid repeating a successful check")
                request("PUT", f"/services/{service_id}/env-vars/SWARMBOARD_CODEX_PREFLIGHT_ID", private=True,
                        json={"value": args.check_id})
                safe({"preflight_enabled": True, "next": "Redeploy to run the check once without creating board history"})
            elif args.action == "clear-preflight":
                request("DELETE", f"/services/{service_id}/env-vars/SWARMBOARD_CODEX_PREFLIGHT_ID", private=True)
                safe({"preflight_disabled": True})
            elif args.action == "deploy":
                body = {"clearCache": "do_not_clear"}
                if args.commit:
                    body["commitId"] = args.commit
                result = request("POST", f"/services/{service_id}/deploys", json=body)
                safe({k: result.get(k) for k in ("id", "status", "commit")})


if __name__ == "__main__":
    main()
