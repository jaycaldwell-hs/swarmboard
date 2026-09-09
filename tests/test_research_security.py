"""Deployment allowlists narrow the fixed provider boundary before secrets are read."""
from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from swarmboard.app import create_app
from swarmboard.credentials import redact, scrub_agent_settings, validate_agent_settings, validate_hosted_provider
from swarmboard.gateways import GatewayError, ModelGateway, OpenAICompatibleGateway
from swarmboard.models import Agent, Event, Post
from swarmboard.repository import Repository
from tests.test_engine_acceptance import ScriptedGateway, make_database


ALLOWED = {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}


@pytest.fixture(autouse=True)
def isolated_provider_environment(monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    for name in ("SWARMBOARD_ALLOWED_PROVIDER_HOSTS", "SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS",
                 "SWARMBOARD_AUTH_USERS", "SWARMBOARD_REQUIRE_AUTH", "SWARMBOARD_HOSTED", "RENDER"):
        monkeypatch.delenv(name, raising=False)


def test_allowlist_defaults_and_explicit_canonical_entries_keep_supported_providers(monkeypatch):
    validate_hosted_provider("openai_compatible", ALLOWED)
    validate_hosted_provider("codex", {})
    monkeypatch.setenv("SWARMBOARD_ALLOWED_PROVIDER_HOSTS", " unused.test, OPENROUTER.AI ")
    monkeypatch.setenv("SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", "OPENROUTER_API_KEY,UNUSED_KEY")
    validate_hosted_provider("openai_compatible", ALLOWED)
    monkeypatch.setenv("SWARMBOARD_ALLOWED_PROVIDER_HOSTS", "")
    monkeypatch.setenv("SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", "")
    validate_hosted_provider("codex", {})  # Server-controlled Codex credentials are separate.


@pytest.mark.asyncio
@pytest.mark.parametrize("setting,value", [
    ("SWARMBOARD_ALLOWED_PROVIDER_HOSTS", ""),
    ("SWARMBOARD_ALLOWED_PROVIDER_HOSTS", "somewhere-else.test"),
    ("SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", ""),
    ("SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", "ANOTHER_KEY"),
])
async def test_allowlists_reject_before_credential_resolution_or_network_client(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    original_getenv = os.getenv
    def guarded_getenv(name, default=None):
        if name == "OPENROUTER_API_KEY":
            pytest.fail("credential read before allowlist validation")
        return original_getenv(name, default)
    monkeypatch.setattr("swarmboard.gateways.os.getenv", guarded_getenv)
    monkeypatch.setattr("swarmboard.gateways.httpx.AsyncClient", lambda **kwargs: pytest.fail("network client created before allowlist validation"))
    participant = SimpleNamespace(provider="openai_compatible", model="qwen/qwen3.8-27b", settings=ALLOWED)
    with pytest.raises(GatewayError, match="outside SWARMBOARD_ALLOWED") as rejected:
        await ModelGateway().complete(participant, [])
    assert rejected.value.category == "configuration" and not rejected.value.retryable


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.1", "169.254.169.254", "[::1]", "[fd00::1]", "attacker.test"])
def test_explicit_extra_hosts_cannot_widen_fixed_openrouter_boundary(monkeypatch, host):
    monkeypatch.setenv("SWARMBOARD_ALLOWED_PROVIDER_HOSTS", "openrouter.ai," + host)
    with pytest.raises(ValueError, match="approved HTTPS provider"):
        validate_hosted_provider("openai_compatible", {**ALLOWED, "base_url": f"https://{host}/api/v1"})


def test_extra_credential_names_cannot_widen_fixed_openrouter_key_binding(monkeypatch):
    monkeypatch.setenv("SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", "OPENROUTER_API_KEY,SWARMBOARD_CODEX_API_KEY")
    with pytest.raises(ValueError, match="matching API key environment"):
        validate_hosted_provider("openai_compatible", {**ALLOWED, "api_key_env": "SWARMBOARD_CODEX_API_KEY"})


@pytest.mark.parametrize("nested", [
    {"sampling": {"apiKey": "literal-secret"}},
    {"custom": [{"headers": {"aUtHoRiZaTiOn": "Bearer literal-secret"}}]},
    {"custom": {"client_secret": "literal-secret"}},
    {"custom": [{"access_token": "literal-secret"}]},
    {"custom": {"credentials": {"username": "x", "password": "literal-secret"}}},
])
def test_recursive_credential_validation_rejects_literals_and_scrub_records_paths_only(nested):
    original = {**ALLOWED, **nested, "sampling": {**nested.get("sampling", {}),
        "provider": {"zdr": True, "require_parameters": True}, "temperature": 0.3}}
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="literal credentials"):
        validate_agent_settings(original)
    cleaned, removed = scrub_agent_settings(original)
    assert "literal-secret" not in str(cleaned) and "literal-secret" not in str(removed)
    assert removed and all(path.startswith("settings.") for path in removed)
    assert cleaned["sampling"]["provider"] == {"zdr": True, "require_parameters": True}
    assert cleaned["api_key_env"] == "OPENROUTER_API_KEY" and original == before


def test_nested_credential_environment_fields_still_require_environment_names():
    with pytest.raises(ValueError, match="environment-variable name"):
        validate_agent_settings({"nested": [{"api_key_env": "literal secret value"}]})


@pytest.mark.parametrize("version", [None, 0, -1, True, "2", 1.5])
def test_saved_persona_version_requires_a_positive_integer(version):
    with pytest.raises(ValueError, match="persona_version must be a positive integer"):
        validate_agent_settings({"persona_version": version})
    assert validate_agent_settings({"persona_version": 2})["persona_version"] == 2


@pytest.mark.asyncio
async def test_agent_api_rejects_allowlist_and_nested_secret_changes_without_persisting(tmp_path, monkeypatch):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'security-api.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            peer = (await client.get("/api/state")).json()["agents"][0]
            for variable, value in (("SWARMBOARD_ALLOWED_PROVIDER_HOSTS", "other.test"),
                                    ("SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", "OTHER_KEY")):
                monkeypatch.setenv(variable, value)
                response = await client.patch(f'/api/agents/{peer["id"]}', json={"persona": "Must not persist"})
                assert response.status_code == 422, response.text
                monkeypatch.delenv(variable)
            response = await client.patch(f'/api/agents/{peer["id"]}', json={
                "settings": {**peer["settings"], "sampling": {"extra": [{"Authorization": "hidden-secret"}]}}})
            assert response.status_code == 422, response.text
            current = next(agent for agent in (await client.get("/api/state")).json()["agents"] if agent["id"] == peer["id"])
            assert current == peer


@pytest.mark.asyncio
async def test_startup_disables_noncompliant_agents_and_scrubs_nested_credentials_without_changing_history(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'security-startup.db'}")
    with factory.begin() as session:
        repo = Repository(session)
        bad = repo.create_agent(handle="legacy_bad", provider="openai_compatible", model="qwen/qwen3.8-27b", persona="Original persona",
                                settings={**ALLOWED, "base_url": "http://169.254.169.254/metadata", "nested": {"api_key": "planted-nested-secret"}})
        safe = repo.create_agent(handle="ada", provider="codex", model="gpt-6-astra", persona="Ada")
        thread = repo.create_thread(title="Existing history")
        post = repo.create_agent_post(thread.id, bad.id, "An earlier saved contribution.").post
        bad_id, safe_id, post_id = bad.id, safe.id, post.id
        original_events = {event.id: copy.deepcopy(event.payload) for event in session.scalars(select(Event))}
    app = create_app(session_factory=factory, gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        with factory() as session:
            bad, safe = session.get(Agent, bad_id), session.get(Agent, safe_id)
            assert bad is not None and not bad.enabled and safe.enabled
            assert "planted-nested-secret" not in str(bad.settings)
            assert session.get(Post, post_id).body == "An earlier saved contribution."
            assert all(session.get(Event, eid).payload == payload for eid, payload in original_events.items())
            audits = list(session.scalars(select(Event).where(Event.agent_id == bad_id,
                Event.event_type.in_(["agent.credentials_scrubbed", "agent.provider_disabled"]))))
            assert {event.event_type for event in audits} == {"agent.credentials_scrubbed", "agent.provider_disabled"}
            assert "planted-nested-secret" not in str([event.payload for event in audits])
    db.dispose()


@pytest.mark.parametrize("field", ["openai_api_key", "openrouter_api_key", "OPENROUTER_API_KEY",
    "googleApiKey", "X_API_KEY", "secret_key", "api_token", "Proxy_Authorization", "id_token", "openai_access_token"])
def test_provider_credential_aliases_are_rejected_scrubbed_and_redacted(field):
    secret = "planted-rotated-opaque-credential"
    settings = {**ALLOWED, "nested": [{field: secret}], "sampling": {
        "max_tokens": 100, "provider": {"zdr": True, "require_parameters": True}}}
    with pytest.raises(ValueError, match="literal credentials"):
        validate_agent_settings(settings)
    clean, audit = scrub_agent_settings(settings)
    assert secret not in str(clean) and secret not in str(audit)
    visible = redact(settings)
    assert visible["nested"][0][field] == "[REDACTED]"
    assert visible["api_key_env"] == "OPENROUTER_API_KEY"
    assert visible["sampling"] == settings["sampling"]
    raw = ' \r\n{"' + field + '": "' + secret + '", "raw": "preserved"}\t'
    assert redact(raw) == raw.replace(secret, "[REDACTED]")


@pytest.mark.parametrize("secret", ["sk-or-v1-" + "a" * 64, "sk-proj-" + "P" * 90,
    "sk-svcacct-" + "S" * 50, "sk-" + "L" * 48, "AIza" + "G" * 35])
def test_recognizable_rotated_provider_keys_are_hidden_in_free_text(secret):
    # No environment contains this planted historical value.
    raw = "Prefix\r\n" + secret + "\tSuffix"
    assert redact({"raw_output": raw}) == {"raw_output": "Prefix\r\n[REDACTED]\tSuffix"}
    assert redact("gpt-6-astra OPENROUTER_API_KEY sk-short-example") == "gpt-6-astra OPENROUTER_API_KEY sk-short-example"


def test_unicode_escaped_active_password_is_hidden_without_reformatting_raw_json(monkeypatch):
    secret = "planted-café-credential"
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps({"researcher": secret}))
    raw = ' \r\n' + json.dumps({"body": secret}, ensure_ascii=True) + '\t'
    assert redact(raw) == raw.replace(json.dumps(secret, ensure_ascii=True)[1:-1], "[REDACTED]")
    ordinary = ' \r\n{"action": "pass", "body": null}\t'
    assert redact(ordinary) == ordinary


def test_allowlist_environment_values_do_not_hide_credential_variable_names(monkeypatch):
    monkeypatch.setenv("SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", "OPENROUTER_API_KEY")
    assert redact(ALLOWED) == ALLOWED


@pytest.mark.parametrize("field,value", [("model", "different-model"), ("messages", []),
    ("tools", [{"type": "function"}]), ("plugins", [{"id": "web"}]),
    ("stream", True), ("base_url", "https://attacker.test"), ("response_format", {"type": "text"}),
    ("extra_body", {"messages": []}), ("extra_headers", {"Host": "attacker.test"})])
@pytest.mark.asyncio
async def test_request_override_fields_fail_before_credential_read_or_network(monkeypatch, field, value):
    def fail_secret(name, default=None):
        if name == "OPENROUTER_API_KEY":
            pytest.fail("unsafe sampling reached credential resolution")
        return original_getenv(name, default)
    original_getenv = os.getenv
    monkeypatch.setattr("swarmboard.gateways.os.getenv", fail_secret)
    monkeypatch.setattr("swarmboard.gateways.httpx.AsyncClient", lambda **kwargs: pytest.fail("unsafe sampling reached network"))
    sampling = {field: value}
    with pytest.raises(ValueError, match="sampling"):
        validate_hosted_provider("openai_compatible", {**ALLOWED, "sampling": sampling})
    participant = SimpleNamespace(provider="openai_compatible", model="qwen/qwen3.8-27b", settings=ALLOWED)
    with pytest.raises(GatewayError, match="sampling"):
        await ModelGateway().complete(participant, [], sampling=sampling)
    with pytest.raises(GatewayError, match="sampling"):
        await OpenAICompatibleGateway("https://openrouter.ai/api/v1").complete(model="qwen/qwen3.8-27b", messages=[], sampling=sampling)


@pytest.mark.asyncio
async def test_malformed_upstream_token_usage_cannot_echo_a_credential():
    secret = "planted-opaque-upstream-credential"
    async def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"action":"pass"}'}}],
            "usage": {"prompt_tokens": secret}})
    gateway = OpenAICompatibleGateway("https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayError, match="invalid token usage") as error:
        await gateway.complete(model="qwen/qwen3.8-27b", messages=[{"role": "user", "content": "Test"}])
    assert secret not in str(error.value)
    assert error.value.raw_output == '{"action":"pass"}'
