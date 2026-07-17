import requests
from bs4 import BeautifulSoup
import time
import json
import os
import logging

# ---------------- CONFIG ----------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = "seen_state.json"

TARGETS = [
    {
        "name": "iHrysko - Pokemon TCG",
        "url": "https://www.ihrysko.sk/pokemon-tcg",
        "selector": ("button", {"class": "add-to-cart"}),
        "in_stock_text": "Vložiť do košíka",
    },
    # {
    #     "name": "AnotherShop - Product X",
    #     "url": "https://example.sk/product-x",
    #     "selector": ("button", {"class": "buy-button"}),
    #     "in_stock_text": "Kúpiť",
    # },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- STATE ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------- ALERTS ----------------
def send_discord_alert(product_name, link):
    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL not set — skipping alert send")
        return
    payload = {
        "content": f"🚨 **POKÉMON DROP ALERT!** 🚨\n**{product_name}** is back in stock!\n👉 {link}"
    }
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to send Discord alert: {e}")

# ---------------- CHECK LOGIC ----------------
def check_target(target, state):
    name, url = target["name"], target["url"]
    tag, attrs = target["selector"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        log.warning(f"[{name}] request failed: {e}")
        return

    if resp.status_code != 200:
        log.warning(f"[{name}] got status {resp.status_code} (blocked / rate-limited?)")
        return

    soup = BeautifulSoup(resp.text, "html.parser")
    element = soup.find(tag, attrs)

    in_stock = bool(element and target["in_stock_text"] in element.get_text())
    was_in_stock = state.get(url, {}).get("in_stock", False)

    if in_stock and not was_in_stock:
        log.info(f"[{name}] IN STOCK — sending alert")
        send_discord_alert(name, url)
    elif not in_stock:
        log.info(f"[{name}] sold out")
    else:
        log.info(f"[{name}] still in stock (already alerted)")

    state[url] = {"in_stock": in_stock, "last_checked": time.time()}

# ---------------- MAIN (single sweep) ----------------
def main():
    state = load_state()
    for target in TARGETS:
        check_target(target, state)
        time.sleep(2)
    save_state(state)
    log.info("Sweep complete.")

if __name__ == "__main__":
    main()
