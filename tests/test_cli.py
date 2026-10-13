from __future__ import annotations

import argparse
import base64
import io
import json

import pytest

from ssher import cli
from ssher import ssh


def parse(argv):
    return cli.build_parser().parse_args(argv)


def full_parse(argv):
    """Go through main's subcommand-or-host decision, like a real invocation."""
    argv = list(argv)
    first = next((a for a in argv if not a.startswith("-")), None)
    if first is not None and first not in cli.SUBCOMMANDS:
        argv.insert(argv.index(first), "run")
    return cli.build_parser().parse_args(argv)


# ------------------------------------------------- host-or-subcommand routing


def test_a_bare_host_is_treated_as_a_command_to_run():
    args = full_parse(["gpu1", "uptime"])
    assert args.func is cli.cmd_run
    assert args.host == "gpu1"


def test_a_subcommand_still_wins():
    assert full_parse(["hosts"]).func is cli.cmd_hosts
    assert full_parse(["test", "gpu1"]).func is cli.cmd_test


def test_run_can_be_written_out_for_a_host_named_like_a_subcommand():
    args = full_parse(["run", "hosts", "uptime"])
    assert args.func is cli.cmd_run and args.host == "hosts"


def test_a_leading_global_flag_does_not_confuse_the_routing():
    args = full_parse(["--compact", "gpu1", "uptime"])
    assert args.func is cli.cmd_run and args.compact is True


# ------------------------------------------------------------ base64 commands


def test_base64_is_decoded():
    assert cli.decode_command(base64.b64encode("echo hi".encode()).decode(), True) == "echo hi"


def test_base64_carries_anything_a_shell_would_mangle():
    nasty = "cat <<'EOF'\nit's \"$HOME\" `whoami`\n第二行\nEOF"
    encoded = base64.b64encode(nasty.encode()).decode()
    assert cli.decode_command(encoded, True) == nasty


def test_plain_text_is_left_alone_without_the_flag():
    assert cli.decode_command("ZWNobyBoaQ==", False) == "ZWNobyBoaQ=="


def test_bad_base64_is_reported_rather_than_run():
    with pytest.raises(ssh.SshError) as exc:
        cli.decode_command("not base64 at all!", True)
    assert exc.value.code == ssh.BAD_REQUEST


def test_base64_of_non_utf8_is_rejected():
    with pytest.raises(ssh.SshError):
        cli.decode_command(base64.b64encode(b"\xff\xfe").decode(), True)


# ------------------------------------------------------------ argument shape


def test_join_command_drops_a_leading_separator():
    assert cli.join_command(["--", "ls", "-la"]) == "ls -la"


def test_parse_env_builds_a_mapping():
    assert cli.parse_env(["A=1", "B=x=y"]) == {"A": "1", "B": "x=y"}


def test_parse_env_rejects_a_bare_name():
    with pytest.raises(ssh.SshError, match="NAME=VALUE"):
        cli.parse_env(["A"])


def test_flags_may_follow_the_host():
    args = full_parse(["gpu1", "--cwd", "/srv", "--timeout", "5m", "--", "ls"])
    cli.take_back_flags(args)
    # Durations stay text until the command knows which limit applies.
    assert args.cwd == "/srv" and args.timeout == "5m"
    assert cli.join_command(args.command) == "ls"


def test_the_equals_form_works_too():
    args = full_parse(["gpu1", "--cwd=/srv", "ls"])
    cli.take_back_flags(args)
    assert args.cwd == "/srv" and cli.join_command(args.command) == "ls"


def test_take_back_stops_at_the_command():
    args = full_parse(["gpu1", "ls", "--cwd", "/srv"])
    cli.take_back_flags(args)
    assert args.cwd is None
    assert cli.join_command(args.command) == "ls --cwd /srv"


def test_a_command_with_its_own_dashes_survives():
    args = full_parse(["gpu1", "ls", "-la", "/etc"])
    cli.take_back_flags(args)
    assert cli.join_command(args.command) == "ls -la /etc"


def test_b64_may_follow_the_host():
    args = full_parse(["gpu1", "--b64", "ZWNobyBoaQ=="])
    cli.take_back_flags(args)
    assert args.b64 is True
    assert cli.join_command(args.command) == "ZWNobyBoaQ=="


def test_a_missing_flag_value_is_reported():
    args = full_parse(["gpu1", "--cwd"])
    with pytest.raises(ssh.SshError, match="needs a value"):
        cli.take_back_flags(args)


def test_a_non_numeric_timeout_is_reported():
    with pytest.raises(ssh.SshError, match="duration"):
        cli.parse_duration("soon", flag="--timeout", cap=7200)


# ------------------------------------------------------------------ durations


