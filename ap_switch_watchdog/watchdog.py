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
* **trunk → onboarding**: any of the following:

  - the known AP's MAC disappears from the bridge host table,
  - the link drops,
  - the AP has been absent from ``connected_macs`` for longer than
    ``trunk_grace_period`` seconds (default 120 s) — within the grace window
    ``connected_macs`` is not checked to avoid heartbeat-lag false reverts;
    after the window a spoofer connecting after the real AP went offline is
    detected,
  - more than one MAC is seen on the management VLAN on this port (the bridge
    external FDB), which indicates a hub, bridge, or second device behind the
    port,
  - PoE is no longer being drawn on the port (``poe-out-status ≠
    "powered-on"``); checked only when a PoE entry exists for the port.

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
from .mikrotik_client import MikroTikClient, MikroTikConnectionError
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
        # Per-port timestamp: last time the AP on this port was seen with an
        # active UniFi management session.  Initialised to now so that ports
        # already in trunk mode receive a full grace window on (re)start.
        self._last_connected: dict[tuple[str, str], float] = {
            (sw.name, port): time.monotonic()
            for sw in config.switches
            for port in sw.ap_ports
        }
        # Track connectivity state so we can log restoration after an outage.
        self._unifi_down: bool = False
        self._switches_down: set[str] = set()

    def run_forever(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:
                logger.exception("unexpected error in poll cycle — continuing")
            try:
                time.sleep(self.config.poll_interval)
            except Exception:
                pass

    def poll_once(self) -> None:
        try:
            known_ap_macs, connected_ap_macs = self.unifi.get_ap_macs()
        except Exception as exc:
            if not self._unifi_down:
                self._unifi_down = True
            logger.warning("failed to query UniFi controller: %s — skipping cycle", exc)
            return

        if self._unifi_down:
            self._unifi_down = False
            logger.info("UniFi controller connection restored")
            # Reset per-port timestamps so ports that were in trunk during the
            # outage get a full grace window to reconnect to the controller.
            # Without this, an outage longer than trunk_grace_period causes
            # trunk ports to flip to onboarding, cutting APs off from the
            # management VLAN — which prevents them from reconnecting and
            # creates a deadlock.
            now = time.monotonic()
            for key in self._last_connected:
                self._last_connected[key] = now

        logger.debug("UniFi: %d known AP MACs: %s", len(known_ap_macs), sorted(known_ap_macs))

        touched_last_cycle = self._touched_ports
        self._touched_ports = set()

        for sw in self.config.switches:
            client = self.switches[sw.name]
            try:
                self._reconcile_switch(sw, client, known_ap_macs, connected_ap_macs, touched_last_cycle)
                if sw.name in self._switches_down:
                    self._switches_down.discard(sw.name)
                    logger.info("%s: connection restored", sw.name)
            except MikroTikConnectionError as exc:
                if sw.name not in self._switches_down:
                    self._switches_down.add(sw.name)
                logger.warning("%s", exc)
                client.close()
            except Exception:
                if sw.name not in self._switches_down:
                    self._switches_down.add(sw.name)
                logger.exception("failed to reconcile ports on %s", sw.name)
                client.close()  # drop stale connection so the next cycle reconnects fresh

    def _reconcile_switch(
        self,
        sw: SwitchConfig,
        client: MikroTikClient,
        known_ap_macs: set[str],
        connected_ap_macs: set[str],
        touched_last_cycle: set[tuple[str, str]],
    ) -> None:
        macs_by_port: dict[str, list[str]] = {}
        for mac, ports in client.get_bridge_hosts().items():
            for port in ports:
                macs_by_port.setdefault(port, []).append(mac)

        logger.debug("%s: bridge MACs by port: %s", sw.name, dict(macs_by_port))

        mgmt_macs_by_port = client.get_vlan_macs_by_port(self.config.vlans.management)

        for port in sw.ap_ports:
            if (sw.name, port) in touched_last_cycle:
                continue
            try:
                self._reconcile_port(
                    sw.name, client, port,
                    macs_by_port.get(port, []),
                    mgmt_macs_by_port.get(port, []),
                    known_ap_macs, connected_ap_macs,
                )
            except MikroTikConnectionError:
                raise  # abort the whole switch so poll_once can close + reset the client
            except Exception:
                logger.exception("failed to reconcile %s/%s", sw.name, port)

    def _reconcile_port(
        self,
        switch_name: str,
        client: MikroTikClient,
        port: str,
        macs_on_port: list[str],
        mgmt_macs_on_port: list[str],
        known_ap_macs: set[str],
        connected_ap_macs: set[str],
    ) -> None:
        link_up = client.get_port_link_status(port) is not False
        ap_mac = next((mac for mac in macs_on_port if mac in known_ap_macs), None)

        pvid = client.get_port_pvid(port)
        dot1x_disabled = client.get_dot1x_disabled(port)
        is_trunk = pvid == str(self.config.vlans.management) and dot1x_disabled is True
        current = "trunk" if is_trunk else "onboarding"

        # Refresh the per-port "last seen connected" timestamp whenever UniFi
        # confirms the AP has an active management session.
        if ap_mac and ap_mac in connected_ap_macs:
            self._last_connected[(switch_name, port)] = time.monotonic()

        elapsed = time.monotonic() - self._last_connected.get((switch_name, port), 0.0)
        grace_ok = elapsed <= self.config.trunk_grace_period

        # More than one MAC on the management VLAN means a hub, bridge, or
        # second device is behind the port.  Only checked on already-trunked
        # ports: during onboarding the AP is still on the onboarding VLAN, so
        # the management VLAN FDB will be empty and the check is meaningless.
        single_mac_on_mgmt = len(mgmt_macs_on_port) <= 1

        # PoE: only "powered-on" confirms a device is actively drawing power.
        # Any other known status means no PoE load is detected, which blocks
        # trunk (a MAC spoofer without a PoE device cannot draw power).
        # If no PoE entry exists for the port (None), the check is skipped.
        poe_status = client.get_poe_out_status(port)
        poe_active = poe_status == "powered-on" if poe_status is not None else True

        # Asymmetric desired computation - see module docstring for rationale.
        if current == "trunk":
            # Within the grace window keep trunk without the connected check
            # (avoids heartbeat-lag false reverts).  Once the grace window
            # expires, require the AP to be connected — this is what catches
            # a spoofer who connects after the real AP has gone offline.
            desired = (
                "trunk"
                if (link_up and ap_mac and grace_ok and single_mac_on_mgmt and poe_active)
                else "onboarding"
            )
        else:
            # Not yet trunked: require UniFi to confirm the AP is connected
            # (active management session) and PoE is active before opening
            # the port.  A device spoofing an AP MAC won't draw PoE.
            desired = (
                "trunk"
                if (link_up and ap_mac and ap_mac in connected_ap_macs and poe_active)
                else "onboarding"
            )

        logger.debug(
            "%s/%s: link_up=%s ap_mac=%s connected=%s grace_ok=%s "
            "single_mac_on_mgmt=%s poe_status=%s pvid=%s dot1x_disabled=%s current=%s desired=%s",
            switch_name, port, link_up, ap_mac,
            bool(ap_mac and ap_mac in connected_ap_macs),
            grace_ok, single_mac_on_mgmt, poe_status,
            pvid, dot1x_disabled, current, desired,
        )
        logger.info(
            "%s/%s  mode=%-12s  ap=%-17s  link=%-4s  connected=%-3s  poe=%s",
            switch_name, port, current,
            ap_mac or "—",
            "up" if link_up else "down",
            "yes" if (ap_mac and ap_mac in connected_ap_macs) else "no",
            poe_status or "n/a",
        )
        if current == desired:
            return

        if desired == "trunk":
            logger.info("AP %s present and connected on %s/%s -> trunk", ap_mac, switch_name, port)
        elif not link_up:
            logger.info("%s/%s link down -> onboarding", switch_name, port)
        elif not ap_mac:
            logger.info("%s/%s no known AP present -> onboarding", switch_name, port)
        elif not grace_ok:
            logger.info(
                "%s/%s AP %s absent from UniFi beyond grace period -> onboarding",
                switch_name, port, ap_mac,
            )
        elif not single_mac_on_mgmt:
            logger.info(
                "%s/%s %d MACs on management VLAN (expected 1) -> onboarding",
                switch_name, port, len(mgmt_macs_on_port),
            )
        else:
            logger.info("%s/%s PoE not active (%s) -> onboarding", switch_name, port, poe_status)

        client.set_port_mode(port, desired, self.config.vlans)
        self._touched_ports.add((switch_name, port))
