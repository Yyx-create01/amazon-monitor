---
name: amazon-listing-monitor
description: Use when building an Amazon product page monitoring system with daily checks, Feishu notifications, and GitHub Actions deployment. Triggers: "Amazon monitoring", "listing change detection", "product page checker", "ASIN monitoring with Feishu", "亚马逊监控".
---

# Amazon Listing Monitor

## Overview

Lightweight Python script that daily-checks Amazon product pages for changes across 7+ dimensions, compares against a SQLite baseline, and pushes alerts to Feishu. Runs on GitHub Actions, triggered by Windows Task Scheduler via `repository_dispatch` API. Designed for 400-500 ASINs/day with direct HTTP requests — no paid proxy/scraping APIs needed.

## Architecture (Do NOT Over-Engineer)

```
Feishu sheet (ASINs + names) → Python requests → BeautifulSoup parse → SQLite diff → Feishu sheet + bot msg
                                   ↑ 5-8s delay        ↑ 7 dimensions        ↑ baseline.db
          ┌─ Windows Task Scheduler (daily 9:00 AM Beijing)
          │   curl → POST /repos/.../dispatches → repository_dispatch
          ▼
    GitHub Actions workflow (ubuntu-latest, 120min timeout)
```

**Stack:** Python 3, `requests`, `beautifulsoup4`, `lxml`. Single file (`main.py`, ~740 lines). No framework, no SDKs for Feishu (use raw HTTP). **GitHub Actions `schedule` is unreliable — do NOT use it.** Use an external trigger (Windows Task Scheduler, cron-job.org, etc.) to call the `repository_dispatch` API at the desired time. Public repos have unlimited minutes; private repos get 2,000 min/month.

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

- **Delay:** 5-8 seconds between ASINs (uniform random). 400 ASINs × ~6.5s avg = ~43 min per run.
- **Retry:** On 429 or 503, exponential backoff: `sleep(5 * (attempt + 1))`, max 3 retries.
- **Blocked detection:** 3 consecutive empty titles → print warning + include in Feishu summary.
- **Session reuse:** `requests.Session()` to share TCP connections across ASINs.

If rate-limited, increase delay further. Don't add proxies or headless browsers — just slow down.

## First-Run Baseline

- First execution: store ALL fetched data as baseline. **Do not send any change alerts.**
- Print a summary notification only ("Baseline established for N products").
- From second run onward: compare, alert only on actual diffs.

## Handling Unavailable Products

When product is unavailable (no Add to Cart button or `#outOfStock` detected):
- Set `add_to_cart` to "Unavailable"
- **Override `price_raw` to "不可售"** — do NOT report price from third-party sellers
- Skip price comparison in diff logic (unavailable prices are meaningless)

### OOS Redirect to Child Variants

Amazon sometimes redirects an out-of-stock ASIN to an in-stock child variant (different ASIN, different title/price/bullets). This causes **false changes** — the scraper reads child variant data and stores it under the parent ASIN key, corrupting the baseline.

**Fix:** `fetch_page()` must return a `(soup, redirected)` tuple. After `requests.get(..., allow_redirects=True)`, check `asin not in resp.url`. If redirected, set hardcoded OOS data (`title="不可售（重定向到子体）"`, `price_raw="不可售"`, `add_to_cart="Unavailable"`) and skip normal parsing.

**Critical:** Use `asin not in resp.url` — do NOT use exact URL match (`url != resp.url`). Amazon appends `/ref=sr_1_1` segments to same-ASIN URLs, which would be falsely flagged as redirects with exact matching.

### Same-URL Variant Switch (Different Problem from OOS Redirect)

Amazon sometimes shows different child variant data (different title, price, bullets) under the **same ASIN URL** without any URL redirect. This is NOT caught by redirect detection — the URL doesn't change, only the page content does.

**Symptom:** Title, price, and/or bullet points change simultaneously for the same ASIN, but the product identity has shifted (e.g., "50pcs" → "100pcs" variant, or "Wire Lugs Kit" → "Copper Wire Lugs" variant).

**Detection (`is_variant_switch()`):** When `title` changes AND (`price_raw` OR `bullet_points`) also changes, treat it as a variant switch:
- **Do NOT send any alert** (not a real listing change)
- **Do NOT update the baseline** (prevents corruption)
- Print a log line: "Variant switch detected, skipping (no alert)"

**Why this rule?** Title is the strongest identity signal. If it changes alongside price or bullets, it's almost certainly a different variant being served. Covers:
- title + price (e.g., "400PCS/$39.99" → "680PCS/$54.99" — different variant at same URL)
- title + bullets (e.g., "Wire Lugs Kit" → "Copper Wire Lugs" — different product name)
- title + price + bullets (e.g., "50pcs/$9.99" → "100pcs/$18.99" — different pack size)

