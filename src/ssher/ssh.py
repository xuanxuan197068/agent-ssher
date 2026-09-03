"""Everything that talks to a server.

We shell out to the OpenSSH client rather than speaking the protocol ourselves.
That is not a shortcut: ssh already reads ~/.ssh/config exactly the way the user
expects, already handles ProxyJump, agents, keys and host-key policy, and is
already installed. Driving it costs less time per call than importing an SSH
library would, and leaves nothing running in the background.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from . import config as cfgmod
from . import output as outmod
from . import secrets

# Error codes an agent may branch on.
BAD_REQUEST = "BAD_REQUEST"
HOST_NOT_FOUND = "HOST_NOT_FOUND"
AUTH_FAILED = "AUTH_FAILED"
CONNECT_FAILED = "CONNECT_FAILED"
HOST_KEY_UNKNOWN = "HOST_KEY_UNKNOWN"
HOST_KEY_MISMATCH = "HOST_KEY_MISMATCH"
TIMEOUT = "TIMEOUT"
EXEC_FAILED = "EXEC_FAILED"
TRANSFER_FAILED = "TRANSFER_FAILED"
SECRET_ERROR = "SECRET_ERROR"
NO_SSH = "NO_SSH"

SSH_FAILED = 255  # ssh's own "I could not do it" status


class SshError(Exception):
    def __init__(self, message: str, code: str = EXEC_FAILED, **extra: Any):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra


@dataclass
class Target:
    """A host, reduced to what the ssh command line needs."""

    alias: str          # canonical name, used for config and keyring lookups
    ssh_host: str       # the argument handed to ssh
    port: int | None = None
    init: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout: int = 60


# --------------------------------------------------------------- host lookup

ADHOC_RE = re.compile(r"^(?:(?P<user>[^@\s]+)@)?(?P<host>\[[^\]]+\]|[^@:\s]+)(?::(?P<port>\d+))?$")


def resolve(host: str, cfg: dict[str, Any]) -> Target:
    host = (host or "").strip()
    if not host:
        raise SshError("host must not be empty", BAD_REQUEST)

    defaults = cfg["defaults"]
    aliases = {entry["alias"].lower(): entry["alias"] for entry in cfgmod.parse_ssh_config()}
    aliases.update({name.lower(): name for name in cfg["hosts"]})

    canonical = aliases.get(host.lower())
    if canonical is None:
        match = ADHOC_RE.match(host)
        if not match or ("@" not in host and ":" not in host and "." not in host):
            raise SshError(
                f"unknown host {host!r}. It is not in {cfgmod.ssh_config_path()}. "
                f"Run `ssher hosts` to see what is available, or pass user@hostname.",
                HOST_NOT_FOUND,
            )
        port = match.group("port")
        bare = match.group("host").strip("[]")
        user = match.group("user")
        return Target(
            alias=host,
            ssh_host=f"{user}@{bare}" if user else bare,
            port=int(port) if port else None,
            timeout=defaults["timeout"],
        )

    table = cfg["hosts"].get(canonical, {})
    return Target(
        alias=canonical,
        ssh_host=canonical,
        port=None,  # ssh config owns the port for a named host
        init=table.get("init"),
        cwd=table.get("cwd"),
        env=table.get("env") or None,
        timeout=int(table.get("timeout") or defaults["timeout"]),
    )


# ----------------------------------------------------------- script assembly

TILDE_RE = re.compile(r"^~[A-Za-z0-9_.-]*(?:/|$)")


def quote_path(path: str) -> str:
    """Quote a path for the remote shell while leaving a leading ~ expandable.

    shlex.quote would wrap the whole thing in single quotes, and bash does not
    expand a tilde inside quotes, so `cd '~/app'` goes looking for a directory
    literally named "~".
    """
    if not TILDE_RE.match(path):
        return shlex.quote(path)
    head, sep, rest = path.partition("/")
    return head + (sep + shlex.quote(rest) if rest else "")


def build_script(
    target: Target,
    cmd: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Assemble what the remote shell will run.

    The host's init line goes first so anything it puts on PATH is visible to
    the rest. init and cd are guarded, because a command that runs in the wrong
    directory or the wrong environment is worse than one that does not run.
    """
    lines: list[str] = []
    if target.init:
        lines.append(f"{{ {target.init} ; }} || exit 1")
    effective_cwd = cwd if cwd is not None else target.cwd
    if effective_cwd:
        lines.append(f"cd {quote_path(effective_cwd)} || exit 1")
    merged = dict(target.env or {})
    merged.update(env or {})
    for name, value in merged.items():
        lines.append(f"export {name}={shlex.quote(str(value))}")
    lines.append(cmd)
    return "\n".join(lines)


