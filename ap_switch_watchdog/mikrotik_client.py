"""RouterOS API wrapper for a single MikroTik switch.

Built on top of the ``RouterOS-api`` package (``routeros_api``), connected
via API-SSL (port 8729) with ``plaintext_login=True`` and ``ssl_verify=False``
as required for RouterOS 7 with a self-signed API certificate.

RouterOS property names use hyphens (``mac-address``, ``on-interface``,
``vlan-ids``, ``up-script``, ...). The underlying library converts
underscores to hyphens for arguments passed to ``get``/``set``/``add``/
``remove`` (and ``id`` <-> ``.id``), but returns response dictionaries with
the original hyphenated keys. This module follows that convention:
underscore kwargs going out, hyphenated keys coming back.

Two RouterOS 7 quirks this module works around:

* Dynamic ``/interface/bridge/vlan`` entries (auto-created when a port's
  PVID points at a VLAN that has no static entry yet) cannot be modified via
  the API. The setup script therefore pre-creates *static* entries for every
  VLAN used here; this module only ever ``set``s those existing entries.
* ``/interface/dot1x/server`` entries are pre-created (disabled or enabled)
  by the setup script for every AP-capable port; this module only toggles
  their ``disabled`` flag.
* Changing a live port's PVID/VLAN membership does not generate a link-down
  event, so a connected AP can keep using its old (now-invalid) DHCP lease
  until its lease/heartbeat timeout expires - appearing "offline" to the
  controller for that whole window. After applying a new port mode, this
  module briefly flaps the port's ``/interface`` entry (see
  :meth:`MikroTikClient.flap_port`) so the AP notices the link change and
  renews immediately.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Callable, Generator, Optional

import routeros_api

from .config import VlanConfig
from .port_lists import add_port, remove_port

logger = logging.getLogger(__name__)

PoolFactory = Callable[[], "routeros_api.RouterOsApiPool"]


class MikroTikError(Exception):
    """Base class for MikroTik client errors."""


class MikroTikConnectionError(MikroTikError):
    """Raised when the switch is unreachable or the API connection drops."""


class PortNotFoundError(MikroTikError):
    """The given interface is not a port of the configured bridge."""


class VlanNotConfiguredError(MikroTikError):
    """No static ``/interface/bridge/vlan`` entry exists for a VLAN ID.

    Run the setup script to pre-create the required static VLAN entries.
    """


class Dot1xEntryNotFoundError(MikroTikError):
    """No ``/interface/dot1x/server`` entry exists for a port.

    Run the setup script to pre-create the required dot1x-server entries.
    """


class MikroTikClient:
    """Thin, testable wrapper around the RouterOS API for one switch."""

    BRIDGE_VLAN_PATH = "/interface/bridge/vlan"
    BRIDGE_PORT_PATH = "/interface/bridge/port"
    BRIDGE_HOST_PATH = "/interface/bridge/host"
    DOT1X_SERVER_PATH = "/interface/dot1x/server"
    ETHERNET_POE_PATH = "/interface/ethernet/poe"
    INTERFACE_PATH = "/interface"
    SCRIPT_PATH = "/system/script"
    NETWATCH_PATH = "/tool/netwatch"

    # 802.1X with MAC-auth fallback, so clients that don't speak EAPOL can
    # still authenticate by MAC address; timeouts per spec (10/5).
    DOT1X_AUTH_TYPES = "dot1x,mac-auth"
    DOT1X_AUTH_TIMEOUT = "10"
    DOT1X_RETRANS_TIMEOUT = "5"
    DOT1X_RADIUS_MAC_FORMAT = "xxxxxxxxxxxx"

    def __init__(
        self,
        *,
        name: str,
        host: str,
        username: str,
        password: str,
        port: int = 8729,
        bridge: str = "bridge1",
        pool_factory: Optional[PoolFactory] = None,
        flap_settle_time: float = 2.0,
    ) -> None:
        self.name = name
        self.host = host
        self.bridge = bridge
        self._username = username
        self._password = password
        self._port = port
        self._pool_factory = pool_factory or self._default_pool_factory
        self._pool: Optional["routeros_api.RouterOsApiPool"] = None
        self._api = None
        self._flap_settle_time = flap_settle_time

    def _default_pool_factory(self) -> "routeros_api.RouterOsApiPool":
        return routeros_api.RouterOsApiPool(
            host=self.host,
            username=self._username,
            password=self._password,
            port=self._port,
            use_ssl=True,
            ssl_verify=False,
            ssl_verify_hostname=False,
            plaintext_login=True,
        )

    # -- connection management -------------------------------------------------

    @contextmanager
    def _connection_guard(self) -> Generator[None, None, None]:
        """Convert any RouterOsApiConnectionError to MikroTikConnectionError.

        Wraps both connection setup and mid-operation failures (timeouts, dropped
        sockets) so callers only need to handle one exception type.
        """
        try:
            yield
        except routeros_api.exceptions.RouterOsApiConnectionError as exc:
            self.close()
            raise MikroTikConnectionError(f"{self.name}: {exc}") from exc

    def connect(self):
        if self._api is None:
            logger.debug("%s: connecting to %s:%s", self.name, self.host, self._port)
            with self._connection_guard():
                self._pool = self._pool_factory()
                self._api = self._pool.get_api()
        return self._api

    def close(self) -> None:
        try:
            if self._pool is not None:
                self._pool.disconnect()
        finally:
            self._pool = None
            self._api = None

    def __enter__(self) -> "MikroTikClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def api(self):
        return self.connect()

    # -- discovery ---------------------------------------------------------------

    def get_bridge_hosts(self) -> dict[str, str]:
        """Return ``{mac (lowercase): bridge port}`` for this switch's bridge.

        One ``/interface/bridge/host print`` call covers every learned MAC,
        so callers should fetch this once per poll cycle rather than calling
        :meth:`find_port_by_mac` per AP.
        """
        with self._connection_guard():
            hosts = self.api.get_resource(self.BRIDGE_HOST_PATH)
            result: dict[str, str] = {}
            for entry in hosts.get():
                if entry.get("bridge") and entry["bridge"] != self.bridge:
                    continue
                mac = entry.get("mac-address")
                on_interface = entry.get("on-interface")
                if mac and on_interface:
                    result.setdefault(mac.lower(), on_interface)
            return result

    def find_port_by_mac(self, mac: str) -> Optional[str]:
        """Return the bridge port name a MAC address was learned on, if any."""
        return self.get_bridge_hosts().get(mac.lower())

    def get_vlan_macs_by_port(self, vlan_id: int) -> dict[str, list[str]]:
        """Return ``{port: [mac, ...]}`` for **external** MACs on ``vlan_id``.

        Only entries with ``local != "true"`` are counted — the bridge's own
        MAC (flag ``L`` in the CLI) also appears in the host table on the
        management VLAN but is not a client device and must be excluded.
        Uses the ``vid`` field present when VLAN filtering is enabled on the
        bridge; entries without a ``vid`` field are silently skipped.
        """
        with self._connection_guard():
            hosts = self.api.get_resource(self.BRIDGE_HOST_PATH)
            result: dict[str, list[str]] = {}
            for entry in hosts.get():
                if entry.get("bridge") and entry["bridge"] != self.bridge:
                    continue
                if entry.get("local") == "true":
                    continue
                if entry.get("vid") != str(vlan_id):
                    continue
                mac = entry.get("mac-address")
                on_interface = entry.get("on-interface")
                if mac and on_interface:
                    result.setdefault(on_interface, []).append(mac.lower())
            return result

    def get_poe_out_status(self, port: str) -> Optional[str]:
        """Return the ``poe-out-status`` string for ``port``, or ``None`` if unavailable.

        ``None`` means the switch has no PoE entry for this port or the path is
        unsupported — callers skip the PoE check in that case.
        ``MikroTikConnectionError`` is re-raised so the poll cycle is aborted.
        """
        try:
            with self._connection_guard():
                poe = self.api.get_resource(self.ETHERNET_POE_PATH)
                entries = poe.get(interface=port)
        except MikroTikConnectionError:
            raise
        except Exception:
            return None
        if not entries:
            return None
        return entries[0].get("poe-out-status")

    # -- port mode switching -------------------------------------------------------

    def set_port_mode(self, port: str, mode: str, vlans: VlanConfig) -> None:
        """Switch ``port`` into ``"trunk"`` or ``"onboarding"`` mode."""
        with self._connection_guard():
            if mode == "trunk":
                self._apply_trunk(port, vlans)
            elif mode == "onboarding":
                self._apply_onboarding(port, vlans)
            else:
                raise ValueError(f"unknown port mode: {mode!r}")

    def _apply_trunk(self, port: str, vlans: VlanConfig) -> None:
        """AP came online: open the port up for management + client VLANs.

        VLAN membership and PVID are configured *before* dot1x is disabled,
        so the port never carries trunk traffic while still unauthenticated.
        The port is then flapped so the AP picks up its new VLAN immediately
        instead of sitting on a stale DHCP lease.
        """
        self._remove_from_vlan(vlans.onboarding, port, untagged=True)
        self._add_to_vlan(vlans.management, port, untagged=True)
        for vid in vlans.trunk:
            self._add_to_vlan(vid, port, tagged=True)
        self._set_pvid(port, vlans.management)
        self.set_dot1x(port, enabled=False)
        self.flap_port(port)

    def _apply_onboarding(self, port: str, vlans: VlanConfig) -> None:
        """AP went offline: lock the port back down.

        dot1x is re-enabled *first* (blocking all non-EAPOL traffic
        immediately), then VLAN membership/PVID are reverted, and finally
        the port is flapped so a still-connected device renews onto the
        onboarding VLAN right away.
        """
        self.set_dot1x(port, enabled=True)
        self._set_pvid(port, vlans.onboarding)
        for vid in vlans.trunk:
            self._remove_from_vlan(vid, port, tagged=True)
        self._remove_from_vlan(vlans.management, port, untagged=True)
        self._add_to_vlan(vlans.onboarding, port, untagged=True)
        self.flap_port(port)

    def flap_port(self, port: str) -> None:
        """Briefly disable then re-enable the ``/interface`` entry for ``port``.

        Forces a link-down/link-up event so a connected device's NIC
        notices the change immediately (and renews its DHCP lease) rather
        than waiting out its lease/heartbeat timeout after a PVID/VLAN
        change that doesn't otherwise affect link state. Best-effort: if no
        ``/interface`` entry is found for ``port``, this is a no-op.
        """
        interfaces = self.api.get_resource(self.INTERFACE_PATH)
        entries = interfaces.get(name=port)
        if not entries:
            logger.warning("%s: cannot flap %s, no /interface entry found", self.name, port)
            return
        entry_id = entries[0]["id"]
        interfaces.set(id=entry_id, disabled="yes")
        if self._flap_settle_time:
            time.sleep(self._flap_settle_time)
        interfaces.set(id=entry_id, disabled="no")

    def get_port_link_status(self, port: str) -> Optional[bool]:
        """Return whether ``port``'s physical link is up (``running``).

        Returns ``None`` if no ``/interface`` entry is found for ``port``.
        Unlike the UniFi controller's heartbeat-based offline detection
        (which can take tens of seconds), this reflects the switch's own
        link state and is effectively instant.
        """
        with self._connection_guard():
            interfaces = self.api.get_resource(self.INTERFACE_PATH)
            entries = interfaces.get(name=port)
            if not entries:
                return None
            return entries[0].get("running") == "true"

    # -- bridge port / VLAN table helpers -------------------------------------------

    def _set_pvid(self, port: str, vlan_id: int) -> None:
        ports = self.api.get_resource(self.BRIDGE_PORT_PATH)
        entries = ports.get(interface=port)
        entries = [e for e in entries if e.get("bridge") == self.bridge]
        if not entries:
            raise PortNotFoundError(
                f"{self.name}: {port} is not a port of bridge {self.bridge}"
            )
        ports.set(id=entries[0]["id"], pvid=str(vlan_id))

    def _get_static_vlan_entry(self, vlan_id: int) -> dict:
        vlans = self.api.get_resource(self.BRIDGE_VLAN_PATH)
        for entry in vlans.get(bridge=self.bridge):
            if entry.get("vlan-ids") == str(vlan_id):
                return entry
        raise VlanNotConfiguredError(
            f"{self.name}: no static VLAN {vlan_id} entry on bridge {self.bridge}; "
            "run the setup script first"
        )

    def _add_to_vlan(self, vlan_id: int, port: str, *, tagged: bool = False, untagged: bool = False) -> None:
        vlans = self.api.get_resource(self.BRIDGE_VLAN_PATH)
        entry = self._get_static_vlan_entry(vlan_id)
        update = {}
        if tagged:
            update["tagged"] = add_port(entry.get("tagged"), port)
        if untagged:
            update["untagged"] = add_port(entry.get("untagged"), port)
        if update:
            vlans.set(id=entry["id"], **update)

    def _remove_from_vlan(self, vlan_id: int, port: str, *, tagged: bool = False, untagged: bool = False) -> None:
        vlans = self.api.get_resource(self.BRIDGE_VLAN_PATH)
        entry = self._get_static_vlan_entry(vlan_id)
        update = {}
        if tagged:
            update["tagged"] = remove_port(entry.get("tagged"), port)
        if untagged:
            update["untagged"] = remove_port(entry.get("untagged"), port)
        if update:
            vlans.set(id=entry["id"], **update)

    # -- dot1x -----------------------------------------------------------------------

    def set_dot1x(self, port: str, enabled: bool) -> None:
        """Enable/disable the pre-created dot1x-server entry for ``port``."""
        dot1x = self.api.get_resource(self.DOT1X_SERVER_PATH)
        entries = dot1x.get(interface=port)
        if not entries:
            raise Dot1xEntryNotFoundError(
                f"{self.name}: no dot1x-server entry for {port}; run the setup script first"
            )
        dot1x.set(id=entries[0]["id"], disabled="no" if enabled else "yes")

    def get_dot1x_disabled(self, port: str) -> Optional[bool]:
        """Return whether the dot1x-server entry for ``port`` is disabled.

        ``set_dot1x`` writes ``"yes"``/``"no"`` (the CLI convention, which
        the API accepts), but RouterOS's API can normalize boolean
        properties to ``"true"``/``"false"`` on read-back - both are
        accepted here. Returns ``None`` if no ``/interface/dot1x/server``
        entry exists for ``port``.
        """
        with self._connection_guard():
            dot1x = self.api.get_resource(self.DOT1X_SERVER_PATH)
            entries = dot1x.get(interface=port)
            if not entries:
                return None
            return entries[0].get("disabled") in ("yes", "true")

    # -- setup helpers (used by scripts/setup_switches.py) ----------------------------

    def ensure_static_vlan(self, vlan_id: int, *, tagged: str = "", untagged: str = "") -> None:
        """Create a static ``/interface/bridge/vlan`` entry if missing."""
        vlans = self.api.get_resource(self.BRIDGE_VLAN_PATH)
        for entry in vlans.get(bridge=self.bridge):
            if entry.get("vlan-ids") == str(vlan_id):
                return
        vlans.add(bridge=self.bridge, vlan_ids=str(vlan_id), tagged=tagged, untagged=untagged)
        logger.info("%s: created static VLAN %s on %s", self.name, vlan_id, self.bridge)

    def ensure_dot1x_entry(self, port: str, *, enabled: bool) -> None:
        """Create or update the ``/interface/dot1x/server`` entry for ``port``.

        Always (re-)applies ``auth-types`` (dot1x + mac-auth fallback) and the
        auth/retrans timeouts. ``enabled`` only seeds the initial ``disabled``
        state for newly created entries - the watchdog manages ``disabled``
        afterwards via :meth:`set_dot1x`, so existing entries are left as-is.
        """
        dot1x = self.api.get_resource(self.DOT1X_SERVER_PATH)
        entries = dot1x.get(interface=port)
        config_kwargs = dict(
            auth_types=self.DOT1X_AUTH_TYPES,
            auth_timeout=self.DOT1X_AUTH_TIMEOUT,
            retrans_timeout=self.DOT1X_RETRANS_TIMEOUT,
            radius_mac_format=self.DOT1X_RADIUS_MAC_FORMAT,
        )
        if entries:
            dot1x.set(id=entries[0]["id"], **config_kwargs)
            return
        dot1x.add(interface=port, disabled="no" if enabled else "yes", **config_kwargs)
        logger.info("%s: created dot1x-server entry for %s", self.name, port)

    def ensure_script(self, name: str, source: str) -> None:
        """Create or update a ``/system/script`` entry."""
        scripts = self.api.get_resource(self.SCRIPT_PATH)
        entries = scripts.get(name=name)
        if entries:
            scripts.set(id=entries[0]["id"], source=source)
            logger.info("%s: updated script %s", self.name, name)
        else:
            scripts.add(name=name, source=source, policy="read,write,policy,test")
            logger.info("%s: created script %s", self.name, name)

    def ensure_netwatch(
        self,
        *,
        host: str,
        interval: str,
        timeout: str,
        up_script: str = "",
        down_script: str = "",
        comment: str = "",
    ) -> None:
        """Create or update a ``/tool/netwatch`` entry monitoring ``host``."""
        netwatch = self.api.get_resource(self.NETWATCH_PATH)
        kwargs = dict(
            host=host,
            interval=interval,
            timeout=timeout,
            up_script=up_script,
            down_script=down_script,
            comment=comment,
        )
        entries = netwatch.get(host=host)
        if entries:
            netwatch.set(id=entries[0]["id"], **kwargs)
            logger.info("%s: updated netwatch entry for %s", self.name, host)
        else:
            netwatch.add(**kwargs)
            logger.info("%s: created netwatch entry for %s", self.name, host)

    def get_port_pvid(self, port: str) -> Optional[str]:
        """Return the current PVID of ``port``, or ``None`` if not found."""
        with self._connection_guard():
            ports = self.api.get_resource(self.BRIDGE_PORT_PATH)
            for entry in ports.get(interface=port):
                if entry.get("bridge") == self.bridge:
                    return entry.get("pvid")
            return None