## Notification Strategy

**Daily summary card (always sent):**
Use Feishu interactive card (`msg_type: "interactive"`) with color-coded header:
- All normal: green header `✅ 每日检查完成 | YYYY-MM-DD`
- With changes: orange header `⚠️ 每日检查完成 | YYYY-MM-DD`, body lists each ASIN change
- Blocked/error: red header `🚫 检查异常 — 可能被限制`

Card body uses `lark_md` for markdown rendering. Each changed ASIN shown as `**ASIN**（品名）— 变化详情`. Product names are read from column B of the source sheet. Includes an `action` button linking to the Feishu result sheet. Use `wide_screen_mode: true` for better readability.

**Feishu result sheet:** Use `PUT /open-apis/sheets/v2/spreadsheets/{token}/values` to overwrite the sheet each day (range in body: `valueRange.range`). Write header row + data rows + empty padding rows to clear old data. **Do NOT use `values_clear` — this endpoint does not exist in Feishu's API.** The FeishuClient `_req()` method must support PUT (not just GET/POST).

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
| OOS redirect → false changes | Scrape child variant data under parent ASIN | Detect redirect via `asin not in resp.url`, store hardcoded OOS data |
| Exact URL match for redirect | `AMAZON_BASE + asin != resp.url.rstrip("/")` | Use `asin not in resp.url` — Amazon adds `/ref=` segments |
| Feishu `values_clear` 404 | Call non-existent API endpoint | Use `PUT /values` with range in body, pad with empty rows |
| FeishuClient only GET/POST | PUT requests silently become POST | Handle PUT in `_req()` method dispatch |
| Baseline only updated on changes | ASINs with empty baseline never get proper data | Always call `save_baseline()` — it handles empty→populated silently |
| Blocked warning stuck on | Warning never resets after parsing recovers | Reset `blocked_warning = False` when `empty_title_streak` returns to 0 |
| GitHub Actions inactivity | Assume scheduled runs last forever | 60-day no-commit = disabled. Baseline auto-commit resets the timer. |
| GitHub Actions permissions | `permissions` only at job level | Set at workflow top level: `contents: write`, `actions: read`, `checks: write`. Also check repo Settings → Actions → General → "Read and write permissions". |
| GitHub schedule unreliable | Use `schedule` cron in workflow YAML | GitHub schedule has random delays (hours to days), especially on some accounts. Use `repository_dispatch` triggered by an external scheduler (Windows Task Scheduler, cron-job.org, etc.) at the exact desired time. |
| Variant switch → false changes | Same-ASIN URL serves different child variant data (different title/price/bullets) without URL redirect — redirect detection can't catch this | Detect when title changes AND (price OR bullets also changes) → silently skip (no alert, no baseline update). Covers: title+price, title+bullets, and title+price+bullets. |

## Baseline Management

- **First run:** `baseline is None` → `insert_baseline()` for every ASIN, no alerts sent.
- **Subsequent runs:** Always call `save_baseline()` after processing each ASIN (not just when changes detected). This silently fixes empty→populated transitions for ASINs that previously failed to parse but now return valid data.
- **Corrupted baselines:** When an OOS redirect causes child variant data to be stored under a parent ASIN, identify the ASIN and restore it from a known-good git commit (`git show <commit>:baseline.db`).

## Project Structure

```
amazon-monitor/
├── main.py              # Single script (~740 lines)
├── requirements.txt     # requests, beautifulsoup4, lxml
├── baseline.db          # SQLite auto-generated on first run
├── .env                 # Feishu credentials (not committed)
├── skills/
│   └── amazon-listing-monitor/
│       └── SKILL.md     # This skill document
└── .github/workflows/
    └── daily-check.yml  # repository_dispatch + workflow_dispatch
```

## Feishu Setup Checklist

1. Create enterprise self-built app in Feishu Developer Console
2. Get App ID + App Secret → set as GitHub Actions Secrets
3. Grant permissions: `doc:sheet` (sheets read/write), `im:message:send` (bot messages)
4. Prepare source sheet (column A = ASIN, column B = product name) + result sheet (change log)
5. Get chat_id for bot notification target (group or individual)

## Limitations

- CSS selectors may need adjustment per Amazon marketplace/category
- No JavaScript rendering — fields relying on dynamic loading will be missed
- 400 ASINs × 5-8s delay = upper bound ~53 min; must fit within GitHub Actions 6h limit
- Not designed for multi-marketplace (`.com` only by default)
