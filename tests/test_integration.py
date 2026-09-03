"""End-to-end tests that drive the real command line against a real host.

Opt in by pointing SSHER_TEST_HOST at an alias you can reach:

    SSHER_TEST_HOST=wsl uv run pytest tests/test_integration.py

Everything is confined to ~/ssher-pytest on that host and removed afterwards.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

import pytest

HOST = os.environ.get("SSHER_TEST_HOST")

pytestmark = pytest.mark.skipif(not HOST, reason="set SSHER_TEST_HOST to run these")

REMOTE_DIR = "~/ssher-pytest"

# printf with two escaped newlines, kept out of the assertions below
PRINTF_TWO_LINES = r'printf "a\nb\n"'


def ssher(*argv: str, expect_ok: bool = True) -> dict:
    """Run the CLI exactly the way an agent would, and parse its JSON."""
    proc = subprocess.run(
        [sys.executable, "-m", "ssher.cli", "--compact", *argv],
        capture_output=True,
        timeout=180,
    )
    stdout = proc.stdout.decode("utf-8", "replace").strip()
    assert stdout, f"no output. stderr: {proc.stderr.decode('utf-8', 'replace')}"
    payload = json.loads(stdout)
    if expect_ok:
        assert payload["ok"] is True, payload
    return payload


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


@pytest.fixture(autouse=True, scope="module")
def _cleanup():
    yield
    ssher(HOST, f"rm -rf {REMOTE_DIR}")


# ------------------------------------------------------------------- basics


def test_connects_and_identifies_the_host():
    result = ssher("test", HOST)
    assert result["has_bash"] is True
    assert result["uname"]


def test_lists_the_host_it_is_about_to_use():
    aliases = {h["alias"].lower() for h in ssher("hosts")["hosts"]}
    assert HOST.lower() in aliases


def test_runs_a_command_and_returns_stdout():
    assert ssher(HOST, "echo hello")["stdout"] == "hello\n"


def test_reports_a_failing_command_without_calling_it_a_tool_error():
    result = ssher(HOST, "exit 7")
    assert result["ok"] is True and result["exit_code"] == 7


def test_check_turns_a_failing_command_into_an_error():
    result = ssher(HOST, "--check", "exit 7", expect_ok=False)
    assert result["error"]["code"] == "EXEC_FAILED"


def test_separates_stdout_from_stderr():
    result = ssher(HOST, "echo out; echo err >&2")
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"


# ---------------------------------------------------------------- base64 path


def test_base64_carries_a_command_no_shell_could_survive():
    script = (
        f"mkdir -p {REMOTE_DIR}\n"
        f"cat > {REMOTE_DIR}/nasty.txt <<'XEOF'\n"
        "it's \"真的\" $HOME `whoami` $(id -u)\n"
        "second line\n"
        "XEOF\n"
        f"cat {REMOTE_DIR}/nasty.txt"
    )
    result = ssher(HOST, "--b64", b64(script))
    assert result["stdout"] == "it's \"真的\" $HOME `whoami` $(id -u)\nsecond line\n"


def test_bad_base64_is_refused_before_anything_is_sent():
    result = ssher(HOST, "--b64", "this is not base64!", expect_ok=False)
    assert result["error"]["code"] == "BAD_REQUEST"


# ------------------------------------------------------------------ options


def test_cwd_is_honoured():
    assert ssher(HOST, "--cwd", "/tmp", "pwd")["stdout"].strip() == "/tmp"


def test_a_tilde_cwd_reaches_the_home_directory():
    home = ssher("test", HOST)["home"]
    assert ssher(HOST, "--cwd", "~", "pwd")["stdout"].strip() == home


def test_a_bad_cwd_stops_the_command_from_running():
    result = ssher(HOST, "--cwd", "/no/such/dir", "echo should-not-appear")
    assert result["exit_code"] != 0
    assert "should-not-appear" not in result["stdout"]


def test_env_reaches_the_command():
    result = ssher(HOST, "--env", "GREETING=hi there", 'echo "$GREETING"')
    assert result["stdout"] == "hi there\n"


def test_stdin_is_piped_in():
    assert ssher(HOST, "--stdin", "piped\n", "cat")["stdout"] == "piped\n"


def test_a_slow_command_is_given_up_on():
    result = ssher(HOST, "--timeout", "2", "echo start; sleep 30")
    assert result["timed_out"] is True
    assert "start" in result["stdout"]


def test_long_output_is_truncated_from_the_middle():
    result = ssher(HOST, "--max-output", "2000", "seq 1 20000")
    assert result["truncated"] is True
    assert result["stdout"].startswith("1\n")
    assert result["stdout"].rstrip().endswith("20000")


# ---------------------------------------------------------------- transfers


def test_a_file_survives_an_upload_and_a_download(tmp_path):
    source = tmp_path / "payload.bin"
    source.write_bytes(bytes(range(256)) * 8)
    ssher("upload", HOST, str(source), f"{REMOTE_DIR}/payload.bin")

    back = tmp_path / "returned.bin"
    ssher("download", HOST, f"{REMOTE_DIR}/payload.bin", str(back))
    assert back.read_bytes() == source.read_bytes()


def test_a_directory_needs_the_recursive_flag(tmp_path):
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "a.txt").write_text("a", encoding="utf-8")
    result = ssher("upload", HOST, str(tmp_path / "tree"), f"{REMOTE_DIR}/tree",
                   expect_ok=False)
    assert "recursive" in result["error"]["message"]


def test_a_directory_round_trips_with_the_flag(tmp_path):
    tree = tmp_path / "tree2"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "b.txt").write_text("第二行", encoding="utf-8")
    ssher("upload", HOST, str(tree), f"{REMOTE_DIR}/tree2", "-r")

    back = tmp_path / "back"
    ssher("download", HOST, f"{REMOTE_DIR}/tree2", str(back), "-r")
    assert (back / "sub" / "b.txt").read_text(encoding="utf-8") == "第二行"


def test_uploading_something_that_is_not_there_says_so(tmp_path):
    result = ssher("upload", HOST, str(tmp_path / "missing"), f"{REMOTE_DIR}/x",
                   expect_ok=False)
    assert result["error"]["code"] == "BAD_REQUEST"


# ------------------------------------------------------------------- errors


def test_an_unknown_alias_is_reported_before_connecting():
    result = ssher("test", "definitely-not-a-real-host", expect_ok=False)
    assert result["error"]["code"] == "HOST_NOT_FOUND"


def test_an_unreachable_port_is_a_connect_failure():
    result = ssher("test", "root@127.0.0.1:59999", expect_ok=False)
    assert result["error"]["code"] == "CONNECT_FAILED"


def test_a_rejected_key_is_an_auth_failure():
    target = ssher("test", HOST)
    hostname = target["uname"] and "127.0.0.1"
    result = ssher("test", f"nosuchuser-{os.getpid()}@{hostname}", "--accept-new",
                   expect_ok=False)
    assert result["error"]["code"] in ("AUTH_FAILED", "CONNECT_FAILED")


def raw(*argv: str) -> bytes:
    """Run with --raw and return the exact bytes it printed."""
    proc = subprocess.run(
        [sys.executable, "-m", "ssher.cli", "--raw", *argv],
        capture_output=True, timeout=60,
    )
    return proc.stdout


def test_raw_output_is_byte_exact():
    # A newline the local platform rewrote would corrupt a redirected log or a
    # binary stream on its way past.
    assert raw(HOST, PRINTF_TWO_LINES) == b"a\nb\n"


def test_raw_output_adds_no_trailing_newline():
    assert raw(HOST, "printf x") == b"x"


def test_raw_output_keeps_non_ascii_bytes():
    assert raw(HOST, "printf 你好") == "你好".encode("utf-8")


def test_json_output_is_not_line_translated():
    proc = subprocess.run(
        [sys.executable, "-m", "ssher.cli", HOST, "echo hi"],
        capture_output=True, timeout=60,
    )
    assert proc.stdout.startswith(b"{\n")
    assert b"\r\n" not in proc.stdout
