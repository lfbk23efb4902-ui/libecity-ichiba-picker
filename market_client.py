import random
import re
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

BUDGETS = [1000, 3000, 5000, 10000]

CATEGORIES = [
    {"id": 1,   "name": "食品"},
    {"id": 2,   "name": "飲料"},
    {"id": 3,   "name": "ファッション"},
    {"id": 4,   "name": "美容・コスメ・健康"},
    {"id": 5,   "name": "キッズ・ベビー"},
    {"id": 6,   "name": "日用品・文房具"},
    {"id": 7,   "name": "本・音楽・映像"},
    {"id": 8,   "name": "エンタメ・ホビー"},
    {"id": 9,   "name": "家電・電化製品"},
    {"id": 10,  "name": "キッチン・調理グッズ"},
    {"id": 11,  "name": "家具・インテリア"},
    {"id": 12,  "name": "車・バイク・自転車"},
    {"id": 336, "name": "その他"},
]

BASE_URL = "https://ichiba.libecity.com"

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
})


def _parse_price(text: str):
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_page(html: str, price_max: int) -> list:
    """HTML から price_max 以下の商品カードを抽出して返す。"""
    soup = BeautifulSoup(html, "html.parser")
    products = []

    for card in soup.select("section.productCard"):
        price_tag = card.select_one("p.price")
        if not price_tag:
            continue
        price = _parse_price(price_tag.get_text())
        if price is None or price <= 0 or price > price_max:
            continue

        name_tag = card.select_one("h3.caption")
        name = name_tag.get_text(strip=True) if name_tag else ""

        img = card.select_one("img.img_product")
        image_url = img["src"] if img and img.get("src") else ""

        link = card.select_one("a.link_block")
        href = link["href"] if link else ""
        product_url = href if href.startswith("http") else BASE_URL + href

        # オプション・同梱専用・送料など単品購入できない商品を除外
        skip_keywords = ["オプション", "ラッピング", "送料", "手数料", "追加料金", "同梱専用", "同梱のみ", "熨斗"]
        if any(kw in name for kw in skip_keywords):
            continue

        # 送料情報
        shipping_tag = card.select_one(".shipping")
        shipping_included = (
            "is_include" in (shipping_tag.get("class") or [])
            if shipping_tag else True
        )

        # 送料別の場合は見込み送料を加算した実効価格で予算計算する（平均的な送料として500円を見込む）
        SHIPPING_ESTIMATE = 500
        effective_price = price if shipping_included else price + SHIPPING_ESTIMATE

        products.append({
            "name": name,
            "price": price,
            "effective_price": effective_price,  # 予算計算用（送料込みの場合はpriceと同じ）
            "image_url": image_url,
            "url": product_url,
            "shipping_included": shipping_included,
            "shipping_fee": 0,  # 後で詳細ページから取得
        })

    return products


def fetch_shipping_fee(product_url: str) -> int:
    """
    商品詳細ページから送料を取得して返す。取得できなければ 0。
    ¥直後の数字のみを抽出して誤パースを防ぐ。
    """
    try:
        resp = _session.get(product_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 「基本送料 ¥250」形式 → span.txt_tax
        for tag in soup.select("span.txt_tax"):
            text = tag.get_text()
            if "送料" in text:
                m = re.search(r'[¥￥]([\d,]+)', text)
                if m:
                    return int(m.group(1).replace(",", ""))

        # 「基本送料：250円」形式 → p.shippingInfo_text
        for tag in soup.select("p.shippingInfo_text"):
            text = tag.get_text()
            if "送料" in text:
                m = re.search(r'(\d[\d,]*)\s*円', text)
                if m:
                    return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return 0


def enrich_shipping(picks: list) -> list:
    """
    ピックアップ済みの商品リストに対して、送料別の商品だけ詳細ページを並列取得して
    shipping_fee を更新して返す。
    """
    targets = [p for p in picks if not p.get("shipping_included", True)]
    if not targets:
        return picks

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_shipping_fee, p["url"]): p for p in targets}
        for future in as_completed(futures):
            try:
                futures[future]["shipping_fee"] = future.result()
            except Exception:
                pass
    return picks


def _fetch_one_page(cat_id: int, page: int, price_max: int) -> list:
    """1カテゴリ1ページ分を取得してパースする（並列処理用）"""
    resp = _session.get(
        f"{BASE_URL}/search",
        params={"category_id": cat_id, "order": "new", "page": page},
        timeout=8,
    )
    resp.raise_for_status()
    return _parse_page(resp.text, price_max)


def fetch_candidates(budget: int, category_ids: list, max_pages: int = 1) -> list:
    """
    指定カテゴリから budget 以下の商品を候補として収集する。
    カテゴリ×ページを並列取得して高速化。
    """
    price_max = int(budget * 0.9)
    seen_urls = set()
    all_products = []

    # 全タスク（カテゴリ×ページ）を並列実行
    tasks = [(cat_id, page) for cat_id in category_ids for page in range(1, max_pages + 1)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one_page, cat_id, page, price_max): (cat_id, page)
                   for cat_id, page in tasks}
        for future in as_completed(futures):
            try:
                for p in future.result():
                    if p["url"] not in seen_urls:
                        seen_urls.add(p["url"])
                        all_products.append(p)
            except Exception:
                pass  # 1ページ失敗しても続行

    return all_products


def pick_combination(candidates: list, budget: int, target_count: int = None, trials: int = 80) -> list:
    """
    候補商品からランダムに選び、合計が budget に最も近い組み合わせを返す。

    target_count が None の場合は 2〜10 個でランダムに試行する。
    個数指定時は予算を均等割りして各商品の上限価格を調整する。
    """
    if not candidates:
        return []

    best = []
    best_total = 0

    for _ in range(trials):
        count = target_count if target_count else random.randint(2, 10)

        # 個数指定時は1個あたりの上限を budget/count×1.5 に絞って均等に使えるようにする
        if target_count:
            per_item_cap = int(budget / count * 1.5)
            pool = [p for p in candidates if p["effective_price"] <= per_item_cap]
            if len(pool) < count:
                pool = [p for p in candidates if p["effective_price"] <= budget]
        else:
            pool = [p for p in candidates if p["effective_price"] <= budget]

        random.shuffle(pool)

        selected = []
        remaining = budget
        for p in pool:
            if p["effective_price"] <= remaining:
                selected.append(p)
                remaining -= p["effective_price"]
            if len(selected) >= count:
                break

        total = sum(p["effective_price"] for p in selected)
        # 個数が指定に近く、予算消化率が高いものを優先
        count_ok = (len(selected) == count) if target_count else (len(selected) >= 2)
        if count_ok and total > best_total:
            best = selected
            best_total = total

    # 個数指定で一度も達成できなければ条件を緩めて再試行
    if target_count and len(best) < target_count:
        for _ in range(40):
            pool = [p for p in candidates if p["price"] <= budget]
            random.shuffle(pool)
            selected = []
            remaining = budget
            for p in pool:
                if p["price"] <= remaining:
                    selected.append(p)
                    remaining -= p["price"]
                if len(selected) >= target_count:
                    break
            total = sum(p["price"] for p in selected)
            if len(selected) >= 2 and total > best_total:
                best = selected
                best_total = total

    return best
