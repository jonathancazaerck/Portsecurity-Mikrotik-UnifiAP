"""Shared test fixtures: a small fake RouterOS API.

Mimics just enough of ``routeros_api`` (``RouterOsApiPool`` /
``get_resource().get()/.add()/.set()/.remove()``) for the client code under
test, including its underscore-to-hyphen argument convention and the
``id`` <-> ``.id`` mapping (see ``key_cleaner_decorator.py`` in
RouterOS-api), so the unit tests exercise the same call shapes
``mikrotik_client`` makes against the real library.
"""

from __future__ import annotations

import itertools

import pytest

from ap_switch_watchdog.config import SwitchConfig, VlanConfig
from ap_switch_watchdog.mikrotik_client import MikroTikClient


def _encode_key(key: str) -> str:
    if key == "id":
        return "id"
    return key.replace("_", "-")


class FakeResource:
    def __init__(self, table: list[dict], id_counter: itertools.count):
        self._table = table
        self._id_counter = id_counter

    def get(self, **kwargs) -> list[dict]:
        if not kwargs:
            return [dict(entry) for entry in self._table]
        filters = {_encode_key(k): str(v) for k, v in kwargs.items()}
        return [
            dict(entry)
            for entry in self._table
            if all(str(entry.get(k)) == v for k, v in filters.items())
        ]

    def add(self, **kwargs) -> str:
        entry = {_encode_key(k): v for k, v in kwargs.items()}
        entry["id"] = f"*{next(self._id_counter)}"
        self._table.append(entry)
        return entry["id"]

    def set(self, **kwargs) -> None:
        entry_id = kwargs.pop("id")
        for entry in self._table:
            if entry.get("id") == entry_id:
                for k, v in kwargs.items():
                    entry[_encode_key(k)] = v
                return
        raise KeyError(f"no entry with id {entry_id!r}")

    def remove(self, **kwargs) -> None:
        entry_id = kwargs["id"]
        self._table[:] = [e for e in self._table if e.get("id") != entry_id]


class FakeApi:
    def __init__(self, db: dict[str, list[dict]]):
        self._db = db
        self._id_counter = itertools.count(1)

    def get_resource(self, path: str) -> FakeResource:
        return FakeResource(self._db.setdefault(path, []), self._id_counter)


class FakeRouterOsApiPool:
    def __init__(self, db: dict[str, list[dict]]):
        self._db = db
        self.disconnected = False

    def get_api(self) -> FakeApi:
        return FakeApi(self._db)

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def vlans() -> VlanConfig:
    return VlanConfig(onboarding=99, management=10, trunk=[30, 50])


@pytest.fixture
def switch_config() -> SwitchConfig:
    return SwitchConfig(
        name="sw01",
        host="192.0.2.2",
        username="watchdog",
        password="secret",
        port=8729,
        bridge="bridge1",
        ap_ports=["ether2", "ether3", "ether4"],
    )


def seed_baseline(db: dict[str, list[dict]], switch: SwitchConfig, vlans: VlanConfig) -> None:
    """Populate ``db`` as if the setup script had already run.

    All ``switch.ap_ports`` start in the onboarding baseline: PVID =
    onboarding VLAN, untagged member of the onboarding VLAN, dot1x active.
    """
    bridge = switch.bridge
    counter = itertools.count(1)

    ports_table = db.setdefault("/interface/bridge/port", [])
    for port in switch.ap_ports:
        ports_table.append(
            {"id": f"*{next(counter)}", "bridge": bridge, "interface": port, "pvid": str(vlans.onboarding)}
        )

    vlan_table = db.setdefault("/interface/bridge/vlan", [])
    onboarding_untagged = ",".join(switch.ap_ports)
    for vid in (vlans.onboarding, vlans.management, *vlans.trunk):
        entry = {"id": f"*{next(counter)}", "bridge": bridge, "vlan-ids": str(vid), "tagged": "", "untagged": ""}
        if vid == vlans.onboarding:
            entry["untagged"] = onboarding_untagged
        vlan_table.append(entry)

    dot1x_table = db.setdefault("/interface/dot1x/server", [])
    for port in switch.ap_ports:
        dot1x_table.append({"id": f"*{next(counter)}", "interface": port, "disabled": "no"})

    interface_table = db.setdefault("/interface", [])
    for port in switch.ap_ports:
        interface_table.append({"id": f"*{next(counter)}", "name": port, "disabled": "no", "running": "true"})


def make_client(
    db: dict[str, list[dict]] | None = None,
    *,
    switch: SwitchConfig | None = None,
    flap_settle_time: float = 0,
) -> MikroTikClient:
    db = db if db is not None else {}
    switch = switch or SwitchConfig(
        name="sw01", host="192.0.2.2", username="watchdog", password="secret", bridge="bridge1"
    )
    return MikroTikClient(
        name=switch.name,
        host=switch.host,
        username=switch.username,
        password=switch.password,
        port=switch.port,
        bridge=switch.bridge,
        pool_factory=lambda: FakeRouterOsApiPool(db),
        flap_settle_time=flap_settle_time,
    )