def wrap(script: str, shell: str, sudo: bool, sudo_password: bool) -> str:
    base = script if shell in ("none", "", None) else f"{shell} {shlex.quote(script)}"
    if not sudo:
        return base
    return f"sudo {'-S -p ' + shlex.quote('') if sudo_password else '-n'} {base}"


# A command line has a hard length limit, about 32 KB on Windows. Past this we
# stop putting the script in the arguments and feed it to the remote shell's
# standard input instead, which has no such limit.
ARGV_SCRIPT_LIMIT = 8000


def stdin_shell(shell: str) -> str | None:
    """Turn a "run this argument" shell into a "read your input" one.

    "bash -lc" becomes "bash -ls", "sh -c" becomes "sh -s". Returns None when
    there is no shell to read anything, which is the "none" case.
    """
    if shell in ("none", "", None):
        return None
    parts = shell.split()
    if len(parts) > 1 and parts[-1].startswith("-") and parts[-1].endswith("c"):
        parts[-1] = parts[-1][:-1] + "s"
    else:
        parts.append("-s")
    return " ".join(parts)


# ------------------------------------------------------------- ssh invocation


def _no_window() -> int:
    # Without this, every ssh subprocess started from a windowed parent flashes
    # its own console on Windows.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def ssh_argv(
    target: Target,
    *,
    accept_new: bool = False,
    connect_timeout: int | None = None,
    tty: bool = False,
    extra: list[str] | None = None,
) -> list[str]:
    argv = ["ssh", "-tt" if tty else "-T", "-o", "BatchMode=yes"]
    if connect_timeout:
        argv += ["-o", f"ConnectTimeout={connect_timeout}"]
    if accept_new:
        # Only ever on request. Passing it always would silently override a
        # `StrictHostKeyChecking yes` the user set on purpose.
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    if target.port:
        argv += ["-p", str(target.port)]
    argv += extra or []
    argv.append(target.ssh_host)
    return argv


SSH_DIAGNOSTICS = [
    (re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED", re.I), HOST_KEY_MISMATCH,
     "the server's host key does not match ~/.ssh/known_hosts. Do not work around "
     "this unless you know the server was rebuilt; if it was, remove its line from "
     "known_hosts yourself."),
    (re.compile(r"Host key verification failed|No (?:RSA |ECDSA |ED25519 )?host key is known",
                re.I), HOST_KEY_UNKNOWN,
     "this host is not in ~/.ssh/known_hosts yet. Re-run with --accept-new to "
     "record its key, or connect once with plain ssh first."),
    (re.compile(r"Permission denied|Too many authentication failures", re.I), AUTH_FAILED,
     "the server rejected every key offered. ssher runs ssh in batch mode, so it "
     "never falls back to a password prompt. Add a key, or fix IdentityFile."),
    (re.compile(r"Could not resolve hostname|Name or service not known", re.I), CONNECT_FAILED,
     "the hostname does not resolve."),
    (re.compile(r"Connection refused|Connection (?:timed out|closed)|No route to host|"
                r"Network is unreachable|Operation timed out", re.I), CONNECT_FAILED,
     "the server did not accept a connection."),
    (re.compile(r"Bad configuration option|Bad port|line \d+: ", re.I), BAD_REQUEST,
     "ssh rejected the configuration."),
]


