#!/usr/bin/env python3
"""Amazon Product Page Monitor — daily link checker with Feishu integration."""

import os
import re
import sys

# Load .env file if present (for local runs)
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env_path)
except ImportError:
    pass

# Force UTF-8 output on Windows (avoid GBK encoding errors with CJK/emoji characters)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import json
import time
import random
import sqlite3
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "baseline.db"
BJT = timezone(timedelta(hours=8))

# Feishu credentials from env
FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
SOURCE_SHEET_TOKEN = os.environ["FEISHU_SOURCE_SHEET_TOKEN"]   # spreadsheet with ASINs
RESULT_SHEET_TOKEN = os.environ["FEISHU_RESULT_SHEET_TOKEN"]   # spreadsheet for change log
FEISHU_CHAT_ID = os.environ["FEISHU_CHAT_ID"]                  # chat_id or "user_id" to DM
SOURCE_SHEET_ID = os.environ.get("FEISHU_SOURCE_SHEET_ID", "0")  # sheet tab, default "0"
RESULT_SHEET_ID = os.environ.get("FEISHU_RESULT_SHEET_ID", "0")

# SellerSprite MCP for BSR / category data
SELLERSPRITE_MCP_URL = os.environ.get(
    "SELLERSPRITE_MCP_URL",
    "https://mcp.sellersprite.com/mcp",
)
SELLERSPRITE_SECRET_KEY = os.environ.get(
    "SELLERSPRITE_SECRET_KEY",
    "06594abb126c497aa42ccb9286ec6b66",
)

# Amazon request settings
# Shorter delay for local runs; GitHub Actions IPs need longer and
# internal batching to avoid rate limiting.
INTERNAL_BATCH_SIZE = 0    # 0 = no internal batching
INTERNAL_BATCH_PAUSE = 0
if os.environ.get("GITHUB_ACTIONS") == "true":
    REQUEST_DELAY_MIN = 15  # seconds
    REQUEST_DELAY_MAX = 20
    INTERNAL_BATCH_SIZE = 50   # pause every 50 ASINs
    INTERNAL_BATCH_PAUSE = 300  # 5 minutes
else:
    REQUEST_DELAY_MIN = 5   # seconds
    REQUEST_DELAY_MAX = 8
REQUEST_TIMEOUT = 15
AMAZON_BASE = "https://www.amazon.com/dp/"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


# ── Database ────────────────────────────────────────────────────────

