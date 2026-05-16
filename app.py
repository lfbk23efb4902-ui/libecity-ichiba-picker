import os
from flask import Flask, render_template, request, session
from market_client import BUDGETS, CATEGORIES, fetch_candidates, pick_combination, enrich_shipping

app = Flask(__name__)
app.secret_key = os.urandom(24)


@app.route("/")
def index():
    return render_template("index.html", categories=CATEGORIES, budgets=BUDGETS)


@app.route("/search", methods=["POST"])
def search():
    # カスタム金額が入力されていればそちらを優先
    custom_str = request.form.get("custom_budget", "").strip().replace(",", "").replace("¥", "")
    if custom_str.isdigit() and int(custom_str) >= 100:
        budget = int(custom_str)
    else:
        budget = int(request.form.get("budget", 1000))

    category_ids = [int(v) for v in request.form.getlist("categories")]
    count_str = request.form.get("count", "")
    target_count = int(count_str) if count_str.isdigit() else None

    # カスタム金額の場合はBUDGETSリストに含まれていなくてもOK
    if budget not in BUDGETS and budget < 100:
        budget = 1000
    if not category_ids:
        return render_template(
            "result.html",
            picks=[],
            budget=budget,
            total=0,
            target_count=target_count,
            error="カテゴリを1つ以上選んでください",
            categories=CATEGORIES,
            budgets=BUDGETS,
        )

    try:
        candidates = fetch_candidates(budget, category_ids)
        picks = pick_combination(candidates, budget, target_count)
        enrich_shipping(picks)  # 送料別商品の送料を詳細ページから取得
    except Exception as e:
        return render_template(
            "result.html",
            picks=[],
            budget=budget,
            total=0,
            target_count=target_count,
            selected_category_ids=category_ids,
            error=f"検索中にエラーが発生しました: {e}",
            categories=CATEGORIES,
            budgets=BUDGETS,
        )

    # 実際の送料が判明した商品はそちらを、不明な場合は見込み額（effective_price）を使う
    total = sum(
        p["price"] + (p["shipping_fee"] if p["shipping_fee"] > 0 else
                      (0 if p["shipping_included"] else 500))
        for p in picks
    )
    return render_template(
        "result.html",
        picks=picks,
        budget=budget,
        total=total,
        target_count=target_count,
        selected_category_ids=category_ids,
        error=None,
        categories=CATEGORIES,
        budgets=BUDGETS,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5003))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)
