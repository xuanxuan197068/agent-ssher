"""The job layer, with the server replaced by a recorder.

These pin down what gets sent and how replies are read. The real round trips
are in test_integration.py.
"""

from __future__ import annotations

import base64
import re

import pytest

from ssher import jobs, ssh
from ssher.ssh import SshError, Target


def target(**kwargs) -> Target:
    kwargs.setdefault("alias", "box")
    kwargs.setdefault("ssh_host", "box")
    return Target(**kwargs)


class FakeServer:
    """Stands in for run_command: records each script, answers from a queue."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.scripts: list[str] = []
        self.kwargs: list[dict] = []

    def __call__(self, tgt, script, **kwargs):
        self.scripts.append(script)
        self.kwargs.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        base = {"ok": True, "host": tgt.alias, "exit_code": 0, "stdout": "",
                "stderr": "", "timed_out": False}
        base.update(reply)
        return base


@pytest.fixture
def server(monkeypatch):
    def install(*replies):
        fake = FakeServer(*replies)
        monkeypatch.setattr(jobs, "run_command", fake)
        return fake
    return install


def status_reply(state="running", exit_code="", size=0, log="", skipped=0,
                 started="1760000000", finished="", now="1760000010", match="0",
                 meta='{"name": "train", "cmd": "python t.py", "cwd": null}'):
    stdout = (
        f"__STATE__{state}\n__EXIT__{exit_code}\n__STARTED__{started}\n"
        f"__FINISHED__{finished}\n__NOW__{now}\n__SIZE__{size}\n__MATCH__{match}\n"
        f"__META__{meta}\n__SSHER_LOG__\n{log}"
    )
    return {"stdout": stdout, "stderr": f"__SKIPPED__{skipped}\n"}


# --------------------------------------------------------------------- ids


def test_a_job_id_carries_a_sortable_stamp_and_the_name():
    job_id = jobs.new_job_id("my train/run!")
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{4}-my-train-run$", job_id)


def test_a_job_id_without_a_name_is_still_valid():
    assert jobs.check_job_id(jobs.new_job_id())


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "..", "x" * 81, "-leading", "a b"])
def test_anything_that_could_leave_the_job_directory_is_refused(bad):
    with pytest.raises(SshError) as exc:
        jobs.check_job_id(bad)
    assert exc.value.code == ssh.BAD_REQUEST


# ------------------------------------------------------------------- start


def test_start_ships_the_script_as_base64_so_no_shell_can_touch_it(server):
    fake = server({"stdout": "__PID__4242\n"})
    nasty = "echo \"it's $HOME\" `whoami` $(id -u)\ncat <<'EOF'\n你好\nEOF"
    reply = jobs.start(target(), nasty, name="t")

    assert reply["ok"] and reply["pid"] == 4242 and reply["state"] == "running"
    encoded = re.findall(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d > \"\$D/cmd.sh\"",
                         fake.scripts[0])
    assert encoded, "the script should travel as base64"
    assert nasty in base64.b64decode(encoded[0]).decode("utf-8")


def test_start_includes_the_host_setup_and_cwd(server):
    fake = server({"stdout": "__PID__1\n"})
    jobs.start(target(init="conda activate x"), "python t.py", cwd="/srv")
    encoded = re.findall(r"printf %s ([A-Za-z0-9+/=]+) \| base64 -d > \"\$D/cmd.sh\"",
                         fake.scripts[0])[0]
    script = base64.b64decode(encoded).decode()
    assert script.index("conda activate x") < script.index("cd /srv") < script.index("python t.py")


def test_start_detaches_into_its_own_session(server):
    fake = server({"stdout": "__PID__1\n"})
    jobs.start(target(), "sleep 1")
    assert "setsid" in fake.scripts[0] and "< /dev/null &" in fake.scripts[0]


def test_start_without_a_pid_is_a_failure(server):
    server({"stdout": "__PID__\n", "stderr": "mkdir: Permission denied"})
    with pytest.raises(SshError, match="Permission denied"):
        jobs.start(target(), "sleep 1")


def test_start_refuses_an_empty_command(server):
    with pytest.raises(SshError, match="nothing to run"):
        jobs.start(target(), "   ")


# -------------------------------------------------------------------- wait


def test_wait_reports_a_running_job_and_where_to_resume(server):
    server(status_reply(state="running", size=120, log="epoch 1\n"))
    reply = jobs.wait(target(), "20261013-120000-ab12", max_wait=5)
    assert reply["state"] == "running" and reply["exit_code"] is None
    assert reply["log_offset"] == 120 and reply["output"] == "epoch 1\n"
    assert "since_offset 120" in reply["hint"]


def test_wait_reports_the_exit_code_and_run_time(server):
    server(status_reply(state="exited", exit_code="3", finished="1760000042"))
    reply = jobs.wait(target(), "20261013-120000-ab12")
    assert reply["state"] == "exited" and reply["exit_code"] == 3
    assert reply["elapsed_seconds"] == 42
    assert reply["started_at"].startswith("2025-10-09")
    assert "hint" not in reply


def test_wait_passes_the_offset_and_ceiling_to_the_server(server):
    fake = server(status_reply())
    jobs.wait(target(), "20261013-120000-ab12", max_wait=17, since_offset=4096)
    assert "OFF=4096" in fake.scripts[0]
    assert "+ 17 ))" in fake.scripts[0]
    # The local timeout must outlast the remote wait, or a full wait looks like a hang.
    assert fake.kwargs[0]["timeout"] > 17


def test_wait_reports_skipped_output(server):
    server(status_reply(state="exited", exit_code="0", log="tail\n", skipped=99000))
    reply = jobs.wait(target(), "20261013-120000-ab12", max_output=300)
    assert reply["output_skipped_bytes"] == 99000
    assert "sed -n" in reply["skipped_hint"]


def test_wait_collapses_progress_bar_frames(server):
    server(status_reply(log="start\n\r[#   ] 1/4\r[##  ] 2/4\r[### ] 3/4\ndone\n"))
    reply = jobs.wait(target(), "20261013-120000-ab12")
    assert reply["output"] == "start\n[### ] 3/4\ndone\n"


def test_wait_reports_a_match_only_when_asked(server):
    server(status_reply(match="1"), status_reply())
    assert jobs.wait(target(), "20261013-120000-ab12", until="READY")["matched"] is True
    assert "matched" not in jobs.wait(target(), "20261013-120000-ab12")


def test_wait_quotes_the_pattern_for_the_remote_shell(server):
    fake = server(status_reply())
    jobs.wait(target(), "20261013-120000-ab12", until="it's | $(boom)")
    assert "PAT='it'\"'\"'s | $(boom)'" in fake.scripts[0]


def test_wait_refuses_a_broken_pattern_before_connecting(server):
    fake = server()
    with pytest.raises(SshError, match="regular expression"):
        jobs.wait(target(), "20261013-120000-ab12", until="(unclosed")
    assert fake.scripts == []


def test_wait_on_a_missing_job_says_so(server):
    server({"stdout": "__MISSING__1\n"})
    with pytest.raises(SshError) as exc:
        jobs.wait(target(), "20261013-120000-ab12")
    assert exc.value.code == jobs.JOB_NOT_FOUND


def test_wait_explains_a_job_that_vanished(server):
    server(status_reply(state="gone"))
    assert "restarted" in jobs.wait(target(), "20261013-120000-ab12")["hint"]


def test_wait_retries_once_when_the_handshake_is_dropped(server, monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda _s: None)
    fake = server(SshError("dropped", ssh.CONNECT_FAILED), status_reply(state="exited",
                                                                        exit_code="0"))
    assert jobs.wait(target(), "20261013-120000-ab12")["state"] == "exited"
    assert len(fake.scripts) == 2


def test_wait_does_not_retry_an_auth_failure(server):
    fake = server(SshError("no", ssh.AUTH_FAILED))
    with pytest.raises(SshError):
        jobs.wait(target(), "20261013-120000-ab12")
    assert len(fake.scripts) == 1


def test_status_is_a_wait_that_does_not_wait(server):
    fake = server(status_reply())
    jobs.status(target(), "20261013-120000-ab12")
    assert "+ 0 ))" in fake.scripts[0]


def test_a_management_script_that_times_out_is_an_error(server):
    server({"timed_out": True, "exit_code": -1})
    with pytest.raises(SshError) as exc:
        jobs.wait(target(), "20261013-120000-ab12")
    assert exc.value.code == ssh.TIMEOUT


# -------------------------------------------------------------- kill, list


def test_kill_reports_what_it_did(server):
    server({"stdout": "__RESULT__terminated\n"})
    reply = jobs.kill(target(), "20261013-120000-ab12")
    assert reply["was_running"] and reply["result"] == "terminated"


def test_killing_a_finished_job_is_not_an_error(server):
    server({"stdout": "__RESULT__not-running:exited\n"})
    reply = jobs.kill(target(), "20261013-120000-ab12")
    assert reply["was_running"] is False and reply["state"] == "exited"


def test_kill_targets_the_whole_process_group(server):
    fake = server({"stdout": "__RESULT__terminated\n"})
    jobs.kill(target(), "20261013-120000-ab12")
    assert 'kill -TERM -- "-$P"' in fake.scripts[0]


def test_list_is_newest_first(server):
    meta = '{"name": "a", "cmd": "x", "cwd": null}'
    server({"stdout": (
        "__NOW__1760000100\n"
        f"20261013-1-aaaa\texited\t0\t1760000000\t1760000050\t10\t{meta}\n"
        f"20261013-2-bbbb\trunning\t\t1760000060\t\t20\t{meta}\n"
    )})
    reply = jobs.listing(target())
    assert [j["job_id"] for j in reply["jobs"]] == ["20261013-2-bbbb", "20261013-1-aaaa"]
    running, done = reply["jobs"]
    assert running["elapsed_seconds"] == 40 and running["exit_code"] is None
    assert done["elapsed_seconds"] == 50 and done["exit_code"] == 0


def test_list_on_a_host_with_no_jobs(server):
    server({"stdout": "__NOW__1760000100\n"})
    assert jobs.listing(target()) == {"ok": True, "host": "box", "jobs": [], "count": 0}
