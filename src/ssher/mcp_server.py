"""ssher as an MCP server, for agents whose harness speaks MCP.

The CLI makes an agent write a shell command line, and on Windows that line is
parsed by PowerShell before ssher sees it, which is where quoting goes wrong.
Here the command arrives as a string field in a JSON request instead, so there
is no shell on this side to rewrite it, and --b64 and --cmd-file have no reason
to exist. Everything else is the same code the CLI runs.

Every MCP client puts a ceiling on how long one tool call may take, and some of
those ceilings are a minute. So no tool here blocks longer than
SSHER_MCP_CALL_LIMIT seconds (default 50): a longer `run` is cut short with a
hint to use a job, and `job_wait` simply returns "running" for the agent to
call again. Raise the limit in the server's registration for clients that allow
longer calls, keeping it a little under their own limit.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any

from pydantic import Field

from mcp.server.mcpserver import MCPServer

from . import __version__
from . import config as cfgmod
from . import jobs
from . import secrets
from . import ssh

CALL_LIMIT_ENV = "SSHER_MCP_CALL_LIMIT"
DEFAULT_CALL_LIMIT = 50

INSTRUCTIONS = """\
Run commands on remote servers over SSH. Hosts are the aliases in the user's
~/.ssh/config (call `hosts` to list them) or user@hostname:port.

Send commands as plain text in `cmd`, exactly as you would type them in bash on
the server. Quotes, $VARS, backticks, here-docs and newlines all arrive intact;
do not escape or base64 anything.

Each `run` is a fresh login shell: cd and export do not carry over. Use the
`cwd` and `env` parameters, or chain commands with &&.

Anything that may take more than about a minute belongs in a job: `job_start`
returns at once, then call `job_wait` until state is no longer "running",
passing back the log_offset it returns as since_offset each time. job_wait
returns within a second of the job finishing, so there is no interval to pick.
"""

log = logging.getLogger("ssher.mcp")


def call_limit() -> int:
    raw = os.environ.get(CALL_LIMIT_ENV, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return min(int(raw), 24 * 3600)
    return DEFAULT_CALL_LIMIT


def call_budget(limit: int) -> int:
    """How long a tool may spend on the server and still reply inside `limit`.

    Work that ends exactly at the limit arrives after it, once the handshake
    and the reply are added, so leave a fifth of the limit, between one and
    five seconds, for those.
    """
    headroom = max(1, min(5, limit // 5))
    return max(0, limit - headroom)


def _fail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def _guarded(fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Turn our own errors into the same {ok: false, error} shape the CLI prints."""
    try:
        return fn(*args, **kwargs)
    except ssh.SshError as exc:
        return _fail(exc.code, exc.message, **exc.extra)
    except cfgmod.ConfigError as exc:
        return _fail(ssh.BAD_REQUEST, str(exc))
    except secrets.SecretError as exc:
        return _fail(ssh.SECRET_ERROR, str(exc))
    except OSError as exc:
        return _fail(ssh.BAD_REQUEST, str(exc))


def _context(host: str) -> tuple[ssh.Target, dict[str, Any]]:
    cfg = cfgmod.load_config()
    return ssh.resolve(host, cfg), cfg["defaults"]


server = MCPServer(
    name="ssher",
    title="SSH for agents",
    instructions=INSTRUCTIONS,
    version=__version__,
    log_level="WARNING",
)

Host = Annotated[
    str,
    Field(description="An alias from ~/.ssh/config, as listed by `hosts`, or user@hostname:port."),
]


# ---------------------------------------------------------------- discovery


@server.tool()
def hosts() -> dict[str, Any]:
    """List every host you can reach, with the user and hostname each one maps to."""
    def body() -> dict[str, Any]:
        cfg = cfgmod.load_config()
        entries = cfgmod.list_hosts(cfg)
        for entry in entries:
            if secrets.has(entry["alias"]):
                entry["has_sudo_password"] = True
        return {"ok": True, "hosts": entries, "count": len(entries)}

    return _guarded(body)


