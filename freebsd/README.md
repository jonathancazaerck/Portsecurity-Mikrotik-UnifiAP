# FreeBSD deployment

A local "port"-style installer: not part of the Ports Collection (so it
doesn't depend on `/usr/ports` or pre-packaged dependencies like
`RouterOS-api`), but installable the same way - `make install`/`make
deinstall` as root, plus an `rc.d` script.

It installs `ap-switch-watchdog` and its Python dependencies into a private
virtualenv under `${PREFIX}/libexec/ap-switch-watchdog/venv` (default
`PREFIX=/usr/local`), so it doesn't touch the system's `pkg`-managed Python.

## Prerequisites

Python 3 is not installed by default on FreeBSD:

```sh
pkg install python3
```

For a specific version (e.g. 3.11): `pkg install python311` and then pass
`PYTHON=python3.11` to every `make` call below.

## Install

Copy this whole repository to the FreeBSD server (`git clone`, `rsync`, ...),
then:

```sh
cd freebsd
make install
```

This:

- creates the venv and runs `pip install ..` (the project root)
- installs `${PREFIX}/etc/ap-switch-watchdog/config.yaml` (copied from
  `../config/config.yaml` if present, else from `config.example.yaml`) -
  only if it doesn't already exist
- installs `${PREFIX}/etc/ap-switch-watchdog/env` (mode 600) for the
  `WATCHDOG_UNIFI_PASSWORD` / `WATCHDOG_MIKROTIK_PASSWORD` env vars - only if
  it doesn't already exist
- installs `${PREFIX}/etc/rc.d/ap_switch_watchdog`

## Configure and start

```sh
$EDITOR /usr/local/etc/ap-switch-watchdog/config.yaml
$EDITOR /usr/local/etc/ap-switch-watchdog/env      # set both passwords
sysrc ap_switch_watchdog_enable=YES
service ap_switch_watchdog start
service ap_switch_watchdog status
tail -f /var/log/ap_switch_watchdog.log
```

Run `service ap_switch_watchdog stop` before re-running `python
scripts/setup_switches.py` interactively, if you ever need to.

## One-time switch setup

The setup script also runs from the venv:

```sh
/usr/local/libexec/ap-switch-watchdog/venv/bin/ap-switch-watchdog-setup \
    -c /usr/local/etc/ap-switch-watchdog/config.yaml
```

(needs `WATCHDOG_UNIFI_PASSWORD`/`WATCHDOG_MIKROTIK_PASSWORD` exported in the
shell, or source `/usr/local/etc/ap-switch-watchdog/env` first.)

## Upgrade

After `git pull` on the FreeBSD box:

```sh
cd freebsd
make upgrade
service ap_switch_watchdog restart
```

## Uninstall

```sh
service ap_switch_watchdog stop
sysrc -x ap_switch_watchdog_enable
cd freebsd
make deinstall
```

`config.yaml` and `env` under `/usr/local/etc/ap-switch-watchdog/` are left
in place; remove that directory manually if you want a clean slate.
