"""Explicit open-weight peer roster, verified against OpenRouter on 2026-09-08."""
from copy import deepcopy

OPEN_MODELS = (
    "qwen/qwen3.8-27b",
    "moonshotai/kimi-k2.5",
    "deepseek/deepseek-v4-pro-0813",
    "z-ai/glm-5",
    "mistralai/mistral-small-2603",
    "meta-llama/llama-3.3-70b-instruct",
)
PEER_MODELS = dict(zip(("hiro", "yt", "raven", "da5id", "benway", "ng"), OPEN_MODELS))


def openrouter_settings(existing: dict, model: str) -> dict:
    settings = deepcopy(existing)
    for key in ("project", "location", "headers"):
        settings.pop(key, None)
    settings.update(base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
                    response_format="json_schema", timeout_seconds=180)
    # Remove knobs inherited from Ollama, xAI, Gemini or a different reasoning model.
    settings["sampling"] = {"max_tokens": 4096,
                            "provider": {"zdr": True, "require_parameters": True}}
    if not model.startswith("meta-llama/"):
        settings["sampling"]["reasoning"] = {"effort": "low", "exclude": True}
    return settings


def migrate_peers(repo):
    """Idempotently move saved peers to the explicit roster, with an audit event."""
    from .repository import InvalidStateError
    agents = repo.list_agents()
    peers = [a for a in agents if a.handle != "ada"]
    if len(peers) > len(OPEN_MODELS):
        raise InvalidStateError("more peers than verified unique models; extend OPEN_MODELS first")
    assigned = {a.id: PEER_MODELS[a.handle] for a in peers if a.handle in PEER_MODELS}
    remaining = [model for model in OPEN_MODELS if model not in assigned.values()]
    for agent in peers:
        if agent.id not in assigned:
            assigned[agent.id] = remaining.pop(0)
    changes = []
    for agent in peers:
        model = assigned[agent.id]
        settings = openrouter_settings(agent.settings, model)
        if agent.provider == "openai_compatible" and agent.model == model and agent.settings == settings:
            continue
        previous = {"provider": agent.provider, "model": agent.model}
        agent.provider, agent.model, agent.settings = "openai_compatible", model, settings
        repo.session.flush()
        payload = {"previous": previous, "provider": agent.provider, "model": model,
                   "credential_env": "OPENROUTER_API_KEY", "reason": "unique open-weight peer roster"}
        repo.add_event("agent.updated", agent_id=agent.id, actor_type="human", payload=payload)
        changes.append({"handle": agent.handle, **payload})
    return changes
