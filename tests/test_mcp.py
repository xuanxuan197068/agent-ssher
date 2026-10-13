"""The MCP front door: its schema, its limits, and its error shape.

What the agent sees here is all the documentation it gets, so the schema is
tested as carefully as the behaviour.
"""

from __future__ import annotations

import asyncio

import pytest

from ssher import jobs, mcp_server, ssh


def tools() -> dict:
    return {tool.name: tool for tool in asyncio.run(mcp_server.server.list_tools())}


def schema(tool) -> dict:
    return getattr(tool, "input_schema", None) or tool.inputSchema


# ------------------------------------------------------------------ schema


def test_the_tool_set_is_small_and_complete():
    assert set(tools()) == {
        "hosts", "test", "run", "upload", "download",
        "job_start", "job_wait", "job_kill", "job_list",
    }


def test_every_tool_and_parameter_is_described():
    for name, tool in tools().items():
        assert tool.description and tool.description.strip(), name
        for param, spec in schema(tool).get("properties", {}).items():
            assert spec.get("description"), f"{name}.{param} has no description"


def test_the_timeout_says_seconds_in_its_name_and_its_description():
    spec = schema(tools()["run"])["properties"]["timeout_seconds"]
    assert "SECONDS, not milliseconds" in spec["description"]


def test_run_takes_the_command_as_one_plain_string():
    props = schema(tools()["run"])["properties"]
    assert props["cmd"]["type"] == "string"
    assert "b64" not in props and "cmd_file" not in props


def test_job_wait_explains_how_to_resume():
    desc = schema(tools()["job_wait"])["properties"]["since_offset"]["description"]
    assert "log_offset" in desc


# ------------------------------------------------------------------ limits


@pytest.mark.parametrize("limit, budget", [(2, 1), (6, 5), (50, 45), (60, 55), (900, 895)])
def test_the_budget_leaves_room_for_the_reply(limit, budget):
    assert mcp_server.call_budget(limit) == budget


def test_the_limit_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(mcp_server.CALL_LIMIT_ENV, "840")
    assert mcp_server.call_limit() == 840


@pytest.mark.parametrize("raw", ["", "0", "-5", "lots"])
def test_a_missing_or_silly_limit_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv(mcp_server.CALL_LIMIT_ENV, raw)
    assert mcp_server.call_limit() == mcp_server.DEFAULT_CALL_LIMIT


@pytest.fixture
def recorded(sandbox, monkeypatch):
    """Capture what the tools would have asked the server to do."""
    calls: dict = {}

    def fake_run(target, cmd, **kwargs):
        calls["run"] = {"cmd": cmd, **kwargs}
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    def fake_wait(target, job_id, **kwargs):
        calls["wait"] = {"job_id": job_id, **kwargs}
        return {"ok": True, "state": "running"}

    monkeypatch.setattr(ssh, "run_command", fake_run)
    monkeypatch.setattr(jobs, "wait", fake_wait)
    return calls


def test_run_stays_inside_the_call_limit(recorded, monkeypatch):
    monkeypatch.setenv(mcp_server.CALL_LIMIT_ENV, "50")
    reply = mcp_server.run(host="gpu1", cmd="make", timeout_seconds=300)
    assert recorded["run"]["timeout"] == 45
    assert reply["timeout_seconds_used"] == 45


def test_run_does_not_report_a_cap_it_did_not_apply(recorded, monkeypatch):
    monkeypatch.setenv(mcp_server.CALL_LIMIT_ENV, "50")
    reply = mcp_server.run(host="gpu1", cmd="ls", timeout_seconds=10)
    assert recorded["run"]["timeout"] == 10
    assert "timeout_seconds_used" not in reply


def test_run_sends_the_command_exactly_as_given(recorded):
    nasty = "echo \"it's $HOME\" `whoami`\ncat <<'EOF'\n你好\nEOF"
    mcp_server.run(host="gpu1", cmd=nasty)
    assert recorded["run"]["cmd"] == nasty


def test_job_wait_defaults_to_the_budget(recorded, monkeypatch):
    monkeypatch.setenv(mcp_server.CALL_LIMIT_ENV, "60")
    mcp_server.job_wait(host="gpu1", job_id="20261013-120000-ab12")
    assert recorded["wait"]["max_wait"] == 55


def test_job_wait_never_asks_for_more_than_the_budget(recorded, monkeypatch):
    monkeypatch.setenv(mcp_server.CALL_LIMIT_ENV, "60")
    mcp_server.job_wait(host="gpu1", job_id="20261013-120000-ab12", max_wait_seconds=3600)
    assert recorded["wait"]["max_wait"] == 55


def test_job_wait_zero_is_a_status_check(recorded):
    mcp_server.job_wait(host="gpu1", job_id="20261013-120000-ab12", max_wait_seconds=0)
    assert recorded["wait"]["max_wait"] == 0


# ------------------------------------------------------------------ errors


def test_errors_come_back_in_the_same_shape_as_the_cli(sandbox):
    reply = mcp_server.run(host="no-such-host", cmd="true")
    assert reply["ok"] is False
    assert reply["error"]["code"] == ssh.HOST_NOT_FOUND
    assert "ssher hosts" in reply["error"]["message"]


def test_a_bad_job_id_is_refused_without_connecting(sandbox):
    reply = mcp_server.job_wait(host="gpu1", job_id="../../etc")
    assert reply["ok"] is False and reply["error"]["code"] == ssh.BAD_REQUEST
