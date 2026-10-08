import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from concurrent import futures
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Set

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()


# =========================
# CONFIG
# =========================

MIN_PRICE_URL = int(os.getenv("MIN_PRICE_URL", "3500"))
MAX_PRICE_URL = int(os.getenv("MAX_PRICE_URL", "6000"))


def _with_price_bounds(url: str) -> str:
    """Append min_price/max_price unless already in the query string."""
    if not re.search(r"[?&]min_price=", url):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}min_price={MIN_PRICE_URL}"
    if not re.search(r"[?&]max_price=", url):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}max_price={MAX_PRICE_URL}"
    return url


SEARCHES = {
    "sf_dog_friendly": _with_price_bounds(
        "https://sfbay.craigslist.org/search/san-francisco-ca/apa"
        "?lat=37.7739&lon=-122.434"
        "&pets_dog=1&search_distance=0.6&sort=date"
    ),
}

BLOCK_KEYWORDS: List[str] = [
    "room for rent",
    "soma life",
    "mid-market",
    "mid market",
    "corona heights",
    "tmlp",
    "access to fitness sf",
]
# Extra comma-separated keywords, e.g. BLOCK_KEYWORDS_EXTRA="mid-market,tenderloin".
# Case-insensitive substring match against title + meta + neighborhood.
BLOCK_KEYWORDS += [
    kw.strip() for kw in os.getenv("BLOCK_KEYWORDS_EXTRA", "").split(",") if kw.strip()
]

STATE_DIR = Path("state")
SEEN_FILE = STATE_DIR / "seen_posts.json"
SEEN_TITLES_FILE = STATE_DIR / "seen_titles.json"
HEARTBEAT_FILE = STATE_DIR / "last_heartbeat_epoch.txt"
LAST_ERROR_FILE = STATE_DIR / "last_error_hash.txt"

HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "28800"))
# Minimum free disk (MB) on the state dir required to launch the browser.
# Below this, the run self-heals (journal vacuum, apt clean) and otherwise
# exits 0 without crashing geckodriver — a full disk used to crash-loop
# every cron tick and spam Telegram because the error-dedupe file itself
# could not be written (ENOSPC).
MIN_DISK_FREE_MB = int(os.getenv("MIN_DISK_FREE_MB", "150"))
# Minimum seconds between repeat Telegram error alerts for the same error.
ERROR_NOTIFY_COOLDOWN_SECONDS = int(os.getenv("ERROR_NOTIFY_COOLDOWN_SECONDS", "3600"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HEADLESS = os.getenv("HEADLESS", "1") != "0"
PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", "30"))
RESULT_WAIT_SECONDS = int(os.getenv("RESULT_WAIT_SECONDS", "20"))
MAX_ATTEMPTS = max(1, int(os.getenv("MAX_ATTEMPTS", "3")))
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "10"))
# driver.quit() can hang forever on a wedged browser; bound it so a stuck
# shutdown can never hold the cron lock and silently stall later runs.
DRIVER_QUIT_TIMEOUT_SECONDS = int(os.getenv("DRIVER_QUIT_TIMEOUT_SECONDS", "15"))
JITTER_SECONDS = (1, 4)
MAX_MESSAGE_LISTINGS = int(os.getenv("MAX_MESSAGE_LISTINGS", "12"))
# Local Firefox (default) or Chrome; or point REMOTE_WEBDRIVER_URL at docker-selenium / Grid.
BROWSER = os.getenv("BROWSER", "firefox").strip().lower()
FIREFOX_BINARY = os.getenv("FIREFOX_BINARY", "").strip()
GECKODRIVER_PATH = os.getenv("GECKODRIVER_PATH", "").strip()
CHROME_BINARY = os.getenv("CHROME_BINARY", "").strip()
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "").strip()
REMOTE_WEBDRIVER_URL = os.getenv("REMOTE_WEBDRIVER_URL", "").strip()


# =========================
# MODELS
# =========================


@dataclass(frozen=True)
class Listing:
    search_name: str
    post_id: str
    title: str
    link: str
    price: str
    hood: str
    meta: str


# =========================
# STORAGE
# =========================


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def safe_write_text(path: Path, text: str) -> None:
    """Persist state; survive ENOSPC so Telegram can still alert without crashing."""
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not write {path}: {exc}", file=sys.stderr)


def load_seen() -> Set[str]:
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass
    return set()


def save_seen(seen: Set[str]) -> None:
    safe_write_text(SEEN_FILE, json.dumps(sorted(seen), indent=2))


def canonical_listing_title(title: str) -> str:
    """Normalize title for dedup: trim and collapse internal whitespace."""
    return " ".join(title.strip().split())


