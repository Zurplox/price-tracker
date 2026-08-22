"""
🔍 Price scanner — fetches ONE url through the same layered fetch as the tracker
and writes every price it finds (with labels) into scans.json, so the dashboard
can show them as a tickable list. Runs via the "Scan prices" GitHub Actions
workflow (manual dispatch with a scan_url input).
"""

import os
import sys

import tracker


def main():
    url = os.environ.get("SCAN_URL", "").strip()
    if not url:
        print("No SCAN_URL provided")
        sys.exit(1)
    print("🔍 Scanning " + url)

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

    # Dedupe by (label, price), keep the list readable
    seen, out = set(), []
    for c in candidates:
        key = (c["label"], c["price"])
        if key not in seen:
            seen.add(key)
            out.append(c)
    out = out[:50]

    scans = tracker.load_json("scans.json", {})
    scans[url] = {"scanned_at": tracker.now_iso(), "via": via or "none", "candidates": out}
    tracker.save_json("scans.json", scans)

    print("\nFound " + str(len(out)) + " price candidate(s) via " + (via or "nothing"))
    for c in out[:12]:
        print("  • " + str(c["price"]) + " — " + c["label"])


if __name__ == "__main__":
    main()
