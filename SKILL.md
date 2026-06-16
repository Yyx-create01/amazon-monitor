---
name: amazon-listing-monitor
description: Use when building an Amazon product page monitoring system with daily checks, Feishu notifications, and GitHub Actions deployment. Triggers: "Amazon monitoring", "listing change detection", "product page checker", "ASIN monitoring with Feishu", "亚马逊监控".
---

# Amazon Listing Monitor

## Overview

Lightweight Python script that daily-checks Amazon product pages for changes across 7+ dimensions, compares against a SQLite baseline, and pushes alerts to Feishu. Deploys on GitHub Actions (free tier). Designed for 400-500 ASINs/day with direct HTTP requests — no paid proxy/scraping APIs needed.

## Architecture (Do NOT Over-Engineer)

```
Feishu sheet (ASINs) → Python requests → BeautifulSoup parse → SQLite diff → Feishu sheet + bot msg
                         ↑ 2-4s delay        ↑ 7 dimensions       ↑ baseline.db
```

**Stack:** Python 3, `requests`, `beautifulsoup4`, `lxml`. Single file (`main.py`, ~600 lines). No framework, no SDKs for Feishu (use raw HTTP). Deploy via GitHub Actions cron `0 0 * * *` (= 08:00 Beijing).

**Key principle:** 400 requests/day to Amazon is trivial. Don't reach for Playwright, Bright Data, or async — they add complexity without benefit at this scale.

## 7 Dimensions

| # | Field | Page Location | Notes |
|---|-------|--------------|-------|
| 1 | Title | `#productTitle` | |
| 2 | Price | `.a-price .a-offscreen` + promo context | See Promo Detection below |
| 3 | Bullet Points | `#feature-bullets .a-list-item` | Multiple fallback selectors needed |
| 4 | Add to Cart | `#add-to-cart-button` (presence + text) | Unavailable → set price to "不可售" |
| 5 | Sold By | `#merchant-info` / `#soldByThirdParty` | |
| 6 | Breadcrumb | `#wayfinding-breadcrumbs_feature_div` | |
| 7 | Variations | `#twister` / variation selectors | Skip if product has no variations |

Additionally: Rating (stars) and review count, with conservative thresholds (rating diff ≥ 0.1, review drop ≥ 6 or surge > 10).

## Promo Detection — The Hardest Part

### What IS a promotion (changes the displayed price):
- **Deal Badge** (BD / Limited Time Deal): CSS `.dealBadge`, `#dealprice`, `[data-a-badge="deal"]`
- **Lightning Deal** (LD): `[class*="lightning-deal"]`
- **Prime Day Deal**: `.prime-day-badge` + text "Prime Day Deal"
- **Price discount**: `-XX%` text AND "Typical price" label, **both within the price block** (`#corePrice_desktop` / `.a-price`)

### What is NOT a promotion (DO NOT flag):
- **Coupon** — separate clip mechanism, doesn't change displayed price
- **Subscribe & Save** — subscription pricing, not a promo
- **Prime Exclusive Badge** — membership label, not a price change
- **BOGO** — buy-one-get-one, doesn't change displayed price
- **`-XX%` vs "List Price"** — MSRP reference, not a real discount. Only flag when reference is "Typical price" (Amazon's historical price)

### Critical: Search within the price block, NOT the full page
Searching `-\d+%` or `Typical price` across the entire page WILL cause false matches from reviews, comparison tables, and A+ content. Always scope to the price container.

### `is_promo` is a classification helper, NOT a monitoring dimension
`is_promo` is stored in the baseline and used to label price changes as "（促销中）" vs normal — but it is NEVER reported as its own change event. Do not include it in the diff field list.

## Rate Limiting Strategy

- **Delay:** 2-4 seconds between ASINs (uniform random). 400 ASINs × ~5s avg = ~35-90 min per run.
- **Retry:** On 429 or 503, exponential backoff: `sleep(5 * (attempt + 1))`, max 3 retries.
- **Blocked detection:** 3 consecutive empty titles → print warning + include in Feishu summary.
- **Session reuse:** `requests.Session()` to share TCP connections across ASINs.

If rate-limited, increase delay to 3-5s. Don't add proxies or headless browsers — just slow down.

## First-Run Baseline

- First execution: store ALL fetched data as baseline. **Do not send any change alerts.**
- Print a summary notification only ("Baseline established for N products").
- From second run onward: compare, alert only on actual diffs.

## Handling Unavailable Products

When product is unavailable (no Add to Cart button or `#outOfStock` detected):
- Set `add_to_cart` to "Unavailable"
- **Override `price_raw` to "不可售"** — do NOT report price from third-party sellers
- Skip price comparison in diff logic (unavailable prices are meaningless)

## Notification Strategy

**Daily summary (always sent):**
- All normal: `✅ 每日检查完成 | 共检查 N 个 ASIN，全部无异常`
- With changes: `⚠️ 每日检查完成 | 发现 M 个 ASIN 有变化` + change list + link to Feishu sheet

**Feishu result sheet:** Append one row per change: ASIN, timestamp, field label (Chinese), old value, new value.

## Pitfalls We Hit (Avoid These)

| Pitfall | Wrong Approach | Right Approach |
|---------|---------------|----------------|
| Scraping API overkill | Use Bright Data/Oxylabs for 400/day | Direct `requests` works fine at this volume |
| `-\d+%` global search | `soup.find(string=re.compile(r"-\d+%"))` | Scope to `price_block.find()` only |
| Treating coupon as promo | Flag coupon as price change | Coupon is separate, doesn't change displayed price |
| Reporting promo status as change | Include `is_promo` in diff fields | Use `is_promo` only to label price changes |
| Price on unavailable products | Capture whatever price shows on page | Set to "不可售", skip price comparison |
| `.a-color-price` as unavailable indicator | Treat colored price as "unavailable" | `.a-color-price` is a style class, not status |
| `List Price` vs `Typical price` | Treat all reference prices equally | Only "Typical price" = real discount. "List Price" = MSRP, skip. |
| Windows GBK encoding | Print emoji to terminal | `sys.stdout.reconfigure(encoding="utf-8")` or use ASCII alternatives |

## Project Structure

```
amazon-monitor/
├── main.py              # Single script (~600 lines)
├── requirements.txt     # requests, beautifulsoup4, lxml
├── baseline.db          # SQLite auto-generated on first run
├── .env                 # Feishu credentials (not committed)
└── .github/workflows/
    └── daily-check.yml  # cron: "0 0 * * *" + workflow_dispatch
```

## Feishu Setup Checklist

1. Create enterprise self-built app in Feishu Developer Console
2. Get App ID + App Secret → set as GitHub Actions Secrets
3. Grant permissions: `doc:sheet` (sheets read/write), `im:message:send` (bot messages)
4. Prepare source sheet (ASIN list in column A) + result sheet (change log)
5. Get chat_id for bot notification target (group or individual)

## Limitations

- CSS selectors may need adjustment per Amazon marketplace/category
- No JavaScript rendering — fields relying on dynamic loading will be missed
- 400 ASINs × 2-4s delay = upper bound ~90 min; must fit within GitHub Actions 6h limit
- Not designed for multi-marketplace (`.com` only by default)
