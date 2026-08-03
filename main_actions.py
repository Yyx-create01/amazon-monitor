#!/usr/bin/env python3
"""Amazon Product Page Monitor — GitHub Actions edition (requests, no webclaw)."""

import os, re, json, sys, time, random, sqlite3, unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "baseline_actions.db"
BJT = timezone(timedelta(hours=8))

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FEISHU_TENANT = os.environ.get("FEISHU_TENANT", "xa0j7ax4q5v")
FEISHU_APP_TOKEN = os.environ["FEISHU_APP_TOKEN"]
FEISHU_SOURCE_TABLE_ID = os.environ["FEISHU_SOURCE_TABLE_ID"]
FEISHU_RESULT_TABLE_ID = os.environ["FEISHU_RESULT_TABLE_ID"]
FEISHU_BASELINE_TABLE_ID = os.environ["FEISHU_BASELINE_TABLE_ID"]
FEISHU_CHAT_ID = os.environ["FEISHU_CHAT_ID"]

REQUEST_DELAY_MIN = int(os.environ.get("REQUEST_DELAY_MIN", 5))
REQUEST_DELAY_MAX = int(os.environ.get("REQUEST_DELAY_MAX", 10))
INTERNAL_BATCH_SIZE = int(os.environ.get("BATCH_SIZE", 20))
INTERNAL_BATCH_PAUSE = int(os.environ.get("BATCH_PAUSE", 60))

AMAZON_BASE = "https://www.amazon.com/dp/"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
HEADERS_TMPL = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}

FIELD_LABELS = {
    "title": "标题", "price_raw": "价格", "is_promo": "促销状态",
    "bullet_points": "五点描述", "add_to_cart": "购物车", "sold_by": "Sold By",
    "breadcrumb": "类目节点", "variations": "变体关系", "rating": "评分",
    "review_count": "评论数", "sales_rank": "销售排名",
}

# ── Database ────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""CREATE TABLE IF NOT EXISTS baseline (
        asin TEXT PRIMARY KEY, title TEXT, price_raw TEXT, is_promo INTEGER DEFAULT 0,
        bullet_points TEXT, add_to_cart TEXT, sold_by TEXT, breadcrumb TEXT,
        variations TEXT, rating TEXT, review_count TEXT, sales_rank TEXT, updated_at TEXT)""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(baseline)")]
    if "sales_rank" not in cols:
        conn.execute("ALTER TABLE baseline ADD COLUMN sales_rank TEXT")
    conn.commit()
    return conn

def load_baseline(conn, asin):
    row = conn.execute("SELECT * FROM baseline WHERE asin=?", (asin,)).fetchone()
    if not row:
        return None
    cols = [d[1] for d in conn.execute("PRAGMA table_info(baseline)")]
    return dict(zip(cols, row))

def save_baseline(conn, data):
    fields = [k for k in data if k != "asin"]
    values = [data[f] for f in fields] + [data["asin"]]
    conn.execute(
        f"UPDATE baseline SET {', '.join(f'{f}=?' for f in fields)} WHERE asin=?",
        values)
    conn.commit()

def insert_baseline(conn, data):
    fields = list(data)
    conn.execute(
        f"INSERT INTO baseline ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
        [data[f] for f in fields])
    conn.commit()

# ── Text normalization ──────────────────────────────────────────────
def normalize_text(val):
    if val is None: return ""
    text = unicodedata.normalize("NFKC", str(val))
    return re.sub(r"\s+", " ", text).lower().strip()

