# Craigslist apartment watcher

Runs a Craigslist search with **Selenium** (defaults to **local Firefox**), remembers seen listing IDs, and sends **Telegram** notifications for new matches. Errors and periodic “still working” heartbeats (default every 8 hours) go to Telegram too. You can use **local Chrome/Chromium** or a **remote** browser (e.g. `selenium/standalone-chrome` on the same VM) via env vars below.

Runs **once per invocation** (good for `cron` every minute).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` (never commit it):

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `TELEGRAM_BOT_TOKEN` | yes* | — | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | yes* | — | Your chat id (e.g. from `getUpdates`) |
| `HEARTBEAT_SECONDS` | no | `28800` (8 hours) | Seconds between “still working, nothing new” when no new listings |
| `HEADLESS` | no | `1` | `0` to show browser window (local debug) |
| `MIN_PRICE_URL` | no | `2501` | Appended as `min_price=` on each search URL |
| `BROWSER` | no | `firefox` | `firefox` or `chrome` (local or with `REMOTE_WEBDRIVER_URL`) |
| `REMOTE_WEBDRIVER_URL` | no | — | If set, use Grid / docker-selenium (e.g. `http://127.0.0.1:4444/wd/hub`) |
| `FIREFOX_BINARY` | no | auto | Firefox path (Linux / VM) |
| `GECKODRIVER_PATH` | no | auto | Geckodriver path (Linux / VM) |
| `CHROME_BINARY` | no | auto | Chromium/Chrome binary path |
| `CHROMEDRIVER_PATH` | no | auto | Chromedriver path |
| `PAGE_LOAD_TIMEOUT` | no | `30` | Page load timeout (seconds) |
| `RESULT_WAIT_SECONDS` | no | `20` | Max wait for result list to appear |
| `MAX_MESSAGE_LISTINGS` | no | `12` | Cap listings per Telegram message |

\*If missing, messages print to stdout instead of Telegram.

Example:

```env
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
HEARTBEAT_SECONDS=28800
HEADLESS=1
```

## Run

```bash
source .venv/bin/activate
python craigslist_watch.py
```

**First run:** seeds current results into `state/seen_posts.json` and `state/seen_titles.json`, then sends one Telegram “initialized” message. After that, alerts fire only for listings whose **post ID is new** and whose **title** (normalized whitespace) has not been seen before—so common reposts with the same title are skipped even when Craigslist assigns a new post ID.

## Deploy on the VM

Use **git on the server**, not `scp` of the project tree. Copying files from your laptop can overwrite or mix in **`state/`**, a wrong **`.env`**, or other local-only files.

1. **Push** from your machine: `git push origin main` (so `origin` has the commit you want).
2. **SSH** into the host and go to the clone (e.g. `/root/craigslist-apartment-hunter`).
3. **Update** the repo: `git pull` (if you hit conflicts from old manual copies, `git fetch origin && git reset --hard origin/main` matches GitHub exactly—only do that if you intend to drop stray edits on the server).
4. **Dependencies** (when `requirements.txt` changed): `source .venv/bin/activate && pip install -r requirements.txt`
5. **Smoke test:** `python craigslist_watch.py` — should exit `0`. Cron keeps using the same paths; no need to restart it if only code changed.

## Cron (every minute)

```cron
* * * * * cd /path/to/craigslist-apartment-hunter && .venv/bin/python craigslist_watch.py >> cron.log 2>&1
```

## Customize

- **Search URLs:** edit `SEARCHES` in `craigslist_watch.py`. Each URL is passed through `_with_min_price()` so `min_price=<MIN_PRICE_URL>` is added unless you already set `min_price` in the URL.
- **Title filters:** edit `BLOCK_KEYWORDS` (e.g. skip “room for rent”).

## State files (`state/`)

| File | Purpose |
|------|---------|
| `seen_posts.json` | Post IDs already seen |
| `seen_titles.json` | Listing titles already seen (normalized); suppresses repost spam with new IDs |
| `last_heartbeat_epoch.txt` | Last heartbeat time (Unix epoch) |
| `last_error_hash.txt` | Dedupes repeated error Telegrams |

Heartbeats reset when you get **new listings** or the **bootstrap** message so you don’t get a heartbeat immediately after an alert.

## Linux server / VM

- **Firefox (typical):** Mozilla **`.deb` Firefox** + pinned **geckodriver**; set `FIREFOX_BINARY` / `GECKODRIVER_PATH` if needed. Avoid relying on Snap-only browser paths for automation.
- **Chrome:** set `BROWSER=chrome` and `CHROME_BINARY` / `CHROMEDRIVER_PATH` if you use system Chromium.
- **Docker Selenium** on the same host: run the container, then e.g. `REMOTE_WEBDRIVER_URL=http://127.0.0.1:4444/wd/hub` and `BROWSER=chrome` (or `firefox` to match the image).
- **~1–2 GB RAM** is comfortable; add **swap** on very small hosts.
- Timestamps in Telegram are **Pacific** (`America/Los_Angeles`).

## Post ID → URL

You can’t build the full listing URL from the numeric ID alone. The script already sends the real link from each result. To look up by ID manually:  
`https://sfbay.craigslist.org/search/apa?query=POST_ID`
