"""Command line.

The shape an agent uses is `ssher <host> <command>`, with `--b64` when the
command carries anything a shell might chew on. Everything prints one JSON
object and exits 0 when ssher did its job.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import io
import json
import os
import sys
from typing import Any

from . import __version__
from . import config as cfgmod
from . import secrets
from . import ssh
from .guide import GUIDE

EXIT_OK = 0
EXIT_REMOTE = 1
EXIT_ERROR = 2

SUBCOMMANDS = {
    "run", "hosts", "test", "upload", "download", "secret", "config", "agent-guide",
}

CONFIG_TEMPLATE = """# ssher configuration. Everything here is optional.
#
# How to REACH a host belongs in ~/.ssh/config, not here: hostname, user, port,
# IdentityFile, ProxyJump. ssher hands the alias straight to ssh, so whatever
# works with `ssh myhost` works with `ssher myhost`.
#
# This file is for what ssh has no opinion about.

[defaults]
timeout = 60         # seconds before a command is given up on
max_output = 65536   # bytes kept per stream; the middle is dropped
shell = "bash -lc"   # wrapper for each command; "none" runs it directly

# [hosts.gpu1]
# description = "training box"
# tags = ["gpu"]
# init = "source ~/miniconda3/etc/profile.d/conda.sh && conda activate torch"
# cwd = "/srv/app"
# env = { HF_HOME = "/data/hf" }
"""


# ------------------------------------------------------------------ plumbing


def _force_utf8() -> None:
    """UTF-8 out, and no newline translation.

    Without `newline=""`, Windows turns every \\n we print into \\r\\n. That is
    merely untidy in the JSON, but in `--raw` it rewrites the server's output on
    the way past, which would corrupt a redirected log or a binary stream.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", newline="")
            except Exception:
                pass


def error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def emit(payload: dict[str, Any], args: argparse.Namespace) -> None:
    print(json.dumps(
        payload,
        ensure_ascii=getattr(args, "ascii", False),
        indent=None if getattr(args, "compact", False) else 2,
    ))


def _write_through(stream: Any, text: str) -> None:
    """Write bytes, so nothing between here and the pipe rewrites them."""
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        return
    stream.flush()
    buffer.write(text.encode("utf-8", "replace"))
    buffer.flush()


def finish(payload: dict[str, Any], args: argparse.Namespace) -> int:
    if getattr(args, "raw", False) and payload.get("ok"):
        _write_through(sys.stdout, payload.get("stdout", ""))
        if payload.get("stderr"):
            _write_through(sys.stderr, payload["stderr"])
    else:
        emit(payload, args)

    if not payload.get("ok", False):
        return EXIT_ERROR
    if getattr(args, "exit_code", False) or getattr(args, "raw", False):
        code = payload.get("exit_code")
        if isinstance(code, int) and code != 0:
            return EXIT_REMOTE if code < 0 else min(code, 125)
    return EXIT_OK


