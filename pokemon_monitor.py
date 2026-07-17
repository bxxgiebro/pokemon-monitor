import requests
from bs4 import BeautifulSoup
import re
import time
import json
import os
import logging
from urllib.parse import urljoin

# ---------------- CONFIGURATION ----------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "YOUR_DISCORD_WEBHOOK_HERE")
STATE_FILE = "seen_state.json"
CHECK_INTERVAL = 420  # Time between sweeps in seconds (7 minutes)

SITES = [
    {
        "name": "iHrysko",
        "url": "https://www.ihrysko.sk/pokemon-tcg-c17668",
        "product_url_pattern": r"-p\d+",
        "in_stock_keywords": ["Vložiť do košíka", "skladom", "skladom v eshope"],
        "out_of_stock_keywords": ["Očakávame", "dlhodobo nedostupné", "Vypredané", "Nedostupné"],
    },
    {
        "name": "CardEmpire",
        "url": "https://www.cardempire.sk/pokemon-karty/",
        "product_url_pattern": r"/produkt/[a-zA-Z0-9-]+",
        "in_stock_keywords": ["Kúpiť", "Skladom"],
        "out_of_stock_keywords": ["Vypredané", "Nedostupné", "vypredané"],
    },
    {
        "name": "VeselyDrak",
        "url": "https://www.vesely-drak.sk/produkty/pokemon-karty/",
        "product_url_pattern": r"/produkty/[^/]+/\d+-",
        "in_stock_keywords": ["Skladom", "Skladem", "Do košíka"],
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
        "url": "https://www.pgs.sk/pokemon-tcg-4c9937",
        "product_url_pattern": r"-4p\d+",
        "in_stock_keywords": ["Na sklade", "Do košíka"],
        "out_of_stock_keywords": ["Neznáma dostupnosť", "Na ceste", "Vypredané"],
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
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
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        # Prevents crashing on read-only or ephemeral cloud host filesystems
        log.warning(f"Disk write skipped (normal for ephemeral cloud hosting): {e}")


def send_discord_alert(title, product_name, link):
    if not DISCORD_WEBHOOK_URL or "YOUR_DISCORD_WEBHOOK" in DISCORD_WEBHOOK_URL:
        log.error("Valid DISCORD_WEBHOOK_URL not configured.")
        return
    payload = {"content": f"🚨 **{title}** 🚨\n**{product_name}**\n👉 {link}"}
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to send Discord alert: {e}")


def find_product_container(anchor_element):
    """Climbs upward to locate the bounding container of a product card safely."""
    current = anchor_element
    for _ in range(5):
        if not current.parent:
            break
        current = current.parent
        # Check if the parent class suggests it's a grid item, box, card, or product wrapper
        class_list = current.get("class", [])
        class_str = " ".join(class_list).lower() if class_list else ""
        if any(kw in class_str for kw in ["product", "item", "card", "grid", "block", "thumbnail"]):
            return current
    return anchor_element.parent  # Fallback if no explicit class found


def scan_site(site):
    try:
        resp = requests.get(site["url"], headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            log.warning(f"[{site['name']}] Received status code {resp.status_code}")
            return None
    except Exception as e:
        log.error(f"[{site['name']}] Connection failed: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(site["product_url_pattern"])
    products = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pattern.search(href):
            continue
            
        name = a.get_text(strip=True)
        if not name or len(name) < 5:
            continue  # Filters out empty image links or short navigational text

        # Fixed: Robust URL combining using urljoin
        full_url = urljoin(site["url"], href)

        # Dynamic container discovery to gather card context (prices, stock indicators)
        container = find_product_container(a)
        context_text = container.get_text(" ", strip=True)

        # Explicit stock logic validation
        if any(kw in context_text for kw in site["out_of_stock_keywords"]):
            in_stock = False
        elif any(kw in context_text for kw in site["in_stock_keywords"]):
            in_stock = True
        else:
            in_stock = False  # Safe default fallback

        # Store or update with the longest variant of the title found for accuracy
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
            continue  # Safely builds the inventory baseline without firing spam alerts on boot

        if prev_info is None:
            # Found a completely new item listed on the page
            if info["in_stock"]:
                log.info(f"[{name}] NEW PRODUCT IN STOCK: {info['name']}")
                send_discord_alert(f"NEW DROP - {name.upper()}", info["name"], url)
                time.sleep(2)
        elif info["in_stock"] and not prev_info.get("in_stock", False):
            # Item flipped from out of stock to in stock
            log.info(f"[{name}] RESTOCKED: {info['name']}")
            send_discord_alert(f"RESTOCK - {name.upper()}", info["name"], url)
            time.sleep(2)

    if is_first_run:
        log.info(f"[{name}] Baseline initialized with {len(current)} products. Monitoring active.")

    state[name] = current


def main():
    log.info("Initializing Hardened Pokémon Live Monitor...")
    state = load_state()
    
    while True:
        log.info("Starting site sweep...")
        for site in SITES:
            check_site(site, state)
            time.sleep(3)  # Anti-throttle delay between parsing separate stores
            
        save_state(state)
        log.info(f"Sweep complete. Sleeping for {CHECK_INTERVAL} seconds...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Monitor manually stopped.")
