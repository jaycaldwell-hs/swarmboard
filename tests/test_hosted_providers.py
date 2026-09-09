from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from swarmboard.credentials import validate_hosted_provider
from swarmboard.gateways import GatewayError, ModelGateway, OpenAICompatibleGateway


@pytest.fixture(autouse=True)
def isolated_hosted_env(monkeypatch):
    for name in ("SWARMBOARD_HOSTED", "RENDER", "SWARMBOARD_REQUIRE_AUTH", "SWARMBOARD_AUTH_USERS", "OPENAI_COMPAT_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("host,prefix,key", [
    ("openrouter.ai", "/api", "OPENROUTER_API_KEY"),
])
@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/", "/v1/chat/completions", "/v1/chat/completions/"])
def test_hosted_allows_only_expected_provider_paths(monkeypatch, host, prefix, key, suffix):
    monkeypatch.setenv("SWARMBOARD_HOSTED", "1")
    validate_hosted_provider("openai_compatible", {"base_url": f"https://{host}{prefix}{suffix}", "api_key_env": key})


@pytest.mark.parametrize("flag,value", [
    ("SWARMBOARD_HOSTED", "1"), ("RENDER", "true"),
    ("SWARMBOARD_REQUIRE_AUTH", "1"), ("SWARMBOARD_AUTH_USERS", '{"researcher":"test-password"}'),
])
def test_hosted_activation_and_normalized_default_tls_origin(monkeypatch, flag, value):
    monkeypatch.setenv(flag, value)
    validate_hosted_provider("codex", {})
    validate_hosted_provider("openai_compatible", {"base_url": "https://OPENROUTER.AI:443/api/v1/", "api_key_env": "OPENROUTER_API_KEY"})
    with pytest.raises(ValueError, match="Codex/Astra or OpenRouter"):
        validate_hosted_provider("ollama", {})


@pytest.mark.parametrize("base_url", [
    "https://api.openai.com/v1", "https://api.x.ai/v1",
    "https://attacker.test/v1", "http://openrouter.ai/api/v1", "https://openrouter.ai:8443/v1",
    "http://127.0.0.1/v1", "http://localhost/v1", "http://10.0.0.1/v1",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/v1", "http://[fd00::1]/v1",
    "https://openrouter.ai.:443/v1", "https://openrouter.ai.attacker.test/v1",
    "https://openrouter.ai@attacker.test/v1", "https://attacker@openrouter.ai/api/v1",
    "https://openrouter.ai/api/v1?secret=1", "https://openrouter.ai/api/v1?", "https://openrouter.ai/api/v1#",
    "https://openrouter.ai/api/v1/../anything", "https://openrouter.ai/%76%31", "https://openrouter.ai//v1",
    "https://openrouter.ai/api/v1//", "https://openrouter.ai/api/v1/chat", "https://openrouter.ai/v1",
    "https://openrouter.ai\\@attacker.test/v1", " https://openrouter.ai/api/v1", "https://openrouter.ai\n/v1",
    "https://[invalid", "file:///var/data/swarmboard.db", 123,
])
@pytest.mark.asyncio
async def test_imported_bad_provider_urls_fail_before_any_client_creation(monkeypatch, base_url):
    monkeypatch.setenv("SWARMBOARD_HOSTED", "1")
    monkeypatch.setattr("swarmboard.gateways.httpx.AsyncClient", lambda **kwargs: pytest.fail("network client was created"))
    agent = SimpleNamespace(provider="openai_compatible", model="test", settings={
        "base_url": base_url, "api_key_env": "OPENROUTER_API_KEY",
    })
    with pytest.raises(GatewayError, match="approved HTTPS provider") as exc:
        await ModelGateway().complete(agent, [{"role": "user", "content": "Do not send."}])
    assert exc.value.category == "configuration" and not exc.value.retryable


@pytest.mark.parametrize("key", ["SWARMBOARD_AUTH_USERS", "SWARMBOARD_PERSONA_BUNDLE_B64", "RENDER_API_KEY", "SWARMBOARD_CODEX_API_KEY",
                                 "OPENAI_API_KEY", "XAI_API_KEY", "CUSTOM_API_KEY", "", None])
@pytest.mark.asyncio
async def test_arbitrary_secret_names_fail_before_environment_resolution(monkeypatch, key):
    import os

    monkeypatch.setenv("SWARMBOARD_HOSTED", "1")
    original_getenv = os.getenv

    def guarded_getenv(name, default=None):
        if key and name == key:
            pytest.fail("forbidden credential was read")
        return original_getenv(name, default)

    monkeypatch.setattr("swarmboard.gateways.os.getenv", guarded_getenv)
    monkeypatch.setattr("swarmboard.gateways.httpx.AsyncClient", lambda **kwargs: pytest.fail("network client was created"))
    agent = SimpleNamespace(provider="openai_compatible", model="test", settings={
        "base_url": "https://openrouter.ai/api/v1", "api_key_env": key,
    })
    with pytest.raises(GatewayError, match="matching API key environment variable"):
        await ModelGateway().complete(agent, [{"role": "user", "content": "Do not send."}])


