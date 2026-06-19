import pytest

from ap_switch_watchdog.config import NetwatchConfig, SwitchConfig, UniFiConfig, VlanConfig, WatchdogConfig
from ap_switch_watchdog.unifi_client import UniFiError
from ap_switch_watchdog.watchdog import APSwitchWatchdog
from tests.conftest import make_client, seed_baseline

AP1 = "aa:bb:cc:dd:ee:01"
AP2 = "aa:bb:cc:dd:ee:02"
OTHER_DEVICE = "aa:bb:cc:dd:ee:99"


class FakeUniFiClient:
    def __init__(self, known_macs: set[str]):
        self.known_macs = set(known_macs)
        self.calls = 0

    def get_known_ap_macs(self) -> set[str]:
        self.calls += 1
        return set(self.known_macs)


def _vlan(db, vlan_id):
    return next(e for e in db["/interface/bridge/vlan"] if e["vlan-ids"] == str(vlan_id))


def _port(db, name):
    return next(e for e in db["/interface/bridge/port"] if e["interface"] == name)


def _dot1x(db, name):
    return next(e for e in db["/interface/dot1x/server"] if e["interface"] == name)


def _iface(db, name):
    return next(e for e in db["/interface"] if e["name"] == name)


@pytest.fixture
def config(vlans):
    return WatchdogConfig(
        poll_interval=10,
        unifi=UniFiConfig(url="https://192.0.2.1", username="watchdog", password="secret"),
        vlans=vlans,
        switches=[
            SwitchConfig(
                name="sw01",
                host="192.0.2.2",
                username="watchdog",
                password="secret",
                bridge="bridge1",
                ap_ports=["ether2", "ether3", "ether4"],
            )
        ],
        netwatch=NetwatchConfig(watchdog_host="192.0.2.10"),
    )


@pytest.fixture
def sw01(config, vlans):
    db = {}
    seed_baseline(db, config.switches[0], vlans)
    # Bridge host table: AP1 learned on ether2.
    db["/interface/bridge/host"] = [
        {"id": "*h1", "bridge": "bridge1", "mac-address": AP1, "on-interface": "ether2"},
    ]
    client = make_client(db, switch=config.switches[0])
    return db, client


def make_watchdog(config, sw01_client, known_ap_macs):
    unifi = FakeUniFiClient(known_ap_macs)
    return APSwitchWatchdog(config, unifi_client=unifi, switches={"sw01": sw01_client}), unifi


# -- onboarding -> trunk -----------------------------------------------------------


def test_known_ap_with_link_is_trunked(config, sw01, vlans):
    db, client = sw01
    wd, unifi = make_watchdog(config, client, {AP1})

    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.management)
    assert "ether2" not in _vlan(db, vlans.onboarding)["untagged"].split(",")
    assert "ether2" in _vlan(db, vlans.management)["untagged"].split(",")
    for vid in vlans.trunk:
        assert "ether2" in _vlan(db, vid)["tagged"].split(",")
    assert _dot1x(db, "ether2")["disabled"] == "yes"


def test_reconciliation_is_stable_once_trunked(config, sw01, vlans):
    db, client = sw01
    wd, unifi = make_watchdog(config, client, {AP1})

    wd.poll_once()  # ether2: onboarding -> trunk (touched this cycle)
    wd.poll_once()  # ether2 skipped (touched last cycle)
    wd.poll_once()  # ether2 reconsidered: still correct -> no-op

    assert _port(db, "ether2")["pvid"] == str(vlans.management)
    assert _dot1x(db, "ether2")["disabled"] == "yes"


def test_unknown_mac_on_port_stays_onboarding(config, sw01, vlans):
    db, client = sw01
    # AP1 is on ether2 per the bridge host table, but not a known AP.
    wd, unifi = make_watchdog(config, client, set())

    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert _dot1x(db, "ether2")["disabled"] == "no"


def test_non_ap_device_on_port_stays_onboarding(config, sw01, vlans):
    db, client = sw01
    db["/interface/bridge/host"] = [
        {"id": "*h1", "bridge": "bridge1", "mac-address": OTHER_DEVICE, "on-interface": "ether2"},
    ]
    wd, unifi = make_watchdog(config, client, {AP1})  # AP1 known, but not present here

    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert _dot1x(db, "ether2")["disabled"] == "no"


def test_empty_port_stays_onboarding(config, sw01, vlans):
    db, client = sw01
    db["/interface/bridge/host"] = []
    wd, unifi = make_watchdog(config, client, {AP1})

    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert _dot1x(db, "ether2")["disabled"] == "no"


def test_known_ap_without_link_stays_onboarding(config, sw01, vlans):
    db, client = sw01
    _iface(db, "ether2")["running"] = "false"
    wd, unifi = make_watchdog(config, client, {AP1})

    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert _dot1x(db, "ether2")["disabled"] == "no"


