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

    def get_known_ap_macs(self) -> set[str]:
        """Return the MAC addresses (lowercase) of all APs known to the controller.

        This deliberately ignores the per-device ``state`` (online/offline)
        field, which the controller derives from missed heartbeats and can
        lag 30-70+ seconds behind reality after a VLAN change. The watchdog
        only uses this as a security allowlist - "is this MAC address a
        UniFi AP the controller knows about" - and answers "is it actually
        present right now" from the switch's own bridge host table and link
        state instead, which are effectively instant.
        """
        devices = self._get_devices()
        macs = set()
        for device in devices:
            if device.get("type") != AP_DEVICE_TYPE:
                continue
            mac = device.get("mac")
            if mac:
                macs.add(mac.lower())
        return macs

    def _get_devices(self) -> list[dict]:
        if self._api_prefix is None:
            self.login()

        url = f"{self.base_url}{self._api_prefix}/api/s/{self.site}/stat/device"
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code == 401:
            # Session expired - log in again and retry once.
            self.login()
            url = f"{self.base_url}{self._api_prefix}/api/s/{self.site}/stat/device"
            resp = self.session.get(url, timeout=self.timeout)

        if resp.status_code != 200:
            raise UniFiError(
                f"fetching devices from {self.base_url} failed with HTTP {resp.status_code}"
            )

        return resp.json().get("data", [])

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "UniFiClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
