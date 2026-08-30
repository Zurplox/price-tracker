"""
💰 Price Tracker v3 — self-serve edition with pick-your-price scanning.
Reads products.json (managed by your dashboard page), finds prices automatically,
saves history, and pings Telegram on drops. Runs on GitHub Actions — no computer needed.

Price-fetching layers, in order (first success wins):
  1. Shopee item API shortcut (Shopee links only)
  2. Plain fast fetch + price detection
  3. Real browser engine (Playwright) for JS/bot-protected pages
  4. ScraperAPI proxy (only if you add the SCRAPER_API_KEY secret)

Per-product options:
  "mode": "lowest"  → track the LOWEST price on the page (hotels, flights, multi-option)
  "watch_label"     → track the ONE price the user ticked after a scan, re-found by
                      its label each run (never silently switches to a different option)
  "selector"        → manual CSS selector escape hatch
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
SERPAPI_KEY = os.environ.get("SERPAPI_KEY")  # optional — enables flight tracking via Google Flights

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MONEY_RE = re.compile(r"^(?:S\$|SGD|RM|MYR|US\$|USD|\$)\s?[\d,]+(?:\.\d{1,2})?$")
BAD_URL_RE = re.compile(r"verify|punish|_____tmd_____|captcha|robot|denied", re.I)
STRIKE_RE = re.compile(r"was|old|compare|strike|rrp|original|retail|line-through", re.I)
NOISE_RE = re.compile(r"fee|tax|charge|deposit|service|handling|points|miles|interest|donat|tip|gratuity", re.I)
GOOD_PRICE_RE = re.compile(r"current|sale|selling|final|main|pdp|buy|offer|product|amount|total", re.I)


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
    if url.startswith(("flight:", "hotel:")):
        return "S$"
    if re.search(r"\.(sg|com\.sg)(/|$)", url):
        return "S$"
    if re.search(r"\.my(/|$)", url):
        return "RM"
    return "$"


def fmt(price, url):
    return "?" if price is None else currency_for(url) + format(price, ",.2f")


def is_bad_url(url):
    """True if the URL is a captcha/anti-bot page, not the real product page."""
    return bool(url) and bool(BAD_URL_RE.search(url))


def tracked_key(p):
    """What exactly is being tracked — auto, lowest mode, a selector, or a user-picked
    label. When this changes, the price history restarts fresh (day 1 semantics)."""
    return p.get("watch_label") or p.get("selector") or p.get("mode") or "auto"


def history_reset_needed(p):
    """Reset history only when the tracked target genuinely CHANGED (a stored key
    existed and differs now). First run after an upgrade (no key stored yet) adopts
    silently, so existing history is never wiped."""
    old = p.get("_tracked_key")
    return old is not None and old != tracked_key(p)


# ---------- flight tracking (SerpApi Google Flights) ----------

FLIGHT_RE = re.compile(r"^flight:([A-Za-z]{3})-([A-Za-z]{3})/(\d{4}-\d{2}-\d{2})(?:/(\d{4}-\d{2}-\d{2}))?(?:/(\d+))?$")


def parse_flight_spec(url):
    """Parse flight:SIN-SYD/2026-08-24[/2026-08-31][/2] into a spec dict, or None."""
    m = FLIGHT_RE.match(url.strip())
    if not m:
        return None
    dep, arr, out, ret, adults = m.groups()
    return {"dep": dep.upper(), "arr": arr.upper(), "out": out, "ret": ret,
            "adults": int(adults) if adults else 1,
            "type": "1" if ret else "2"}


def flight_title(spec):
    dates = spec["out"][5:].replace("-", "/") + (" \u2192 " + spec["ret"][5:].replace("-", "/") if spec["ret"] else "")
    return spec["dep"] + " \u21c4 " + spec["arr"] + " \u00b7 " + dates


def flight_link(url):
    """Human-openable Google Flights link for a flight: spec (used in card + alerts)."""
    spec = parse_flight_spec(url)
    if not spec:
        return url
    q = "Flights from " + spec["dep"] + " to " + spec["arr"] + " on " + spec["out"]
    if spec["ret"]:
        q += " through " + spec["ret"]
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def lowest_flight_price(data):
    """Min price across best_flights + other_flights in a SerpApi google_flights result."""
    found = [f["price"] for group in ("best_flights", "other_flights")
             for f in data.get(group, []) if isinstance(f.get("price"), (int, float))]
    return float(min(found)) if found else None


def serpapi_flight_price(spec):
    """Lowest current Google Flights fare for the spec. Needs the SERPAPI_KEY secret."""
    if not SERPAPI_KEY:
        print("  (no SERPAPI_KEY secret — add it in repo Settings → Secrets → Actions)")
        return None
    params = {"engine": "google_flights", "api_key": SERPAPI_KEY,
              "departure_id": spec["dep"], "arrival_id": spec["arr"],
              "outbound_date": spec["out"], "type": spec["type"],
              "adults": spec["adults"], "currency": "SGD", "hl": "en", "gl": "sg"}
    if spec["ret"]:
        params["return_date"] = spec["ret"]
    r = requests.get("https://serpapi.com/search", params=params, timeout=60)
    r.raise_for_status()
    return lowest_flight_price(r.json())


def serpapi_flight_offers(url, limit=10):
    """Per-airline fare breakdown for a flight: spec - returns a LIST of
    {store, title, price, link, same_store} rows, one per airline (group),
    cheapest first. 'store' carries the airline name(s) so the dashboard's
    market panel can render flight rows unchanged.

    DIAGNOSTIC ONLY. Tracking still records the single cheapest fare via
    serpapi_flight_price; nothing here enters price history or graphs.
    """
    spec = parse_flight_spec(url)
    if not spec or not SERPAPI_KEY:
        if not SERPAPI_KEY:
            print("  (no SERPAPI_KEY secret - cannot fetch airline fares)")
        return []
    params = {"engine": "google_flights", "api_key": SERPAPI_KEY,
              "departure_id": spec["dep"], "arrival_id": spec["arr"],
              "outbound_date": spec["out"], "type": spec["type"],
              "adults": spec["adults"], "currency": "SGD", "hl": "en", "gl": "sg"}
    if spec["ret"]:
        params["return_date"] = spec["ret"]
    r = requests.get("https://serpapi.com/search", params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    link = flight_link(url)
    best = {}
    for group in ("best_flights", "other_flights"):
        for opt in data.get(group, []) or []:
            price = opt.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            legs = opt.get("flights") or []
            airlines = []
            for leg in legs:
                a = (leg.get("airline") or "").strip()
                if a and a not in airlines:
                    airlines.append(a)
            store = " + ".join(airlines) if airlines else "Unknown airline"
            stops = max(len(legs) - 1, 0)
            mins = opt.get("total_duration")
            bits = ["nonstop" if stops == 0 else str(stops) + " stop" + ("s" if stops > 1 else "")]
            if isinstance(mins, (int, float)) and mins:
                bits.append(str(int(mins) // 60) + "h " + str(int(mins) % 60).zfill(2) + "m")
            row = {"store": store[:40], "title": " · ".join(bits)[:90],
                   "price": float(price), "link": link, "same_store": False}
            prev = best.get(store)
            if prev is None or price < prev["price"]:
                best[store] = row
    return sorted(best.values(), key=lambda o: o["price"])[:limit]


def serpapi_shopping_offers(name, url, limit=10):
    """Market snapshot via Google Shopping — returns a LIST of other sellers' offers,
    each with its own link so you can verify it yourself.

    DIAGNOSTIC ONLY. Nothing here is ever returned to check_product and nothing here
    can enter price history. A shopping result is almost always a DIFFERENT seller:
    the Vention powerbank proved it, reading S$11.80 from a random store while the
    tracked Shopee link said $24.99. Tracking a link only means something if the
    number comes FROM that link.
    """
    if not SERPAPI_KEY:
        print("  (no SERPAPI_KEY secret - cannot fetch market offers)")
        return []
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    domain = (m.group(1) if m else "").lower()
    stem = domain.split(".")[0] if domain else ""
    r = requests.get("https://serpapi.com/search", timeout=60, params={
        "engine": "google_shopping", "api_key": SERPAPI_KEY,
        "q": name, "gl": "sg", "hl": "en"})
    r.raise_for_status()
    out = []
    for it in r.json().get("shopping_results", []):
        price = it.get("extracted_price")
        if not isinstance(price, (int, float)) or price <= 0:
            continue
        link = it.get("product_link") or it.get("link") or ""
        store = (it.get("source") or "seller").strip()
        out.append({
            "store": store[:40],
            "title": (it.get("title") or "").strip()[:90],
            "price": float(price),
            "link": link,
            "same_store": bool(stem and stem in (link + " " + store).lower()),
        })
    seen, deduped = set(), []
    for o in sorted(out, key=lambda o: o["price"]):   # cheapest first
        key = (o["store"], o["price"])
        if key not in seen:
            seen.add(key)
            deduped.append(o)
    return deduped[:limit]


def _norm_name(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower())


HOTEL_RE = re.compile(r"^hotel:([^/]+)/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})(?:/(\d+))?$")


def parse_hotel_spec(url):
    """Parse hotel:HOTEL NAME/CHECK-IN/CHECK-OUT[/ADULTS] into a spec dict, or None."""
    m = HOTEL_RE.match(url.strip())
    if not m:
        return None
    name, cin, cout, adults = m.groups()
    if cout <= cin:
        return None
    return {"name": name.strip(), "in": cin, "out": cout,
            "adults": int(adults) if adults else 2}


def hotel_title(spec):
    return spec["name"] + " · " + spec["in"][5:].replace("-", "/") + " → " + spec["out"][5:].replace("-", "/")


def hotel_link(url):
    """Human-openable Google Hotels link for a hotel: spec (used in card + alerts)."""
    spec = parse_hotel_spec(url)
    if not spec:
        return url
    return "https://www.google.com/travel/hotels?q=" + urllib.parse.quote(spec["name"])


def pick_hotel_price(properties, name):
    """Lowest nightly rate of the property whose name best matches the tracked name.
    None when nothing matches — an honest miss, never a random hotel's price."""
    want = _norm_name(name)
    tokens = [t for t in want.split() if len(t) > 2]
    best = None
    best_score = 0
    for prop in properties:
        pname = _norm_name(prop.get("name"))
        if not pname or not tokens:
            continue
        score = sum(1 for t in tokens if t in pname)
        if score == 0:
            continue
        rate = prop.get("rate_per_night") or {}
        p = rate.get("lowest_extracted")
        if not isinstance(p, (int, float)):
            continue
        if best is None or score > best_score or (score == best_score and p < best):
            best, best_score = float(p), score
    return best


