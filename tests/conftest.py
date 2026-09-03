from __future__ import annotations

import pytest

SSH_CONFIG = """\
Host *
    ServerAliveInterval 30

Host WSL
    HostName 172.22.26.26
    User xuan
    IdentityFile ~/.ssh/wsl

Host gpu1 gpu1.alias
    HostName 10.0.0.5
    User trainer
    Port 2222

Match host something
    User ignored

Host bastionite
    HostName jump.example.com
"""


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """An isolated ~/.ssher and ssh config so tests never read the real ones."""
    home = tmp_path / "ssher"
    home.mkdir()
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(SSH_CONFIG, encoding="utf-8")

    monkeypatch.setenv("SSHER_HOME", str(home))
    monkeypatch.setenv("SSHER_SSH_CONFIG", str(ssh_config))

    # Keep the OS keychain out of the tests.
    from ssher import secrets

    monkeypatch.setattr(secrets, "get", lambda alias, kind=secrets.KIND: None)
    monkeypatch.setattr(secrets, "has", lambda alias, kind=secrets.KIND: False)

    return {"home": home, "ssh_config": ssh_config, "tmp": tmp_path}


@pytest.fixture
def write_config(sandbox):
    def _write(text: str):
        path = sandbox["home"] / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return _write
