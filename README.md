<img width="1571" height="1564" alt="image" src="https://github.com/user-attachments/assets/8c1d0f82-b291-469a-b305-18147ef81a46" />


# Backloggery Watchdog

Backloggery Watchdog keeps your RetroAchievements activity and selected Steam data in sync with your Backloggery collection. It runs in Docker and saves its mappings, Steam review choices, and sync history in a local SQLite database.

## What it does

### RetroAchievements

The connector watches the newest game in your RetroAchievements Recently Played list. It uses the RA Game ID to keep that game linked to the same Backloggery entry on later syncs.

It syncs achievement progress, game status, platform, and region. It copies Rich Presence into Notes when the message belongs to that same game. RA's win condition determines whether a game is Beaten; RA mastery determines whether it is Completed. It won't infer either status from achievement names or Rich Presence text.

When RA reports that you're online, the connector watches that game more often. For a mapped game, Priority stays `Now Playing` while RA reports Online and through the offline confirmation window. It waits for several stable offline checks before ending the watch. Priority aging applies only to mapped games it has seen online. It leaves Priority alone for games it has never watched and doesn't lower a game's status just because you've been away.

The default aging interval is 14 days (`WATCHDOG_PRIORITY_DECAY_DAYS`). For RA, the timer starts when the offline watch is confirmed. If the tracked game's Last Played value, Rich Presence, or achievement progress changes, the timer starts over. After 14 days, Priority becomes `Paused`; after 28 days, `High`; after 42 days, `Low`. A mastered game returns to `Normal` after 28 days.

The connector saves RA Game ID to Backloggery entry ID mappings in its database. You can inspect or change them with the `mapping` commands below. It sets new RA entries to Ownership `Own` and Format `Physical`, and preserves Backloggery fields it doesn't manage.

### Steam

Steam support starts when both `STEAM_WEB_API` and `STEAM_ID` are set. Steam must be able to show the account's Game Details for the library and recent playtime requests to work.

Every 15 minutes by default, the connector reads the full owned-games list and updates local Steam candidates. The candidate list's default display limit of 20 doesn't limit the library scan.

For Steam games linked to PC entries, it refreshes playtime, achievement counts, Notes, and status. Notes show recent and lifetime playtime in hours and minutes, followed by up to five recently unlocked achievements. A game with lifetime playtime is Unfinished. A game with no playtime is Unplayed unless it's being added while Steam reports it as active, in which case it starts as Unfinished. A game with every available achievement is Completed. Steam doesn't provide achievement data for every game.

Steam entries use Format `Digital`. Games in your owned-games list use Ownership `Own`. If you're playing an official Steam game that isn't in that list, the connector can recognize it through the public Steam catalog and mark it `Household`, which covers games available through Steam Families. It ignores non-Steam shortcuts.

The connector checks Steam presence every minute by default. When Steam reports an exact active AppID and that game is linked to a Backloggery PC entry, it sets Priority to `Now Playing`. Two missed checks end the active signal and start the decay timer. With the default 14-day interval, Priority becomes `Paused` after 14 inactive days, `High` after 28 days, and `Low` after 42 days. A mastered game returns to `Normal` after 28 days. If Steam reports the game active again, the timer resets. While a game is inactive, the connector leaves priorities outside this sequence alone.

You can turn on automatic submission for an active official Steam game with `WATCHDOG_STEAM_AUTO_SUBMIT_ACTIVE=true`. This also covers an eligible Steam Families game. It won't auto-submit candidates you've discarded, ignored, skipped, or marked as already tracked. Automatic active-game submission is off by default.

You can also import the library, review candidates yourself, then submit the ones you accept. Your decisions are saved across scans and container restarts. Discarded and ignored candidates stay excluded from auto-submission. In the interactive review, choosing skip leaves a candidate unreviewed for next time. If you use the direct `steam review APP_ID skip` command, the candidate gets a saved `skipped` status instead.

The connector spaces out Backloggery requests, follows server retry instructions, and checks entries after writes to confirm the changes.

## Before you start

You'll need:

- Docker Engine and Docker Compose (`docker compose`). Docker's [Engine installation guide](https://docs.docker.com/engine/install/) and [Compose installation guide](https://docs.docker.com/compose/install/) cover setup.
- A Backloggery account, plus its `PHPSESSID` and `log_token` session cookies.
- A RetroAchievements account and Web API key.
- A Steam account and Web API key if you want Steam support.
- Outbound network access to Backloggery and RetroAchievements. Steam needs access too when enabled.

Docker builds the app image, so you don't need to install Python on the host. The image uses Python 3.13.

## Get your credentials

