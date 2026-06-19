import pytest

from ap_switch_watchdog.mikrotik_client import (
    Dot1xEntryNotFoundError,
    PortNotFoundError,
    VlanNotConfiguredError,
)
from tests.conftest import make_client, seed_baseline


def _vlan(db, vlan_id):
    return next(e for e in db["/interface/bridge/vlan"] if e["vlan-ids"] == str(vlan_id))


def _port(db, name):
    return next(e for e in db["/interface/bridge/port"] if e["interface"] == name)


def _dot1x(db, name):
    return next(e for e in db["/interface/dot1x/server"] if e["interface"] == name)


def _iface(db, name):
    return next(e for e in db["/interface"] if e["name"] == name)


# -- find_port_by_mac / get_bridge_hosts -------------------------------------------


def test_get_bridge_hosts(switch_config):
    db = {
        "/interface/bridge/host": [
            {"id": "*1", "bridge": "bridge1", "mac-address": "AA:BB:CC:DD:EE:01", "on-interface": "ether2"},
            {"id": "*2", "bridge": "bridge1", "mac-address": "aa:bb:cc:dd:ee:02", "on-interface": "ether3"},
            {"id": "*3", "bridge": "bridge2", "mac-address": "aa:bb:cc:dd:ee:03", "on-interface": "ether4"},
        ]
    }
    client = make_client(db, switch=switch_config)

    hosts = client.get_bridge_hosts()

    assert hosts == {"aa:bb:cc:dd:ee:01": "ether2", "aa:bb:cc:dd:ee:02": "ether3"}


def test_find_port_by_mac_normalizes_case(switch_config):
    db = {
        "/interface/bridge/host": [
            {"id": "*1", "bridge": "bridge1", "mac-address": "AA:BB:CC:DD:EE:01", "on-interface": "ether2"},
        ]
    }
    client = make_client(db, switch=switch_config)

    assert client.find_port_by_mac("aa:bb:cc:dd:ee:01") == "ether2"
    assert client.find_port_by_mac("AA:BB:CC:DD:EE:01") == "ether2"


def test_find_port_by_mac_not_found(switch_config):
    client = make_client({"/interface/bridge/host": []}, switch=switch_config)
    assert client.find_port_by_mac("aa:bb:cc:dd:ee:99") is None


# -- set_port_mode: onboarding -> trunk --------------------------------------------