# ── Scraper ─────────────────────────────────────────────────────────
def fetch_page(asin, session):
    strategies = [
        (AMAZON_BASE + asin, random.choice(USER_AGENTS)),
        (f"https://www.amazon.com/gp/aw/d/{asin}",
         "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 Version/18.5 Mobile/15E148 Safari/604.1"),
        (f"{AMAZON_BASE}{asin}?th=1&psc=1", random.choice(USER_AGENTS)),
        (f"{AMAZON_BASE}{asin}?language=en_US&currency=USD", random.choice(USER_AGENTS)),
    ]
    for attempt, (url, user_agent) in enumerate(strategies):
        headers = {
            **HEADERS_TMPL,
            "User-Agent": user_agent,
            "Cache-Control": "no-cache",
            "Cookie": "lc-main=en_US; i18n-prefs=USD",
        }
        try:
            # Clear challenge/session cookies before switching strategy.
            session.cookies.clear()
            resp = session.get(url, headers=headers, timeout=25, allow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                if soup.select_one("form[action='/errors/validateCaptcha']"):
                    print(f"    attempt {attempt+1}: CAPTCHA")
                    time.sleep(4 * (attempt + 1))
                    continue
                redirected = asin not in resp.url
                return soup, redirected
            print(f"    attempt {attempt+1}: HTTP {resp.status_code}")
            time.sleep(4 * (attempt + 1))
        except requests.RequestException as e:
            print(f"    attempt {attempt+1}: {type(e).__name__}")
            time.sleep(3 * (attempt + 1))
            continue
    return None, False

def parse_product(soup):
    result = {k: "" for k in FIELD_LABELS}
    result["is_promo"] = 0

    # Title
    t = soup.select_one("#productTitle") or soup.select_one("#title")
    if t: result["title"] = t.get_text(strip=True)

    # Price
    pe = soup.select_one(".a-price .a-offscreen")
    pw = soup.select_one(".a-price-whole")
    pf = soup.select_one(".a-price-fraction")
    if pe: result["price_raw"] = pe.get_text(strip=True)
    elif pw:
        f = pf.get_text(strip=True) if pf else "00"
        result["price_raw"] = f"${pw.get_text(strip=True)}.{f}"

    # Promo
    if any(soup.select_one(s) for s in [".dealBadge","#dealprice",'[data-a-badge="deal"]']):
        result["is_promo"] = 1
    if soup.select_one('[class*="lightning-deal"]') or soup.select_one('[id*="lightning-deal"]'):
        result["is_promo"] = 1
    pb = soup.select_one("#corePrice_desktop") or soup.select_one("#corePrice_feature_div")
    if pb:
        typ = pb.find(string=re.compile(r"Typical\s*price", re.I))
        pct = pb.find(string=re.compile(r"-\d+%", re.I))
        if typ and pct: result["is_promo"] = 1

    # Bullets
    for sel in ["#feature-bullets .a-list-item", "#feature-bullets li",
                '[data-feature-name="featurebullets"] .a-list-item',
                "#featurebullets_feature_div .a-list-item"]:
        bullets = soup.select(sel)
        if bullets:
            result["bullet_points"] = " || ".join(b.get_text(strip=True) for b in bullets)
            break

    # ATC (ATC-first to avoid geo-restriction false positives)
    atc = None
    for sel in ["#add-to-cart-button", "#desktop_buybox input[type='submit']",
                '[id="submit.add-to-cart"]', "input[name='submit.add-to-cart']",
                "#buybox .a-button-inner input[type='submit']",
                "#mobile_buybox input[type='submit']", ".a-button-stack input[type='submit']"]:
        atc = soup.select_one(sel)
        if atc: break
    if atc:
        result["add_to_cart"] = atc.get("value") or atc.get("aria-label") or atc.get("title") or atc.get_text(strip=True) or "Add to Cart"
    else:
        oos_texts = []
        for el in soup.select("#outOfStock, #outOfStockBuyBox, [data-feature-name='outOfStockBuyBox'], #availability span, [data-feature-name='availability'] span"):
            t = (el.get_text(strip=True) or "").lower()
            if t: oos_texts.append(t)
        combined = " ".join(oos_texts)
        is_oos = any(p in combined for p in ["currently unavailable","out of stock","temporarily out of stock","sold out","no longer available","discontinued"])
        is_geo = any(w in combined for w in ["cannot be shipped","shipping to","delivery location","see similar items"])
        if is_oos and not is_geo:
            result["add_to_cart"] = "Unavailable"
            result["price_raw"] = "不可售"

    # Sold By
    for sel in ["#merchant-info", "#soldByThirdParty", '[data-feature-name="merchantInfo"]',
                "#sellerProfileTriggerId", "#tabular-buybox-truncate-0 .tabular-buybox-text",
                ".offer-merchant-id"]:
        el = soup.select_one(sel)
        if el:
            raw = el.get_text(" ", strip=True)
            if raw and not any(p in raw.lower() for p in ["visit the store", "visit the brand store"]):
                result["sold_by"] = raw
                break

    # Breadcrumb
    for sel in ["#wayfinding-breadcrumbs_feature_div", "#breadcrumb-container", "#breadcrumb"]:
        bc = soup.select_one(sel)
        if bc:
            crumbs = bc.select("a, .a-breadcrumb-crumb, .a-breadcrumb-piece")
            if crumbs:
                result["breadcrumb"] = " > ".join(c.get_text(strip=True) for c in crumbs)
                break

    # Variations
    vc = soup.select_one("#twister_feature_div") or soup.select_one("#twister") or soup.select_one('[id*="variation"]')
    if vc:
        opts = vc.select("option:not([value=\"-1\"])")
        if opts:
            result["variations"] = f"{len(opts)} variations"

    # Rating / Reviews
    re_el = soup.select_one(".a-icon-alt") or soup.select_one("#acrPopover")
    if re_el:
        m = re.search(r"[\d.]+", re_el.get_text(strip=True))
        if m: result["rating"] = m.group()
    rc_el = soup.select_one("#acrCustomerReviewText") or soup.select_one('[data-hook="total-review-count"]')
    if rc_el:
        m = re.search(r"[\d,]+", rc_el.get_text(strip=True))
        if m: result["review_count"] = m.group().replace(",", "")

    # BSR
    bsr = ""
    for th in soup.find_all("th"):
        if re.match(r"^\s*Best\s+Sellers?\s+Rank\s*$", th.get_text(), re.I):
            td = th.find_next_sibling("td")
            if td: bsr = td.get_text(" ", strip=True)
            else:
                tr = th.find_parent("tr")
                if tr:
                    row_td = tr.find("td")
                    if row_td: bsr = row_td.get_text(" ", strip=True)
            if bsr: break
    if not bsr:
        leg = soup.select_one("#SalesRank")
        if leg: bsr = leg.get_text(strip=True)
    if bsr:
        cleaned = re.sub(r"^.*?Best\s+Sellers?\s+Rank\s*:?\s*", "", bsr, flags=re.I)
        cleaned = re.sub(r"\s*\(\s*See\s+Top\s+\d+[^)]*\)", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*ASIN\s*.*$", "", cleaned, flags=re.I)
        segments = [s.strip() for s in re.split(r"\s*(?=#[\d,]+)", cleaned) if s.strip()]
        ranks = [s for s in segments if re.match(r"#[\d,]+", s)]
        if ranks: result["sales_rank"] = " | ".join(ranks)
        else: result["sales_rank"] = cleaned.strip()

    return result

# ── Comparison ──────────────────────────────────────────────────────
def compare(current, baseline):
    if baseline is None: return []
    text_fields = ["title", "bullet_points", "breadcrumb", "variations", "sales_rank"]
    changes = []

    old_seller = _normalize_seller(baseline.get("sold_by"))
    new_seller = _normalize_seller(current.get("sold_by"))
    if old_seller and new_seller and old_seller != new_seller:
        changes.append("sold_by")

    old_atc = normalize_text(baseline.get("add_to_cart"))
    new_atc = normalize_text(current.get("add_to_cart"))
    if old_atc and new_atc and ("unavailable" in old_atc) != ("unavailable" in new_atc):
        changes.append("add_to_cart")

    if normalize_text(current.get("add_to_cart")) not in ("", "unavailable"):
        old_raw = str(baseline.get("price_raw","") or "").strip()
        new_raw = str(current.get("price_raw","") or "").strip()
        old_cny = old_raw.startswith("CNY") or old_raw.startswith("¥")
        new_cny = new_raw.startswith("CNY") or new_raw.startswith("¥")
        if old_cny == new_cny and old_raw and new_raw:
            op = _normalize_price(old_raw); np = _normalize_price(new_raw)
            if op is not None and np is not None and abs(op - np) >= 0.05:
                changes.append("price_raw")

    for f in text_fields:
        ov = normalize_text(baseline.get(f))
        nv = normalize_text(current.get(f))
        if not nv: continue
        if ov == nv: continue
        if f == "bullet_points" and _bullets_similar(ov, nv): continue
        changes.append(f)

    or_ = _parse_num(baseline.get("rating")); nr = _parse_num(current.get("rating"))
    if or_ is not None and nr is not None and abs(round(nr,1) - round(or_,1)) >= 0.1:
        changes.append("rating")

    oc = _parse_num(baseline.get("review_count")); nc = _parse_num(current.get("review_count"))
    if oc is not None and nc is not None:
        d = nc - oc
        if d != 0: changes.append("review_count")

    return changes

def _bullets_similar(old, new, threshold=0.5):
    def split(t):
        return {i.strip().rstrip(";.,").strip() for i in t.replace("||","|").split("|") if len(i.strip()) >= 5}
    oi = split(old); ni = split(new)
    if not oi or not ni: return False
    all_items = oi | ni
    return len(oi & ni) / len(all_items) >= threshold if all_items else False

def is_variant_switch(changed):
    return "title" in set(changed) and ("price_raw" in set(changed) or "bullet_points" in set(changed))

def _normalize_seller(val):
    if not val: return ""
    return normalize_text(re.sub(r"\s*\([^)]*\)\s*", " ", str(val)))

def _normalize_price(val):
    if not val: return None
    try:
        t = str(val).strip()
        if t.startswith("CNY") or t.startswith("¥"):
            return round(float(re.sub(r"[^0-9.]","",t.replace("CNY","").replace("¥","")))/7.25,2)
        return float(re.sub(r"[^0-9.]","",t.replace("$","")))
    except: return None

def _parse_num(val):
    if not val: return None
    try: return float(str(val).strip())
    except: return None

# ── Feishu (Bitable) ────────────────────────────────────────────────
class FeishuClient:
    BASE = "https://open.feishu.cn"
    def __init__(self):
        self._token = None; self._exp = 0
    def _auth(self):
        now = time.time()
        if self._token and now < self._exp - 60: return self._token
        r = requests.post(f"{self.BASE}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id":FEISHU_APP_ID,"app_secret":FEISHU_APP_SECRET}, timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("code")!=0: raise RuntimeError(f"Feishu auth: {d}")
        self._token = d["tenant_access_token"]; self._exp = now + d.get("expire",7200)
        return self._token
    def _req(self, method, path, body=None):
        token = self._auth()
        h = {"Authorization":f"Bearer {token}","Content-Type":"application/json; charset=utf-8"}
        url = f"{self.BASE}{path}"
        try:
            if method=="GET": r = requests.get(url, headers=h, timeout=15)
            elif method=="PUT": r = requests.put(url, headers=h, json=body, timeout=15)
            else: r = requests.post(url, headers=h, json=body, timeout=15)
            r.raise_for_status()
            d = r.json()
            if d.get("code")!=0: print(f"[WARN] Feishu API [{path}]: {d.get('code')} {d.get('msg')}")
            return d
        except requests.RequestException as e:
            print(f"[WARN] Feishu HTTP [{path}]: {e}")
            return None

def read_asin_list(client):
    base = f"/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_SOURCE_TABLE_ID}/records"
    asin_map = {}
    page_token = None
    while True:
        path = base + "?page_size=500"
        if page_token: path += f"&page_token={page_token}"
        data = client._req("GET", path)
        if not data: sys.exit(1)
        for rec in data.get("data",{}).get("items",[]):
            f = rec.get("fields",{})
            val = str(f.get("ASIN","")).strip()
            if re.match(r"^B[A-Z0-9]{9}$", val):
                asin_map[val] = {
                    "name": str(f.get("品名","")).strip(),
                    "parent_name": str(f.get("父体名","")).strip(),
                    "product_line": str(f.get("产品线","")).strip(),
                }
        if not data.get("data",{}).get("has_more"): break
        page_token = data.get("data",{}).get("page_token","")
        if not page_token: break
    return asin_map

def write_results(client, rows, asin_info=None):
    if not rows: return
    asin_info = asin_info or {}
    now_ts = int(datetime.now(BJT).timestamp() * 1000)
    records = []
    for r in rows:
        info = asin_info.get(r["asin"], {})
        records.append({"fields": {
            "日期": now_ts, "ASIN": r["asin"], "品名": info.get("name",""),
            "产品线": info.get("product_line",""), "变化字段": r["field"],
            "旧值": r["old_value"], "新值": r["new_value"],
            "检查时间": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S"),
        }})
    for i in range(0, len(records), 500):
        chunk = records[i:i+500]
        client._req("POST", f"/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_RESULT_TABLE_ID}/records/batch_create", {"records": chunk})

def send_card(client, card):
    all_sent = True
    for cid in FEISHU_CHAT_ID.split(","):
        cid = cid.strip()
        body = {"receive_id": cid, "msg_type": "interactive", "content": json.dumps(card)}
        if client._req("POST", "/open-apis/im/v1/messages?receive_id_type=chat_id", body) is None:
            all_sent = False
    return all_sent


def _card_text(value, limit=110):
    """Compact, single-line, Lark-card-safe text."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("`", "'").replace("<", "‹").replace(">", "›")
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text or "（空）"


def _format_rating_reviews(data):
    rating = str(data.get("rating", "") or "").strip()
    review_raw = str(data.get("review_count", "") or "").strip()
    review_number = _parse_num(review_raw)
    if review_number is not None:
        reviews = f"{int(review_number):,}"
    else:
        reviews = review_raw
    if rating and reviews:
        return f"{rating} ({reviews})"
    return rating or (f"({reviews})" if reviews else "")


def _split_bullets(value):
    text = str(value or "")
    if "||" in text:
        return [x.strip() for x in text.split("||") if x.strip()]
    return [text.strip()] if text.strip() else []


def _format_card_change(field, old_value, new_value):
    """Describe exactly what changed without flooding the card."""
    label = "评分/评论数" if field == "rating_reviews" else FIELD_LABELS.get(field, field)
    if field == "bullet_points":
        old_items = _split_bullets(old_value)
        new_items = _split_bullets(new_value)
        count = max(len(old_items), len(new_items))
        changed_items = []
        for i in range(count):
            old_item = old_items[i] if i < len(old_items) else ""
            new_item = new_items[i] if i < len(new_items) else ""
            if normalize_text(old_item) != normalize_text(new_item):
                changed_items.append((i + 1, old_item, new_item))
        if not changed_items:
            return f"  - **{label}**：内容或顺序发生变化"
        lines = []
        for number, old_item, new_item in changed_items:
            lines.append(
                f"  - **五点第{number}条**：{_card_text(old_item, 75)}"
                f" → {_card_text(new_item, 75)}"
            )
        return "\n".join(lines)

    if field == "title":
        return (
            f"  - **{label}**：{_card_text(old_value, 100)}"
            f" → {_card_text(new_value, 100)}"
        )

    return (
        f"  - **{label}**：{_card_text(old_value, 70)}"
        f" → {_card_text(new_value, 70)}"
    )


def build_summary_card_pages(total, valid_count, failed_asins, changes_found, asin_info):
    """Group all changes by field and paginate without omitting any ASIN."""
    changed_by_field = {}
    changed_asins = set()
    for row in changes_found:
        field_key = row.get("field_key", "")
        changed_by_field.setdefault(field_key, []).append(row)
        changed_asins.add(row["asin"])

    category_meta = {
        "add_to_cart": ("🛒", "购物车状态变化"),
        "sold_by": ("🏪", "Sold By变化"),
        "price_raw": ("💰", "价格变化"),
        "is_promo": ("🏷️", "促销状态变化"),
        "rating_reviews": ("⭐", "评分/评论数变化"),
        "sales_rank": ("📊", "销售排名变化"),
        "title": ("📝", "标题变化"),
        "bullet_points": ("📋", "五点描述变化"),
        "variations": ("🔀", "变体关系变化"),
        "breadcrumb": ("🗂️", "类目节点变化"),
        "rating": ("⭐", "评分变化"),
        "review_count": ("💬", "评论数变化"),
    }
    category_order = [
        "add_to_cart", "sold_by", "price_raw", "is_promo",
        "rating_reviews", "sales_rank", "title", "bullet_points",
        "variations", "breadcrumb", "rating", "review_count",
    ]
    ordered_fields = [f for f in category_order if f in changed_by_field]
    ordered_fields.extend(sorted(f for f in changed_by_field if f not in category_order))

    intro = [
        f"有效检查 **{valid_count}**/{total} 个 ASIN",
        f"发现 **{len(changed_asins)}** 个 ASIN、**{len(changes_found)}** 项变化",
    ]
    pages = []
    max_chars = 18000

    if changed_by_field:
        lines = intro
        for field_key in ordered_fields:
            rows = sorted(changed_by_field[field_key], key=lambda row: row["asin"])
            emoji, category_label = category_meta.get(
                field_key, ("📌", f"{FIELD_LABELS.get(field_key, field_key)}变化")
            )
            category_header = f"\n{emoji} **{category_label}（{len(rows)}）**"
            if len("\n".join(lines)) + len(category_header) > max_chars:
                pages.append("\n".join(lines))
                lines = []
            lines.append(category_header)

            for row in rows:
                asin = row["asin"]
                info = asin_info.get(asin, {})
                name = _card_text(info.get("name") or "未填写品名", 40)
                if field_key == "bullet_points":
                    detail = row.get("card_detail") or _format_card_change(
                        field_key, row.get("old_value", ""), row.get("new_value", "")
                    )
                    detail = re.sub(r"^\s*-\s*\*\*五点", "  - **", detail, flags=re.M)
                    block_text = f"\n**{asin}（{name}）**\n{detail}"
                else:
                    block_text = (
                        f"\n- **{asin}（{name}）**："
                        f"{_card_text(row.get('old_value', ''), 100)}"
                        f" → {_card_text(row.get('new_value', ''), 100)}"
                    )

                if len("\n".join(lines)) + len(block_text) > max_chars:
                    pages.append("\n".join(lines))
                    lines = [f"{emoji} **{category_label}（续）**"]
                lines.append(block_text)
        pages.append("\n".join(lines))
    else:
        pages = ["\n".join(intro + ["", "全部无异常"])]

    if failed_asins:
        failed_list = sorted(failed_asins)
        failure_block = "\n".join([
            "",
            f"❌ **{len(failed_list)}** 个 ASIN 抓取失败，原 Baseline 已保留：",
            "、".join(failed_list),
        ])
        if len(pages[-1]) + len(failure_block) > max_chars:
            pages.append(failure_block.strip())
        else:
            pages[-1] += failure_block

    return pages

# ── Baseline persistence (Bitable ↔ SQLite) ─────────────────────────
BASELINE_FIELD_MAP = {
    "asin": "ASIN", "title": "标题", "price_raw": "价格",
    "is_promo": "促销状态", "bullet_points": "五点描述",
    "add_to_cart": "购物车", "sold_by": "Sold By",
    "breadcrumb": "类目节点", "variations": "变体关系",
    "rating": "评分", "review_count": "评论数",
    "sales_rank": "销售排名", "updated_at": "更新时间",
}
BASELINE_FIELDS_SQL = list(BASELINE_FIELD_MAP.keys())
BASELINE_BITABLE_FIELDS = {v: k for k, v in BASELINE_FIELD_MAP.items()}

def pull_baseline_from_bitable(client, conn):
    """Load baseline from Bitable into local SQLite at startup."""
    base = f"/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_BASELINE_TABLE_ID}/records"
    page_token = None
    count = 0
    while True:
        path = base + "?page_size=500"
        if page_token: path += f"&page_token={page_token}"
        data = client._req("GET", path)
        if not data: break
        for rec in data.get("data",{}).get("items",[]):
            fields = rec.get("fields", {})
            asin = str(fields.get("ASIN","")).strip()
            if not re.match(r"^B[A-Z0-9]{9}$", asin): continue
            row = {"asin": asin}
            for bt_field, sql_field in BASELINE_BITABLE_FIELDS.items():
                if sql_field == "asin": continue
                val = fields.get(bt_field, "")
                if val is not None: val = str(val).strip()[:500]
                else: val = ""
                row[sql_field] = val
            existing = conn.execute("SELECT asin FROM baseline WHERE asin=?", (asin,)).fetchone()
            if not existing:
                conn.execute(
                    f"INSERT INTO baseline ({', '.join(row.keys())}) VALUES ({', '.join('?' for _ in row)})",
                    list(row.values()))
                count += 1
            else:
                # Update non-empty fields
                for k, v in row.items():
                    if k != "asin" and v:
                        conn.execute(f"UPDATE baseline SET {k}=? WHERE asin=?", (v, asin))
        if not data.get("data",{}).get("has_more"): break
        page_token = data.get("data",{}).get("page_token","")
        if not page_token: break
    conn.commit()
    print(f"  Pulled {count} new baselines from Bitable")

def push_baseline_to_bitable(client, conn):
    """Sync local SQLite baseline to Bitable (delete all, then batch create)."""
    rows = conn.execute("SELECT * FROM baseline").fetchall()
    cols = [d[1] for d in conn.execute("PRAGMA table_info(baseline)")]
    if not rows: return

    # Delete all existing records
    base = f"/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{FEISHU_BASELINE_TABLE_ID}/records"
    all_ids = []
    page_token = None
    while True:
        path = base + "?page_size=500"
        if page_token: path += f"&page_token={page_token}"
        data = client._req("GET", path)
        if not data: break
        for item in data.get("data",{}).get("items",[]):
            if item.get("record_id"): all_ids.append(item["record_id"])
        if not data.get("data",{}).get("has_more"): break
        page_token = data.get("data",{}).get("page_token","")
    if all_ids:
        for i in range(0, len(all_ids), 500):
            client._req("POST", f"{base}/batch_delete", {"records": all_ids[i:i+500]})

    # Batch create from SQLite
    records = []
    for row in rows:
        row_dict = dict(zip(cols, row))
        fields = {}
        for sql_col, bt_field in BASELINE_FIELD_MAP.items():
            val = row_dict.get(sql_col, "")
            if val is not None: val = str(val).strip()[:500]
            else: val = ""
            fields[bt_field] = val
        records.append({"fields": fields})

    for i in range(0, len(records), 500):
        client._req("POST", f"{base}/batch_create", {"records": records[i:i+500]})
    print(f"  Pushed {len(records)} baselines to Bitable")

# ── Main ────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')}] Starting (Actions)...")
    conn = init_db()
    client = FeishuClient()
    session = requests.Session()

    asins = read_asin_list(client)
    print(f"  Read {len(asins)} ASINs")
    pull_baseline_from_bitable(client, conn)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=str, default="")
    parser.add_argument("--limit", type=int, default=0)
    args, _ = parser.parse_known_args()
    batch_num = batch_total = None
    if args.batch and "/" in args.batch:
        p = args.batch.split("/"); batch_num = int(p[0]); batch_total = int(p[1])

    shuffled = list(asins.items()); random.shuffle(shuffled)
    if batch_num and batch_total:
        cs = (len(shuffled) + batch_total - 1) // batch_total
        s = (batch_num - 1) * cs; e = min(batch_num * cs, len(shuffled))
        shuffled = shuffled[s:e]
        print(f"  Batch {batch_num}/{batch_total}: {len(shuffled)} ASINs")

    if args.limit > 0:
        shuffled = shuffled[:args.limit]
        print(f"  Limit mode: {len(shuffled)} ASINs")

    total = len(shuffled)
    changes_found = []
    asin_changes = {}
    field_groups = {}
    empty_streak = 0; cooldown_count = 0
    failed_asins = set()
    valid_count = 0

    for i, (asin, _) in enumerate(shuffled, 1):
        print(f"  [{i}/{total}] {asin}...")
        soup, redirected = fetch_page(asin, session)

        if soup is None:
            print("    SKIP: fetch failed")
            failed_asins.add(asin)
            continue

        if redirected and not (soup.select_one("#productTitle") or soup.select_one("#title")):
            current = {"title":"不可售（重定向）","price_raw":"不可售","is_promo":0,
                       "bullet_points":"","add_to_cart":"Unavailable","sold_by":"",
                       "breadcrumb":"","variations":"","rating":"","review_count":"","sales_rank":""}
        else:
            current = parse_product(soup)

        if not current.get("title",""):
            failed_asins.add(asin)
            empty_streak += 1
            if empty_streak >= 8:
                cooldown_count += 1
                cd = min(300 * cooldown_count, 1800)
                print(f"  [COOLDOWN] {cd}s (#{cooldown_count})")
                time.sleep(cd)
                empty_streak = 0
            continue
        else:
            empty_streak = 0
            valid_count += 1

        baseline = load_baseline(conn, asin)
        if baseline is None:
            insert_baseline(conn, {**current, "asin": asin, "updated_at": datetime.now(BJT).isoformat()})
            print("    Baseline stored")
        else:
            changed = compare(current, baseline)
            if is_variant_switch(changed):
                print("    Variant switch — skipped")
            elif changed:
                report_fields = list(changed)
                if "rating" in report_fields or "review_count" in report_fields:
                    report_fields = [f for f in report_fields if f not in ("rating", "review_count")]
                    report_fields.append("rating_reviews")

                for f in report_fields:
                    if f == "rating_reviews":
                        old_report_value = _format_rating_reviews(baseline)
                        new_report_value = _format_rating_reviews(current)
                        report_label = "评分/评论数"
                    else:
                        old_report_value = baseline.get(f, "")
                        new_report_value = current.get(f, "")
                        report_label = FIELD_LABELS.get(f, f)
                    detail = f"{f}: {old_report_value} -> {new_report_value}"
                    print(f"    CHANGE: {detail[:100]}")
                    changes_found.append({"asin":asin,"field":report_label,
                        "old_value":str(old_report_value).strip()[:200],
                        "new_value":str(new_report_value).strip()[:200],
                        "field_key":f,
                        "card_detail":_format_card_change(
                            f, old_report_value, new_report_value
                        )})
                    field_groups.setdefault(f,[]).append({"asin":asin,"detail":detail})
                    asin_changes.setdefault(asin,[]).append(detail)
            save_baseline(conn, {**current, "asin":asin, "updated_at":datetime.now(BJT).isoformat()})
            if not changed: print("    No changes")

        if i < total:
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
            if INTERNAL_BATCH_SIZE > 0 and i % INTERNAL_BATCH_SIZE == 0:
                print(f"  [BATCH PAUSE] {INTERNAL_BATCH_PAUSE}s")
                time.sleep(INTERNAL_BATCH_PAUSE)

    today_str = datetime.now(BJT).strftime("%Y-%m-%d")
    write_results(client, changes_found, asins)
    print(f"  Wrote {len(changes_found)} changes")

    sheet_url = f"https://{FEISHU_TENANT}.feishu.cn/base/{FEISHU_APP_TOKEN}?table={FEISHU_RESULT_TABLE_ID}"

    if failed_asins:
        color, title = "red", f"🚫 检查存在失败 | {today_str}"
    elif asin_changes:
        color, title = "orange", f"⚠️ 每日检查完成 | {today_str}"
    else:
        color, title = "green", f"✅ 每日检查完成 | {today_str}"

    summary_pages = build_summary_card_pages(
        total, valid_count, failed_asins, changes_found, asins
    )
    all_cards_sent = True
    page_total = len(summary_pages)
    for page_number, summary in enumerate(summary_pages, 1):
        page_title = title if page_total == 1 else f"{title}（{page_number}/{page_total}）"
        elements = [{"tag":"div","text":{"tag":"lark_md","content":summary}},
                    {"tag":"hr"},
                    {"tag":"action","actions":[{"tag":"button","text":{"tag":"plain_text","content":"查看飞书表格"},"type":"primary","url":sheet_url}]}]
        card = {"config":{"wide_screen_mode":True},"header":{"title":{"tag":"plain_text","content":page_title},"template":color},"elements":elements}
        if not send_card(client, card):
            all_cards_sent = False
        if page_number < page_total:
            time.sleep(1)
    if all_cards_sent:
        print(f"  Cards sent: {page_total} page(s), all changes included")
    else:
        print("  [ERROR] One or more cards were not sent")

    if valid_count > 0:
        push_baseline_to_bitable(client, conn)
    else:
        print("  [WARN] Baseline push skipped: no valid product data")
    conn.close()
    print(f"[{datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')}] Done.")

if __name__ == "__main__":
    main()