@pytest.mark.parametrize("text, seconds", [
    ("90", 90), ("90s", 90), ("5m", 300), ("2h", 7200), ("1h30m", 5400),
    ("1h0m5s", 3605), (" 45 ", 45), ("10M", 600),
])
def test_durations_parse(text, seconds):
    assert cli.parse_duration(text, flag="--timeout", cap=86400) == seconds


def test_a_duration_past_the_cap_is_refused():
    with pytest.raises(ssh.SshError, match="past the"):
        cli.parse_duration("3h", flag="--timeout", cap=7200)


def test_the_milliseconds_mistake_is_named_with_the_value_meant():
    # 240000 is what an agent writes when it means four minutes in milliseconds.
    with pytest.raises(ssh.SshError) as exc:
        cli.parse_duration("240000", flag="--timeout", cap=7200)
    assert "not milliseconds" in exc.value.message
    assert "pass 240" in exc.value.message


def test_the_milliseconds_hint_is_not_offered_for_a_value_with_units():
    with pytest.raises(ssh.SshError) as exc:
        cli.parse_duration("3h", flag="--timeout", cap=7200)
    assert "milliseconds" not in exc.value.message


def test_zero_is_refused_where_a_minimum_applies():
    with pytest.raises(ssh.SshError, match="at least"):
        cli.parse_duration("0", flag="--timeout", cap=7200)


def test_zero_is_allowed_where_it_means_do_not_wait():
    assert cli.parse_duration("0", flag="--max-wait", cap=86400, minimum=0) == 0


@pytest.mark.parametrize("bad", ["", "m", "5 m", "-5", "1.5h", "5d"])
def test_malformed_durations_are_refused(bad):
    with pytest.raises(ssh.SshError, match="duration"):
        cli.parse_duration(bad, flag="--timeout", cap=86400)


# ------------------------------------------------------------------ job verbs


def test_job_is_a_subcommand_not_a_host():
    args = full_parse(["job", "list", "gpu1"])
    assert args.func is cli.cmd_job and args.job_action == "list"


def test_job_start_takes_its_flags_after_the_host():
    args = full_parse(["job", "start", "gpu1", "--name", "train", "--cwd", "/srv",
                       "--", "python", "train.py"])
    cli.take_back_flags(args, cli.JOB_TAKEBACK)
    assert args.name == "train" and args.cwd == "/srv"
    assert cli.join_command(args.command) == "python train.py"


def test_job_start_reads_a_command_file(tmp_path):
    script = tmp_path / "train.sh"
    script.write_text("echo one\necho two\n", encoding="utf-8")
    args = full_parse(["job", "start", "gpu1", "--cmd-file", str(script)])
    cli.take_back_flags(args, cli.JOB_TAKEBACK)
    assert cli.acquire_command(args) == "echo one\necho two\n"


def test_job_wait_defaults_to_a_short_bounded_wait():
    args = full_parse(["job", "wait", "gpu1", "20261013-120000-ab12"])
    assert args.max_wait == str(cli.DEFAULT_WAIT) and args.since == 0


def test_job_wait_carries_the_offset_and_pattern():
    args = full_parse(["job", "wait", "gpu1", "20261013-120000-ab12",
                       "--since", "4096", "--until", "Traceback|Error", "--max-wait", "12h"])
    assert args.since == 4096 and args.until == "Traceback|Error" and args.max_wait == "12h"


def test_mcp_is_a_subcommand():
    assert full_parse(["mcp"]).func is cli.cmd_mcp


def test_global_flags_work_on_either_side_of_the_subcommand():
    assert full_parse(["--compact", "hosts"]).compact is True
    assert full_parse(["hosts", "--compact"]).compact is True


def test_a_global_flag_given_up_front_is_not_reset_by_the_subparser():
    assert full_parse(["--compact", "test", "gpu1"]).compact is True


# ------------------------------------------------------------------ output