def serpapi_hotel_price(spec):
    """Lowest nightly SGD rate for the tracked hotel via Google Hotels. Needs SERPAPI_KEY."""
    if not SERPAPI_KEY:
        print("  (no SERPAPI_KEY secret — add it in repo Settings → Secrets → Actions)")
        return None
    r = requests.get("https://serpapi.com/search", timeout=60, params={
        "engine": "google_hotels", "api_key": SERPAPI_KEY,
        "q": spec["name"], "check_in_date": spec["in"], "check_out_date": spec["out"],
        "adults": spec["adults"], "currency": "SGD", "hl": "en", "gl": "sg"})
    r.raise_for_status()
    return pick_hotel_price(r.json().get("properties", []), spec["name"])


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


# ---------- shared extraction helpers ----------

def tag_ident(tag):
    return (" ".join(str(c) for c in (tag.get("class") or [])) + " "
            + str(tag.get("id") or "") + " " + str(tag.get("style") or ""))


def is_pricey(tag):
    """Elements likely to hold a selling price (not crossed-out 'was' prices)."""
    if tag.name in ("script", "style", "svg", "del", "s", "strike"):
        return False
    ident = tag_ident(tag)
    if "price" not in ident.lower() and not tag.has_attr("data-price") and tag.get("itemprop") != "price":
        return False
    if STRIKE_RE.search(ident):
        return False
    return True


