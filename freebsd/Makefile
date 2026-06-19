# Local "port" for ap-switch-watchdog.
#
# Not part of the FreeBSD Ports Collection - just installs this package into
# its own virtualenv under PREFIX, plus an rc.d script and a config skeleton,
# so it can be deployed with `make install` like any other locally-built
# piece of software.
#
# Usage (as root, from this directory):
#   make install      # create venv, install package + deps, rc.d script, config
#   make upgrade      # re-install into the existing venv (e.g. after git pull)
#   make deinstall    # remove venv + rc.d script (config/env are kept)

PREFIX?=	/usr/local
PYTHON?=	python3
SRCDIR?=	..

LIBEXECDIR=	$(PREFIX)/libexec/ap-switch-watchdog
VENV=		$(LIBEXECDIR)/venv
ETCDIR=		$(PREFIX)/etc/ap-switch-watchdog
RCDIR=		$(PREFIX)/etc/rc.d

.PHONY: all install upgrade deinstall venv

all:

venv:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip

install: venv
	$(VENV)/bin/pip install $(SRCDIR)
	mkdir -p $(ETCDIR)
	if [ ! -f $(ETCDIR)/config.yaml ]; then \
		if [ -f $(SRCDIR)/config/config.yaml ]; then \
			cp $(SRCDIR)/config/config.yaml $(ETCDIR)/config.yaml; \
		else \
			cp $(SRCDIR)/config/config.example.yaml $(ETCDIR)/config.yaml; \
		fi; \
	fi
	if [ ! -f $(ETCDIR)/env ]; then \
		printf '%s\n' \
			'# Sourced by the rc.d script before starting the watchdog.' \
			'# WATCHDOG_UNIFI_PASSWORD=...' \
			'# WATCHDOG_MIKROTIK_PASSWORD=...' \
			> $(ETCDIR)/env; \
		chmod 600 $(ETCDIR)/env; \
	fi
	sed 's,%%PREFIX%%,$(PREFIX),g' files/ap_switch_watchdog.in > $(RCDIR)/ap_switch_watchdog
	chmod 555 $(RCDIR)/ap_switch_watchdog
	@echo ""
	@echo "Installed into $(VENV)."
	@echo "Next steps:"
	@echo "  1. Edit $(ETCDIR)/config.yaml"
	@echo "  2. Set passwords in $(ETCDIR)/env (chmod 600, root-owned)"
	@echo "  3. sysrc ap_switch_watchdog_enable=YES"
	@echo "  4. service ap_switch_watchdog start"

upgrade: venv
	$(VENV)/bin/pip install --upgrade $(SRCDIR)

deinstall:
	rm -rf $(VENV)
	rm -f $(RCDIR)/ap_switch_watchdog
	@echo "$(ETCDIR) (config.yaml, env) was left in place."
