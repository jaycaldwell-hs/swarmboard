"""The authenticated frontend must not load third-party code or resources."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
import pytest

from swarmboard.app import create_app
from swarmboard.config import Settings
from .test_engine_acceptance import ScriptedGateway
from .auth_helpers import login


class PageResources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.scripts = []
        self.handlers = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script":
            self.scripts.append(values)
        for name, value in attrs:
            if name.lower().startswith("on"):
                self.handlers.append(name)
            if name in {"src", "href", "action", "formaction", "poster"}:
                self.references.append(value)


@pytest.fixture
async def docs_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps({"collaborator": "planted-docs-password"}))
    monkeypatch.setenv("SWARMBOARD_REQUIRE_AUTH", "1")
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'docs.db'}", persona_dir=tmp_path)
    app = create_app(settings=settings, gateway=ScriptedGateway(), recover_on_start=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test") as client:
            await login(client, "collaborator", "planted-docs-password")
            yield app, client


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/redoc"])
async def test_api_reference_uses_local_assets_and_keeps_schema_available(docs_client, path):
    app, client = docs_client
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test") as anonymous:
        assert (await anonymous.get(path)).status_code == 401
    response = await client.get(path)
    assert response.status_code == 200
    page = PageResources()
    page.feed(response.text)
    assert len(page.scripts) == 1 and page.scripts[0].get("src") == "/static/auth.js"
    assert page.handlers == []
    assert "/openapi.json" in page.references
    assert "/static/api_docs.css" in page.references
    assert all(urlsplit(urljoin("https://board.test", ref)).netloc == "board.test" for ref in page.references)
    assert "GET" in response.text and "POST" in response.text
    assert "/api/sessions" in response.text and "/api/turns/{turn_id}" in response.text
    css = await client.get("/static/api_docs.css")
    assert css.status_code == 200
    assert "@import" not in css.text and "url(" not in css.text
    schema = await client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/sessions" in schema.json()["paths"]
    assert "/docs" not in schema.json()["paths"] and "/redoc" not in schema.json()["paths"]
    assert app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_reference_renders_schema_descriptions_as_inert_text(docs_client):
    app, client = docs_client
    app.openapi = lambda: {"paths": {"/api/example": {"get": {
        "summary": '<script src="https://observer.invalid/script.js"></script>',
        "description": '<img src="https://observer.invalid/pixel" onerror="alert(1)">',
    }}}}
    response = await client.get("/docs")
    assert response.status_code == 200
    assert "&lt;script" in response.text and "&lt;img" in response.text
    page = PageResources()
    page.feed(response.text)
    assert len(page.scripts) == 1 and page.scripts[0].get("src") == "/static/auth.js"
    assert not page.handlers
    assert not any("observer.invalid" in reference for reference in page.references)
