#!/usr/bin/env python3
"""Idempotent one-time(-ish) setup for switches managed by the AP watchdog.

For every switch in ``config.yaml`` this:

1. Creates static ``/interface/bridge/vlan`` entries for the onboarding,
   management, and trunk VLANs. RouterOS 7 auto-creates *dynamic* VLAN
   entries from a port's PVID, and dynamic entries cannot be edited via the
   API - so the watchdog needs these static entries to already exist.
2. Creates (or updates) a ``/interface/dot1x/server`` entry for every
   ``ap_ports`` port - with MAC-auth fallback enabled and the auth/retrans
   timeouts set - and puts the port into its baseline "onboarding" state
   (onboarding VLAN untagged/PVID, dot1x active).
3. Installs the failsafe revert script (see
   :mod:`ap_switch_watchdog.routeros_script`) and a ``/tool/netwatch`` entry
   that monitors the watchdog host - the switch's dead-man's-switch.

Re-running this script is safe: every step only creates or updates what is
needed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ap_switch_watchdog.config import ConfigError, SwitchConfig, WatchdogConfig, load_config
from ap_switch_watchdog.mikrotik_client import MikroTikClient
from ap_switch_watchdog.routeros_script import FAILSAFE_SCRIPT_NAME, render_failsafe_script

logger = logging.getLogger(__name__)


def setup_switch(client: MikroTikClient, switch: SwitchConfig, config: WatchdogConfig) -> None:
    logger.info("--- %s (%s) ---", switch.name, switch.host)

    for vlan_id in (config.vlans.onboarding, config.vlans.management, *config.vlans.trunk):
        client.ensure_static_vlan(vlan_id)

    for port in switch.ap_ports:
        client.ensure_dot1x_entry(port, enabled=True)
        client.set_port_mode(port, "onboarding", config.vlans)
        logger.info(
            "%s: %s baseline = onboarding (VLAN %s, dot1x active)",
            switch.name, port, config.vlans.onboarding,
        )

    script_source = render_failsafe_script(switch, config.vlans)
    client.ensure_script(FAILSAFE_SCRIPT_NAME, script_source)

    client.ensure_netwatch(
        host=config.netwatch.watchdog_host,
        interval=config.netwatch.interval,
        timeout=config.netwatch.probe_timeout,
        down_script=(
            f':log warning "ap-switch-watchdog: {config.netwatch.watchdog_host} unreachable, '
            f'reverting AP ports to onboarding"\n'
            f"/system script run {FAILSAFE_SCRIPT_NAME}"
        ),
        up_script=(
            f':log info "ap-switch-watchdog: {config.netwatch.watchdog_host} reachable again"'
        ),
        comment="ap-switch-watchdog dead-man's switch",
    )
    logger.info(
        "%s: netwatch monitoring %s (failsafe: %s)",
        switch.name, config.netwatch.watchdog_host, FAILSAFE_SCRIPT_NAME,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="path to config.yaml (default: %(default)s)"
    )
    parser.add_argument("--switch", help="only set up the switch with this name")
    parser.add_argument(
        "--print-failsafe-script",
        action="store_true",
        help="print the generated failsafe RouterOS script(s) and exit without connecting",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        logger.error("failed to load configuration from %s: %s", args.config, exc)
        return 1

    switches = config.switches
    if args.switch:
        switches = [sw for sw in switches if sw.name == args.switch]
        if not switches:
            logger.error("no switch named %r in %s", args.switch, args.config)
            return 1

    if args.print_failsafe_script:
        for sw in switches:
            print(f"# === {sw.name} ===")
            print(render_failsafe_script(sw, config.vlans))
        return 0

    for sw in switches:
        client = MikroTikClient(
            name=sw.name,
            host=sw.host,
            username=sw.username,
            password=sw.password,
            port=sw.port,
            bridge=sw.bridge,
        )
        with client:
            setup_switch(client, sw, config)

    logger.info("setup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