def collect_ld_prices(node, out):
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
                            out.append(p)
        for v in node.values():
            collect_ld_prices(v, out)
    elif isinstance(node, list):
        for item in node:
            collect_ld_prices(item, out)


def find_price_in_ld(node):
    found = []
    collect_ld_prices(node, found)
    return found[0] if found else None


def label_for(el):
    """Best-effort human label for a price element: the nearest container's own text."""
    price_text = el.get_text(" ", strip=True)
    node = el
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        extra = re.sub(r"\s+", " ", text.replace(price_text, " ")).strip()
        if len(extra) >= 8:
            return extra[:70]
    h = el.find_previous(["h1", "h2", "h3", "h4"])
    if h:
        return re.sub(r"\s+", " ", h.get_text(" ", strip=True))[:70]
    return None


# ---------- extraction: single price (default mode) ----------

def extract_generic(html):
    """Find a single price via structured data most shops embed. Returns (price, method, title)."""
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

    # 3) Price-ish page elements — prefer elements whose class screams "the actual
    #    price" (pdp-price, current-price��), so shipping fees like "S$4.00" lose.
    #    Within a group: shortest text, then lowest number.
    candidates = []
    for el in soup.find_all(is_pricey):
        value = el.get("content") or el.get("data-price") or el.get_text(" ", strip=True)
        price = parse_number(value)
        if price and len(str(value)) < 30:
            good = bool(GOOD_PRICE_RE.search(tag_ident(el)))
            candidates.append((0 if good else 1, len(str(value)), price))
    if candidates:
        candidates.sort()
        return candidates[0][2], "auto (page element)", title

    return None, None, title


