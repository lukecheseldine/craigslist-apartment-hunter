# Craigslist Apartment Watcher (Selenium + Telegram)

Scrapes a Craigslist apartments search and notifies you on Telegram:

- New listings (deduped by post ID)
- Errors (so you know when it breaks)
- Hourly heartbeat: “still working, nothing new”

Designed to be **cron-friendly**: it runs **once** and exits.

## Requirements

- macOS or Linux
- Python 3.10+
- **Firefox** (local) or **Chrome/Chromium** (optional; set `BROWSER=chrome`)
- Geckodriver / ChromeDriver as needed (or Selenium Manager on desktop)

**Headless VPS:** Prefer **1–2 GB RAM** for a single Selenium run. On **512 MB** hosts, add **swap** (e.g. 1–2 GB) or the browser may be OOM-killed. Use Mozilla’s `.deb` Firefox on Ubuntu instead of Snap-only automation when possible.

## Setup

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install selenium requests python-dotenv
```

Create a `.env` file (copy from your machine; never commit real tokens):

```env
TELEGRAM_BOT_TOKEN=123456:abc...
TELEGRAM_CHAT_ID=123456789
HEARTBEAT_SECONDS=3600
ERROR_NOTIFY_COOLDOWN_SECONDS=900
HEADLESS=1

# Optional — defaults work on macOS; on Linux VPS set explicitly:
# BROWSER=firefox
# FIREFOX_BINARY=/usr/bin/firefox
# GECKODRIVER_PATH=/usr/local/bin/geckodriver

# Optional — Chrome instead of Firefox:
# BROWSER=chrome
# CHROME_BINARY=/usr/bin/google-chrome
# CHROMEDRIVER_PATH=/usr/bin/chromedriver

# Optional — Selenium Grid / docker-selenium (remote):
# REMOTE_WEBDRIVER_URL=http://127.0.0.1:4444/wd/hub
```

### Environment variables

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram bot API |
| `HEARTBEAT_SECONDS` | Seconds between “still working” messages when nothing new (default 3600) |
| `HEADLESS` | `1` = headless browser (default) |
| `BROWSER` | `firefox` (default) or `chrome` |
| `FIREFOX_BINARY` | Path to Firefox binary (recommended on servers) |
| `GECKODRIVER_PATH` | Path to geckodriver (recommended on servers) |
| `CHROME_BINARY`, `CHROMEDRIVER_PATH` | Chrome/Chromium mode |
| `REMOTE_WEBDRIVER_URL` | If set, uses remote WebDriver instead of local browser |
| `PAGE_LOAD_TIMEOUT`, `RESULT_WAIT_SECONDS` | Tuning timeouts |

The script sets `dom.ipc.processCount` to **1** to reduce RAM on small VMs.

## Run once (manual test)

```bash
source .venv/bin/activate
python craigslist_watch.py
```

### First run behavior (bootstrap)

On the very first run, the script:

- Loads the current visible results
- Stores their post IDs in `state/seen_posts.json`
- Sends a Telegram message like “initialized… alerts start now”

After that, it only notifies on new IDs.

## Cron (run every minute)

Edit your crontab:

```bash
crontab -e
```

Add:

```cron
* * * * * cd /Users/lukecheseldine/Desktop/projects/craigslist && /Users/lukecheseldine/Desktop/projects/craigslist/.venv/bin/python /Users/lukecheseldine/Desktop/projects/craigslist/craigslist_watch.py >> /Users/lukecheseldine/Desktop/projects/craigslist/cron.log 2>&1
```

View logs:

```bash
tail -n 200 /Users/lukecheseldine/Desktop/projects/craigslist/cron.log
```

## What’s in the Telegram “new listings” message?

Each listing includes a **direct link** to the posting.

The script pulls the listing URL from the search result card and includes it under each title.

## State files

Created automatically in the `state/` folder:

- `seen_posts.json`: list of seen post IDs (dedupe)
- `last_heartbeat_epoch.txt`: last heartbeat timestamp
- `last_error_hash.txt`: used to avoid spamming the same error every minute

## Post ID → URL (what’s the URL for a post?)

In practice: **you can’t reliably reconstruct the exact posting URL from only the numeric post ID**,
because the path includes category and a title “slug”.

Best options:

- **Use the stored URL** (the script already captures it and sends it in the alert).
- **Search Craigslist for the ID**:
  - Open a search like: `https://sfbay.craigslist.org/search/apa?query=POST_ID`
  - This usually surfaces the posting if it’s still live.

## Customizing the search

Edit `SEARCHES` in `craigslist_watch.py` to add more URLs.
The script will check every configured search each run.

## Robustness

If the page looks blocked (captcha-like text), has zero result cards, or cards parse but no links are found, the run **raises an error** and Telegram gets an alert (with duplicate suppression). That avoids silently treating a broken page as “no new listings.”

## Linux VPS (DigitalOcean, etc.)

1. Clone the repo, create venv, `pip install selenium requests python-dotenv`.
2. Install **Firefox** from [Mozilla’s apt repo](https://support.mozilla.org/en-US/kb/install-firefox-linux#w_install-from-your-distribution-package-manager) (not only the Snap stub) and a matching **geckodriver** (e.g. under `/usr/local/bin`).
3. Set `FIREFOX_BINARY`, `GECKODRIVER_PATH`, and `BROWSER=firefox` in `.env`.
4. Add swap if RAM is tight: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`.
5. Cron (paths are examples):

```cron
* * * * * cd /root/craigslist-apartment-hunter && /root/craigslist-apartment-hunter/.venv/bin/python /root/craigslist-apartment-hunter/craigslist_watch.py >> /root/craigslist-apartment-hunter/cron.log 2>&1
```

## Changelog (recent)

- **2026-04** — Firefox low-memory preference (`dom.ipc.processCount`); optional Chrome and `REMOTE_WEBDRIVER_URL`; explicit `FIREFOX_BINARY` / `GECKODRIVER_PATH`; block/captcha and malformed HTML treated as errors; Pacific timestamps in Telegram.

