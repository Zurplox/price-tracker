"""
💰 Price Tracker v2 — self-serve edition.
Reads products.json (which your dashboard page manages), finds prices automatically,
saves history, and pings Telegram on drops. Runs on GitHub Actions — no computer needed.

Price-fetching layers, in order (first success wins):
  1. Shopee item API shortcut (Shopee links only)
  2. Plain fast fetch + auto price detection
  3. Real browser engine (Playwright) for JS/bot-protected pages
  4. ScraperAPI proxy (only if you add the SCRAPER_API_KEY secret)
"""

import json
import os
import re
import urllib.parse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

PRODUCTS_FILE = "products.json"
PRICES_FILE = "prices.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY")  # optional backup layer

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_number(text):
    """Turn 'S$1,299.00' into 1299.0"""
    if text is None:
        return None
    m = re.search(r"\d[\d,]*(\.\d{1,2})?", str(text))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def make_id(url):
    m = re.search(r"i\.(\d+)\.(\d+)", url)
    if "shopee" in url and m:
        return "shopee-" + m.group(1) + "-" + m.group(2)
    host = re.sub(r"\W+", "", re.search(r"https?://([^/]+)", url).group(1) if "http" in url else "item")
    return host + "-" + format(abs(hash(url)) % 10**8, "x")


def currency_for(url):
    if re.search(r"\.(sg|com\.sg)(/|$)", url):
        return "S$"
    if re.search(r"\.my(/|$)", url):
        return "RM"
    return "$"


def fmt(price, url):
    return "?" if price is None else currency_for(url) + format(price, ",.2f")


# ---------- fetching ----------

def get_html(url):
    r = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r.text, r.url