def decode_command(text: str, is_b64: bool) -> str:
    if not is_b64:
        return text
    try:
        return base64.b64decode(text, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ssh.SshError(
            f"--b64 was given but the value is not valid base64 of UTF-8 text: {exc}",
            ssh.BAD_REQUEST,
        ) from exc


def parse_env(pairs: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in pairs or []:
        name, sep, value = item.partition("=")
        if not sep:
            raise ssh.SshError(f"--env expects NAME=VALUE, got {item!r}", ssh.BAD_REQUEST)
        env[name] = value
    return env


def join_command(parts: list[str]) -> str:
    if parts and parts[0] == "--":
        parts = parts[1:]
    return " ".join(parts).strip()


# The command is collected with REMAINDER so `ssher box ls -la` needs no
# quoting. That also swallows our own flags when they follow the host, so we
# take them back off the front of the remainder. Scanning stops at the first
# non-flag, which leaves a real command starting with a dash alone.
TAKEBACK: dict[str, tuple[str, type | None]] = {
    "--b64": ("b64", None),
    "--cmd-file": ("cmd_file", str),
    "--cwd": ("cwd", str),
    "--env": ("env", list),
    "--timeout": ("timeout", int),
    "--sudo": ("sudo", None),
    "--tty": ("tty", None),
    "--shell": ("shell", str),
    "--stdin": ("stdin", str),
    "--max-output": ("max_output", int),
    "--check": ("check", None),
    "--accept-new": ("accept_new", None),
}


def take_back_flags(args: argparse.Namespace) -> None:
    parts = list(args.command or [])
    while parts:
        name, eq, inline = parts[0].partition("=")
        if name not in TAKEBACK:
            break
        dest, type_ = TAKEBACK[name]
        parts.pop(0)
        if type_ is None:
            setattr(args, dest, True)
            continue
        if eq:
            value = inline
        elif parts:
            value = parts.pop(0)
        else:
            raise ssh.SshError(f"{name} needs a value", ssh.BAD_REQUEST)
        if type_ is int:
            if not value.lstrip("-").isdigit():
                raise ssh.SshError(f"{name} expects a number, got {value!r}", ssh.BAD_REQUEST)
            setattr(args, dest, int(value))
        elif type_ is list:
            setattr(args, dest, list(getattr(args, dest) or []) + [value])
        else:
            setattr(args, dest, value)
    args.command = parts


# ---------------------------------------------------------------- subcommands


def read_command_file(path: str, stdin_taken: bool) -> str:
    """Read the command from a file, or from stdin when the path is "-".

    A file never passes through a shell, so nothing can rewrite it, and it is
    not subject to the command-line length limit. The normalising below is for
    the stdin case on Windows, where PowerShell puts a byte-order mark in front
    of what it pipes and turns every newline into a carriage return pair. A
    stray carriage return inside a here-doc breaks the remote script in ways
    that are miserable to debug.
    """
    if path == "-":
        if stdin_taken:
            raise ssh.SshError(
                "--cmd-file - and --stdin - both want this process's stdin. "
                "Put the command in a real file, or pass the command's input inline.",
                ssh.BAD_REQUEST,
            )
        data = sys.stdin.buffer.read()
    else:
        try:
            data = open(os.path.expanduser(path), "rb").read()
        except OSError as exc:
            raise ssh.SshError(f"cannot read command file: {exc}", ssh.BAD_REQUEST) from exc

    text = data.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def cmd_run(args) -> int:
    take_back_flags(args)
    cfg = cfgmod.load_config()
    target = ssh.resolve(args.host, cfg)
    defaults = cfg["defaults"]

    inline = join_command(args.command)
    if args.cmd_file and inline:
        raise ssh.SshError(
            f"the command came twice: --cmd-file and {inline.split()[0]!r} on the "
            f"command line. Use one.",
            ssh.BAD_REQUEST,
        )
    if args.cmd_file:
        raw = read_command_file(args.cmd_file, args.stdin == "-")
    elif inline:
        raw = inline
    else:
        raise ssh.SshError(
            "nothing to run. Try: ssher myhost 'uname -a'", ssh.BAD_REQUEST
        )
    command = decode_command(raw.strip() if args.b64 else raw, args.b64)

    stdin_text = None
    if args.stdin == "-":
        stdin_text = sys.stdin.buffer.read().decode("utf-8", "replace")
    elif args.stdin is not None:
        stdin_text = args.stdin

    result = ssh.run_command(
        target,
        command,
        cwd=args.cwd,
        env=parse_env(args.env) or None,
        stdin=stdin_text,
        timeout=args.timeout,
        sudo=args.sudo,
        tty=args.tty,
        shell=args.shell or defaults["shell"],
        max_output=args.max_output or defaults["max_output"],
        accept_new=args.accept_new,
        connect_timeout=defaults["connect_timeout"],
        check=args.check,
    )
    return finish(result, args)


def cmd_hosts(args) -> int:
    cfg = cfgmod.load_config()
    entries = cfgmod.list_hosts(cfg)
    for entry in entries:
        if secrets.has(entry["alias"]):
            entry["has_sudo_password"] = True
    return finish(
        {
            "ok": True,
            "hosts": entries,
            "count": len(entries),
            "ssh_config": str(cfgmod.ssh_config_path()),
            "ssher_config": str(cfgmod.config_path()),
        },
        args,
    )


def cmd_test(args) -> int:
    cfg = cfgmod.load_config()
    target = ssh.resolve(args.host, cfg)
    return finish(
        ssh.probe(
            target,
            accept_new=args.accept_new,
            connect_timeout=cfg["defaults"]["connect_timeout"],
            timeout=cfg["defaults"]["timeout"],
        ),
        args,
    )


def cmd_upload(args) -> int:
    return _transfer(args, "upload")


def cmd_download(args) -> int:
    return _transfer(args, "download")


def _transfer(args, direction: str) -> int:
    cfg = cfgmod.load_config()
    target = ssh.resolve(args.host, cfg)
    return finish(
        ssh.transfer(
            target,
            direction=direction,
            local=args.local,
            remote=args.remote,
            recursive=args.recursive,
            timeout=args.timeout or cfg["defaults"]["transfer_timeout"],
            accept_new=args.accept_new,
            connect_timeout=cfg["defaults"]["connect_timeout"],
            mkdir=getattr(args, "mkdir", False),
        ),
        args,
    )


def cmd_secret(args) -> int:
    if args.secret_action == "list":
        stored = [
            {"host": entry["alias"]}
            for entry in cfgmod.list_hosts()
            if secrets.has(entry["alias"])
        ]
        return finish({"ok": True, "sudo_passwords": stored, "count": len(stored)}, args)

    if args.secret_action == "rm":
        removed = secrets.delete(args.host)
        return finish({"ok": True, "host": args.host, "removed": removed}, args)

    try:
        value = getpass.getpass(f"sudo password for {args.host}: ")
    except (EOFError, KeyboardInterrupt):
        return finish(error(ssh.SECRET_ERROR, "cancelled"), args)
    if not value:
        return finish(error(ssh.SECRET_ERROR, "empty value, nothing stored"), args)
    try:
        secrets.set_(args.host, value)
    except secrets.SecretError as exc:
        return finish(error(ssh.SECRET_ERROR, str(exc)), args)
    return finish({"ok": True, "host": args.host, "stored": True}, args)


def cmd_config(args) -> int:
    path = cfgmod.config_path()
    if args.config_action == "path":
        return finish(
            {"ok": True, "ssher_config": str(path), "ssh_config": str(cfgmod.ssh_config_path())},
            args,
        )
    if args.config_action == "init":
        if path.exists() and not args.force:
            return finish(
                error(ssh.BAD_REQUEST, f"{path} already exists. Pass --force to replace it."),
                args,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        return finish({"ok": True, "created": str(path)}, args)
    return finish({"ok": True, **cfgmod.load_config()}, args)


def cmd_guide(args) -> int:
    sys.stdout.write(GUIDE)
    return EXIT_OK


# ------------------------------------------------------------------ assembly

GLOBAL_FLAGS = [
    ("--raw", "raw", "print stdout instead of JSON, and mirror the remote exit code"),
    ("--compact", "compact", "single-line JSON"),
    ("--ascii", "ascii", "escape non-ASCII in the JSON output"),
    ("--exit-code", "exit_code", "exit with the remote command's status"),
]


def _globals_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    for flag, dest, _help in GLOBAL_FLAGS:
        parent.add_argument(
            flag, dest=dest, action="store_true",
            default=argparse.SUPPRESS, help=argparse.SUPPRESS,
        )
    return parent


def add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--b64", action="store_true",
                        help="the command is base64; nothing else can touch it")
    parser.add_argument("--cmd-file", dest="cmd_file", metavar="PATH",
                        help="read the command from a file, or from stdin when PATH is -")
    parser.add_argument("--cwd", help="directory to run in")
    parser.add_argument("--env", action="append", metavar="NAME=VALUE")
    parser.add_argument("--timeout", type=int, metavar="SECONDS")
    parser.add_argument("--sudo", action="store_true",
                        help="run under sudo; password from the keychain if stored")
    parser.add_argument("--tty", action="store_true", help="allocate a terminal")
    parser.add_argument("--shell", help="wrapper shell, or 'none'")
    parser.add_argument("--stdin", metavar="TEXT", help="text for stdin, or - to pass ours")
    parser.add_argument("--max-output", type=int, dest="max_output")
    parser.add_argument("--check", action="store_true",
                        help="report ok:false when the exit code is non-zero")
    parser.add_argument("--accept-new", action="store_true",
                        help="record an unknown host key on first connect")
    parser.add_argument("host")
    parser.add_argument("command", nargs=argparse.REMAINDER)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ssher",
        usage="ssher <host> <command>...  |  ssher <subcommand> ...",
        description="Run a command on a remote server over SSH. Prints JSON.",
        epilog="`ssher agent-guide` prints usage notes written for an agent.",
    )
    parser.add_argument("--version", action="version", version=f"ssher {__version__}")
    for flag, dest, help_text in GLOBAL_FLAGS:
        parser.add_argument(flag, dest=dest, action="store_true", default=False, help=help_text)

    common = _globals_parent()
    _sub = parser.add_subparsers(dest="command_name")

    def add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        kwargs.setdefault("parents", [common])
        # argparse would otherwise prefix each subcommand's usage line with the
        # custom top-level usage string, which reads as nonsense.
        kwargs.setdefault("prog", f"ssher {name}")
        return _sub.add_parser(name, **kwargs)

    p = add("run", help="run a command; the same as omitting the word 'run'")
    add_run_flags(p)
    p.set_defaults(func=cmd_run)

    p = add("hosts", help="list every host ssher can see")
    p.set_defaults(func=cmd_hosts)

    p = add("test", help="connect once and report what is on the other end")
    p.add_argument("--accept-new", action="store_true")
    p.add_argument("host")
    p.set_defaults(func=cmd_test)

    for name, help_text in (("upload", "copy a local path to the server"),
                            ("download", "copy a path from the server")):
        p = add(name, help=help_text)
        p.add_argument("host")
        if name == "upload":
            p.add_argument("local")
            p.add_argument("remote")
        else:
            p.add_argument("remote")
            p.add_argument("local")
        p.add_argument("-r", "--recursive", action="store_true")
        p.add_argument("--timeout", type=int, metavar="SECONDS")
        p.add_argument("--accept-new", action="store_true")
        if name == "upload":
            p.add_argument("--mkdir", action="store_true",
                           help="create the remote parent directory first")
        p.set_defaults(func=cmd_upload if name == "upload" else cmd_download)

    p = add("secret", help="store a sudo password in the OS keychain")
    ssub = p.add_subparsers(dest="secret_action", required=True)
    for name in ("set", "rm"):
        sp = ssub.add_parser(name, parents=[common])
        sp.add_argument("host")
    ssub.add_parser("list", parents=[common])
    p.set_defaults(func=cmd_secret, host=None)

    p = add("config", help="show or create ~/.ssher/config.toml")
    csub = p.add_subparsers(dest="config_action", required=True)
    cp = csub.add_parser("init", parents=[common])
    cp.add_argument("--force", action="store_true")
    csub.add_parser("path", parents=[common])
    csub.add_parser("show", parents=[common])
    p.set_defaults(func=cmd_config, force=False)

    p = add("agent-guide", help="print usage notes written for an agent")
    p.set_defaults(func=cmd_guide)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)

    # `ssher myhost uname -a` with no subcommand word. Anything that is not a
    # known subcommand or an option is taken to be a host.
    first = next((a for a in argv if not a.startswith("-")), None)
    if first is not None and first not in SUBCOMMANDS:
        argv.insert(argv.index(first), "run")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK

    try:
        return args.func(args)
    except ssh.SshError as exc:
        emit(error(exc.code, exc.message, **exc.extra), args)
        return EXIT_ERROR
    except cfgmod.ConfigError as exc:
        emit(error(ssh.BAD_REQUEST, str(exc)), args)
        return EXIT_ERROR
    except secrets.SecretError as exc:
        emit(error(ssh.SECRET_ERROR, str(exc)), args)
        return EXIT_ERROR
    except OSError as exc:
        emit(error(ssh.BAD_REQUEST, str(exc)), args)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
