import requests
from bs4 import BeautifulSoup
import re
import time
import json
import os
import logging
import math
from urllib.parse import urljoin, urlsplit

# ---------------- CONFIG ----------------

SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "")
SCRAPINGBEE_KEY = os.environ.get("SCRAPINGBEE_KEY", "")
ALZA_ENABLED = os.environ.get("ALZA_ENABLED", "false").lower() == "true"
# One Alza page per sweep. A session must be manually started from GitHub Actions.
ALZA_CHECK_INTERVAL_SECONDS = max(600, int(os.environ.get("ALZA_CHECK_INTERVAL_SECONDS") or "600"))
ALZA_TARGET_URL = (os.environ.get("ALZA_TARGET_URL") or
                   "https://www.alza.sk/hracky/pokemon-karty/18879069.htm")
ALZA_START_SESSION = os.environ.get("ALZA_START_SESSION", "false").lower() == "true"
ALZA_SESSION_SECONDS = 12 * 60 * 60
# Free account: 1000 credits/month. Reserve at least 100 for earlier/other requests.
ALZA_CREDIT_SAFETY_RESERVE = 100  # Stop when fewer than 110 free credits remain.
ALZA_MAX_COST = 10  # ScraperAPI's max_cost prevents a more expensive scrape.
ALZA_SESSION_BUDGET = 750  # Worst-case allowance for a 12h session (~72 * 10).

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
        "in_stock_keywords": ["Skladom", "Do košíka"],
        "out_of_stock_keywords": ["Produkt nie je skladom", "Nedostupné"],
        "scanner": "dracik",
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
        "name": "Brloh",
        "url": "https://www.brloh.sk/pokemon-c1781",
        "product_url_pattern": r"-p\d+",
        "in_stock_keywords": ["Vložiť do košíka", "skladom"],
        "out_of_stock_keywords": ["Očakávame", "dlhodobo nedostupné", "Vypredané"],
    },
    {
        "name": "iHrysko",
        "url": "https://www.ihrysko.sk/pokemon-tcg-c17668",
        "product_url_pattern": r"-p\d+",
        "in_stock_keywords": ["Vložiť do košíka", "skladom"],
        "out_of_stock_keywords": ["Očakávame", "dlhodobo nedostupné", "Vypredané"],
    },
]

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
        return False
    payload = {"content": f"🚨 **{title}** 🚨\n**{product_name}**\n👉 {link}"}
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error(f"Failed to send Discord alert: {e}")
        return False


def find_product_container(anchor_element):
    current = anchor_element
    for _ in range(8):
        if not current.parent:
            break
        current = current.parent
        if current.find(["h2", "h3"]):
            return current
    return anchor_element.parent


