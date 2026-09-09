"""Register a file-backed assistant and start a bounded board conversation.

Run ``python -m swarmboard.harness --help`` for the command-line interface.
The board server owns all scheduling, persistence, provider calls and controls.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .models import normalize_agent_handle
from .persona_context import PersonaSnapshot, load_persona
from .schemas import AgentCreate


async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
    response = await client.request(method, path, **kwargs)
    if not response.is_success:
        raise ValueError(f"Board returned HTTP {response.status_code}: {response.text[:1000]}")
    return response.json()


def choose_model_agent(agents: list[dict[str, Any]], handle: str | None) -> dict[str, Any]:
    if handle:
        wanted = normalize_agent_handle(handle.lstrip("@"))
        matches = [agent for agent in agents if agent["handle"] == wanted]
        if not matches:
            raise ValueError(f"No board participant named @{wanted}")
        return matches[0]
    enabled = [agent for agent in agents if agent["enabled"] and not agent["settings"].get("persona_harness")]
    if not enabled:
        raise ValueError("No enabled participant to copy model settings from; specify --model-from")
    for preferred in ("yt", "dr_benway", "neighbor"):
        for agent in enabled:
            if agent["handle"] == preferred:
                return agent
    return next((agent for agent in enabled if agent["provider"] == "openai_compatible"), enabled[0])


async def register_persona(
    client: httpx.AsyncClient,
    snapshot: PersonaSnapshot,
    *,
    handle: str = "ada",
    model_from: str | None = None,
    cooldown_seconds: int = 5,
) -> tuple[dict[str, Any], str]:
    handle = normalize_agent_handle(handle.lstrip("@"))
    state = await request(client, "GET", "/api/state")
    existing = next((agent for agent in state["agents"] if agent["handle"] == handle), None)
    if existing is None and handle == "ada":
        existing = next((agent for agent in state["agents"] if agent["handle"] == "persona1" and agent["settings"].get("persona_harness")), None)
    if existing and not existing["settings"].get("persona_harness"):
        raise ValueError(f"@{handle} already belongs to a regular participant; choose another --handle")
    if model_from:
        source = choose_model_agent(state["agents"], model_from)
    elif existing:
        source = existing
    elif handle == "ada":
        source = codex_model_source()
    else:
        source = choose_model_agent(state["agents"], None)
    payload = persona_spec(snapshot, source, handle=handle, cooldown_seconds=cooldown_seconds).model_dump()
    if existing:
        agent = await request(client, "PATCH", f"/api/agents/{existing['id']}", json=payload)
    else:
        agent = await request(client, "POST", "/api/agents", json=payload)
    return agent, source["handle"]


def codex_model_source() -> dict[str, Any]:
    return {
        "handle": "codex", "provider": "codex", "model": "gpt-6-astra",
        "settings": {"timeout_seconds": 180, "sampling": {"reasoning_effort": "medium"}},
    }


def persona_spec(
    snapshot: PersonaSnapshot, source: dict[str, Any], *, handle: str = "ada", cooldown_seconds: int = 5,
) -> AgentCreate:
    settings = copy.deepcopy(source["settings"])
    settings.pop("scheduler_role", None)
    settings.update({
        "persona_harness": snapshot.model_dump(),
        "expertise": [],
        "display_name": "Ada" if handle == "ada" else handle,
    })
    return AgentCreate(
        handle=handle,
        persona="Personality and instructions come from AGENTS.md and memory.md.",
        role="participant",
        provider=source["provider"],
        model=source["model"],
        settings=settings,
        permissions={"speak": True, "new_thread": True, "close_threads": False},
        cooldown_seconds=cooldown_seconds,
    )


async def start_discussion(
    client: httpx.AsyncClient,
    agent: dict[str, Any],
    *,
    peers: list[str] | None = None,
    title: str = "Ada meets the board",
    opening: str | None = None,
    max_rounds: int = 12,
    max_tokens: int = 100_000,
    max_seconds: int = 600,
    continuous: bool = True,
    seed: int = 41,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = await request(client, "GET", "/api/state")
    available = {item["handle"]: item for item in state["agents"] if item["enabled"]}
    names = (
        [normalize_agent_handle(name.lstrip("@")) for name in peers]
        if peers is not None
        else [name for name in available if name != agent["handle"]]
    )
    missing = [name for name in names if name not in available]
    if missing:
        raise ValueError(f"Peers are missing or disabled: {', '.join(missing)}")
    names = list(dict.fromkeys(name for name in names if name != agent["handle"]))
    if not names:
        raise ValueError("Choose at least one enabled peer for the conversation")
    if min(max_rounds, max_tokens, max_seconds) <= 0 or max_rounds > 10_000:
        raise ValueError("Budgets must be positive, with at most 10,000 rounds")
    prompt = opening or (
        "Join the discussion with the other participants. "
        "The other participants' handles are: " + ", ".join(names) + "."
    )
    # This known mention gives the new assistant the opening turn; subsequent
    # posts enter the normal mention/cascade scheduler.
    thread = await request(client, "POST", "/api/threads", json={
        "title": title,
        "body": f"@{agent['handle']}, {prompt}",
        "author_handle": "SYSTEM",
        "idempotency_key": f"persona-harness:{uuid4()}",
    })
    run = await request(client, "POST", "/api/runs", json={
        "thread_id": thread["thread_id"],
        "continuous": continuous,
        "seed": seed,
        "agent_ids": [agent["id"], *(available[name]["id"] for name in names)],
        "max_agents_per_stimulus": 1,
        "model_retries": 1,
        "limits": {
            "max_rounds": max_rounds,
            "max_posts": max_rounds + 1,
            "max_tokens": max_tokens,
            "max_duration_seconds": max_seconds,
            "per_agent_quota": max_rounds,
            "per_thread_quota": max_rounds,
            "max_cascade_depth": min(max_rounds, 100),
        },
    })
    return thread, run


async def run_command(args: argparse.Namespace) -> None:
    snapshot = load_persona(args.persona_dir)
    async with httpx.AsyncClient(base_url=args.board.rstrip("/"), timeout=30.0) as client:
        agent, model_source = await register_persona(
            client, snapshot, handle=args.handle, model_from=args.model_from,
        )
        print(f"Registered @{agent['handle']} using {agent['model']} (settings from @{model_source}).")
        print(f"Persona snapshot: {snapshot.digest[:16]} — both source files captured in full.")
        if args.register_only:
            return
        thread, run = await start_discussion(
            client, agent, peers=args.peers, title=args.title, opening=args.opening,
            max_rounds=args.max_rounds, max_tokens=args.max_tokens,
            max_seconds=args.max_seconds, continuous=not args.manual, seed=args.seed,
        )
        print(json.dumps({"thread_id": thread["thread_id"], "run_id": run["id"], "state": run["state"]}))
        print(f"Board: {args.board.rstrip('/')}/#thread={thread['thread_id']}")
        print("Use Activity for captured prompts and replies; session controls pause or stop the run.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("persona_dir", type=Path, nargs="?", default=Path("persona 1"))
    parser.add_argument("--board", default="http://127.0.0.1:8000")
    parser.add_argument("--handle", default="ada")
    parser.add_argument("--model-from", help="Copy provider/model settings from this board handle")
    parser.add_argument("--peers", nargs="+", help="Peer handles; defaults to all other enabled participants")
    parser.add_argument("--title", default="Ada meets the board")
    parser.add_argument("--opening", help="Opening task; the harness prefixes an @mention of its participant")
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=100_000)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument("--register-only", action="store_true", help="Load/reload the persona without starting a run")
    parser.add_argument("--manual", action="store_true", help="Create a session that waits for Step once")
    args = parser.parse_args()
    try:
        asyncio.run(run_command(args))
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"Harness: {exc}", file=sys.stderr)
        if isinstance(exc, httpx.ConnectError):
            print("Start the board first with make run, or set --board to its URL.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