def test_apply_trunk_updates_pvid_vlans_and_dot1x(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    client.set_port_mode("ether2", "trunk", vlans)

    assert _port(db, "ether2")["pvid"] == str(vlans.management)
    assert "ether2" not in _vlan(db, vlans.onboarding)["untagged"].split(",")
    assert "ether2" in _vlan(db, vlans.management)["untagged"].split(",")
    for vid in vlans.trunk:
        assert "ether2" in _vlan(db, vid)["tagged"].split(",")
    assert _dot1x(db, "ether2")["disabled"] == "yes"


def test_apply_trunk_does_not_disturb_other_ports(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    client.set_port_mode("ether2", "trunk", vlans)

    # ether3/ether4 stay in the onboarding VLAN's untagged list.
    onboarding_untagged = _vlan(db, vlans.onboarding)["untagged"].split(",")
    assert "ether3" in onboarding_untagged
    assert "ether4" in onboarding_untagged
    assert _dot1x(db, "ether3")["disabled"] == "no"


# -- set_port_mode: trunk -> onboarding --------------------------------------------


def test_apply_onboarding_reverts_trunk_port(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    client.set_port_mode("ether2", "trunk", vlans)
    client.set_port_mode("ether2", "onboarding", vlans)

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert "ether2" in _vlan(db, vlans.onboarding)["untagged"].split(",")
    assert "ether2" not in _vlan(db, vlans.management)["untagged"].split(",")
    for vid in vlans.trunk:
        assert "ether2" not in _vlan(db, vid)["tagged"].split(",")
    assert _dot1x(db, "ether2")["disabled"] == "no"


def test_apply_onboarding_leaves_other_trunked_ports_alone(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    client.set_port_mode("ether2", "trunk", vlans)
    client.set_port_mode("ether3", "trunk", vlans)

    client.set_port_mode("ether2", "onboarding", vlans)

    # ether3 must still be trunked after ether2 was reverted.
    assert _port(db, "ether3")["pvid"] == str(vlans.management)
    for vid in vlans.trunk:
        assert "ether3" in _vlan(db, vid)["tagged"].split(",")
    assert _dot1x(db, "ether3")["disabled"] == "yes"


def test_apply_trunk_flaps_the_port(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)
    flapped = []
    client.flap_port = flapped.append

    client.set_port_mode("ether2", "trunk", vlans)

    assert flapped == ["ether2"]


def test_apply_onboarding_flaps_the_port(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)
    client.set_port_mode("ether2", "trunk", vlans)

    flapped = []
    client.flap_port = flapped.append
    client.set_port_mode("ether2", "onboarding", vlans)

    assert flapped == ["ether2"]


def test_set_port_mode_unknown_mode_raises(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    with pytest.raises(ValueError):
        client.set_port_mode("ether2", "bogus", vlans)


# -- error handling -----------------------------------------------------------------


def test_set_port_mode_missing_static_vlan_raises(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    # Drop the management VLAN's static entry, simulating a switch that
    # hasn't been through the setup script yet.
    db["/interface/bridge/vlan"] = [
        e for e in db["/interface/bridge/vlan"] if e["vlan-ids"] != str(vlans.management)
    ]
    client = make_client(db, switch=switch_config)

    with pytest.raises(VlanNotConfiguredError):
        client.set_port_mode("ether2", "trunk", vlans)


def test_set_port_mode_missing_dot1x_entry_raises(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    db["/interface/dot1x/server"] = []
    client = make_client(db, switch=switch_config)

    with pytest.raises(Dot1xEntryNotFoundError):
        client.set_port_mode("ether2", "trunk", vlans)


def test_set_pvid_unknown_port_raises(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    with pytest.raises(PortNotFoundError):
        client.set_port_mode("ether99", "trunk", vlans)


# -- setup helpers --------------------------------------------------------------------


def test_ensure_static_vlan_creates_once(switch_config):
    db = {"/interface/bridge/vlan": []}
    client = make_client(db, switch=switch_config)

    client.ensure_static_vlan(99)
    client.ensure_static_vlan(99)

    entries = [e for e in db["/interface/bridge/vlan"] if e["vlan-ids"] == "99"]
    assert len(entries) == 1
    assert entries[0]["bridge"] == "bridge1"


def test_ensure_dot1x_entry_creates_once(switch_config):
    db = {"/interface/dot1x/server": []}
    client = make_client(db, switch=switch_config)

    client.ensure_dot1x_entry("ether2", enabled=True)
    client.ensure_dot1x_entry("ether2", enabled=True)

    entries = [e for e in db["/interface/dot1x/server"] if e["interface"] == "ether2"]
    assert len(entries) == 1
    assert entries[0]["disabled"] == "no"
    assert entries[0]["auth-types"] == "dot1x,mac-auth"
    assert entries[0]["auth-timeout"] == "10"
    assert entries[0]["retrans-timeout"] == "5"
    assert entries[0]["radius-mac-format"] == "xxxxxxxxxxxx"


def test_ensure_dot1x_entry_reapplies_config_without_touching_disabled(switch_config):
    db = {
        "/interface/dot1x/server": [
            {"id": "*1", "interface": "ether2", "disabled": "yes", "auth-types": "dot1x"}
        ]
    }
    client = make_client(db, switch=switch_config)

    client.ensure_dot1x_entry("ether2", enabled=True)

    entries = [e for e in db["/interface/dot1x/server"] if e["interface"] == "ether2"]
    assert len(entries) == 1
    assert entries[0]["disabled"] == "yes"  # untouched on existing entries
    assert entries[0]["auth-types"] == "dot1x,mac-auth"
    assert entries[0]["auth-timeout"] == "10"
    assert entries[0]["retrans-timeout"] == "5"
    assert entries[0]["radius-mac-format"] == "xxxxxxxxxxxx"


def test_ensure_script_creates_and_updates(switch_config):
    db = {"/system/script": []}
    client = make_client(db, switch=switch_config)

    client.ensure_script("my-script", "puts hello")
    client.ensure_script("my-script", "puts world")

    entries = [e for e in db["/system/script"] if e["name"] == "my-script"]
    assert len(entries) == 1
    assert entries[0]["source"] == "puts world"


def test_ensure_netwatch_creates_and_updates(switch_config):
    db = {"/tool/netwatch": []}
    client = make_client(db, switch=switch_config)

    client.ensure_netwatch(host="192.0.2.10", interval="10s", timeout="30s", down_script="a")
    client.ensure_netwatch(host="192.0.2.10", interval="10s", timeout="30s", down_script="b")

    entries = [e for e in db["/tool/netwatch"] if e["host"] == "192.0.2.10"]
    assert len(entries) == 1
    assert entries[0]["down-script"] == "b"


def test_flap_port_disables_then_reenables(switch_config):
    db = {"/interface": [{"id": "*1", "name": "ether2", "disabled": "no"}]}
    client = make_client(db, switch=switch_config)

    client.flap_port("ether2")

    assert _iface(db, "ether2")["disabled"] == "no"


def test_flap_port_missing_interface_is_noop(switch_config):
    db = {"/interface": []}
    client = make_client(db, switch=switch_config)

    client.flap_port("ether2")  # must not raise


def test_get_port_link_status(switch_config):
    db = {
        "/interface": [
            {"id": "*1", "name": "ether2", "running": "true"},
            {"id": "*2", "name": "ether3", "running": "false"},
        ]
    }
    client = make_client(db, switch=switch_config)

    assert client.get_port_link_status("ether2") is True
    assert client.get_port_link_status("ether3") is False
    assert client.get_port_link_status("ether99") is None


def test_get_port_pvid(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    assert client.get_port_pvid("ether2") == str(vlans.onboarding)
    assert client.get_port_pvid("ether99") is None


def test_get_dot1x_disabled(switch_config, vlans):
    db = {}
    seed_baseline(db, switch_config, vlans)
    client = make_client(db, switch=switch_config)

    assert client.get_dot1x_disabled("ether2") is False  # dot1x active by default

    client.set_dot1x("ether2", enabled=False)
    assert client.get_dot1x_disabled("ether2") is True

    assert client.get_dot1x_disabled("ether99") is None


def test_get_dot1x_disabled_accepts_true_false_read_back(switch_config):
    # set_dot1x writes "yes"/"no", but RouterOS's API can normalize boolean
    # properties to "true"/"false" on read-back - both must be recognized.
    db = {
        "/interface/dot1x/server": [
            {"id": "*1", "interface": "ether2", "disabled": "true"},
            {"id": "*2", "interface": "ether3", "disabled": "false"},
        ]
    }
    client = make_client(db, switch=switch_config)

    assert client.get_dot1x_disabled("ether2") is True
    assert client.get_dot1x_disabled("ether3") is False


# -- connection lifecycle -------------------------------------------------------------


def test_context_manager_connects_and_disconnects(switch_config):
    from tests.conftest import FakeRouterOsApiPool

    db = {}
    pools = []

    def factory():
        pool = FakeRouterOsApiPool(db)
        pools.append(pool)
        return pool

    from ap_switch_watchdog.mikrotik_client import MikroTikClient

    client = MikroTikClient(
        name=switch_config.name,
        host=switch_config.host,
        username=switch_config.username,
        password=switch_config.password,
        bridge=switch_config.bridge,
        pool_factory=factory,
    )

    with client:
        client.api.get_resource("/interface/bridge/host").get()

    assert len(pools) == 1
    assert pools[0].disconnected is True
