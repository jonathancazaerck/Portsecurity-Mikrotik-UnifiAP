# ap-switch-watchdog

Secures MikroTik switch ports that UniFi APs are plugged into. By default a
port sits in an **onboarding VLAN** with **802.1X (dot1x) active** - it can't
pass any traffic except EAPOL. Every poll cycle, the watchdog asks the switch
which MAC addresses it currently sees on each `ap_port` (bridge host table +
link state) and checks those against the UniFi controller's list of known AP
MAC addresses. If all conditions are met the port becomes a **management+client
trunk** with dot1x disabled. If not (AP unplugged, moved elsewhere, MAC
disappeared, or the checks fail), the port is reverted to onboarding.

```
        UniFi controller                    MikroTik switch
   "which MACs are known APs?"        bridge host table, link state,
   "which are currently connected?"   PVID, dot1x, PoE status
      (poll every poll_interval)         (same interval)
              │                                     │
              └──────────────────┬──────────────────┘
                                  ▼
                       ┌──────────────────┐
                       │  ap-switch-       │  per ap_port: all checks pass?
                       │  watchdog         │
                       └──────────────────┘
                                  │ RouterOS API (SSL, 8729)
                                  ▼
   ┌─────────────────────────────────────────────────────────┐
   │ no  ──► PVID=99 (onboarding), untagged VLAN 99           │
   │         dot1x-server: enabled                            │
   │                                                           │
   │ yes ──► PVID=10 (management), untagged VLAN 10           │
   │         tagged VLAN 30, 50 (client SSIDs)                │
   │         dot1x-server: disabled                           │
   │                                                           │
   │ Netwatch dead-man's-switch: if the watchdog host stops   │
   │ responding, revert all AP ports to onboarding locally.   │
   └─────────────────────────────────────────────────────────┘
```

## Layout

```
ap_switch_watchdog/
  config.py          # YAML config loading, passwords from env vars
  unifi_client.py     # UniFi controller: known + connected AP MAC addresses
  mikrotik_client.py  # RouterOS API: bridge host table, link/PVID/dot1x/PoE, mode switching
  port_lists.py       # helpers for "tagged"/"untagged" comma-separated port lists
  routeros_script.py  # generates the on-switch failsafe revert script (RSC)
  watchdog.py         # main reconciliation loop
  cli.py              # `ap-switch-watchdog` entry point
scripts/
  setup_switches.py   # `ap-switch-watchdog-setup` - one-time/idempotent switch setup
config/
  config.example.yaml
tests/                # unit tests with a mocked RouterOS API
```

## Configuration

Copy `config/config.example.yaml` to `config.yaml` and adjust it. Credentials
are **not** stored in the file - set:

```sh
export WATCHDOG_UNIFI_PASSWORD='...'
export WATCHDOG_MIKROTIK_PASSWORD='...'
```

(referenced from `config.yaml` via `password_env`). Add more switches by
appending entries to the `switches:` list - each needs its own
`password_env` if credentials differ.

`vlans.onboarding` / `management` / `trunk` apply to **every** switch, so all
switches must share the same VLAN numbering.

`trunk_grace_period` (default 120 s) controls how long a trunked port is kept
open after its AP's UniFi management session disappears - see "Anti-MAC-spoofing
checks" below.

## Setup (run once per switch, and again whenever `ap_ports`/VLANs change)

```sh
pip install -e .
python scripts/setup_switches.py -c config.yaml
```

