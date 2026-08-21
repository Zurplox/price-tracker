# 💰 Price Tracker

Paste a link on your own dashboard page → the tracker finds the price automatically → you get a **Telegram ping** when it drops. Runs free on GitHub Actions every 6 hours. Your computer can be off; you never need to touch code.

## How it works

```
You paste a link on your dashboard page
        ↓  (saved into products.json in your repo)
GitHub Actions wakes up every 6 hours (or when you press "Run check now")
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

- **Add a product**: paste link + optional target price → **Add product**. A check runs immediately; price appears in ~2–3 min.
- **Get alerts**: automatic on Telegram — when a price drops at all, or crosses your target.
- **Tweak targets**: type in the "Alert below" field on any card — saves automatically.
- **"Run check now"**: forces an immediate check.

## Notes

- Auto-detect works on most shops out of the box. If a card shows **⚠ needs attention**, open its **Advanced** section and paste a CSS selector (right-click the price on the shop page → Inspect → right-click highlighted HTML → Copy → Copy selector).
- Shopee/Lazada sometimes show a captcha to bots — if a check fails, hitting **Run check now** a bit later usually works.
- Free tier usage: each run takes ~2–3 minutes of your 2,000 free monthly minutes — 4 runs/day ≈ 300 min/month. Plenty of headroom.
- Want hourly checks? Edit `.github/workflows/tracker.yml` and change the cron line.

## Optional: ScraperAPI backup layer (recommended for Shopee/Lazada)

If a mega-site ever beats all three built-in layers, this 4th layer routes the fetch through a scraping proxy with rotating residential IPs, captcha solving, and real-browser rendering on their end.

1. Sign up free at <https://www.scraperapi.com> (no credit card) — your **API key** is shown on the dashboard right after signup
2. Repo → **Settings → Secrets and variables → Actions → New repository secret** → name it exactly `SCRAPER_API_KEY` → paste the key → **Add secret**
3. Done — nothing to enable. The tracker detects the key automatically each run and only spends credits when layers 1–3 all fail.

Free tier: **1,000 credits/month** (a browser-rendered page ≈ 10 credits). As a fallback-only layer, typical usage is near zero. Check usage anytime on the ScraperAPI dashboard. Only use it for public product pages, never logged-in pages.