@pytest.mark.parametrize("provider", ["ollama", "vertex", "vertex_gemini", "gemini_vertex", "openai", "compatible"])
@pytest.mark.asyncio
async def test_hosted_rejects_other_gateway_types(monkeypatch, provider):
    monkeypatch.setenv("SWARMBOARD_HOSTED", "1")
    monkeypatch.setattr("swarmboard.gateways.httpx.AsyncClient", lambda **kwargs: pytest.fail("network client was created"))
    with pytest.raises(GatewayError, match="Codex/Astra or OpenRouter"):
        await ModelGateway().complete(SimpleNamespace(provider=provider, model="test", settings={}), [])


def test_hosted_validates_default_base_url_and_imported_literal_credentials(monkeypatch):
    monkeypatch.setenv("SWARMBOARD_HOSTED", "1")
    validate_hosted_provider("openai_compatible", {"api_key_env": "OPENROUTER_API_KEY"})
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://attacker.test")
    with pytest.raises(ValueError, match="approved HTTPS provider"):
        validate_hosted_provider("openai_compatible", {"api_key_env": "OPENROUTER_API_KEY"})
    with pytest.raises(ValueError, match="literal credentials"):
        validate_hosted_provider("openai_compatible", {"base_url": "https://openrouter.ai", "api_key_env": "OPENROUTER_API_KEY",
                                                       "headers": {"Authorization": "Bearer imported-secret"}})


@pytest.mark.asyncio
async def test_local_boards_reject_custom_providers_and_environment_names(monkeypatch):
    monkeypatch.setenv("CUSTOM_PROVIDER_KEY", "local-test-key")
    seen = []

    async def capture(gateway, **kwargs):
        seen.append((gateway.base_url, gateway.headers["Authorization"]))
        return "local-result"

    monkeypatch.setattr(OpenAICompatibleGateway, "complete", capture)
    agent = SimpleNamespace(provider="openai_compatible", model="test", settings={
        "base_url": "http://custom.test:8080", "api_key_env": "CUSTOM_PROVIDER_KEY",
    })
    with pytest.raises(GatewayError, match="approved HTTPS provider"):
        await ModelGateway().complete(agent, [])
    assert seen == []
    with pytest.raises(ValueError, match="Codex/Astra or OpenRouter"):
        validate_hosted_provider("ollama", {"base_url": "http://localhost:11434"})


@pytest.mark.parametrize("auth_flag", ["SWARMBOARD_REQUIRE_AUTH", "SWARMBOARD_AUTH_USERS"])
@pytest.mark.asyncio
async def test_authenticated_gateway_rejects_arbitrary_secret_before_reading_it(monkeypatch, auth_flag):
    import os

    monkeypatch.setenv(auth_flag, "1" if auth_flag == "SWARMBOARD_REQUIRE_AUTH" else '{"researcher":"test-password"}')
    # Explicitly disabling hosted flags must not weaken a board with login configured.
    monkeypatch.setenv("SWARMBOARD_HOSTED", "0")
    monkeypatch.setenv("RENDER", "false")
    original_getenv = os.getenv

    def guarded_getenv(name, default=None):
        if name == "SWARMBOARD_AUTH_USERS":
            pytest.fail("login credentials were read by the gateway")
        return original_getenv(name, default)

    monkeypatch.setattr("swarmboard.gateways.os.getenv", guarded_getenv)
    monkeypatch.setattr("swarmboard.gateways.httpx.AsyncClient", lambda **kwargs: pytest.fail("network client was created"))
    agent = SimpleNamespace(provider="openai_compatible", model="test", settings={
        "base_url": "https://openrouter.ai/api/v1", "api_key_env": "SWARMBOARD_AUTH_USERS",
    })
    with pytest.raises(GatewayError, match="matching API key environment variable"):
        await ModelGateway().complete(agent, [])


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.test"}, {"hOsT": "127.0.0.1"}, {":authority": "attacker.test"},
    {"X-Forwarded-Host": "attacker.test"}, {"Forwarded": "host=attacker.test"},
    {"Content-Length": "0"}, {"Transfer-Encoding": "chunked"},
    {"HTTP-Referer": "https://board.test\r\nHost: attacker.test"}, {"X-Title": 123},
    "Host: attacker.test", [["Host", "attacker.test"]],
])
@pytest.mark.asyncio
async def test_hosted_rejects_header_overrides_before_resolving_credentials(monkeypatch, headers):
    import os

    monkeypatch.setenv("SWARMBOARD_HOSTED", "1")
    original_getenv = os.getenv

    def guarded_getenv(name, default=None):
        if name == "OPENROUTER_API_KEY":
            pytest.fail("credential was read before validating headers")
        return original_getenv(name, default)

    monkeypatch.setattr("swarmboard.gateways.os.getenv", guarded_getenv)
    monkeypatch.setattr("swarmboard.gateways.httpx.AsyncClient", lambda **kwargs: pytest.fail("network client was created"))
    agent = SimpleNamespace(provider="openai_compatible", model="test", settings={
        "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "headers": headers,
    })
    with pytest.raises(GatewayError, match="provider headers") as exc:
        await ModelGateway().complete(agent, [])
    assert exc.value.category == "configuration" and not exc.value.retryable