@server.tool()
def test(
    host: Host,
    accept_new: Annotated[bool, Field(
        description="Record the host key on a first connection. Use only after a "
                    "HOST_KEY_UNKNOWN error."
    )] = False,
) -> dict[str, Any]:
    """Connect once and report uname, home directory, shell and round-trip time.

    Use it first on a host you have not touched, or to tell a dead host from a
    failing command.
    """
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        return ssh.probe(
            target,
            accept_new=accept_new,
            connect_timeout=defaults["connect_timeout"],
            timeout=max(1, min(defaults["timeout"], call_budget(call_limit()))),
        )

    return _guarded(body)


# ---------------------------------------------------------------------- run


@server.tool()
def run(
    host: Host,
    cmd: Annotated[str, Field(
        description="The command, as plain bash. Multi-line scripts, quotes and "
                    "$VARS are fine as they are; do not escape anything."
    )],
    cwd: Annotated[str | None, Field(
        description="Directory to run in. A leading ~ means the remote home."
    )] = None,
    env: Annotated[dict[str, str] | None, Field(
        description="Environment variables to set first."
    )] = None,
    timeout_seconds: Annotated[int | None, Field(
        description="SECONDS, not milliseconds. Defaults to the host's timeout, and is "
                    "capped at the server's per-call limit. Longer work: use job_start.",
        ge=1,
    )] = None,
    stdin: Annotated[str | None, Field(description="Text to feed to the command's stdin.")] = None,
    sudo: Annotated[bool, Field(
        description="Run under sudo, using the password the user stored with "
                    "`ssher secret set`; without one, sudo fails rather than prompting."
    )] = False,
    check: Annotated[bool, Field(
        description="Report ok:false when the command exits non-zero."
    )] = False,
    max_output_bytes: Annotated[int | None, Field(
        description="Cap per stream; the middle of longer output is dropped. Default 65536.",
        ge=256,
    )] = None,
    accept_new: Annotated[bool, Field(
        description="Record the host key on a first connection."
    )] = False,
) -> dict[str, Any]:
    """Run a command and wait for it. Returns exit_code, stdout and stderr.

    ok:false means ssher could not do it (unreachable, bad host, auth). ok:true
    with a non-zero exit_code means the command ran and failed, which is a
    normal result. timed_out:true means it was stopped at the time limit; start
    long work with job_start instead.
    """
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        limit = call_limit()
        wanted = timeout_seconds if timeout_seconds is not None else target.timeout
        used = max(1, min(wanted, call_budget(limit)))
        result = ssh.run_command(
            target,
            cmd,
            cwd=cwd,
            env=env,
            stdin=stdin,
            timeout=used,
            sudo=sudo,
            shell=defaults["shell"],
            max_output=max_output_bytes or defaults["max_output"],
            accept_new=accept_new,
            connect_timeout=defaults["connect_timeout"],
            check=check,
        )
        # Only worth saying when it overrides a number the agent chose; the
        # host's own default being capped is what the hint below is for.
        if timeout_seconds is not None and used < wanted:
            result["timeout_seconds_used"] = used
        if result.get("timed_out"):
            asked = "asked for" if timeout_seconds is not None else "the host's timeout is"
            result["hint"] = (
                f"stopped after {used}s"
                + (f" ({asked} {wanted}s, but one call may last at most {limit}s)"
                   if used < wanted else "")
                + ". Start it with job_start and collect it with job_wait instead."
            )
        return result

    return _guarded(body)


# ----------------------------------------------------------------- transfers


@server.tool()
def upload(
    host: Host,
    local: Annotated[str, Field(description="Local file or directory, on this machine.")],
    remote: Annotated[str, Field(description="Destination path on the server.")],
    recursive: Annotated[bool, Field(description="Required to copy a directory.")] = False,
    mkdir: Annotated[bool, Field(
        description="Create the destination's parent directory first."
    )] = False,
) -> dict[str, Any]:
    """Copy a local file or directory to the server, over scp.

    To change a remote file, it is usually simplest to write the new version
    locally and upload it.
    """
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        return ssh.transfer(
            target, direction="upload", local=local, remote=remote,
            recursive=recursive, mkdir=mkdir,
            timeout=max(1, min(defaults["transfer_timeout"], call_budget(call_limit()))),
            connect_timeout=defaults["connect_timeout"],
        )

    return _guarded(body)