# ---------- extraction: lowest price on page ----------

def extract_lowest(html):
    """Hotels/flights/multi-option pages: collect every plausible bookable price and
    return the lowest. Skips crossed-out prices, <del> elements, filter chips like
    '<S$ 560', and fee/tax/donation fragments — but never filters statistically,
    so a legit flash-sale price always survives."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    found = []

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        collect_ld_prices(data, found)
    for attrs in ({"property": "product:price:amount"}, {"property": "og:price:amount"},
                  {"itemprop": "price"}):
        for tag in soup.find_all("meta", attrs=attrs):
            p = parse_number(tag.get("content"))
            if p:
                found.append(p)

    for el in soup.find_all(is_pricey):
        value = el.get("content") or el.get("data-price") or el.get_text(" ", strip=True)
        text = str(value)
        if "<" in text or ">" in text:
            continue  # filter chips like "<S$ 560"
        price = parse_number(text)
        if price and len(text) < 30:
            found.append(price)

    # Pure money-looking text nodes (S$ 494, $225.00, SGD 199)
    for node in soup.find_all(string=MONEY_RE):
        parent = node.parent
        if parent is None or parent.name in ("script", "style", "del", "s", "strike"):
            continue
        ident = tag_ident(parent)
        if STRIKE_RE.search(ident) or NOISE_RE.search(ident):
            continue  # crossed-out prices and fee/tax/charge fragments are not bookable prices
        price = parse_number(node)
        if price:
            found.append(price)

    found = sorted(set(p for p in found if p >= 1))  # drop junk like occupancy "2"
    if found:
        return found[0], "auto (lowest of " + str(len(found)) + " prices)", title
    return None, None, title


def apply_extraction(html, product):
    """Selector (if set) wins; then mode-aware auto-detection."""
    if product.get("selector"):
        el = BeautifulSoup(html, "lxml").select_one(product["selector"])
        price = parse_number(el.get_text(" ", strip=True)) if el else None
        if price:
            return price, "manual selector", None
    if product.get("mode") == "lowest":
        return extract_lowest(html)
    return extract_generic(html)


# ---------- extraction: scan everything / track a ticked label ----------

def collect_ld_offers(node, add, ctx_name=None):
    if isinstance(node, dict):
        name = node.get("name") or ctx_name
        offers = node.get("offers")
        if offers:
            if isinstance(offers, dict):
                offers = [offers]
            for o in offers:
                if isinstance(o, dict):
                    p = parse_number(o.get("price") or o.get("lowPrice"))
                    if p:
                        add(o.get("name") or name, p)
        for v in node.values():
            collect_ld_offers(v, add, name)
    elif isinstance(node, list):
        for item in node:
            collect_ld_offers(item, add, ctx_name)


def extract_all(html):
    """Every plausible price on the page with a human label: [{label, price}, ...]."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()

    def add(label, price):
        if not price:
            return
        key = (label or "", price)
        if key in seen:
            return
        seen.add(key)
        out.append({"label": label or "Price #" + str(len(out) + 1), "price": price})

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        collect_ld_offers(data, add)

    for el in soup.find_all(is_pricey):
        value = el.get("content") or el.get("data-price") or el.get_text(" ", strip=True)
        text = str(value)
        if "<" in text or ">" in text or len(text) >= 30:
            continue
        price = parse_number(text)
        if price:
            add(label_for(el), price)

    for node in soup.find_all(string=MONEY_RE):
        parent = node.parent
        if parent is None or parent.name in ("script", "style", "del", "s", "strike"):
            continue
        ident = tag_ident(parent)
        if STRIKE_RE.search(ident) or NOISE_RE.search(ident):
            continue
        price = parse_number(node)
        if price:
            add(label_for(parent), price)

    return out


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def match_label(html, watch_label):
    """Find the candidate whose label matches watch_label; return its CURRENT price.
    Exact match first, then tolerant partial match (labels can shift slightly)."""
    want = _norm(watch_label)
    best = None
    for c in extract_all(html):
        lbl = _norm(c["label"])
        if lbl == want:
            return c["price"]
        if want in lbl or lbl in want:
            best = c["price"]
    return best


