"""Command line entry point for the AP switch watchdog."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_config
from .watchdog import APSwitchWatchdog

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ap-switch-watchdog",
        description=(
            "Move MikroTik switch ports between an onboarding VLAN (dot1x active) "
            "and a management/client trunk (dot1x off) based on UniFi AP "
            "connection state."
        ),
    )
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="path to config.yaml (default: %(default)s)"
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single poll cycle and exit"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        logger.error("failed to load configuration from %s: %s", args.config, exc)
        return 1

    watchdog = APSwitchWatchdog(config)
    if args.once:
        watchdog.poll_once()
    else:
        logger.info(
            "starting watchdog, polling every %ss across %d switch(es)",
            config.poll_interval, len(config.switches),
        )
        watchdog.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
