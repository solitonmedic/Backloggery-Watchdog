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
from .steam import (
    SteamClient,
    achievement_counts,
    format_steam_notes,
    latest_unlocked_achievements,
)
from .steam_submit import reconcile_steam_digital_format, submit_accepted_steam_candidates
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
    clear_overrides = mapping_commands.add_parser("clear-overrides")
    clear_overrides.add_argument("ra_game_id", type=int)
    clear_overrides.add_argument("--field", choices=("title", "region"))
    steam = commands.add_parser("steam")
    steam_commands = steam.add_subparsers(dest="steam_command", required=True)
    steam_import = steam_commands.add_parser("import")
    steam_import.add_argument("--account", default=os.environ.get("STEAM_ID"))
    steam_list = steam_commands.add_parser("candidates")
    steam_list.add_argument("--limit", type=int, default=20)
    steam_list.add_argument(
        "--status",
        choices=(
            "unreviewed",
            "accepted",
            "accepted_with_edits",
            "discarded",
            "skipped",
            "ignored",
            "already_tracked",
        ),
    )
    steam_show = steam_commands.add_parser("show")
    steam_show.add_argument("app_id", type=int)
    steam_review = steam_commands.add_parser("review")
    steam_review.add_argument("app_id", type=int, nargs="?")
    steam_review.add_argument(
        "decision",
        nargs="?",
        choices=("accept", "discard", "skip", "ignore", "already-tracked", "edit"),
    )
    steam_review.add_argument("--interactive", action="store_true")
    steam_review.add_argument("--limit", type=int, default=20)
    steam_review.add_argument("--title")
    steam_review.add_argument("--platform")
    steam_review.add_argument("--subsystem")
    steam_review.add_argument("--review-note", dest="review_notes")
    steam_submit = steam_commands.add_parser("submit")
    steam_submit.add_argument("app_id", type=int, nargs="?")
    steam_submit.add_argument("--limit", type=int, default=1000)
    steam_format = steam_commands.add_parser("format-digital")
    steam_format.add_argument("--limit", type=int, default=20)
    steam_format.add_argument("--dry-run", action="store_true")
    return parser