# -- trunk -> onboarding ------------------------------------------------------------


def test_link_down_reverts_trunked_port(config, sw01, vlans):
    db, client = sw01
    wd, unifi = make_watchdog(config, client, {AP1})
    wd.poll_once()  # trunk ether2 (touched)
    wd.poll_once()  # settle cycle: ether2 skipped, touched cleared

    # AP unplugged: link drops and the bridge no longer sees its MAC.
    _iface(db, "ether2")["running"] = "false"
    db["/interface/bridge/host"] = []
    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert "ether2" in _vlan(db, vlans.onboarding)["untagged"].split(",")
    assert _dot1x(db, "ether2")["disabled"] == "no"


def test_mac_no_longer_present_reverts_port(config, sw01, vlans):
    db, client = sw01
    wd, unifi = make_watchdog(config, client, {AP1})
    wd.poll_once()  # trunk ether2 (touched)
    wd.poll_once()  # settle cycle

    # Link stays up, but the AP's MAC is no longer learned on this port
    # (e.g. it rebooted and the bridge dropped the stale entry).
    db["/interface/bridge/host"] = []
    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert _dot1x(db, "ether2")["disabled"] == "no"


def test_touched_port_is_skipped_next_cycle(config, sw01, vlans):
    db, client = sw01
    wd, unifi = make_watchdog(config, client, {AP1})
    wd.poll_once()  # trunk ether2 (touched)

    # Link briefly reports down and the bridge host table is briefly empty
    # right after the flap (still settling) - ports touched last cycle are
    # skipped this cycle rather than immediately reverted.
    _iface(db, "ether2")["running"] = "false"
    db["/interface/bridge/host"] = []
    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.management)
    assert _dot1x(db, "ether2")["disabled"] == "yes"


# -- AP moves to a different port ------------------------------------------------------


def test_ap_moved_to_different_port(config, sw01, vlans):
    db, client = sw01
    wd, unifi = make_watchdog(config, client, {AP1})
    wd.poll_once()  # AP1 on ether2 -> trunk
    wd.poll_once()  # settle cycle

    # AP physically moved to ether3.
    db["/interface/bridge/host"][0]["on-interface"] = "ether3"
    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
    assert _dot1x(db, "ether2")["disabled"] == "no"
    assert _port(db, "ether3")["pvid"] == str(vlans.management)
    assert _dot1x(db, "ether3")["disabled"] == "yes"


# -- multiple APs -----------------------------------------------------------------------


def test_two_aps_independent(config, sw01, vlans):
    db, client = sw01
    db["/interface/bridge/host"].append(
        {"id": "*h2", "bridge": "bridge1", "mac-address": AP2, "on-interface": "ether3"}
    )
    wd, unifi = make_watchdog(config, client, {AP1, AP2})

    wd.poll_once()

    assert _port(db, "ether2")["pvid"] == str(vlans.management)
    assert _port(db, "ether3")["pvid"] == str(vlans.management)
    assert _dot1x(db, "ether2")["disabled"] == "yes"
    assert _dot1x(db, "ether3")["disabled"] == "yes"
    # ether4 untouched
    assert _dot1x(db, "ether4")["disabled"] == "no"


# -- error isolation -------------------------------------------------------------------


def test_error_for_one_port_does_not_block_others(config, sw01, vlans):
    db, client = sw01
    db["/interface/bridge/host"].append(
        {"id": "*h2", "bridge": "bridge1", "mac-address": AP2, "on-interface": "ether3"}
    )
    # Break ether2's dot1x entry so applying trunk mode for it raises.
    db["/interface/dot1x/server"] = [e for e in db["/interface/dot1x/server"] if e["interface"] != "ether2"]

    wd, unifi = make_watchdog(config, client, {AP1, AP2})

    wd.poll_once()  # must not raise

    assert _port(db, "ether3")["pvid"] == str(vlans.management)
    assert _dot1x(db, "ether3")["disabled"] == "yes"


def test_unifi_error_skips_poll_cycle(config, sw01, vlans):
    db, client = sw01

    class FailingUniFi:
        def get_known_ap_macs(self):
            raise UniFiError("boom")

    wd = APSwitchWatchdog(config, unifi_client=FailingUniFi(), switches={"sw01": client})

    wd.poll_once()  # must not raise

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)


def test_bridge_host_query_failure_skips_switch(config, sw01, vlans):
    db, client = sw01

    def boom():
        raise RuntimeError("boom")

    client.get_bridge_hosts = boom
    wd, unifi = make_watchdog(config, client, {AP1})

    wd.poll_once()  # must not raise

    assert _port(db, "ether2")["pvid"] == str(vlans.onboarding)