def fetch_page(site):
    """Handles plain requests OR proxy (ScraperAPI first, ScrapingBee fallback)."""
    if site.get("use_proxy"):
        resp = None
        if SCRAPERAPI_KEY:
            try:
                proxy_url = f"http://api.scraperapi.com?api_key={SCRAPERAPI_KEY}&url={site['url']}"
                resp = requests.get(proxy_url, timeout=30)
                if resp.status_code != 200:
                    resp = None
            except requests.RequestException:
                resp = None
        if resp is None and SCRAPINGBEE_KEY:
            log.info(f"[{site['name']}] ScraperAPI unavailable, trying ScrapingBee")
            try:
                proxy_url = f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPINGBEE_KEY}&url={site['url']}"
                resp = requests.get(proxy_url, timeout=30)
            except requests.RequestException:
                resp = None
        if resp is None:
            log.warning(f"[{site['name']}] both proxies unavailable")
            return None
    else:
        try:
            resp = requests.get(site["url"], headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            log.error(f"[{site['name']}] request failed: {e}")
            return None

    if resp.status_code != 200:
        log.warning(f"[{site['name']}] got status {resp.status_code}")
        return None
    return resp


def scan_dracik(site):
    """Dracik has no clean product-vs-category URL pattern, but every real
    product tile has a 'Do kosika' button linking to /basket/add/?product_id=N.
    Nav/category links never have this, so we anchor on that instead."""
    dracik_headers = {**HEADERS, "Referer": "https://www.dracik.sk/",
                       "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                       "Sec-Fetch-Site": "same-origin", "Upgrade-Insecure-Requests": "1"}
    try:
        resp = requests.get(site["url"], headers=dracik_headers, timeout=20)
        if resp.status_code != 200:
            log.warning(f"[{site['name']}] got status {resp.status_code}")
            return None
    except requests.RequestException as e:
        log.error(f"[{site['name']}] request failed: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    basket_pattern = re.compile(r"/basket/add/\?product_id=(\d+)")
    products = {}

    for a in soup.find_all("a", href=True):
        if not basket_pattern.search(a["href"]):
            continue

        container = a
        img = None
        for _ in range(6):
            if not container.parent:
                break
            container = container.parent
            img = container.find("img")
            if img:
                break

        name = img.get("alt", "").strip() if img else ""
        if not name or len(name) < 3:
            continue

        detail_href = None
        for link in container.find_all("a", href=True):
            href = link["href"]
            if "/basket/add/" in href or "favoriteproducts" in href:
                continue
            detail_href = href
            break
        if not detail_href:
            continue

        full_url = urljoin(site["url"], detail_href)
        context_text = container.get_text(" ", strip=True)

        if any(kw in context_text for kw in site["out_of_stock_keywords"]):
            in_stock = False
        elif any(kw in context_text for kw in site["in_stock_keywords"]):
            in_stock = True
        else:
            in_stock = False

        if full_url not in products or len(name) > len(products[full_url]["name"]):
            products[full_url] = {"name": name, "in_stock": in_stock}

    return products


def scan_site(site):
    """Generic scanner used by every site except Dracik (which has its own)."""
    resp = fetch_page(site)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(site["product_url_pattern"])
    products = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pattern.search(href):
            continue

        full_url = urljoin(site["url"], href).split("?")[0]
        container = find_product_container(a)

        anchor_text = a.get_text(strip=True)
        if anchor_text and len(anchor_text) >= 5:
            name = anchor_text
        else:
            heading = container.find(["h2", "h3"])
            name = heading.get_text(strip=True) if heading else ""
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

# ---------------- ALZA (separate scanner) ----------------

ALZA_PRODUCT_RE = re.compile(r"-d(\d+)\.htm(?:[?#]|$)", re.I)
ALZA_BAD_TITLE_RE = re.compile(
    r"(?:^pok[eé]mon\s+up\b|album|binder|deck protector|obaly? na karty|"
    r"hracia podlo[zž]ka|podlo[zž]ka pod karty|leuchtturm|ultra\s*pro|"
    r"ply[sš]|fig[uú]rk|p[uú]zdro|\bsleeves\b)", re.I
)
ALZA_UNAVAILABLE_RE = re.compile(
    r"moment[aá]lne nedostupn[eé]|nie je skladom|vypredan[eé]|\bstr[aá][zž]i[tť]\b|\bdopyt\b|predobjedn[aá]vka|o[cč]ak[aá]vame",
    re.I,
)
ALZA_AVAILABLE_RE = re.compile(r"na sklade|do ko[sš][ií]ka", re.I)
ALZA_BLOCK_RE = re.compile(
    r"(?:captcha|access denied|verify you are human|cloudflare challenge|just a moment)", re.I
)


def alza_id(url):
    match = ALZA_PRODUCT_RE.search(url)
    return match.group(1) if match else None



def alza_credit_balance():
    """Read remaining ScraperAPI credits, using the actual /account response.

    Protect the free tier: never scrape when a usable credit balance is missing.
    """
    if not SCRAPERAPI_KEY:
        log.error("[Alza] SCRAPERAPI_KEY is missing")
        return None
    try:
        response = requests.get(
            "https://api.scraperapi.com/account",
            params={"api_key": SCRAPERAPI_KEY}, timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected account response type")

        # ScraperAPI actually returns 'creditsLeft' for this account.
        raw_balance = data.get("creditsLeft")
        if raw_balance is None:
            log.error("[Alza] Account response lacks creditsLeft. Keys: %s", list(data))
            return None
        credits_left = float(raw_balance)
        if not math.isfinite(credits_left) or credits_left < 0:
            raise ValueError("Invalid creditsLeft value")

        # requestCount/requestLimit are request counts, not credit counts.
        # They are a second guard only; don't substitute them for creditsLeft.
        request_count, request_limit = data.get("requestCount"), data.get("requestLimit")
        if request_count is not None and request_limit is not None:
            if float(request_count) >= float(request_limit):
                log.warning("[Alza] ScraperAPI request limit reached (%s/%s)",
                            request_count, request_limit)
                return 0.0

        log.info("[Alza] ScraperAPI account reports %.0f credits left", credits_left)
        return credits_left
    except (requests.RequestException, ValueError, TypeError) as exc:
        log.error("[Alza] Cannot check ScraperAPI balance: %s", exc)
        return None


def fetch_alza(url):
    """Exactly one ScraperAPI scrape, no fallback, render or premium proxy."""
    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": url,
        "max_cost": str(ALZA_MAX_COST),
        # render / premium deliberately omitted to protect free credits.
    }
    try:
        response = requests.get("https://api.scraperapi.com", params=params, timeout=70)
        log.info("[Alza] ScraperAPI HTTP %s, %d bytes, sa-credit-cost=%s",
                 response.status_code, len(response.content),
                 response.headers.get("sa-credit-cost", "not supplied"))
        if response.status_code == 403:
            log.error("[Alza] 403 (blocked, or scrape exceeds max_cost=%d).", ALZA_MAX_COST)
            return None
        if response.status_code != 200:
            log.error("[Alza] ScraperAPI HTTP %s: %s", response.status_code,
                      response.text[:220].replace("\n", " "))
            return None
        if (len(response.content) < 1000 or
                (not ALZA_PRODUCT_RE.search(response.text) and
                 ALZA_BLOCK_RE.search(response.text[:10000]))):
            log.warning("[Alza] Short/blocked HTML; preserving existing product state")
            return None
        return response.text
    except requests.RequestException as exc:
        log.error("[Alza] Request failed: %s", exc)
        return None

def alza_tile(anchor, product_id):
    """Find a tile with one distinct product ID, avoiding whole-page stock text."""
    fallback = None
    for ancestor in list(anchor.parents)[:12]:
        if ancestor.name in {"html", "body"}:
            break
        text = ancestor.get_text(" ", strip=True)
        if len(text) > 5000:
            break
        ids = {pid for a in ancestor.find_all("a", href=True)
               if (pid := alza_id(a.get("href", "")))}
        if ids != {product_id}:
            continue
        if fallback is None:
            fallback = ancestor
        # Stock and price normally live together on the same product tile.
        if ("€" in text or ALZA_UNAVAILABLE_RE.search(text) or
                ALZA_AVAILABLE_RE.search(text)):
            return ancestor
    return fallback


def alza_stock(tile):
    if tile is None:
        return None
    text = tile.get_text(" ", strip=True)
    if ALZA_UNAVAILABLE_RE.search(text):
        return False
    if ALZA_AVAILABLE_RE.search(text):
        return True
    # Unknown is not equivalent to out of stock.
    return None


def parse_alza(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    products = {}
    for anchor in soup.select("a[href]"):
        full_url = urljoin(page_url, anchor.get("href", ""))
        host = urlsplit(full_url).netloc.lower()
        if not (host == "alza.sk" or host.endswith(".alza.sk")):
            continue
        product_id = alza_id(full_url)
        if not product_id:
            continue
        tile = alza_tile(anchor, product_id)
        if tile is None:
            continue

        # Prefer linked product titles, then headings, then image alt fallbacks.
        anchor_titles = [anchor.get("title", ""), anchor.get_text(" ", strip=True)]
        for link in tile.find_all("a", href=True):
            if alza_id(link.get("href", "")) == product_id:
                anchor_titles.extend([link.get("title", ""), link.get_text(" ", strip=True)])
        heading_titles = [h.get_text(" ", strip=True) for h in tile.find_all(["h2", "h3", "h4"])]
        img_titles = [i.get("alt", "") for i in tile.find_all("img", alt=True)]
        def valid_titles(items):
            candidates = [" ".join(s.split()) for s in items if s]
            return [s for s in candidates if 7 <= len(s) <= 150 and
                    re.search(r"pok[eé]mon", s, re.I) and not ALZA_BAD_TITLE_RE.search(s)]
        titles = valid_titles(anchor_titles) or valid_titles(heading_titles) or valid_titles(img_titles)
        if not titles:
            continue
        name = max(titles, key=len)
        canonical_url = full_url.split("?", 1)[0].split("#", 1)[0]
        info = {"name": name, "in_stock": alza_stock(tile)}
        if product_id not in products:
            products[product_id] = (canonical_url, info)
        else:
            prev_url, prev = products[product_id]
            # A known true availability wins over unknown/false across duplicates.
            best_stock = True if (prev["in_stock"] is True or info["in_stock"] is True) else (
                False if (prev["in_stock"] is False or info["in_stock"] is False) else None)
            best_name = name if len(name) > len(prev["name"]) else prev["name"]
            products[product_id] = (prev_url, {"name": best_name, "in_stock": best_stock})
    return products


def check_alza(state):
    if not ALZA_ENABLED:
        log.info("[Alza] Disabled. Set ALZA_ENABLED=true in repository Variables.")
        return

    url_parts = urlsplit(ALZA_TARGET_URL)
    if (url_parts.scheme != "https" or url_parts.hostname not in
            {"www.alza.sk", "alza.sk"}):
        log.error("[Alza] ALZA_TARGET_URL must be an https://alza.sk/... URL")
        return

    now = time.time()
    if ALZA_START_SESSION:
        state["_alza_session"] = {
            "started_at": now,
            "ends_at": now + ALZA_SESSION_SECONDS,
            "reserved_credits": 0,
            "requests": 0,
            "target_url": ALZA_TARGET_URL,
            "last_attempt": 0,
        }
        log.info("[Alza] Started new 12-hour session (one page every 10 minutes)")

    session = state.get("_alza_session")
    if not session:
        log.info("[Alza] No active session. Start one using Actions > Run workflow.")
        return
    if session.get("target_url") != ALZA_TARGET_URL:
        log.warning("[Alza] Target changed. Start a new session to monitor this URL.")
        return
    if now >= session.get("ends_at", 0):
        log.info("[Alza] 12-hour session ended. No more Alza requests.")
        return
    since_last = now - session.get("last_attempt", 0)
    if since_last < ALZA_CHECK_INTERVAL_SECONDS:
        log.info("[Alza] Throttled; last attempt %.0f seconds ago", since_last)
        return
    if session.get("reserved_credits", 0) + ALZA_MAX_COST > ALZA_SESSION_BUDGET:
        log.warning("[Alza] Session credit reservation reached %d; stopping.",
                    ALZA_SESSION_BUDGET)
        return

    # Fail closed rather than spend credits we cannot verify are available.
    credits_left = alza_credit_balance()
    if credits_left is None:
        return
    if credits_left < ALZA_MAX_COST + ALZA_CREDIT_SAFETY_RESERVE:
        log.warning("[Alza] Only %.0f credits left; retaining %d-credit safety reserve",
                    credits_left, ALZA_CREDIT_SAFETY_RESERVE)
        return

    # Reserve one worst-case 10-credit request before attempting to fetch.
    # The committed state persists this reservation between Actions runs.
    session["last_attempt"] = now
    session["requests"] = session.get("requests", 0) + 1
    session["reserved_credits"] = session.get("reserved_credits", 0) + ALZA_MAX_COST
    html = fetch_alza(ALZA_TARGET_URL)
    if html is None:
        return
    current = parse_alza(html, ALZA_TARGET_URL)
    log.info("[Alza] Extracted %d product listings from %s", len(current), ALZA_TARGET_URL)
    if not current:
        log.error("[Alza] No product cards parsed. Preserving stock state.")
        return

    previous = state.get("Alza", {})
    previous_by_id = {alza_id(url): (url, dict(info)) for url, info in previous.items()
                      if alza_id(url)}
    # One-time parser migration / new target: first successful page is baseline.
    seed_only = (not state.get("_alza_parser_v2") or
                 state.get("_alza_last_target_url") != ALZA_TARGET_URL)
    if seed_only:
        log.info("[Alza] Establishing baseline; next scan will send new/restock alerts")

    saved = dict(previous)
    sent_new = sent_restock = 0
    for pid, (url, info) in current.items():
        match = previous_by_id.get(pid)
        if match:
            old_url, old = match
            old_stock = old.get("in_stock")
            if not seed_only and old_stock is False and info["in_stock"] is True:
                if not send_discord_alert("Alza", "@everyone RESTOCK - ALZA", info["name"], url):
                    continue  # Keep old out-of-stock status to retry next scan.
                sent_restock += 1
                time.sleep(1.2)
            saved[old_url] = {
                "name": info["name"],
                "in_stock": info["in_stock"] if info["in_stock"] is not None else old_stock,
            }
        else:
            if not seed_only:
                if not send_discord_alert("Alza", "@everyone NEW PRODUCT - ALZA", info["name"], url):
                    continue  # Retry if the webhook is temporarily unavailable.
                sent_new += 1
                time.sleep(1.2)
            saved[url] = info

    # Historical entries are kept so temporarily disappearing products don't
    # get falsely reported as 'new' when they return to the first page.
    state["Alza"] = saved
    state["_alza_parser_v2"] = True
    state["_alza_last_target_url"] = ALZA_TARGET_URL
    state["_alza_last_check"] = now
    log.info("[Alza] %d products on page, %d historical, %d new alerts, %d restock alerts",
             len(current), len(saved), sent_new, sent_restock)

def run_scanner(site):
    if site.get("scanner") == "dracik":
        return scan_dracik(site)
    return scan_site(site)


def check_site(site, state):
    name = site["name"]
    current = run_scanner(site)
    if current is None or len(current) == 0:
        log.warning(f"[{name}] got 0 products, skipping state update to avoid wiping saved data")
        return

    previous = state.get(name, {})
    previous_count = len(previous)
    if previous_count and len(current) > previous_count * 3 and len(current) > 20:
        log.warning(f"[{name}] got {len(current)} products vs {previous_count} before — "
                    f"looks like a scraping fluke, skipping this run without alerting")
        return

    is_first_run = name not in state or not state[name]

    for url, info in current.items():
        prev_info = previous.get(url)

        if is_first_run:
            continue

        if prev_info is None:
            log.info(f"[{name}] NEW PRODUCT: {info['name']}")
            send_discord_alert(name, f"@everyone NEW PRODUCT - {name.upper()}", info["name"], url)
            time.sleep(1.5)
        elif info["in_stock"] and not prev_info.get("in_stock", False):
            log.info(f"[{name}] RESTOCKED: {info['name']}")
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
    check_alza(state)
    save_state(state)
    log.info("Sweep complete.")


if __name__ == "__main__":
    main()