def _steam_command(args: argparse.Namespace) -> int:
    configure_logging(os.environ.get("WATCHDOG_LOG_LEVEL", "INFO").upper(), "plain")
    database_path = os.environ.get("WATCHDOG_DATABASE_PATH", "/data/watchdog.db")
    store = StateStore(database_path)
    try:
        if args.steam_command == "candidates":
            if args.limit < 1:
                raise WatchdogError("candidate limit must be greater than zero")
            print(json.dumps(store.list_steam_candidates(args.limit, args.status), ensure_ascii=False))
            return 0
        if args.steam_command == "show":
            candidate = store.get_steam_candidate(args.app_id)
            if candidate is None:
                raise WatchdogError(f"unknown Steam AppID {args.app_id}")
            print(json.dumps(candidate, ensure_ascii=False, indent=2))
            return 0
        if args.steam_command == "submit":
            if args.limit < 1:
                raise WatchdogError("submission limit must be greater than zero")
            config = Config.from_env()
            if config.dry_run:
                raise WatchdogError("Steam submission requires WATCHDOG_DRY_RUN=false")
            backloggery = BackloggeryClient(
                config.php_session_id,
                config.log_token,
                allow_writes=True,
                stealth_save=config.stealth_save,
                shared_rate_limit_path=f"{config.database_path}.backloggery.lock",
            )
            api_key = os.environ.get("STEAM_WEB_API", "").strip()
            account = os.environ.get("STEAM_ID", "").strip()
            if not api_key or not account:
                backloggery.close()
                raise WatchdogError("Steam submission requires STEAM_WEB_API and STEAM_ID")
            steam = SteamClient(api_key, account)
            try:
                results = submit_accepted_steam_candidates(
                    store,
                    backloggery,
                    steam,
                    config.backloggery_username,
                    app_id=args.app_id,
                    limit=args.limit,
                )
                print(json.dumps({"submitted": len(results), "results": results}, ensure_ascii=False))
                return 0
            finally:
                steam.close()
                backloggery.close()
        if args.steam_command == "format-digital":
            if args.limit < 1:
                raise WatchdogError("format-digital limit must be greater than zero")
            config = Config.from_env()
            dry_run = config.dry_run or args.dry_run
            backloggery = BackloggeryClient(
                config.php_session_id,
                config.log_token,
                allow_writes=not dry_run,
                stealth_save=config.stealth_save,
                shared_rate_limit_path=f"{config.database_path}.backloggery.lock",
            )
            try:
                result = reconcile_steam_digital_format(
                    store,
                    backloggery,
                    config.backloggery_username,
                    limit=args.limit,
                    dry_run=dry_run,
                )
                print(json.dumps(result, ensure_ascii=False))
                return 0
            finally:
                backloggery.close()
        if args.steam_command == "review":
            if args.interactive or (args.app_id is None and args.decision is None):
                if args.app_id is not None or args.decision is not None:
                    raise WatchdogError("interactive review cannot be combined with an AppID or decision")
                if args.limit < 1:
                    raise WatchdogError("review limit must be greater than zero")
                if not args.interactive and not sys.stdin.isatty():
                    raise WatchdogError(
                        "interactive review needs a terminal; pass --interactive to explicitly allow piped input"
                    )
                return _steam_review_queue(store, args.limit)
            if args.app_id is None or args.decision is None:
                raise WatchdogError("provide both an AppID and decision, or run steam review interactively")
            statuses = {
                "accept": "accepted",
                "discard": "discarded",
                "skip": "skipped",
                "ignore": "ignored",
                "already-tracked": "already_tracked",
                "edit": "accepted_with_edits",
            }
            try:
                store.review_steam_candidate(
                    args.app_id,
                    statuses[args.decision],
                    canonical_title=args.title,
                    platform=args.platform,
                    subsystem=args.subsystem,
                    review_notes=args.review_notes,
                )
            except ValueError as exc:
                raise WatchdogError(str(exc)) from exc
            if args.decision in {"accept", "edit"} and not _dry_run_enabled():
                _submit_approved_candidate(store, args.app_id)
            print(json.dumps(store.get_steam_candidate(args.app_id), ensure_ascii=False, indent=2))
            return 0
        api_key = os.environ.get("STEAM_WEB_API", "").strip()
        if not api_key:
            raise WatchdogError("missing required setting: STEAM_WEB_API")
        if not args.account:
            raise WatchdogError("provide --account with a SteamID64 or profile URL, or set STEAM_ID")
        steam = SteamClient(api_key, args.account)
        try:
            games = steam.owned_games()
            recent = steam.recently_played()
            counts = store.save_steam_candidates(games, recent)
            owned_by_id = {int(game["appid"]): game for game in games}
            enriched = 0
            # Achievement detail is useful for recent review cards; avoid a request per owned game.
            for recent_game in recent:
                app_id = int(recent_game["appid"])
                owned_game = owned_by_id.get(app_id, {})
                recent_minutes = int(recent_game.get("playtime_2weeks", 0) or 0)
                lifetime_minutes = int(
                    recent_game.get("playtime_forever", owned_game.get("playtime_forever", 0)) or 0
                )
                try:
                    achievement_data = steam.player_achievements(app_id)
                    achievements = latest_unlocked_achievements(achievement_data)
                    earned, total = achievement_counts(achievement_data)
                    achievement_status = "available"
                except WatchdogError:
                    achievements = []
                    earned = total = None
                    achievement_status = "unavailable"
                notes = format_steam_notes(
                    recent_minutes,
                    lifetime_minutes,
                    achievements if achievement_status == "available" else None,
                )
                store.set_steam_candidate_details(
                    app_id, notes, achievements, achievement_status, earned, total
                )
                enriched += 1
            summary = {
                "steam_id": steam.steam_id,
                "owned_games_seen": counts["seen"],
                "new_candidates": counts["new"],
                "changed_candidates": counts["changed"],
                "recently_played_count": len(recent),
                "recent_details_refreshed": enriched,
                "top_recent": store.list_steam_candidates(min(10, max(1, len(games)))),
            }
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        finally:
            steam.close()
    finally:
        store.close()