@server.tool()
def download(
    host: Host,
    remote: Annotated[str, Field(description="Path on the server.")],
    local: Annotated[str, Field(description="Destination on this machine.")],
    recursive: Annotated[bool, Field(description="Required to copy a directory.")] = False,
) -> dict[str, Any]:
    """Copy a file or directory from the server to this machine, over scp."""
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        return ssh.transfer(
            target, direction="download", local=local, remote=remote,
            recursive=recursive,
            timeout=max(1, min(defaults["transfer_timeout"], call_budget(call_limit()))),
            connect_timeout=defaults["connect_timeout"],
        )

    return _guarded(body)


# ---------------------------------------------------------------------- jobs

JobId = Annotated[str, Field(description="The job_id that job_start returned.")]


@server.tool()
def job_start(
    host: Host,
    cmd: Annotated[str, Field(
        description="The command or script, as plain bash, exactly as for `run`."
    )],
    cwd: Annotated[str | None, Field(description="Directory to run in.")] = None,
    env: Annotated[dict[str, str] | None, Field(
        description="Environment variables to set first."
    )] = None,
    name: Annotated[str | None, Field(
        description="A short label, shown by job_list and folded into the job id."
    )] = None,
) -> dict[str, Any]:
    """Start a long-running command detached on the server. Returns a job_id at once.

    The job keeps running if this connection, the agent, or this machine goes
    away; its output goes to a log on the server. Follow it with job_wait.
    """
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        return jobs.start(
            target, cmd, cwd=cwd, env=env, name=name,
            connect_timeout=defaults["connect_timeout"],
        )

    return _guarded(body)


@server.tool()
def job_wait(
    host: Host,
    job_id: JobId,
    max_wait_seconds: Annotated[int | None, Field(
        description="Longest to block, in seconds. Defaults to, and is capped at, the "
                    "server's per-call limit. Pass 0 to check without waiting.",
        ge=0,
    )] = None,
    since_offset: Annotated[int, Field(
        description="Return only log output after this byte offset. Pass the "
                    "log_offset from the previous job_wait to get just what is new.",
        ge=0,
    )] = 0,
    until: Annotated[str | None, Field(
        description="A regular expression. Also return as soon as the new output "
                    "matches it, for example 'Traceback|Error' or 'listening on'."
    )] = None,
    max_output_bytes: Annotated[int, Field(
        description="At most this much new output is returned, the most recent part.",
        ge=256,
    )] = jobs.DEFAULT_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Wait for a job, and return its state and the output it printed since since_offset.

    Blocks on the server and returns within a second of the job ending, so there
    is no polling interval to choose. state is running, exited, killed or gone.
    While it is running, call again with since_offset set to the log_offset
    returned. exit_code is set once it has exited.
    """
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        # The remote wait has to end a little before the call limit, so that
        # the handshake and the reply still fit inside it.
        wait_for = call_budget(call_limit())
        if max_wait_seconds is not None:
            wait_for = min(max_wait_seconds, wait_for)
        return jobs.wait(
            target, job_id,
            max_wait=wait_for, since_offset=since_offset, until=until,
            max_output=max_output_bytes,
            connect_timeout=defaults["connect_timeout"],
        )

    return _guarded(body)


@server.tool()
def job_kill(host: Host, job_id: JobId) -> dict[str, Any]:
    """Stop a job and every process it started."""
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        return jobs.kill(target, job_id, connect_timeout=defaults["connect_timeout"])

    return _guarded(body)


@server.tool()
def job_list(host: Host) -> dict[str, Any]:
    """List the jobs on a host, newest first, with state, exit code and run time."""
    def body() -> dict[str, Any]:
        target, defaults = _context(host)
        return jobs.listing(target, connect_timeout=defaults["connect_timeout"])

    return _guarded(body)


def serve() -> None:
    # stdout carries the protocol and nothing else; anything we log goes to stderr.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    ssh.use_spelling(ssh.MCP_SPELLING)
    server.run("stdio")
