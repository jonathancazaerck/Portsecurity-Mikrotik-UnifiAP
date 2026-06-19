"""Helpers for RouterOS comma-separated port-list properties.

``/interface/bridge/vlan`` entries represent their ``tagged``/``untagged``
port membership as a single comma-separated string (e.g. ``"ether2,ether3"``).
These helpers add/remove a single port while preserving the rest of the list.
"""

from __future__ import annotations


def parse_port_list(value: str | None) -> list[str]:
    """Split a RouterOS port-list string into individual interface names."""
    if not value:
        return []
    return [port for port in value.split(",") if port]


def format_port_list(ports: list[str]) -> str:
    """Join interface names back into a RouterOS port-list string."""
    return ",".join(ports)


def add_port(value: str | None, port: str) -> str:
    """Return ``value`` with ``port`` added, if not already present."""
    ports = parse_port_list(value)
    if port not in ports:
        ports.append(port)
    return format_port_list(ports)


def remove_port(value: str | None, port: str) -> str:
    """Return ``value`` with ``port`` removed, if present."""
    ports = [p for p in parse_port_list(value) if p != port]
    return format_port_list(ports)