Keep `.env` private. It contains account credentials and Backloggery session cookies. Don't commit it or share its contents in screenshots, issues, or chat.

### RetroAchievements

Sign in to [RetroAchievements](https://retroachievements.org/), open your account control panel, and copy the Web API key from the **Keys** section into `RA_API_KEY`. The [official API guide](https://api-docs.retroachievements.org/getting-started.html) has the same steps.

Set `RA_USERNAME` to your RetroAchievements username.

### Backloggery

Sign in to Backloggery in your browser. Open developer tools and find the cookies for `backloggery.com`, usually under **Application** or **Storage** → **Cookies**. Copy `PHPSESSID` and `log_token` into the matching `.env` fields, then set `BACKLOGGERY_USERNAME` to your account name.

These cookies act like your logged-in session. Anyone who gets them may be able to access your account. They can expire, so replace them if authentication stops working. Browser developer tools label these panels differently.

### Steam

Sign in and open Valve's [Steam Web API Key page](https://steamcommunity.com/dev/apikey). Register a domain as Steam requests, accept the API terms, and copy the key into `STEAM_WEB_API`.

Set `STEAM_ID` to your 17-digit SteamID64 or Steam profile URL. Vanity URLs such as `https://steamcommunity.com/id/example/` are supported. In Steam's privacy settings, make **Game Details** visible so the Web API can read your library and recent games. See Valve's [Steam Web API documentation](https://steamcommunity.com/dev).

## Run it with Docker Compose

Clone the GitHub repository and enter the checkout:

```sh
git clone https://github.com/solitonmedic/Backloggery-Watchdog.git
cd Backloggery-Watchdog
```

Make your configuration file and edit it with your credentials:

```sh
cp .env.example .env
```

The required settings are `RA_API_KEY`, `RA_USERNAME`, `PHPSESSID`, `log_token`, and `BACKLOGGERY_USERNAME`. Add `STEAM_WEB_API` and `STEAM_ID` if you want Steam support. Leave `WATCHDOG_DRY_RUN=true` while you check that everything is configured correctly.

Create a directory for the database. The container runs as UID/GID `1001:1001`. On Linux, give that user access to the directory if Docker reports a permission error:

```sh
mkdir -p data
chmod 700 data
sudo chown 1001:1001 data
```

Protect the credentials file, then build and start the service:

```sh
chmod 600 .env
docker compose up -d --build
docker compose ps
docker compose logs -f watchdog
```

Check the service and account connections with:

```sh
docker compose exec watchdog backloggery-watchdog healthcheck
docker compose exec watchdog backloggery-watchdog auth-check
```

`auth-check` checks RetroAchievements and Backloggery. To check Steam access, run `steam import`; it reads the library into the local database without submitting anything to Backloggery.

Stop the container and keep your database with:

```sh
docker compose down
```

Your database stays in the host's `data/` directory. Keep that directory to retain your mappings and review decisions.

## Settings

| Setting | Default | What it controls |
| --- | --- | --- |
| `RA_API_KEY` | Required | RetroAchievements Web API key. |
| `RA_USERNAME` | Required | RetroAchievements username. |
| `PHPSESSID` | Required | Backloggery session cookie. |
| `log_token` | Required | Backloggery session token cookie. |
| `BACKLOGGERY_USERNAME` | Required | Backloggery account to update. |
| `STEAM_WEB_API` | Empty | Steam Web API key. Set this and `STEAM_ID` to enable Steam. |
| `STEAM_ID` | Empty | SteamID64 or profile URL. Set this and `STEAM_WEB_API` to enable Steam. |
| `WATCHDOG_DRY_RUN` | `true` | When true, the connector reads data and reports planned changes without writing to Backloggery. |
| `WATCHDOG_STEALTH_SAVE` | `false` | Turns on Backloggery Stealth Save behavior. |
| `WATCHDOG_DATABASE_PATH` | `/data/watchdog.db` | SQLite database location. Keep it under `/data` so the Docker mount preserves it. |
| `WATCHDOG_OFFLINE_POLL_SECONDS` | `900` | How often to check RA while offline. |
| `WATCHDOG_ONLINE_POLL_SECONDS` | `60` | How often to check RA while online. |
| `WATCHDOG_OFFLINE_STABLE_POLLS` | `3` | Stable offline checks before ending an RA active watch. |
| `WATCHDOG_PRIORITY_DECAY_DAYS` | `14` | Days between Steam or RA Priority changes. |
| `WATCHDOG_STEAM_SCAN_SECONDS` | `900` | How often to scan the Steam library and linked entries. |
| `WATCHDOG_STEAM_PRESENCE_SECONDS` | `60` | How often to check Steam's active game. |
| `WATCHDOG_STEAM_AUTO_SUBMIT_ACTIVE` | `false` | Adds an eligible active Steam game automatically. Requires Steam settings and `WATCHDOG_DRY_RUN=false`. |
| `WATCHDOG_LOG_LEVEL` | `INFO` | Log verbosity. |
| `WATCHDOG_LOG_FORMAT` | `plain` | Use `plain` or `json` logs. |

## Commands

Run commands inside the running container with `docker compose exec`. The executable is `backloggery-watchdog`.

| Command | What it does |
| --- | --- |
| `run` | Runs the service continuously. Docker Compose starts this by default and restarts it if it exits. |
| `once` | Runs one service cycle, then exits. |
| `auth-check` | Checks RetroAchievements and Backloggery authentication without writing to the account. |
| `healthcheck` | Checks whether the service heartbeat is current. Docker uses this for container health. |
| `mapping list` | Shows saved RA Game ID to Backloggery entry mappings and field overrides. |
| `mapping set RA_GAME_ID BACKLOGGERY_ENTRY_ID` | Saves or changes a mapping. |
| `mapping clear-overrides RA_GAME_ID [--field title\|region]` | Clears the title and/or region override. Without `--field`, clears both. |
| `steam import [--account STEAM_ID_OR_PROFILE_URL]` | Reads the full owned library and recent games into local candidates. It refreshes details for recent games and doesn't submit them. |
| `steam candidates [--limit N] [--status STATUS]` | Lists saved candidates. The default display limit is 20. Statuses include `unreviewed`, `accepted`, `accepted_with_edits`, `discarded`, `skipped`, `ignored`, and `already_tracked`. |
| `steam show APP_ID` | Shows the saved details and review state for one Steam game. |
| `steam review` | Opens an interactive review of up to 20 unreviewed candidates. Choose accept, discard, skip, or quit. |
| `steam review APP_ID DECISION` | Saves a decision for one candidate: `accept`, `discard`, `skip`, `ignore`, `already-tracked`, or `edit`. For `edit`, optional flags include `--title`, `--platform`, `--subsystem`, and `--review-note`. |
| `steam submit [APP_ID] [--limit N]` | Submits accepted candidates. Requires `WATCHDOG_DRY_RUN=false`. Without an AppID, it submits accepted candidates up to the limit. |
| `steam format-digital [--limit N] [--dry-run]` | Changes linked Steam PC entries to Digital format. Respects the global dry-run setting; `--dry-run` always prevents writes. |
| `steam notes-hours [--limit N] [--dry-run]` | Updates older Steam playtime Notes to hours and minutes while keeping the achievement line and other Notes. Respects dry-run settings. |

See the available flags from inside the container:

```sh
docker compose exec watchdog backloggery-watchdog --help
docker compose exec watchdog backloggery-watchdog steam --help
```

### Review Steam candidates

Import the library, see the first 20 unreviewed candidates, then open the interactive review:

```sh
docker compose exec watchdog backloggery-watchdog steam import
docker compose exec watchdog backloggery-watchdog steam candidates --limit 20 --status unreviewed
docker compose exec -it watchdog backloggery-watchdog steam review
```

The `-it` flags give the review command a terminal for its prompts. In Portainer, open the container console and run the command there.

You can also make a decision for one AppID directly:

```sh
docker compose exec watchdog backloggery-watchdog steam review 123456 accept
docker compose exec watchdog backloggery-watchdog steam review 123456 discard
```

With dry-run on, accepting a candidate saves your choice locally and doesn't submit it. With dry-run off, accepting through `steam review` submits it right away. You can also submit candidates you accepted earlier:

```sh
docker compose exec watchdog backloggery-watchdog steam submit
```

When you change `.env`, run `docker compose up -d` so the container picks up the new settings. Use `--build` when you've changed the application or Docker image.

## If something goes wrong

- If the app reports missing settings, check the names and values in `.env`.
- If Backloggery authentication stops working, get fresh session cookies from your browser and replace both values.
- If Steam returns no owned games, check the profile ID, API key, and Game Details privacy setting.
- If review says it needs a terminal, run it with `docker compose exec -it ...` or use your container console.
- If Docker can't write the database, make sure `data/` is writable by UID/GID `1001:1001`.
- If a change doesn't show up in Backloggery, check that `WATCHDOG_DRY_RUN=false` and look at the container logs. Accepting a candidate while dry-run is on doesn't submit it.

## Development

The app source is in `src/backloggery_watchdog/`. It requires Python 3.13 or newer; dependencies are listed in `pyproject.toml` and the requirements files. Docker Compose is the usual way to run it.
