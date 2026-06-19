"""Configuration loading for the AP switch watchdog.

Configuration lives in a YAML file (see ``config/config.example.yaml``).
Credentials are never stored in the file itself - they are read from
environment variables referenced via ``password_env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(Exception):
    """Raised for missing/invalid configuration."""


@dataclass(frozen=True)
class VlanConfig:
    onboarding: int
    management: int
    trunk: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class UniFiConfig:
    url: str
    username: str
    password: str
    site: str = "default"
    verify_ssl: bool = False


@dataclass(frozen=True)
class SwitchConfig:
    name: str
    host: str
    username: str
    password: str
    port: int = 8729
    bridge: str = "bridge1"
    ap_ports: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NetwatchConfig:
    watchdog_host: str
    interval: str = "10s"
    probe_timeout: str = "1s"


@dataclass(frozen=True)
class WatchdogConfig:
    poll_interval: int
    unifi: UniFiConfig
    vlans: VlanConfig
    switches: list[SwitchConfig]
    netwatch: NetwatchConfig


def _resolve_password(raw: dict, *, context: str) -> str:
    """Resolve a password from ``password`` or ``password_env``."""
    if "password_env" in raw:
        env_var = raw["password_env"]
        value = os.environ.get(env_var)
        if not value:
            raise ConfigError(
                f"{context}: environment variable {env_var!r} is not set"
            )
        return value
    if "password" in raw:
        return str(raw["password"])
    raise ConfigError(f"{context}: neither 'password' nor 'password_env' configured")


def load_config(path: str | Path) -> WatchdogConfig:
    """Load and validate the watchdog configuration from ``path``."""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top-level YAML document must be a mapping")

    try:
        unifi_raw = raw["unifi"]
        unifi = UniFiConfig(
            url=unifi_raw["url"].rstrip("/"),
            username=unifi_raw["username"],
            password=_resolve_password(unifi_raw, context="unifi"),
            site=unifi_raw.get("site", "default"),
            verify_ssl=bool(unifi_raw.get("verify_ssl", False)),
        )

        vlans_raw = raw["vlans"]
        vlans = VlanConfig(
            onboarding=int(vlans_raw["onboarding"]),
            management=int(vlans_raw["management"]),
            trunk=[int(v) for v in vlans_raw.get("trunk", [])],
        )

        switches = []
        for sw in raw["switches"]:
            switches.append(
                SwitchConfig(
                    name=sw["name"],
                    host=sw["host"],
                    username=sw["username"],
                    password=_resolve_password(sw, context=f"switch {sw.get('name')}"),
                    port=int(sw.get("port", 8729)),
                    bridge=sw.get("bridge", "bridge1"),
                    ap_ports=list(sw.get("ap_ports", [])),
                )
            )
        if not switches:
            raise ConfigError(f"{path}: 'switches' must contain at least one entry")

        netwatch_raw = raw["netwatch"]
        netwatch = NetwatchConfig(
            watchdog_host=netwatch_raw["watchdog_host"],
            interval=netwatch_raw.get("interval", "10s"),
            probe_timeout=netwatch_raw.get("probe_timeout", "1s"),
        )

        return WatchdogConfig(
            poll_interval=int(raw.get("poll_interval", 10)),
            unifi=unifi,
            vlans=vlans,
            switches=switches,
            netwatch=netwatch,
        )
    except KeyError as exc:
        raise ConfigError(f"{path}: missing required key {exc.args[0]!r}") from exc
