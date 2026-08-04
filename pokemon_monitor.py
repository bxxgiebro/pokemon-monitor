import requests
from bs4 import BeautifulSoup
import re
import time
import json
import os
import logging
from urllib.parse import urljoin

# ---------------- CONFIG ----------------

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "")
ALZA_ENABLED = os.environ.get("ALZA_ENABLED", "false").lower() == "true"

WEBHOOKS = {
    "iHrysko": os.environ.get("DISCORD_WEBHOOK_IHRYSKO", ""),
    "VeselyDrak": os.environ.get("DISCORD_WEBHOOK_VESELYDRAK", ""),
    "Smarty": os.environ.get("DISCORD_WEBHOOK_SMARTY", ""),
    "PGS": os.environ.get("DISCORD_WEBHOOK_PGS", ""),
    "Brloh": os.environ.get("DISCORD_WEBHOOK_BRLOH", ""),
    "Dracik": os.environ.get("DISCORD_WEBHOOK_DRACIK", ""),
    "Alza": os.environ.get("DISCORD_WEBHOOK_ALZA", ""),
}
STATE_FILE = "seen_state.json"

SITES = [
   {
        "name": "Dracik",
        "url": "https://www.dracik.sk/pokemon-1076/",
        "product_url_pattern": r"^/[a-z0-9\-]{8,}/$",
        "in_stock_keywords": ["Skladom", "Do košíka"],
        "out_of_stock_keywords": ["Produkt nie je skladom", "Nedostupné"],
        "require_price_context": True,
    },
    {
        "name": "VeselyDrak",
        "url": "https://www.vesely-drak.sk/produkty/pokemon-karty/",
        "product_url_pattern": r"/produkty/[^/]+/\d+-",
        "in_stock_keywords": ["Skladom", "Do košíka"],
        "out_of_stock_keywords": ["Vypredané", "Nedostupné"],
    },
    {
        "name": "Smarty",
        "url": "https://www.smarty.sk/pokemon-tcg-4c9937",
        "product_url_pattern": r"-4p\d+",
        "in_stock_keywords": ["Na sklade", "Do košíka"],
        "out_of_stock_keywords": ["Neznáma dostupnosť", "Na ceste", "Vypredané"],
    },
    {
        "name": "PGS",
        "url": "https://www.pgs.sk/pokemon-tcg-4c13083",
        "product_url_pattern": r"-4p\d+",
        "in_stock_keywords": ["Na sklade", "Do košíka"],
        "out_of_stock_keywords": ["Neznáma dostupnosť", "Na ceste", "Vypredané"],
    },
    {
        # Guessed to follow the same "-pNNNN" pattern as iHrysko (same shop platform).
        # Verify against the first run's log.
        "name": "Brloh",
        "url": "https://www.brloh.sk/pokemon-c1781",
        "product_url_pattern": r"-p\d+",
        "in_stock_keywords": ["Vložiť do košíka", "skladom"],
        "out_of_stock_keywords": ["Očakávame", "dlhodobo nedostupné", "Vypredané"],
    },
    {
        "name": "Dracik",
        "url": "https://www.dracik.sk/pokemon-1076/",
        "product_url_pattern": r"/basket/add/\?product_id=\d+",
        "in_stock_keywords": ["Skladom"],
        "out_of_stock_keywords": ["Produkt nie je skladom", "Nedostupné"],
    },
]
if ALZA_ENABLED:
    SITES.append({
        "name": "Alza",
        "url": "https://www.alza.sk/hracky/pokemon-karty/18879069.htm",
        "product_url_pattern": r"-d\d+\.htm",
        "in_stock_keywords": ["Na sklade", "Do košíka"],
        "out_of_stock_keywords": ["Dopyt", "Momentálne nedostupné", "Vypredané"],
        "use_proxy": True,
    })

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Failed to read state file, starting fresh: {e}")
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_discord_alert(site_name, title, product_name, link):
    webhook_url = WEBHOOKS.get(site_name)
    if not webhook_url:
        log.error(f"No webhook configured for {site_name} — skipping alert send")
        return
    payload = {"content": f"🚨 **{title}** 🚨\n**{product_name}**\n👉 {link}"}
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to send Discord alert: {e}")


def find_product_container(anchor_element):
    current = anchor_element
    for _ in range(8):
        if not current.parent:
            break
        current = current.parent
        if current.find(["h2", "h3"]):
            return current
    return anchor_element.parent


def scan_site(site):
    try:
        if site.get("use_proxy") and SCRAPERAPI_KEY:
            proxy_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={site['url']}"
            resp = requests.get(proxy_url, timeout=30)
        else:
            resp = requests.get(site["url"], headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            log.warning(f"[{site['name']}] got status {resp.status_code}")
            return None
    except requests.RequestException as e:
        log.error(f"[{site['name']}] request failed: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(site["product_url_pattern"])
    products = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pattern.search(href):
            continue

        full_url = urljoin(site["url"], href)
        container = find_product_container(a)

        heading = container.find(["h2", "h3"])
        name = heading.get_text(strip=True) if heading else a.get_text(strip=True)
        if not name or len(name) < 5:
            continue

        context_text = container.get_text(" ", strip=True)
        if site.get("require_price_context") and "€" not in context_text:
            continue

        if any(kw in context_text for kw in site["out_of_stock_keywords"]):
            in_stock = False
        elif any(kw in context_text for kw in site["in_stock_keywords"]):
            in_stock = True
        else:
            in_stock = False

        if full_url not in products or len(name) > len(products[full_url]["name"]):
            products[full_url] = {"name": name, "in_stock": in_stock}

    return products


def check_site(site, state):
    name = site["name"]
    current = scan_site(site)
    if current is None:
        return

    is_first_run = name not in state
    previous = state.get(name, {})

    for url, info in current.items():
        prev_info = previous.get(url)

        if is_first_run:
            continue  # seed baseline, don't alert on everything at once

        if prev_info is None:
            log.info(f"[{name}] NEW PRODUCT: {info['name']}")
            send_discord_alert(name, f"@everyone NEW PRODUCT - {name.upper()}", info["name"], url)
            time.sleep(1.5)
        elif info["in_stock"] and not prev_info.get("in_stock", False):
            log.info(f"@everyone [{name}] RESTOCKED: {info['name']}")
            send_discord_alert(name, f"@everyone RESTOCK - {name.upper()}", info["name"], url)
            time.sleep(1.5)

    if is_first_run:
        log.info(f"[{name}] first run — seeded baseline with {len(current)} products, no alerts sent")

    state[name] = current
    log.info(f"[{name}] scanned {len(current)} products")


def main():
    state = load_state()
    for site in SITES:
        check_site(site, state)
        time.sleep(2)
    save_state(state)
    log.info("Sweep complete.")


if __name__ == "__main__":
    main()
