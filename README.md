# 💰 Price Tracker

Paste a link on your own dashboard page → the tracker finds the price automatically → you get a **Telegram ping** when it drops. Runs free on GitHub Actions every morning at 4am SGT. Your computer can be off; you never need to touch code.

## How it works

```
You paste a link on your dashboard page
        ↓  (saved into products.json in your repo)
GitHub Actions wakes up daily at 4am SGT (or when you press "Run check now")
        ↓  (finds the price automatically — even on Shopee, via a real browser)
Price dropped or hit your target? → Telegram message 📲
        ↓
Dashboard shows current price + history chart
```

## One-time setup (~15 min)

### 1. Create the repo
Create a **new repository** on GitHub (private is fine — Actions is still free) and upload all files from this folder, keeping the structure:
- `index.html` ← your dashboard
- `tracker.py`, `requirements.txt`
- `products.json`, `prices.json`
- `.github/workflows/tracker.yml`

### 2. Telegram bot (2 min)
1. Message **@BotFather** → `/newbot` → copy the **token**
2. Open a chat with your new bot → press **Start**
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` → find `"chat":{"id":123…` → that's your **chat ID**

### 3. Repo secrets
Repo → **Settings → Secrets and variables → Actions** → add:
- `TELEGRAM_BOT_TOKEN` = bot token
- `TELEGRAM_CHAT_ID` = chat ID

### 4. Turn on your dashboard page
Repo → **Settings → Pages → Build and deployment** → Source: **Deploy from a branch** → Branch: **main / (root)** → Save.
Your dashboard appears at `https://<your-username>.github.io/<repo-name>/` in ~1 minute.

### 5. Connect the dashboard (once per device)
Open your dashboard → **⚙ Settings** → paste a **fine-grained personal access token**
(create at <https://github.com/settings/personal-access-tokens/new>):
- Repository access: **Only select repositories** → this repo
- Permissions: **Contents: Read & write** and **Actions: Read & write**

The token is stored only in your browser (localStorage), never in the repo.
Note: GitHub Pages sites are technically public — without your token the page shows only sample data, so this is fine.

## Daily use

- **Add a product**: paste link + optional name/target → **Add product**. Names auto-fill from the link (and improve themselves from the page title after a successful check); rename anytime in a card's **Advanced** section. Nothing runs until the daily 4am SGT schedule or your next **Run check now** (one press checks everything in one go).
- **Get alerts**: automatic on Telegram — when a price drops at all, or crosses your target.
- **Status line**: the top of the dashboard always shows the last update time — plus a 🔵 chip whenever a check or scan is genuinely running (works from any device, including the 4am auto-run). If a run ever hangs on GitHub's side (rare), the dashboard auto-cancels zombie runs older than ~20 minutes and unlocks itself; you can also cancel runs manually from the repo's **Actions** tab.
- **Tweak targets**: type in the "Alert below" field on any card — saves automatically.
- **"Run check now"**: forces an immediate check. While a check or scan is in progress, the buttons lock and a status pill tells you when fresh data will land (the page refreshes itself) — repeated presses just queue more runs and waste free minutes, so no need to spam.
- **💸 Multi-option pages (hotels, flights, fare bundles)**: tick **Lowest price on page** when adding — the tracker records the *cheapest* option on the page and keeps working even when specific rooms/rates sell out. You can also toggle it later in a card's **Advanced** section.
- **🎯 Watch one specific option**: press **🔍 Scan all** in the header (scans every tracked link in one run, ~5–10 min) — or a card's **🔍 Scan this page** for a single link. When the list appears, open it, pick the exact price (room type, fare bundle…), and hit **Track selected price**. The tracker follows that exact option run after run — and honestly reports if it sells out, instead of silently tracking something else. Want two options? Add the same link twice and pick differently in each card. Changing your pick later (or the selector/mode) restarts that card's history from day 1, so charts never blend two different options.
- **📱 Sharing & other devices**: viewing needs nothing — anyone with the link sees live data read-only. To add/scan/run from a device, enter the owner password in the banner's **🔓 Unlock actions** box (once per device — it's remembered), or paste the token in ⚙ Settings. **🔒 Lock this device** (in Settings) revokes it. The token ships only as an encrypted blob (AES-256-GCM, PBKDF2 310k rounds) — never as plain text — and GitHub can't auto-revoke what it can't recognize. Everyone with the password shares ONE watchlist, and alerts still go to the owner's Telegram. Want a new password or rotated token? Re-encrypt a fresh blob and swap the VAULT values.

## ✈️ Flight tracking (Google Flights via SerpApi)

Track a route's lowest fare — no more checking Google Flights by hand. This works even though airline booking pages block scrapers, because the data comes from SerpApi's hosted API, not from scraping Google or airline sites.

1. Sign up free at [serpapi.com](https://serpapi.com) — the free plan gives **250 searches/month, no card required**. Copy your API key from the dashboard.

**Where a tracked price comes from:** always the link you pasted, via website -> real browser -> ScraperAPI, in that order. There is deliberately **no Google Shopping fallback** — a shopping result is a *different seller's* listing, so recording it as "the price of this link" corrupts the history chart. (It once logged S$11.80 against a Shopee page that actually said $24.99.) If every layer fails, the card says so instead of substituting a stranger's price.

**📊 Check market — opt-in, per card:** press it on any non-flight card to see your link's own price on top, then other sellers' offers from Google Shopping, each with an **Open ↗** button so you can check the listing yourself. It costs 1 SerpApi search, runs *only* when you press it, and is strictly read-only: a market price can never become the tracked price or enter the history chart. Flight and hotel cards don't show the button — they're already API-priced.
2. Repo → **Settings → Secrets and variables → Actions → New repository secret** → name `SERPAPI_KEY`, value = your key.
3. Add a product using the special flight link format:
   - Round trip: `flight:SIN-SYD/2026-08-24/2026-08-31`
   - One way: `flight:SIN-NRT/2026-11-02`

  (Tip: on the dashboard, the collapsible **✈️ Track a flight** builder under the add form constructs this link for you — From/To/dates/adults in, done. No template to memorize.)

**Same trick for hotels 🏨:** `hotel:Fragrance Hotel - Oasis/2026-12-11/2026-12-15/2` (name / check-in / check-out / adults) tracks the lowest nightly rate via Google Hotels — a clean escape from Trip.com's captcha walls. The dashboard has a matching collapsible **🏨 Track a hotel** builder.
   - 2 adults: append `/2` at the end
4. Prices come back in SGD on the same 4am schedule, with the same Telegram alerts ("alert below 600" = ping me when the fare drops under S$600). The card's title links straight to Google Flights for booking.

Each daily check uses 1 search per flight or hotel card (~30/month each) — about 8 travel cards fit comfortably in the free tier. Note: the tracked price is the lowest fare Google Flights shows for that route + dates (per adult); it follows the market, not one specific departure time.

## Notes

- Auto-detect works on most shops out of the box. If a card shows **⚠ needs attention**, open its **Advanced** section and paste a CSS selector (right-click the price on the shop page → Inspect → right-click highlighted HTML → Copy → Copy selector).
- Shopee/Lazada sometimes show a captcha to bots — if a check fails, hitting **Run check now** a bit later usually works.
- Free tier usage: each run takes ~2–3 minutes of your 2,000 free monthly minutes — 1 run/day ≈ 90 min/month. Plenty of headroom.
- Want hourly checks? Edit `.github/workflows/tracker.yml` and change the cron line.

## Optional: ScraperAPI backup layer (recommended for Shopee/Lazada)

If a mega-site ever beats all three built-in layers, this 4th layer routes the fetch through a scraping proxy with rotating residential IPs, captcha solving, and real-browser rendering on their end.

1. Sign up free at <https://www.scraperapi.com> (no credit card) — your **API key** is shown on the dashboard right after signup
2. Repo → **Settings → Secrets and variables → Actions → New repository secret** → name it exactly `SCRAPER_API_KEY` → paste the key → **Add secret**
3. Done — nothing to enable. The tracker detects the key automatically each run and only spends credits when layers 1–3 all fail.

Free tier: **1,000 credits/month** (a browser-rendered page ≈ 10 credits). As a fallback-only layer, typical usage is near zero. Check usage anytime on the ScraperAPI dashboard. Only use it for public product pages, never logged-in pages.