def diagnose(returncode: int, stderr: str, target: Target) -> SshError | None:
    """Tell an ssh-level failure apart from a command that simply exited non-zero.

    ssh reports its own failures as 255, but a remote command is free to exit
    255 too, so the status alone is not enough. We only claim it was ssh when
    ssh also said something we recognise.
    """
    if returncode != SSH_FAILED:
        return None
    for pattern, code, explanation in SSH_DIAGNOSTICS:
        if pattern.search(stderr):
            return SshError(f"{target.alias}: {explanation}", code, stderr=stderr.strip())
    return None


def _run(argv: list[str], *, input_: bytes | None, timeout: int) -> tuple[int, bytes, bytes, bool]:
    try:
        proc = subprocess.run(
            argv,
            input=input_,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=_no_window(),
        )
        return proc.returncode, proc.stdout or b"", proc.stderr or b"", False
    except subprocess.TimeoutExpired as exc:
        return -1, exc.stdout or b"", exc.stderr or b"", True
    except OSError as exc:
        # Windows reports an over-long command line with an error code Python
        # turns into FileNotFoundError, so "not found" is not to be believed
        # until we have actually looked for the program.
        if shutil.which(argv[0]) is None:
            raise SshError(
                f"{argv[0]} is not on PATH. ssher drives the OpenSSH client; install "
                f"it or add it to PATH.",
                NO_SSH,
            ) from exc
        raise SshError(f"could not start {argv[0]}: {exc}", EXEC_FAILED) from exc


# ------------------------------------------------------------------- the ops


def run_command(
    target: Target,
    cmd: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout: int | None = None,
    sudo: bool = False,
    tty: bool = False,
    shell: str = "bash -lc",
    max_output: int = 65536,
    accept_new: bool = False,
    connect_timeout: int = 20,
    check: bool = False,
) -> dict[str, Any]:
    timeout = timeout if timeout is not None else target.timeout

    sudo_password = secrets.get(target.alias, "sudo") if sudo else None
    script = build_script(target, cmd, cwd, env)

    payload = b""
    if sudo_password:
        payload += (sudo_password + "\n").encode("utf-8")
    if stdin is not None:
        payload += stdin.encode("utf-8")

    script_bytes = len(script.encode("utf-8"))
    if script_bytes <= ARGV_SCRIPT_LIMIT:
        remote = wrap(script, shell, sudo, bool(sudo_password))
    else:
        # Too long for a command line. Hand it to the remote shell on stdin,
        # which means stdin is no longer free for anything else.
        reader = stdin_shell(shell)
        if reader is None:
            raise SshError(
                f"the script is {script_bytes} bytes, too long for a command line, "
                f"and --shell none leaves no shell to read it. Use a real shell.",
                BAD_REQUEST,
            )
        if payload:
            raise SshError(
                f"the script is {script_bytes} bytes, so it has to travel on stdin, "
                f"but stdin is already carrying "
                f"{'the sudo password' if sudo_password else 'the command input'}. "
                f"Upload the script with `ssher upload` and run it by path instead.",
                BAD_REQUEST,
            )
        remote = f"sudo -n {reader}" if sudo else reader
        payload = script.encode("utf-8")

    argv = ssh_argv(
        target, accept_new=accept_new, connect_timeout=connect_timeout, tty=tty
    ) + [remote]

    started = time.monotonic()
    code, out, err, timed_out = _run(argv, input_=payload or None, timeout=timeout)
    duration_ms = int((time.monotonic() - started) * 1000)

    stdout, out_cut = outmod.clean(out, max_output, ansi=tty)
    stderr, err_cut = outmod.clean(err, max_output, ansi=tty)

    if timed_out:
        return {
            "ok": True,
            "host": target.alias,
            "exit_code": -1,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "truncated": out_cut or err_cut,
            "timed_out": True,
            "hint": (
                f"no answer within {timeout}s, so ssh was killed. The remote command "
                f"usually dies with it. For work that takes longer, start it detached: "
                f"setsid nohup <cmd> > ~/out.log 2>&1 < /dev/null &"
            ),
        }

    failure = diagnose(code, stderr, target)
    if failure is not None:
        raise failure

    result: dict[str, Any] = {
        "ok": True,
        "host": target.alias,
        "exit_code": code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "truncated": out_cut or err_cut,
        "timed_out": False,
    }
    if check and code != 0:
        result["ok"] = False
        result["error"] = {"code": EXEC_FAILED, "message": f"command exited with {code}"}
    return result