def _dry_run_enabled() -> bool:
    return os.environ.get("WATCHDOG_DRY_RUN", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _submit_approved_candidate(store: StateStore, app_id: int) -> dict[str, object]:
    config = Config.from_env()
    if config.dry_run:
        raise WatchdogError("Steam submission requires WATCHDOG_DRY_RUN=false")
    api_key = os.environ.get("STEAM_WEB_API", "").strip()
    account = os.environ.get("STEAM_ID", "").strip()
    if not api_key or not account:
        raise WatchdogError("Steam submission requires STEAM_WEB_API and STEAM_ID")
    backloggery = BackloggeryClient(
        config.php_session_id,
        config.log_token,
        allow_writes=True,
        stealth_save=config.stealth_save,
        shared_rate_limit_path=f"{config.database_path}.backloggery.lock",
    )
    steam = SteamClient(api_key, account)
    try:
        results = submit_accepted_steam_candidates(
            store,
            backloggery,
            steam,
            config.backloggery_username,
            app_id=app_id,
        )
        return results[0]
    finally:
        steam.close()
        backloggery.close()


def _steam_review_queue(store: StateStore, limit: int) -> int:
    candidates = store.list_steam_candidates(limit=limit, status="unreviewed")
    if not candidates:
        print("No unreviewed Steam candidates.")
        return 0

    accepted = discarded = skipped = 0
    print(f"Reviewing up to {len(candidates)} unreviewed Steam candidates.")
    for position, candidate in enumerate(candidates, start=1):
        app_id = int(candidate["steam_appid"])
        title = str(candidate.get("canonical_title") or candidate["steam_name"])
        print(f"\n[{position}/{len(candidates)}] {title} (AppID {app_id})")
        print(
            f"Playtime: {candidate['playtime_recent']} minutes recently; "
            f"{candidate['playtime_forever']} minutes lifetime"
        )
        earned = candidate.get("achievements_earned")
        total = candidate.get("achievements_total")
        if earned is not None and total is not None:
            print(f"Steam achievements: {earned}/{total} earned")
        elif candidate.get("achievement_status") == "unavailable":
            print("Steam achievements: unavailable")
        notes = candidate.get("notes")
        if notes:
            print(str(notes))
        print("[a]ccept  [d]iscard  [s]kip for now  [q]uit")
        while True:
            try:
                choice = input("Choice: ").strip().lower()
            except EOFError:
                print("\nInput ended; remaining candidates are unchanged.")
                choice = "q"
            if choice in {"a", "accept"}:
                store.review_steam_candidate(app_id, "accepted")
                accepted += 1
                if not _dry_run_enabled():
                    try:
                        _submit_approved_candidate(store, app_id)
                    except WatchdogError as exc:
                        print(f"Accepted locally; Backloggery submission failed: {exc}")
                    else:
                        print("Accepted and submitted to Backloggery under PC.")
                else:
                    print("Accepted locally; dry-run mode prevented Backloggery submission.")
                break
            if choice in {"d", "discard"}:
                store.review_steam_candidate(app_id, "discarded")
                discarded += 1
                print("Discarded; this choice is saved across future Steam imports.")
                break
            if choice in {"s", "skip"}:
                skipped += 1
                print("Skipped for now; it remains unreviewed.")
                break
            if choice in {"q", "quit"}:
                print("Review stopped; unvisited candidates remain unreviewed.")
                print(
                    f"Session totals: {accepted} accepted, {discarded} discarded, "
                    f"{skipped} skipped."
                )
                return 0
            print("Choose a, d, s, or q.")

    print(
        f"Review complete. Session totals: {accepted} accepted, {discarded} discarded, "
        f"{skipped} skipped."
    )
    return 0


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
        if args.command == "steam":
            return _steam_command(args)
        config = Config.from_env()
        configure_logging(config.log_level, config.log_format)
        store = StateStore(config.database_path)
        if args.command == "healthcheck":
            return _health(store, config)
        if args.command == "mapping":
            if args.mapping_command == "set":
                store.set_mapping(args.ra_game_id, args.backloggery_entry_id)
                logger.info({"event": "mapping_saved", "ra_game_id": args.ra_game_id, "backloggery_entry_id": args.backloggery_entry_id})
            elif args.mapping_command == "clear-overrides":
                store.clear_field_overrides(args.ra_game_id, args.field)
                logger.info({"event": "mapping_overrides_cleared", "ra_game_id": args.ra_game_id, "field": args.field or "all"})
            else:
                print(json.dumps(store.list_mappings(), separators=(",", ":")))
            return 0

        ra = RetroAchievementsClient(config.ra_api_key, config.ra_username)
        backloggery = BackloggeryClient(
            config.php_session_id,
            config.log_token,
            allow_writes=not config.dry_run,
            stealth_save=config.stealth_save,
            shared_rate_limit_path=f"{config.database_path}.backloggery.lock",
        )
        steam_api_key = os.environ.get("STEAM_WEB_API", "").strip()
        steam_account = os.environ.get("STEAM_ID", "").strip()
        steam = SteamClient(steam_api_key, steam_account) if steam_api_key and steam_account else None
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
            service = WatchdogService(config, store, ra, backloggery, steam)
            if args.command == "once":
                service.cycle()
                return 0
            service.run()
        finally:
            ra.close()
            backloggery.close()
            if steam is not None:
                steam.close()
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