def extract_for(product, html):
    """Per-layer extraction honoring watch_label > selector > mode."""
    if product.get("watch_label"):
        price = match_label(html, product["watch_label"])
        return (price, "watched label", None) if price else (None, None, None)
    return apply_extraction(html, product)


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

def check_product(p):
    original_url = p["url"]
    resolved_url = original_url
    shopid, itemid = shopee_ids(original_url)

    # Flight spec (flight:SIN-SYD/2026-08-24[/2026-08-31][/2]) — data comes from the
    # SerpApi Google Flights API instead of page scraping: structured JSON, no wall.
    if original_url.startswith("flight:"):
        spec = parse_flight_spec(original_url)
        if not spec:
            return None, None, None
        try:
            price = serpapi_flight_price(spec)
        except Exception as e:
            print("  (serpapi failed: " + str(e) + ")")
            return None, None, None
        if price:
            return price, "✈️ google flights", flight_title(spec)
        return None, None, None

    # Hotel spec (hotel:NAME/CHECK-IN/CHECK-OUT[/ADULTS]) — nightly rates from the
    # SerpApi Google Hotels API: structured JSON, no captcha walls (bye Trip.com pain).
    if original_url.startswith("hotel:"):
        spec = parse_hotel_spec(original_url)
        if not spec:
            return None, None, None
        try:
            price = serpapi_hotel_price(spec)
        except Exception as e:
            print("  (serpapi hotels failed: " + str(e) + ")")
            return None, None, None
        if price:
            return price, "🏨 google hotels", hotel_title(spec)
        return None, None, None

    # Resolve short links (shope.ee / lzd.co etc.) to the real product URL
    if not shopid and re.match(r"https?://(shope\.ee|s\.shopee|www\.lazada|lzd)", original_url):
        try:
            r = requests.get(original_url, headers=HEADERS, timeout=20, allow_redirects=True)
            if not is_bad_url(r.url):
                resolved_url = r.url
            shopid, itemid = shopee_ids(resolved_url)
        except Exception:
            pass

    # Layer 1 — Shopee item API (skipped when watching a specific label: the API
    # only knows the listing's main price, not the user's pick)
    if shopid and itemid and not p.get("watch_label"):
        try:
            price, name = shopee_api_price(shopid, itemid)
            if price:
                return price, "auto (shopee api)", name
        except Exception as e:
            print("  (shopee api blocked: " + str(e) + ")")

    # Layer 2 — fast plain fetch (discarded if it lands on a captcha page)
    html = None
    try:
        html, landing_url = get_html(resolved_url)
        if is_bad_url(landing_url):
            html = None
    except Exception as e:
        print("  (plain fetch failed: " + str(e) + ")")

    price = method = title = None
    if html:
        price, method, title = extract_for(p, html)

    # Layer 3 — real browser (always aimed at the real product URL, never a captcha page)
    if not price:
        html2, browser_url = render_html(resolved_url)
        if html2 and not is_bad_url(browser_url):
            price, method, title = extract_for(p, html2)
            if method:
                method += " (browser)"

    # Layer 4 — ScraperAPI proxy (only when SCRAPER_API_KEY secret exists)
    if not price and SCRAPER_API_KEY:
        html3 = fetch_via_scraperapi(resolved_url)
        if html3:
            price, method, title = extract_for(p, html3)
            if method:
                method += " (scraperapi)"

    # There is deliberately NO Google Shopping fallback layer here.
    #
    # A shopping result is another seller's listing, so using it as "the price of this
    # link" is simply wrong. The Vention powerbank showed the damage: the card recorded
    # S$11.80 from Google Shopping while the tracked Shopee page said $24.99.
    #
    # If every layer above failed we return None and the card says so honestly
    # ("last check failed - showing earlier price"). Other sellers' prices are available
    # on demand via the dashboard's market check, which writes to scans.json under a
    # "market:" key that the tracker never reads.

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


# ---------- one-time cleanup: drop prices that did not come from the link ----------

FOREIGN_METHOD_MARKERS = ("google shopping", "shopping backup")