def probe(target: Target, **kwargs: Any) -> dict[str, Any]:
    """The `test` op: connect once and report what is on the other end."""
    started = time.monotonic()
    result = run_command(
        target,
        'echo "__U__$(uname -a 2>/dev/null)"; echo "__H__$HOME"; '
        'echo "__S__$SHELL"; echo "__B__$(command -v bash || echo none)"',
        shell="bash -lc",
        **kwargs,
    )
    fields = {}
    for line in result["stdout"].splitlines():
        for key in ("U", "H", "S", "B"):
            token = f"__{key}__"
            if line.startswith(token):
                fields[key] = line[len(token):].strip()
    return {
        "ok": True,
        "host": target.alias,
        "ssh_host": target.ssh_host,
        "round_trip_ms": int((time.monotonic() - started) * 1000),
        "uname": fields.get("U", ""),
        "home": fields.get("H", ""),
        "shell": fields.get("S", ""),
        "has_bash": fields.get("B", "none") != "none",
    }


MISSING_DIR_RE = re.compile(
    r"No such file|path canonicalization failed|Not a directory", re.I
)


def transfer(
    target: Target,
    *,
    direction: str,
    local: str,
    remote: str,
    recursive: bool = False,
    timeout: int = 600,
    accept_new: bool = False,
    connect_timeout: int = 20,
    mkdir: bool = False,
) -> dict[str, Any]:
    """upload or download, via scp."""
    local_path = os.path.expanduser(local)
    # scp hands the remote path to a remote shell, so it needs remote quoting.
    remote_spec = f"{target.ssh_host}:{quote_path(remote)}"

    argv = ["scp", "-q", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}"]
    if accept_new:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    if target.port:
        argv += ["-P", str(target.port)]
    if recursive:
        argv.append("-r")

    parent = posixpath.dirname(remote)

    if direction == "upload":
        if not os.path.exists(local_path):
            raise SshError(f"local path does not exist: {local_path}", BAD_REQUEST)
        if os.path.isdir(local_path) and not recursive:
            raise SshError(f"{local_path} is a directory; pass --recursive", BAD_REQUEST)
        if mkdir and parent:
            run_command(
                target,
                f"mkdir -p {quote_path(parent)}",
                timeout=min(timeout, 60),
                accept_new=accept_new,
                connect_timeout=connect_timeout,
                check=True,
            )
        argv += [local_path, remote_spec]
    else:
        parent = os.path.dirname(os.path.abspath(local_path))
        os.makedirs(parent, exist_ok=True)
        argv += [remote_spec, local_path]

    started = time.monotonic()
    code, out, err, timed_out = _run(argv, input_=None, timeout=timeout)
    stderr = outmod.decode(err).strip()

    if timed_out:
        raise SshError(f"transfer did not finish within {timeout}s", TIMEOUT)
    if code != 0:
        failure = diagnose(SSH_FAILED, stderr, target)
        if failure is not None:
            raise failure
        hint = ""
        if direction == "upload" and not mkdir and parent and MISSING_DIR_RE.search(stderr):
            # scp will not create the destination's parent, and its own message
            # does not say which path it could not find.
            hint = (
                f" The remote directory {parent} probably does not exist; scp will "
                f"not create it. Re-run with --mkdir."
            )
        raise SshError(
            f"scp failed with status {code}: {stderr or 'no message'}.{hint}",
            TRANSFER_FAILED,
        )

    files, size = _measure(local_path)
    return {
        "ok": True,
        "host": target.alias,
        "direction": direction,
        "local": local_path,
        "remote": remote,
        "files": files,
        "bytes": size,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _measure(path: str) -> tuple[int, int]:
    if os.path.isfile(path):
        return 1, os.path.getsize(path)
    if not os.path.isdir(path):
        return 0, 0
    files = 0
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return files, total
