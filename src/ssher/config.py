"""Configuration.

~/.ssh/config is the source of truth for how to reach a host. ssher's own file
only annotates: a description, a setup line to run first, a default directory.
Anything about the connection itself belongs in the ssh config, where ssh will
read it and where it also works for plain `ssh`.
"""

from __future__ import annotations

import glob
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "timeout": 60,
    "max_output": 65536,
    "shell": "bash -lc",
    "connect_timeout": 20,
    "transfer_timeout": 600,
}

HOST_KEYS = {"description", "tags", "init", "cwd", "env", "timeout"}


class ConfigError(Exception):
    pass


def ssher_home() -> Path:
    return Path(os.environ.get("SSHER_HOME") or (Path.home() / ".ssher"))


def config_path() -> Path:
    return ssher_home() / "config.toml"


def ssh_config_path() -> Path:
    return Path(os.environ.get("SSHER_SSH_CONFIG") or (Path.home() / ".ssh" / "config"))


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    defaults = dict(DEFAULTS)
    for key, value in (raw.get("defaults") or {}).items():
        if key not in DEFAULTS:
            raise ConfigError(
                f"{path}: unknown key in [defaults]: {key}. "
                f"Accepted: {', '.join(sorted(DEFAULTS))}"
            )
        defaults[key] = value

    hosts: dict[str, dict[str, Any]] = {}
    for alias, table in (raw.get("hosts") or {}).items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [hosts.{alias}] must be a table")
        unknown = set(table) - HOST_KEYS
        if unknown:
            raise ConfigError(
                f"{path}: [hosts.{alias}] has unknown key(s): {', '.join(sorted(unknown))}. "
                f"Connection settings such as hostname, user, port and identity_file "
                f"belong in {ssh_config_path()}."
            )
        hosts[alias] = dict(table)

    return {"defaults": defaults, "hosts": hosts, "path": str(path)}


def _tokenize(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" in line and " " not in line.split("=")[0].strip():
        key, _, value = line.partition("=")
    else:
        key, _, value = line.partition(" ")
    key = key.strip()
    if not key:
        return None
    return key.lower(), value.strip().strip('"')


def parse_ssh_config(path: Path | None = None, _depth: int = 0) -> list[dict[str, Any]]:
    """Read enough of the ssh config to list concrete host aliases.

    Only for listing and for telling a typo apart from a real host. ssh itself
    resolves everything that matters when we actually connect.
    """
    path = path or ssh_config_path()
    if not path.exists() or _depth > 5:
        return []

    entries: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    in_match = False

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for line in lines:
        parsed = _tokenize(line)
        if not parsed:
            continue
        key, value = parsed

        if key == "include":
            for pattern in value.split():
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = str(path.parent / pattern)
                for inc in sorted(glob.glob(pattern)):
                    entries.extend(parse_ssh_config(Path(inc), _depth + 1))
            continue

        if key == "match":
            in_match, current = True, []
            continue

        if key == "host":
            in_match, current = False, []
            for alias in value.split():
                if any(ch in alias for ch in "*?!"):
                    continue
                entry = {
                    "alias": alias,
                    "hostname": None,
                    "user": None,
                    "port": None,
                    "source": "ssh_config",
                }
                entries.append(entry)
                current.append(entry)
            continue

        if in_match or not current:
            continue

        field = {"hostname": "hostname", "user": "user", "port": "port"}.get(key)
        if not field:
            continue
        for entry in current:
            if entry.get(field) is None:
                entry[field] = int(value) if field == "port" and value.isdigit() else value

    return entries


def list_hosts(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = cfg if cfg is not None else load_config()
    merged: dict[str, dict[str, Any]] = {}

    for entry in parse_ssh_config():
        merged.setdefault(entry["alias"], dict(entry))

    for alias, table in cfg["hosts"].items():
        entry = merged.get(alias)
        if entry is None:
            entry = {
                "alias": alias,
                "hostname": None,
                "user": None,
                "port": None,
                "source": "ssher",
                "note": "annotated here but not in ~/.ssh/config, so ssh cannot reach it",
            }
            merged[alias] = entry
        else:
            entry["source"] = "ssh_config+ssher"
        for key in ("description", "tags", "init", "cwd"):
            if table.get(key) is not None:
                entry[key] = table[key]

    return sorted(merged.values(), key=lambda e: e["alias"].lower())
