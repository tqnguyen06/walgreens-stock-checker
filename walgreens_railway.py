"""
Walgreens In-Store Stock Checker - Railway Edition

Monitors Walgreens products for in-store stock availability and sends
Pushover + Discord alerts when items are found nearby.

No browser required - uses Walgreens inventory API directly.

Usage:
    python walgreens_railway.py              # Run continuous monitor
    python walgreens_railway.py --once       # Check once and exit (for cron)
    python walgreens_railway.py --test       # Test notifications
    python walgreens_railway.py --extract    # Extract product IDs from a URL (run locally)
    python walgreens_railway.py --help       # Show help

Environment Variables:
    WALGREENS_PRODUCTS       - Product configs (see below for format)
    WALGREENS_ZIP            - Zip code(s) for store search, comma-separated (default: 32218)
    WALGREENS_RADIUS         - Search radius in miles (default: 25)
    WALGREENS_CHECK_INTERVAL - Seconds between checks (default: 600)
    PUSHOVER_APP_TOKEN       - Pushover API token
    DISCORD_WEBHOOK_URL      - Discord webhook URL (optional)
    DISCORD_ROLE_ID          - Discord role ID to ping (optional)
    TIMEZONE                 - Timezone for timestamps (default: America/New_York)

WALGREENS_PRODUCTS format (pipe-separated fields, semicolon between products):
    name|articleId|planogram|url ; name|articleId|planogram|url

    Example:
    Pokemon ETB|000000000012449025|40000405020|https://www.walgreens.com/store/c/.../ID=300455939-product

    Use --extract to get articleId and planogram from a product URL.
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Optional

import requests
from zoneinfo import ZoneInfo

# Configuration
WALGREENS_ZIPS = [z.strip() for z in os.getenv("WALGREENS_ZIP", "32218").split(",") if z.strip()]
WALGREENS_RADIUS = int(os.getenv("WALGREENS_RADIUS", "25"))
WALGREENS_CHECK_INTERVAL = int(os.getenv("WALGREENS_CHECK_INTERVAL", "600"))
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "")
PUSHOVER_USER_KEY = "uzmaqrmwawus7dk8smym64rzovrt5p"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_ROLE_ID = os.getenv("DISCORD_ROLE_ID", "")
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

BASE_URL = "https://www.walgreens.com"
INVENTORY_API = f"{BASE_URL}/locator/v1/search/stores/inventory/radius?requestor=COS"
STATE_FILE = os.getenv("STATE_FILE", "/tmp/walgreens_stock_state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    """Print with timestamp."""
    tz = ZoneInfo(TIMEZONE)
    ts = datetime.now(tz).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def get_time_str() -> str:
    """Get current time string."""
    tz = ZoneInfo(TIMEZONE)
    return datetime.now(tz).strftime("%I:%M %p %Z on %B %d, %Y")


# ---------------------------------------------------------------------------
# Product parsing
# ---------------------------------------------------------------------------

def parse_products_env() -> list[dict]:
    """Parse WALGREENS_PRODUCTS env var into product list.

    Uses semicolon (;) between products because Walgreens URLs contain commas.
    Fields within each product are pipe (|) separated.
    """
    raw = os.getenv("WALGREENS_PRODUCTS", "")
    if not raw.strip():
        return []

    products = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) < 3:
            print(f"WARNING: Skipping malformed product entry: {entry}")
            print("  Expected format: name|articleId|planogram|url")
            continue
        products.append({
            "name": parts[0].strip(),
            "articleId": parts[1].strip(),
            "planogram": parts[2].strip(),
            "url": parts[3].strip() if len(parts) > 3 else "",
        })
    return products


# ---------------------------------------------------------------------------
# Inventory API
# ---------------------------------------------------------------------------

def check_inventory(products: list[dict], zip_code: str, radius: int) -> dict:
    """
    Check in-store stock for products at a single zip code.

    Returns dict mapping product name -> { stores: [...], total_stores_checked: int }
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    })

    results = {}

    for product in products:
        payload = {
            "requestType": "filterInStockStores",
            "p": "1",
            "s": "50",
            "r": str(radius),
            "excludeEmergencyClosed": True,
            "articles": [
                {
                    "planogram": product["planogram"],
                    "articleId": product["articleId"],
                    "qty": 1,
                    "isSelectedItem": "true",
                    "opstudyId": None,
                }
            ],
            "bySource": "Web_BAS_PDP_COS",
            "inStockOnly": False,
            "zip": zip_code,
            "q": zip_code,
        }

        try:
            resp = session.post(INVENTORY_API, json=payload, timeout=15)

            if resp.status_code == 403:
                log(f"  403 Forbidden for {product['name']} - Walgreens may be blocking requests")
                results[product["name"]] = {"error": "blocked", "stores": []}
                continue

            if resp.status_code != 200:
                log(f"  HTTP {resp.status_code} for {product['name']}")
                results[product["name"]] = {"error": f"http_{resp.status_code}", "stores": []}
                continue

            data = resp.json()
            store_results = data.get("results", [])

            in_stock_stores = []
            for store_data in store_results:
                store = store_data.get("store", {})
                inventory = store_data.get("inventory", [])
                distance = store_data.get("distance", "?")

                for inv in inventory:
                    if inv.get("articleId") == product["articleId"]:
                        if inv.get("inventoryCount", 0) > 0 or inv.get("status") == "In Stock":
                            addr = store.get("address", {})
                            in_stock_stores.append({
                                "name": store.get("name", store.get("storeName", f"Store #{store.get('storeNumber', '?')}")),
                                "number": store.get("storeNumber", ""),
                                "street": addr.get("street", ""),
                                "city": addr.get("city", ""),
                                "state": addr.get("state", ""),
                                "zip": addr.get("zip", ""),
                                "distance": distance,
                                "count": inv.get("inventoryCount", 0),
                            })

            results[product["name"]] = {
                "stores": in_stock_stores,
                "total_stores_checked": len(store_results),
            }

            status = f"IN STOCK at {len(in_stock_stores)} stores" if in_stock_stores else f"Out of stock ({len(store_results)} stores)"
            log(f"  [{zip_code}] {product['name']}: {status}")

        except requests.RequestException as e:
            log(f"  Error checking {product['name']}: {e}")
            results[product["name"]] = {"error": str(e), "stores": []}

        # Small delay between products
        if len(products) > 1:
            time.sleep(1)

    return results