def ns(**kwargs):
    base = dict(raw=False, compact=True, ascii=False, exit_code=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_a_tool_error_exits_two(capsys):
    assert cli.finish(cli.error(ssh.AUTH_FAILED, "nope"), ns()) == cli.EXIT_ERROR


def test_a_failing_remote_command_still_exits_zero_by_default(capsys):
    assert cli.finish({"ok": True, "exit_code": 1}, ns()) == cli.EXIT_OK


def test_exit_code_flag_mirrors_the_remote_status(capsys):
    assert cli.finish({"ok": True, "exit_code": 3}, ns(exit_code=True)) == 3


def test_a_timeout_maps_to_one(capsys):
    assert cli.finish({"ok": True, "exit_code": -1}, ns(exit_code=True)) == cli.EXIT_REMOTE


def test_raw_prints_stdout_instead_of_json(capsys):
    cli.finish({"ok": True, "exit_code": 0, "stdout": "hello\n"}, ns(raw=True))
    assert capsys.readouterr().out == "hello\n"


def test_json_keeps_non_ascii_readable(capsys):
    cli.finish({"ok": True, "stdout": "你好"}, ns())
    assert "你好" in capsys.readouterr().out


def test_json_can_escape_non_ascii(capsys):
    cli.finish({"ok": True, "stdout": "你好"}, ns(ascii=True))
    out = capsys.readouterr().out
    assert "\\u4f60" in out and json.loads(out)["stdout"] == "你好"


# -------------------------------------------------------- command from a file


def test_a_command_file_is_read_as_written(tmp_path):
    path = tmp_path / "script.sh"
    path.write_text("echo one\necho two\n", encoding="utf-8")
    assert cli.read_command_file(str(path), False) == "echo one\necho two\n"


def test_a_byte_order_mark_is_stripped(tmp_path):
    # Windows editors and PowerShell both like to add one.
    path = tmp_path / "bom.sh"
    path.write_bytes("\ufeffecho hi\n".encode("utf-8"))
    assert cli.read_command_file(str(path), False) == "echo hi\n"


def test_carriage_returns_are_normalised(tmp_path):
    # A stray CR inside a here-doc breaks the remote script in confusing ways.
    path = tmp_path / "crlf.sh"
    path.write_bytes(b"echo one\r\necho two\r\n")
    assert cli.read_command_file(str(path), False) == "echo one\necho two\n"


def test_non_ascii_survives_a_command_file(tmp_path):
    path = tmp_path / "cjk.sh"
    path.write_text("echo 你好 🚀\n", encoding="utf-8")
    assert cli.read_command_file(str(path), False) == "echo 你好 🚀\n"


def test_a_missing_command_file_is_reported(tmp_path):
    with pytest.raises(ssh.SshError, match="cannot read command file"):
        cli.read_command_file(str(tmp_path / "nope.sh"), False)


def test_stdin_cannot_carry_the_command_and_its_input_at_once():
    with pytest.raises(ssh.SshError, match="both want"):
        cli.read_command_file("-", True)


def test_cmd_file_may_follow_the_host():
    args = full_parse(["gpu1", "--cmd-file", "x.sh"])
    cli.take_back_flags(args)
    assert args.cmd_file == "x.sh" and args.command == []


# ------------------------------------------------------- byte-exact raw output


class FakeStream:
    """A text stream with a bytes buffer, like the real sys.stdout."""

    def __init__(self):
        self.buffer = io.BytesIO()
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def test_raw_output_is_written_as_bytes():
    # Going through the text layer on Windows would turn every \n into \r\n,
    # rewriting the server's output on its way to a file or a pipe.
    stream = FakeStream()
    cli._write_through(stream, "a\nb\n")
    assert stream.buffer.getvalue() == b"a\nb\n"
    assert stream.text == ""


def test_raw_output_keeps_non_ascii():
    stream = FakeStream()
    cli._write_through(stream, "你好\n")
    assert stream.buffer.getvalue() == "你好\n".encode("utf-8")


def test_raw_output_falls_back_when_there_is_no_buffer():
    class NoBuffer:
        def __init__(self):
            self.text = ""

        def write(self, s):
            self.text += s

    stream = NoBuffer()
    cli._write_through(stream, "hi")
    assert stream.text == "hi"


# ---------------------------------------------- secrets use the canonical alias


def test_secret_rm_uses_the_alias_spelling_from_the_ssh_config(sandbox, monkeypatch):
    # `secret set wsl` must file the password under "WSL", because that is the
    # name --sudo looks up. Filing it under what the user typed would leave sudo
    # failing with nothing to explain why.
    deleted: list[str] = []
    monkeypatch.setattr(cli.secrets, "delete", lambda alias: deleted.append(alias) or True)
    args = cli.build_parser().parse_args(["secret", "rm", "wsl", "--compact"])
    cli.cmd_secret(args)
    assert deleted == ["WSL"]


def test_secret_set_uses_the_canonical_alias(sandbox, monkeypatch):
    stored: dict[str, str] = {}
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "hunter2")
    monkeypatch.setattr(cli.secrets, "set_", lambda alias, value: stored.update({alias: value}))
    args = cli.build_parser().parse_args(["secret", "set", "wsl", "--compact"])
    cli.cmd_secret(args)
    assert stored == {"WSL": "hunter2"}


def test_a_typo_host_is_refused_rather_than_filed_under_a_name_that_does_not_exist(sandbox):
    args = cli.build_parser().parse_args(["secret", "rm", "nosuchhost", "--compact"])
    with pytest.raises(ssh.SshError) as exc:
        cli.cmd_secret(args)
    assert exc.value.code == ssh.HOST_NOT_FOUND
