"""Host resolution, script assembly and failure classification."""

from __future__ import annotations

import pytest

from ssher import config as cfgmod
from ssher import ssh
from ssher.ssh import SshError, Target


def resolve(host):
    return ssh.resolve(host, cfgmod.load_config())


# ------------------------------------------------------------- host lookup


def test_an_alias_is_handed_to_ssh_unchanged(sandbox):
    target = resolve("gpu1")
    assert target.alias == "gpu1"
    assert target.ssh_host == "gpu1"
    # ssh reads the port from its own config; we must not second-guess it.
    assert target.port is None


def test_alias_matching_is_case_insensitive(sandbox):
    assert resolve("wsl").alias == "WSL"
    assert resolve("WSL").alias == "WSL"


def test_an_unknown_alias_says_where_to_look(sandbox):
    with pytest.raises(SshError) as exc:
        resolve("nope")
    assert exc.value.code == ssh.HOST_NOT_FOUND
    assert "ssher hosts" in exc.value.message


def test_a_user_at_host_is_accepted_without_config(sandbox):
    target = resolve("root@10.0.0.1")
    assert target.ssh_host == "root@10.0.0.1"
    assert target.port is None


def test_a_port_is_split_out_for_the_command_line(sandbox):
    target = resolve("deploy@example.com:2222")
    assert target.ssh_host == "deploy@example.com"
    assert target.port == 2222


def test_a_bracketed_ipv6_address_is_unwrapped(sandbox):
    target = resolve("root@[2001:db8::1]:2200")
    assert target.ssh_host == "root@2001:db8::1"
    assert target.port == 2200


def test_a_bare_word_is_not_guessed_to_be_a_hostname(sandbox):
    # "typo" is far more likely a mistyped alias than a real single-label host.
    with pytest.raises(SshError):
        resolve("typo")


def test_a_dotted_name_is_taken_as_a_hostname(sandbox):
    assert resolve("box.example.com").ssh_host == "box.example.com"


def test_an_empty_host_is_rejected(sandbox):
    with pytest.raises(SshError, match="must not be empty"):
        resolve("   ")


def test_config_supplies_init_and_cwd(write_config):
    write_config('[hosts.gpu1]\ninit = "conda activate x"\ncwd = "/srv"\n')
    target = resolve("gpu1")
    assert target.init == "conda activate x"
    assert target.cwd == "/srv"


# ---------------------------------------------------------- path quoting


def test_a_tilde_path_stays_expandable():
    assert ssh.quote_path("~/app") == "~/app"
    assert ssh.quote_path("~") == "~"
    assert ssh.quote_path("~alice/logs") == "~alice/logs"


def test_a_tilde_path_with_spaces_quotes_only_the_tail():
    assert ssh.quote_path("~/my app") == "~/'my app'"


def test_a_tilde_anywhere_else_is_quoted_shut():
    assert ssh.quote_path("/srv/~backup") == "'/srv/~backup'"


def test_an_ordinary_path_with_spaces_is_quoted():
    assert ssh.quote_path("/srv/my app") == "'/srv/my app'"


def test_a_path_cannot_break_out_of_its_quotes():
    quoted = ssh.quote_path("/srv/'; rm -rf /; '")
    assert quoted.startswith("'") and quoted.endswith("'")


# -------------------------------------------------------- script assembly


def target(**kwargs) -> Target:
    kwargs.setdefault("alias", "box")
    kwargs.setdefault("ssh_host", "box")
    return Target(**kwargs)


def test_a_plain_command_passes_straight_through():
    assert ssh.build_script(target(), "ls -la") == "ls -la"


def test_cwd_is_guarded_so_a_bad_path_does_not_run_the_command():
    script = ssh.build_script(target(), "ls", cwd="/srv/app")
    assert script.splitlines()[0] == "cd /srv/app || exit 1"


def test_the_init_line_runs_first_and_is_guarded():
    script = ssh.build_script(target(init="conda activate x"), "python -V")
    assert script.splitlines()[0] == "{ conda activate x ; } || exit 1"


def test_the_request_cwd_beats_the_configured_one():
    script = ssh.build_script(target(cwd="/default"), "ls", cwd="/asked")
    assert "/asked" in script and "/default" not in script


def test_env_values_with_spaces_are_quoted():
    script = ssh.build_script(target(), "run", env={"MSG": "hello world"})
    assert "export MSG='hello world'" in script