# ---------------------------------------------------------------------------
# State management (track what we've already alerted on)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load previous alert state."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return {"alerted": {}}


def save_state(state: dict) -> None:
    """Save alert state."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except IOError as e:
        log(f"Could not save state: {e}")


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------

def send_discord_alert(product_name: str, stores: list[dict], product_url: str) -> bool:
    """Send Discord embed for in-stock product."""
    if not DISCORD_WEBHOOK_URL:
        return False

    store_lines = []
    for s in stores[:10]:
        store_lines.append(
            f"**#{s['number']}** - {s['street']}, {s['city']} {s['state']} "
            f"({s['distance']} mi) — Qty: {s['count']}"
        )

    description = "\n".join(store_lines)
    if len(stores) > 10:
        description += f"\n*...and {len(stores) - 10} more stores*"

    embed = {
        "title": f"IN STOCK: {product_name}",
        "description": description,
        "color": 0x00FF00,
        "footer": {"text": f"Zips: {', '.join(WALGREENS_ZIPS)} | Radius: {WALGREENS_RADIUS}mi | {get_time_str()}"},
    }
    if product_url:
        embed["url"] = product_url

    payload: dict = {"embeds": [embed]}
    if DISCORD_ROLE_ID:
        payload["content"] = f"<@&{DISCORD_ROLE_ID}>"

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        log(f"Discord alert sent for {product_name}")
        return True
    except requests.RequestException as e:
        log(f"Discord error: {e}")
        return False


# ---------------------------------------------------------------------------
# Pushover notifications
# ---------------------------------------------------------------------------

def send_pushover_alert(product_name: str, stores: list[dict], product_url: str) -> bool:
    """Send Pushover emergency alert for in-stock product."""
    if not PUSHOVER_APP_TOKEN:
        return False

    store_lines = []
    for s in stores[:3]:
        store_lines.append(f"#{s['number']} {s['street']}, {s['city']} ({s['distance']}mi) Qty: {s['count']}")

    message = f"{product_name}\n\n" + "\n".join(store_lines)
    if len(stores) > 3:
        message += f"\n+{len(stores) - 3} more stores"

    data = {
        "token": PUSHOVER_APP_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "title": "WALGREENS IN STOCK",
        "message": message,
        "priority": 2,
        "retry": 30,
        "expire": 300,
        "sound": "siren",
        "url": product_url,
        "url_title": "Open Walgreens",
        "timestamp": int(time.time()),
    }
    try:
        resp = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=10)
        if resp.status_code == 200:
            log("Pushover alert sent")
            return True
        else:
            log(f"Pushover failed: HTTP {resp.status_code}")
            return False
    except requests.RequestException as e:
        log(f"Pushover error: {e}")
        return False


# ---------------------------------------------------------------------------
# Product ID extraction (run locally with --extract)
# ---------------------------------------------------------------------------

def extract_product_ids(url: str) -> None:
    """
    Extract articleId and planogram from a Walgreens product page.
    Requires Selenium - run locally, not on Railway.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        print("ERROR: Selenium is required for --extract. Run locally:")
        print("  pip install selenium")
        return

    print(f"Loading: {url}")

    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--log-level=3")

    driver = webdriver.Chrome(options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    try:
        driver.get(url)
        time.sleep(4)

        state_json = driver.execute_script(
            "return JSON.stringify(window.__ATC_APP_INITIAL_STATE__?.product || {});"
        )
        state_str = state_json or "{}"

        article_match = re.search(r'"articleId"\s*:\s*"([^"]+)"', state_str)
        pln_match = re.search(r'"pln"\s*:\s*"([^"]+)"', state_str)
        title = driver.title.replace(" | Walgreens", "").strip()

        if article_match and pln_match:
            article_id = article_match.group(1)
            planogram = pln_match.group(1)

            print(f"\n{'='*60}")
            print(f"Product: {title}")
            print(f"Article ID: {article_id}")
            print(f"Planogram:  {planogram}")
            print(f"\nAdd this to your WALGREENS_PRODUCTS env var:")
            print(f"\n  {title}|{article_id}|{planogram}|{url}")
            print(f"\n{'='*60}")
        else:
            print("ERROR: Could not find articleId/planogram on this page.")
            print("Make sure the URL is a valid Walgreens product page.")
    finally:
        driver.quit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(products: list[dict], silent: bool = False) -> bool:
    """Run a single check across all zip codes. Returns True if any product is in stock.
    If silent=True, records state but skips sending alerts (used for baseline scan)."""
    zips_str = ", ".join(WALGREENS_ZIPS)
    log(f"Checking {len(products)} product(s) across {len(WALGREENS_ZIPS)} zip(s): {zips_str}")

    # Check each zip and merge results, deduplicating stores by store number
    merged: dict[str, list[dict]] = {p["name"]: [] for p in products}

    for zip_code in WALGREENS_ZIPS:
        results = check_inventory(products, zip_code, WALGREENS_RADIUS)
        for product in products:
            name = product["name"]
            r = results.get(name, {})
            # Deduplicate by store number
            seen_stores = {s["number"] for s in merged[name]}
            for store in r.get("stores", []):
                if store["number"] not in seen_stores:
                    merged[name].append(store)
                    seen_stores.add(store["number"])

        # Small delay between zips
        if len(WALGREENS_ZIPS) > 1:
            time.sleep(1)

    state = load_state()
    any_in_stock = False
    state_changed = False

    for product in products:
        name = product["name"]
        stores = merged[name]
        current_store_numbers = {s["number"] for s in stores}

        # Get previously known in-stock stores for this product
        known_stores = set(state.get("in_stock_stores", {}).get(name, []))

        # Find stores that are newly in stock (not previously known)
        new_stores = [s for s in stores if s["number"] not in known_stores]
        # Find stores that went out of stock
        gone_stores = known_stores - current_store_numbers

        if stores:
            any_in_stock = True

        if new_stores:
            if silent:
                log(f"BASELINE: {name} at {len(new_stores)} store(s) (no alert)")
            else:
                log(f"NEW STOCK: {name} at {len(new_stores)} new store(s)!")
                send_pushover_alert(name, new_stores, product.get("url", ""))
                send_discord_alert(name, new_stores, product.get("url", ""))

        if gone_stores:
            log(f"{name}: {len(gone_stores)} store(s) went out of stock")

        if not stores and known_stores:
            log(f"{name}: All stores now out of stock")

        if stores:
            log(f"{name}: In stock at {len(stores)} store(s) ({len(new_stores)} new)")
        else:
            log(f"{name}: Out of stock everywhere")

        # Update state with current in-stock store numbers
        if current_store_numbers != known_stores:
            state.setdefault("in_stock_stores", {})[name] = list(current_store_numbers)
            state_changed = True

    if state_changed:
        save_state(state)

    return any_in_stock


def run_continuous(products: list[dict]) -> None:
    """Run continuous monitoring loop."""
    log(f"Starting continuous monitor")
    log(f"Products: {len(products)}")
    log(f"Zips: {', '.join(WALGREENS_ZIPS)} | Radius: {WALGREENS_RADIUS}mi")
    log(f"Check interval: {WALGREENS_CHECK_INTERVAL}s ({WALGREENS_CHECK_INTERVAL // 60}min)")
    log(f"Pushover: {'Yes' if PUSHOVER_APP_TOKEN else 'No'}")
    log(f"Discord: {'Yes' if DISCORD_WEBHOOK_URL else 'No'}")

    for p in products:
        log(f"  - {p['name']}")

    check_count = 0

    while True:
        check_count += 1
        log(f"--- Check #{check_count} ---")

        try:
            run_once(products)
        except Exception as e:
            log(f"Error during check: {e}")

        log(f"Next check in {WALGREENS_CHECK_INTERVAL}s")
        time.sleep(WALGREENS_CHECK_INTERVAL)


def test_notifications() -> None:
    """Test Pushover and Discord notifications."""
    print("\n--- Testing Notifications ---\n")

    fake_stores = [{
        "name": "Test Store",
        "number": "12345",
        "street": "123 Main St",
        "city": "Jacksonville",
        "state": "FL",
        "zip": "32218",
        "distance": 1.5,
        "count": 2,
    }]

    if PUSHOVER_APP_TOKEN:
        ok = send_pushover_alert("Test Product", fake_stores, "https://www.walgreens.com")
        print(f"Pushover: {'OK' if ok else 'FAILED'}")
    else:
        print("Pushover: Not configured (set PUSHOVER_APP_TOKEN)")

    if DISCORD_WEBHOOK_URL:
        ok = send_discord_alert("Test Product", fake_stores, "https://www.walgreens.com")
        print(f"Discord: {'OK' if ok else 'FAILED'}")
    else:
        print("Discord: Not configured (set DISCORD_WEBHOOK_URL)")


def show_help() -> None:
    print("""
Walgreens In-Store Stock Checker - Railway Edition
===================================================

Monitors Walgreens in-store inventory and alerts via Pushover + Discord.

Commands:
    python walgreens_railway.py              Continuous monitoring
    python walgreens_railway.py --once       Single check, then exit
    python walgreens_railway.py --test       Test notifications
    python walgreens_railway.py --extract    Extract product IDs (local only)
    python walgreens_railway.py --help       Show this help

Setup:
    1. Run --extract locally for each product URL to get IDs
    2. Set WALGREENS_PRODUCTS env var with the extracted values
    3. Deploy to Railway

WALGREENS_PRODUCTS format:
    name|articleId|planogram|url ; name|articleId|planogram|url

Example:
    Pokemon ETB|000000000012449025|40000405020|https://www.walgreens.com/store/c/pokemon-trading-card-game,-elite-trainer-box/ID=300455939-product

Environment Variables:
    WALGREENS_PRODUCTS       Product list (required, see format above)
    WALGREENS_ZIP            Zip code(s), comma-separated (default: 32218)
                             Example: 32218,32258,32082
    WALGREENS_RADIUS         Search radius in miles per zip (default: 25)
    WALGREENS_CHECK_INTERVAL Seconds between checks (default: 600)
    PUSHOVER_APP_TOKEN       Pushover API token
    DISCORD_WEBHOOK_URL      Discord webhook URL (optional)
    DISCORD_ROLE_ID          Discord role ID to ping (optional)
    TIMEZONE                 Timezone (default: America/New_York)
""")


def main() -> None:
    if "--help" in sys.argv or "-h" in sys.argv:
        show_help()
        return

    if "--test" in sys.argv:
        test_notifications()
        return

    if "--extract" in sys.argv:
        url = input("Walgreens product URL: ").strip()
        if url:
            extract_product_ids(url)
        return

    products = parse_products_env()
    if not products:
        print("ERROR: No products configured.")
        print("Set WALGREENS_PRODUCTS env var. Run with --help for format.")
        print("Use --extract to get product IDs from a URL.")
        sys.exit(1)

    if "--once" in sys.argv:
        run_once(products)
    else:
        run_continuous(products)


if __name__ == "__main__":
    main()
