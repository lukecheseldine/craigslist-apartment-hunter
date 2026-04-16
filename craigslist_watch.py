import json
import os
import random
import re
import shutil
import sys
import time
import traceback
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

SEARCHES = {
    "sf_dog_friendly": (
        "https://sfbay.craigslist.org/search/san-francisco-ca/apa"
        "?lat=37.7739&lon=-122.434&max_price=4500"
        "&pets_dog=1&search_distance=0.6&sort=date"
    ),
}

ALLOW_KEYWORDS: List[str] = []

BLOCK_KEYWORDS: List[str] = [
    "room for rent",
    "sublet",
]

STATE_DIR = Path("state")
SEEN_FILE = STATE_DIR / "seen_posts.json"
HEARTBEAT_FILE = STATE_DIR / "last_heartbeat_epoch.txt"
LAST_ERROR_FILE = STATE_DIR / "last_error_hash.txt"

HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "3600"))
ERROR_NOTIFY_COOLDOWN_SECONDS = int(os.getenv("ERROR_NOTIFY_COOLDOWN_SECONDS", "900"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HEADLESS = os.getenv("HEADLESS", "1") != "0"
PAGE_LOAD_TIMEOUT = int(os.getenv("PAGE_LOAD_TIMEOUT", "30"))
RESULT_WAIT_SECONDS = int(os.getenv("RESULT_WAIT_SECONDS", "20"))
JITTER_SECONDS = (1, 4)
MAX_MESSAGE_LISTINGS = int(os.getenv("MAX_MESSAGE_LISTINGS", "12"))
# Skip listings at or below this price (common scam range). Unparseable price = keep listing.
MIN_PRICE_DOLLARS = float(os.getenv("MIN_PRICE_DOLLARS", "2500"))
FIREFOX_BINARY = os.getenv("FIREFOX_BINARY", "").strip()
GECKODRIVER_PATH = os.getenv("GECKODRIVER_PATH", "").strip()
CHROME_BINARY = os.getenv("CHROME_BINARY", "").strip()
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "").strip()
BROWSER = os.getenv("BROWSER", "firefox").strip().lower()
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
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")


def load_last_heartbeat_epoch() -> int:
    try:
        return int(HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def save_last_heartbeat_epoch(epoch: int) -> None:
    HEARTBEAT_FILE.write_text(str(epoch), encoding="utf-8")


def load_last_error_hash() -> str:
    try:
        return LAST_ERROR_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def save_last_error_hash(value: str) -> None:
    LAST_ERROR_FILE.write_text(value, encoding="utf-8")


# =========================
# TELEGRAM
# =========================

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def now_pacific() -> str:
    # Example: Tue Apr 14, 10:23 AM PT
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
    # Avoid spamming the same recurring error on every cron run.
    last_key = load_last_error_hash()
    if error_key != last_key:
        send_telegram(message)
        save_last_error_hash(error_key)


# =========================
# SELENIUM SETUP
# =========================

def build_driver():
    if REMOTE_WEBDRIVER_URL:
        if BROWSER == "chrome":
            options = ChromeOptions()
            if HEADLESS:
                options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            driver = webdriver.Remote(command_executor=REMOTE_WEBDRIVER_URL, options=options)
        else:
            options = FirefoxOptions()
            if HEADLESS:
                options.add_argument("--headless")
            options.set_preference("dom.webdriver.enabled", False)
            options.set_preference("media.peerconnection.enabled", False)
            # Keep memory footprint low on small servers.
            options.set_preference("dom.ipc.processCount", 1)
            driver = webdriver.Remote(command_executor=REMOTE_WEBDRIVER_URL, options=options)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        return driver

    if BROWSER == "chrome":
        options = ChromeOptions()
        if HEADLESS:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")

        binary_candidates = [
            CHROME_BINARY,
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
        ]
        for candidate in binary_candidates:
            if candidate and Path(candidate).is_file():
                options.binary_location = candidate
                break

        if CHROMEDRIVER_PATH:
            service = ChromeService(executable_path=CHROMEDRIVER_PATH)
        else:
            service = ChromeService()

        driver = webdriver.Chrome(service=service, options=options)
    else:
        options = FirefoxOptions()
        if HEADLESS:
            options.add_argument("--headless")

        # On some Ubuntu servers, /usr/bin/firefox is a snap wrapper and Selenium
        # needs the real binary path.
        binary_candidates = [
            FIREFOX_BINARY,
            "/snap/firefox/current/usr/lib/firefox/firefox",
            shutil.which("firefox"),
        ]
        for candidate in binary_candidates:
            if candidate and Path(candidate).is_file():
                options.binary_location = candidate
                break

        options.set_preference("dom.webdriver.enabled", False)
        options.set_preference("media.peerconnection.enabled", False)
        # Keep memory footprint low on small servers.
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
    selectors = [
        (By.CLASS_NAME, "posting-title"),
        (By.CSS_SELECTOR, "a.posting-title"),
        (By.CSS_SELECTOR, "a"),
    ]
    for by, selector in selectors:
        try:
            el = card.find_element(by, selector)
            href = el.get_attribute("href")
            if href:
                return href
        except Exception:
            continue
    return None


def title_from_card(card) -> str:
    selectors = [
        (By.CLASS_NAME, "posting-title"),
        (By.CSS_SELECTOR, "a.posting-title"),
        (By.CSS_SELECTOR, "a"),
    ]
    for by, selector in selectors:
        try:
            text = card.find_element(by, selector).text.strip()
            if text:
                return text
        except Exception:
            continue
    return "(untitled)"


def scrape_search(driver: webdriver.Firefox, search_name: str, url: str) -> List[Listing]:
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


def parse_price_dollars(price_text: str) -> Optional[float]:
    """First $ amount in the string, e.g. '$3,200' -> 3200.0; also plain '3,200' in price field."""
    if not price_text:
        return None
    t = price_text.strip()
    m = re.search(r"\$\s*([\d,]+)", t)
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    if re.fullmatch(r"[\d,]+", t):
        try:
            return float(t.replace(",", ""))
        except ValueError:
            return None
    return None


def listing_price_for_filter(listing: Listing) -> Optional[float]:
    """Prefer card price; fall back to title if no price on card."""
    p = parse_price_dollars(listing.price)
    if p is not None:
        return p
    return parse_price_dollars(listing.title)


def passes_filters(listing: Listing) -> bool:
    haystack = normalize_text([listing.title, listing.meta, listing.hood])

    if ALLOW_KEYWORDS and not any(keyword.lower() in haystack for keyword in ALLOW_KEYWORDS):
        return False

    if any(keyword.lower() in haystack for keyword in BLOCK_KEYWORDS):
        return False

    price_val = listing_price_for_filter(listing)
    if price_val is not None and price_val <= MIN_PRICE_DOLLARS:
        return False

    return True


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

def bootstrap_seen(driver: webdriver.Firefox, seen: Set[str]) -> int:
    total = 0
    for search_name, url in SEARCHES.items():
        items = scrape_search(driver, search_name, url)
        total += len(items)
        for item in items:
            seen.add(item.post_id)
    save_seen(seen)
    return total


def should_send_heartbeat() -> bool:
    now_epoch = int(time.time())
    return (now_epoch - load_last_heartbeat_epoch()) >= HEARTBEAT_SECONDS


def send_heartbeat() -> None:
    now_epoch = int(time.time())
    send_telegram(f"[{now_pacific()}] still working, nothing new.")
    save_last_heartbeat_epoch(now_epoch)


def run_once() -> int:
    ensure_state_dir()
    seen = load_seen()
    driver = build_driver()

    try:
        if not seen:
            total = bootstrap_seen(driver, seen)
            send_telegram(
                f"[{now_pacific()}] Craigslist watcher initialized. "
                f"Seeded {total} existing listings, alerts start now."
            )
            save_last_heartbeat_epoch(int(time.time()))
            return 0

        filtered_new_items: List[Listing] = []
        changed_seen = False

        for search_name, url in SEARCHES.items():
            items = scrape_search(driver, search_name, url)
            for item in items:
                if item.post_id in seen:
                    continue
                seen.add(item.post_id)
                changed_seen = True

                if passes_filters(item):
                    filtered_new_items.append(item)

            time.sleep(random.randint(*JITTER_SECONDS))

        if changed_seen:
            save_seen(seen)

        if filtered_new_items:
            send_telegram(format_new_listing_message(filtered_new_items))
            save_last_heartbeat_epoch(int(time.time()))

        if should_send_heartbeat() and not filtered_new_items:
            send_heartbeat()

        # If we were previously stuck in an error mode, clear dedupe marker after a good run.
        if load_last_error_hash():
            save_last_error_hash("")

        return 0

    finally:
        try:
            driver.quit()
        except Exception:
            pass


def main() -> None:
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
