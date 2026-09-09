"""Prepare a private env file for Render from local credentials and persona files."""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--persona", type=Path, default=Path("persona 1"))
    parser.add_argument("--output", type=Path, default=Path("private/render.env"))
    parser.add_argument("--auth-users-file", type=Path, help="Private JSON mapping of usernames to passwords; otherwise prompt")
    args = parser.parse_args()
    local = {**dotenv_values(args.env), **os.environ}
    key = local.get("SWARMBOARD_CODEX_API_KEY") or local.get("OPENAI_API_KEY")
    if not key:
        parser.error("OPENAI_API_KEY or SWARMBOARD_CODEX_API_KEY must be configured")
    if args.auth_users_file:
        users = json.loads(args.auth_users_file.read_text())
    else:
        users = {name: getpass.getpass(f"Password for {name}: ") for name in ("admin", "cat")}
    if not isinstance(users, dict) or not users or any(not isinstance(v, str) or not v for v in users.values()):
        parser.error("Every account must have a password")
    bundle = {name: (args.persona / name).read_bytes().decode("utf-8") for name in ("AGENTS.md", "memory.md")}
    values = {
        "SWARMBOARD_AUTH_USERS": json.dumps(users, ensure_ascii=False, separators=(",", ":")),
        "OPENAI_API_KEY": key,
        "OPENROUTER_API_KEY": local.get("OPENROUTER_API_KEY", ""),
        "SWARMBOARD_PERSONA_BUNDLE_B64": base64.b64encode(json.dumps(bundle, ensure_ascii=False).encode()).decode(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        for name, value in values.items():
            stream.write(f"{name}={json.dumps(value, ensure_ascii=False)}\n")
    print(f"Prepared {len(values)} private deployment values in {args.output}. Do not commit this file.")


if __name__ == "__main__":
    main()