# Field name → Chinese label for Feishu result sheet
FIELD_LABELS = {
    "title": "标题",
    "price_raw": "价格",
    "is_promo": "促销状态",
    "bullet_points": "五点描述",
    "add_to_cart": "购物车",
    "sold_by": "Sold By",
    "breadcrumb": "类目节点",
    "variations": "变体关系",
    "rating": "评分",
    "review_count": "评论数",
    "sales_rank": "销售排名",
}

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baseline (
            asin           TEXT PRIMARY KEY,
            title          TEXT,
            price_raw      TEXT,
            is_promo       INTEGER DEFAULT 0,
            bullet_points  TEXT,
            add_to_cart    TEXT,
            sold_by        TEXT,
            breadcrumb     TEXT,
            variations     TEXT,
            rating         TEXT,
            review_count   TEXT,
            sales_rank     TEXT,
            updated_at     TEXT
        )
    """)
    # Migration: add sales_rank column to existing databases
    cols = [row[1] for row in conn.execute("PRAGMA table_info(baseline)")]
    if "sales_rank" not in cols:
        conn.execute("ALTER TABLE baseline ADD COLUMN sales_rank TEXT")
    conn.commit()
    return conn


def load_baseline(conn: sqlite3.Connection, asin: str) -> dict | None:
    row = conn.execute("SELECT * FROM baseline WHERE asin=?", (asin,)).fetchone()
    if row is None:
        return None
    cols = [d[1] for d in conn.execute("PRAGMA table_info(baseline)")]
    return dict(zip(cols, row))


_baseline_backed_up = False


def _backup_baseline(conn: sqlite3.Connection):
    """Back up baseline.db once per run before any writes."""
    global _baseline_backed_up
    if _baseline_backed_up:
        return
    try:
        backup_path = DB_PATH.with_suffix(".db.bak")
        backup_conn = sqlite3.connect(str(backup_path))
        conn.backup(backup_conn)
        backup_conn.close()
        _baseline_backed_up = True
    except Exception:
        pass  # non-critical, don't block the main flow


def save_baseline(conn: sqlite3.Connection, data: dict):
    """Save parsed data to baseline. Protects critical fields from being
    overwritten by empty values (e.g. when Amazon rate-limits and the parser
    can't extract data)."""
    # Guard: if newly-parsed value is empty but old baseline has valid data,
    # keep the old value to prevent corruption from parse failures.
    protected_fields = ["price_raw", "title"]
    old = conn.execute(
        "SELECT " + ", ".join(protected_fields) + " FROM baseline WHERE asin=?",
        [data["asin"]],
    ).fetchone()
    if old:
        cols = [d[0] for d in conn.execute("PRAGMA table_info(baseline)")]
        old_vals = {}
        for i, f in enumerate(protected_fields):
            idx = cols.index(f) if f in cols else -1
            old_vals[f] = old[idx] if idx >= 0 else ""

    fields = [k for k in data if k != "asin"]
    placeholders = ", ".join(f"{f}=?" for f in fields)
    values = []
    for f in fields:
        v = data[f]
        if old and f in protected_fields:
            if (v is None or str(v).strip() == "") and old_vals[f] and str(old_vals[f]).strip():
                v = old_vals[f]  # keep old value
        values.append(v)
    values.append(data["asin"])
    # Backup baseline before first write of each run
    _backup_baseline(conn)
    conn.execute(f"UPDATE baseline SET {placeholders} WHERE asin=?", values)
    conn.commit()
    return True


def insert_baseline(conn: sqlite3.Connection, data: dict):
    _backup_baseline(conn)
    fields = list(data)
    placeholders = ", ".join("?" for _ in fields)
    values = [data[f] for f in fields]
    conn.execute(f"INSERT INTO baseline ({', '.join(fields)}) VALUES ({placeholders})", values)
    conn.commit()


# ── Text Normalization ────────────────────────────────────────────────
def normalize_text(val) -> str:
    """Collapse whitespace, lowercase, strip special chars for comparison."""
    if val is None:
        return ""
    text = str(val)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    text = text.lower().strip()
    return text


# ── Amazon Scraper ──────────────────────────────────────────────────
def fetch_page(asin: str, session: requests.Session | None = None) -> tuple[BeautifulSoup | None, bool]:
    """Returns (soup, redirected). redirected=True means Amazon served a different ASIN page."""
    proxy_url = os.environ.get("CF_PROXY")  # Optional: Cloudflare Worker proxy
    if proxy_url:
        url = f"{proxy_url}/?asin={asin}"
    else:
        url = AMAZON_BASE + asin

    headers = {**HEADERS, "User-Agent": random.choice(USER_AGENTS)}
    fetcher = session if session else requests
    for attempt in range(3):
        try:
            resp = fetcher.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code == 200:
                # Redirect detection: use X-Final-Url header if proxy, else resp.url
                final_url = resp.headers.get("X-Final-Url", resp.url)
                redirected = asin not in final_url
                return BeautifulSoup(resp.text, "lxml"), redirected
            if resp.status_code in (429, 503):
                time.sleep(5 * (attempt + 1))
                continue
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
            continue
    return None, False


def parse_product(soup: BeautifulSoup) -> dict:
    result = {
        "title": "",
        "price_raw": "",
        "is_promo": 0,
        "bullet_points": "",
        "add_to_cart": "",
        "sold_by": "",
        "breadcrumb": "",
        "variations": "",
        "rating": "",
        "review_count": "",
        "sales_rank": "",
    }

    # 1. Title
    title_el = soup.select_one("#productTitle")
    if title_el:
        result["title"] = title_el.get_text(strip=True)

    # 2. Price + promo detection
    price_el = soup.select_one(".a-price .a-offscreen")
    price_whole = soup.select_one(".a-price-whole")
    price_fraction = soup.select_one(".a-price-fraction")
    if price_el:
        result["price_raw"] = price_el.get_text(strip=True)
    elif price_whole:
        frac = price_fraction.get_text(strip=True) if price_fraction else "00"
        result["price_raw"] = f"${price_whole.get_text(strip=True)}.{frac}"

    # Check for promotions that actually change the displayed price.
    #
    # Real promos:
    #   - Deal Badge (BD / Limited Time Deal): -XX% $XX.XX + Typical price: $XX.XX
    #   - Lightning Deal (LD)
    #   - Prime Day Deal
    #
    # NOT promos (do NOT flag):
    #   - Coupon — separate clip mechanism, doesn't change displayed price
    #   - Subscribe & Save, Prime Exclusive Badge, BOGO — don't change price itself
    #   - -XX% vs "List Price" (MSRP) — just manufacturer reference, not a real discount
    promo_selectors = [
        ".dealBadge",
        "#dealprice",
        '[data-a-badge="deal"]',
        ".a-badge-label-deal",
        "#dealBadge",
        '[data-a-badge-type="deal"]',
    ]
    promo_indicators = [soup.select_one(sel) for sel in promo_selectors]

    # Lightning Deal
    lightning_deal = (
        soup.select_one('[class*="lightning-deal"]')
        or soup.select_one('[id*="lightning-deal"]')
    )

    # Prime Day
    prime_day = (
        soup.select_one('[data-feature-name="primeDayBadge"]')
        or soup.select_one(".prime-day-badge")
        or soup.select_one("#primeDayBadge")
        or bool(soup.find(string=re.compile(r"Prime\s*Day\s*Deal", re.I)))
    )

    # Price discount: -XX% with "Typical price" (Amazon's historical price → real discount).
    # Only search within the price block to avoid false matches elsewhere on the page.
    # "List Price" (MSRP) is NOT a promotion and is excluded.
    price_block = (
        soup.select_one("#corePrice_desktop")
        or soup.select_one("#corePrice_feature_div")
        or soup.select_one('[data-feature-name="corePrice"]')
        or soup.select_one(".a-price")
    )
    price_discount = False
    if price_block:
        typical = price_block.find(string=re.compile(r"Typical\s*price", re.I))
        discount_pct = price_block.find(string=re.compile(r"-\d+%", re.I))
        price_discount = typical is not None and discount_pct is not None

    if any(promo_indicators) or lightning_deal or prime_day or price_discount:
        result["is_promo"] = 1

    # 3. Bullet points
    bullets = soup.select("#feature-bullets .a-list-item")
    if not bullets:
        bullets = soup.select("#feature-bullets li")
    if not bullets:
        bullets = soup.select('[data-feature-name="featurebullets"] .a-list-item')
    if not bullets:
        bullets = soup.select("#featurebullets_feature_div .a-list-item")
    if bullets:
        result["bullet_points"] = " || ".join(
            b.get_text(strip=True) for b in bullets
        )

    # 4. Add to Cart
    atc = (
        soup.select_one("#add-to-cart-button")
        or soup.select_one('[id="submit.add-to-cart"]')
        or soup.select_one("input[name='submit.add-to-cart']")
        or soup.select_one("#buybox .a-button-inner input[type='submit']")
    )
    if atc:
        result["add_to_cart"] = atc.get("value") or atc.get("aria-label") or atc.get_text(strip=True)
    else:
        unavailable = (
            soup.select_one("#outOfStock")
            or soup.select_one("#availability span.a-declarative")
            or soup.select_one('[data-feature-name="availability"] .a-size-medium')
        )
        if unavailable:
            result["add_to_cart"] = "Unavailable"
            result["price_raw"] = "不可售"

    # 5. Sold By
    sold_by_el = (
        soup.select_one("#merchant-info")
        or soup.select_one("#soldByThirdParty")
        or soup.select_one('[data-feature-name="merchantInfo"]')
        or soup.select_one("#sellerProfileTriggerId")
        or soup.select_one("#tabular-buybox-truncate-0 .tabular-buybox-text")
        or soup.select_one(".offer-merchant-id")
    )
    if sold_by_el:
        result["sold_by"] = sold_by_el.get_text(strip=True)

    # 6. Breadcrumb / category
    breadcrumb = (
        soup.select_one("#wayfinding-breadcrumbs_feature_div")
        or soup.select_one("#breadcrumb-container")
        or soup.select_one("#breadcrumb")
    )
    if breadcrumb:
        crumbs = breadcrumb.select("a, .a-breadcrumb-crumb, .a-breadcrumb-piece")
        result["breadcrumb"] = " > ".join(
            c.get_text(strip=True) for c in crumbs
        )
    if not result["breadcrumb"]:
        alt_breadcrumb = soup.select(".a-breadcrumb-piece")
        if alt_breadcrumb:
            result["breadcrumb"] = " > ".join(
                c.get_text(strip=True) for c in alt_breadcrumb
            )

    # 7. Variations
    variation_container = (
        soup.select_one("#twister")
        or soup.select_one('[id*="variation"]')
        or soup.select_one(".twisterContainer")
        or soup.select_one("#variation-size")
        or soup.select_one("#variation-color")
        or soup.select_one("#variation-style")
        or soup.select_one(".a-variation-container")
    )
    if variation_container:
        labels = variation_container.select(".a-form-label, .a-size-small.a-color-secondary")
        options = variation_container.select("option:not([value=\"-1\"])")
        swatches_selected = variation_container.select(".swatchSelect, .a-button-selected")
        parts = []
        for label in labels:
            parts.append(label.get_text(strip=True))
        if options:
            opts_text = ", ".join(o.get_text(strip=True) for o in options[:20])
            parts.append(f"[{opts_text}]")
        if swatches_selected:
            parts.append("Selected: " + ", ".join(s.get_text(strip=True) for s in swatches_selected))
        result["variations"] = " | ".join(parts)

    # 8. Reviews
    rating_el = (
        soup.select_one(".a-icon-alt")
        or soup.select_one("#acrPopover")
        or soup.select_one('[data-hook="rating-out-of-text"]')
        or soup.select_one("i[data-hook='review-star-rating'] .a-icon-alt")
        or soup.select_one("span[data-hook='rating-out-of-text']")
    )
    if rating_el:
        text = rating_el.get_text(strip=True)
        m = re.search(r"[\d.]+", text)
        if m:
            result["rating"] = m.group()

    review_count_el = (
        soup.select_one("#acrCustomerReviewText")
        or soup.select_one('[data-hook="total-review-count"]')
        or soup.select_one("#ratings-summary .totalReviewCount")
        or soup.select_one("span#acrCustomerReviewText")
    )
    if review_count_el:
        text = review_count_el.get_text(strip=True)
        # "1,234 ratings" / "50 global ratings" → extract number
        m = re.search(r"[\d,]+", text)
        if m:
            result["review_count"] = m.group().replace(",", "")

    # 9. Best Sellers Rank
    # Common markup patterns:
    #   <li id="SalesRank">Best Sellers Rank: #123,456 in Category</li>
    #   <span>Best Sellers Rank:</span> <span>#123,456 in Category</span>
    #   <th>Best Sellers Rank</th> <td>#123,456 in Category</td>
    sales_rank_el = (
        soup.select_one("#SalesRank")
        or soup.select_one('#detailBulletsWrapper_feature_div li:-soup-contains("Best Sellers Rank")')
        or soup.select_one('[data-feature-name="detailBullets"] li:-soup-contains("Best Sellers Rank")')
    )
    if sales_rank_el:
        result["sales_rank"] = sales_rank_el.get_text(strip=True)
    if not result["sales_rank"]:
        # Fallback: search for "Best Sellers Rank" text on the page
        bsr_label = soup.find(string=re.compile(r"Best\s+Sellers?\s+Rank", re.I))
        if bsr_label:
            parent = bsr_label.find_parent()
            if parent:
                full_text = parent.get_text(strip=True)
                # Extract just the rank portion: "#X in Category" or "#X"
                m = re.search(r"(?:Best\s+Sellers?\s+Rank[:\s]*)?(#[\d,]+(?:\s+in\s+.+)?)", full_text, re.I)
                if m:
                    result["sales_rank"] = m.group(1).strip()

    return result


# ── Comparison ──────────────────────────────────────────────────────
def compare(current: dict, baseline: dict | None) -> list[str]:
    """Compare current data vs baseline. Returns list of changed field names."""
    if baseline is None:
        return []  # first run, no comparison

    text_fields = ["title", "bullet_points", "add_to_cart", "breadcrumb", "variations", "sales_rank"]
    changes = []

    # sold_by: compare only the core seller name, ignoring parenthetical
    # suffixes like (FBA) or (FBM) that MCP adds
    old_seller = _normalize_seller(baseline.get("sold_by"))
    new_seller = _normalize_seller(current.get("sold_by"))
    if old_seller and new_seller and old_seller != new_seller:
        changes.append("sold_by")

    # price_raw: compare when both are the same currency.
    # Cross-currency (CNY vs USD) is skipped — it's an IP artifact
    # that self-cleans as USD overwrites the old CNY baseline.
    if normalize_text(current.get("add_to_cart")) not in ("", "unavailable"):
        old_raw = str(baseline.get("price_raw", "") or "").strip()
        new_raw = str(current.get("price_raw", "") or "").strip()
        old_is_cny = old_raw.startswith("CNY") or old_raw.startswith("¥")
        new_is_cny = new_raw.startswith("CNY") or new_raw.startswith("¥")
        if old_is_cny == new_is_cny and old_raw and new_raw:
            old_price = _normalize_price(old_raw)
            new_price = _normalize_price(new_raw)
            if old_price is not None and new_price is not None:
                if abs(old_price - new_price) >= 0.05:
                    changes.append("price_raw")

    for f in text_fields:
        old_val = normalize_text(baseline.get(f))
        new_val = normalize_text(current.get(f))
        if old_val and new_val and old_val != new_val:
            changes.append(f)

    # Rating: round to 1 decimal, change if |diff| >= 0.1 (both must be present)
    old_rating = _parse_num(baseline.get("rating"))
    new_rating = _parse_num(current.get("rating"))
    if old_rating is not None and new_rating is not None:
        if abs(round(new_rating, 1) - round(old_rating, 1)) >= 0.1:
            changes.append("rating")

    # Review count: alert only if decrease >= 6 or increase > 10 (both must be present)
    old_count = _parse_num(baseline.get("review_count"))
    new_count = _parse_num(current.get("review_count"))
    if old_count is not None and new_count is not None:
        diff = new_count - old_count
        if diff <= -6 or diff > 10:
            changes.append("review_count")

    return changes


def is_variant_switch(changed_fields: list[str]) -> bool:
    """Detect likely variant switch: when title changes together with price OR
    bullets, the same URL is likely serving a different child ASIN's data.
    Title is the strongest identity signal — if it changes alongside another
    core field, it's almost certainly a different variant, not a listing edit.
    This is NOT a real listing update — do NOT overwrite the baseline."""
    fields = set(changed_fields)
    return "title" in fields and ("price_raw" in fields or "bullet_points" in fields)


def _normalize_seller(val) -> str:
    """Extract core seller name, stripping parenthetical suffixes like (FBA)."""
    if val is None:
        return ""
    text = str(val).strip()
    # Remove common suffixes: (FBA), (FBM), (Amazon), etc.
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    return normalize_text(text)


def _normalize_price(val) -> float | None:
    """Parse a price string (USD or CNY) and return a numeric USD value.
    '$19.99' → 19.99, 'CNY120.94' → ~16.67 (at ~7.25 rate).
    Returns None on parse failure."""
    if val is None or str(val).strip() == "":
        return None
    text = str(val).strip()
    try:
        if text.startswith("CNY") or text.startswith("¥"):
            num = float(re.sub(r"[^0-9.]", "", text.replace("CNY", "").replace("¥", "")))
            return round(num / 7.25, 2)
        else:
            num = float(re.sub(r"[^0-9.]", "", text.replace("$", "")))
            return num
    except ValueError:
        return None


def _parse_num(val) -> float | None:
    """Parse a numeric string. Returns None on failure."""
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(str(val).strip())
    except ValueError:
        return None


def format_change_detail(field: str, old_data: dict, new_data: dict) -> str:
    """Human-readable change description for a single field."""
    if field == "is_promo":
        label = "开始促销" if new_data.get("is_promo") == 1 else "结束促销"
        return f"促销状态: {label}"
    if field == "price_raw":
        if new_data.get("is_promo"):
            return f"价格变化（促销中）: {old_data.get('price_raw')} → {new_data.get('price_raw')}"
        return f"价格变化: {old_data.get('price_raw')} → {new_data.get('price_raw')}"
    if field == "sales_rank":
        return _format_sales_rank_change(
            str(old_data.get("sales_rank", "") or ""),
            str(new_data.get("sales_rank", "") or ""),
        )
    if field == "variations":
        return _format_variations_change(
            str(old_data.get("variations", "") or ""),
            str(new_data.get("variations", "") or ""),
        )
    field_labels = {
        "title": "标题变化",
        "bullet_points": "五点变化",
        "add_to_cart": "购物车变化",
        "sold_by": "Sold By 变化",
        "breadcrumb": "类目变化",
        "rating": "评分变化",
        "review_count": "评论数变化",
    }
    label = field_labels.get(field, f"{field} 变化")
    old_val = str(old_data.get(field, "") or "").strip()
    new_val = str(new_data.get(field, "") or "").strip()
    # Truncate long values for message
    if len(old_val) > 50 or len(new_val) > 50:
        return f"{label}"
    return f"{label}: {old_val} → {new_val}"


def _parse_rank_parts(rank_str: str) -> dict[str, str]:
    """Parse a sales_rank string like '#97,116 in Category | #272 in SubCat'
    into a dict mapping {label: rank_number}."""
    # Strip brackets and other noise characters
    rank_str = rank_str.strip("[] ")
    parts = {}
    for segment in rank_str.split(" | "):
        m = re.match(r"#([\d,]+)\s+in\s+(.+)", segment.strip())
        if m:
            label = m.group(2).strip()
            rank = m.group(1)
            parts[label] = rank
    return parts


def _format_variations_change(old_str: str, new_str: str) -> str:
    """Format variation count change with reason.
    '47 variations → 48 variations' → '变体变化: 47→48（子体上架）'"""
    old_num = _parse_num(old_str.replace("variations", ""))
    new_num = _parse_num(new_str.replace("variations", ""))
    if old_num is not None and new_num is not None:
        diff = int(new_num - old_num)
        if 1 <= diff <= 2:
            reason = "子体上架"
        elif -2 <= diff <= -1:
            reason = "子体缺货下架"
        else:
            reason = ""
        arrow = f"{int(old_num)}→{int(new_num)}"
        if reason:
            return f"变体变化: {arrow}（{reason}）"
        return f"变体变化: {arrow}"
    return f"变体变化: {old_str} → {new_str}"


def _format_sales_rank_change(old_str: str, new_str: str) -> str:
    """Format BSR change showing all subcategory ranks.
    Changed ranks get arrows, unchanged ones show current rank for context.
    Old: '#4 in Spade | #1199 in Electrical'
    New: '#4 in Spade | #1279 in Electrical'
    → 'Spade Terminals: #4；Electrical Equipment: 1199→1279↓'"""
    old_parts = _parse_rank_parts(old_str)
    new_parts = _parse_rank_parts(new_str)

    parts = []
    for label in new_parts:
        new_rank = new_parts[label]
        old_rank = old_parts.get(label)
        if old_rank and old_rank != new_rank:
            direction = "↑" if int(new_rank.replace(",", "")) < int(old_rank.replace(",", "")) else "↓"
            parts.append(f"{label}: {old_rank}→{new_rank}{direction}")
        elif not old_rank:
            parts.append(f"{label}: 新增 #{new_rank}")
        else:
            parts.append(f"{label}: #{new_rank}")

    # Also note categories that disappeared
    for label in old_parts:
        if label not in new_parts:
            parts.append(f"{label}: 已移除")

    if not parts:
        return "销售排名变化（具体数据变动）"

    return "销售排名: " + "；".join(parts)


# ── Feishu Integration ──────────────────────────────────────────────

class FeishuClient:
    """Lightweight Feishu API client using raw HTTP (no SDK dependencies)."""

    BASE = "https://open.feishu.cn"

    def __init__(self):
        self._token = None
        self._token_expiry = 0

    def _ensure_token(self):
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token

        resp = requests.post(
            f"{self.BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": FEISHU_APP_ID,
                "app_secret": FEISHU_APP_SECRET,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu auth error: {data}")
        self._token = data["tenant_access_token"]
        self._token_expiry = now + data.get("expire", 7200)
        return self._token

    def _req(self, method: str, path: str, body: dict | None = None) -> dict | None:
        token = self._ensure_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        url = f"{self.BASE}{path}"
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=15)
            elif method == "PUT":
                resp = requests.put(url, headers=headers, json=body, timeout=15)
            else:
                resp = requests.post(url, headers=headers, json=body, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[WARN] Feishu HTTP error [{path}]: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"  Response: {e.response.text[:500]}")
            return None
        data = resp.json()
        if data.get("code") != 0:
            print(f"[WARN] Feishu API error [{path}]: {data.get('code')} {data.get('msg')}")
            return None
        return data


def read_asin_list(client: FeishuClient) -> dict[str, dict]:
    """Read ASINs (col A), names (col B), parent names (col C), and
    product lines (col D) from the source sheet.
    Returns dict: asin -> {name, parent_name, product_line}."""
    path = (
        f"/open-apis/sheets/v2/spreadsheets/{SOURCE_SHEET_TOKEN}"
        f"/values/{SOURCE_SHEET_ID}!A:D"
    )
    data = client._req("GET", path)
    if data is None:
        print("[ERROR] Cannot read ASIN list from Feishu")
        sys.exit(1)
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    asin_map: dict[str, dict] = {}
    for row in values:
        if row:
            val = str(row[0]).strip()
            if re.match(r"^B[A-Z0-9]{9}$", val):
                name = str(row[1]).strip() if len(row) >= 2 else ""
                parent_name = str(row[2]).strip() if len(row) >= 3 else ""
                product_line = str(row[3]).strip() if len(row) >= 4 else ""
                asin_map[val] = {
                    "name": name,
                    "parent_name": parent_name,
                    "product_line": product_line,
                }
    return asin_map


def write_result_rows(client: FeishuClient, rows: list[dict]):
    """Overwrite the result sheet with today's change-log data (clears old rows via PUT)."""
    today = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    path = f"/open-apis/sheets/v2/spreadsheets/{RESULT_SHEET_TOKEN}/values"
    header = [["ASIN", "检查时间", "变化字段", "旧值", "新值"]]
    data_rows = [
        [r["asin"], today, r["field"], r["old_value"], r["new_value"]]
        for r in rows
    ]
    # Pad with empty rows to overwrite any leftover data from previous days
    total_rows = header + data_rows
    while len(total_rows) < 200:
        total_rows.append(["", "", "", "", ""])
    body = {
        "valueRange": {
            "range": f"{RESULT_SHEET_ID}!A1:E{len(total_rows)}",
            "values": total_rows,
        }
    }
    client._req("PUT", path, body)


def send_feishu_card(client: FeishuClient, card: dict):
    """Send an interactive card message to one or more chat IDs (comma-separated)."""
    for chat_id in FEISHU_CHAT_ID.split(","):
        chat_id = chat_id.strip()
        path = "/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        }
        client._req("POST", path, body)


# ── SellerSprite MCP Client ────────────────────────────────────────────

class SellerSpriteClient:
    """Thin wrapper around SellerSprite MCP for asin_detail calls."""

    def __init__(self):
        self._base = SELLERSPRITE_MCP_URL
        self._secret = SELLERSPRITE_SECRET_KEY
        self._req_headers = {
            "Content-Type": "application/json",
            "secret-key": self._secret,
            "Accept": "application/json, text/event-stream",
        }
        self._last_init = 0.0
        self._init_session()

    def _init_session(self):
        """Initialize MCP session (required before any tool call)."""
        self._last_init = time.time()
        try:
            r = requests.post(
                self._base,
                headers=self._req_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "amazon-monitor", "version": "1.0"},
                    },
                },
                timeout=15,
            )
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        return False

    def get_asin_detail(self, asin: str, marketplace: str = "US") -> dict | None:
        """Fetch product detail from SellerSprite. Returns dict with
        nodeLabelPath, subcategories, bsrRank, bsrLabel, etc.
        Returns None on any error."""
        # Renew session every 30 minutes to avoid silent expiry
        if time.time() - self._last_init > 1800:
            self._init_session()
        try:
            r = requests.post(
                self._base,
                headers=self._req_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "asin_detail",
                        "arguments": {"asin": asin, "marketplace": marketplace},
                    },
                },
                timeout=30,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            content = data.get("result", {}).get("content", [])
            if not content:
                return None
            text = content[0].get("text", "")
            if not text:
                return None
            parsed = json.loads(text)
            if parsed.get("code") != "OK":
                # If session expired, re-init and retry once
                if "Unauthenticated" in str(parsed) or "token" in str(parsed).lower():
                    if self._init_session():
                        return self.get_asin_detail(asin, marketplace)
                return None
            return parsed.get("data", {})
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError):
            return None


