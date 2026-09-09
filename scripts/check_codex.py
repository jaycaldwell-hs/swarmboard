"""One bounded live Codex/API check; never prints keys, prompts or diagnostics."""
from __future__ import annotations

import asyncio
import json
import os

from dotenv import load_dotenv

from swarmboard.codex_gateway import CodexGateway
from swarmboard.gateways import GatewayError, action_json_schema


async def check() -> int:
    load_dotenv()
    os.environ["SWARMBOARD_CODEX_AUTH"] = "api_key"
    gateway = CodexGateway(timeout_seconds=90)
    try:
        result = await gateway.complete(
            model="gpt-6-astra", sampling={"reasoning_effort": "medium"},
            messages=[
                {"role": "system", "content": "Return one JSON pass action matching this schema: " + json.dumps(action_json_schema())},
                {"role": "user", "content": "Connection check. Pass without posting."},
            ],
        )
    except GatewayError as exc:
        print(json.dumps({"ok": False, "category": exc.category, "detail": str(exc)}))
        return 1
    print(json.dumps({"ok": result.action.action == "pass", "model": result.model,
                      "action": result.action.action, "total_tokens": result.usage.total_tokens,
                      "runtime": result.response_metadata}))
    return 0 if result.action.action == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
