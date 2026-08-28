"""
🔍 Price scanner — fetches links through the same layered fetch as the tracker
and writes every price it finds (with labels) into scans.json, so the dashboard
can show them as tickable lists.

Runs via the "Scan prices" GitHub Actions workflow:
  - dispatch WITH a scan_url input   -> scans just that link (per-card scan button)
  - dispatch WITHOUT it              -> scans ALL tracked links in one run (Scan all)
  - dispatch WITH a market_url input -> link price + other sellers (market check)
"""

import os
import sys

import tracker


def scan_one(url):
    """Scan a single URL. Returns (candidates, via)."""
    if url.startswith(("flight:", "hotel:")):
        print("  (api-backed link — tracked via SerpApi; nothing to scan)")
        return [], "serpapi"
    candidates, via = [], None

    # Shopee shortcut — include the listing's main price as a candidate
    shopid, itemid = tracker.shopee_ids(url)
    if shopid and itemid:
        try:
            price, name = tracker.shopee_api_price(shopid, itemid)
            if price:
                candidates.append({"label": (name or "Shopee listing") + " — main price", "price": price})
                via = "shopee api"
        except Exception as e:
            print("  (shopee api blocked: " + str(e) + ")")

    # Layered fetch: plain → browser → scraperapi. Keep going until we have a
    # decent spread of candidates (or run out of layers).
    html = None
    try:
        html, landing = tracker.get_html(url)
        if tracker.is_bad_url(landing):
            html = None
    except Exception as e:
        print("  (plain fetch failed: " + str(e) + ")")
    if html:
        via = (via + "+" if via else "") + "plain fetch"
        candidates.extend(tracker.extract_all(html))

    if len(candidates) < 2:
        html2, browser_url = tracker.render_html(url)
        if html2 and not tracker.is_bad_url(browser_url):
            via = (via + "+" if via else "") + "browser"
            candidates.extend(tracker.extract_all(html2))

    if len(candidates) < 2 and tracker.SCRAPER_API_KEY:
        html3 = tracker.fetch_via_scraperapi(url)
        if html3:
            via = (via + "+" if via else "") + "scraperapi"
            candidates.extend(tracker.extract_all(html3))

    # Empty-handed? One last dice roll — ScraperAPI rotates exit IPs per request,
    # so a retry often slips past a wall that just blocked us seconds ago.
    if not candidates and tracker.SCRAPER_API_KEY:
        html4 = tracker.fetch_via_scraperapi(url)
        if html4:
            via = (via + "+" if via else "") + "scraperapi retry"
            candidates.extend(tracker.extract_all(html4))

    # Dedupe by (label, price), keep the list readable
    seen, out = set(), []
    for c in candidates:
        key = (c["label"], c["price"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out[:50], via


def market_one(product, url, name):
    """Market snapshot for one link.

    Returns the link's OWN price (scraped through the normal tracking cascade, so it is
    exactly the number tracking would record) PLUS other sellers' offers from SerpApi,
    each with its own link to open.

    Stored under a "market:" key. The tracker never reads that key, so a market price
    can never become the tracked price.
    """
    link = {"price": None, "method": None, "error": None}
    try:
        price, method, _title = tracker.check_product(product)
        link["price"], link["method"] = price, method
        if price is None:
            link["error"] = "the site blocked this run - press the market button again to retry"
    except Exception as e:
        link["error"] = str(e)[:140]
    print("  link price: " + (tracker.fmt(link["price"], url) if link["price"] else "not found"))

    offers = []
    if tracker.SERPAPI_KEY:
        try:
            offers = tracker.serpapi_shopping_offers(name, url, limit=10)
        except Exception as e:
            print("  (serpapi market lookup failed: " + str(e) + ")")
    else:
        print("  (no SERPAPI_KEY secret - link price only)")
    for o in offers[:8]:
        print("    - " + str(o["price"]) + " " + o["store"])

    return {"scanned_at": tracker.now_iso(), "link": link, "offers": offers,
            "via": "link + serpapi shopping" if offers else "link only"}


def main():
    url = os.environ.get("SCAN_URL", "").strip()
    market_url = os.environ.get("MARKET_URL", "").strip()

    if market_url:
        # On-demand market check: link price + other sellers, stored read-only under a
        # "market:" key so it can never feed tracking history.
        if market_url.startswith(("flight:", "hotel:")):
            print("(flights and hotels are already API-priced - no market check needed)")
            return
        name = os.environ.get("MARKET_NAME", "").strip()
        data = tracker.load_json("products.json", {"products": []})
        products = data.get("products", data) if isinstance(data, dict) else data
        product = next((p for p in products if p.get("url") == market_url), None)
        if product is None:
            product = {"url": market_url, "name": name}
        if not name:
            name = product.get("name") or market_url
        scans = tracker.load_json("scans.json", {})
        print("\U0001F4CA Market check for: " + name)
        result = market_one(product, market_url, name)
        scans["market:" + market_url] = result
        tracker.save_json("scans.json", scans)
        print("Done - 1 link price + " + str(len(result["offers"])) + " other seller(s).")
        return

    if url:
        targets = [url]
    else:
        data = tracker.load_json("products.json", {"products": []})
        products = data.get("products", data) if isinstance(data, dict) else data
        targets = list(dict.fromkeys(p["url"] for p in products if not p.get("paused")))

    if not targets:
        print("Nothing to scan")
        sys.exit(1)

    print("🔍 Scanning " + str(len(targets)) + " link(s)")
    scans = tracker.load_json("scans.json", {})

    for t in targets:
        print("\n--- " + t)
        try:
            candidates, via = scan_one(t)
        except Exception as e:
            print("  ⚠️ scan failed: " + str(e))
            candidates, via = [], None
        scans[t] = {"scanned_at": tracker.now_iso(), "via": via or "none", "candidates": candidates}
        print("  found " + str(len(candidates)) + " candidate(s)" + (" via " + via if via else ""))
        for c in candidates[:8]:
            print("    • " + str(c["price"]) + " — " + c["label"])

    tracker.save_json("scans.json", scans)
    print("\nDone — scanned " + str(len(targets)) + " link(s).")


if __name__ == "__main__":
    main()
