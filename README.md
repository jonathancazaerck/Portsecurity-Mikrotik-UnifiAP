# ap-switch-watchdog

Secures MikroTik switch ports that UniFi APs are plugged into. By default a
port sits in an **onboarding VLAN** with **802.1X (dot1x) active** - it can't
pass any traffic except EAPOL. Every poll cycle, the watchdog asks the switch
which MAC addresses it currently sees on each `ap_port` (bridge host table +
link state) and checks those against the UniFi controller's list of known AP
MAC addresses. If a known AP is present and the port has link, the port
becomes a **management+client trunk** with dot1x disabled. If not (AP
unplugged, moved elsewhere, or no longer learned), the port is reverted to
onboarding.

```
        UniFi controller                    MikroTik switch
   "which MACs are known APs?"        bridge host table, link state,
      (poll every poll_interval)         PVID, dot1x (same interval)
              │                                     │
              └──────────────────┬──────────────────┘
                                  ▼
                       ┌──────────────────┐
                       │  ap-switch-       │  per ap_port: known AP's MAC
                       │  watchdog         │  present AND link up?
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
  unifi_client.py     # UniFi controller: which MAC addresses are known APs?
  mikrotik_client.py  # RouterOS API: bridge host table, link/PVID/dot1x, mode switching
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
2. Fetch the set of MAC addresses UniFi knows about as access points
   (`stat/device`, `type == "uap"`) - **regardless of their reported
   online/offline `state`**, see "Why not poll AP online/offline state?"
   below.
3. For each `ap_port`, compute:
   - **link up?** - `/interface` `running`.
   - **known AP present?** - any MAC the bridge has learned on this port
     that's also in UniFi's known-AP set.
   - **desired mode** = `trunk` if both are true, else `onboarding`.
4. Compare against the port's **actual** current mode, read live from the
   switch (PVID == management VLAN *and* dot1x disabled => `trunk`,
   otherwise `onboarding`). If actual != desired, `set_port_mode` applies the
   VLAN/dot1x change for the new mode - dot1x disabled *last* when opening a
   port, re-enabled *first* when locking it down - and flaps the port (see
   "Port flap on mode change" below).

Ports changed this cycle are skipped on the *next* poll, giving the link and
bridge host table one `poll_interval` to settle after the flap before being
reconsidered.

There is **no persisted state** - every poll fully reconciles each port from
live switch + UniFi data, so a restart picks up exactly where the switches
currently are.

### Why not poll AP online/offline state?

UniFi's per-device `state` field (online/offline) is heartbeat-based and can
lag 30-70+ seconds behind reality - especially right after a VLAN change,
when the AP briefly loses and regains its management-plane connection to the
controller while it picks up an address on the new VLAN. Earlier versions of
this watchdog used that field as the trigger for trunk/onboarding, with
`offline_debounce` and a separate link-down "fast path" layered on top to
paper over its lag and the false "offline" blips it produced right after
every mode switch - each fix narrowed the false-revert window but never
closed it.

Instead, "is a known AP physically present on this port right now" is
answered entirely from the switch's own bridge host table and link state,
which RouterOS updates essentially instantly and aren't affected by the
controller's heartbeat timing at all. UniFi is only used as a
slowly-changing **allowlist** of known AP MAC addresses - the security
question "is this device allowed a trunk", not "is it online right now" - so
its heartbeat lag no longer matters.

### Port flap on mode change

Changing a live port's PVID/VLAN membership does not produce a link-down
event on RouterOS, so a connected AP can keep using its old (now invalid) IP
on the wrong VLAN until its lease/heartbeat timeout expires. To avoid this,
`MikroTikClient.flap_port` briefly disables and re-enables the port's
`/interface` entry after every mode switch (disable, wait ~2s, re-enable), so
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
- **`/interface/dot1x/server`** - the resource path for bridge-port 802.1X
  authenticator entries (confirmed against MikroTik's official docs:
  `/interface dot1x server`, *not* `/interface/dot1x-server`). If a future
  RouterOS release moves it again, update `MikroTikClient.DOT1X_SERVER_PATH`
  in `ap_switch_watchdog/mikrotik_client.py` and the matching line in
  `ap_switch_watchdog/routeros_script.py`.
- **`/interface/bridge/host` field names** - `mac-address`, `on-interface`,
  `bridge`. Run `/interface/bridge/host print` via the API and compare.
- **`/interface` `running` field** - used by `flap_port` (to find the
  interface by `name`) and `get_port_link_status` (the per-poll link check).
  Run `/interface print` via the API for one of your `ap_ports` and confirm
  it returns a `running` property (`"true"`/`"false"`) reflecting physical
  link state.
- **`(R/M)STP` on the bridge** - MikroTik docs note dot1x-protected ports
  need the bridge running (R/M)STP for EAPOL to be handled correctly.
- **Failsafe RSC script syntax** (`ap_switch_watchdog/routeros_script.py`) -
  the `:toarray`/`:foreach`/`do={}` function syntax is standard RouterOS
  scripting, but run `--print-failsafe-script` and test it manually
  (`/system script run ap-switch-watchdog-failsafe-revert`) before relying on
  the Netwatch trigger.
