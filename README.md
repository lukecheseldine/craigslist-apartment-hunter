# Craigslist Apartment Watcher (Selenium + Telegram)

Scrapes a Craigslist apartments search and notifies you on Telegram:

- New listings (deduped by post ID)
- Errors (so you know when it breaks)
- Hourly heartbeat: “still working, nothing new”

Designed to be **cron-friendly**: it runs **once** and exits.

## Requirements

- macOS (or Linux)
- Python 3
- Firefox installed
- Selenium can run Firefox (geckodriver is usually handled automatically by Selenium Manager; if not, install it)

## Setup

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install selenium requests python-dotenv
```

Create a `.env` file:

```env
TELEGRAM_BOT_TOKEN=123456:abc...
TELEGRAM_CHAT_ID=123456789
HEARTBEAT_SECONDS=3600
ERROR_NOTIFY_COOLDOWN_SECONDS=900
HEADLESS=1
```

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