def render_html(url):
    """Real-browser fetch for JavaScript-heavy, bot-protected pages (Shopee & friends)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  (playwright not installed — skipping browser fallback)")
        return None, url
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(user_agent=HEADERS["User-Agent"],
                                    viewport={"width": 1366, "height": 768})
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)  # let prices render
            html, final_url = page.content(), page.url
            browser.close()
        return html, final_url
    except Exception as e:
        print("  (browser fetch failed: " + str(e) + ")")
        return None, url


def fetch_via_scraperapi(url):
    """Last-resort fetch through the ScraperAPI proxy (needs SCRAPER_API_KEY secret).
    render=true runs the page in a real browser on their end (costs ~10 credits)."""
    if not SCRAPER_API_KEY:
        return None
    try:
        api = ("https://api.scraperapi.com/?api_key=" + SCRAPER_API_KEY
               + "&render=true&url=" + urllib.parse.quote(url, safe=""))
        r = requests.get(api, timeout=90)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print("  (scraperapi fetch failed: " + str(e) + ")")
        return None


# ---------- price extraction ----------

def find_price_in_ld(node):
    if isinstance(node, dict):
        offers = node.get("offers")
        if offers:
            if isinstance(offers, dict):
                offers = [offers]
            for o in offers:
                if isinstance(o, dict):
                    for key in ("price", "lowPrice"):
                        p = parse_number(o.get(key))
                        if p:
                            return p
        for v in node.values():
            p = find_price_in_ld(v)
            if p:
                return p
    elif isinstance(node, list):
        for item in node:
            p = find_price_in_ld(item)
            if p:
                return p
    return None


def extract_generic(html):
    """Find a price using structured data most shops embed. Returns (price, method, title)."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else None

    # 1) JSON-LD structured data (most e-commerce sites include this)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        price = find_price_in_ld(data)
        if price:
            return price, "auto (structured data)", title

    # 2) Meta tags
    for attrs in ({"property": "product:price:amount"}, {"property": "og:price:amount"},
                  {"itemprop": "price"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            price = parse_number(tag["content"])
            if price:
                return price, "auto (meta tag)", title

    # 3) Price-ish page elements — prefer the shortest text (deepest element),
    #    skip crossed-out "was" prices.
    def is_pricey(tag):
        if tag.name in ("script", "style", "svg"):
            return False
        ident = " ".join(str(c) for c in (tag.get("class") or [])) + " " + str(tag.get("id") or "")
        if "price" not in ident.lower() and not tag.has_attr("data-price") and tag.get("itemprop") != "price":
            return False
        if re.search(r"was|old|compare|strike|rrp|original|retail", ident, re.I):
            return False
        if "line-through" in str(tag.get("style") or ""):
            return False
        return True

    candidates = []
    for el in soup.find_all(is_pricey):
        value = el.get("content") or el.get("data-price") or el.get_text(" ", strip=True)
        price = parse_number(value)
        if price and len(str(value)) < 30:
            candidates.append((len(str(value)), price))
    if candidates:
        candidates.sort()
        return candidates[0][1], "auto (page element)", title

    return None, None, title


# ---------- shopee shortcut ----------

def shopee_ids(url):
    m = re.search(r"i\.(\d+)\.(\d+)", url)
    return (m.group(1), m.group(2)) if m else (None, None)


def shopee_api_price(shopid, itemid):
    api = "https://shopee.sg/api/v4/item/get?itemid=" + str(itemid) + "&shopid=" + str(shopid)
    r = requests.get(api, headers={**HEADERS, "Referer": "https://shopee.sg/"}, timeout=30)
    data = r.json().get("data")
    if not data:
        return None, None
    raw = data.get("price") or data.get("price_min")
    price = raw / 100000 if raw else None
    return price, data.get("name")


# ---------- per-product check ----------

def apply_extraction(html, product):
    """Run selector (if set) then auto-detection on a fetched page."""
    if product.get("selector"):
        el = BeautifulSoup(html, "lxml").select_one(product["selector"])
        price = parse_number(el.get_text(" ", strip=True)) if el else None
        if price:
            return price, "manual selector", None
    return extract_generic(html)


def check_product(p):
    url = p["url"]
    html, final_url = None, url
    shopid, itemid = shopee_ids(url)

    # Resolve short links (shope.ee / lzd.co etc.)
    if not shopid and re.match(r"https?://(shope\.ee|s\.shopee|www\.lazada|lzd)", url):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
            final_url = r.url
            shopid, itemid = shopee_ids(final_url)
        except Exception:
            pass

    # Layer 1 — Shopee item API: much cheaper than a full browser
    if shopid and itemid:
        try:
            price, name = shopee_api_price(shopid, itemid)
            if price:
                return price, "auto (shopee api)", name
        except Exception as e:
            print("  (shopee api blocked: " + str(e) + ")")

    # Layer 2 — fast plain fetch
    try:
        html, final_url = get_html(final_url)
    except Exception as e:
        print("  (plain fetch failed: " + str(e) + ")")

    price = method = title = None
    if html:
        price, method, title = apply_extraction(html, p)

    # Layer 3 — real browser for JS-heavy / protected pages
    if not price:
        html2, final_url = render_html(final_url)
        if html2:
            price, method, title = apply_extraction(html2, p)
            if method:
                method += " (browser)"

    # Layer 4 — ScraperAPI proxy (only when SCRAPER_API_KEY secret exists)
    if not price and SCRAPER_API_KEY:
        html3 = fetch_via_scraperapi(final_url)
        if html3:
            price, method, title = apply_extraction(html3, p)
            if method:
                method += " (scraperapi)"

    return price, method, title


# ---------- telegram ----------

def md_escape(text):
    """Escape Telegram legacy-Markdown special characters in dynamic text."""
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secrets not set — would have sent:\n" + message)
        return
    api = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    try:
        requests.post(api, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=30)
    except Exception as e:
        print("⚠️ Telegram send failed: " + str(e))


# ---------- main ----------

def main():
    data = load_json(PRODUCTS_FILE, {"products": []})
    products = data.get("products", data) if isinstance(data, dict) else data
    prices = load_json(PRICES_FILE, {})
    alerts = []

    for p in products:
        if p.get("paused"):
            continue
        p.setdefault("id", make_id(p["url"]))
        name = p.get("name") or p["url"]
        print("\n🔎 " + name)

        try:
            price, method, title = check_product(p)
        except Exception as e:
            p["status"] = "error: " + str(e)
            p["last_checked"] = now_iso()
            print("  ⚠️ " + str(e))
            continue

        p["last_checked"] = now_iso()

        if not price:
            p["status"] = "needs attention — couldn't find a price (try adding a selector in Advanced)"
            print("  ⚠️ no price found")
            continue

        prev = p.get("last_price")
        if title and (not p.get("name") or p["name"].lower().startswith(("http", "product"))):
            p["name"] = title[:80]
        p.update(status="tracking", method=method, last_price=price)

        hist = prices.setdefault(p["id"], [])
        hist.append({"t": p["last_checked"], "p": price})
        prices[p["id"]] = hist[-500:]

        target = p.get("target")
        if target and price <= target and (prev is None or prev > target):
            alerts.append("🎯 *TARGET HIT — " + md_escape(p["name"]) + "*\n"
                          "Now *" + fmt(price, p["url"]) + "* (target " + fmt(target, p["url"]) + ")\n"
                          "[🛒 View Product](" + p["url"] + ")")
        elif prev is not None and price < prev:
            alerts.append("📉 *PRICE DROP — " + md_escape(p["name"]) + "*\n"
                          "Was " + fmt(prev, p["url"]) + " → Now *" + fmt(price, p["url"]) + "*\n"
                          "[🛒 View Product](" + p["url"] + ")")
        print("  ✅ " + fmt(price, p["url"]) + " via " + str(method))

    save_json(PRODUCTS_FILE, {"products": products} if isinstance(data, dict) else products)
    save_json(PRICES_FILE, prices)

    if alerts:
        send_telegram("💰 *Price alert!*\n\n" + "\n\n".join(alerts))
    print("\nDone — " + str(len(products)) + " product(s), " + str(len(alerts)) + " alert(s).")


if __name__ == "__main__":
    main()