def _trigger_github_actions():
    """Fire a repository_dispatch to run the monitoring workflow on GitHub
    Actions (US IP). Used as a parallel backup when local IP is rate-limited."""
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
    if not token:
        print("  [DISPATCH] No GITHUB_DISPATCH_TOKEN set — skipping Actions trigger")
        return
    try:
        r = requests.post(
            f"https://api.github.com/repos/{os.environ.get('GITHUB_REPO', 'Yyx-create01/amazon-monitor')}/dispatches",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"event_type": "daily-check"},
            timeout=10,
        )
        if r.status_code == 204:
            print("  [DISPATCH] GitHub Actions triggered (US IP backup)")
        else:
            print(f"  [DISPATCH] Failed: {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        print(f"  [DISPATCH] Error: {e}")


def format_breadcrumb_from_mcp(node_label_path: str) -> str:
    """Convert MCP nodeLabelPath to breadcrumb format.
    'A:B:C:D' → 'A > B > C > D'"""
    if not node_label_path:
        return ""
    return node_label_path.replace(":", " > ")


def format_sales_rank_from_mcp(detail: dict) -> str:
    """Extract subcategory BSR from asin_detail response.
    Format: '#272 in Heat-Shrink Tubing | #659 in Tubing'"""
    parts = []
    for sub in (detail.get("subcategories") or []):
        rank = sub.get("rank")
        label = sub.get("label")
        if rank is not None and label:
            parts.append(f"#{rank} in {label}")
    return " | ".join(parts) if parts else ""


def enrich_from_mcp(current: dict, mcp_detail: dict) -> list[str]:
    """Overlay MCP fields onto current dict. Returns list of overridden field names.
    Only overrides when MCP value is non-empty and differs from current."""
    overrides = []
    # Title
    mcp_title = mcp_detail.get("title", "")
    if mcp_title and mcp_title != current.get("title", ""):
        current["title"] = mcp_title
        overrides.append("title")
    # Price
    mcp_price = mcp_detail.get("price")
    if mcp_price is not None and mcp_price > 0:
        price_str = f"${mcp_price:.2f}"
        if price_str != current.get("price_raw", ""):
            current["price_raw"] = price_str
            overrides.append("price")
    # Bullet points (features)
    features = mcp_detail.get("features", [])
    if features and isinstance(features, list):
        mcp_bullets = " || ".join(f for f in features if f)
        if mcp_bullets and mcp_bullets != current.get("bullet_points", ""):
            current["bullet_points"] = mcp_bullets
            overrides.append("bullets")
    # Sold by
    seller = mcp_detail.get("sellerName", "")
    if seller:
        mcp_sold_by = seller
        if mcp_sold_by != current.get("sold_by", ""):
            current["sold_by"] = mcp_sold_by
            overrides.append("sold_by")
    # Breadcrumb from nodeLabelPath
    mcp_breadcrumb = format_breadcrumb_from_mcp(
        mcp_detail.get("nodeLabelPath", "")
    )
    if mcp_breadcrumb and mcp_breadcrumb != current.get("breadcrumb", ""):
        current["breadcrumb"] = mcp_breadcrumb
        overrides.append("breadcrumb")
    # Rating
    mcp_rating = mcp_detail.get("rating")
    if mcp_rating is not None and mcp_rating > 0:
        rating_str = str(mcp_rating)
        if rating_str != current.get("rating", ""):
            current["rating"] = rating_str
            overrides.append("rating")
    # Review count
    mcp_ratings = mcp_detail.get("ratings")
    if mcp_ratings is not None and mcp_ratings > 0:
        reviews_str = str(mcp_ratings)
        if reviews_str != current.get("review_count", ""):
            current["review_count"] = reviews_str
            overrides.append("reviews")
    # Sales rank
    mcp_sales_rank = format_sales_rank_from_mcp(mcp_detail)
    if mcp_sales_rank and mcp_sales_rank != current.get("sales_rank", ""):
        current["sales_rank"] = mcp_sales_rank
        overrides.append("sales_rank")
    # Variations count (MCP gives count, not labels — only use if static is empty)
    mcp_variations = mcp_detail.get("variations")
    if mcp_variations is not None and not current.get("variations", ""):
        current["variations"] = f"{mcp_variations} variations"
        overrides.append("variations")
    return overrides


# ── Main ────────────────────────────────────────────────────────────
def main():
    # Rotate logs: keep last 7 days, delete older
    log_file = BASE_DIR / "monitor.log"
    if log_file.exists():
        rotated = BASE_DIR / f"monitor_{datetime.now(BJT).strftime('%Y%m%d')}.log"
        if not rotated.exists():
            log_file.rename(rotated)
        # Clean up logs older than 7 days
        for old_log in sorted(BASE_DIR.glob("monitor_*.log")):
            try:
                date_str = old_log.stem.replace("monitor_", "")
                log_date = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=BJT)
                if (datetime.now(BJT) - log_date).days > 7:
                    old_log.unlink()
            except (ValueError, OSError):
                pass

    print(f"[{datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')}] Starting Amazon product monitor...")
    conn = init_db()
    client = FeishuClient()
    session = requests.Session()
    sellersprite = SellerSpriteClient()

    # 1. Read ASIN list from Feishu
    asins = read_asin_list(client)
    print(f"  Read {len(asins)} ASINs from source sheet")

    # Batch support: --batch 1/3 processes only the first third of ASINs.
    # Use --batch N/TOTAL to split the run across multiple time windows
    # and reduce request density. E.g. --batch 1/3, --batch 2/3, --batch 3/3.
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=str, default="", help="Batch N/TOTAL, e.g. 1/3")
    parser.add_argument("--mcp-only", action="store_true", help="Skip static scraping, use MCP for everything")
    parser.add_argument("--dry-run", action="store_true", help="Test mode: no save, no notification")
    args, _ = parser.parse_known_args()
    batch_num = batch_total = None
    if args.batch and "/" in args.batch:
        parts = args.batch.split("/")
        batch_num = int(parts[0])
        batch_total = int(parts[1])

    # Shuffle to avoid predictable scan patterns (same brand, sorted order)
    shuffled = list(asins.items())
    random.shuffle(shuffled)

    # Slice for batch mode
    if batch_num and batch_total:
        chunk_size = (len(shuffled) + batch_total - 1) // batch_total
        start = (batch_num - 1) * chunk_size
        end = min(batch_num * chunk_size, len(shuffled))
        shuffled = shuffled[start:end]
        batch_label = f" [batch {batch_num}/{batch_total}]"
        print(f"  Batch {batch_num}/{batch_total}: {len(shuffled)} ASINs (indices {start}-{end-1})")
    else:
        batch_label = ""

    if not asins:
        print("  No ASINs found. Exiting.")
        session.close()
        conn.close()
        return

    # 2. Process each ASIN
    changes_found: list[dict] = []  # for Feishu result table
    asin_changes: dict[str, list[str]] = {}  # asin → list of change details
    field_groups: dict[str, list[dict]] = {}  # field_type → [{asin, detail}]
    total = len(asins)
    empty_title_streak = 0  # consecutive ASINs with empty title (possible bot check)
    cooldown_count = 0      # how many times we've paused due to rate limiting
    skipped_by_rate_limit = 0  # ASINs not checked because run was aborted
    empty_data_count = 0       # ASINs with empty scrape (rate-limited)

    # Parent ASIN cache — BSR is per-parent, not per-child.
    # After the first child of a parent is processed, all siblings are
    # discovered via MCP's variationList and their BSR reused.
    asin_to_parent: dict[str, str] = {}  # child ASIN → parent ASIN
    parent_bsr: dict[str, dict[str, str]] = {}  # parent → {breadcrumb, sales_rank, variations}
    mcp_calls_saved = 0
    mcp_only_mode = args.mcp_only  # manual mode: skip static scrape, use MCP
    dry_run = args.dry_run        # test mode: no save, no notification
    actions_triggered = False  # whether we've asked GitHub Actions to take over

    for i, (asin, _name) in enumerate(shuffled, 1):
        # Clear cookies every 25 ASINs to reduce tracking
        if i % 25 == 0:
            session.cookies.clear()
        print(f"  [{i}/{total}] Checking {asin}...")

        # MCP-only mode: Amazon rate-limited us — skip static scrape entirely.
        if mcp_only_mode:
            mcp_detail = sellersprite.get_asin_detail(asin)
            if mcp_detail and mcp_detail.get("title"):
                current = {
                    "title": "", "price_raw": "", "is_promo": 0,
                    "bullet_points": "", "add_to_cart": "", "sold_by": "",
                    "breadcrumb": "", "variations": "", "rating": "",
                    "review_count": "", "sales_rank": "",
                }
                enriched = enrich_from_mcp(current, mcp_detail)
                if current.get("title"):
                    print(f"    MCP-only: {', '.join(enriched)}")
                else:
                    print(f"    MCP-only: no data, skipping")
                    continue
            else:
                print(f"    MCP-only: unavailable, skipping")
                continue
            # Jump straight to baseline logic
            baseline = load_baseline(conn, asin)
            if baseline is None:
                insert_baseline(conn, {
                    "asin": asin, **current,
                    "updated_at": datetime.now(BJT).isoformat(),
                })
                print(f"    Baseline stored")
            else:
                changed_fields = compare(current, baseline)
                if not is_variant_switch(changed_fields) and changed_fields:
                    details = []
                    for field in changed_fields:
                        detail = format_change_detail(field, baseline, current)
                        print(f"    CHANGE: {detail}")
                        changes_found.append({
                            "asin": asin,
                            "field": FIELD_LABELS.get(field, field),
                            "old_value": str(baseline.get(field, "") or "").strip()[:200],
                            "new_value": str(current.get(field, "") or "").strip()[:200],
                        })
                        details.append(detail)
                        field_groups.setdefault(field, []).append({"asin": asin, "detail": detail})
                    asin_changes[asin] = details
                save_baseline(conn, {
                    "asin": asin, **current,
                    "updated_at": datetime.now(BJT).isoformat(),
                })
                if not changed_fields:
                    print(f"    No changes")
            if i < total:
                time.sleep(0.5)  # short delay between MCP-only calls
            continue

        soup, redirected = fetch_page(asin, session)

        # ── Path A: Fetch completely failed ──────────────────────────
        if soup is None:
            # Try MCP as last resort — it's independent of Amazon scraping.
            mcp_detail = sellersprite.get_asin_detail(asin)
            if mcp_detail and mcp_detail.get("title"):
                current = {
                    "title": "", "price_raw": "", "is_promo": 0,
                    "bullet_points": "", "add_to_cart": "", "sold_by": "",
                    "breadcrumb": "", "variations": "", "rating": "",
                    "review_count": "", "sales_rank": "",
                }
                enriched = enrich_from_mcp(current, mcp_detail)
                if current.get("title"):
                    print(f"    MCP rescued (fetch failed): {', '.join(enriched)}")
                    # Fall through to baseline logic below
                else:
                    print(f"    SKIP: page fetch failed, MCP also unavailable")
                    continue
            else:
                print(f"    SKIP: page fetch failed, MCP also unavailable")
                continue

        # ── Path B: Redirected → OOS ─────────────────────────────────
        elif redirected:
            baseline = load_baseline(conn, asin)
            oos_data = {
                "asin": asin,
                "title": "不可售（重定向到子体）",
                "price_raw": "不可售",
                "is_promo": 0,
                "bullet_points": "",
                "add_to_cart": "Unavailable",
                "sold_by": "",
                "breadcrumb": "",
                "variations": "",
                "rating": "",
                "review_count": "",
                "sales_rank": "",
                "updated_at": datetime.now(BJT).isoformat(),
            }
            # Enrich OOS data with MCP (breadcrumb + sales_rank) so
            # these fields aren't wiped to empty in the baseline.
            mcp_detail = sellersprite.get_asin_detail(asin)
            if mcp_detail:
                mcp_breadcrumb = format_breadcrumb_from_mcp(
                    mcp_detail.get("nodeLabelPath", "")
                )
                if mcp_breadcrumb:
                    oos_data["breadcrumb"] = mcp_breadcrumb
                mcp_sales_rank = format_sales_rank_from_mcp(mcp_detail)
                if mcp_sales_rank:
                    oos_data["sales_rank"] = mcp_sales_rank
            if baseline is None:
                insert_baseline(conn, oos_data)
                print(f"    OOS (redirected) — baseline stored")
            else:
                changed_fields = compare(oos_data, baseline)
                if changed_fields:
                    details: list[str] = []
                    for field in changed_fields:
                        detail = format_change_detail(field, baseline, oos_data)
                        print(f"    CHANGE: {detail}")
                        changes_found.append({
                            "asin": asin,
                            "field": FIELD_LABELS.get(field, field),
                            "old_value": str(baseline.get(field, "") or "").strip()[:200],
                            "new_value": str(oos_data.get(field, "") or "").strip()[:200],
                        })
                        details.append(detail)
                        field_groups.setdefault(field, []).append({"asin": asin, "detail": detail})
                    asin_changes[asin] = details
                save_baseline(conn, oos_data)
                if not changed_fields:
                    print(f"    OOS (redirected) — no changes")
            if i < total:
                time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                if INTERNAL_BATCH_SIZE > 0 and i % INTERNAL_BATCH_SIZE == 0:
                    time.sleep(INTERNAL_BATCH_PAUSE)
            continue

        # ── Path C: Normal page ─────────────────────────────────────
        else:
            current = parse_product(soup)

            # Detect potential bot blocking: consecutive empty titles
            if not current["title"]:
                empty_title_streak += 1
                empty_data_count += 1
                print(f"    Empty title (streak={empty_title_streak})")
                if empty_title_streak >= 3 and not actions_triggered:
                    # Rate-limited — trigger GitHub Actions (US IP)
                    # as the primary fallback.
                    _trigger_github_actions()
                    actions_triggered = True
                    skipped_by_rate_limit = total - i
                    print(f"  [FALLBACK] Rate-limited. "
                          f"GitHub Actions triggered (US IP). "
                          f"Skipping remaining {skipped_by_rate_limit} ASINs locally.")
                    break
                    empty_title_streak = 0
            else:
                if empty_title_streak >= 3:
                    print(f"  [INFO] Parsing resumed after empty streak — "
                          "temporary block cleared.")
                empty_title_streak = 0

            # Enrich with SellerSprite MCP.
            # BSR is per-parent, not per-child — skip MCP for siblings
            # whose parent we've already processed.
            known_parent = asin_to_parent.get(asin)
            if known_parent and known_parent in parent_bsr:
                # Reuse cached data from the parent's first child.
                # BSR, breadcrumb, and variations are all per-parent, not per-child.
                cached = parent_bsr[known_parent]
                if cached.get("breadcrumb"):
                    current["breadcrumb"] = cached["breadcrumb"]
                if cached.get("sales_rank"):
                    current["sales_rank"] = cached["sales_rank"]
                if cached.get("variations"):
                    current["variations"] = cached["variations"]
                mcp_calls_saved += 1
                print(f"    MCP skip: parent {known_parent} already processed (saved #{mcp_calls_saved})")
            else:
                mcp_detail = sellersprite.get_asin_detail(asin)
                if mcp_detail:
                    enriched = enrich_from_mcp(current, mcp_detail)
                    if enriched:
                        print(f"    MCP enriched: {', '.join(enriched)}")
                    # Cache parent and siblings for future ASINs.
                    # Also mark the parent ASIN itself so it won't
                    # trigger another MCP call when encountered later.
                    parent = mcp_detail.get("parent")
                    if parent:
                        asin_to_parent[asin] = parent
                        asin_to_parent[parent] = parent  # parent → self
                        parent_bsr[parent] = {
                            "breadcrumb": current.get("breadcrumb", ""),
                            "sales_rank": current.get("sales_rank", ""),
                            "variations": current.get("variations", ""),
                        }
                        # Discover and cache all sibling ASINs from variationList
                        for var in (mcp_detail.get("variationList") or []):
                            child_asin = var.get("asin")
                            if child_asin:
                                asin_to_parent[child_asin] = parent
                else:
                    print(f"    MCP: unavailable, falling back to static scrape")

        # ── Baseline logic (shared by Path A + Path C) ──────────────
        baseline = load_baseline(conn, asin)

        # CNY prices are an IP artifact — never let them into the baseline.
        # If old baseline is USD and new is CNY, strip new so USD is preserved.
        # If old is also CNY, let both stay so comparison still works.
        price_raw = str(current.get("price_raw", "") or "").strip()
        if price_raw.startswith("CNY") or price_raw.startswith("¥"):
            old_raw = str(baseline.get("price_raw", "") or "").strip() if baseline else ""
            if not (old_raw.startswith("CNY") or old_raw.startswith("¥")):
                current["price_raw"] = ""

        # Skip baseline update when no valid data from any source.
        if not current.get("title", ""):
            print(f"    SKIP baseline: no valid data from any source")

        elif baseline is None:
            # First run — store baseline silently
            insert_baseline(conn, {
                "asin": asin,
                **current,
                "updated_at": datetime.now(BJT).isoformat(),
            })
            print(f"    Baseline stored")
        else:
            changed_fields = compare(current, baseline)

            if is_variant_switch(changed_fields):
                print(f"    Variant switch detected, skipping (no alert)")

            else:
                if changed_fields:
                    details: list[str] = []
                    for field in changed_fields:
                        detail = format_change_detail(field, baseline, current)
                        print(f"    CHANGE: {detail}")
                        changes_found.append({
                            "asin": asin,
                            "field": FIELD_LABELS.get(field, field),
                            "old_value": str(baseline.get(field, "") or "").strip()[:200],
                            "new_value": str(current.get(field, "") or "").strip()[:200],
                        })
                        details.append(detail)
                        field_groups.setdefault(field, []).append({"asin": asin, "detail": detail})
                    asin_changes[asin] = details

                save_baseline(conn, {
                    "asin": asin,
                    **current,
                    "updated_at": datetime.now(BJT).isoformat(),
                })

                if not changed_fields:
                    print(f"    No changes")

        # Random delay between ASINs (skip after last one)
        if i < total:
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            # Internal batching: pause between chunks to reduce request density
            if INTERNAL_BATCH_SIZE > 0 and i % INTERNAL_BATCH_SIZE == 0:
                print(f"  [BATCH PAUSE] {INTERNAL_BATCH_PAUSE}s cooldown "
                      f"(ASINs {i-INTERNAL_BATCH_SIZE+1}-{i} done)...")
                time.sleep(INTERNAL_BATCH_PAUSE)

    # 3. Batch result accumulation
    today_str = datetime.now(BJT).strftime("%Y-%m-%d")
    batch_file = BASE_DIR / f"batch_results_{today_str.replace(chr(45), '')}.json"

    is_final_batch = (not batch_num) or (batch_num == batch_total)
    if not is_final_batch:
        save_data = {
            "changes_found": changes_found,
            "field_groups": {k: v for k, v in field_groups.items()},
            "asin_changes": dict(asin_changes),
            "mcp_calls_saved": mcp_calls_saved,
            "skipped": skipped_by_rate_limit,
            "actions": actions_triggered,
        }
        with open(batch_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False)
        print(f"  Batch {batch_num}/{batch_total} saved for final summary")
    else:
        if batch_file.exists():
            with open(batch_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            changes_found.extend(saved.get("changes_found", []))
            for k, v in saved.get("field_groups", {}).items():
                field_groups.setdefault(k, []).extend(v)
            for k, v in saved.get("asin_changes", {}).items():
                asin_changes.setdefault(k, []).extend(v)
            mcp_calls_saved += saved.get("mcp_calls_saved", 0)
            skipped_by_rate_limit = max(skipped_by_rate_limit, saved.get("skipped", 0))
            actions_triggered = actions_triggered or saved.get("actions", False)
            batch_file.unlink()

        # Write merged results to Feishu
    # 3. Batch result accumulation
    today_str = datetime.now(BJT).strftime("%Y-%m-%d")
    batch_file = BASE_DIR / f"batch_results_{today_str.replace(chr(45), '')}.json"

    is_final_batch = (not batch_num) or (batch_num == batch_total)
    if not is_final_batch:
        save_data = {
            "changes_found": changes_found,
            "field_groups": {k: list(v) for k, v in field_groups.items()},
            "asin_changes": dict(asin_changes),
            "mcp_calls_saved": mcp_calls_saved,
            "skipped": skipped_by_rate_limit,
            "actions": actions_triggered,
        }
        with open(batch_file, "w", encoding="utf-8") as f:
            json.dump(save_data, f, ensure_ascii=False)
        print(f"  Batch {batch_num}/{batch_total} saved for final summary")
        # Skip Feishu — only final batch sends card
        print(f"[{datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')}] Done.")
        session.close()
        conn.close()
        return
    else:
        if batch_file.exists():
            with open(batch_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            changes_found.extend(saved.get("changes_found", []))
            for k, v in saved.get("field_groups", {}).items():
                field_groups.setdefault(k, []).extend(v)
            for k, v in saved.get("asin_changes", {}).items():
                asin_changes.setdefault(k, []).extend(v)
            mcp_calls_saved += saved.get("mcp_calls_saved", 0)
            skipped_by_rate_limit = max(skipped_by_rate_limit, saved.get("skipped", 0))
            actions_triggered = actions_triggered or saved.get("actions", False)
            batch_file.unlink()

        # 5. Write merged results + send summary card
        if not dry_run:
            write_result_rows(client, changes_found)
            print(f"  Wrote {len(changes_found)} changes to result sheet")
        else:
            print(f"  [DRY RUN] Would write {len(changes_found)} changes to result sheet")

    if not dry_run:
        sheet_url = f"https://zhangmen365.feishu.cn/sheets/{RESULT_SHEET_TOKEN}?sheet={RESULT_SHEET_ID}"

    checked_count = total - skipped_by_rate_limit
    summary_md_extra = ""
    if empty_data_count > 0:
        summary_md_extra = f"\n\n📡 {empty_data_count} 个 ASIN 抓取失败（限流），数据来自MCP缓存。"
    if skipped_by_rate_limit > 0:
        header_color = "red"
        header_title = f"🚫 检查中止{batch_label} — {today_str}"
        summary_md_extra += f"\n\n---\n🚫 本地被限流，剩余 **{skipped_by_rate_limit}** 个 ASIN 未检查。"
        if actions_triggered:
            summary_md_extra += "\nGitHub Actions（US IP）已触发，将接力完成。"
    elif cooldown_count > 0:
        header_color = "orange"
        header_title = f"⚠️ 检查完成（有限流波动）| {today_str}"
    elif asin_changes:
        header_color = "orange"
        header_title = f"⚠️ 每日检查完成 | {today_str}"
    else:
        header_color = "green"
        header_title = f"✅ 每日检查完成 | {today_str}"

    elements: list[dict] = []
    summary_md = f"共检查 **{checked_count}**/**{total}** 个 ASIN"

    if summary_md_extra:
        summary_md += summary_md_extra

    if asin_changes:
        summary_md += f"\n\n发现 **{len(asin_changes)}** 个 ASIN 有变化："

        # Group by field type with emoji headers
        FIELD_EMOJI = {
            "price_raw": "💰",
            "sales_rank": "📊",
            "title": "📝",
            "bullet_points": "📋",
            "breadcrumb": "🗂",
            "sold_by": "🏪",
            "add_to_cart": "🛒",
            "rating": "⭐",
            "review_count": "💬",
            "variations": "🔀",
            "rating_reviews": "⭐",
        }
        FIELD_GROUP_LABEL = {
            "price_raw": "价格变化",
            "sales_rank": "销售排名变化",
            "title": "标题变化",
            "bullet_points": "五点变化",
            "breadcrumb": "类目变化",
            "sold_by": "Sold By变化",
            "add_to_cart": "购物车变化",
            "rating": "评分变化",
            "review_count": "评论数变化",
            "variations": "变体变化",
            "rating_reviews": "评分&评论变化",
        }
        # Merge rating + review_count into a single group
        rating_entries = field_groups.pop("rating", []) + field_groups.pop("review_count", [])
        if rating_entries:
            field_groups["rating_reviews"] = rating_entries

        for field, entries in field_groups.items():
            emoji = FIELD_EMOJI.get(field, "📌")
            label = FIELD_GROUP_LABEL.get(field, f"{field}变化")

            if field in ("sales_rank", "variations"):
                # Per-parent fields — group by product line → parent name.
                # One entry per (product_line, parent) combo.
                lines: dict[str, dict[str, str]] = {}  # line → {parent → detail}
                for entry in entries:
                    info = asins.get(entry["asin"], {})
                    product_line = info.get("product_line", "")
                    if not product_line:
                        product_line = info.get("parent_name") or info.get("name") or entry["asin"]
                    parent_name = info.get("parent_name") or info.get("name") or entry["asin"]
                    detail_text = entry["detail"]
                    if ":" in detail_text:
                        detail_text = detail_text.split(":", 1)[1].strip()
                    lines.setdefault(product_line, {})
                    if parent_name not in lines[product_line]:
                        lines[product_line][parent_name] = detail_text
                total_lines = sum(len(p) for p in lines.values())
                summary_md += f"\n\n{emoji} **{label}（{total_lines}）**"
                for product_line, parents in lines.items():
                    summary_md += f"\n · **{product_line}**"
                    for parent_name, detail_text in parents.items():
                        summary_md += f"\n   - {parent_name}: {detail_text}"
            else:
                summary_md += f"\n\n{emoji} **{label}（{len(entries)}）**"
                for entry in entries:
                    a = entry["asin"]
                    name = asins.get(a, {}).get("name", "")
                    product = f"**{a}**" + (f"（{name}）" if name else "")
                    detail_text = entry["detail"]
                    if ":" in detail_text:
                        detail_text = detail_text.split(":", 1)[1].strip()
                    summary_md += f"\n · {product} — {detail_text}"
    else:
        summary_md += "\n\n全部无异常"

    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": summary_md},
    })
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看飞书表格"},
            "type": "primary",
            "url": sheet_url,
        }],
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": header_color,
        },
        "elements": elements,
    }

    if not dry_run:
        send_feishu_card(client, card)
        print(f"  Summary card sent: {header_title}")
    else:
        print(f"  [DRY RUN] Card skipped: {header_title}")

    # Clean up batch file after final card
    if batch_num and batch_num == batch_total and batch_file.exists():
        batch_file.unlink(missing_ok=True)

    print(f"  Summary card sent: {header_title}")

    session.close()
    conn.close()
    print(f"[{datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')}] Done.")


if __name__ == "__main__":
    main()
