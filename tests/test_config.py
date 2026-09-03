from __future__ import annotations

import pytest

from ssher import config as cfgmod


def test_ssh_config_lists_concrete_hosts_only(sandbox):
    entries = {e["alias"]: e for e in cfgmod.parse_ssh_config()}
    assert set(entries) == {"WSL", "gpu1", "gpu1.alias", "bastionite"}
    assert entries["gpu1"]["hostname"] == "10.0.0.5"
    assert entries["gpu1"]["port"] == 2222
    assert entries["WSL"]["user"] == "xuan"


def test_match_block_does_not_leak_into_previous_host(sandbox):
    entries = {e["alias"]: e for e in cfgmod.parse_ssh_config()}
    assert entries["gpu1"]["user"] == "trainer"
    assert entries["bastionite"]["user"] is None


def test_include_pulls_in_another_file(sandbox):
    extra = sandbox["tmp"] / "extra_config"
    extra.write_text("Host included\n    HostName 10.9.9.9\n", encoding="utf-8")
    sandbox["ssh_config"].write_text(
        f"Include {extra}\n\nHost local\n    HostName 127.0.0.1\n", encoding="utf-8"
    )
    aliases = {e["alias"] for e in cfgmod.parse_ssh_config()}
    assert aliases == {"included", "local"}


def test_defaults_are_filled_in(sandbox):
    cfg = cfgmod.load_config()
    assert cfg["defaults"]["timeout"] == 60
    assert cfg["defaults"]["shell"] == "bash -lc"
    assert cfg["hosts"] == {}


def test_defaults_can_be_overridden(write_config):
    write_config("[defaults]\ntimeout = 5\n")
    assert cfgmod.load_config()["defaults"]["timeout"] == 5


def test_unknown_default_key_is_rejected(write_config):
    write_config("[defaults]\ntimeuot = 5\n")
    with pytest.raises(cfgmod.ConfigError, match="timeuot"):
        cfgmod.load_config()


def test_unknown_host_key_is_rejected(write_config):
    write_config('[hosts.gpu1]\nnonsense = "x"\n')
    with pytest.raises(cfgmod.ConfigError, match="nonsense"):
        cfgmod.load_config()


def test_a_connection_setting_is_pointed_back_at_the_ssh_config(write_config):
    # hostname, user, port and identity_file belong to ssh, not to us. Silently
    # ignoring them would leave someone editing a file that does nothing.
    write_config('[hosts.gpu1]\nhostname = "10.0.0.9"\n')
    with pytest.raises(cfgmod.ConfigError, match="belong in"):
        cfgmod.load_config()


def test_broken_toml_names_the_file(write_config):
    path = write_config("[defaults\n")
    with pytest.raises(cfgmod.ConfigError, match=str(path.name)):
        cfgmod.load_config()


def test_list_hosts_merges_both_sources(write_config):
    write_config('[hosts.gpu1]\ndescription = "trainer"\ntags = ["gpu"]\n')
    hosts = {h["alias"]: h for h in cfgmod.list_hosts()}
    assert hosts["gpu1"]["description"] == "trainer"
    assert hosts["gpu1"]["hostname"] == "10.0.0.5"  # still from the ssh config
    assert hosts["gpu1"]["source"] == "ssh_config+ssher"


def test_a_host_only_we_know_about_is_flagged_as_unreachable(write_config):
    write_config('[hosts.ghost]\ndescription = "typo or leftover"\n')
    hosts = {h["alias"]: h for h in cfgmod.list_hosts()}
    assert hosts["ghost"]["source"] == "ssher"
    assert "ssh cannot reach it" in hosts["ghost"]["note"]
