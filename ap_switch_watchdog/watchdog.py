"""Main watchdog loop.

Every ``poll_interval`` seconds, for each configured ``ap_port`` on each
switch, the port's *desired* mode is computed from live data and reconciled
against its *actual* current mode:

1. ``MikroTikClient.get_bridge_hosts`` - which MAC addresses the switch's
   bridge has currently learned, and on which port.
2. ``MikroTikClient.get_port_link_status`` - whether the port currently has a
   physical link (``/interface`` ``running``), which RouterOS reflects
   essentially instantly.
3. ``UniFiClient.get_ap_macs`` - ``(known_macs, connected_macs)`` from a
   single ``stat/device`` call. ``known_macs`` is the security allowlist
   (every ever-adopted AP MAC, regardless of online/offline state).
   ``connected_macs`` is the subset where the AP currently has an active
   management session with the controller (``state == 1``).

Desired mode uses an **asymmetric rule** to close the MAC-spoofing window
without reintroducing heartbeat-lag false reverts:

* **onboarding → trunk**: link up *and* bridge has a ``known_macs`` MAC on
  the port *and* that MAC is in ``connected_macs``.  The connected check
  means a device spoofing a known-but-offline AP's MAC is not granted trunk
  access: the real AP would need to be simultaneously online, which would
  immediately displace it from the management VLAN and cause it to disconnect
  from the controller - making ``connected_macs`` go empty quickly.
* **trunk → onboarding**: the known AP's MAC disappears from the bridge host
  table *or* the link drops.  UniFi's ``connected`` state is deliberately
  *not* checked here - heartbeat lag (30-70 s) would cause false reverts right
  after every mode switch.

The port's actual current mode is read live via ``MikroTikClient.get_port_pvid``
and ``MikroTikClient.get_dot1x_disabled`` (PVID == management VLAN *and* dot1x
disabled -> ``"trunk"``; checking both means a partially-applied mode switch
is retried next cycle instead of being mistaken for "done"). If desired !=
actual, ``MikroTikClient.set_port_mode`` applies the VLAN/dot1x change and
flaps the port (see ``MikroTikClient.flap_port``).

Ports changed this cycle are skipped on the *next* poll, giving the link and
bridge host table one ``poll_interval`` to settle after the flap before being
reconsidered - without this, a port could be flapped back and forth every
cycle while its link/MAC-table entry is still catching up.

There is no persisted state: every poll fully reconciles each port's actual
mode against its desired mode from live switch + UniFi data, so a watchdog
restart picks up exactly where the switches currently are.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import SwitchConfig, WatchdogConfig
from .mikrotik_client import MikroTikClient
from .unifi_client import UniFiClient, UniFiError

logger = logging.getLogger(__name__)


class APSwitchWatchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        unifi_client: Optional[UniFiClient] = None,
        switches: Optional[dict[str, MikroTikClient]] = None,
    ) -> None:
        self.config = config
        self.unifi = unifi_client or UniFiClient(
            url=config.unifi.url,
            username=config.unifi.username,
            password=config.unifi.password,
            site=config.unifi.site,
            verify_ssl=config.unifi.verify_ssl,
        )
        if switches is not None:
            self.switches = switches
        else:
            self.switches = {
                sw.name: MikroTikClient(
                    name=sw.name,
                    host=sw.host,
                    username=sw.username,
                    password=sw.password,
                    port=sw.port,
                    bridge=sw.bridge,
                )
                for sw in config.switches
            }
        self._touched_ports: set[tuple[str, str]] = set()

    def run_forever(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:
                logger.exception("poll cycle failed")
            time.sleep(self.config.poll_interval)

    def poll_once(self) -> None:
        try:
            known_ap_macs, connected_ap_macs = self.unifi.get_ap_macs()
        except UniFiError:
            logger.exception("failed to query UniFi controller")
            return

        touched_last_cycle = self._touched_ports
        self._touched_ports = set()

        for sw in self.config.switches:
            client = self.switches[sw.name]
            try:
                self._reconcile_switch(sw, client, known_ap_macs, connected_ap_macs, touched_last_cycle)
            except Exception:
                logger.exception("failed to reconcile ports on %s", sw.name)

    def _reconcile_switch(
        self,
        sw: SwitchConfig,
        client: MikroTikClient,
        known_ap_macs: set[str],
        connected_ap_macs: set[str],
        touched_last_cycle: set[tuple[str, str]],
    ) -> None:
        macs_by_port: dict[str, list[str]] = {}
        for mac, port in client.get_bridge_hosts().items():
            macs_by_port.setdefault(port, []).append(mac)

        for port in sw.ap_ports:
            if (sw.name, port) in touched_last_cycle:
                continue
            try:
                self._reconcile_port(sw.name, client, port, macs_by_port.get(port, []), known_ap_macs, connected_ap_macs)
            except Exception:
                logger.exception("failed to reconcile %s/%s", sw.name, port)

    def _reconcile_port(
        self,
        switch_name: str,
        client: MikroTikClient,
        port: str,
        macs_on_port: list[str],
        known_ap_macs: set[str],
        connected_ap_macs: set[str],
    ) -> None:
        link_up = client.get_port_link_status(port) is not False
        ap_mac = next((mac for mac in macs_on_port if mac in known_ap_macs), None)

        pvid = client.get_port_pvid(port)
        dot1x_disabled = client.get_dot1x_disabled(port)
        is_trunk = pvid == str(self.config.vlans.management) and dot1x_disabled is True
        current = "trunk" if is_trunk else "onboarding"

        # Asymmetric desired computation - see module docstring for rationale.
        if current == "trunk":
            # Already trunked: stay trunked as long as the AP's MAC is present
            # and the link is up.  UniFi's connected state is NOT checked here
            # to avoid heartbeat-lag false reverts.
            desired = "trunk" if (link_up and ap_mac) else "onboarding"
        else:
            # Not yet trunked: require UniFi to confirm the AP is connected
            # (active management session) before opening the port.  This raises
            # the bar for MAC spoofing without affecting normal AP boot time
            # beyond the first trunk grant.
            desired = "trunk" if (link_up and ap_mac and ap_mac in connected_ap_macs) else "onboarding"

        logger.debug(
            "%s/%s: link_up=%s ap_mac=%s connected=%s pvid=%s dot1x_disabled=%s current=%s desired=%s",
            switch_name, port, link_up, ap_mac,
            bool(ap_mac and ap_mac in connected_ap_macs),
            pvid, dot1x_disabled, current, desired,
        )
        if current == desired:
            return

        if desired == "trunk":
            logger.info("AP %s present and connected on %s/%s -> trunk", ap_mac, switch_name, port)
        elif not link_up:
            logger.info("%s/%s link down -> onboarding", switch_name, port)
        else:
            logger.info("%s/%s no known AP present -> onboarding", switch_name, port)

        client.set_port_mode(port, desired, self.config.vlans)
        self._touched_ports.add((switch_name, port))
