"""End-to-end tests for jobs and for the MCP server, against a real host.

    SSHER_TEST_HOST=wsl uv run pytest tests/test_integration_jobs.py

The target may be a machine with real work on it, so these tests remove only
the jobs they started themselves, never the whole job directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

HOST = os.environ.get("SSHER_TEST_HOST")

pytestmark = pytest.mark.skipif(not HOST, reason="set SSHER_TEST_HOST to run these")

STARTED: list[str] = []


def ssher(*argv: str, expect_ok: bool = True) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "ssher.cli", "--compact", *argv],
        capture_output=True, timeout=180,
    )
    stdout = proc.stdout.decode("utf-8", "replace").strip()
    assert stdout, f"no output. stderr: {proc.stderr.decode('utf-8', 'replace')}"
    payload = json.loads(stdout)
    if expect_ok:
        assert payload["ok"] is True, payload
    return payload


def start(cmd: str, name: str = "pytest") -> str:
    job_id = ssher("job", "start", HOST, "--name", name, "--", cmd)["job_id"]
    STARTED.append(job_id)
    return job_id


@pytest.fixture(autouse=True, scope="module")
def _cleanup():
    yield
    for job_id in STARTED:
        ssher("job", "kill", HOST, job_id, expect_ok=False)
    if STARTED:
        dirs = " ".join(f"~/.ssher/jobs/{job_id}" for job_id in STARTED)
        ssher(HOST, f"rm -rf {dirs}", expect_ok=False)


# --------------------------------------------------------------------- jobs


def test_a_job_returns_at_once_and_reports_its_exit_code_when_done():
    t0 = time.monotonic()
    job_id = start("sleep 2; echo finished; exit 7")
    assert time.monotonic() - t0 < 15

    reply = ssher("job", "wait", HOST, job_id, "--max-wait", "30")
    assert reply["state"] == "exited" and reply["exit_code"] == 7
    assert reply["output"] == "finished\n"


def test_wait_returns_soon_after_the_job_ends_not_at_the_ceiling():
    job_id = start("sleep 3")
    t0 = time.monotonic()
    ssher("job", "wait", HOST, job_id, "--max-wait", "60")
    # Three seconds of work, plus a second of polling and a connection or two.
    assert time.monotonic() - t0 < 15


def test_a_wait_that_runs_out_returns_running_and_loses_nothing():
    job_id = start("echo first; sleep 4; echo second")
    early = ssher("job", "wait", HOST, job_id, "--max-wait", "1")
    assert early["state"] == "running"

    later = ssher("job", "wait", HOST, job_id, "--max-wait", "30",
                  "--since", str(early["log_offset"]))
    assert later["state"] == "exited"
    # The two increments add up to the whole log: nothing lost, nothing repeated.
    assert early["output"] + later["output"] == "first\nsecond\n"


def test_until_returns_as_soon_as_the_pattern_appears():
    job_id = start("sleep 2; echo 'server READY on 8080'; sleep 120")
    t0 = time.monotonic()
    reply = ssher("job", "wait", HOST, job_id, "--until", "READY", "--max-wait", "60")
    assert reply["matched"] is True and reply["state"] == "running"
    assert time.monotonic() - t0 < 20


def test_kill_stops_every_process_the_job_started():
    job_id = start("sleep 3017 & sleep 3018; wait")
    time.sleep(1)
    assert ssher("job", "kill", HOST, job_id)["was_running"] is True
    left = ssher(HOST, "pgrep -f '[s]leep 301[78]' || true")
    assert left["stdout"].strip() == ""
    assert ssher("job", "status", HOST, job_id)["state"] == "killed"


def test_a_job_keeps_the_hosts_quoting_free_input():
    nasty = "echo \"it's $HOME\" | sed 's/x/y/'; printf '%s\\n' '你好'"
    job_id = start(nasty)
    reply = ssher("job", "wait", HOST, job_id, "--max-wait", "30")
    assert reply["output"].startswith("it's /") and reply["output"].endswith("你好\n")


def test_job_list_shows_the_jobs_started_here():
    listed = {job["job_id"] for job in ssher("job", "list", HOST)["jobs"]}
    assert set(STARTED) <= listed


def test_an_unknown_job_is_reported_as_such():
    reply = ssher("job", "status", HOST, "20000101-000000-0000-none", expect_ok=False)
    assert reply["error"]["code"] == "JOB_NOT_FOUND"


def test_the_milliseconds_mistake_is_stopped_before_connecting():
    t0 = time.monotonic()
    reply = ssher(HOST, "--timeout", "240000", "true", expect_ok=False)
    assert reply["error"]["code"] == "BAD_REQUEST"
    assert "pass 240" in reply["error"]["message"]
    assert time.monotonic() - t0 < 5


# ---------------------------------------------------------------------- MCP


def _mcp(calls, env_extra=None):
    """Run `ssher mcp` over stdio with the official client and make some calls."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    def payload(result):
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                return json.loads(text)
        return {}

    async def go():
        env = dict(os.environ)
        env.update(env_extra or {})
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "ssher.cli", "mcp"], env=env
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await calls(session, payload)

    return asyncio.run(go())


def test_mcp_lists_its_tools():
    async def calls(session, _payload):
        return [tool.name for tool in (await session.list_tools()).tools]

    assert set(_mcp(calls)) >= {"run", "job_start", "job_wait", "hosts"}


def test_mcp_delivers_a_command_no_shell_argument_could_carry():
    nasty = (
        "cat <<'EOF'\n"
        "it's \"real\" $HOME `whoami` $(id -u) 你好 \U0001F680 a&b|c;d>e #f\n"
        "EOF\n"
        "echo \"remote: $HOME\""
    )

    async def calls(session, payload):
        return payload(await session.call_tool("run", {"host": HOST, "cmd": nasty}))

    reply = _mcp(calls)
    assert reply["stdout"].startswith(
        "it's \"real\" $HOME `whoami` $(id -u) 你好 \U0001F680 a&b|c;d>e #f\n"
    )
    assert "remote: /" in reply["stdout"]


def test_mcp_follows_a_job_to_the_end_with_incremental_output():
    async def calls(session, payload):
        started = payload(await session.call_tool("job_start", {
            "host": HOST, "name": "pytest-mcp",
            "cmd": "for i in 1 2 3; do echo step $i; sleep 1; done",
        }))
        STARTED.append(started["job_id"])
        offset, seen = 0, ""
        for _ in range(20):
            reply = payload(await session.call_tool("job_wait", {
                "host": HOST, "job_id": started["job_id"],
                "since_offset": offset, "max_wait_seconds": 2,
            }))
            seen += reply["output"]
            offset = reply["log_offset"]
            if reply["state"] != "running":
                return reply, seen
        raise AssertionError("job never finished")

    reply, seen = _mcp(calls)
    assert reply["state"] == "exited" and reply["exit_code"] == 0
    # Stitched together from increments, each line exactly once.
    assert seen == "step 1\nstep 2\nstep 3\n"


def test_mcp_never_blocks_past_its_call_limit():
    async def calls(session, payload):
        t0 = time.monotonic()
        reply = payload(await session.call_tool("run", {
            "host": HOST, "cmd": "sleep 30", "timeout_seconds": 300,
        }))
        return reply, time.monotonic() - t0

    reply, took = _mcp(calls, {"SSHER_MCP_CALL_LIMIT": "8"})
    assert reply["timed_out"] is True and reply["timeout_seconds_used"] < 8
    assert took < 8
    assert "job_start" in reply["hint"]
