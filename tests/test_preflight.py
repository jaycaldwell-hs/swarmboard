from __future__ import annotations

import hashlib
import json

import pytest

from swarmboard.gateways import AgentAction, GatewayError, GatewayResult, StructuredOutputError, TokenUsage
from swarmboard.preflight import run_codex_preflight


@pytest.fixture(autouse=True)
def isolated_preflight(monkeypatch):
    monkeypatch.delenv("SWARMBOARD_CODEX_PREFLIGHT_ID", raising=False)


def result(action="pass"):
    parsed = AgentAction(action="pass") if action == "pass" else AgentAction(
        action="new_thread", title="Unexpected output", body="Do not persist this", intent="clarify")
    return GatewayResult(action=parsed, raw_output="sensitive-raw-output", provider="codex", model="gpt-6-astra",
                         latency_ms=1, usage=TokenUsage(10, 2, 12), response_metadata={
                             "auth_mode": "api_key", "configured_cli_version": "0.153.4",
                             "private_diagnostic": "do-not-record-this",
                         })


def mock_gateway(monkeypatch, outcomes):
    calls = []

    class Gateway:
        def __init__(self, *, timeout_seconds):
            assert timeout_seconds == 90

        async def complete(self, **kwargs):
            calls.append(kwargs)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr("swarmboard.preflight.CodexGateway", Gateway)
    return calls


def test_unconfigured_preflight_does_nothing(tmp_path, monkeypatch, capsys):
    calls = mock_gateway(monkeypatch, [])
    assert run_codex_preflight(tmp_path / "board.db") is None
    assert calls == []
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().out == ""


def test_success_is_private_atomic_and_same_id_skips_billing(tmp_path, monkeypatch, capsys):
    check_id = "private-unprinted-check-id"
    monkeypatch.setenv("SWARMBOARD_CODEX_PREFLIGHT_ID", check_id)
    calls = mock_gateway(monkeypatch, [result(), result()])
    path = tmp_path / "board.db"
    saved = run_codex_preflight(path)
    assert saved == {"status": "passed", "auth_mode": "api_key", "version": "0.153.4", "token_count": 12}
    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-6-astra"
    assert calls[0]["sampling"] == {"reasoning_effort": "medium"}
    assert {message["role"] for message in calls[0]["messages"]} == {"system", "user"}
    assert all("persona" not in message["content"].lower() for message in calls[0]["messages"])
    directory = tmp_path / ".runtime-checks"
    marker = directory / (hashlib.sha256(check_id.encode()).hexdigest() + ".json")
    assert directory.stat().st_mode & 0o777 == 0o700
    assert marker.stat().st_mode & 0o777 == 0o600
    assert list(directory.iterdir()) == [marker]
    assert json.loads(marker.read_text()) == saved
    assert not path.exists()
    assert run_codex_preflight(path) == saved
    assert len(calls) == 1
    output = capsys.readouterr().out
    assert json.loads(output.splitlines()[-1])["status"] == "already_passed"
    for excluded in (check_id, "sensitive-raw-output", "do-not-record-this"):
        assert excluded not in output + marker.read_text()
    monkeypatch.setenv("SWARMBOARD_CODEX_PREFLIGHT_ID", "another-check")
    run_codex_preflight(path)
    assert len(calls) == 2


@pytest.mark.parametrize("failure,category", [
    (GatewayError("sensitive-gateway-diagnostic", category="authentication"), "authentication"),
    (GatewayError("sensitive-gateway-diagnostic", category="timeout"), "timeout"),
    (StructuredOutputError("sensitive-output-diagnostic", raw_output="sensitive-body"), "structured_output"),
])
def test_failure_has_no_success_marker_and_can_retry(tmp_path, monkeypatch, capsys, failure, category):
    monkeypatch.setenv("SWARMBOARD_CODEX_PREFLIGHT_ID", "retryable-check")
    calls = mock_gateway(monkeypatch, [failure, result()])
    path = tmp_path / "board.db"
    with pytest.raises(GatewayError) as raised:
        run_codex_preflight(path)
    assert raised.value.category == category
    assert not list((tmp_path / ".runtime-checks").iterdir())
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "failed"
    assert "sensitive" not in output + str(raised.value)
    assert not path.exists()
    assert run_codex_preflight(path)["status"] == "passed"
    assert len(calls) == 2


def test_non_pass_action_cannot_mark_runtime_success(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMBOARD_CODEX_PREFLIGHT_ID", "unexpected-action")
    mock_gateway(monkeypatch, [result("new_thread")])
    with pytest.raises(GatewayError) as raised:
        run_codex_preflight(tmp_path / "board.db")
    assert raised.value.category == "structured_output"
    assert not list((tmp_path / ".runtime-checks").iterdir())


def test_corrupt_marker_fails_without_rebilling(tmp_path, monkeypatch):
    check_id = "bad-marker"
    monkeypatch.setenv("SWARMBOARD_CODEX_PREFLIGHT_ID", check_id)
    directory = tmp_path / ".runtime-checks"
    directory.mkdir()
    marker = directory / (hashlib.sha256(check_id.encode()).hexdigest() + ".json")
    marker.write_text("not-json")
    calls = mock_gateway(monkeypatch, [])
    with pytest.raises(GatewayError) as raised:
        run_codex_preflight(tmp_path / "board.db")
    assert raised.value.category == "configuration"
    assert calls == []


def test_failed_atomic_marker_write_removes_temporary_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARMBOARD_CODEX_PREFLIGHT_ID", "marker-write-failure")
    mock_gateway(monkeypatch, [result()])

    def fail_replace(source, destination):
        raise OSError("sensitive-storage-diagnostic")

    monkeypatch.setattr("swarmboard.preflight.os.replace", fail_replace)
    with pytest.raises(GatewayError) as raised:
        run_codex_preflight(tmp_path / "board.db")
    assert raised.value.category == "configuration"
    assert not list((tmp_path / ".runtime-checks").iterdir())
    assert "sensitive" not in capsys.readouterr().out + str(raised.value)
