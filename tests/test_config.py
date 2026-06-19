import pytest
import yaml

from ap_switch_watchdog.config import ConfigError, load_config

BASE_CONFIG = {
    "poll_interval": 10,
    "unifi": {
        "url": "https://192.0.2.1",
        "username": "watchdog",
        "password_env": "TEST_UNIFI_PASSWORD",
        "site": "default",
        "verify_ssl": False,
    },
    "vlans": {"onboarding": 99, "management": 10, "trunk": [30, 50]},
    "switches": [
        {
            "name": "sw01",
            "host": "192.0.2.2",
            "username": "watchdog",
            "password_env": "TEST_MIKROTIK_PASSWORD",
            "port": 8729,
            "bridge": "bridge1",
            "ap_ports": ["ether2", "ether3"],
        }
    ],
    "netwatch": {"watchdog_host": "192.0.2.10", "interval": "10s", "probe_timeout": "1s"},
}


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_UNIFI_PASSWORD", "unifi-secret")
    monkeypatch.setenv("TEST_MIKROTIK_PASSWORD", "mikrotik-secret")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(BASE_CONFIG))
    return path


def test_load_config(config_path):
    config = load_config(config_path)

    assert config.poll_interval == 10
    assert config.unifi.url == "https://192.0.2.1"
    assert config.unifi.password == "unifi-secret"
    assert config.vlans.onboarding == 99
    assert config.vlans.management == 10
    assert config.vlans.trunk == [30, 50]

    assert len(config.switches) == 1
    sw = config.switches[0]
    assert sw.name == "sw01"
    assert sw.password == "mikrotik-secret"
    assert sw.ap_ports == ["ether2", "ether3"]

    assert config.netwatch.watchdog_host == "192.0.2.10"
    assert config.trunk_grace_period == 120  # default


def test_missing_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_UNIFI_PASSWORD", raising=False)
    monkeypatch.setenv("TEST_MIKROTIK_PASSWORD", "mikrotik-secret")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(BASE_CONFIG))

    with pytest.raises(ConfigError, match="TEST_UNIFI_PASSWORD"):
        load_config(path)


def test_missing_required_key_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_UNIFI_PASSWORD", "unifi-secret")
    monkeypatch.setenv("TEST_MIKROTIK_PASSWORD", "mikrotik-secret")
    raw = dict(BASE_CONFIG)
    del raw["vlans"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError, match="vlans"):
        load_config(path)


def test_empty_switches_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_UNIFI_PASSWORD", "unifi-secret")
    raw = {**BASE_CONFIG, "switches": []}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError, match="switches"):
        load_config(path)
