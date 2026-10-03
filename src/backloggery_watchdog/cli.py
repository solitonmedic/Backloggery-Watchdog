from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

from .backloggery import BackloggeryClient
from .config import Config
from .errors import WatchdogError
from .logging import configure_logging
from .ra import RetroAchievementsClient
from .service import WatchdogService
from .state import StateStore

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backloggery-watchdog")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("once")
    commands.add_parser("auth-check")
    commands.add_parser("healthcheck")
    mapping = commands.add_parser("mapping")
    mapping_commands = mapping.add_subparsers(dest="mapping_command", required=True)
    mapping_commands.add_parser("list")
    set_mapping = mapping_commands.add_parser("set")
    set_mapping.add_argument("ra_game_id", type=int)
    set_mapping.add_argument("backloggery_entry_id", type=int)
    return parser


def _health(store: StateStore, config: Config) -> int:
    heartbeat = store.get_runtime("heartbeat_at")
    if heartbeat is None:
        logger.error({"event": "healthcheck", "status": "unhealthy", "reason": "heartbeat missing"})
        return 1
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(heartbeat)).total_seconds()
    except ValueError:
        logger.error({"event": "healthcheck", "status": "unhealthy", "reason": "heartbeat invalid"})
        return 1
    maximum_age = max(config.offline_poll_seconds * 2, 300)
    if age > maximum_age:
        logger.error({"event": "healthcheck", "status": "unhealthy", "reason": "heartbeat stale"})
        return 1
    logger.info(
        {
            "event": "healthcheck",
            "status": "healthy",
            "service_state": store.get_runtime("service_state") or "unknown",
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        config = Config.from_env()
        configure_logging(config.log_level)
        store = StateStore(config.database_path)
        if args.command == "healthcheck":
            return _health(store, config)
        if args.command == "mapping":
            if args.mapping_command == "set":
                store.set_mapping(args.ra_game_id, args.backloggery_entry_id)
                logger.info({"event": "mapping_saved", "ra_game_id": args.ra_game_id, "backloggery_entry_id": args.backloggery_entry_id})
            else:
                print(json.dumps(store.list_mappings(), separators=(",", ":")))
            return 0

        ra = RetroAchievementsClient(config.ra_api_key, config.ra_username)
        backloggery = BackloggeryClient(config.php_session_id, config.log_token)
        try:
            if args.command == "auth-check":
                profile = ra.profile()
                platforms = backloggery.platforms()
                logger.info(
                    {
                        "event": "auth_check",
                        "status": "success",
                        "ra_user": profile.get("User"),
                        "backloggery_platform_count": len(platforms),
                    }
                )
                return 0
            service = WatchdogService(config, store, ra, backloggery)
            if args.command == "once":
                service.cycle()
                return 0
            service.run()
        finally:
            ra.close()
            backloggery.close()
    except WatchdogError as exc:
        if not logging.getLogger().handlers:
            configure_logging("INFO")
        logger.error({"event": "fatal", "message": str(exc)})
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if "store" in locals():
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
