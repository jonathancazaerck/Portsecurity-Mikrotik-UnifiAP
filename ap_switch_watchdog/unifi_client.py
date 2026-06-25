"""Minimal UniFi Network Controller client.

Only what the watchdog needs: log in and ask "which MAC addresses belong to
access points known to this controller?".

Supports both controller flavours:

* UniFi OS (UDM/UDM-Pro/Cloud Gateway, Network Server appliance): login via
  ``POST /api/auth/login``, all further calls under ``/proxy/network``.
* Classic software controller: login via ``POST /api/login``, calls directly
  under ``/api``.

The controller type is auto-detected on first login and cached.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests
import urllib3

logger = logging.getLogger(__name__)

# Device "type" reported by the controller for UniFi access points.
AP_DEVICE_TYPE = "uap"
# ``state`` values that count as "managed by this controller".
# 1 = connected, 5 = provisioning (controller is actively pushing config).
# Both are sufficient for the trunk grant: the AP is adopted, present, and
# drawing PoE — a spoofer cannot replicate that without the real AP also
# talking to the controller simultaneously.
STATES_MANAGED = {1, 5}


class UniFiError(Exception):
    """Raised on UniFi controller communication/authentication failures."""


class UniFiClient:
    def __init__(
        self,
        *,
        url: str,
        username: str,
        password: str,
        site: str = "default",
        verify_ssl: bool = False,
        timeout: float = 10,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = url.rstrip("/")
        self.username = username
        self.password = password
        self.site = site
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.verify = verify_ssl
        # "/proxy/network" for UniFi OS, "" for a classic controller.
        self._api_prefix: Optional[str] = None

        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def login(self) -> None:
        """Authenticate, auto-detecting UniFi OS vs. classic controller."""
        payload = {"username": self.username, "password": self.password}

        try:
            resp = self.session.post(
                f"{self.base_url}/api/auth/login", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise UniFiError(f"could not reach {self.base_url}: {exc}") from exc

        if resp.status_code == 200:
            self._api_prefix = "/proxy/network"
            logger.debug("logged in to %s as UniFi OS controller", self.base_url)
            return

        try:
            resp = self.session.post(
                f"{self.base_url}/api/login", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise UniFiError(f"could not reach {self.base_url}: {exc}") from exc

        if resp.status_code != 200:
            raise UniFiError(
                f"login to {self.base_url} failed with HTTP {resp.status_code}"
            )
        self._api_prefix = ""
        logger.debug("logged in to %s as classic controller", self.base_url)

    def get_ap_macs(self) -> tuple[set[str], set[str]]:
        """Return ``(known_macs, connected_macs)`` for all APs on this controller.

        Both sets contain lowercase-normalised MAC addresses and are computed
        from a single ``stat/device`` API call.

        ``known_macs`` — the security allowlist: every AP MAC address that has
        ever been adopted by this controller, regardless of current online /
        offline state.  Used to identify a device as "AP" vs. "unknown client"
        from the switch bridge host table.

        ``connected_macs`` — subset of ``known_macs`` where ``state ==
        STATE_CONNECTED``, meaning the AP currently has an active management
        session with the controller.  Used as a second guard for the
        onboarding → trunk transition: a device spoofing a known-but-offline
        AP's MAC will not be granted trunk access, because the real AP is not
        simultaneously connected to the controller.
        """
        devices = self._get_devices()
        known: set[str] = set()
        connected: set[str] = set()
        for device in devices:
            if device.get("type") != AP_DEVICE_TYPE:
                continue
            mac = device.get("mac")
            if not mac:
                continue
            mac = mac.lower()
            known.add(mac)
            if device.get("state") in STATES_MANAGED:
                connected.add(mac)
        return known, connected

    def _get(self, url: str) -> requests.Response:
        try:
            return self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise UniFiError(f"request to {url} failed: {exc}") from exc

    def _get_devices(self) -> list[dict]:
        if self._api_prefix is None:
            self.login()

        url = f"{self.base_url}{self._api_prefix}/api/s/{self.site}/stat/device"
        resp = self._get(url)
        if resp.status_code == 401:
            # Session expired - log in again and retry once.
            self.login()
            url = f"{self.base_url}{self._api_prefix}/api/s/{self.site}/stat/device"
            resp = self._get(url)

        if resp.status_code == 404:
            # Newer standalone Network Application also accepts /api/auth/login,
            # so the prefix auto-detection can misidentify it as UniFi OS.
            # Try the other prefix once to self-correct.
            other_prefix = "" if self._api_prefix == "/proxy/network" else "/proxy/network"
            alt_url = f"{self.base_url}{other_prefix}/api/s/{self.site}/stat/device"
            try:
                alt_resp = self._get(alt_url)
            except UniFiError:
                alt_resp = None
            if alt_resp is not None and alt_resp.status_code == 200:
                logger.debug(
                    "auto-corrected API prefix from %r to %r",
                    self._api_prefix,
                    other_prefix,
                )
                self._api_prefix = other_prefix
                return alt_resp.json().get("data", [])

        if resp.status_code != 200:
            raise UniFiError(
                f"fetching devices from {self.base_url} failed with HTTP {resp.status_code}"
                f" (site={self.site!r}; check site name in config)"
            )

        return resp.json().get("data", [])

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "UniFiClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