This is **idempotent** - safe to re-run. It assumes `ap_ports` are already
bridge ports (i.e. you've already done `/interface/bridge/port add
bridge=bridge1 interface=etherX` and enabled `vlan-filtering=yes` on the
bridge). It then:

1. Creates **static** `/interface/bridge/vlan` entries for VLANs 99, 10, 30,
   50 on the configured bridge. RouterOS 7 auto-creates *dynamic* VLAN
   entries from a port's PVID, and dynamic entries can't be edited via the
   API - the watchdog needs static entries to exist so it can `set` their
   `tagged`/`untagged` port lists.
2. Creates (or updates) a `/interface/dot1x/server` entry for every
   `ap_ports` port - `auth-types=dot1x,mac-auth` (MAC-auth fallback for
   clients that don't speak EAPOL), `auth-timeout=10`,
   `retrans-timeout=5`, `radius-mac-format=xxxxxxxxxxxx` - and puts each
   port into its baseline onboarding
   state (PVID 99, untagged member of VLAN 99, dot1x active).
3. Installs the generated failsafe script (`ap-switch-watchdog-failsafe-revert`,
   see below) and a `/tool/netwatch` entry.

Use `--switch sw01` to limit setup to one switch, or
`--print-failsafe-script` to inspect the generated RouterOS script without
connecting to anything.

## Running

```sh
ap-switch-watchdog -c config.yaml          # run forever, polling every poll_interval seconds
ap-switch-watchdog -c config.yaml --once   # single poll cycle, for cron/debugging
ap-switch-watchdog -c config.yaml -v       # debug logging
```

### Reconciliation loop

Every `poll_interval` seconds, for each switch:

1. Fetch the bridge host table (`/interface/bridge/host`) once, building a
   `port -> [MACs]` map of what the switch currently sees on each `ap_port`.
   Also fetch the host table filtered to the management VLAN to count external
   (non-local) MACs per port.
2. Fetch the set of MAC addresses UniFi knows about as access points
   (`stat/device`, `type == "uap"`), split into two sets:
   - `known_macs` - every ever-adopted AP MAC, regardless of online/offline state.
   - `connected_macs` - subset where the AP currently has an active management
     session with the controller (`state == 1`).
3. For each `ap_port`, compute the **desired mode** using an asymmetric rule
   (see "Anti-MAC-spoofing checks" below).
4. Compare against the port's **actual** current mode, read live from the
   switch (PVID == management VLAN *and* dot1x disabled => `trunk`,
   otherwise `onboarding`). If actual != desired, `set_port_mode` applies the
   VLAN/dot1x change - dot1x disabled *last* when opening a port, re-enabled
   *first* when locking it down - and flaps the port (see "Port flap on mode
   change" below).

Ports changed this cycle are skipped on the *next* poll, giving the link and
bridge host table one `poll_interval` to settle after the flap before being
reconsidered.

There is **no persisted state** - every poll fully reconciles each port from
live switch + UniFi data, so a restart picks up exactly where the switches
currently are.

### Anti-MAC-spoofing checks

The desired mode is computed using an **asymmetric rule** that closes the
MAC-spoofing window without reintroducing heartbeat-lag false reverts:

**onboarding → trunk** requires ALL of:
- Link up (`/interface running`).
- A `known_macs` AP MAC learned on this port by the bridge.
- That MAC also in `connected_macs` (active UniFi management session). A device
  spoofing the MAC of a registered-but-offline AP is not granted trunk access -
  the real AP would need to be simultaneously online, which is impossible to
  fake without access to the controller.
- PoE `powered-on` on the port (if the switch reports PoE status). A laptop or
  other non-PoE device spoofing an AP MAC will not draw PoE power.

**trunk → onboarding** triggers on ANY of:
- Link drops.
- The AP's MAC disappears from the bridge host table (AP rebooted, unplugged,
  moved).
- More than one **external** (non-local) MAC visible on the management VLAN on
  this port - indicates a hub, bridge, or second device behind the port.
- PoE is no longer `powered-on` (if reported by the switch).
- The AP has been absent from `connected_macs` for longer than
  `trunk_grace_period` seconds (default 120 s). Within the grace window the
  connected check is deliberately skipped to absorb the 30-70 s UniFi
  heartbeat gap that occurs right after every mode switch. Once the window
  expires a device that assumed a known AP's MAC while the real AP was offline
  is detected and the port is reverted.

### Why the onboarding → trunk transition checks `connected_macs`

UniFi's per-device `state` field (connected/disconnected) is heartbeat-based
and can lag 30-70+ seconds behind reality. This matters in two different ways:

- **For granting trunk** (`onboarding → trunk`): the lag is acceptable. A real
  AP will be seen as connected within seconds of its first poll after booting,
  and the lag only adds a brief delay to the first trunk grant after a cold
  boot - it doesn't cause a false deny.
- **For keeping trunk** (`trunk → onboarding`): the lag causes false reverts.
  Right after every mode switch the AP's management-plane connection briefly
  drops while it picks up a new IP on the new VLAN. If `connected_macs` were
  checked here too, the port would bounce back to onboarding before the AP
  finishes reconnecting. The grace window absorbs this: within `trunk_grace_period`
  seconds the connected check is skipped; after it, the check is re-applied as
  a late-stage MAC-spoofing defence.

### Port flap on mode change

Changing a live port's PVID/VLAN membership does not produce a link-down
event on RouterOS, so a connected AP can keep using its old (now invalid) IP
on the wrong VLAN until its lease/heartbeat timeout expires. To avoid this,
`MikroTikClient.flap_port` briefly disables and re-enables the port's
`/interface` entry after every mode switch (disable, wait ~2 s, re-enable), so
the AP's NIC sees a link-down/link-up event and renews its IP immediately on
the new VLAN.

This flap can make the link and bridge host table briefly stale for the port
that was just changed - to avoid bouncing the same port back and forth every
poll cycle while that settles, the reconciliation loop skips any port it
changed last cycle before reconsidering it.

## Netwatch dead-man's-switch

Each switch runs a `/tool/netwatch` entry that pings
`netwatch.watchdog_host` (the host running this watchdog) every
`netwatch.interval`, waiting up to `netwatch.probe_timeout` for a reply
(RouterOS requires `interval >= probe_timeout`). The down-script fires on the
very first missed reply, so detection takes roughly one `interval`. The
down-script runs `ap-switch-watchdog-failsafe-revert` - a generated RouterOS
script that reverts **every** `ap_ports` entry on that switch back to the
onboarding VLAN with dot1x active, independent of the Python process. This is
RouterOS 7's substitute for a per-interface link-down script (not reliably
available on VLAN-filtering bridge/switch-chip ports): instead of reacting to
individual link state, the switch fails closed if it can no longer reach the
watchdog at all.

When the watchdog comes back, the next poll cycle (within `poll_interval`
seconds) re-establishes trunk mode for any AP that's actually present.

## Testing

```sh
pip install -e ".[dev]"
pytest
```

All RouterOS interaction is exercised against a small fake API
(`tests/conftest.py`) that mimics `routeros_api`'s `get_resource().get()` /
`.add()` / `.set()` / `.remove()` and its underscore-to-hyphen /
`id` <-> `.id` argument conventions, so the tests cover the exact call shapes
`mikrotik_client` makes.

## Deployment on FreeBSD

See [`freebsd/README.md`](freebsd/README.md) for a local "port"-style
`make install` that sets up a dedicated virtualenv, config skeleton, and an
`rc.d` service.

## Assumptions to verify against your hardware

This was built and tested against a mocked RouterOS API (no access to the
real sw01). Before relying on it in production, verify on RouterOS 7.x:

- **An AP's MAC becomes visible in `/interface/bridge/host` while its port is
  still in the onboarding VLAN** (dot1x active). This is the core assumption
  the reconciliation loop relies on to ever decide a port should become a
  trunk in the first place - if an AP's MAC never appears in the bridge host
  table before its port is trunked, the port will never leave onboarding.
- **`/interface/bridge/host` field names** - `mac-address`, `on-interface`,
  `bridge`, `vid` (VLAN ID, present when `vlan-filtering=yes` on the bridge),
  `local` (`"true"` for the bridge's own MAC, `"false"` for externally learned
  MACs). Run `/interface/bridge/host print` via the API and confirm these
  fields are present and match the expected values.
- **`/interface/dot1x/server`** - the resource path for bridge-port 802.1X
  authenticator entries (confirmed against MikroTik's official docs:
  `/interface dot1x server`, *not* `/interface/dot1x-server`). If a future
  RouterOS release moves it again, update `MikroTikClient.DOT1X_SERVER_PATH`
  in `ap_switch_watchdog/mikrotik_client.py` and the matching line in
  `ap_switch_watchdog/routeros_script.py`.
- **`/interface` `running` field** - used by `flap_port` (to find the
  interface by `name`) and `get_port_link_status` (the per-poll link check).
  Run `/interface print` via the API for one of your `ap_ports` and confirm
  it returns a `running` property (`"true"`/`"false"`) reflecting physical
  link state.
- **`/interface/ethernet/poe` field names** - `interface`, `poe-out-status`
  (values include `"powered-on"`, `"waiting-for-load"`, `"off"`). If a port
  has no PoE entry the check is silently skipped. Run
  `/interface/ethernet/poe print` to inspect what your switch reports.
- **`(R/M)STP` on the bridge** - MikroTik docs note dot1x-protected ports
  need the bridge running (R/M)STP for EAPOL to be handled correctly.
- **Failsafe RSC script syntax** (`ap_switch_watchdog/routeros_script.py`) -
  the `:toarray`/`:foreach`/`do={}` function syntax is standard RouterOS
  scripting, but run `--print-failsafe-script` and test it manually
  (`/system script run ap-switch-watchdog-failsafe-revert`) before relying on
  the Netwatch trigger.
