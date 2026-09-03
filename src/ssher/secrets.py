"""Sudo passwords, kept in the OS keychain.

Login passwords are deliberately not supported: ssher runs ssh in batch mode, so
a host that cannot authenticate with a key fails immediately instead of hanging
on a prompt an agent cannot answer. Give such a host a key. A sudo password is
different, because it is asked for by the remote shell after login and there is
no key-based way around it.
"""

from __future__ import annotations

SERVICE = "ssher"
KIND = "sudo"


class SecretError(Exception):
    pass


def _keyring():
    try:
        import keyring
    except Exception as exc:  # pragma: no cover - depends on the host
        raise SecretError(f"keyring is unavailable: {exc}") from exc
    return keyring


def key_for(alias: str, kind: str = KIND) -> str:
    return f"{alias}:{kind}"


def get(alias: str, kind: str = KIND) -> str | None:
    try:
        return _keyring().get_password(SERVICE, key_for(alias, kind))
    except Exception:
        return None


def set_(alias: str, value: str, kind: str = KIND) -> None:
    _keyring().set_password(SERVICE, key_for(alias, kind), value)


def delete(alias: str, kind: str = KIND) -> bool:
    try:
        _keyring().delete_password(SERVICE, key_for(alias, kind))
        return True
    except Exception:
        return False


def has(alias: str, kind: str = KIND) -> bool:
    return get(alias, kind) is not None
