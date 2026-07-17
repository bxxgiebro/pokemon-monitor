import requests
from bs4 import BeautifulSoup
import re
import time
import json
import os
import logging

# ---------------- CONFIG ----------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "seen_state.json"

# Each site scans a category page and finds ALL products on it.
# product_url_pattern: regex that matches a product link's href (used to find product links)
# in_stock_keywords: if any of these appear near the product, we consider it in stock
# out_of_stock_keywords: if any of these appear, we consider it NOT in stock (checked first)

SITES = [
    {
        "name": "iHrysko",
        "url": "https://www.ihrysko.sk/pokemon-tcg-c17668",
        "product_url_pattern": r"-p\d+",
        "in_stock_keywords": ["Vložiť do košíka", "skladom"],
        "out_of_stock_keywords": ["Očakávame", "dlhodobo nedostupné", "Vypredané"],
    },
    {
        "name": "Alza",
        "url": "https://www.alza.sk/hracky/pokemon-karty/18879069.htm",
        "product_url_pattern": r"-d\d+\.htm",
        "in_stock_keywords": ["Na sklade", "Do košíka"],
        "out_of_stock_keywords": ["Dopyt", "Momentálne nedostupné", "Vypredané"],
    },
    {
        "name": "VeselyDrak",
        "url": "https://www.vesely-drak.sk/produkty/pokemon-karty/",
        "product_url_pattern": r"/produkty/[^/]+/\d+-",
        "in_stock_keywords": ["Skladom", "Skladem"],
        "out_of_stock_keywords": ["Predobjednávka", "Vypredané", "Nedostupné"],
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
        "url": "https://www.pgs.sk/pokemon-tcg-4c9937",
        "product_url_pattern": r"-4p\d+",
        "in_stock_keywords": ["Na sklade", "Do košíka"],
        "out_of_stock_keywords": ["Neznáma dostupnosť", "Na ceste", "Vypredané"],
    },
    {
        "name": "Dracik",
        "url": "https://www.dracik.sk/karty-pokemon/",
        "product_url_pattern": r"^/[a-z0-9-]+/$",
        "in_stock_keywords": ["Skladom"],
        "out_of_stock_keywords": ["nie je skladom", "Obmedzený predaj"],
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_discord_alert(title, product_name, link):
    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL not set — skipping alert send")
        return
    payload = {"content": f"🚨 **{title}** 🚨\n**{product_name}**\n👉 {link}"}
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to send Discord alert: {e}")


def scan_site(site):
    """Returns dict of {product_url: {"name": str, "in_stock": bool}}"""
    resp = requests.get(site["url"], headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        log.warning(f"[{site['name']}] got status {resp.status_code}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(site["product_url_pattern"])
    products = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pattern.search(href):
            continue
        name = a.get_text(strip=True)
        if not name or len(name) < 3:
            continue  # skip empty/icon links

        full_url = href if href.startswith("http") else site["url"].split("/", 3)[0] + "//" + site["url"].split("/")[2] + href

        # Walk up a few parent levels to get surrounding product-card text (price, stock status, etc.)
        container = a
        for _ in range(4):
            if container.parent:
                container = container.parent
        context_text = container.get_text(" ", strip=True)

        if any(kw in context_text for kw in site["out_of_stock_keywords"]):
            in_stock = False
        elif any(kw in context_text for kw in site["in_stock_keywords"]):
            in_stock = True
        else:
            in_stock = False  # unknown = treat as not confirmed in stock

        # Keep the longest name we've seen for this URL (avoids picking up stray short link text)
        if full_url not in products or len(name) > len(products[full_url]["name"]):
            products[full_url] = {"name": name, "in_stock": in_stock}

    return products


def check_site(site, state):
    name = site["name"]
    current = scan_site(site)
    if current is None:
        return

    is_first_run = name not in state  # no baseline yet for this site
    previous = state.get(name, {})

    for url, info in current.items():
        prev_info = previous.get(url)

        if is_first_run:
            continue  # just seed the baseline, don't alert on everything at once

        if prev_info is None:
            log.info(f"[{name}] NEW PRODUCT: {info['name']}")
            send_discord_alert("NEW POKÉMON PRODUCT", info["name"], url)
            time.sleep(1.5)  # avoid Discord rate limit (429)
        elif info["in_stock"] and not prev_info.get("in_stock", False):
            log.info(f"[{name}] RESTOCKED: {info['name']}")
            send_discord_alert("BACK IN STOCK", info["name"], url)
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