def test_request_env_overrides_host_env():
    script = ssh.build_script(target(env={"A": "1"}), "run", env={"A": "2"})
    assert "export A=2" in script and "export A=1" not in script


def test_an_env_value_cannot_break_out_of_its_quotes():
    script = ssh.build_script(target(), "run", env={"A": "it's; rm -rf /"})
    assert script.count("export A=") == 1
    assert script.splitlines()[0].endswith("'")


def test_the_command_is_always_last():
    script = ssh.build_script(
        target(init="setup"), "the-command", cwd="/srv", env={"A": "1"}
    )
    assert script.splitlines()[-1] == "the-command"


def test_a_multiline_script_survives_the_shell_wrapper():
    assert ssh.wrap("cd /srv\nls", "bash -lc", False, False) == "bash -lc 'cd /srv\nls'"


def test_shell_none_runs_the_command_directly():
    assert ssh.wrap("ls", "none", False, False) == "ls"


def test_sudo_without_a_stored_password_never_waits_for_one():
    assert ssh.wrap("ls", "none", True, False) == "sudo -n ls"


def test_sudo_with_a_stored_password_reads_it_from_stdin():
    assert ssh.wrap("ls", "none", True, True) == "sudo -S -p '' ls"


# ----------------------------------------------------------- ssh argv


def test_batch_mode_is_always_on():
    # Without it, ssh would sit at a password prompt no agent can answer.
    assert "BatchMode=yes" in ssh.ssh_argv(target())


def test_host_key_policy_is_left_to_the_user_config_by_default():
    assert "StrictHostKeyChecking=accept-new" not in ssh.ssh_argv(target())


def test_accept_new_is_only_added_on_request():
    argv = ssh.ssh_argv(target(), accept_new=True)
    assert "StrictHostKeyChecking=accept-new" in argv


def test_a_port_becomes_a_flag():
    assert ssh.ssh_argv(target(port=2222))[-3:-1] == ["-p", "2222"]


def test_the_host_is_the_last_argument():
    assert ssh.ssh_argv(target())[-1] == "box"


# --------------------------------------------------- failure classification


def test_a_command_exiting_non_zero_is_not_an_ssh_failure():
    assert ssh.diagnose(1, "boom", target()) is None
    assert ssh.diagnose(127, "not found", target()) is None


def test_a_remote_command_may_exit_255_without_being_blamed_on_ssh():
    # 255 is ssh's own error status, but a program is free to use it too.
    assert ssh.diagnose(255, "my program failed", target()) is None


def test_a_rejected_key_is_reported_as_auth():
    err = ssh.diagnose(255, "box: Permission denied (publickey).", target())
    assert err is not None and err.code == ssh.AUTH_FAILED


def test_an_unknown_host_key_points_at_accept_new():
    err = ssh.diagnose(255, "Host key verification failed.", target())
    assert err.code == ssh.HOST_KEY_UNKNOWN
    assert "--accept-new" in err.message


def test_a_changed_host_key_is_not_something_to_work_around():
    err = ssh.diagnose(255, "@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", target())
    assert err.code == ssh.HOST_KEY_MISMATCH
    assert "Do not work around" in err.message


def test_an_unresolvable_name_is_a_connect_failure():
    err = ssh.diagnose(255, "ssh: Could not resolve hostname box", target())
    assert err.code == ssh.CONNECT_FAILED


def test_a_refused_connection_is_a_connect_failure():
    err = ssh.diagnose(255, "ssh: connect to host box port 22: Connection refused", target())
    assert err.code == ssh.CONNECT_FAILED


def test_the_original_stderr_is_kept_for_the_reader():
    err = ssh.diagnose(255, "box: Permission denied (publickey).", target())
    assert "publickey" in err.extra["stderr"]


# --------------------------------------------------- long scripts on stdin


def test_a_short_script_travels_in_the_arguments():
    assert len("echo hi") <= ssh.ARGV_SCRIPT_LIMIT


def test_the_stdin_shell_reads_instead_of_taking_an_argument():
    assert ssh.stdin_shell("bash -lc") == "bash -ls"
    assert ssh.stdin_shell("sh -c") == "sh -s"


def test_a_shell_with_separate_flags_still_converts():
    assert ssh.stdin_shell("bash -l -c") == "bash -l -s"


def test_a_shell_without_a_c_flag_gets_one_appended():
    assert ssh.stdin_shell("zsh") == "zsh -s"


def test_there_is_no_stdin_shell_when_no_shell_is_used():
    assert ssh.stdin_shell("none") is None