def load_seen_titles() -> Set[str]:
    if not SEEN_TITLES_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_TITLES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass
    return set()


def save_seen_titles(seen_titles: Set[str]) -> None:
    safe_write_text(SEEN_TITLES_FILE, json.dumps(sorted(seen_titles), indent=2))


def load_last_heartbeat_epoch() -> int:
    try:
        return int(HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def save_last_heartbeat_epoch(epoch: int) -> None:
    safe_write_text(HEARTBEAT_FILE, str(epoch))


def load_last_error() -> tuple:
    """Return (error_key, notified_epoch). File format is 'key\\nepoch';
    old files holding just the key are treated as long-expired."""
    try:
        lines = LAST_ERROR_FILE.read_text(encoding="utf-8").split("\n")
        key = lines[0].strip() if lines else ""
        epoch = int(lines[1].strip()) if len(lines) > 1 and lines[1].strip() else 0
        return key, epoch
    except Exception:
        return "", 0


def save_last_error(key: str, epoch: int) -> None:
    # Keys embed exception text; flatten newlines so the file stays parseable.
    safe_write_text(LAST_ERROR_FILE, f"{' '.join(key.split())}\n{epoch}")


def load_last_error_hash() -> str:
    return load_last_error()[0]


def save_last_error_hash(value: str) -> None:
    save_last_error(value, int(time.time()))


def disk_free_mb(path: Path) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return 0.0


def ensure_disk_space() -> bool:
    """Make sure there is room to run. A full disk makes geckodriver exit
    with status 64, which crash-loops every cron tick. Try cheap, safe
    cleanup first (journal vacuum, apt cache); return False if still low."""
    ensure_state_dir()
    if disk_free_mb(STATE_DIR) >= MIN_DISK_FREE_MB:
        return True
    for cmd in (["journalctl", "--vacuum-size=80M"], ["apt-get", "clean"]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=180)
        except Exception:
            pass
    return disk_free_mb(STATE_DIR) >= MIN_DISK_FREE_MB


# =========================
# TELEGRAM
# =========================

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def now_pacific() -> str:
    dt = datetime.now(timezone.utc).astimezone(PACIFIC_TZ)
    tz_abbrev = dt.tzname() or "PT"
    return dt.strftime(f"%a %b %d, %I:%M %p {tz_abbrev}")


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("\n[telegram disabled]")
        print(message)
        print()
        return

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    resp.raise_for_status()


def send_error_notification(error_key: str, message: str) -> None:
    error_key = " ".join(error_key.split())
    last_key, last_epoch = load_last_error()
    if error_key == last_key and (int(time.time()) - last_epoch) < ERROR_NOTIFY_COOLDOWN_SECONDS:
        return
    send_telegram(message)
    save_last_error(error_key, int(time.time()))


# =========================
# SELENIUM
# =========================


def build_driver():
    """Local Firefox (default), local Chrome, or remote Grid / docker-selenium."""
    if REMOTE_WEBDRIVER_URL:
        if BROWSER == "chrome":
            opts = ChromeOptions()
            if HEADLESS:
                opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            driver = webdriver.Remote(command_executor=REMOTE_WEBDRIVER_URL, options=opts)
        else:
            opts = FirefoxOptions()
            if HEADLESS:
                opts.add_argument("--headless")
            opts.set_preference("dom.webdriver.enabled", False)
            opts.set_preference("media.peerconnection.enabled", False)
            opts.set_preference("dom.ipc.processCount", 1)
            driver = webdriver.Remote(command_executor=REMOTE_WEBDRIVER_URL, options=opts)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        return driver

    if BROWSER == "chrome":
        opts = ChromeOptions()
        if HEADLESS:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        for candidate in (
            CHROME_BINARY,
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
        ):
            if candidate and Path(candidate).is_file():
                opts.binary_location = candidate
                break
        if CHROMEDRIVER_PATH:
            svc = ChromeService(executable_path=CHROMEDRIVER_PATH)
        else:
            svc = ChromeService()
        driver = webdriver.Chrome(service=svc, options=opts)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        return driver

    options = FirefoxOptions()
    if HEADLESS:
        options.add_argument("--headless")

    for candidate in (
        FIREFOX_BINARY,
        "/snap/firefox/current/usr/lib/firefox/firefox",
        shutil.which("firefox"),
    ):
        if candidate and Path(candidate).is_file():
            options.binary_location = candidate
            break

    options.set_preference("dom.webdriver.enabled", False)
    options.set_preference("media.peerconnection.enabled", False)
    options.set_preference("dom.ipc.processCount", 1)

    if GECKODRIVER_PATH:
        service = FirefoxService(executable_path=GECKODRIVER_PATH)
    else:
        service = FirefoxService()

    driver = webdriver.Firefox(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver


# =========================
# PARSING
# =========================


def extract_post_id(link: str) -> str:
    match = re.search(r"/(\d+)\.html", link)
    return match.group(1) if match else link.strip().lower()


def text_or_empty(parent, by: By, selector: str) -> str:
    try:
        return parent.find_element(by, selector).text.strip()
    except Exception:
        return ""


def first_link_from_card(card) -> Optional[str]:
    for by, selector in (
        (By.CLASS_NAME, "posting-title"),
        (By.CSS_SELECTOR, "a.posting-title"),
        (By.CSS_SELECTOR, "a"),
    ):
        try:
            el = card.find_element(by, selector)
            href = el.get_attribute("href")
            if href:
                return href
        except Exception:
            continue
    return None


def title_from_card(card) -> str:
    for by, selector in (
        (By.CLASS_NAME, "posting-title"),
        (By.CSS_SELECTOR, "a.posting-title"),
        (By.CSS_SELECTOR, "a"),
    ):
        try:
            text = card.find_element(by, selector).text.strip()
            if text:
                return text
        except Exception:
            continue
    return "(untitled)"


def scrape_search(driver, search_name: str, url: str) -> List[Listing]:
    driver.get(url)

    WebDriverWait(driver, RESULT_WAIT_SECONDS).until(
        EC.presence_of_element_located((By.CLASS_NAME, "cl-search-result"))
    )

    page_lower = (driver.page_source or "").lower()
    block_markers = [
        "captcha",
        "unusual traffic",
        "your request has been blocked",
        "request blocked",
        "access denied",
        "temporarily blocked",
        "pardon the interruption",
    ]
    if any(m in page_lower for m in block_markers):
        raise RuntimeError(f"{search_name}: page looks blocked/captcha-like")

    cards = driver.find_elements(By.CLASS_NAME, "cl-search-result")
    if not cards:
        title = (driver.title or "").strip()
        raise RuntimeError(f"{search_name}: expected search results, found 0 cards (title={title!r})")

    listings: List[Listing] = []

    for card in cards:
        link = first_link_from_card(card)
        if not link:
            continue

        title = title_from_card(card)
        post_id = extract_post_id(link)
        price = text_or_empty(card, By.CLASS_NAME, "price")
        hood = text_or_empty(card, By.CLASS_NAME, "nearby")
        meta = text_or_empty(card, By.CLASS_NAME, "meta")

        listings.append(
            Listing(
                search_name=search_name,
                post_id=post_id,
                title=title,
                link=link,
                price=price,
                hood=hood,
                meta=meta,
            )
        )

    if not listings:
        title = (driver.title or "").strip()
        raise RuntimeError(
            f"{search_name}: found {len(cards)} result cards but extracted 0 listings (title={title!r})"
        )

    return listings


# =========================
# FILTERING
# =========================


def normalize_text(parts: Iterable[str]) -> str:
    return " ".join(parts).lower()


def passes_filters(listing: Listing) -> bool:
    haystack = normalize_text([listing.title, listing.meta, listing.hood])
    return not any(kw.lower() in haystack for kw in BLOCK_KEYWORDS)


def format_listing_block(listing: Listing) -> str:
    details = " ".join(x for x in [listing.price, listing.hood, listing.meta] if x).strip()
    if details:
        return f"{listing.title}\n{details}\n{listing.link}"
    return f"{listing.title}\n{listing.link}"


def format_new_listing_message(new_items: List[Listing]) -> str:
    header = f"[{now_pacific()}] New Craigslist listings: {len(new_items)}"
    blocks = [format_listing_block(item) for item in new_items[:MAX_MESSAGE_LISTINGS]]
    body = "\n\n".join(blocks)
    out = f"{header}\n\n{body}"
    if len(new_items) > MAX_MESSAGE_LISTINGS:
        out += f"\n\n(+{len(new_items) - MAX_MESSAGE_LISTINGS} more)"
    return out


# =========================
# ONE SHOT RUN (CRON FRIENDLY)
# =========================


def bootstrap_seen(driver, seen: Set[str], seen_titles: Set[str]) -> int:
    total = 0
    for search_name, url in SEARCHES.items():
        items = scrape_search(driver, search_name, url)
        total += len(items)
        for item in items:
            seen.add(item.post_id)
            seen_titles.add(canonical_listing_title(item.title))
    save_seen(seen)
    save_seen_titles(seen_titles)
    return total


def should_send_heartbeat() -> bool:
    now_epoch = int(time.time())
    return (now_epoch - load_last_heartbeat_epoch()) >= HEARTBEAT_SECONDS


def send_heartbeat() -> None:
    now_epoch = int(time.time())
    send_telegram(f"[{now_pacific()}] still working, nothing new.")
    save_last_heartbeat_epoch(now_epoch)


def _run_once_with_driver(driver, seen: Set[str], seen_titles: Set[str]) -> int:
    if not seen:
        total = bootstrap_seen(driver, seen, seen_titles)
        send_telegram(
            f"[{now_pacific()}] Craigslist watcher initialized. "
            f"Seeded {total} existing listings, alerts start now."
        )
        save_last_heartbeat_epoch(int(time.time()))
        return 0

    # Older installs only tracked post IDs; prime title memory from live results once.
    if not seen_titles:
        for search_name, url in SEARCHES.items():
            items = scrape_search(driver, search_name, url)
            for item in items:
                seen_titles.add(canonical_listing_title(item.title))
            time.sleep(random.randint(*JITTER_SECONDS))
        save_seen_titles(seen_titles)

    filtered_new_items: List[Listing] = []
    changed_seen = False
    changed_titles = False

    for search_name, url in SEARCHES.items():
        items = scrape_search(driver, search_name, url)
        for item in items:
            if item.post_id in seen:
                continue
            seen.add(item.post_id)
            changed_seen = True

            canon = canonical_listing_title(item.title)
            if canon in seen_titles:
                continue
            seen_titles.add(canon)
            changed_titles = True

            if passes_filters(item):
                filtered_new_items.append(item)

        time.sleep(random.randint(*JITTER_SECONDS))

    if changed_seen:
        save_seen(seen)
    if changed_titles:
        save_seen_titles(seen_titles)

    if filtered_new_items:
        send_telegram(format_new_listing_message(filtered_new_items))
        save_last_heartbeat_epoch(int(time.time()))

    if should_send_heartbeat() and not filtered_new_items:
        send_heartbeat()

    if load_last_error_hash():
        save_last_error_hash("")

    return 0


def quit_driver(driver, timeout: int = 15) -> None:
    """Shut the browser down, but never hang: driver.quit() can block forever
    on a wedged browser, which would hold the cron lock and silently stall
    every later run until the process dies."""
    if driver is None:
        return
    ex = futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(driver.quit)
        try:
            fut.result(timeout=timeout)
            return
        except Exception:
            pass
        # quit() hung or blew up: make sure the browser process itself dies.
        try:
            service = getattr(driver, "service", None)
            if service is not None:
                service.stop()
        except Exception:
            pass
    finally:
        ex.shutdown(wait=False)


def run_once() -> int:
    """Run one check, retrying transient browser failures with a fresh driver."""
    ensure_state_dir()
    seen = load_seen()
    seen_titles = load_seen_titles()

    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        driver = None
        try:
            driver = build_driver()
            return _run_once_with_driver(driver, seen, seen_titles)
        except (TimeoutException, WebDriverException) as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                print(
                    f"attempt {attempt}/{MAX_ATTEMPTS} failed "
                    f"({type(exc).__name__}); retrying in {RETRY_DELAY_SECONDS}s",
                    file=sys.stderr,
                )
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                print(
                    f"attempt {attempt}/{MAX_ATTEMPTS} failed "
                    f"({type(exc).__name__}); giving up",
                    file=sys.stderr,
                )
        finally:
            quit_driver(driver, DRIVER_QUIT_TIMEOUT_SECONDS)
    assert last_exc is not None
    raise last_exc


def main() -> None:
    if not ensure_disk_space():
        msg = (
            f"[{now_pacific()}] Craigslist watcher paused: disk critically low "
            f"(<{MIN_DISK_FREE_MB} MB free), skipping run."
        )
        print(msg, file=sys.stderr)
        try:
            send_error_notification("disk-full", msg)
        except Exception:
            pass
        sys.exit(0)
    try:
        exit_code = run_once()
        sys.exit(exit_code)
    except (TimeoutException, WebDriverException, requests.RequestException) as exc:
        error_key = f"{type(exc).__name__}:{str(exc)[:180]}"
        detail = traceback.format_exc()[-1500:]
        send_error_notification(
            error_key=error_key,
            message=f"[{now_pacific()}] Craigslist watcher error: {exc}\n\n{detail}",
        )
        raise
    except Exception as exc:
        error_key = f"fatal:{type(exc).__name__}:{str(exc)[:180]}"
        detail = traceback.format_exc()[-1500:]
        send_error_notification(
            error_key=error_key,
            message=f"[{now_pacific()}] Craigslist watcher fatal error: {exc}\n\n{detail}",
        )
        raise


if __name__ == "__main__":
    main()