def purge_foreign_prices(products, prices):
    """Earlier builds could record another seller's Google Shopping price as the tracked
    price. Those readings are not this link's price, so they are cleared exactly once:
    the card drops back to "no price yet" and re-derives everything from its own link.

    Idempotent - each product is stamped with _link_only so this never runs twice.
    """
    cleaned = []
    for p in products:
        if p.get("_link_only"):
            continue
        p["_link_only"] = True
        p.pop("backup", None)          # the old opt-in flag no longer does anything
        method = str(p.get("method") or "").lower()
        if any(mark in method for mark in FOREIGN_METHOD_MARKERS):
            p["last_price"] = None
            p["method"] = None
            p["status"] = "reset - the old price came from another store; re-checking from your link"
            p.pop("_tracked_key", None)
            pid = p.get("id")
            if pid:
                prices[pid] = []
            cleaned.append(p.get("name") or p["url"])
    if cleaned:
        print("\U0001F9F9 Cleared non-link prices for: " + ", ".join(cleaned))
    return cleaned


# ---------- main ----------

def main():
    data = load_json(PRODUCTS_FILE, {"products": []})
    products = data.get("products", data) if isinstance(data, dict) else data
    prices = load_json(PRICES_FILE, {})
    alerts = []

    purge_foreign_prices(products, prices)

    for p in products:
        if p.get("paused"):
            continue
        p.setdefault("id", make_id(p["url"]))
        name = p.get("name") or p["url"]
        print("\n🔎 " + name)

        try:
            price, method, title = check_product(p)
        except Exception as e:
            price = None
            print("  ⚠️ " + str(e))

        p["last_checked"] = now_iso()

        if not price:
            # Flaky mega-sites: keep the last good price visible instead of scary errors.
            p["fail_count"] = int(p.get("fail_count") or 0) + 1
            if p.get("last_price") is None:
                if p["url"].startswith("flight:"):
                    p["status"] = "needs attention — flight check failed (is the SERPAPI_KEY secret added? route may be unavailable)"
                elif p["url"].startswith("hotel:"):
                    p["status"] = "needs attention — hotel check failed (SERPAPI_KEY added? hotel name may not match a Google Hotels listing)"
                elif p.get("watch_label"):
                    p["status"] = "rate gone — watched option not found (rescan and re-pick)"
                else:
                    p["status"] = "needs attention — couldn't find a price (try 🔍 Scan or a selector in Advanced)"
            else:
                p["status"] = "tracking"
                p["note"] = "last check failed — showing earlier price"
            continue

        prev = p.get("last_price")
        if title and (not p.get("name") or p["name"].lower().startswith(("http", "product", "flight:", "hotel:"))
                      or p["name"].lower() in ("detail", "pdp", "deal")
                      or (p["name"].isupper() and len(p["name"]) <= 4)):  # junky auto-names like "SIN"
            p["name"] = title[:80]
        if p.get("watch_label"):
            method = "🎯 " + p["watch_label"][:28]
        p.update(status="tracking", method=method, last_price=price, fail_count=0)
        p.pop("note", None)

        if history_reset_needed(p):
            prices[p["id"]] = []   # genuinely watching something new → restart (day 1)
        p["_tracked_key"] = tracked_key(p)

        hist = prices.setdefault(p["id"], [])
        hist.append({"t": p["last_checked"], "p": price})
        prices[p["id"]] = hist[-500:]

        target = p.get("target")
        link = p["url"]
        if p["url"].startswith("flight:"):
            link = flight_link(p["url"])
        elif p["url"].startswith("hotel:"):
            link = hotel_link(p["url"])
        if target and price <= target and (prev is None or prev > target):
            alerts.append("🎯 *TARGET HIT — " + md_escape(p["name"]) + "*\n"
                          "Now *" + fmt(price, p["url"]) + "* (target " + fmt(target, p["url"]) + ")\n"
                          "[🛒 View Product](" + link + ")")
        elif prev is not None and price < prev:
            alerts.append("📉 *PRICE DROP — " + md_escape(p["name"]) + "*\n"
                          "Was " + fmt(prev, p["url"]) + " → Now *" + fmt(price, p["url"]) + "*\n"
                          "[🛒 View Product](" + link + ")")
        print("  ✅ " + fmt(price, p["url"]) + " via " + str(method))

    save_json(PRODUCTS_FILE, {"products": products} if isinstance(data, dict) else products)
    save_json(PRICES_FILE, prices)

    if alerts:
        send_telegram("💰 *Price alert!*\n\n" + "\n\n".join(alerts))
    print("\nDone — " + str(len(products)) + " product(s), " + str(len(alerts)) + " alert(s).")


if __name__ == "__main__":
    main()
