# Craigslist apartment watcher

Runs a Craigslist search with **Selenium (Firefox)**, remembers seen listing IDs, and sends **Telegram** notifications for new matches. Errors and hourly “still working” heartbeats go to Telegram too.

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
| `HEARTBEAT_SECONDS` | no | `3600` | Seconds between “still working, nothing new” when no new listings |
| `HEADLESS` | no | `1` | `0` to show browser window (local debug) |
| `MIN_PRICE_URL` | no | `2501` | Appended as `min_price=` on each search URL |
| `FIREFOX_BINARY` | no | auto | Firefox path (often set on Linux servers) |
| `GECKODRIVER_PATH` | no | auto | Geckodriver path (often set on Linux servers) |
| `PAGE_LOAD_TIMEOUT` | no | `30` | Page load timeout (seconds) |
| `RESULT_WAIT_SECONDS` | no | `20` | Max wait for result list to appear |
| `MAX_MESSAGE_LISTINGS` | no | `12` | Cap listings per Telegram message |

\*If missing, messages print to stdout instead of Telegram.

Example:

```env
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
HEARTBEAT_SECONDS=3600
HEADLESS=1
```

## Run

```bash
source .venv/bin/activate
python craigslist_watch.py
```

**First run:** seeds current results into `state/seen_posts.json` and sends one Telegram “initialized” message. After that, only **new** post IDs trigger alerts.

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
| `last_heartbeat_epoch.txt` | Last heartbeat time (Unix epoch) |
| `last_error_hash.txt` | Dedupes repeated error Telegrams |

Heartbeats reset when you get **new listings** or the **bootstrap** message so you don’t get a heartbeat immediately after an alert.

## Linux server notes

- Prefer **Mozilla’s Firefox `.deb`** + a pinned **geckodriver**, not Snap-only automation; set `FIREFOX_BINARY` and `GECKODRIVER_PATH` if needed.
- **~1–2 GB RAM** is comfortable; add **swap** on very small hosts.
- Timestamps in Telegram are **Pacific** (`America/Los_Angeles`).

## Post ID → URL

You can’t build the full listing URL from the numeric ID alone. The script already sends the real link from each result. To look up by ID manually:  
`https://sfbay.craigslist.org/search/apa?query=POST_ID`