@pytest.mark.asyncio
async def test_hosted_allowed_request_uses_expected_key_without_following_redirect(monkeypatch):
    monkeypatch.setenv("SWARMBOARD_HOSTED", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "hosted-test-key")
    requests = []

    async def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.test/collect"})

    original_init = OpenAICompatibleGateway.__init__

    def init_with_transport(gateway, *args, **kwargs):
        original_init(gateway, *args, **kwargs, transport=httpx.MockTransport(redirect))

    monkeypatch.setattr(OpenAICompatibleGateway, "__init__", init_with_transport)
    agent = SimpleNamespace(provider="openai_compatible", model="test", settings={
        "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY",
        "headers": {"HTTP-Referer": "https://board.test", "x-title": "Swarmboard"},
    })
    with pytest.raises(GatewayError):
        await ModelGateway().complete(agent, [{"role": "user", "content": "Test request."}])
    assert len(requests) == 1
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert requests[0].headers["Host"] == "openrouter.ai"
    assert requests[0].headers["Authorization"] == "Bearer hosted-test-key"
    assert requests[0].headers["HTTP-Referer"] == "https://board.test"
    assert requests[0].headers["X-Title"] == "Swarmboard"


@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.asyncio
async def test_hosted_agent_api_rejects_unsafe_creation_and_effective_partial_updates(tmp_path, monkeypatch, hosted):
    from swarmboard.app import create_app
    from .test_engine_acceptance import ScriptedGateway
    from .auth_helpers import login

    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("SWARMBOARD_HOSTED", "1" if hosted else "0")
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"researcher":"test-password"}')
    gateway = ScriptedGateway()
    app = create_app(database_url=f"sqlite:///{tmp_path / 'provider-api.db'}", gateway=gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test") as client:
            await login(client, "researcher", "test-password")
            original_agents = (await client.get("/api/state")).json()["agents"]
            allowed = {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}
            payload = {"handle": "hosted_peer", "provider": "openai_compatible", "model": "test-model",
                       "persona": "Test participant.", "settings": allowed}
            bad_settings = [
                {**allowed, "base_url": "https://attacker.test/v1"},
                {**allowed, "base_url": "http://10.0.0.1/v1"},
                {**allowed, "api_key_env": "SWARMBOARD_AUTH_USERS"},
                {**allowed, "api_key_env": "SWARMBOARD_CODEX_API_KEY"},
                {**allowed, "headers": {"Host": "attacker.test"}},
                {**allowed, "headers": {"X-Forwarded-Host": "attacker.test"}},
            ]
            for settings in bad_settings:
                response = await client.post("/api/agents", json={**payload, "settings": settings})
                assert response.status_code == 422, response.text
                assert "attacker.test" not in response.text and "SWARMBOARD_AUTH_USERS" not in response.text
            assert (await client.get("/api/state")).json()["agents"] == original_agents
            response = await client.post("/api/agents", json=payload)
            assert response.status_code == 201, response.text
            created = response.json()
            assert created["settings"] == allowed
            for patch in [{"settings": settings, "model": "must-not-persist"} for settings in bad_settings] + [{"provider": "ollama"}]:
                response = await client.patch(f"/api/agents/{created['id']}", json=patch)
                assert response.status_code == 422, response.text
                current = next(agent for agent in (await client.get("/api/state")).json()["agents"] if agent["id"] == created["id"])
                assert current == created

            # A provider-only patch must validate settings already saved on the agent.
            response = await client.post("/api/agents", json={**payload, "handle": "codex_peer", "provider": "codex",
                                                            "settings": bad_settings[0]})
            assert response.status_code == 201, response.text
            codex = response.json()
            response = await client.patch(f"/api/agents/{codex['id']}", json={"provider": "openai_compatible"})
            assert response.status_code == 422, response.text
            current = next(agent for agent in (await client.get("/api/state")).json()["agents"] if agent["id"] == codex["id"])
            assert current == codex
    assert gateway.calls == []
