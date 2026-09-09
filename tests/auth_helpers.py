"""Explicit browser-style authentication for authenticated API fixtures."""


async def login(client, username, password):
    response = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, f"Fixture login returned HTTP {response.status_code}"
    return response.json()
