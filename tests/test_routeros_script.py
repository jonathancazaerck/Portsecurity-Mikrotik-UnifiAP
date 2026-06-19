from ap_switch_watchdog.routeros_script import render_failsafe_script
from ap_switch_watchdog.config import SwitchConfig, VlanConfig


def make_switch(ap_ports):
    return SwitchConfig(
        name="sw01",
        host="192.0.2.2",
        username="watchdog",
        password="secret",
        bridge="bridge1",
        ap_ports=ap_ports,
    )


def test_script_references_bridge_and_onboarding_vlan():
    vlans = VlanConfig(onboarding=99, management=10, trunk=[30, 50])
    script = render_failsafe_script(make_switch(["ether2"]), vlans)

    assert ':local bridge "bridge1"' in script
    assert "pvid=99" in script


def test_script_has_a_block_per_ap_port():
    vlans = VlanConfig(onboarding=99, management=10, trunk=[30, 50])
    script = render_failsafe_script(make_switch(["ether2", "ether3", "ether4"]), vlans)

    for port in ("ether2", "ether3", "ether4"):
        assert f':set port "{port}"' in script


def test_script_re_enables_dot1x_per_port():
    vlans = VlanConfig(onboarding=99, management=10, trunk=[30, 50])
    script = render_failsafe_script(make_switch(["ether2", "ether3"]), vlans)

    assert script.count("/interface/dot1x/server set [find interface=$port] disabled=no") == 2


def test_script_touches_management_and_trunk_vlans():
    vlans = VlanConfig(onboarding=99, management=10, trunk=[30, 50])
    script = render_failsafe_script(make_switch(["ether2"]), vlans)

    assert "vlan-ids=10" in script  # management VLAN (untagged removal)
    assert "vlan-ids=30" in script  # trunk VLAN
    assert "vlan-ids=50" in script  # trunk VLAN
    assert "vlan-ids=99" in script  # onboarding VLAN (untagged addition)


def test_script_defines_list_helper_functions_once():
    vlans = VlanConfig(onboarding=99, management=10, trunk=[30, 50])
    script = render_failsafe_script(make_switch(["ether2", "ether3"]), vlans)

    assert script.count(":local removeFromList do={") == 1
    assert script.count(":local addToList do={") == 1


def test_script_declares_scratch_vars_once_to_avoid_redeclaration():
    vlans = VlanConfig(onboarding=99, management=10, trunk=[30, 50])
    script = render_failsafe_script(make_switch(["ether2", "ether3", "ether4"]), vlans)

    # ":local port" etc. must only appear once; subsequent ports use ":set".
    assert script.count(":local port\n") == 1
    assert script.count(":local vlanEntry\n") == 1
    assert script.count(":local curList\n") == 1
    assert script.count(':set port "ether3"') == 1
