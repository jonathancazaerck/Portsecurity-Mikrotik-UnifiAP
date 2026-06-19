import pytest
import yaml

from ap_switch_watchdog.routeros_script import FAILSAFE_SCRIPT_NAME
from scripts import setup_switches
from tests.conftest import make_client

CONFIG_DICT = {
    "poll_interval": 10,
    "unifi": {
        "url": "https://192.0.2.1",
        "username": "watchdog",
        "password_env": "TEST_UNIFI_PASSWORD",
    },
    "vlans": {"onboarding": 99, "management": 10, "trunk": [30, 50]},
    "switches": [
        {
            "name": "sw01",
            "host": "192.0.2.2",
            "username": "watchdog",
            "password_env": "TEST_MIKROTIK_PASSWORD",
            "bridge": "bridge1",
            "ap_ports": ["ether2", "ether3"],
        }
    ],
    "netwatch": {"watchdog_host": "192.0.2.10", "interval": "10s", "probe_timeout": "1s"},
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_UNIFI_PASSWORD", "unifi-secret")
    monkeypatch.setenv("TEST_MIKROTIK_PASSWORD", "mikrotik-secret")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(CONFIG_DICT))
    from ap_switch_watchdog.config import load_config

    return load_config(path)


def _db_with_bridge_ports(config):
    """A bridge with ap_ports already added as members (pre-existing, pre-setup)."""
    return {
        "/interface/bridge/port": [
            {"id": f"*{i}", "bridge": config.switches[0].bridge, "interface": port, "pvid": "1"}
            for i, port in enumerate(config.switches[0].ap_ports, start=1)
        ]
    }


def test_setup_switch_creates_static_vlans(config):
    db = _db_with_bridge_ports(config)
    client = make_client(db, switch=config.switches[0])

    setup_switches.setup_switch(client, config.switches[0], config)

    vlan_ids = sorted(int(e["vlan-ids"]) for e in db["/interface/bridge/vlan"])
    assert vlan_ids == [10, 30, 50, 99]


def test_setup_switch_is_idempotent(config):
    db = _db_with_bridge_ports(config)
    client = make_client(db, switch=config.switches[0])

    setup_switches.setup_switch(client, config.switches[0], config)
    setup_switches.setup_switch(client, config.switches[0], config)

    vlan_ids = sorted(int(e["vlan-ids"]) for e in db["/interface/bridge/vlan"])
    assert vlan_ids == [10, 30, 50, 99]
    assert len(db["/system/script"]) == 1
    assert len(db["/tool/netwatch"]) == 1


def test_setup_switch_sets_baseline_onboarding_for_ap_ports(config):
    db = _db_with_bridge_ports(config)
    client = make_client(db, switch=config.switches[0])

    setup_switches.setup_switch(client, config.switches[0], config)

    for port in ("ether2", "ether3"):
        port_entry = next(e for e in db["/interface/bridge/port"] if e["interface"] == port)
        assert port_entry["pvid"] == "99"
        dot1x_entry = next(e for e in db["/interface/dot1x/server"] if e["interface"] == port)
        assert dot1x_entry["disabled"] == "no"

    onboarding_vlan = next(e for e in db["/interface/bridge/vlan"] if e["vlan-ids"] == "99")
    assert set(onboarding_vlan["untagged"].split(",")) == {"ether2", "ether3"}


def test_setup_switch_installs_failsafe_script_and_netwatch(config):
    db = _db_with_bridge_ports(config)
    client = make_client(db, switch=config.switches[0])

    setup_switches.setup_switch(client, config.switches[0], config)

    scripts = db["/system/script"]
    assert any(s["name"] == FAILSAFE_SCRIPT_NAME for s in scripts)

    netwatch_entries = db["/tool/netwatch"]
    assert len(netwatch_entries) == 1
    entry = netwatch_entries[0]
    assert entry["host"] == "192.0.2.10"
    assert entry["interval"] == "10s"
    assert entry["timeout"] == "1s"
    assert FAILSAFE_SCRIPT_NAME in entry["down-script"]


# -- CLI -----------------------------------------------------------------------------


def test_main_print_failsafe_script_does_not_connect(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_UNIFI_PASSWORD", "unifi-secret")
    monkeypatch.setenv("TEST_MIKROTIK_PASSWORD", "mikrotik-secret")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(CONFIG_DICT))

    rc = setup_switches.main(["-c", str(path), "--print-failsafe-script"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "=== sw01 ===" in out
    assert FAILSAFE_SCRIPT_NAME not in out  # script body doesn't echo its own name
    assert ':set port "ether2"' in out


def test_main_unknown_switch_filter_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_UNIFI_PASSWORD", "unifi-secret")
    monkeypatch.setenv("TEST_MIKROTIK_PASSWORD", "mikrotik-secret")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(CONFIG_DICT))

    rc = setup_switches.main(["-c", str(path), "--switch", "doesnotexist", "--print-failsafe-script"])

    assert rc == 1


def test_main_missing_config_errors(tmp_path):
    rc = setup_switches.main(["-c", str(tmp_path / "nope.yaml")])
    assert rc == 1
