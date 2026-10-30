"""(cloud) - メルカリ売り切れ検知 → eBay在庫0

GitHub Actions から30分ごとに実行される。
商品リストは Google スプレッドシート（Apps Script 経由）から読み書きする。
秘密情報はすべて GitHub の Secrets から環境変数として受け取る。
"""
import base64
import json
import os
import random
import re
import statistics
import sys
import time
import unicodedata
import math
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

APP_ID = os.environ["EBAY_APP_ID"]
CERT_ID = os.environ["EBAY_CERT_ID"]
REFRESH_TOKEN = os.environ["EBAY_REFRESH_TOKEN"]
SHEET_API_URL = os.environ["SHEET_API_URL"]
SHEET_API_KEY = os.environ["SHEET_API_KEY"]
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

SCOPES = " ".join([
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
])
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TRADING_URL = "https://api.ebay.com/ws/api.dll"
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
JST = ZoneInfo("Asia/Tokyo")


def now():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


def notify(msg):
    # 公開リポジトリの実行記録は誰でも見られるため、通知の中身は記録に出さない
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=15)
        except Exception:
            print("Discord通知に失敗しました")


def alert(subject, body):
    """売れた・要確認など、すぐ見てほしい知らせだけをメール（スマホのGmail）とDiscordに送る"""
    notify(f"{subject}\n{body}")
    try:
        sheet("mail", subject=subject, body=body)
    except Exception:
        print("メール通知に失敗しました")


# ---------- スプレッドシート ----------
def sheet(action, **payload):
    # Googleの応答がたまに壊れる（書き込み自体は成功していることが多い）ので、数回やり直す
    for attempt in range(3):
        try:
            r = requests.post(
                SHEET_API_URL,
                data=json.dumps({"key": SHEET_API_KEY, "action": action, **payload}),
                headers={"Content-Type": "text/plain"},
                timeout=60,
            )
            data = r.json()
            break
        except (ValueError, requests.RequestException):
            if action == "append":      # 追加は二重にならないよう、やり直さない
                print("スプレッドシートの応答が読めませんでした（追加）")
                return {}
            time.sleep(5 * (attempt + 1))
    else:
        if action in ("update", "log", "cleanup"):
            print(f"スプレッドシートの応答が読めませんでした（{action}）。続行します")
            return {}
        raise RuntimeError("スプレッドシートの応答が読めません（3回やり直しても失敗）")
    if data.get("error"):
        raise RuntimeError(f"スプレッドシートのエラー: {data['error']}")
    return data


# ---------- eBay ----------
def get_access_token():
    auth = "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN, "scope": SCOPES},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"eBayトークン更新に失敗 ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


# eBayのRefresh Tokenの期限（取り直したら TOKEN_ISSUED を書き換える）
TOKEN_ISSUED = "2026-10-21"
TOKEN_DAYS = 547   # 約18か月
TOKEN_HOWTO = ("取り直し方：Claudeに「eBayのトークンを取り直したい」と伝えてください。"
               "取り直すまで、在庫チェック・出品・売り切れ時の出品停止がすべて止まります。")


def token_days_left():
    issued = datetime.strptime(TOKEN_ISSUED, "%Y-%m-%d").replace(tzinfo=JST)
    return (issued + timedelta(days=TOKEN_DAYS) - datetime.now(JST)).days


def token_warning():
    """期限の60日前から、毎日9時台に1回だけメールで知らせる"""
    left = token_days_left()
    if left <= 60 and datetime.now(JST).hour == 9:
        alert(f"【要対応】eBayのトークンの期限まであと{max(left, 0)}日です",
              f"eBayとツールをつなぐトークン（Refresh Token）の期限が近づいています（あと約{max(left, 0)}日）。\n\n{TOKEN_HOWTO}")


FINANCES_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.finances"


ANALYTICS_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"
NON_SALE_OTHER = []   # 広告費以外の手数料（出品手数料など）。経費シートに入れる


def get_finances_token(scope=FINANCES_SCOPE):
    """追加の権限（お金の明細・閲覧数）のトークン。権限がまだなければ None（ほかの処理は止めない）"""
    auth = "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    try:
        r = requests.post(
            TOKEN_URL,
            headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN, "scope": scope},
            timeout=30,
        )
        return r.json().get("access_token") if r.status_code == 200 else None
    except Exception:
        return None


def ebay_ad_fees(days=90):
    """広告費 {注文番号: ドル}。お金の明細の権限がまだなければ None"""
    tok = get_finances_token()
    if not tok:
        return None
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out, offset = {}, 0
    while offset < 2000:
        r = requests.get("https://apiz.ebay.com/sell/finances/v1/transaction",
                         params={"filter": [f"transactionDate:[{since}..]", "transactionType:{NON_SALE_CHARGE}"],
                                 "limit": 200, "offset": offset},
                         headers={"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}, timeout=30)
        if r.status_code != 200:
            print("広告費の明細を読めませんでした:", r.status_code, r.text[:150])
            return None
        js = r.json()
        for t in js.get("transactions", []):
            ft = str(t.get("feeType", "")).upper()
            if "AD_FEE" not in ft and "PROMOT" not in ft:   # 広告以外（出品手数料など）は経費シートへ
                v = float((t.get("amount") or {}).get("value") or 0)
                NON_SALE_OTHER.append({"id": t.get("transactionId", ""), "date": (t.get("transactionDate") or "")[:10],
                                       "type": ft or "OTHER", "usd": round(-v if t.get("bookingEntry") == "CREDIT" else v, 2),
                                       "memo": str(t.get("transactionMemo") or "")[:80]})
                continue
            oid = t.get("orderId") or next((x.get("referenceId") for x in t.get("references", [])
                                             if x.get("referenceType") == "ORDER_ID"), "")
            if not oid:
                continue
            v = float((t.get("amount") or {}).get("value") or 0)
            if t.get("bookingEntry") == "CREDIT":
                v = -v   # 返金された広告費
            out[oid] = round(out.get(oid, 0) + v, 2)
        if len(js.get("transactions", [])) < 200:
            break
        offset += 200
    return out


def listing_views(ids, days=30):
    """出品ごとの閲覧数と表示回数 {出品番号: (閲覧, 表示)}（直近30日）。権限がなければ {}"""
    tok = get_finances_token(ANALYTICS_SCOPE)
    if not tok or not ids:
        return {}
    end = datetime.now(ZoneInfo("America/Los_Angeles"))
    rng = f"[{(end - timedelta(days=days)).strftime('%Y%m%d')}..{end.strftime('%Y%m%d')}]"
    out, ids = {}, list(ids)
    for k in range(0, len(ids), 200):
        r = requests.get("https://api.ebay.com/sell/analytics/v1/traffic_report", params={
            "dimension": "LISTING", "metric": "LISTING_VIEWS_TOTAL,LISTING_IMPRESSION_TOTAL",
            "filter": f"marketplace_ids:{{EBAY_US}},date_range:{rng},listing_ids:{{{'|'.join(ids[k:k + 200])}}}"},
            headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if r.status_code != 200:
            print("閲覧数を読めませんでした:", r.status_code, r.text[:150])
            return out
        js = r.json()
        keys = [m.get("key") for m in (js.get("header") or {}).get("metrics", [])]
        for rec in js.get("records", []):
            lid = ((rec.get("dimensionValues") or [{}])[0]).get("value")
            vals = {kk: (mv or {}).get("value") for kk, mv in zip(keys, rec.get("metricValues", []))}
            out[str(lid)] = (int(vals.get("LISTING_VIEWS_TOTAL") or 0), int(vals.get("LISTING_IMPRESSION_TOTAL") or 0))
    return out


def unsold_reason(days, views, watch, price, target, model):
    """売れていない理由の予想（リストのY列）"""
    if days is None:
        return ""
    if days < 3:
        return "出品して間もない（様子見）"
    rs = []
    try:
        if target and price and float(price) > float(target) * 1.1:
            rs.append(f"相場より高い（出品${float(price):.0f}／売れている相場${float(target):.0f}）")
    except (TypeError, ValueError):
        pass
    if views is not None and views < 3:
        rs.append(f"ほとんど見られていない（{int(days)}日で閲覧{views}回）→検索に出ていない")
    elif views is not None and views >= 10 and not watch:
        rs.append(f"見られているがウォッチされない（閲覧{views}回）→値段・写真・状態が合っていない可能性")
    if watch:
        rs.append(f"ウォッチ{watch}人→少し値下げすれば売れる可能性")
    sold = SOLD90_BY_KEY.get(model_key(model))
    if sold is not None and sold < 10:
        rs.append(f"売れるペースが遅い機種（90日で{sold}台）")
    return " / ".join(rs[:3]) or "はっきりした原因なし（もう少し様子見）"


# ---------- ウォッチしている人への値引きオファー ----------
OFFER_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory"
OFFER_DISCOUNT = 0.10      # 今の値段から10%引き（ただし利益2,000円の値段より下げない）
OFFER_EVERY_DAYS = 7       # 同じ出品に送るのは7日に1回まで
OFFER_FILE = os.path.join(".cache", "offers.json")
OFFER_MSG = ("Thank you for watching this item! Here is a special price just for you. "
             "This is the only one we have, and it ships from Japan with tracking.")


def send_watch_offers(targets):
    """ウォッチしている人（eBayが「興味あり」と判定した人）に値引きオファーを送る"""
    if not targets:
        return
    tok = get_finances_token(OFFER_SCOPE)
    if not tok:
        print("値引きオファー：権限がないため送れません")
        return
    hdr = {"Authorization": f"Bearer {tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US", "Content-Type": "application/json"}
    eligible, offset = set(), 0
    while offset < 2000:
        r = requests.get("https://api.ebay.com/sell/negotiation/v1/find_eligible_items",
                         params={"limit": 200, "offset": offset}, headers=hdr, timeout=30)
        if r.status_code == 204:
            break
        if r.status_code != 200:
            print("値引きオファー：対象の出品を読めませんでした:", r.status_code, r.text[:150])
            return
        items = r.json().get("eligibleItems", [])
        eligible |= {str(x.get("listingId")) for x in items}
        if len(items) < 200:
            break
        offset += 200
    try:
        with open(OFFER_FILE, encoding="utf-8") as f:
            sent = json.load(f)
    except (OSError, ValueError):
        sent = {}
    cut = time.time() - OFFER_EVERY_DAYS * 86400
    n_sent = 0
    for lid, (cur, floor) in targets.items():
        if lid not in eligible or sent.get(lid, 0) > cut or not cur or not floor:
            continue
        price = max(float(floor), round(float(cur) * (1 - OFFER_DISCOUNT), 2))
        if price > float(cur) * 0.95:   # 5%以上安くできない（利益2,000円の値段に近い）ときは送らない
            continue
        body = {"allowCounterOffer": False, "message": OFFER_MSG,
                "offeredItems": [{"listingId": lid, "quantity": 1,
                                  "price": {"currency": "USD", "value": f"{price:.2f}"}}]}
        r = requests.post("https://api.ebay.com/sell/negotiation/v1/send_offer_to_interested_buyers",
                          json=body, headers=hdr, timeout=30)
        if r.status_code == 200:
            n_sent += 1
            sent[lid] = time.time()
            print(f"値引きオファーを送りました：{lid} ${cur:.2f}→${price:.2f}（{len(r.json().get('offers', []))}人）")
        else:
            print(f"値引きオファーを送れませんでした：{lid}", r.status_code, r.text[:150])
            sent[lid] = time.time() - (OFFER_EVERY_DAYS - 1) * 86400   # 失敗したら1日後にもう一度
    os.makedirs(".cache", exist_ok=True)
    with open(OFFER_FILE, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in sent.items() if v > cut - 86400}, f)
    print(f"値引きオファー：対象{len(eligible & set(targets))}件、送信{n_sent}件")


def trading_call(call_name, body_xml, token):
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">{body_xml}</{call_name}Request>'
    )
    r = requests.post(
        TRADING_URL,
        headers={
            "X-EBAY-API-CALL-NAME": call_name,
            "X-EBAY-API-SITEID": "0",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        },
        data=xml.encode("utf-8"),
        timeout=30,
    )
    root = ebay_xml(r, call_name)
    ack = root.findtext("e:Ack", default="", namespaces=NS)
    errors = [e.findtext("e:LongMessage", default="", namespaces=NS) for e in root.findall("e:Errors", NS)]
    return ack, errors


def stop_on_ebay(item_id, token):
    """在庫がなくなったらeBayの出品を終了する（終了できないときだけ在庫0にする）"""
    ack, errors = trading_call(
        "EndFixedPriceItem",
        f"<ItemID>{item_id}</ItemID><EndingReason>NotAvailable</EndingReason>",
        token,
    )
    if ack in ("Success", "Warning"):
        return True, "eBayの出品を終了しました"
    if any(re.search(r"already (been )?(ended|closed)|has ended|終了", e, re.I) for e in errors):
        return True, "eBayの出品はすでに終了しています"
    ack2, errors2 = trading_call(
        "ReviseInventoryStatus",
        f"<InventoryStatus><ItemID>{item_id}</ItemID><Quantity>0</Quantity></InventoryStatus>",
        token,
    )
    if ack2 in ("Success", "Warning"):
        return True, "出品を終了できず、eBay在庫を0にしました"
    return False, f"eBay停止に失敗: {' / '.join(errors + errors2)[:200]}"


# ---------- メルカリ ----------
JUDGE_JS = """() => {
    const text = document.body.innerText;
    if (/削除されています|見つかりません|公開停止/.test(text)) return 'deleted';
    // メルカリオークションは仕入れ値が確定しないので対象外
    if (document.querySelector('[data-testid="bid-button"], [data-testid="bid-button-container"]') ||
        text.includes('オークション商品')) return 'auction';
    // メルカリShops: 在庫0でも非表示の「購入手続きへ」が残るため、専用の目印で判定
    if (document.querySelector('[data-testid="out-of-stock"], [data-testid="disabled-purchase-button"]'))
        return 'sold';
    const live = [...document.querySelectorAll('button, a')].some(b =>
        (b.innerText || '').includes('購入手続きへ') && !b.disabled &&
        b.getAttribute('aria-disabled') !== 'true' && b.offsetParent !== null);
    if (live) return 'on_sale';
    if (text.includes('売り切れ')) return 'sold';
    return 'unknown';
}"""


def check_mercari(page, url, retry=True):
    st, info = _check_mercari(page, url)
    if st == "unknown" and retry:   # メルカリが一時的に重いことがあるので、少し待ってもう一度だけ見る
        page.wait_for_timeout(8000)
        st, info = _check_mercari(page, url)
        if st != "unknown":
            print(f"2回目で判定できました：{url} → {st}")
    return st, info


def _check_mercari(page, url):
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"判定できず（ページを開けない）{url}:", repr(e)[:120])
        return "unknown", {}
    if resp is not None and resp.status == 404:
        return "deleted", {}
    if resp is not None and resp.status in (403, 429):
        return "blocked", {}
    try:
        page.wait_for_function(
            """() => { const t = document.body ? document.body.innerText : '';
                return /購入手続きへ|売り切れ|削除されています|見つかりません|公開停止|入札する|オークション商品/.test(t); }""",
            timeout=25000,
        )
    except Exception:
        try:
            snip = re.sub(r"\s+", " ", page.evaluate("() => document.body ? document.body.innerText : ''"))[:120]
            print(f"判定できず（表示が出ない）{url} 状態{resp.status if resp else '?'} 題名「{page.title()[:40]}」本文「{snip}」")
        except Exception:
            pass
        return "unknown", {}
    page.wait_for_timeout(600)
    status = page.evaluate(JUDGE_JS)
    info = page.evaluate(INFO_JS) if status in ("on_sale", "sold") else {}
    if status == "on_sale" and not info.get("price"):   # 値段の表示が遅れることがあるので、少し待って読み直す
        page.wait_for_timeout(2500)
        info = page.evaluate(INFO_JS)
    return status, info


DETAIL_JS = """() => ({
    images: [...document.querySelectorAll('[data-testid^="image-"] img')].map(i => i.currentSrc || i.src)
              .filter((v, i, a) => v && a.indexOf(v) === i).slice(0, 12),
    description: (document.querySelector('[data-testid="description"]') || {}).innerText || '',
    condition: ((document.querySelector('[data-testid="商品の状態"]') || {}).innerText || '').split('\\n')[0],
    category: [...document.querySelectorAll('a[href*="category_id="]')].map(a => a.innerText.trim()).filter(Boolean).join(' > '),
})"""

INFO_JS = """() => {
    const title = (document.querySelector('h1')?.innerText || document.title.replace(/ - メルカリ.*$/, '')).trim();
    const main = (document.querySelector('main') || document.body).innerText;
    const head = main.split('商品の説明')[0].replace(/過去価格\\s*¥\\s*[\\d,]+/g, '').replace(/\\d+\\s*%\\s*OFF/gi, '');   // メルカリShopsの値引き表示（過去価格・◯%OFF）は除く
    const pe = document.querySelector('[data-testid="price"]');   // 値段の欄を優先して読む
    const pm = pe ? pe.innerText.replace(/\\s/g, '').match(/([\\d,]+)/) : null;
    const m = pm || head.match(/¥\\s*([\\d,]+)/);
    const sa = document.querySelector('a[href*="/shops/profile/"], a[href*="/user/profile/"]');   // 出品者（ショップ・個人）
    const sm = sa ? (sa.getAttribute('href') || '').match(/(?:shops|user)\\/profile\\/([^\\/?#]+)/) : null;
    return { title, price: m ? parseInt(m[1].replace(/,/g, ''), 10) : null, seller: sm ? sm[1] : '' };
}"""

# ---------- 仕入れ候補の利益計算 ----------
_app_token = None


def app_token():
    """出品者の権限が要らない検索用のトークン"""
    global _app_token
    if _app_token:
        return _app_token
    auth = "Basic " + base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=30,
    )
    r.raise_for_status()
    _app_token = r.json()["access_token"]
    return _app_token


MODEL_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,4}-?\d{2,4}[A-Za-z]{0,2})(?![A-Za-z0-9])")


KNOWN_MODELS = []   # 設定シートの検索キーワード（型番）。商品名にあればそれを優先
DIGIT_MODEL_RE = re.compile(r"(?<![A-Za-z0-9])(A?\d{3}(?:KC|SH))(?![A-Za-z0-9])", re.I)   # 903KC・602SH・A203KC など


def _flat(s):
    return re.sub(r"[-\s‐－ー]", "", unicodedata.normalize("NFKC", s or "")).upper()


def has_model(text, model):
    """文章に型番が含まれるか（KY42C・KY-42C・NP902KC・V602SH は一致、3902KC・0805SHISEIDO は不一致）"""
    q = re.sub(r"[-\s‐－ー]", "", unicodedata.normalize("NFKC", model or "")).upper()
    if len(q) < 4:
        return False
    pat = r"[-\s‐－ー]?".join(re.escape(c) for c in q)
    t = unicodedata.normalize("NFKC", text or "").upper()
    return re.search(r"(?<![0-9])" + pat + r"(?![A-Z0-9])", t) is not None


def guess_query(title):
    """商品名から型番らしき文字列を拾う（例：KY-42C、SH-02L、903KC、SO903i）"""
    t = unicodedata.normalize("NFKC", title or "")
    for k in KNOWN_MODELS:
        if has_model(t, k):   # 「0805SHISEIDO」のような別商品の品番を型番と間違えない
            return k
    m = DIGIT_MODEL_RE.search(t)
    if m:
        return m.group(1).upper()
    for m in MODEL_RE.finditer(t):
        code = m.group(1)
        if re.search(r"[A-Za-z]", code) and len(re.findall(r"\d", code)) >= 2 and len(code.replace("-", "")) >= 4:
            return code
    return ""


_market_cache = {}


def normalize_query(query):
    """型番のゆれ（PCH-2000ZA → PCH-2000 など）を、設定シートの検索キーワードにそろえる（eBayへの問い合わせ回数を減らす）"""
    k = re.sub(r"[-\s]", "", (query or "").upper())
    best = None
    for kw in KNOWN_MODELS:
        kk = re.sub(r"[-\s]", "", kw.upper())
        if kk and k.startswith(kk) and (best is None or len(kk) > len(re.sub(r"[-\s]", "", best.upper()))):
            best = kw
    return best or query


EBAY_LIMITED = [False]   # eBayの1日の問い合わせ上限（429）に当たったら、その回はもう問い合わせない


def ebay_market_usd(query):
    """eBay(US)で同じ型番の中古・即決出品の価格(送料込み)の中央値と件数（携帯電話カテゴリ）"""
    query = normalize_query(query)
    if query in _market_cache:
        _market_stats["hit"] += 1
        return _market_cache[query]
    if EBAY_LIMITED[0]:
        return None, 0
    _market_stats["miss"] += 1
    try:
        _market_cache[query] = _ebay_market_usd(query)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            EBAY_LIMITED[0] = True
            print("eBayの1日の問い合わせ上限に達したので、この回は保存してある相場だけで続けます")
            return None, 0
        raise
    _market_time[query] = time.time()
    return _market_cache[query]


# ---- 候補さがし用：eBay相場を6時間ファイルに覚えておく（出品の直前には必ず最新の相場を取り直す） ----
MARKET_FILE = os.path.join(".cache", "market.json")
MARKET_TTL = 24 * 3600   # 2026/10/28 6時間→24時間（eBayの1日の問い合わせ上限に当たったため）
_market_time = {}
_market_stats = {"hit": 0, "miss": 0}


def load_market_cache():
    try:
        with open(MARKET_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        cut = time.time() - MARKET_TTL
        for q, (t, v) in saved.items():
            if t >= cut:
                _market_cache[q] = tuple(v)
                _market_time[q] = t
        print(f"保存してあった相場：{len(_market_cache)}件（24時間以内）")
    except FileNotFoundError:
        print("保存してあった相場：なし")
    except Exception as e:
        print("保存してあった相場を読めませんでした:", str(e)[:100])


def save_market_cache():
    try:
        os.makedirs(os.path.dirname(MARKET_FILE), exist_ok=True)
        with open(MARKET_FILE, "w", encoding="utf-8") as f:
            json.dump({q: [_market_time[q], list(v)] for q, v in _market_cache.items()
                       if q in _market_time and v}, f, ensure_ascii=False)   # 相場が見つからなかった型番も覚えておく（毎回問い合わせない）
    except Exception as e:
        print("相場を保存できませんでした:", str(e)[:100])


_my_seller = None


def my_seller():
    """自分のeBayユーザー名（コードに書かずに、eBayから取得する）"""
    global _my_seller
    if _my_seller is None:
        _my_seller = os.environ.get("EBAY_SELLER", "").lower()
        if not _my_seller:
            try:
                xml = ('<?xml version="1.0" encoding="utf-8"?>'
                       '<GetUserRequest xmlns="urn:ebay:apis:eBLBaseComponents"></GetUserRequest>')
                r = requests.post(TRADING_URL, headers={
                    "X-EBAY-API-CALL-NAME": "GetUser", "X-EBAY-API-SITEID": "0",
                    "X-EBAY-API-COMPATIBILITY-LEVEL": "1209",
                    "X-EBAY-API-IAF-TOKEN": get_access_token(), "Content-Type": "text/xml"},
                    data=xml.encode("utf-8"), timeout=30)
                _my_seller = (ET.fromstring(r.content).findtext("e:User/e:UserID", default="", namespaces=NS) or "").lower()
            except Exception as e:
                print("自分のユーザー名を取得できませんでした:", e)
                _my_seller = ""
    return _my_seller

# ---------- 携帯ゲーム機（ガラケーと同じ仕組みで扱う） ----------
# mercari: メルカリの本体カテゴリ、ebay_cat: eBayのカテゴリ（Video Game Consoles）
GAMES = {
    "PSP-1000": {"brand": "Sony", "platform": "Sony PSP", "series": "Sony PSP-1000", "mercari": "7076"},
    "PSP-2000": {"brand": "Sony", "platform": "Sony PSP", "series": "Sony PSP-2000", "mercari": "7076"},
    "PSP-3000": {"brand": "Sony", "platform": "Sony PSP", "series": "Sony PSP-3000", "mercari": "7076"},
    "PCH-1000": {"brand": "Sony", "platform": "Sony PlayStation Vita", "series": "Sony PS Vita PCH-1000", "mercari": "7058"},
    "PCH-2000": {"brand": "Sony", "platform": "Sony PlayStation Vita", "series": "Sony PS Vita PCH-2000", "mercari": "7058"},
    # 任天堂の携帯機：メルカリは機種ごとの本体カテゴリ（exact）、Yahoo!フリマは呼び名（alias）で見分ける
    "DMG-01": {"brand": "Nintendo", "platform": "Nintendo Game Boy", "series": "Nintendo Game Boy DMG-01",
               "mercari": "7181", "exact": True, "yahoo_q": "ゲームボーイ 本体", "ebay_q": "Game Boy DMG-01",
               "alias": r"ゲームボーイ|GAME ?BOY|DMG", "neg": r"ポケット|カラー|アドバンス|ミクロ|ライト|POCKET|COLOR|ADVANCE|MICRO|GBA|GBC|SP"},
    "MGB-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Pocket", "series": "Nintendo Game Boy Pocket MGB-001",
                "mercari": "8907", "exact": True, "yahoo_q": "ゲームボーイポケット", "ebay_q": "Game Boy Pocket",
                "alias": r"ゲームボーイ\s*ポケット|GAME ?BOY ?POCKET|MGB", "neg": r"ライト|カラー|LIGHT|COLOR"},
    "CGB-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Color", "series": "Nintendo Game Boy Color CGB-001",
                "mercari": "8908", "exact": True, "yahoo_q": "ゲームボーイカラー", "ebay_q": "Game Boy Color CGB-001",
                "alias": r"ゲームボーイ\s*カラー|GAME ?BOY ?COLOU?R|GBC|CGB", "neg": r"アドバンス|ADVANCE"},
    "AGB-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Advance", "series": "Nintendo Game Boy Advance AGB-001",
                "mercari": "7174", "exact": True, "yahoo_q": "ゲームボーイアドバンス", "ebay_q": "Game Boy Advance AGB-001",
                "alias": r"アドバンス|ADVANCE|GBA|AGB", "neg": r"SP|ミクロ|MICRO|AGS|OXY"},
    "AGS-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Advance SP", "series": "Nintendo Game Boy Advance SP AGS-001",
                "mercari": "8904", "exact": True, "yahoo_q": "ゲームボーイアドバンスSP", "ebay_q": "Game Boy Advance SP",
                "alias": r"アドバンス\s*SP|ADVANCE ?SP|GBA ?SP|AGS", "neg": r"$^"},
    "OXY-001": {"brand": "Nintendo", "platform": "Nintendo Game Boy Micro", "series": "Nintendo Game Boy Micro OXY-001",
                "mercari": "7178", "exact": True, "yahoo_q": "ゲームボーイミクロ", "ebay_q": "Game Boy Micro",
                "alias": r"(ゲームボーイ|GAME\s*BOY|GB)\s*(ミクロ|MICRO)|OXY", "neg": r"経済|テキスト|演習"},
    "USG-001": {"brand": "Nintendo", "platform": "Nintendo DS Lite", "series": "Nintendo DS Lite USG-001",
                "mercari": "8899", "exact": True, "yahoo_q": "DS Lite", "ebay_q": "Nintendo DS Lite",
                "alias": r"DS\s*(LITE|ライト)|DSL|USG", "neg": r"3DS|DSI"},
    "TWL-001": {"brand": "Nintendo", "platform": "Nintendo DSi", "series": "Nintendo DSi TWL-001",
                "mercari": "8898", "exact": True, "yahoo_q": "DSi", "ebay_q": "Nintendo DSi TWL-001",
                "alias": r"DSI|TWL", "neg": r"3DS|LL|XL"},
    "CTR-001": {"brand": "Nintendo", "platform": "Nintendo 3DS", "series": "Nintendo 3DS CTR-001",
                "mercari": "7022", "exact": True, "yahoo_q": "3DS", "ebay_q": "Nintendo 3DS CTR-001", "region_lock": True,
                "alias": r"3DS|CTR", "neg": r"NEW|ニュー|LL|XL|2DS"},
    "SPR-001": {"brand": "Nintendo", "platform": "Nintendo 3DS LL", "series": "Nintendo 3DS LL SPR-001",
                "mercari": "7038", "exact": True, "yahoo_q": "3DS LL", "ebay_q": "Nintendo 3DS LL SPR-001", "region_lock": True,
                "alias": r"3DS\s*(LL|XL)|SPR", "neg": r"NEW|ニュー|2DS"},
    "RED-001": {"brand": "Nintendo", "platform": "New Nintendo 3DS LL", "series": "New Nintendo 3DS LL RED-001",
                "mercari": "8882", "exact": True, "yahoo_q": "New 3DS LL", "ebay_q": "New Nintendo 3DS LL", "region_lock": True,
                "alias": r"(NEW|ニュー)\s*(ニンテンドー)?\s*3DS\s*(LL|XL)|RED-001", "neg": r"2DS"},
    # 2026/10/21 追加（eBayで売れた台数と利益を調べて選んだもの）。DSi LL はメルカリでは DSi と同じカテゴリ
    "UTL-001": {"brand": "Nintendo", "platform": "Nintendo DSi XL", "series": "Nintendo DSi LL XL UTL-001",
                "mercari": "8898", "exact": True, "yahoo_q": "DSi LL", "ebay_q": "Nintendo DSi XL",
                "alias": r"DSI\s*(LL|XL)|UTL", "neg": r"3DS",
                "region_note": "This Japanese DSi XL plays Nintendo DS games from any region. DSi-enhanced games and DSiWare are Japanese region only."},
    # 古いゲーム機（液晶の不具合が多いので最低利益を高めにする：RETRO_MIN_PROFIT）
    "WSC-001": {"brand": "Bandai", "platform": "Bandai WonderSwan Color", "series": "Bandai WonderSwan Color WSC-001",
                "mercari": "7197", "exact": True, "yahoo_q": "ワンダースワンカラー", "ebay_q": "Bandai WonderSwan Color",
                "alias": r"スワン\s*カラー|SWAN\s*COLOU?R|WSC", "neg": r"クリスタル|CRYSTAL|SC-001"},
    # 2026/10/28 追加（eBayで過去90日に売れた台数を確認済み）
    "MGB-101": {"brand": "Nintendo", "platform": "Nintendo Game Boy Light", "series": "Nintendo Game Boy Light MGB-101",
                "mercari": "7181", "exact": True, "yahoo_q": "ゲームボーイライト", "ebay_q": "Game Boy Light",
                "alias": r"ゲームボーイ\s*ライト|GAME ?BOY ?LIGHT|MGB-?101", "neg": r"ポケット|カラー|アドバンス|POCKET|COLOR|ADVANCE"},
    "SCT-001": {"brand": "Bandai", "platform": "Bandai WonderSwan", "series": "Bandai SwanCrystal SCT-001",
                "mercari": "7197", "exact": True, "yahoo_q": "スワンクリスタル", "ebay_q": "Bandai SwanCrystal",
                "alias": r"スワン\s*クリスタル|SWAN\s*CRYSTAL|SCT-?001", "neg": r"$^"},
    "PI-TG6": {"brand": "NEC", "platform": "NEC PC Engine", "series": "NEC PC Engine GT PI-TG6",
               "mercari": "7231", "exact": True, "yahoo_q": "PCエンジンGT", "ebay_q": "PC Engine GT",
               "alias": r"PC\s*エンジン\s*GT|PC\s*ENGINE\s*GT|TURBO\s*EXPRESS|PI-?TG6", "neg": r"$^"},
    "NEOP-10": {"brand": "SNK", "platform": "SNK Neo Geo Pocket Color", "series": "SNK Neo Geo Pocket Color",
                "mercari": "7228", "exact": True, "yahoo_q": "ネオジオポケットカラー", "ebay_q": "Neo Geo Pocket Color",
                "alias": r"ネオジオ\s*ポケット\s*カラー|NEO\s*-?\s*GEO\s*POCKET\s*COLOU?R|NGPC", "neg": r"$^"},
    # リサーチ用に追加（仕入れが2万円を超えるものは自動で外れる）
    "KTR-001": {"brand": "Nintendo", "platform": "New Nintendo 3DS", "series": "New Nintendo 3DS KTR-001",
                "mercari": "8885", "exact": True, "yahoo_q": "New 3DS", "ebay_q": "New Nintendo 3DS KTR-001", "region_lock": True,
                "alias": r"(NEW|ニュー)\s*(ニンテンドー)?\s*3DS|KTR", "neg": r"LL|XL|2DS"},
    "JAN-001": {"brand": "Nintendo", "platform": "New Nintendo 2DS XL", "series": "New Nintendo 2DS LL XL JAN-001",
                "mercari": "8888", "exact": True, "yahoo_q": "New 2DS LL", "ebay_q": "New Nintendo 2DS XL", "region_lock": True,
                "alias": r"2DS\s*(LL|XL)|JAN-001", "neg": r"$^"},
    "PSP-N1000": {"brand": "Sony", "platform": "Sony PSP Go", "series": "Sony PSP Go N1000",
                  "mercari": "8900", "exact": True, "yahoo_q": "PSP go", "ebay_q": "Sony PSP Go",
                  "alias": r"PSP\s*-?\s*GO|N1000", "neg": r"$^"},
}


# ---------- デジカメ・MDウォークマン（ゲーム機と同じ仕組みで扱う） ----------
# 数字は eBay で過去90日に売れた中古の台数（2026年10月にセラーハブの売れ行き調査で確認）
CAMERA_MODELS = {"DSC-W830": 663, "DSC-W570": 670, "DSC-W800": 530, "DSC-W55": 383, "DSC-W120": 314,
                 "DSC-W350": 310, "DSC-W220": 174, "DSC-W50": 138, "DSC-W310": 132, "DSC-W100": 66, "DSC-W300": 53,
                 # 2026/10/21 追加
                 "DSC-T700": 582, "DSC-W80": 321, "DSC-TX1": 281, "DSC-W630": 227, "DSC-W320": 223,
                 # 2026/10/21 リサーチ用に追加（1,000件の枠がある間に出してみて、売れる機種を見つける）
                 "DSC-WX350": 242, "DSC-T77": 190, "DSC-T900": 157, "DSC-W810": 123, "DSC-W730": 121, "DSC-W610": 109,
                 "DSC-W150": 102, "DSC-T10": 81, "DSC-W170": 78, "DSC-W710": 78, "DSC-W200": 74, "DSC-T100": 72,
                 "DSC-W690": 68, "DSC-W650": 66, "DSC-WX200": 60, "DSC-T200": 57, "DSC-WX100": 48, "DSC-TX5": 41, "DSC-WX7": 40,
                 "DSC-T99": 376, "DSC-W530": 279, "DSC-W330": 221, "DSC-TX7": 156, "DSC-TX10": 145, "DSC-WX500": 137,
                 "DSC-W290": 128, "DSC-WX220": 116, "DSC-WX300": 111, "DSC-W510": 109, "DSC-W230": 102, "DSC-T90": 98,
                 "DSC-T70": 86, "DSC-TX30": 86, "DSC-TX66": 84, "DSC-T2": 78, "DSC-W560": 76, "DSC-T20": 73,
                 "DSC-TX55": 73, "DSC-WX50": 72, "DSC-TX20": 57, "DSC-WX80": 47, "DSC-T300": 41}
MD_MODELS = {"MZ-NE410": 62, "MZ-RH1": 29, "MZ-E620": 26, "MZ-N1": 22, "MZ-N910": 20, "MZ-N920": 15, "MZ-E900": 10,
             "MZ-N707": 51, "MZ-N505": 46}
for _m in CAMERA_MODELS:
    GAMES[_m] = {"kind": "camera", "brand": "Sony", "platform": "Sony Cyber-shot", "series": f"Sony Cyber-shot {_m}",
                 "mercari": "4007", "ebay_cat": "31388", "ebay_q": f"Sony Cyber-shot {_m}", "yahoo_q": _m}
for _m in MD_MODELS:
    GAMES[_m] = {"kind": "md", "brand": "Sony", "platform": "Sony MD Walkman", "series": f"Sony MD Walkman {_m}",
                 "mercari": "3951", "ebay_cat": "15056", "ebay_q": f"Sony MiniDisc {_m}", "yahoo_q": _m}
# ほかのメーカーのコンパクトデジカメ（型番: (メーカー, eBayの項目Series, 商品名に使う呼び名)）
OTHER_CAMERAS = {
    "S6300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6300"), "S8200": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S8200"),
    "S6200": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6200"), "S7000": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S7000"),
    "S9500": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S9500"), "S9300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S9300"),
    "S9900": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S9900"), "S3300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S3300"),
    "IXY-650": ("Canon", "Canon PowerShot", "Canon IXY 650 PowerShot ELPH 360 HS"),
    "IXY-180": ("Canon", "Canon PowerShot", "Canon IXY 180 PowerShot ELPH 180"),   # 2026/10/21 追加
    # 2026/10/21 リサーチ用に追加
    "S3500": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S3500"), "S4300": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S4300"),
    "S6500": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6500"), "S6800": ("Nikon", "Nikon Coolpix", "Nikon COOLPIX S6800"),
    **{_k: ("Nikon", "Nikon Coolpix", f"Nikon COOLPIX {_k}") for _k in
       ("S4100", "S6100", "S3600", "S9700", "S8100", "S6000", "S9100", "S3700", "S9600", "S8000", "S6600", "S5200")},
    # 2026/10/28 追加（eBayで過去90日に売れた台数を確認済み）
    **{f"IXY-{_k}": ("Canon", "Canon PowerShot", f"Canon IXY {_k}") for _k in
       ("200", "630", "600F", "610F", "190", "150", "420F", "640", "210", "160")},
    "SX620": ("Canon", "Canon PowerShot", "Canon PowerShot SX620 HS"),
    "SX720": ("Canon", "Canon PowerShot", "Canon PowerShot SX720 HS"),
}
for _m, (_b, _s, _n) in OTHER_CAMERAS.items():
    GAMES[_m] = {"kind": "camera", "brand": _b, "platform": _s, "series": _n, "ser_name": _s,
                 "mercari": "4007", "ebay_cat": "31388", "ebay_q": _n, "yahoo_q": _n.split(" PowerShot")[0]}
for _m in OTHER_CAMERAS:
    GAMES[_m]["need_brand"] = True   # 型番が短く他社と重なる（例：富士フイルム FinePix S8200）ので、商品名にメーカー名が必須
GAMES["IXY-650"]["ebay_q"] = "Canon PowerShot ELPH 360 HS"   # eBayでは主にこの名前で売られている
GAMES["IXY-180"]["ebay_q"] = "Canon PowerShot ELPH 180"
# カメラ・MDのメーカー名（ほかのメーカー名が商品名にあれば、同じ型番の別商品として外す）
CAMERA_BRANDS = {
    "Sony": r"ソニー|SONY|CYBER-?SHOT|サイバーショット|WALKMAN|ウォークマン|DISCMAN|ディスクマン|HANDYCAM|ハンディカム",
    "Nikon": r"ニコン|NIKON|COOLPIX|クールピクス",
    "Canon": r"キヤノン|キャノン|CANON|IXY|POWERSHOT|パワーショット",
    "Fujifilm": r"富士フイルム|富士フィルム|フジフイルム|フジフィルム|FUJI|FINEPIX|ファインピクス",
    "Olympus": r"オリンパス|OLYMPUS",
    "Panasonic": r"パナソニック|PANASONIC|LUMIX|ルミックス|SHOCKWAVE|ショックウェーブ",
    "Casio": r"カシオ|CASIO|EXILIM|エクシリム|G-?SHOCK|Gショック|ジーショック",
    "Pentax": r"ペンタックス|PENTAX",
    "Ricoh": r"リコー|RICOH",   # 2026/10/28 リコーのデジカメを扱うためペンタックスと分けた
    "Kodak": r"コダック|KODAK",
    "Seiko": r"セイコー|SEIKO",
    "Citizen": r"シチズン|CITIZEN",
    "Aiwa": r"アイワ|AIWA",
    "Victor": r"ビクター|VICTOR|JVC",
}


# ---------- 2026/10/28 追加ジャンル：CCDデジカメ（他メーカー）・カセット/CDウォークマン・ビデオカメラ・時計 ----------
# 型番の書き方がゆれる・型番が短いものは alias（呼び名の正規表現）でも見分ける。メーカー名が商品名にないものは外す（need_brand）
# ship：送料の見込み（円）。設定シートの送料より高いときだけ使う（ビデオカメラは箱が大きく重いので高め）
NEW_GENRES = {
    "camera": {"mercari": "4007", "ebay_cat": "31388", "yahoo": 19801},
    "cassette": {"mercari": "3952", "ebay_cat": "15053", "yahoo": 49489},
    # CD：直径14cmで今の箱（22×13×6cm）に入らない。20×20×6cmなら0.5kgの料金だが、箱次第で上がるので余裕を見る
    "cd": {"mercari": "3950", "ebay_cat": "15054", "yahoo": 49487, "ship": 4000},
    # ビデオカメラ：25×18×10cmほどの箱で1kgの料金（東海岸あてで約5,600円）
    "video": {"mercari": "4012", "ebay_cat": "11724", "yahoo": 49429, "ship": 5600},
    "watch": {"mercari": "39", "ebay_cat": "31387", "yahoo": 1647},
}


def _code_re(code):
    """型番 → 区切り（ハイフン・空白）のゆれを許す正規表現"""
    return r"[-\s]?".join(re.escape(c) for c in re.sub(r"[-\s]", "", code.upper()))


def _add_new(keyword, kind, brand, series, ebay_q, code=None, brand_re=None, suffix="", **extra):
    """keyword：設定シートの検索キーワード（メルカリの検索語）。code：商品名に出てくる型番"""
    g = NEW_GENRES[kind]
    code = code or keyword
    gi = {"kind": kind, "brand": brand, "platform": series, "series": f"{series} {code}", "ser_name": series,
          "mercari": g["mercari"], "ebay_cat": g["ebay_cat"], "ebay_q": ebay_q, "yahoo_q": keyword,
          "need_brand": True, "code": code}
    pre = rf"(?:{brand_re})[^\n]{{0,20}}?" if brand_re else ""
    gi["alias"] = pre + r"(?<![A-Z0-9])" + _code_re(code) + (f"(?:{suffix})?" if suffix else "") + r"(?![A-Z0-9])"
    if g.get("ship"):
        gi["ship"] = g["ship"]
    gi.update(extra)
    GAMES[keyword.upper().replace(" ", "-")] = gi


_FUJI = r"FINEPIX|ファインピクス|富士フ[イィ]ルム|FUJIFILM"
for _c in ("F31fd", "F30", "F200EXR", "F100fd", "Z1", "Z3", "Z5fd", "F70EXR", "J10", "Z200fd"):
    _add_new(f"FinePix {_c}", "camera", "Fujifilm", "Fujifilm FinePix", f"Fujifilm FinePix {_c}", code=_c, brand_re=_FUJI)
for _c in ("DMC-FX01", "DMC-FX07", "DMC-FX9", "DMC-FX35", "DMC-FX37", "DMC-FX33", "DMC-FS7", "DMC-FX30", "DMC-FX40"):
    _add_new(_c, "camera", "Panasonic", "Panasonic Lumix", f"Panasonic Lumix {_c}")
    GAMES[_c]["alias"] = rf"(?:LUMIX|ルミックス)[^\n]{{0,15}}?(?<![A-Z0-9])(?:DMC-?)?{_code_re(_c[4:])}(?![A-Z0-9])|" + GAMES[_c]["alias"]
for _c in ("EX-Z1000", "EX-Z850", "EX-S600", "EX-Z750", "EX-S500", "EX-Z500", "EX-Z3", "EX-Z55", "EX-S770", "EX-Z100"):
    _add_new(_c, "camera", "Casio", "Casio Exilim", f"Casio Exilim {_c}")
for _c in ("CX1", "CX2", "CX3", "CX4", "R10"):
    _add_new(f"RICOH {_c}", "camera", "Ricoh", "Ricoh", f"Ricoh {_c}", code=_c, brand_re=r"RICOH|リコー")
for _c in ("WM-EX1", "WM-EX2", "WM-EX5", "WM-EX9", "WM-EX20", "WM-EX600", "WM-EX610", "WM-EX615", "WM-EX631",
           "WM-EX651", "WM-EX655", "WM-EX677", "WM-EX808", "WM-EX900", "WM-EX910", "WM-EX921"):
    _add_new(_c, "cassette", "Sony", "Sony Walkman", f"Sony Walkman {_c}")
for _c in ("D-NE730", "D-NE830", "D-NE920", "D-NE10", "D-NE20", "D-EJ1000", "D-EJ955", "D-E01", "D-NE1", "D-EJ800"):
    _add_new(_c, "cd", "Sony", "Sony Discman", f"Sony Discman {_c}")
for _c in ("SL-CT520", "SL-CT820"):
    _add_new(_c, "cd", "Panasonic", "Panasonic", f"Panasonic {_c}")
for _c, _sx in (("HDR-CX370", "V"), ("HDR-CX590", "V"), ("HDR-CX420", ""), ("HDR-TG1", ""),
                ("DCR-PC101", ""), ("DCR-PC300", "K"), ("DCR-PC350", "")):
    _add_new(_c, "video", "Sony", "Sony Handycam", f"Sony Handycam {_c}", suffix=_sx)
for _c, _mv in (("SARB033", "Automatic"), ("SARB035", "Automatic"), ("SARB017", "Automatic"),
                ("SARB059", "Automatic"), ("SARB065", "Automatic"), ("SARY055", "Automatic")):
    _add_new(_c, "watch", "Seiko", "Seiko", f"Seiko {_c}", movement=_mv, display="Analog")
for _c, _mv, _sx in (("GW-M5610", "Quartz (Solar)", "U"), ("GW-5000", "Quartz (Solar)", "U"),
                     ("DW-5000", "Quartz", ""), ("GW-B5600", "Quartz (Solar)", "")):
    _add_new(_c, "watch", "Casio", "Casio G-Shock", f"Casio G-Shock {_c}", suffix=_sx, movement=_mv, display="Digital")
NEW_KINDS = {"cassette", "cd", "video", "watch"}
# 時計：偽物・部品だけの出品を外す（商品名で判断）
WATCH_FAKE_RE = re.compile(r"レプリカ|コピー|スーパーコピー|偽物|風(?![防])|タイプ|互換|オマージュ|カスタム|MOD|"
                           r"(ベルト|バンド|ケース|ベゼル|風防|ガラス|文字盤|ムーブメント|箱|保証書)(のみ|だけ|単品)|部品|パーツ", re.I)
WATCH_MIN_PRICE_RATIO = 0.3   # 時計は相場のこの割合未満なら偽物・故障の可能性が高い


def brand_reject_reason(t, gi):
    """カメラ・MD：商品名に別メーカー名がある／（必要な機種で）自社のメーカー名がない"""
    u = unicodedata.normalize("NFKC", t or "").upper()
    own = gi.get("brand", "")
    for b, pat in CAMERA_BRANDS.items():
        if b != own and re.search(pat, u):
            return f"別メーカーの商品の可能性（{b}）"
    if gi.get("need_brand") and not re.search(CAMERA_BRANDS.get(own, r"$^"), u):
        return "商品名にメーカー名がありません（別メーカーの同じ型番の可能性）"
    return ""


# ゲーム機以外で「本体ではない・動かない」可能性が高い商品名
OTHER_HARD_WORDS = re.compile(
    r"(ケース|バッテリー|電池|充電器|ストラップ|説明書|取説|箱|リモコン|イヤホン|ディスク|パーツ|部品|ケーブル)(のみ|単品|だけ)|"
    r"部品取り|動作未確認|稼働未確認|未確認|ジャンク|ジャング|故障|不動|訳あり|訳アリ|ワケアリ|難あり|画面割れ|液晶割れ|レンズ割れ|カビ|(?i:junk)")


def match_game(title, key):
    """商品名がその機種か（型番が書いてあるか、呼び名が合っていて別機種の名前がない）"""
    gi = GAMES.get(key) or game_info(key)   # 2026/10/28 「FinePix F31fd」のような空白入りのキーワードでも見つける
    t = unicodedata.normalize("NFKC", title or "").upper()
    if has_model(t, key):
        return True
    if not gi or not gi.get("alias"):
        return False
    return bool(re.search(gi["alias"], t)) and not re.search(gi.get("neg", r"$^"), t)


# ゲーム機は「本体カテゴリ」に部品・箱・ソフト・チラシなどが混ざるので、商品名を厳しく確認する
GAME_HARD_WORDS = re.compile(
    r"フェイスプレート|プレートのみ|チラシ|広告|ポスター|カタログ|攻略本|非売品|販促|ステッカー|シール|スキン|"
    r"交換用|修理用|部品取り|パーツ|ドライバー|互換|外箱|内箱|空箱|箱のみ|シェル|外装|ハウジング|"
    r"ソフト|カセット|UMD|説明書|取説|充電器|ACアダプタ|ケーブル|ポーチ|ケース|カバー|フィルム|"
    r"動作未確認|未確認|ジャンク|ジャング|故障|不動|訳あり|難あり|画面割れ|液晶割れ|"
    r"バッテリー|電池|充電スタンド|充電台|クレードル|スタンド|ホルダ|ディスプレイ|液晶パネル|(?i:junk)|訳アリ|ワケアリ")
# ゲーム機の商品名に、本体らしさ（本体・動作・メーカー名・色など）が1つもないものはソフトや周辺機器の可能性が高い（2026/10/28）
GAME_BODYISH_RE = re.compile(
    r"本体|動作|稼働|任天堂|NINTENDO|ニンテンドー|SONY|ソニー|PLAYSTATION|プレイステーション|PSP|VITA|ゲームボーイ|GAME ?BOY|"
    r"LITE|ライト|LL|XL|ブラック|ホワイト|シルバー|ブルー|レッド|ピンク|グリーン|イエロー|ゴールド|ネイビー|パープル|オレンジ|"
    r"ブラウン|グレー|ワイン|メタリック|クリア|BLACK|WHITE|SILVER|BLUE|RED|PINK|GREEN|YELLOW|GOLD", re.I)
# 説明文に「本体は付属しません」など、本体が入っていないことが書かれている出品（本体に見せかけた出品を防ぐ）
NO_BODY_RE = re.compile(
    r"本体(は|が)?\s*(付属し(ません|ない)|含まれ(ません|ない)|入って(いません|いない|おりません)|ありません|ございません|"
    r"付きません|つきません|なし|無し|ない|ナシ)|本体\s*(なし|無し|ナシ|別売)|"
    r"(外箱|空箱|箱|ケース|説明書|付属品|ソフト|カセット|シェル|外装|プレート|パーツ)(だけ|のみ)の(出品|販売)|"
    r"(外箱|空箱|付属品|説明書|ケース|シェル|外装)のみ(です|となります|の出品|の販売|になります)|"
    r"(画像|写真)の(外箱|箱)のみ|部品取り|パーツ取り|"
    # 2026/10/29 展示用の模型（モックアップ）は携帯電話ではないので外す
    r"モックアップ|モック品|模型|ダミー(機|携帯|ケータイ)|ディスプレイサンプル|展示用(の)?(サンプル|見本)|"
    # 2026/10/29 時計の交換用ベルト・バンドだけの出品（「替えベルト付き」の本体は外さない）
    r"(交換|替え|換え)(用)?の?\s*(ベルト|バンド)(?![^。\n]{0,3}(付|つき|あり|有|も))|(ベルト|バンド)\s*(のみ|だけ|単体)")


# 動作未確認・ジャンクなど、動くと言えない出品（普通の中古品として売ると返品・悪い評価になる）
JUNK_RE = re.compile(
    r"動作(は)?未確認|稼働未確認|動作未チェック|動作確認(は|を)?(して|でき|行って|おこなって)(い|お)?(ません|ない|ず|りません)|"
    r"動作確認(が|は)?(取れて|とれて|出来て)(い|お)?(ません|ない|りません)|動作確認(が|は)?(取れ|とれ|出来|でき)(ず|ません)|"
    r"動作(の)?確認(が|は|を|も)?(でき|出来|取れ|とれ|して|行って|おこなって)(い|お)?(ない|ません|ず|りません)|"
    r"(動作|通電)(の)?(確認|チェック)(が|は)?(できない|出来ない|できず|出来ず|不可|していない)|"
    r"(動作|操作|通電)(の|は|が)?(確認|チェック)(が|は|を|も)?(されて|しておら|できておら|取れておら|行われて)(い|お)?(ない|ません|ず|りません)|"
    r"電源(を|は)?(入れて|入れておら)(い|お)?(ない|ません|ず|りません)|"
    r"訳あり|訳アリ|ワケアリ|わけあり|訳有り?|"
    r"ジャン[クグ](?!では|じゃ|ではな)|(?i:\bjunk\b)|ガラクタ|がらくた|通電(確認)?のみ|電源(が|は)?(入りません|入らない|つきません|つかない)|"
    r"起動(しません|しない|できません)|故障(品|中|しています|あり)|不動品|部品取り|画面割れ|液晶割れ|"
    r"(液晶|画面|モニター|ディスプレイ)(画面)?(が|は|に|には|も)?(何も|全く|まったく|一切|全然)?"
    r"(映ら|写ら|映りません|写りません|表示され(ない|ません|ず)|表示しな|つかな|点かな|点灯しな|真っ暗|真っ黒)")


# 不具合の記載（「不具合なし」「ムラはありません」のような否定は除く）
DEFECT_RE = re.compile(
    r"(誤作動|誤動作|不具合|不良|故障|モヤ|ムラ|シミ|ドット抜け|画面焼け|液晶焼け|焼き付き|反応しない|反応が悪い|"
    r"効かない|作動しない|動かない|映らない|写らない|ピントが合わない|切り替わらない|充電できない|"
    r"エラーが出|エラー表示|ノイズが|音が出ない|音飛び|黒点|黒い点|黄ばみ|勝手に|[JＪ]グレード|[JＪ]ランク|"
    # 2026/10/29 液晶の影・斑模様など（「画面に影響」は除く）
    r"(液晶|画面|モニター|ディスプレイ)[^。\n]{0,12}?(影(?!響)|斑|まだら|にじみ|滲み|縦線|横線|線が入|線が出|線あり)|"
    r"斑模様|まだら模様|白斑|白いシミ|液晶漏れ|液漏れ)"
    r"(?![^。\n、]{0,5}?(なし|無し|ナシ|ない|なく|無く|ありません|ございません|見当たりません|見られません|確認されません))")


def junk_reason(text):
    t = unicodedata.normalize("NFKC", text or "")
    m = JUNK_RE.search(t)
    if m:
        return f"動作が確認できない出品（{m.group(0)[:15]}）"
    m = DEFECT_RE.search(t)
    return f"不具合の記載がある出品（{m.group(0)[:15]}）" if m else ""


# 商品名の「〇〇様」「〇〇様専用」は特定の人向けの専用出品（ほかの人は買えない・買うとトラブル）
RESERVED_RE = re.compile(r"(?<![仕模同多異有王皇神殿])様(?![々子式相変])|専用出品|お取り置き|取り置き中|購入不可")


def reserved_reason(title):
    t = unicodedata.normalize("NFKC", title or "")
    m = RESERVED_RE.search(t)
    return f"専用出品の可能性（{m.group(0)}）" if m else ""


def no_body_reason(text):
    t = unicodedata.normalize("NFKC", text or "")
    m = NO_BODY_RE.search(t)
    return f"説明に本体が入っていない旨の記載（{m.group(0)[:20]}）" if m else ""


GAME_MIN_PRICE_RATIO = 0.12   # 相場のこれ未満の値段は部品・ジャンクの可能性が高い


def game_reject_reason(title, model, price_jpy=None, sale_usd=None, settings=None):
    """ゲーム機として出品してはいけない理由（なければ空文字）"""
    t = unicodedata.normalize("NFKC", title or "")
    if not match_game(t, model):
        return "商品名に機種名がありません"
    kind = (game_info(model) or {}).get("kind")
    if kind in ("camera", "md") or kind in NEW_KINDS:
        why = brand_reject_reason(t, game_info(model))
        if why:
            return why
        m = OTHER_HARD_WORDS.search(t)
        if m:
            return f"本体ではない・動かない可能性（{m.group(0)}）"
        if kind == "watch":
            m = WATCH_FAKE_RE.search(t)
            if m:
                return f"偽物・部品の可能性（{m.group(0)}）"
        ratio = WATCH_MIN_PRICE_RATIO if kind == "watch" else GAME_MIN_PRICE_RATIO
        try:
            if price_jpy and sale_usd and float(price_jpy) < float(sale_usd) * usd_jpy(settings or {}) * ratio:
                return "相場に比べて安すぎる（部品・ジャンクの可能性）"
        except (TypeError, ValueError):
            pass
        return ""
    words = [w for w in GAME_HARD_WORDS.findall(t)
             if not (w in ("ソフト", "カセット", "UMD", "説明書", "取説", "充電器", "ACアダプタ", "ケーブル",
                           "ポーチ", "ケース", "カバー", "フィルム", "外箱", "内箱", "バッテリー", "電池",
                           "充電スタンド", "充電台", "クレードル", "スタンド", "ホルダ")
                     and "本体" in t and "のみ" not in t)]   # 「本体＋ソフト付き」「本体 箱付き」は本体
    if words:
        return f"本体ではない可能性（{words[0]}）"
    if not has_model(t.upper(), model) and not GAME_BODYISH_RE.search(t):
        return "本体かどうか分からない（ソフト・周辺機器の可能性）"
    if re.search(r"(?<!本体)のみ", t):
        return "本体以外の単品の可能性（〜のみ）"
    try:
        if price_jpy and sale_usd and float(price_jpy) < float(sale_usd) * usd_jpy(settings or {}) * GAME_MIN_PRICE_RATIO:
            return "相場に比べて安すぎる（部品・ジャンクの可能性）"
    except (TypeError, ValueError):
        pass
    return ""
GAME_EBAY_CATEGORY = "139971"
# ゲームボーイ系は古くて仕入れが高く数も少ないので、利益が大きく取れるときだけ扱う
GAMEBOY_MODELS = {"DMG-01", "MGB-001", "CGB-001", "AGB-001", "AGS-001", "OXY-001"}
GAMEBOY_MIN_PROFIT = 2000   # 2026/10/26 ほかの商品と同じ2,000円にそろえた（以前は10,000円）
RETRO_MODELS = {"WSC-001", "NEOP-10"}   # 古いゲーム機：液晶の不具合・返品が多いので利益が大きいときだけ
RETRO_MIN_PROFIT = 2000   # 2026/10/26 ほかの商品と同じ2,000円にそろえた（以前は4,000円）

# 出品枠（月350件・2万ドル）を有効に使うためのルール
MAX_PER_MODEL = 3   # 同じ機種の出品は3件まで（仕入れが安い＝利益が大きいものを残す）
SWAP_MARGIN = 1000  # 枠がいっぱいのとき、利益がこれ以上大きい候補なら一番利益の小さい出品と入れ替える
# 過去90日にeBayで売れた台数（多い機種ほど優先して出品枠を使う）
SOLD90 = {"902KC": 41, "KY-42C": 23, "601SH": 20, "A202KC": 18, "P-01G": 18, "903KC": 16, "KYF31": 13,
          "701KC": 12, "SH-02L": 11, "805SH": 9, "SH-01J": 8, "A203KC": 8, "A205SH": 7, "602SH": 7, "KYF42": 6,
          "PCH-1000": 80, "PCH-2000": 77, "PSP-3000": 73, "PSP-1000": 41, "RED-001": 34, "USG-001": 22,
          "CTR-001": 17, "CGB-001": 15, **CAMERA_MODELS, **MD_MODELS,
          **{k: 40 for k in OTHER_CAMERAS}, "IXY-180": 120, "UTL-001": 80, "WSC-001": 25, "NEOP-10": 36,
          "IXY-200": 54, "IXY-630": 39, "IXY-600F": 29, "IXY-610F": 25, "IXY-190": 26, "IXY-150": 22, "IXY-420F": 21,
          "IXY-640": 13, "IXY-210": 12, "IXY-160": 11, "SX620": 80, "SX720": 80, "MGB-101": 51, "SCT-001": 36,
          "PI-TG6": 26, "806SH": 9,   # 2026/10/28 追加（SX620・SX720は海外の売り手も多いので控えめに80）
          "KTR-001": 97, "JAN-001": 163, "PSP-N1000": 67, "S4100": 170, "S6100": 169, "S3600": 147, "S9700": 141,
          "S8100": 135, "S6000": 134, "S9100": 134, "S3700": 106, "S9600": 75, "S8000": 67, "S6600": 64, "S5200": 62,
          "S3500": 190, "S4300": 123, "S6500": 114, "S6800": 91}


def model_key(q):
    return re.sub(r"[-\s]", "", (q or "").upper())


SOLD90_BY_KEY = {model_key(k): v for k, v in SOLD90.items()}


def cap_for(q):
    """機種ごとの出品数の上限。
    2026/10/29 無料出品枠が1,000件（ベーシックストア）の間は、売れにくい機種も含めて全機種20件まで出す。
    250件の無料プランに戻したら以前のルール：90日の販売台数÷4、最低3件・最大20件"""
    if FREE_LISTINGS >= 1000:
        return 20
    return max(MAX_PER_MODEL, min(20, round(SOLD90_BY_KEY.get(model_key(q), 0) / 4)))
MAX_COST_DEFAULT = 20000      # 仕入れ値の上限（円）。設定シートで max_cost を渡せばそちらを優先
PROFIT_RATIO = 0              # 2026/10/22 仕入れ値の割合はやめて、最低利益（2,000円・広告付きは1,500円）だけにした（以前は0.3）


def game_info(model):
    key = re.sub(r"[-\s]", "", (model or "").upper())
    for k, v in GAMES.items():
        if k.replace("-", "") == key:
            return v
    return None


def max_cost(settings):
    try:
        return float(settings.get("max_cost") or MAX_COST_DEFAULT)
    except (TypeError, ValueError):
        return MAX_COST_DEFAULT


def min_profit_for(cost, settings, model=""):
    base = max(float(settings.get("min_profit") or 2000), float(cost or 0) * PROFIT_RATIO)
    if (model or "").strip().upper() in GAMEBOY_MODELS:
        base = max(base, GAMEBOY_MIN_PROFIT)
    elif (model or "").strip().upper() in RETRO_MODELS:
        base = max(base, RETRO_MIN_PROFIT)
    return base


def _ebay_market_usd(query):
    r = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        params={"q": (game_info(query) or {}).get("ebay_q", query),
                "category_ids": (game_info(query) or {}).get("ebay_cat", GAME_EBAY_CATEGORY) if game_info(query) else "9355",
                "filter": "buyingOptions:{FIXED_PRICE},conditions:{USED}", "limit": 50},
        headers={"Authorization": f"Bearer {app_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        timeout=30,
    )
    r.raise_for_status()
    prices = []
    for it in r.json().get("itemSummaries", []):
        if (it.get("seller", {}).get("username") or "").lower() == my_seller():
            continue   # 自分の出品は相場に入れない
        try:
            p = float(it["price"]["value"])
            ship = it.get("shippingOptions", [{}])[0].get("shippingCost", {}).get("value")
            prices.append(p + (float(ship) if ship else 0))
        except (KeyError, ValueError, IndexError):
            continue
    if not prices:
        return None, 0
    return round(statistics.median(prices), 2), len(prices)


_fx = None


def usd_jpy(settings):
    global _fx
    manual = str(settings.get("fx") or "").strip()
    if manual:
        return float(manual)
    if _fx is None:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=30)
        r.raise_for_status()
        _fx = float(r.json()["rates"]["JPY"])
    return _fx


def adj(sale_usd, settings):
    """出品中の価格は実際に売れる価格より高めなので、補正をかける"""
    return float(sale_usd) * float(settings.get("market_adj") or 80) / 100


AD_RATE = 3            # 広告率（%）：デジカメ・MD・ゲーム機だけに広告を付ける（ガラケーは付けない）
AD_PROFIT_RELIEF = 0    # 2026/10/22 広告付きも最低利益2,000円にそろえた（以前は500円下げて1,500円）


def ad_pct(model):
    return AD_RATE if game_info(model or "") else 0


DUTY_PCT = 12.5   # 2026/10/25 アメリカの関税の見込み（売れた値段の%）。出品者が払う（DDP）ので手数料と同じように差し引く


def duty_pct(settings):
    """関税の見込み（%）。設定シートの duty_pct があればそちらを使う"""
    try:
        return float(settings.get("duty_pct") if settings.get("duty_pct") not in (None, "") else DUTY_PCT)
    except (TypeError, ValueError):
        return DUTY_PCT


def ship_for(settings, model=""):
    """送料の見込み（円）。設定シートの送料と、ジャンルごとの送料（重い・箱が大きいもの）の高いほう"""
    base = float(settings.get("ship_jpy") or 3000)
    return max(base, float((game_info(model or "") or {}).get("ship") or 0))


def profit(cost_jpy, sale_usd, settings, model=""):
    sale_usd = adj(sale_usd, settings)
    fx = usd_jpy(settings)
    fee = float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2) + ad_pct(model) + duty_pct(settings)
    ship = ship_for(settings, model)
    revenue = sale_usd * fx
    p = revenue * (1 - fee / 100) - ship - cost_jpy
    return round(p), (round(p / revenue * 100, 1) if revenue else 0)


LABEL = {"on_sale": "販売中", "sold": "売り切れ", "deleted": "削除・非公開",
         "unknown": "判定できず", "blocked": "メルカリにアクセスを拒否された",
         "auction": "オークション商品"}
MIN_COUNT = 10   # eBayの相場の件数がこれ未満の機種は候補にしない


# ---------- 同じ商品が複数のサイトに出ているかを写真で見分ける ----------
IMGHASH_FILE = os.path.join(".cache", "imgthumb.json")
_imghash = {}   # 仕入れ先URL → 1枚目の写真を16×16の白黒に縮めたもの（256バイト）
# 同じ商品なら「値段＋この金額」が小さいほうを残す（メルカリ優先。Yahoo!フリマは500円以上安いときだけ残す）
SRC_PENALTY = {"メルカリ": 0, "Yahoo!フリマ": 499, "ラクマ": 599}


def load_imghash():
    try:
        with open(IMGHASH_FILE, encoding="utf-8") as f:
            _imghash.update({k: bytes.fromhex(v) for k, v in json.load(f).items()})
    except FileNotFoundError:
        pass
    except Exception as e:
        print("写真の記録を読めませんでした:", str(e)[:80])


def save_imghash():
    try:
        os.makedirs(os.path.dirname(IMGHASH_FILE), exist_ok=True)
        with open(IMGHASH_FILE, "w", encoding="utf-8") as f:
            json.dump({k: v.hex() for k, v in _imghash.items()}, f)
    except Exception as e:
        print("写真の記録を保存できませんでした:", str(e)[:80])


def dhash_url(u):
    from PIL import Image
    import io
    r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("L").resize((16, 16), Image.LANCZOS).tobytes()


def src_hash(url, images):
    if url in _imghash:
        return _imghash[url]
    if not images:
        return None
    try:
        _imghash[url] = dhash_url(images[0])
    except Exception as e:
        print("写真を比べられませんでした:", str(e)[:80])
        return None
    return _imghash[url]


def same_img(a, b):
    """同じ写真か（縮めた白黒画像の明るさの差の平均が6以下）。
    同じお店が同じ場所で撮った別の個体（差は40以上）は同じとみなさない"""
    if not a or not b or len(a) != len(b):
        return False
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) <= 6


def src_score(url, price):
    return float(price or 0) + SRC_PENALTY.get(source_name(url), 0)


def main():
    load_imghash()
    try:
        _main()
    finally:
        save_imghash()


def _main():
    data = sheet("list")
    rows, settings = data["rows"], data.get("settings", {})
    excluded = excluded_sellers(data)   # 「除外者リスト」シートの出品者（この人の商品は候補にも出品にもしない）
    KNOWN_MODELS[:] = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "")) if k.strip()]
    targets = [r for r in rows if r["status"] in ("", "active")]
    # 出品中の商品を先に確認して利益を把握してから、候補を出品する（入れ替えの判断に使う）
    targets.sort(key=lambda r: 0 if r["ebay_item_id"] else 1)
    watch = [r for r in targets if r["ebay_item_id"]]
    research = [r for r in targets if not r["ebay_item_id"]]
    print(f"{now()} 監視 {len(watch)}件／仕入れ候補 {len(research)}件")
    if not targets:
        sheet("log", message=f"{now()} 正常終了：対象の商品なし")
        return

    # eBayの認証を最初に確認しておく（売り切れを見つけてから失敗するのを防ぐ）
    try:
        token = get_access_token() if watch else None
    except RuntimeError as e:
        # トークンが切れた・無効になった → すぐにメールで知らせる（このままだと売り切れても出品が止まらない）
        alert("【緊急】eBayとツールの接続が切れました",
              f"eBayのトークンが使えなくなったため、在庫チェックができません。\n\nエラー：{str(e)[:200]}\n\n{TOKEN_HOWTO}")
        raise
    updates, blocked, counts, stopped, researched, listed = [], 0, {}, 0, 0, 0
    sold_on_ebay, sold_n = {}, 0
    if watch:
        try:
            sold_on_ebay = ebay_sold_items(token)   # eBayで売れた商品（仕入れ元で買う必要がある）
            # 売れた商品を一番先に確認して、仕入れ先のURLをすぐメールで送る
            targets.sort(key=lambda r: 0 if r["ebay_item_id"] in sold_on_ebay else (1 if r["ebay_item_id"] else 2))
        except Exception as e:
            print("eBayで売れた商品の確認に失敗:", str(e)[:100])
    # ---- eBayでキャンセルが済んだ注文の行は、止めて「履歴」に移す（リストから消える） ----
    try:
        cancelled = ebay_cancelled_items(token)
    except Exception as e:
        cancelled = set()
        print("キャンセルされた注文の確認に失敗:", str(e)[:100])
    for r in rows:
        if r["ebay_item_id"] and r["ebay_item_id"] in cancelled and r["status"] != "stopped":
            updates.append({"row": r["row"], "status": "stopped",
                            "result": f"{now()} eBayの注文がキャンセルされました（出品を終了しました）"})
            sold_on_ebay.pop(r["ebay_item_id"], None)
            print("キャンセルされた注文の行を履歴に移します:", r["ebay_item_id"])
    if cancelled:
        rows = [r for r in rows if r["ebay_item_id"] not in cancelled]
        targets = [r for r in targets if r["ebay_item_id"] not in cancelled]
        watch = [r for r in watch if r["ebay_item_id"] not in cancelled]
    active_items, today = {}, datetime.now(JST).strftime("%m/%d")
    if watch:
        try:
            active_items = ebay_active_items(token)   # ウォッチ数・今の値段・出品日
            print(f"出品中 {len(active_items)}件／ウォッチあり {sum(1 for v in active_items.values() if v['watch'])}件")
        except Exception as e:
            print("出品中の商品の確認に失敗:", str(e)[:100])
    try:
        views = listing_views(active_items.keys())   # 閲覧数（売れない理由の予想に使う）
    except Exception as e:
        views = {}
        print("閲覧数の確認に失敗:", str(e)[:100])
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ja-JP",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        # 高速化：画像・動画・フォントは読み込まない（判定に不要。写真のURLはページ内に残る）
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("image", "media", "font") else route.continue_())
        desc_pending = []   # 説明文の理由で止める出品（20件を超えたら判定ミスとみなして止めない）
        listed_per_model = {}
        for x in rows:
            if x["ebay_item_id"] and x["status"] in ("", "active"):
                k = model_key(x.get("query"))
                listed_per_model[k] = listed_per_model.get(k, 0) + 1
        quota_hit = False   # eBayの出品枠の上限に達したら、入れ替えできるときだけ出品する
        free_used = None    # 今月使う無料出品枠（出品するときに初めて数える）
        free_full = False
        ebay_busy = False   # 2026/10/28 eBayが一時的に受け付けない（回数の上限など）→ この回は出品をやめて次の回へ
        day_full = False    # 今日の出品ペースの上限に達した
        unknown_twice = []  # 2回続けてメルカリの状態を判定できなかった出品（売り切れに気づけないおそれ）
        offer_targets = {}  # ウォッチされている出品 {出品番号: (今の値段, 利益2,000円の値段)}：値引きオファーを送る
        listed_profit = {}  # 出品中の商品の利益 {行番号: (機種, 利益, eBay出品番号)}

        def swap_out(model=None, cand_p=None):
            """候補の利益が出品中の一番小さい利益より SWAP_MARGIN 円以上多ければ、その出品を終了して枠を空ける"""
            pool = [(v[1], row, v) for row, v in listed_profit.items() if model is None or v[0] == model]
            if not pool or cand_p is None:
                return False
            low_p, low_row, (low_model, _, low_id) = min(pool)
            if cand_p < low_p + SWAP_MARGIN:
                return False
            ok, msg = stop_on_ebay(low_id, token or get_access_token())
            if not ok:
                return False
            updates.append({"row": low_row, "status": "stopped",
                            "result": f"利益が大きい商品（約{int(cand_p):,}円）と入れ替えのため終了（この出品の利益は約{int(low_p):,}円） → {msg}"})
            del listed_profit[low_row]
            listed_per_model[low_model] = max(0, listed_per_model.get(low_model, 1) - 1)
            return True

        dup_seen, dup_stop = [], []   # 出品中の商品の写真／同じ商品で止めるほう

        def dup_match(h, row_no):
            for d in dup_seen:
                if d["row"] != row_no and same_img(d["hash"], h):
                    return d
            return None

        def dup_note(r, price, images, ebay_id):
            """出品中の商品を登録。同じ写真の出品がすでにあれば、条件の悪いほうを止める予定に入れる"""
            h = src_hash(r["mercari_url"], images)
            if h is None:
                return
            me = {"row": r["row"], "hash": h, "ebay_item_id": ebay_id, "url": r["mercari_url"],
                  "price": price, "score": src_score(r["mercari_url"], price)}
            other = dup_match(h, r["row"])
            if not other:
                dup_seen.append(me)
                return
            lose, keep = (me, other) if me["score"] >= other["score"] else (other, me)
            lose["keep"] = keep
            dup_stop.append(lose)
            if lose is other:
                dup_seen.remove(other)
                dup_seen.append(me)

        for r in targets:
            yahoo = is_yahoo(r["mercari_url"])
            if yahoo:
                status, info, ydetail = check_yahoo(r["mercari_url"])
            else:
                status, info = check_mercari(page, r["mercari_url"])
            counts[status] = counts.get(status, 0) + 1
            upd = {"row": r["row"], "last_checked": now(), "result": LABEL[status]}

            title_now = (info or {}).get("title") or r.get("title") or ""
            acc = is_accessory(title_now, (info or {}).get("price"), r.get("ebay_usd"), settings)
            acc_why = acc_note = ""
            if is_rakuma(r["mercari_url"]):
                acc, acc_why = True, "ラクマからの仕入れはやめたため"   # 出品中・候補ともに止める
            if not acc and seller_id((info or {}).get("seller")) in excluded:
                acc, acc_why = True, "除外者リストの出品者の商品"   # 出品中・候補ともに止める
            if not acc and reserved_reason(title_now):
                acc_why = reserved_reason(title_now)   # 専用出品は買えないので候補・出品とも止める
                acc = True
            if not acc and game_info(r.get("query") or ""):
                acc_why = game_reject_reason(title_now, (r.get("query") or "").strip(),
                                             (info or {}).get("price"), r.get("ebay_usd"), settings)
                acc = bool(acc_why)
                if acc and r["ebay_item_id"] and acc_why.startswith("商品名にメーカー名がありません"):
                    acc = False   # 出品中のものはメーカー名がないだけでは止めない（知らせるだけ）
                    acc_note = f"【要確認】{acc_why}"
            if not acc and status == "on_sale":
                # 説明文も確認（本体なし・箱のみ・動作未確認・不具合などの記載があれば止める）
                try:
                    det_now = ydetail if yahoo else page.evaluate(DETAIL_JS)
                    desc_now, cond_now = det_now.get("description", ""), det_now.get("condition", "")
                except Exception:
                    desc_now, cond_now = "", ""
                why = no_body_reason(title_now + "\n" + desc_now) or junk_reason(title_now + "\n" + desc_now) \
                    or ("商品の状態が「全体的に状態が悪い」" if "状態が悪い" in cond_now else "")
                if why and not r["ebay_item_id"]:
                    acc, acc_why = True, why   # 候補の段階で外す（待機中の候補も）
                elif why and r["ebay_item_id"] not in sold_on_ebay:
                    desc_pending.append({"row": r["row"], "ebay_item_id": r["ebay_item_id"], "why": why})
            if r["ebay_item_id"] and r["ebay_item_id"] in sold_on_ebay:
                # ---- eBayで売れた：仕入れ元で買うように目立たせる（この行は自動では消さない） ----
                s = sold_on_ebay[r["ebay_item_id"]]
                usd = f"${s['usd']:.2f}" if s.get("usd") else ""
                src = source_name(r["mercari_url"])
                upd["status"] = PENDING
                if s.get("usd"):
                    upd["ebay_usd"] = s["usd"]   # 実際に売れた値段（売上表に移すときに使う）
                if status == "on_sale" and (info or {}).get("price"):
                    p_txt = ""
                    if s.get("usd"):
                        fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(r.get("query")) / 100 + duty_pct(settings) / 100
                        p = s["usd"] * usd_jpy(settings) * (1 - fee) - ship_for(settings, r.get("query")) - info["price"]
                        p_txt = f"・見込み利益 約{int(p):,}円"
                        upd["profit"] = round(p)
                    upd["cost"] = info["price"]
                    upd["result"] = (f"🔴【要購入】eBayで売れました（{usd}）。{src}でまだ買えます"
                                     f"（{int(info['price']):,}円{p_txt}）→ 商品URLから購入してください")
                else:
                    # 仕入れ元が売り切れ：代わりの商品は使わず、購入者にお詫びしてキャンセルする
                    upd["result"] = (f"⚠️【確認】【仕入れ先が売り切れ】eBayで売れました（{usd}）が、{src}はすでに{LABEL[status]}です。"
                                     "購入者にお詫びしてキャンセルしてください（キャンセルが済んだらこの行を削除してください）")
                k = model_key(r.get("query"))
                listed_per_model[k] = max(0, listed_per_model.get(k, 1) - 1)
                sold_n += 1
                name = (title_now or r.get("title") or r.get("query") or "")[:40]
                alert(f"【要仕入れ】eBayで売れました {usd}｜{name}",
                      f"{upd['result']}\n\n"
                      f"■ 仕入れ先（{src}）\n{r['mercari_url']}\n\n"
                      f"■ eBay出品番号：{r['ebay_item_id']}\n\n"
                      "購入したら、スプレッドシートの「仕入れた」にチェックを入れてください。")
            elif r["ebay_item_id"]:
                # ---- 出品中の商品：売り切れたらeBayを止める ----
                if acc:
                    token = token or get_access_token()
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    upd["result"] = f"{acc_why or '説明書・充電器などの単体'}のため出品を止めました → {msg}"
                    if ok:
                        upd["status"] = "stopped"
                        stopped += 1
                elif status in ("sold", "deleted", "auction"):
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    upd["result"] = f"{LABEL[status]} → {msg}"
                    if ok:
                        upd["status"] = "stopped"
                        stopped += 1
                        notify(f"【停止】{r.get('memo','')} メルカリ{LABEL[status]} → {msg}（eBay {r['ebay_item_id']}）")
                    else:
                        notify(f"【要対応】{r.get('memo','')} メルカリ{LABEL[status]}ですが{msg}（eBay {r['ebay_item_id']}）")
                elif status == "on_sale" and info.get("price"):
                    try:   # 同じ商品が別サイトにも出ていないか、1枚目の写真で確認する
                        imgs = [] if r["mercari_url"] in _imghash else \
                            (ydetail if yahoo else page.evaluate(DETAIL_JS)).get("images", [])
                        dup_note(r, info["price"], imgs, r["ebay_item_id"])
                    except Exception as e:
                        print("写真の比較に失敗:", str(e)[:80])
                    # 出品中の値段を見直す（2026/10/26 ウォッチの有無に関係なく、利益2,000円の値段にそろえる）
                    try:
                        q = (r.get("query") or "").strip()
                        target, floor = market_price_usd(info["price"], q, settings)
                        floor = item_floor(info["price"], q, settings, target, r.get("listed_at") or "2000-01-01")
                        act = active_items.get(r["ebay_item_id"], {})
                        if "watch" in act:
                            upd["watch"] = act["watch"]
                        if act.get("days") is not None:
                            upd["days_listed"] = int(act["days"])
                            v = views.get(r["ebay_item_id"])
                            upd["reason"] = unsold_reason(act["days"], v[0] if v else None, act.get("watch"),
                                                          act.get("price"), target, q)
                        hours_since_drop = None   # 前回の値下げからの時間（T列「最終値下げ」から読む）
                        try:
                            last = datetime.strptime(str(r.get("last_drop") or "")[:16], "%Y-%m-%d %H:%M").replace(tzinfo=JST)
                            hours_since_drop = (datetime.now(JST) - last).total_seconds() / 3600 + 0.1
                        except ValueError:
                            pass
                        new_price = decide_price(act.get("price"), target, floor, act.get("watch"),
                                                 act.get("days"), hours_since_drop)
                        slow = slow_drop_price(r.get("memo"), target, floor)
                        if slow:
                            new_price = slow
                        if new_price:
                            # 実際の出品価格で売れたときの利益をシートに書く
                            fx = usd_jpy(settings)
                            fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(r.get("query")) / 100 + duty_pct(settings) / 100
                            rev = new_price * fx
                            p = rev * (1 - fee) - ship_for(settings, r.get("query")) - info["price"]
                            upd["cost"] = info["price"]
                            upd["profit"] = round(p)
                            listed_profit[r["row"]] = (model_key(r.get("query")), p, r["ebay_item_id"])
                            upd["margin"] = round(p / rev * 100, 1) if rev else 0
                            cur = act.get("price")
                            same = (abs(cur - new_price) < 0.005) if cur is not None \
                                else f"${new_price:.2f}" in (r.get("result") or "")
                            if not same:   # 値段が変わるときだけeBayを更新（結果欄は「販売中」だけ。利益は想定利益の列に出る）
                                ack, errs = trading_call("ReviseInventoryStatus",
                                    f"<InventoryStatus><ItemID>{r['ebay_item_id']}</ItemID>"
                                    f"<StartPrice>{new_price:.2f}</StartPrice></InventoryStatus>", token)
                                if ack in ("Success", "Warning") and cur is not None and new_price < cur:
                                    upd["last_drop"] = now()
                            if act.get("watch"):
                                offer_targets[r["ebay_item_id"]] = (new_price, floor)
                    except Exception as e:
                        print(f"値段の見直しに失敗（eBay {r['ebay_item_id']}）:", repr(e)[:150])
            else:
                # ---- 仕入れ候補：商品名・価格・相場・利益を埋める ----
                if acc:
                    upd["result"] = f"{acc_why or '説明書・充電器などの単体'}のため候補から外しました"
                    upd["status"] = "stopped"
                    upd["list"] = False
                elif status in ("sold", "deleted", "auction"):
                    upd["result"] = f"{LABEL[status]}（候補から外しました）"
                    upd["status"] = "stopped"
                elif status == "on_sale" and info and listed_per_model.get(model_key(r.get("query")), 0) >= cap_for(r.get("query")) \
                        and not swap_out(model_key(r.get("query")), expected_profit(info.get("price"), r.get("query"), settings)):
                    upd["result"] = f"同じ機種がすでに{cap_for(r.get('query'))}件出品中のため候補から外しました"
                    upd["status"] = "stopped"
                elif status == "on_sale" and info and ebay_busy:
                    upd["result"] = "【待機】eBayが一時的に受け付けないため、次の回に出品します"
                elif status == "on_sale" and info and free_full:
                    upd["result"] = f"【待機】今月の無料出品枠（{FREE_LISTINGS}件）を使い切ったため、来月1日まで出品を待ちます"
                elif status == "on_sale" and info and day_full:
                    upd["result"] = f"【待機】今日の出品ペースの上限（{day_cap(free_used)}件）に達したため、明日以降に出品します"
                elif status == "on_sale" and info and listed >= list_cap(settings):
                    upd["result"] = f"【待機】1回の出品は{list_cap(settings)}件までのため、次の回（30分後）に出品します"
                elif status == "on_sale" and info and quota_hit \
                        and not swap_out(None, expected_profit(info.get("price"), r.get("query"), settings)):
                    upd["result"] = "【待機】eBayの月間出品枠の上限のため、枠が空くまで待ちます"
                elif status == "on_sale" and info:
                    # ---- 販売中の候補はすべてeBayに出品（「出品する」のチェックは不要） ----
                    try:
                        token = token or get_access_token()
                        detail = ydetail if yahoo else page.evaluate(DETAIL_JS)
                        if free_used is None:
                            try:
                                free_used = free_listings_used(token)
                            except Exception as e:   # 数えられないときは、手数料がかからないよう出品しない
                                print("無料出品枠を確認できませんでした:", str(e)[:100])
                                free_used = FREE_LISTINGS
                            print(f"今月の無料出品枠：{free_used}件使用（月末までの自動更新を含む）／{FREE_LISTINGS}件")
                            print(f"今日の出品：{FREE_TODAY['new']}件／今日の上限{day_cap(free_used)}件（残りの枠を月末までの{FREE_TODAY['days_left']}日で割った数）")
                        if free_used >= FREE_LISTINGS - FREE_MARGIN:
                            free_full = True
                            raise RuntimeError("FREE_FULL")
                        if FREE_TODAY["new"] >= day_cap(free_used):
                            day_full = True
                            raise RuntimeError("DAY_FULL")
                        other = dup_match(src_hash(r["mercari_url"], detail.get("images", [])), r["row"])
                        if other and src_score(r["mercari_url"], info.get("price")) >= other["score"]:
                            res, err = None, (f"SKIP:同じ商品を{source_name(other['url'])}（{int(other['price'] or 0):,}円）で"
                                              f"出品中のため候補から外しました（eBay {other['ebay_item_id']}）")
                        else:
                            res, err = create_listing(r, info, detail, settings, token)
                    except Exception as e:
                        res, err = None, f"出品中にエラー：{str(e)[:200]}"
                        if TEMP_EBAY_ERR.search(str(e)):
                            ebay_busy = True   # eBayが一時的に受け付けない → この回はもう出品しない（次の回にやり直す）
                    if free_full:
                        res, err = None, "WAIT"
                    elif day_full:
                        res, err = None, "DAYWAIT"
                    upd["list"] = False
                    if res:
                        free_used = (free_used or 0) + 1
                        FREE_TODAY["new"] += 1
                        dup_note(r, info.get("price"), detail.get("images", []), res["ebay_item_id"])
                        upd["ebay_item_id"] = res["ebay_item_id"]
                        upd["listed_at"] = now()
                        upd["result"] = f"eBayに出品しました（${res['price']:.2f}）→ 売り切れ監視を開始"
                        listed += 1
                        k = model_key(r.get("query"))
                        listed_per_model[k] = listed_per_model.get(k, 0) + 1
                        notify(f"【出品】{res['title_en']} ${res['price']:.2f}（eBay {res['ebay_item_id']}）")
                    elif err == "WAIT":
                        upd["result"] = f"【待機】今月の無料出品枠（{FREE_LISTINGS}件）を使い切ったため、来月1日まで出品を待ちます"
                    elif err and TEMP_EBAY_ERR.search(err):
                        upd["result"] = f"【待機】eBayが一時的に受け付けないため、次の回に出品します（{err[7:120]}）"
                    elif err == "LIMITWAIT":
                        upd["result"] = "【待機】eBayの相場の問い合わせ上限に達したため、次の回に出品します"
                    elif err == "DAYWAIT":
                        upd["result"] = f"【待機】今日の出品ペースの上限（{day_cap(free_used)}件）に達したため、明日以降に出品します"
                    elif err and err.startswith("SKIP:"):
                        upd["result"] = err[5:]
                        upd["status"] = "stopped"   # 相場では利益が出ない → 候補から外す（自動で行を削除）
                    elif err and re.search(r"limit|限度|上限|exceed", err, re.I):
                        quota_hit = True   # 出品枠の上限：利益が大きければ入れ替え、そうでなければ枠が空くまで待つ
                        upd["result"] = "【待機】eBayの月間出品枠の上限のため、枠が空くまで待ちます"
                        if swap_out(None, expected_profit(info.get("price"), r.get("query"), settings)):
                            try:
                                res2, err2 = create_listing(r, info, detail, settings, token)
                            except Exception as e:
                                res2, err2 = None, str(e)[:200]
                            if res2:
                                upd["ebay_item_id"] = res2["ebay_item_id"]
                                upd["listed_at"] = now()
                                upd["result"] = f"利益が大きいため入れ替えて出品しました（${res2['price']:.2f}）→ 売り切れ監視を開始"
                                listed += 1
                                k = model_key(r.get("query"))
                                listed_per_model[k] = listed_per_model.get(k, 0) + 1
                    else:
                        # 失敗理由が次の確認で上書きされないよう「paused」にして残す
                        upd["result"] = f"【出品できず】{err}"
                        upd["status"] = "paused"
                        notify(f"【出品できず】{info.get('title','')[:30]}：{err}")
                elif status == "on_sale" and info:
                    try:
                        upd.update(research_row(r, info, settings))
                        researched += 1
                        # 利益が少ない・相場の件数が少ないものは候補から外す
                        min_profit = float(settings.get("min_profit") or 2000)
                        low_profit = "profit" in upd and upd["profit"] <= min_profit
                        few = upd.get("ebay_count") not in (None, "") and int(upd["ebay_count"]) < MIN_COUNT
                        if low_profit or few:
                            upd["status"] = "stopped"
                            upd["result"] = "利益が少ないため候補から外しました" if low_profit else "相場の件数が少ないため候補から外しました"
                    except Exception as e:
                        upd["result"] = f"販売中（相場の取得に失敗：{str(e)[:60]}）"

            if status == "blocked":
                blocked += 1
            elif status == "unknown":
                notify(f"【確認して】商品ページの状態を判定できませんでした：{r['mercari_url']}")
                if r.get("ebay_item_id") and str(r.get("result") or "").startswith("判定できず"):
                    unknown_twice.append(r)
            if acc_note and upd.get("status") != "stopped":
                upd["result"] = f"{acc_note} / {upd.get('result') or ''}"
            updates.append(upd)
            if len(updates) >= 10:   # 途中で止まっても結果が残るよう、10件ごとにシートへ書く
                sheet("update", updates=updates)
                updates = []
            time.sleep(random.uniform(0.5, 1.5) if yahoo else random.uniform(1.5, 3))

        # ---- 出品中なのにメルカリの状態が2回続けて読めない商品があれば知らせる（3時間に1回まで） ----
        if unknown_twice:
            flag = os.path.join(".cache", "unknown_alert.txt")
            try:
                last = float(open(flag, encoding="utf-8").read().strip() or 0)
            except (OSError, ValueError):
                last = 0
            if time.time() - last > 3 * 3600:
                alert(f"【確認】メルカリの状態を読めない出品が{len(unknown_twice)}件あります",
                      "30分以上、メルカリで売り切れたかどうかを確認できていません。eBayで売れたら、"
                      "仕入れ先でまだ買えるか確認してから対応してください。\n\n"
                      + "\n".join(f"eBay {x['ebay_item_id']}：{x.get('title', '')[:30]} {x['mercari_url']}" for x in unknown_twice[:30]))
                os.makedirs(".cache", exist_ok=True)
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write(str(time.time()))

        # ---- 見張る件数が増えて30分に間に合わなくなりそうなら知らせる（見張りを2本に分ける目安。7日に1回まで） ----
        if len(targets) >= SPLIT_AT:
            flag = os.path.join(".cache", "split_alert.txt")
            try:
                last = float(open(flag, encoding="utf-8").read().strip() or 0)
            except (OSError, ValueError):
                last = 0
            if time.time() - last > 7 * 86400:
                alert(f"【お知らせ】在庫の見張りが{len(targets)}件になりました（2本に分ける目安）",
                      f"見張る件数が{SPLIT_AT}件を超えたので、このままだと30分ごとの見張りに間に合わなくなります。\n"
                      "Claudeに「在庫の見張りを2本に分けて」と頼んでください（費用はかかりません）。")
                os.makedirs(".cache", exist_ok=True)
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write(str(time.time()))

        # ---- ウォッチしている人に値引きオファーを送る（利益2,000円は必ず残す） ----
        try:
            send_watch_offers(offer_targets)
        except Exception as e:
            print("値引きオファーの送信に失敗:", str(e)[:150])

        # ---- 無料出品枠を使い切ったら、その月に1回だけ知らせる ----
        if free_full:
            flag = os.path.join(".cache", "freefull.txt")
            month = datetime.now(PT).strftime("%Y-%m")
            try:
                done = open(flag, encoding="utf-8").read().strip() == month
            except OSError:
                done = False
            if not done:
                alert(f"【お知らせ】今月の無料出品枠（{FREE_LISTINGS}件）を使い切りました",
                      f"手数料がかからないよう、来月1日まで新しい出品を止めています（使用：{free_used}件）。\n"
                      "ストアのプランを見直すときの参考にしてください。")
                os.makedirs(".cache", exist_ok=True)
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write(month)

        # ---- 説明文に問題があった出品を止める（一度に20件を超えたら判定ミスの可能性が高いので止めずに知らせる） ----
        if len(desc_pending) > DESC_STOP_MAX:
            alert(f"【確認】説明文の判定で{len(desc_pending)}件が引っかかりました（止めていません）",
                  "一度にこれほど多く引っかかるのは判定の不具合の可能性が高いため、出品は止めていません。\n\n"
                  + "\n".join(f"eBay {d['ebay_item_id']}：{d['why']}" for d in desc_pending[:40]))
        else:
            for d in desc_pending:
                ok, msg = stop_on_ebay(d["ebay_item_id"], token or get_access_token())
                updates.append({"row": d["row"], "status": "stopped" if ok else None,
                                "result": f"{d['why']}のため出品を止めました → {msg}"})
                if ok:
                    stopped += 1

        # ---- 同じ商品が別サイトにも出ていて、両方eBayに出品していたら条件の悪いほうを止める ----
        print(f"写真を確認した出品中の商品：{len(dup_seen) + len(dup_stop)}件／同じ商品：{len(dup_stop)}件")
        done_rows = set()
        for d in dup_stop:
            if d["row"] in done_rows or not d.get("ebay_item_id"):
                continue
            done_rows.add(d["row"])
            k = d["keep"]
            ok, msg = stop_on_ebay(d["ebay_item_id"], token or get_access_token())
            updates.append({"row": d["row"], "status": "stopped" if ok else None,
                            "result": f"同じ商品が{source_name(k['url'])}にも出ているため停止"
                                      f"（{source_name(k['url'])} {int(k['price'] or 0):,}円のほうを残しました） → {msg}"})
            if ok:
                stopped += 1
            print("同じ商品のため停止:", d["ebay_item_id"], "残した出品:", k["ebay_item_id"], msg)

        # ---- 「要仕入れ」の行：まだ仕入れていないうちに仕入れ先が売り切れたら知らせる ----
        for r in [x for x in rows if x["status"] == PENDING and x.get("list") is not True]:
            if "【確認】" in (r.get("result") or ""):
                continue   # 一度知らせたものは繰り返さない
            try:
                if is_yahoo(r["mercari_url"]):
                    st, _, _ = check_yahoo(r["mercari_url"])
                else:
                    st, _ = check_mercari(page, r["mercari_url"])
            except Exception:
                continue
            if st not in ("sold", "deleted"):
                continue
            msg = ("⚠️【確認】仕入れ先が売り切れになりました。自分で買った場合は「仕入れた」にチェックしてください。"
                   "買っていない場合は、購入者にお詫びしてキャンセルしてください（キャンセルが済んだらこの行を削除してください）")
            updates.append({"row": r["row"], "result": msg})
            alert(f"【確認】仕入れ先が売り切れました｜{(r.get('title') or r.get('query') or '')[:40]}",
                  f"{msg}\n\n■ 仕入れ先\n{r['mercari_url']}\n\n■ eBay出品番号：{r['ebay_item_id']}")
        browser.close()

    sheet("update", updates=updates)
    try:
        sheet("cleanup")   # 止めた行（売り切れ・候補から外したもの・eBay終了済み）を「履歴」シートに移して削除
    except Exception:
        pass
    if blocked:
        notify(f"【注意】メルカリにアクセスを拒否されました（{blocked}件）。クラウドからのアクセスが制限されている可能性があります。")
    # 記録には件数だけを出す（URLや出品番号は出さない）
    summary = "、".join(f"{LABEL[k]} {v}件" for k, v in counts.items())
    try:
        # 売上シートに、注文番号・税・手数料・受取額を書き込む（売れた商品を仕入れ済みにした後に反映）
        if token:
            sync_sales(token, settings)
    except Exception as e:
        print("売上シートの更新に失敗:", str(e)[:100])
    msg = f"{now()} 正常終了：{summary}／eBayで売れた {sold_n}件／eBay停止 {stopped}件／利益計算 {researched}件／出品 {listed}件"
    print(msg)
    sheet("log", message=msg)


def sync_sales(token, settings):
    """売上シートに、注文番号・税・手数料・受取額などを書き込む（売れた直後の即時実行にも使う）"""
    orders = ebay_orders(token)
    try:
        ad_fees = ebay_ad_fees()   # 広告経由で売れた注文の広告費（権限がまだなければ None）
    except Exception as e:
        ad_fees = None
        print("広告費の確認に失敗:", str(e)[:100])
    if ad_fees is not None:
        for o in orders:
            o["ad_fee"] = ad_fees.get(o["order_id"], 0)   # 広告経由でなければ 0
    try:   # 出品手数料など広告以外のeBayの費用を経費シートへ（同じ明細は二重に入らない）
        sheet("expenses_sync", items=NON_SALE_OTHER, fx=round(usd_jpy(settings), 2))
    except Exception as e:
        print("経費の記録に失敗:", str(e)[:100])
    if orders:
        try:   # 発送済みの注文は、配達された日も調べる（届いたらお礼を送るため）
            dd = delivered_dates([o["order_id"] for o in orders if o.get("shipped")], token)
            for o in orders:
                if dd.get(o["order_id"]):
                    o["delivered"] = dd[o["order_id"]]
            if dd:
                print("配達済みの注文:", dd)
        except Exception as e:
            print("配達日の確認でエラー:", str(e)[:100])
        res = sheet("sales_sync", orders=orders, fx=round(usd_jpy(settings), 2),
                    ad_models=[model_key(k) for k in GAMES], ad_rate=AD_RATE)   # 広告を付ける機種（売上の広告費の計算に使う）
        # 仕入れ済みになって初めて売上シートに入った注文 → 購入者にお礼と発送予定日を送る
        for o in orders:
            if o["item_id"] in (res.get("new_ids") or []) and o.get("buyer"):
                send_thanks(o, token)
        # 届いた注文（売上シートの「お礼」が空のもの）→ お礼だけの短いメッセージを送り、送った日を記録
        done = []
        for o in orders:
            if o["item_id"] in (res.get("thank_ids") or []) and o.get("buyer"):
                at = DELIVERED_AT.get(o["order_id"])
                if at and datetime.now(JST) - at < timedelta(hours=THANKS_DELAY_H):
                    continue   # 届いてから24時間たっていない → 次の回以降に送る
                if send_delivered_thanks(o, token):
                    done.append(o["item_id"])
        if done:
            sheet("thanks_done", ids=done)
        try:   # 届いて1週間たっても評価がない買い手に、1回だけ評価のお願い（評価と送信済みを必ず確認してから）
            thanked = [o["item_id"] for o in orders
                       if DELIVERED_AT.get(o["order_id"]) and o["item_id"] not in (res.get("thank_ids") or [])]
            ask_feedback(orders, token, thanked)
        except Exception as e:
            print("評価のお願いでエラー（送っていません）:", str(e)[:200])


def research_row(r, info, settings):
    out = {"title": info.get("title") or r.get("title", "")}
    cost = info.get("price")
    if cost:
        out["cost"] = cost
    query = (r.get("query") or "").strip() or guess_query(out["title"])
    out["query"] = query
    sale = r.get("ebay_usd")
    if not sale and query:
        sale, n = ebay_market_usd(query)
        out["ebay_usd"] = sale or ""
        out["ebay_count"] = n
    if not query:
        out["result"] = "販売中（型番が見つからず相場不明。検索語の列に型番を入れてください）"
    elif not sale:
        out["result"] = "販売中（eBayに同じ型番の出品が見つかりません）"
    elif cost:
        p, rate = profit(cost, float(sale), settings, query)
        out["profit"] = p
        out["margin"] = rate
        out["result"] = "販売中"
    return out


# ---------- eBayへの出品 ----------
BRANDS = [(r"^(KYF|KYV|KY-|KY)", "Kyocera"), (r"^(SHF|SHV|SH-|SH)", "Sharp"), (r"SH$", "Sharp"), (r"KC$", "Kyocera"),
          (r"^P-?\d", "Panasonic"), (r"^N-?\d", "NEC"), (r"^F-?\d", "Fujitsu"), (r"^SO-?\d", "Sony Ericsson"),
          (r"^CA-?\d", "Casio"), (r"^SA-?\d", "Sanyo"), (r"^D-?\d", "Mitsubishi"), (r"^L-?\d", "LG")]
COLORS = {"ブルーブラック": "Blue Black", "ワインレッド": "Red", "ボルドー": "Red", "ブラウン": "Brown", "茶": "Brown",
          "ライトブルー": "Blue", "水色": "Blue", "ローズ": "Pink", "ベージュ": "Beige", "シャンパン": "Gold",
          "black": "Black", "white": "White", "blue": "Blue", "red": "Red", "pink": "Pink", "silver": "Silver",
          "ブラック": "Black", "黒": "Black", "ホワイト": "White", "白": "White", "ブルー": "Blue", "青": "Blue",
          "ネイビー": "Navy", "レッド": "Red", "赤": "Red", "ピンク": "Pink", "シルバー": "Silver", "ゴールド": "Gold",
          "グリーン": "Green", "緑": "Green", "パープル": "Purple", "オレンジ": "Orange", "イエロー": "Yellow", "グレー": "Gray"}


def brand_of(model, title):
    m = model.upper()
    for pat, name in BRANDS:
        if re.search(pat, m):
            return name
    for name in ["Kyocera", "Sharp", "Panasonic", "NEC", "Fujitsu", "Sony", "Casio"]:
        if name.lower() in title.lower():
            return name
    return "Unbranded"


def carrier_of(model, text):
    t = text.lower()
    if "docomo" in t or "ドコモ" in text:
        return "Docomo"
    if re.search(r"(^|[^a-z])au([^a-z]|$)", t):
        return "au"
    if "softbank" in t or "ソフトバンク" in text:
        return "SoftBank"
    m = model.upper()
    if re.match(r"^[A-Z]{1,2}-\d{2}[A-Z]$", m):
        return "Docomo"
    if re.match(r"^(KYF|SHF|KYV|SHV)", m):
        return "au"
    if re.match(r"^(A\d{3}|\d{3})[A-Z]{2}$", m):
        return "SoftBank"
    return ""


# 型番ごとの正式なシリーズ名（英語タイトルを統一するため）
SERIES = {
    "902KC": "Kyocera DIGNO Keitai 3", "701KC": "Kyocera DIGNO Keitai 2", "A202KC": "Kyocera DIGNO Keitai 4",
    "KY-42C": "Kyocera DIGNO Keitai", "KYF31": "Kyocera GRATINA 4G", "KYF37": "Kyocera GRATINA 4G",
    "KYF39": "Kyocera GRATINA", "KYF42": "Kyocera GRATINA", "KYF38": "Kyocera TORQUE X01",
    "601SH": "Sharp AQUOS Keitai 2", "805SH": "Sharp AQUOS Keitai 3", "A205SH": "Sharp AQUOS Keitai 4",
    "A206SH": "Sharp AQUOS Keitai 4", "SH-01J": "Sharp AQUOS Keitai", "SH-02L": "Sharp AQUOS Keitai",
    "SH-02K": "Sharp AQUOS Keitai", "F-01G": "Fujitsu Raku-Raku Phone", "F-03L": "Fujitsu Raku-Raku Phone",
    "P-01G": "Panasonic", "P-01H": "Panasonic", "P-01J": "Panasonic",
    "903KC": "Kyocera DIGNO Keitai", "A203KC": "Kyocera DIGNO Keitai", "602SH": "Sharp AQUOS Keitai",
}


# 本体の内蔵メモリ（4Gガラホはほぼ8GB）
STORAGE = {"F-01G": "Less than 1 GB", "F-08C": "Less than 1 GB", "P-01G": "Less than 1 GB"}


def storage_of(model):
    m = model.upper()
    if m in STORAGE:
        return STORAGE[m]
    if re.match(r"^(\d{3}|A\d{3})(KC|SH)$|^KY-42C$|^KYF(3[1-9]|4\d)$|^SH-0[12][JKL]$|^SH-32C$|^SHF3\d$", m):
        return "8 GB"
    return "Less than 1 GB"


def build_title(model, title, carrier, unlocked, color):
    """どの商品も同じ形：<メーカー シリーズ> <型番> 4G Flip Phone <SIMフリー/キャリア> <色> Japan"""
    m = model.upper()
    series = SERIES.get(m) or brand_of(model, title)
    net = "SIM Unlocked" if unlocked else (carrier or "")
    gen = "4G " if re.match(r"^(\d{3}|A\d{3})(KC|SH)$|^KY-42C$|^KYF(3[1-9]|4\d)$|^SH-0[12][JKL]$", m) and "4G" not in series else ""
    parts = [series, model, f"{gen}Flip Phone", net, color, "Japan"]
    t = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    if len(t) > 80:   # 長すぎたら色→Japanの順に削る
        for drop in (color, "Japan"):
            if drop and len(t) > 80:
                t = re.sub(r"\s+", " ", t.replace(f" {drop}", "", 1)).strip()
    return t[:80]


PRICE_FLOOR_PROFIT = 2000   # 出品価格はこの利益（円）が残る値段より下げない
# 2026/10/28 利益1,000〜2,000円の商品も出品する（最低利益は設定シートの「最低の利益額」）。
# この時刻より前に出品したものは今まで通り利益2,000円の値段のまま。これ以降に出品したものは、
# 相場が利益2,000円の値段より安ければ相場の値段（ただし最低利益が残る値段より下げない）で出す。
LOW_TIER_START = "2026-10-28 19:00"


def item_floor(cost, query, settings, target, listed_at=""):
    """その出品の下限価格（出品・値下げで使う）"""
    floor2000 = floor_price_usd(cost, query, settings)
    s = str(listed_at or "").strip()
    if s:
        try:
            if "T" in s:   # シートから日時の形で来た場合（UTC）は日本時間に直す
                s = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(JST).strftime("%Y-%m-%d %H:%M")
            s = s.replace("/", "-")[:16]
        except ValueError:
            s = "2000-01-01"
        if s < LOW_TIER_START:
            return floor2000   # 以前からの出品：今まで通り
    floor_min = floor_price_usd(cost, query, settings, min_profit_for(cost, settings, query))
    if target is None:
        return floor2000
    return max(min(round(target, 2), floor2000), floor_min)
NEW_LISTING_DAYS = 3        # 出品から3日間は相場の値段で様子を見る
WATCH_STEP_USD = 2.0        # ウォッチされている商品は $2（約300円）ずつ値下げ（2026/10/22 $0.5→$2）
WATCH_INTERVAL_H = 3        # 値下げの間隔（時間）（2026/10/22 1時間→3時間）


def floor_price_usd(cost, query, settings, profit=None):
    """利益がちょうど profit 円（省略時は2,000円）残る値段。2026/10/26 出品・値下げはすべてこの値段にそろえる"""
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(query) / 100 + duty_pct(settings) / 100
    ship = ship_for(settings, query)
    min_profit = PRICE_FLOOR_PROFIT if profit is None else profit
    return math.ceil((cost + ship + min_profit) / max(0.05, 1 - fee) / fx) - 0.01


SLOW_DROP_STEP_USD = 2.0     # 「ゆっくり値下げ」の出品：30分ごと（自動実行のたび）に $2（約300円）ずつ下げる
SLOW_DROP_MIN = 30


def slow_drop_price(memo, target, floor):
    """メモ欄に「ゆっくり値下げ YYYY-MM-DD HH:MM」と書いた出品だけ、その時刻に相場の値段へ戻し、
    そこから30分ごとに$2ずつ下げる（利益2,000円の値段まで）。書いていなければ None"""
    m = re.search(r"ゆっくり値下げ\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", str(memo or ""))
    if not m or not target or not floor:
        return None
    start = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    steps = max(0, int((datetime.now(JST) - start).total_seconds() // (SLOW_DROP_MIN * 60)))
    return max(round(target - SLOW_DROP_STEP_USD * steps, 2), floor)


def decide_price(cur, target, floor, watch, days, hours_since_drop):
    """出品中の値段を決める。
    ・ウォッチあり：3時間ごとに$2ずつ下げる（下限まで）。別に値引きオファーも送る
    ・ウォッチなし：すぐに下限の値段（利益2,000円）まで下げる"""
    return floor   # 2026/10/26 ウォッチの有無に関係なく、利益2,000円の値段にそろえる
    if cur is None:
        return max(target or floor, floor)
    if watch:   # ウォッチが付いたら、出品からの日数に関係なく少しずつ下げる
        if hours_since_drop is not None and hours_since_drop < WATCH_INTERVAL_H:
            return max(cur, floor)
        return max(round(cur - WATCH_STEP_USD, 2), floor)
    return floor


def market_price_usd(cost, query, settings):
    """売れている相場に合わせた値段。利益が最低額（設定の最低利益）を下回るなら None"""
    sale, n = ebay_market_usd(query) if query else (None, 0)
    if not sale:
        return None, None
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    floor = floor_price_usd(cost, query, settings)                                   # 最低利益が残る最低価格
    target = math.floor(adj(sale, settings)) + 0.99                                  # 実際に売れている値段の目安
    return target, floor


def expected_profit(cost, query, settings):
    """候補を今出品したときに見込める利益（円）。相場が取れなければ None"""
    try:
        target, floor = market_price_usd(float(cost), (query or "").strip(), settings)
        if target is None or target < floor_price_usd(float(cost), (query or "").strip(), settings,
                                                      min_profit_for(cost, settings, query)):
            return None
        target = min(target, floor)   # 出品する値段（利益2,000円の値段が上限）
        fx = usd_jpy(settings)
        fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(query) / 100 + duty_pct(settings) / 100
        return target * fx * (1 - fee) - ship_for(settings, query) - float(cost)
    except (TypeError, ValueError):
        return None


def list_price_usd(cost, settings):
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2) + duty_pct(settings)) / 100
    margin = float(settings.get("target_margin") or 20) / 100
    ship = float(settings.get("ship_jpy") or 3000)
    revenue = (cost + ship) / max(0.05, 1 - fee - margin)
    return math.ceil(revenue / fx) - 0.01


MIN_PIC_SIDE = 500   # eBayの写真ルール：長い辺が500ピクセル以上


def small_picture_bytes(u):
    """写真が小さすぎる（長い辺が500ピクセル未満）ときだけ、800ピクセルに拡大した画像を返す。十分大きければNone"""
    try:
        from PIL import Image
        import io
        r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        w, h = im.size
        if max(w, h) >= MIN_PIC_SIDE:
            return None
        scale = 800 / max(w, h)
        im = im.convert("RGB").resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception as e:
        print("写真の大きさを確認できませんでした:", str(e)[:100])
        return None


# 2026/10/28 eBayが写真の取り込み（Trading API の UploadSiteHostedPictures）を終了し、404 を返すようになったため
# Media API（createImageFromUrl / createImageFromFile）に切り替えた。トークンは今までと同じ（sell.inventory の権限で使える）
MEDIA_URL = "https://apim.ebay.com/commerce/media/v1_beta/image"


def _media_image_url(r, token):
    """Media API の返事から eBay の写真URLを取り出す（本文になければ Location の写真情報を取りに行く）"""
    if r.status_code not in (200, 201):
        body = re.sub(r"\s+", " ", (r.text or "")[:200]).strip()
        raise RuntimeError(f"写真の取り込みに失敗（Media API HTTP {r.status_code}：{body or '中身なし'}）")
    try:
        url = (r.json() or {}).get("imageUrl", "")
    except ValueError:
        url = ""
    if not url and r.headers.get("Location"):
        g = requests.get(r.headers["Location"], headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if g.status_code == 200:
            url = (g.json() or {}).get("imageUrl", "")
    return url


def upload_picture_bytes(data, token):
    """画像データをそのままeBayに送る（小さい写真を拡大したときに使う）"""
    r = requests.post(f"{MEDIA_URL}/create_image_from_file",
                      headers={"Authorization": f"Bearer {token}"},
                      files={"image": ("image.jpg", data, "image/jpeg")}, timeout=60)
    return _media_image_url(r, token)


def upload_picture_url(u, token):
    """写真のURLを渡して、eBayに取り込んでもらう"""
    r = requests.post(f"{MEDIA_URL}/create_image_from_url",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"imageUrl": u}, timeout=60)
    return _media_image_url(r, token)


def upload_pictures(urls, token):
    out, last_err = [], ""
    for u in urls:
        try:
            small = small_picture_bytes(u)
            full = upload_picture_bytes(small, token) if small else upload_picture_url(u, token)   # 小さい写真は拡大してから送る
        except Exception as e:
            last_err = str(e)[:200]
            print("写真の取り込みに失敗:", last_err)
            full = ""
        if full:
            out.append(full)
    if not out and last_err:
        raise RuntimeError(last_err)   # 1枚も取り込めなかったときは理由を結果欄に出す
    return out


def ebay_active_items(token):
    """出品中の商品 {出品番号: {"watch": ウォッチ数, "price": 今の値段, "days": 出品からの日数}}"""
    out = {}
    nowu = datetime.now(ZoneInfo("UTC"))
    for page_no in range(1, 6):
        ack, errs, root = trading_call_root(
            "GetMyeBaySelling",
            "<ActiveList><Include>true</Include>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page_no}</PageNumber></Pagination></ActiveList>",
            token)
        if ack not in ("Success", "Warning"):
            raise RuntimeError("出品中の商品の取得に失敗：" + " / ".join(errs)[:200])
        for it in root.iter("{" + NS["e"] + "}Item"):
            item_id = it.findtext("e:ItemID", default="", namespaces=NS)
            if not item_id:
                continue
            price = it.findtext("e:SellingStatus/e:CurrentPrice", default="", namespaces=NS) or \
                it.findtext("e:BuyItNowPrice", default="", namespaces=NS)
            start = it.findtext("e:ListingDetails/e:StartTime", default="", namespaces=NS)
            days = None
            if start:
                try:
                    days = (nowu - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() / 86400
                except ValueError:
                    days = None
            out[item_id] = {"watch": int(it.findtext("e:WatchCount", default="0", namespaces=NS) or 0),
                            "price": float(price) if price else None, "days": days}
        pages = root.findtext("e:ActiveList/e:PaginationResult/e:TotalNumberOfPages", default="1", namespaces=NS)
        if page_no >= int(pages or 1):
            break
    return out


def ebay_orders(token, days=60):
    """最近の注文（注文番号・合計・税・手数料・受取額）を出品番号ごとに返す"""
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.get("https://api.ebay.com/sell/fulfillment/v1/order",
                     params={"filter": f"creationdate:[{since}..]", "limit": 200},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    out = []
    for o in r.json().get("orders", []):
        total = float(((o.get("pricingSummary") or {}).get("total") or {}).get("value") or 0)
        fee = float((o.get("totalMarketplaceFee") or {}).get("value") or 0)
        shipped = order_shipped_date(o, token)
        for li in o.get("lineItems", []):
            tax = sum(float((t.get("amount") or {}).get("value") or 0) for t in li.get("ebayCollectAndRemitTaxes", []))
            out.append({"item_id": str(li.get("legacyItemId") or ""), "order_id": o.get("orderId", ""),
                        "buyer": (o.get("buyer") or {}).get("username", ""),
                        "ship_by": (li.get("lineItemFulfillmentInstructions") or {}).get("shipByDate", ""),
                        "date": (o.get("creationDate") or "")[:10], "total": round(total, 2), "tax": round(tax, 2),
                        "fee": round(fee, 2), "payout": round(total - fee, 2),   # 税はeBayが別に預かるので、受取額は合計−手数料
                        "shipped": shipped, "kind": item_kind(li.get("title", "")),
                        "country": ((((o.get("fulfillmentStartInstructions") or [{}])[0].get("shippingStep") or {})
                                     .get("shipTo") or {}).get("contactAddress") or {}).get("countryCode", ""),   # 届け先の国（関税の見込みに使う）
                        "ship_to": ship_to_lines(o)})
    return out


def ship_to_lines(o):
    """届け先（名前・番地・市 州 郵便番号）を配送ラベルと同じ並び・大文字で。売上シートのメモに書いて、箱とラベルを照らし合わせる"""
    st = (((o.get("fulfillmentStartInstructions") or [{}])[0].get("shippingStep") or {}).get("shipTo") or {})
    a = st.get("contactAddress") or {}
    zip_ = str(a.get("postalCode") or "")
    if a.get("countryCode") == "US":
        zip_ = zip_[:5]   # ラベルは 5桁（11222-6013 → 11222）
    street = ", ".join(x for x in (a.get("addressLine1"), a.get("addressLine2")) if x)
    city = " ".join(x for x in (a.get("city"), a.get("stateOrProvince"), zip_) if x)
    return "\n".join(x.upper() for x in (st.get("fullName"), street, city) if x)


def item_kind(title):
    """eBayの商品名から種類（売上シートのメモの1行目に書く）"""
    t = (title or "").lower()
    if re.search(r"flip phone|keitai|feature phone|cell ?phone|mobile phone", t):
        return "ガラケー"
    if re.search(r"camcorder|handycam", t):
        return "ビデオカメラ"
    if re.search(r"cassette player|cd player|discman", t):
        return "カセット・CDプレーヤー"
    if re.search(r"\bwatch\b|g-shock|seiko", t):
        return "時計"
    if re.search(r"camera|cyber-?shot|coolpix|ixy|lumix|exilim|finepix|ricoh", t):
        return "デジカメ"
    if re.search(r"walkman|minidisc|mini disc|\bmd\b", t):
        return "MDプレーヤー"
    if re.search(r"game ?boy|nintendo|3ds|\bds\b|psp|vita|playstation|console", t):
        return "ゲーム機"
    return ""


THANKS_MSG = ("Hi! Thank you so much for your purchase. Your item is being prepared with care, "
              "and we will ship it by {date} via FedEx. You'll receive tracking information as soon as it ships. "
              "If you have any questions, feel free to message us anytime!")


def send_thanks(o, token):
    """仕入れが済んだ注文の購入者に、お礼と発送予定日のメッセージを送る"""
    date = ""
    if o.get("ship_by"):
        d = datetime.fromisoformat(o["ship_by"].replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Tokyo"))
        date = d.strftime("%B ") + str(d.day)
    body = THANKS_MSG.format(date=date or "the handling time shown in the listing")
    ack, errs, root = trading_call_root(
        "AddMemberMessageAAQToPartner",
        f"<ItemID>{o['item_id']}</ItemID><MemberMessage><Subject>Thank you for your purchase!</Subject>"
        f"<Body>{escape(body)}</Body><QuestionType>General</QuestionType>"
        f"<RecipientID>{escape(o['buyer'])}</RecipientID></MemberMessage>", token)
    ok = ack in ("Success", "Warning")
    print("購入者へのお礼メッセージ:", o["item_id"], "送信" if ok else "失敗 " + " / ".join(errs)[:200])
    return ok


DELIVERED_MSG = ("Hi! It looks like your item has been delivered. Thank you so much for your purchase — "
                 "we hope you enjoy it!")


def delivered_dates(order_ids, token):
    """配達された日（日本時間 YYYY-MM-DD）を注文番号ごとに返す。配送業者の配達完了がeBayに入った注文だけ"""
    out = {}
    ids = [i for i in order_ids if i]
    for k in range(0, len(ids), 20):
        body = "<OrderIDArray>" + "".join(f"<OrderID>{escape(i)}</OrderID>" for i in ids[k:k + 20]) + \
               "</OrderIDArray><DetailLevel>ReturnAll</DetailLevel>"
        try:
            ack, errs, root = trading_call_root("GetOrders", body, token)
        except Exception as e:
            print("配達日の確認に失敗:", str(e)[:100])
            continue
        for o in root.iter("{urn:ebay:apis:eBLBaseComponents}Order"):
            oid = o.findtext("e:OrderID", default="", namespaces=NS)
            times = [el.text for el in o.iter() if el.tag.endswith("ActualDeliveryTime") and el.text]
            if oid and times:
                d = datetime.fromisoformat(max(times).replace("Z", "+00:00")).astimezone(JST)
                out[oid] = d.strftime("%Y-%m-%d")
                DELIVERED_AT[oid] = d
    return out


DELIVERED_AT = {}          # 注文番号 → 配達された日時（日本時間）
THANKS_DELAY_H = 24        # 2026/10/28 届いてすぐではなく、相手が開けて確かめたころ（24時間後）にお礼を送る


def send_delivered_thanks(o, token):
    """届いた注文の購入者に、お礼だけの短いメッセージを送る"""
    ack, errs, root = trading_call_root(
        "AddMemberMessageAAQToPartner",
        f"<ItemID>{o['item_id']}</ItemID><MemberMessage><Subject>Thank you!</Subject>"
        f"<Body>{escape(DELIVERED_MSG)}</Body><QuestionType>General</QuestionType>"
        f"<RecipientID>{escape(o['buyer'])}</RecipientID></MemberMessage>", token)
    ok = ack in ("Success", "Warning")
    print("届いたお礼のメッセージ:", o["item_id"], "送信" if ok else "失敗 " + " / ".join(errs)[:200])
    return ok


# ---------- 2026/10/31 届いて1週間たっても評価がない買い手に、1回だけ評価のお願いを送る ----------
FEEDBACK_ASK_ENABLED = False   # 試し実行（feedbackcheck）で確かめてから True にする
FEEDBACK_ASK_DAYS = 7          # 届いてから何日たったら送るか
FEEDBACK_ASK_MAX_DAYS = 30     # これより前に届いたものには送らない
FEEDBACK_ASK_SUBJECT = "A small favor"
FEEDBACK_ASK_MSG = ("Hi, I hope you are enjoying your item. "
                    "If you have a moment, I would be very grateful if you could leave feedback. "
                    "As a new seller, it helps me a lot. Thank you again for your purchase!")


def feedback_received_items(token):
    """評価をもらった出品番号（売り手として受け取った評価すべて）。読めなければエラー（＝送らない）"""
    items, page = set(), 1
    while page <= 10:
        ack, errs, root = trading_call_root(
            "GetFeedback",
            "<FeedbackType>FeedbackReceivedAsSeller</FeedbackType><DetailLevel>ReturnAll</DetailLevel>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>", token)
        if ack not in ("Success", "Warning"):
            raise RuntimeError("評価の確認に失敗：" + " / ".join(errs)[:200])
        for fd in root.iter("{urn:ebay:apis:eBLBaseComponents}FeedbackDetail"):
            iid = fd.findtext("e:ItemID", default="", namespaces=NS)
            if iid:
                items.add(iid)
        total = int(root.findtext(".//e:PaginationResult/e:TotalNumberOfPages", default="1", namespaces=NS) or 1)
        if page >= total:
            break
        page += 1
    return items


def sent_message_subjects(token, days=60):
    """送信済みフォルダのメッセージ：出品番号 → 件名の一覧。読めなければエラー（＝送らない）"""
    out = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    ack, errs, root = trading_call_root(
        "GetMyMessages",
        "<FolderID>1</FolderID><DetailLevel>ReturnHeaders</DetailLevel>"
        f"<StartTime>{start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}</StartTime>"
        f"<EndTime>{end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}</EndTime>", token)
    if ack not in ("Success", "Warning"):
        raise RuntimeError("送信済みメッセージの確認に失敗：" + " / ".join(errs)[:200])
    for m in root.iter("{urn:ebay:apis:eBLBaseComponents}Message"):
        iid = m.findtext("e:ItemID", default="", namespaces=NS)
        subj = m.findtext("e:Subject", default="", namespaces=NS)
        if iid:
            out.setdefault(iid, []).append(subj)
    return out


def ask_feedback(orders, token, thanked_ids, dry_run=False):
    """届いて7日たった注文のうち、評価がまだなく、お願いもまだ送っていない買い手にだけ1回送る。
    評価・送信済みが確認できないときは送らない（必ず確認してから送る）"""
    now_ = datetime.now(JST)
    due = []
    for o in orders:
        at = DELIVERED_AT.get(o.get("order_id"))
        if not at or not o.get("buyer"):
            continue
        age = now_ - at
        if timedelta(days=FEEDBACK_ASK_DAYS) <= age <= timedelta(days=FEEDBACK_ASK_MAX_DAYS):
            due.append(o)
    if not due and not dry_run:
        return
    got = feedback_received_items(token)
    sent = sent_message_subjects(token)
    # 送信済みフォルダが読めているかの確認：お礼を送った注文のお礼メッセージが見えなければ、二重送信の恐れがあるので送らない
    check = [i for i in thanked_ids if i]
    seen = [i for i in check if any("Thank you" in s for s in sent.get(i, []))]
    folder_ok = bool(check) and len(seen) == len(check)
    print(f"評価のお願い：対象{len(due)}件／評価あり{len(got)}件／お礼が送信済みフォルダで見えた {len(seen)}/{len(check)}件")
    if dry_run:
        print("評価をもらった出品:", sorted(got))
        print("送信済みフォルダ:", {k: v for k, v in sent.items()})
    if not folder_ok:
        print("送信済みフォルダでお礼メッセージが確認できないため、評価のお願いは送りません")
        return
    for o in due:
        iid = o["item_id"]
        if iid in got:
            print("評価のお願い：評価あり → 送らない", iid)
            continue
        if any(FEEDBACK_ASK_SUBJECT in s for s in sent.get(iid, [])):
            continue   # もう送った
        if dry_run or not FEEDBACK_ASK_ENABLED:
            print("評価のお願い：送る予定（試し実行のため送っていません）", iid, o["buyer"])
            continue
        ack, errs, root = trading_call_root(
            "AddMemberMessageAAQToPartner",
            f"<ItemID>{iid}</ItemID><MemberMessage><Subject>{FEEDBACK_ASK_SUBJECT}</Subject>"
            f"<Body>{escape(FEEDBACK_ASK_MSG)}</Body><QuestionType>General</QuestionType>"
            f"<RecipientID>{escape(o['buyer'])}</RecipientID></MemberMessage>", token)
        ok = ack in ("Success", "Warning")
        print("評価のお願い:", iid, "送信" if ok else "失敗 " + " / ".join(errs)[:200])
        if ok:
            sent.setdefault(iid, []).append(FEEDBACK_ASK_SUBJECT)


def feedbackcheck():
    """評価のお願いの試し実行（送らずに、評価・送信済みフォルダの読み取りと送る予定だけ表示）"""
    token = get_access_token()
    orders = ebay_orders(token)
    dd = delivered_dates([o["order_id"] for o in orders if o.get("shipped")], token)
    print("配達済み:", dd)
    thanked = [o["item_id"] for o in orders if dd.get(o["order_id"])]
    ask_feedback(orders, token, thanked, dry_run=True)


def thanks(item_ids):
    """指定した出品番号の注文の購入者に、お礼メッセージを手動で送る（1回だけの作業）"""
    token = get_access_token()
    for o in ebay_orders(token):
        if o["item_id"] in item_ids:
            send_thanks(o, token)


def order_shipped_date(o, token):
    """発送日（CPaSSが追跡番号をeBayに入れた日・日本時間）。未発送なら空"""
    if not o.get("fulfillmentHrefs"):
        return ""
    try:
        r = requests.get(f"https://api.ebay.com/sell/fulfillment/v1/order/{o['orderId']}/shipping_fulfillment",
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        ds = [x["shippedDate"] for x in r.json().get("fulfillments", []) if x.get("shippedDate")]
        if not ds:
            return ""
        d = datetime.fromisoformat(min(ds).replace("Z", "+00:00"))
        return d.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    except Exception as e:
        print("発送日の取得に失敗:", str(e)[:100])
        return ""


def ebay_sold_items(token, days=30):
    """最近eBayで売れた出品 {出品番号: {"usd": 売れた値段, "buyer": 購入者ID, "date": 日時}}"""
    out = {}
    for page_no in range(1, 6):
        ack, errs, root = trading_call_root(
            "GetMyeBaySelling",
            f"<SoldList><Include>true</Include><DurationInDays>{days}</DurationInDays>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page_no}</PageNumber></Pagination></SoldList>",
            token)
        if ack not in ("Success", "Warning"):
            raise RuntimeError("売れた商品の取得に失敗：" + " / ".join(errs)[:200])
        for t in root.iter("{" + NS["e"] + "}Transaction"):
            item_id = t.findtext("e:Item/e:ItemID", default="", namespaces=NS)
            if not item_id:
                continue
            usd = (t.findtext("e:TotalTransactionPrice", default="", namespaces=NS)
                   or t.findtext("e:TransactionPrice", default="", namespaces=NS))
            out[item_id] = {"usd": float(usd) if usd else None,
                            "buyer": t.findtext("e:Buyer/e:UserID", default="", namespaces=NS),
                            "date": t.findtext("e:CreatedDate", default="", namespaces=NS)}
        pages = root.findtext("e:SoldList/e:PaginationResult/e:TotalNumberOfPages", default="1", namespaces=NS)
        if page_no >= int(pages or 1):
            break
    return out


def ebay_cancelled_items(token, days=30):
    """キャンセルが済んだeBayの注文の出品番号"""
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = requests.get("https://api.ebay.com/sell/fulfillment/v1/order",
                     params={"filter": f"creationdate:[{since}..]", "limit": 200},
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return {str(li.get("legacyItemId") or "") for o in r.json().get("orders", [])
            if (o.get("cancelStatus") or {}).get("cancelState") == "CANCELED"
            for li in o.get("lineItems", [])} - {""}


# eBayが一時的に受け付けないときのエラー（回数の上限・障害）。「出品できず」で止めずに次の回にやり直す
TEMP_EBAY_ERR = re.compile(r"応答が読めません|HTTP (429|5\d\d)|Media API HTTP (429|5\d\d)|call limit|usage limit|exceeded|Too Many|temporarily|try again", re.I)


def ebay_xml(r, call_name=""):
    """eBay(Trading API)の返事をXMLとして読む。読めないとき（回数の上限・一時的な障害など）は、
    返ってきた番号と中身の先頭を書いたエラーにする（2026/10/28：以前は「syntax error」としか分からなかった）"""
    try:
        return ET.fromstring(r.content)
    except ET.ParseError:
        body = re.sub(r"\s+", " ", (r.text or "")[:200]).strip()
        msg = f"eBayの応答が読めません（{call_name} HTTP {r.status_code}：{body or '中身なし'}）"
        print(msg)
        raise RuntimeError(msg)


def trading_call_root(call_name, body_xml, token):
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           f'<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">{body_xml}</{call_name}Request>')
    r = requests.post(TRADING_URL, headers={
        "X-EBAY-API-CALL-NAME": call_name, "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1209", "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": "text/xml"}, data=xml.encode("utf-8"), timeout=60)
    root = ebay_xml(r, call_name)
    ack = root.findtext("e:Ack", default="", namespaces=NS)
    errors = [e.findtext("e:LongMessage", default="", namespaces=NS)
              for e in root.findall("e:Errors", NS) if e.findtext("e:SeverityCode", namespaces=NS) == "Error"]
    return ack, errors, root


# ---------- 無料の出品枠（ベーシックストア：月1,000件）を超えないようにする ----------
MAX_WAIT = 18 * 60          # 在庫の見張りを待つのは合計18分まで（それ以上は待たずに探す）
DISCOVER_BUDGET = 18 * 60   # 候補さがしで探す時間の上限（秒）。機種が多いので、残りは次の回に回す
SPLIT_AT = 230   # 見張る件数がこれ以上になったら、見張りを2本に分けるようメールで知らせる
FREE_LISTINGS = 1000   # 2026/10/21 ベーシックストア契約（それまでは250件）
DESC_STOP_MAX = 20     # 説明文の理由で一度に止める上限（超えたら判定ミスとみなして止めずに知らせる）
FREE_MARGIN = 5        # 数え違いに備えて、245件で止める
PT = ZoneInfo("America/Los_Angeles")   # eBayの月の区切り（米国太平洋時間）


FREE_TODAY = {"new": 0, "days_left": 30}


def list_cap(settings):
    """30分ごとの見張り1回でeBayに出品する最大件数（設定シートB15）"""
    try:
        return max(1, int(float(settings.get("max_add") or 20)))
    except (TypeError, ValueError):
        return 20


CANDIDATE_POOL_MAX = 100   # 出品待ちの候補（まだeBayに出していない行）はシート全体でこれまで。見張りが30分に間に合うように


def day_cap(free_used):
    """今日出してよい新しい出品の件数：残りの無料枠を月末までの日数で割る（月の途中で枠を使い切らないため）"""
    left = (FREE_LISTINGS - FREE_MARGIN) - free_used + FREE_TODAY["new"]
    return max(0, math.ceil(left / FREE_TODAY["days_left"]))


def free_listings_used(token):
    """今月使う無料出品枠の件数（今月出した件数＋30日ごとの自動更新。出品中のものは月末までの更新も先に数える）"""
    now_pt = datetime.now(PT)
    m0 = now_pt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    m1 = (m0 + timedelta(days=32)).replace(day=1)
    f = lambda d: d.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    used, page_no = 0, 1
    today0 = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
    FREE_TODAY["new"] = 0
    FREE_TODAY["days_left"] = max(1, (m1 - today0).days)
    while page_no <= 20:
        ack, errs, root = trading_call_root(
            "GetSellerList",
            f"<EndTimeFrom>{f(m0)}</EndTimeFrom><EndTimeTo>{f(m0 + timedelta(days=118))}</EndTimeTo>"
            "<GranularityLevel>Coarse</GranularityLevel>"
            f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page_no}</PageNumber></Pagination>", token)
        if ack not in ("Success", "Warning"):
            raise RuntimeError("出品の件数を数えられませんでした：" + " / ".join(errs)[:200])
        for it in root.iter("{" + NS["e"] + "}Item"):
            st = it.findtext("e:ListingDetails/e:StartTime", default="", namespaces=NS)
            en = it.findtext("e:ListingDetails/e:EndTime", default="", namespaces=NS)
            status = it.findtext("e:SellingStatus/e:ListingStatus", default="", namespaces=NS)
            if not st:
                continue
            t = datetime.fromisoformat(st.replace("Z", "+00:00"))
            if t >= today0:
                FREE_TODAY["new"] += 1   # 今日（米国太平洋時間）新しく出した件数
            end = datetime.fromisoformat(en.replace("Z", "+00:00")) if en else m1
            last = m1 if status == "Active" else min(end, m1)   # 出品中なら月末までの更新も数える
            while t < last:
                if t >= m0:
                    used += 1
                t += timedelta(days=30)
        pages = int(root.findtext("e:PaginationResult/e:TotalNumberOfPages", default="1", namespaces=NS) or 1)
        if page_no >= pages:
            break
        page_no += 1
    return used


# eBayの「eBay外での取引」ポリシー違反を防ぐ：説明文からURL・連絡先・他サイトの話を消す
RISKY_LINE = re.compile(r"メルカリ|ﾒﾙｶﾘ|ヤフオク|ヤフー|Yahoo|PayPay|ペイペイ|ラクマ|Amazon|アマゾン|楽天|ジモティ|"
                        r"ショップ|店舗|オンラインストア|ストア|ホームページ|HP|サイト|LINE|ライン|Instagram|インスタ|"
                        r"Twitter|ツイッター|Facebook|TikTok|YouTube|プロフ|他にも|他の出品|出品中|同時出品|専用|"
                        r"コメント|いいね|即購入|値下げ|交渉|フォロー|#|＃|連絡|問い合わせ|お問合せ|電話|メール|"
                        r"mercari|rakuma|shop|store|http|www\.|@", re.I)
URL_RE = re.compile(r"https?://\S+|www\.\S+|[\w.-]+\.(?:com|jp|net|org|shop|me|info|biz)\S*|[\w.+-]+@[\w-]+\.[\w.]+|"
                    r"0\d{1,4}[-‐ー−]?\d{1,4}[-‐ー−]?\d{3,4}", re.I)


def clean_desc(text):
    lines = []
    for ln in unicodedata.normalize("NFKC", text or "").splitlines():
        if RISKY_LINE.search(ln):
            continue
        ln = URL_RE.sub("", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)[:1500]


PHONE_CATEGORY = re.compile(r"携帯電話本体|スマートフォン本体|PHS本体|本体[(（]|テレビゲーム.*>\s*本体|コンパクトデジタルカメラ|ポータブルMDプレーヤー|"
                            r"ポータブルCDプレーヤー|CDプレーヤー|カセットプレーヤー|ビデオカメラ|時計|デジタルカメラ")


ONLY_PHONE_CATEGORY = re.compile(r"携帯電話本体|スマートフォン本体|PHS本体|携帯電話|ガラケー")


def not_a_phone(r, title_jp, detail):
    """仕入れ先の商品が携帯電話本体でなさそうなら理由を返す（CDの品番を型番と間違えた、など）"""
    cat = detail.get("category", "")
    if cat and not game_info((r.get("query") or "").strip()) and not ONLY_PHONE_CATEGORY.search(cat):
        # 2026/10/28 知らない型番（例：富士フイルム S8100fd）をガラケーとして出品しないよう、携帯電話のカテゴリだけ認める
        return f"携帯電話本体ではない商品です（カテゴリ：{cat[:40]}）"
    if cat and not PHONE_CATEGORY.search(cat):
        return f"携帯電話本体ではない商品です（カテゴリ：{cat[:40]}）"
    if cat:
        return ""   # カテゴリが携帯電話本体なら本体とみなす（型番が書かれていない出品もあるため）
    q = (r.get("query") or "").strip()
    if len(_flat(q)) >= 4 and not has_model(title_jp + " " + detail.get("description", ""), q):
        return f"商品名・説明に型番 {q} がありません"
    return ""


# 1点ものの中古品であることを正直に伝えて、ウォッチしている人に早めの購入を促す
URGENCY_HTML = ("<p><b>Only 1 available.</b> This is a one-of-a-kind used item and we have just this single unit. "
                "Once it sells, it will not be restocked, so please don't wait too long if you would like it!</p>")


# 2026/10/29 ゲーム機・カメラなどの英語タイトルに、海外の人が検索する言葉を足す（80文字まで使う）
# 正式名（略称で検索しない人向け）。実物を見ていないので Tested / Working などは入れない
FULL_NAMES = {"Sony PSP": "PlayStation Portable", "Sony PSP Go": "PlayStation Portable",
              "Sony PlayStation Vita": "PlayStation Vita"}
# CCDセンサーの機種（「CCD」で探す人が多い）。わかっている機種だけ
CCD_RE = re.compile(r"Cyber-shot DSC-(W\d|T\d)|FinePix|Lumix DMC-F|Exilim EX-|Ricoh R10|COOLPIX S[34]\d{3}")


def game_title(gi, color):
    """<シリーズ 型番> <正式名> <CCD> <商品の種類> <色> Japan（長すぎたら 色→正式名→CCD→Japan の順に削る）"""
    kind = gi.get("kind")
    full = FULL_NAMES.get(gi.get("platform"), "")
    ccd = "CCD" if kind == "camera" and CCD_RE.search(gi["series"]) else ""
    mv = gi.get("movement", "")
    noun = {"camera": "Compact Digital Camera", "md": "Portable MiniDisc Player",
            "cassette": "Portable Cassette Player", "cd": "Portable CD Player",
            "video": "Camcorder Video Camera",
            "watch": ("Automatic Watch" if mv == "Automatic" else
                      "Solar Digital Watch" if "Solar" in mv else
                      "Digital Watch" if gi.get("display") == "Digital" else "Watch")
            }.get(kind, "Handheld Game Console")
    parts = [gi["series"], full, ccd, noun, color, "Japan"]

    def join(ps):
        return re.sub(r"\s+", " ", " ".join(p for p in ps if p)).strip()
    t = join(parts)
    for drop in (color, full, ccd, "Japan"):
        if len(t) <= 80:
            break
        if drop:
            parts.remove(drop)
            t = join(parts)
    return t[:80]


def retitle():
    """出品中のゲーム機・カメラなどのタイトルを新しい形に書き換える（1回だけ実行。ウォッチはそのまま残る）"""
    data = sheet("list")
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] in ("", "active")]
    token = get_access_token()
    ok_n = fail_n = skip_n = 0
    updates = []
    for r in rows:
        title_jp = r.get("title") or ""
        model = (r.get("query") or "").strip() or guess_query(title_jp) or ""
        gi = game_info(model)
        if not gi:
            skip_n += 1   # ガラケーは今の形のまま
            continue
        color = next((en for jp, en in COLORS.items() if jp in title_jp), "")
        new = game_title(gi, color)
        ack, errs, root = trading_call_root(
            "ReviseFixedPriceItem",
            f"<Item><ItemID>{r['ebay_item_id']}</ItemID><Title>{escape(new)}</Title></Item>", token)
        if ack in ("Success", "Warning"):
            ok_n += 1
            print("タイトル変更:", r["ebay_item_id"], new)
        else:
            fail_n += 1
            print("タイトル変更に失敗:", r["row"], " / ".join(errs)[:200])
            updates.append({"row": r["row"], "result": "タイトルの変更に失敗：" + " / ".join(errs)[:200]})
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} タイトルの書き換え：変更 {ok_n}件／失敗 {fail_n}件／ガラケーなどそのまま {skip_n}件"
    print(msg)
    sheet("log", message=msg)


# 2026/10/29 フリマの「商品の状態」は決まった英語にする（機械翻訳だと「傷や汚れあり」が
# 「Damaged and stained（壊れていてしみがある）」になり、買い手が不安になるため）
CONDITION_EN = [
    ("未使用に近い", "Like new - used only briefly, no visible scratches or marks."),
    ("目立った傷や汚れなし", "Good - no noticeable scratches or marks."),
    ("やや傷や汚れあり", "Fair - some light scratches or marks from normal use."),
    ("全体的に状態が悪い", "Poor - heavy signs of use overall. Please check the photos."),
    ("傷や汚れあり", "Used - visible scratches or marks on the body. Please check the photos."),
    ("新品", "New / unused."),
    ("未使用", "New / unused."),
]


def condition_en(cond_jp, translated=""):
    c = unicodedata.normalize("NFKC", cond_jp or "")
    for jp, en in CONDITION_EN:
        if jp in c:
            return en
    return translated


def prepare_text(r, info, detail):
    """出品に使う英語タイトル・説明文など（新規出品と説明文の作り直しで共通）"""
    title_jp = info.get("title") or r.get("title", "")
    model = (r.get("query") or "").strip() or guess_query(title_jp) or ""
    text = title_jp + "\n" + detail.get("description", "")
    unlocked = bool(re.search(r"SIM\s*(フリー|ロック解除)|ロック解除済", unicodedata.normalize("NFKC", text), re.I))
    carrier = carrier_of(model, text)
    color = next((en for jp, en in COLORS.items() if jp in title_jp), "") or \
        next((en for jp, en in COLORS.items() if jp in detail.get("description", "")), "")
    gi = game_info(model)
    if gi:
        title_en = game_title(gi, color)
    else:
        title_en = build_title(model, title_jp, carrier, unlocked, color)
    tr = sheet("translate", texts=[detail.get("condition", ""), clean_desc(detail.get("description", ""))])["texts"]
    cond_en, desc_en = condition_en(detail.get("condition", ""), tr[0]), clean_desc(tr[1])
    if gi:
        desc_html = (
            f"<h2>{escape(title_en)}</h2>"
            f"<p><b>Model:</b> {escape(model)}<br><b>Platform:</b> {escape(gi['platform'])}<br>"
            f"<b>Version:</b> Japanese model<br><b>Condition:</b> {escape(cond_en)}</p>"
            + {"camera": "<p>Japanese domestic model digital camera. The menu language may be Japanese only, so please check before purchase. "
                         "Only the items shown in the photos are included (battery, charger, memory card and accessories are included only if pictured).</p>",
               "md": "<p>Japanese domestic model MiniDisc player. Only the items shown in the photos are included "
                     "(battery, charger, remote, discs and accessories are included only if pictured).</p>",
               "cassette": "<p>Japanese domestic model portable cassette player. This is a vintage item; the rubber belt and parts are "
                           "original unless stated. Only the items shown in the photos are included "
                           "(battery, remote, headphones and accessories are included only if pictured).</p>",
               "cd": "<p>Japanese domestic model portable CD player. Only the items shown in the photos are included "
                     "(battery, AC adapter, remote, headphones and accessories are included only if pictured).</p>",
               "video": "<p>Japanese domestic model camcorder. The menu language may be Japanese only, so please check before purchase. "
                        "Only the items shown in the photos are included (battery, charger, tapes/memory cards and accessories are included only if pictured).</p>",
               "watch": "<p>Japanese domestic model wristwatch. Only the items shown in the photos are included "
                        "(box, papers and extra links are included only if pictured). Water resistance is not guaranteed for a used watch.</p>"}.get(gi.get("kind"),
            "<p>Japanese handheld game console. Only the items shown in the photos are included "
            "(charger, memory card, games and accessories are included only if pictured).</p>")
            + (f"<h3>Notes from the original Japanese description (machine translated)</h3><p>{escape(desc_en).replace(chr(10), '<br>')}</p>" if desc_en else "")
            + URGENCY_HTML + "<p>Ships from Japan. Thank you for looking!</p>")
        if gi.get("region_note"):
            desc_html = desc_html.replace(
                "<p>Ships from Japan.", f"<p><b>Region:</b> {escape(gi['region_note'])}</p><p>Ships from Japan.")
        if gi.get("region_lock"):
            desc_html = desc_html.replace(
                "<p>Ships from Japan.",
                "<p><b>Region:</b> This is a Japanese 3DS system. It plays Japanese region games only.</p><p>Ships from Japan.")
        return {"title_jp": title_jp, "model": model, "unlocked": unlocked, "carrier": carrier, "color": color,
                "title_en": title_en, "cond_en": cond_en, "desc_html": desc_html}
    desc_html = (
        f"<h2>{escape(title_en)}</h2>"
        f"<p><b>Model:</b> {escape(model)}<br><b>Carrier:</b> {escape(carrier or 'See description')}<br>"
        f"<b>SIM lock:</b> {'Unlocked' if unlocked else 'Not stated by the original seller. Please check compatibility before purchase.'}<br>"
        f"<b>Condition:</b> {escape(cond_en)}</p>"
        "<p>Japanese mobile phone (keitai). Only the items shown in the photos are included. "
        "Please make sure the phone supports your carrier's network bands before purchasing.</p>"
        + (f"<h3>Notes from the original Japanese description (machine translated)</h3><p>{escape(desc_en).replace(chr(10), '<br>')}</p>" if desc_en else "")
        + URGENCY_HTML + "<p>Ships from Japan. Thank you for looking!</p>")
    return {"title_jp": title_jp, "model": model, "unlocked": unlocked, "carrier": carrier, "color": color,
            "title_en": title_en, "cond_en": cond_en, "desc_html": desc_html}


def create_listing(r, info, detail, settings, token):
    title_jp = info.get("title") or r.get("title", "")
    cost = info.get("price")
    if not cost:
        return None, "価格が読み取れないため出品できません"
    if not settings.get("ship_profile"):
        return None, "「設定」シートに配送ポリシー名が入っていません"
    if float(cost) > max_cost(settings):
        return None, f"SKIP:仕入れ値{int(cost):,}円が上限{int(max_cost(settings)):,}円を超えるため出品しません"
    if float(cost) < 500:
        return None, f"SKIP:仕入れ値{int(cost):,}円は読み取りミスか本体以外の可能性があるため出品しません"
    bad = not_a_phone(r, title_jp, detail) or no_body_reason(title_jp + "\n" + detail.get("description", "")) \
        or junk_reason(title_jp + "\n" + detail.get("description", ""))
    if not bad and game_info((r.get("query") or "").strip()):
        bad = game_reject_reason(title_jp, (r.get("query") or "").strip(), cost, r.get("ebay_usd"), settings)
    if bad:
        return None, f"SKIP:{bad}のため出品しません"
    t = prepare_text(r, info, detail)
    model, unlocked, carrier, color, title_en, cond_en, desc_html = (
        t["model"], t["unlocked"], t["carrier"], t["color"], t["title_en"], t["cond_en"], t["desc_html"])
    target, floor = market_price_usd(cost, model, settings)
    if target is None:
        if EBAY_LIMITED[0]:
            return None, "LIMITWAIT"   # eBayの1日の問い合わせ上限：外さずに次の回で出品する
        return None, "SKIP:eBayの相場が取れないため出品しません"
    if target < floor_price_usd(cost, model, settings, min_profit_for(cost, settings, model)):
        return None, f"SKIP:相場(${target:.2f})では利益{int(min_profit_for(cost, settings, model)):,}円が残らないため出品しません"
    price = item_floor(cost, model, settings, target)   # 利益2,000円の値段。相場がそれより安ければ相場の値段（最低利益は残す）
    pics = upload_pictures(detail.get("images", []), token)
    if not pics:
        return None, "写真をeBayに取り込めなかったため出品を中止しました（もう一度チェックすると再挑戦します）"

    cond_id = 1000 if "新品" in detail.get("condition", "") else 3000
    gi = game_info(model)
    if gi:
        category_id = gi.get("ebay_cat", GAME_EBAY_CATEGORY)
        kind = gi.get("kind")
        specifics = [("Brand", gi["brand"]), ("Model", model), ("Series", gi.get("ser_name", "Sony Cyber-shot")),
                     ("Type", "Point & Shoot"), ("Color", color or "As Pictured")] if kind == "camera" else \
            [("Brand", gi["brand"]), ("Model", gi.get("code", model)), ("Type", "Portable Cassette Player"),
             ("Color", color or "As Pictured")] if kind == "cassette" else \
            [("Brand", gi["brand"]), ("Model", gi.get("code", model)), ("Type", "Portable CD Player"),
             ("Color", color or "As Pictured")] if kind == "cd" else \
            [("Brand", gi["brand"]), ("Model", gi.get("code", model)), ("Type", "Camcorder"),
             ("Color", color or "As Pictured")] if kind == "video" else \
            [("Brand", gi["brand"]), ("Model", gi.get("code", model)), ("Type", "Wristwatch"), ("Department", "Men"),
             ("Movement", gi.get("movement", "Quartz")), ("Display", gi.get("display", "Analog")),
             ("Country of Origin", "Japan")] if kind == "watch" else \
            [("Brand", gi["brand"]), ("Model", model), ("Type", "Portable MiniDisc Player"),
             ("Color", color or "As Pictured")] if gi.get("kind") == "md" else [("Brand", gi["brand"]), ("Model", model), ("Platform", gi["platform"]),
                     ("Type", "Handheld System"), ("Region Code", "NTSC-J (Japan)"),
                     ("Color", color or "As Pictured"), ("Country of Origin", "Japan")]
    else:
        category_id = "9355"
        specifics = [("Brand", brand_of(model, title_jp)), ("Model", model or "Not Specified"),
                     ("Network", "Unlocked" if unlocked else (carrier or "Unknown")),
                     ("Lock Status", "Network Unlocked" if unlocked else "Network Locked" if carrier else "Unknown"),
                     ("Country of Origin", "Japan")]
        # eBay必須：Color と Storage Capacity
        specifics.append(("Color", color or "As Pictured"))
        specifics.append(("Storage Capacity", storage_of(model)))
    nvl = "".join(f"<NameValueList><Name>{escape(n)}</Name><Value>{escape(v)}</Value></NameValueList>" for n, v in specifics)
    sku = sku_for(model) or re.sub(r"[^A-Za-z0-9]", "", r["mercari_url"].rstrip("/").split("/")[-1])[:50]
    item = (
        f"<Item><Title>{escape(title_en)}</Title>"
        f"<Description><![CDATA[{desc_html}]]></Description>"
        f"<PrimaryCategory><CategoryID>{category_id}</CategoryID></PrimaryCategory>"
        f'<StartPrice currencyID="USD">{price:.2f}</StartPrice>'
        f"<ConditionID>{cond_id}</ConditionID>"
        + (f"<ConditionDescription>{escape(cond_en[:900])}</ConditionDescription>" if cond_id != 1000 else "") +
        "<Country>JP</Country><Location>Japan</Location><Currency>USD</Currency><Site>US</Site>"
        "<ListingDuration>GTC</ListingDuration><ListingType>FixedPriceItem</ListingType><Quantity>1</Quantity>"
        f"<SKU>{sku}</SKU>"
        "<PictureDetails>" + "".join(f"<PictureURL>{escape(u)}</PictureURL>" for u in pics) + "</PictureDetails>"
        f"<ItemSpecifics>{nvl}</ItemSpecifics>"
        "<SellerProfiles>"
        f"<SellerPaymentProfile><PaymentProfileName>{escape(str(settings.get('pay_profile') or 'Payment'))}</PaymentProfileName></SellerPaymentProfile>"
        f"<SellerReturnProfile><ReturnProfileName>{escape(str(settings.get('return_profile') or 'Return'))}</ReturnProfileName></SellerReturnProfile>"
        f"<SellerShippingProfile><ShippingProfileName>{escape(str(settings.get('ship_profile')))}</ShippingProfileName></SellerShippingProfile>"
        "</SellerProfiles></Item>")
    ack, errors, root = trading_call_root("AddFixedPriceItem", item, token)
    item_id = root.findtext("e:ItemID", default="", namespaces=NS)
    if ack in ("Success", "Warning") and item_id:
        return {"ebay_item_id": item_id, "price": price, "title_en": title_en}, None
    return None, "出品に失敗：" + " / ".join(errors)[:300]


# ---------- 自動仕入れ候補さがし ----------
SEARCH_JS = """async () => {
    // スクロールしながら集める（画面外の商品は表示が消えることがあるため、見えたものを順に覚えておく）
    const seen = new Map();
    const grab = () => document.querySelectorAll('a[href*="/item/m"], a[href*="/shops/product/"]').forEach(a => {
        const href = a.getAttribute('href');
        if (href.includes('ad_id')) return;   // 広告枠は除く
        const url = 'https://jp.mercari.com' + href.split('?')[0];
        const img = a.querySelector('img');
        const t = a.innerText.replace(/\\d+\\s*%\\s*OFF/gi, '');   // メルカリShopsの「6%OFF」を値段と間違えない
        const ys = [...t.matchAll(/[¥￥]\\s*([\\d,]+)/g)].map(x => parseInt(x[1].replace(/,/g, ''), 10));
        // 値段は「¥」の付いた数字だけを使う（表示しきれていないときに、いいね数などを値段と間違えないように）
        const price = ys.length ? Math.min(...ys) : null;   // 値引き前と後があれば安いほう
        const title = img ? img.alt.replace(/のサムネイル$/, '') : '';
        const old = seen.get(url);
        if (!old || (!old.price && price) || (!old.title && title))
            seen.set(url, { auction: /現在/.test(a.innerText), url, title: title || (old && old.title) || '',
                            price: price || (old && old.price) || null });   // オークションは「現在 ¥◯◯」と表示される
    });
    for (let i = 0; i < 30; i++) {
        grab();
        window.scrollBy(0, 800);
        await new Promise(r => setTimeout(r, 400));
        if (window.innerHeight + window.scrollY >= document.body.scrollHeight - 50 && i > 3) { await new Promise(r => setTimeout(r, 800)); grab(); break; }
    }
    grab();
    return [...seen.values()];
}"""


# ---------- Yahoo!フリマ（旧PayPayフリマ） ----------
YAHOO_API = "https://paypayfleamarket.yahoo.co.jp/api"
YAHOO_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                 "Accept": "application/json", "Referer": "https://paypayfleamarket.yahoo.co.jp/"}
YAHOO_PHONE_CATEGORY = 38419   # 携帯電話本体
YAHOO_OK_CATEGORIES = {38419, 37849, 37661, 37697, 37799, 37808, 38155, 19801, 49488,
                       49487, 49489, 49429, 1647, 1645}   # 2026/10/28 ＋CD・カセットプレーヤー・ビデオカメラ・腕時計   # ＋コンパクトデジカメ・ポータブルMD   # 携帯電話・PSP・Vita・DS・GB・GBA・3DS の本体


def is_yahoo(url):
    """ブラウザを使わずに確認できる仕入れ先（Yahoo!フリマ・ラクマ）"""
    return "paypayfleamarket.yahoo.co.jp/item/" in url or is_rakuma(url)


def is_rakuma(url):
    return "item.fril.jp/" in (url or "")


def source_name(url):
    return "ラクマ" if is_rakuma(url) else "Yahoo!フリマ" if "paypayfleamarket" in (url or "") else "メルカリ"


def yahoo_item(url):
    item_id = url.rstrip("/").split("/item/")[-1].split("?")[0]
    r = requests.get(f"{YAHOO_API}/item/v2/items/{item_id}", headers=YAHOO_HEADERS, timeout=30)
    return r.status_code, (r.json() if r.status_code == 200 else {})


def check_yahoo(url):
    """メルカリの check_mercari と同じ形 (status, info, detail) を返す（ラクマのURLならラクマを確認）"""
    if is_rakuma(url):
        return check_rakuma(url)
    try:
        code, j = yahoo_item(url)
    except Exception:
        return "unknown", {}, {}
    if code == 404:
        return "deleted", {}, {}
    if code in (403, 429):
        return "blocked", {}, {}
    if code != 200:
        return "unknown", {}, {}
    st = j.get("status")
    status = "on_sale" if st == "OPEN" else "sold" if st in ("SOLD", "CLOSED") else "unknown"
    info = {"title": j.get("title", ""), "price": j.get("price"), "seller": (j.get("seller") or {}).get("id", "")}
    detail = {"images": [i.get("url") for i in j.get("images", []) if i.get("url")][:12],
              "description": j.get("description", ""),
              "condition": (j.get("condition") or {}).get("text", ""),
              "category": " > ".join(c.get("name", "") for c in ((j.get("category") or {}).get("path") or []))
                          or (j.get("category") or {}).get("name", "")}
    return status, info, detail


def seller_id(v):
    """出品者のID（URLを貼られても最後の部分だけにそろえる）"""
    v = str(v or "").strip().rstrip("/")
    m = re.search(r"(?:shops/profile|user/profile|shops|user)/([^/?#]+)", v)
    return (m.group(1) if m else v).strip()


def excluded_sellers(data):
    return {seller_id(x.get("id")) for x in (data.get("excluded") or []) if seller_id(x.get("id"))}


def yahoo_search(keyword, settings=None, max_price=None, max_items=500):
    """Yahoo!フリマを安い順に検索（除外ワードは検索の時点で外す。上限の値段まで、最大500件まで2ページ目以降も見る）"""
    q = keyword + "".join(f" -{w}" for w in search_exclude_words(settings))
    out, offset = [], 0
    while offset < max_items:
        r = requests.get(f"{YAHOO_API}/v1/search", headers=YAHOO_HEADERS, timeout=30,
                         params={"query": q, "results": 100, "sort": "price", "order": "asc",
                                 "itemStatus": "open", "offset": offset})
        r.raise_for_status()
        items = r.json().get("items", [])
        for it in items:
            if (it.get("category") or {}).get("id") not in YAHOO_OK_CATEGORIES:
                continue          # 携帯電話本体以外（電池・ケースなど）は除く
            if it.get("condition") == "used80":
                continue          # 全体的に状態が悪い
            out.append({"url": f"https://paypayfleamarket.yahoo.co.jp/item/{it['id']}",
                        "title": it.get("title", ""), "price": it.get("price"), "seller": it.get("sellerId", "")})
        if len(items) < 100 or (max_price and (items[-1].get("price") or 0) > max_price):
            break             # 最後のページ、または仕入れ上限より高くなったら終わり
        offset += 100
        time.sleep(1)
    return out


# 検索の時点で外すワード（メルカリ・Yahoo!フリマとも、商品名と説明文の両方が対象）
# 設定シートB17で変えられる。説明文にもよく出る言葉（ケース・保護など）は入れない（まともな出品まで消えるため）
SEARCH_EXCLUDE_DEFAULT = ["ジャンク", "部品取り", "パーツ取り", "動作未確認", "通電のみ", "通電確認のみ",
                          "ガラクタ", "訳あり", "訳アリ", "不動品", "電源入らない"]


def search_exclude_words(settings):
    v = str((settings or {}).get("search_exclude") or "").strip()
    words = [w.strip() for w in re.split(r"[,、\s]+", v) if w.strip()] if v else SEARCH_EXCLUDE_DEFAULT
    return words[:20]


def mercari_search_url(kw, gi, settings, page_no=0):
    """メルカリの検索URL（新しい順・本体カテゴリ・通常出品のみ・除外ワード付き・page_noページ目）"""
    url = ("https://jp.mercari.com/search?status=on_sale&sort=created_time&order=desc"
           f"&category_id={gi['mercari'] if gi else '860'}&keyword="
           + ("" if gi and gi.get("exact") else requests.utils.quote(kw)) + MERCARI_FIXED)   # 機種別カテゴリなら検索語なし
    ex = search_exclude_words(settings)
    if ex:
        url += "&exclude_keyword=" + requests.utils.quote(" ".join(ex))
    if page_no:
        url += "&page_token=" + requests.utils.quote(f"v1:{page_no}")
    return url


MERCARI_PAGES = 5   # 深く見るときのメルカリの最大ページ数（1ページ約120件。1ページに満たなくなったら終わり）


FULL_SCAN = os.environ.get("FULL_SCAN") == "1"   # 手動の「全部見る回」：時間の上限なしで、全機種を最後のページまで見る


def mercari_pages_for(kw):
    """ふだんは1ページ目（新着）だけ。機種ごとに1日1回だけ3ページまで見る（前からある出品の値下げを拾う）。
    機種ごとに見る時間をずらして、1回の候補さがしが重くならないようにする"""
    if FULL_SCAN:
        return 50   # 1ページに満たなくなったら終わる（最後のページまで）
    h = sum(ord(c) for c in kw) % 24
    return MERCARI_PAGES if datetime.now(JST).hour == h else 1


ACCESSORY_WORDS = ["説明書", "取説", "取扱", "充電器", "充電台", "卓上ホルダ", "卓上ホルダー", "アダプタ", "ACアダプ",
                   "ケーブル", "電池パック", "充電ホルダ", "充電スタンド", "クレードル", "バッテリーパック", "外箱", "空箱", "元箱のみ", "付属品のみ", "パーツ", "部品",
                   "シェル", "外装", "ハウジング", "筐体", "液晶パネル", "液晶のみ", "画面のみ", "基板", "ボタン",
                   "スティック", "UMD", "ソフトのみ", "ソフト単品", "ゲームソフト", "メモリースティック", "メモリーカード", "ポーチ", "スキンシール"]
BODY_WORDS = ["本体", "付き", "付属", "同梱", "セット内容", "揃い", "完品", "一式", "なし", "無し"]


ACCESSORY_PRICE_RATIO = 0.2   # eBay相場のこの割合以下の値段なら「本体にしては安すぎる」


def looks_like_accessory(title):
    t = unicodedata.normalize("NFKC", title or "")
    return any(w in t for w in ACCESSORY_WORDS) and not any(w in t for w in BODY_WORDS)


def is_accessory(title, price_jpy, sale_usd, settings):
    """携帯電話本体カテゴリに出ている「説明書だけ」「充電器だけ」などを見分ける。
    付属品っぽい単語がある「かつ」本体にしては安すぎる、の両方のときだけ付属品とみなす（本体を弾かないため）"""
    t = unicodedata.normalize("NFKC", title or "")
    if not any(w in t for w in ACCESSORY_WORDS) or any(w in t for w in BODY_WORDS):
        return False
    try:
        market_jpy = float(sale_usd) * usd_jpy(settings)
        return float(price_jpy) <= market_jpy * ACCESSORY_PRICE_RATIO
    except (TypeError, ValueError):
        return False   # 値段か相場が分からないときは外さない


PENDING = "要仕入れ"   # eBayで売れて、まだ仕入れていない行の状態
MERCARI_FIXED = ("&d664efe3-ae5a-4824-b729-e789bf93aba9=B38F1DC9286E0B80812D9B19DB14298C1FF1116CA8332D9EE9061026635C9088"   # 出品形式：通常出品（オークションを除く）
                 "&item_condition_id=1%2C2%2C3%2C4%2C5")   # 商品の状態：「全体的に状態が悪い」を除く


def find_alternative(page, query, sold_usd, settings, skip=()):
    """eBayで売れたのに仕入れ元が売り切れていたとき、同じ機種で利益が残る代わりを探す（利益が一番大きいもの）"""
    q = (query or "").strip()
    if not q or not sold_usd:
        return None
    gi = game_info(q)
    items = []
    url = ("https://jp.mercari.com/search?status=on_sale&sort=price&order=asc"
           f"&category_id={gi['mercari'] if gi else '860'}&keyword="
           + ("" if gi and gi.get("exact") else requests.utils.quote(q)) + MERCARI_FIXED)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector('a[href*="/item/m"], a[href*="/shops/product/"]', timeout=25000)
        items += [dict(it, exact=bool(gi and gi.get("exact"))) for it in page.evaluate(SEARCH_JS)]
    except Exception:
        pass
    try:
        items += yahoo_search(gi.get("yahoo_q", q) if gi else q)
    except Exception:
        pass
    fx = usd_jpy(settings)
    fee = (float(settings.get("fee_pct") or 15) + float(settings.get("other_pct") or 2)) / 100 + ad_pct(q) / 100 + duty_pct(settings) / 100
    ship = ship_for(settings, q)
    best = None
    for it in items:
        u = it["url"].split("?")[0]
        if u in skip or not it.get("price") or it.get("auction") or it["price"] > max_cost(settings):
            continue
        title = it.get("title") or ""
        if not it.get("exact") and not has_model(title, q) and not (gi and gi.get("alias") and match_game(title, q)):
            continue   # 同じ機種と確認できないものは使わない
        if looks_like_accessory(title) or is_accessory(title, it["price"], sold_usd, settings):
            continue
        if gi and game_reject_reason(title, q, it["price"], sold_usd, settings):
            continue
        p = sold_usd * fx * (1 - fee) - ship - it["price"]
        if p > 0 and (best is None or p > best["profit"]):
            best = {"url": u, "price": it["price"], "profit": p, "title": title}
    return best


_mon_check = {"t": 0, "busy": False}


def monitor_running():
    """在庫の見張りが動いているか（動いている間は候補さがしがメルカリを見に行くのを待つ）"""
    tok, repo = os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return False
    if time.time() - _mon_check["t"] < 45:
        return _mon_check["busy"]
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/monitor.yml/runs",
                         params={"status": "in_progress", "per_page": 1},
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, timeout=15)
        busy = r.status_code == 200 and r.json().get("total_count", 0) > 0
    except Exception:
        busy = False
    _mon_check.update(t=time.time(), busy=busy)
    return busy


SHARDS = int(os.environ.get("SHARDS") or 1)   # 候補さがしを何本に分けて同時に動かすか（discover.yml で指定）
SHARD = int(os.environ.get("SHARD") or 0)     # この候補さがしが受け持つ番号（0から）


def discover():
    try:
        if SHARD == 0:
            token_warning()   # トークンの期限が近ければ知らせる（1日1回・1本目だけ）
    except Exception as e:
        print("トークンの期限の確認に失敗:", str(e)[:100])
    data = sheet("list")
    settings = data.get("settings", {})
    known = {r["mercari_url"].split("?")[0] for r in data["rows"]}
    # 一度候補から外して「履歴」に移した商品も飛ばす（2026/10/28：同じジャンク品が毎回拾われ、追加の枠を取っていたため）
    known |= {str(u).split("?")[0] for u in (data.get("seen_urls") or []) if u}
    keywords = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "ガラケー")) if k.strip()]
    KNOWN_MODELS[:] = keywords
    min_margin = 0   # 2026/10/22 利益率の条件はやめて、利益2,000円以上かどうかだけで判断する（設定シートB13は使わない）
    min_profit = float(settings.get("min_profit") or 1500)
    # シートへの追加は20件に限らない（出品の件数は見張り側で1回B15件まで）。ただし出品待ちの候補が多すぎると見張りが遅れるので、
    # 出品待ちはシート全体で CANDIDATE_POOL_MAX 件まで（4本で分ける）。追加できなかった候補は、次の回にまた見つかる
    pending = sum(1 for r in data["rows"] if not r.get("ebay_item_id") and r.get("status") in ("", "active"))
    max_add = max(0, (CANDIDATE_POOL_MAX - pending) // max(1, SHARDS))
    if FULL_SCAN:
        max_add = 10000   # 全部見る回は取りこぼさないよう上限なし（同じ機種は3件までのルールは同じ）
    exclude = [w.strip() for w in re.split(r"[,、\n]", str(settings.get("exclude") or "")) if w.strip()]
    excluded = excluded_sellers(data)

    found, seen, checked = [], set(), 0
    pages_seen = [0]   # メルカリで見たページ数（記録用）
    load_market_cache()   # 前回までに調べたeBay相場（6時間以内）を使い回す
    t0 = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        # 高速化：画像・動画・フォントは読み込まない（検索結果の判定に不要）
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("image", "media", "font") else route.continue_())
        waited = 0   # 在庫の見張りを待った時間（秒）
        order = [k for i, k in enumerate(keywords) if i % SHARDS == SHARD]   # 受け持ちの機種だけ（4本で分ける）
        random.Random(int(time.time() // 3600)).shuffle(order)   # 毎回ちがう順番で回る（時間切れでも、全部の機種を何回かに分けて探せる）
        order.sort(key=lambda k: mercari_pages_for(k) == 1)   # 今の時間に深く見る機種を先に回す（時間切れで深い確認が抜けないように）
        for kw in order:
            if monitor_running() and waited < MAX_WAIT:
                print("在庫の見張りが動いているので、終わるまで待ちます")
                w0 = time.time()
                while monitor_running() and waited + (time.time() - w0) < MAX_WAIT:
                    time.sleep(20)   # 在庫の見張りを優先（同時にメルカリを見に行くと、見張りが判定できなくなることがある）
                waited += time.time() - w0
            if not FULL_SCAN and time.time() - t0 - waited > DISCOVER_BUDGET:   # 待った時間は探す時間に数えない（全部見る回は上限なし）
                print(f"時間切れ（{DISCOVER_BUDGET // 60}分）のため、残りの機種は次回に回します")
                break
            gi = game_info(kw)
            items = []
            for pno in range(mercari_pages_for(kw)):   # ふだんは新着だけ、1日1回だけ前からある出品も見る
                try:
                    page.goto(mercari_search_url(kw, gi, settings, pno), wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_selector('a[href*="/item/m"], a[href*="/shops/product/"]', timeout=25000)
                    got = page.evaluate(SEARCH_JS)
                except Exception:
                    break
                items += got
                pages_seen[0] += 1
                if len(got) < 100:   # 1ページに満たない＝最後のページ
                    break
                time.sleep(random.uniform(3, 5))
            for it in items:
                u = it["url"]
                if u in known or u in seen or not it["price"] or it.get("auction") or it["price"] > max_cost(settings) \
                        or it["price"] < 500:   # 500円未満は読み取りミスか付属品（2026/10/28）
                    continue
                seen.add(u)
                if any(w in it["title"] for w in exclude) or reserved_reason(it["title"]):
                    continue
                q = kw if gi and (gi.get("exact") or (gi.get("alias") and gi.get("kind") and match_game(it["title"], kw))) \
                    else guess_query(it["title"])   # 機種別カテゴリならその機種。呼び名で分かる機種（2026/10/28追加分）もその機種
                if not q or (gi and not game_info(q)):   # カメラなどのカテゴリで見つけた知らない型番は候補にしない（2026/10/28）
                    continue
                checked += 1
                sale, n = ebay_market_usd(q)
                if not sale or n < MIN_COUNT:  # 相場の件数が少ない機種は入れない
                    continue
                if is_accessory(it["title"], it["price"], sale, settings):   # 説明書・充電器だけの出品
                    continue
                if game_info(q) and game_reject_reason(it["title"], q, it["price"], sale, settings):
                    continue   # ゲーム機は商品名で本体かどうかを厳しく確認
                p, rate = profit(it["price"], sale, settings, q)
                if rate >= min_margin and p >= max(min_profit, min_profit_for(0, settings, q)):
                    found.append({"mercari_url": u, "title": it["title"], "cost": it["price"],
                                  "ebay_usd": sale, "ebay_count": n, "query": q,
                                  "profit": p, "margin": rate, "memo": "自動発見",
                                  "result": f"自動発見 {now()}"})
            # Yahoo!フリマも同じキーワードで探す
            try:
                yitems = yahoo_search(gi.get("yahoo_q", kw) if gi else kw, settings, max_cost(settings),
                                      max_items=5000 if FULL_SCAN else 500)
            except Exception:
                yitems = []
            for it in yitems:
                u = it["url"]
                if u in known or u in seen or not it["price"] or it["price"] > max_cost(settings):
                    continue
                seen.add(u)
                if any(w in it["title"] for w in exclude) or reserved_reason(it["title"]):
                    continue
                if seller_id(it.get("seller")) in excluded:   # 除外者リストの出品者
                    continue
                if gi and gi.get("alias"):
                    q = kw if match_game(it["title"], kw) else ""   # 呼び名で機種を見分ける
                else:
                    q = guess_query(it["title"])
                if not q or (gi and not game_info(q)):
                    continue
                checked += 1
                sale, n = ebay_market_usd(q)
                if not sale or n < MIN_COUNT:
                    continue
                if is_accessory(it["title"], it["price"], sale, settings):
                    continue
                if game_info(q) and game_reject_reason(it["title"], q, it["price"], sale, settings):
                    continue
                p, rate = profit(it["price"], sale, settings, q)
                if rate >= min_margin and p >= max(min_profit, min_profit_for(0, settings, q)):
                    found.append({"mercari_url": u, "title": it["title"], "cost": it["price"],
                                  "ebay_usd": sale, "ebay_count": n, "query": q,
                                  "profit": p, "margin": rate, "memo": "自動発見（Yahoo!フリマ）",
                                  "result": f"自動発見 {now()}"})
            time.sleep(random.uniform(4, 7))   # メルカリに止められないよう、検索ごとに少し待つ
        browser.close()
    save_market_cache()

    # よく売れる機種を優先し、同じ機種は（出品中＋候補で）3件までにする
    per_model = {}
    for x in data["rows"]:
        if x["status"] in ("", "active"):
            k = model_key(x.get("query"))
            per_model[k] = per_model.get(k, 0) + 1
    found.sort(key=lambda x: (SOLD90.get(x["query"], 0), x["profit"]), reverse=True)
    added = []
    for x in found:
        if len(added) >= max_add:
            break
        k = model_key(x["query"])
        if per_model.get(k, 0) >= cap_for(x["query"]):
            continue
        per_model[k] = per_model.get(k, 0) + 1
        added.append(x)
    if added:
        sheet("append", rows=added)
        best = added[0]
        notify(f"【仕入れ候補】{len(added)}件を追加しました。最高：{best['title'][:30]} 想定利益 {best['profit']:,}円（{best['margin']}%）")
    msg = (f"{now()} {'【全部見る回】' if FULL_SCAN else ''}候補さがし：メルカリ{pages_seen[0]}ページ・{len(seen)}件を確認、{checked}件を計算、条件に合う{len(found)}件のうち{len(added)}件を追加"
           f"（{(time.time() - t0) / 60:.0f}分・相場は{_market_stats['hit']}件を使い回し、{_market_stats['miss']}件を新しく取得）")
    print(msg)
    sheet("log", message=msg)


# ---------- ラクマ（仕入れはやめた。以前の行を確認して止めるためだけに残す） ----------
RAKUMA_GENRES = {"669": "携帯電話本体", "668": "スマートフォン本体", "1386": "コンパクトデジタルカメラ",
                 "789": "テレビゲーム > 本体", "695": "ポータブルMDプレーヤー"}
RAKUMA_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "ja-JP"}


def check_rakuma(url):
    """ラクマの商品ページ → (status, info, detail)"""
    import html as _html
    try:
        r = requests.get(url, headers=RAKUMA_UA, timeout=30)
    except Exception:
        return "unknown", {}, {}
    if r.status_code == 404:
        return "deleted", {}, {}
    if r.status_code in (403, 429):
        return "blocked", {}, {}
    if r.status_code != 200:
        return "unknown", {}, {}
    h = r.text
    ld = {}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
        try:
            j = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("@type") == "Product":
            ld = j
            break
    offers = ld.get("offers") or {}
    if "SOLD OUT" in h:
        status = "sold"
    elif "購入に進む" in h or "InStock" in str(offers.get("availability", "")):
        status = "on_sale"
    else:
        status = "unknown"
    try:
        price = int(float(offers.get("price")))
    except (TypeError, ValueError):
        price = None
    imgs = []
    for u in re.findall(r"https://img\.fril\.jp/img/\d+/l/\d+\.jpg", h):
        if u not in imgs:
            imgs.append(u)
    g = re.search(r'category_id(?:&quot;|")\s*:\s*(?:&quot;|")(\d+)', h)
    genre = g.group(1) if g else ""
    info = {"title": _html.unescape(ld.get("name", "")).strip(), "price": price}
    detail = {"images": imgs[:12], "description": ld.get("description", ""),
              "condition": "新品" if "NewCondition" in str(offers.get("itemCondition", "")) else "",
              "category": RAKUMA_GENRES.get(genre, f"その他（ラクマ {genre}）")}
    return status, info, detail


def orphans():
    """eBayで出品中なのにシートにない出品を表示する（調査用・何も変更しない）"""
    data = sheet("list")
    ids = {r["ebay_item_id"]: r for r in data["rows"] if r["ebay_item_id"]}
    act = ebay_active_items(get_access_token())
    print(f"eBayで出品中 {len(act)}件／シートの出品番号 {len(ids)}件")
    for i in act:
        if i not in ids:
            print("シートにない出品:", i)
    for i, r in ids.items():
        if i not in act:
            print("eBayで出品中でない行:", i, r["status"], r["row"], (r.get("title") or "")[:30])
    for i in ("366746609929", "366746610851"):
        for r in data["rows"]:
            if i in str(r.get("result")) or i in str(r.get("ebay_item_id")):
                print("関係する行:", r["row"], r["status"], r["ebay_item_id"], r["mercari_url"], (r.get("result") or "")[:60])


def dupdebug(ids):
    """指定したeBay出品の仕入れ先と写真の差を表示する（調査用・何も変更しない）"""
    data = sheet("list")
    rows = [r for r in data["rows"] if r["ebay_item_id"] in ids]
    thumbs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for r in rows:
            if is_yahoo(r["mercari_url"]):
                st, info, detail = check_yahoo(r["mercari_url"])
            else:
                st, info = check_mercari(page, r["mercari_url"])
                detail = page.evaluate(DETAIL_JS) if st == "on_sale" else {}
            imgs = detail.get("images", [])
            print(r["ebay_item_id"], r["status"], st, info.get("price"), r["mercari_url"])
            print("  写真:", imgs[:3])
            t = []
            for u in imgs[:4]:
                try:
                    t.append(dhash_url(u))
                except Exception as e:
                    print("  写真エラー:", u, str(e)[:80])
            thumbs.append((r["ebay_item_id"], t))
        browser.close()
    for i in range(len(thumbs)):
        for j in range(i + 1, len(thumbs)):
            for a_i, a in enumerate(thumbs[i][1]):
                for b_i, b in enumerate(thumbs[j][1]):
                    d = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
                    print(f"  {thumbs[i][0]}[{a_i}] vs {thumbs[j][0]}[{b_i}]: 差 {d:.1f}")


def sku_for(model):
    """eBayのSKU＝型番（CPaSSのSKU設定と同じ文字にして、発送時の申告内容を自動で入れる）"""
    return re.sub(r"[^A-Z0-9-]", "", (model or "").upper())[:50]


def setsku():
    """出品中の全商品のSKUを型番に書き換える（1回だけ実行）"""
    data = sheet("list")
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] in ("", "active") and sku_for(r.get("query"))]
    token = get_access_token()
    ok_n = fail_n = 0
    for r in rows:
        ack, errs, root = trading_call_root(
            "ReviseFixedPriceItem",
            f"<Item><ItemID>{r['ebay_item_id']}</ItemID><SKU>{sku_for(r.get('query'))}</SKU></Item>", token)
        if ack in ("Success", "Warning"):
            ok_n += 1
        else:
            fail_n += 1
            print("SKUの変更に失敗:", r["row"], " / ".join(errs)[:200])
    msg = f"{now()} SKUを型番に変更：{ok_n}件／失敗 {fail_n}件"
    print(msg)
    sheet("log", message=msg)


def fixdesc(end_bad=True):
    """出品中の全商品：携帯電話本体でないものはeBayを終了、それ以外は説明文を安全なものに作り直す（1回だけ実行）"""
    data = sheet("list")
    settings = data.get("settings", {})
    KNOWN_MODELS[:] = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "")) if k.strip()]
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] in ("", "active")]
    token = get_access_token()
    fixed = ended = failed = 0
    updates = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for r in rows:
            try:
                if is_yahoo(r["mercari_url"]):
                    status, info, detail = check_yahoo(r["mercari_url"])
                else:
                    status, info = check_mercari(page, r["mercari_url"])
                    detail = page.evaluate(DETAIL_JS) if status == "on_sale" else {}
                if status != "on_sale" or not info:
                    continue   # 売り切れ等は通常の監視で止まる
                bad = not_a_phone(r, info.get("title", ""), detail)
                if bad and end_bad:
                    ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                    updates.append({"row": r["row"], "status": "stopped" if ok else None,
                                    "result": f"{bad}のためeBayを終了 → {msg}"})
                    ended += ok
                    continue
                t = prepare_text(r, info, detail)
                ack, errs, root = trading_call_root(
                    "ReviseFixedPriceItem",
                    f"<Item><ItemID>{r['ebay_item_id']}</ItemID><Description><![CDATA[{t['desc_html']}]]></Description>"
                    + ("" if "新品" in detail.get("condition", "")
                       else f"<ConditionDescription>{escape(t['cond_en'][:900])}</ConditionDescription>")
                    + "</Item>",
                    token)
                if ack in ("Success", "Warning"):
                    fixed += 1
                else:
                    failed += 1
                    updates.append({"row": r["row"], "result": "説明文の修正に失敗：" + " / ".join(errs)[:200]})
            except Exception as e:
                failed += 1
                print("説明文の修正でエラー:", r["row"], str(e)[:200])
        browser.close()
    updates = [{k: v for k, v in u.items() if v is not None} for u in updates]
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 説明文の作り直し：修正 {fixed}件／eBay終了 {ended}件／失敗 {failed}件"
    print(msg)
    sheet("log", message=msg)


def restore():
    """型番チェックの不具合で止めてしまった出品を元に戻し、説明文を作り直す"""
    data = sheet("list")
    token = get_access_token()
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] == "stopped"
            and (re.search(r"型番 .+ がありませんのためeBayを終了", r.get("result") or "")
                 or ("にも出ているため停止" in (r.get("result") or ""))
                 or ("単体のため出品を止めました" in (r.get("result") or "")
                     and not looks_like_accessory(r.get("title") or "")))]
    updates, ok_n, relist_n, fail_n = [], 0, 0, 0
    for r in rows:
        ack, errs, root = trading_call_root(
            "ReviseInventoryStatus",
            f"<InventoryStatus><ItemID>{r['ebay_item_id']}</ItemID><Quantity>1</Quantity></InventoryStatus>", token)
        if ack in ("Success", "Warning"):
            ok_n += 1
            updates.append({"row": r["row"], "status": "active", "result": "出品を元に戻しました（在庫1）"})
            continue
        ack2, errs2, root2 = trading_call_root(
            "RelistFixedPriceItem", f"<Item><ItemID>{r['ebay_item_id']}</ItemID><Quantity>1</Quantity></Item>", token)
        new_id = root2.findtext("e:ItemID", default="", namespaces=NS)
        if ack2 in ("Success", "Warning") and new_id:
            relist_n += 1
            updates.append({"row": r["row"], "status": "active", "ebay_item_id": new_id,
                            "result": f"再出品しました（新しい出品番号 {new_id}）"})
        else:
            fail_n += 1
            updates.append({"row": r["row"], "result": "元に戻せませんでした：" + " / ".join(errs + errs2)[:200]})
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 出品の復元：在庫を戻した {ok_n}件／再出品 {relist_n}件／失敗 {fail_n}件"
    print(msg)
    sheet("log", message=msg)


def trim():
    """同じ機種が4件以上出品されている場合、仕入れが安い3件を残して残りをeBayで終了する（1回だけ実行）"""
    data = sheet("list")
    token = get_access_token()
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] in ("", "active")]
    groups = {}
    for r in rows:
        groups.setdefault(model_key(r.get("query")), []).append(r)
    updates, ended, kept = [], 0, 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(locale="ja-JP", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        for k, rs in groups.items():
            if len(rs) <= MAX_PER_MODEL:
                kept += len(rs)
                continue
            priced = []
            for r in rs:
                try:
                    if is_yahoo(r["mercari_url"]):
                        st, info, _ = check_yahoo(r["mercari_url"])
                    else:
                        st, info = check_mercari(page, r["mercari_url"])
                    price = info.get("price") if st == "on_sale" and info else None
                except Exception:
                    price = None
                if price:
                    priced.append((price, r))
                else:
                    kept += 1   # 値段が確認できないものは触らない（売り切れなら通常の監視で止まる）
            priced.sort(key=lambda t: t[0])
            for i, (price, r) in enumerate(priced):
                if i < MAX_PER_MODEL:
                    kept += 1
                    continue
                ok, msg = stop_on_ebay(r["ebay_item_id"], token)
                if ok:
                    ended += 1
                    updates.append({"row": r["row"], "status": "stopped",
                                    "result": f"同じ機種が多いため終了（仕入れが安い{MAX_PER_MODEL}件を残しました） → {msg}"})
        browser.close()
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 重複機種の整理：残した {kept}件／終了 {ended}件"
    print(msg)
    sheet("log", message=msg)


def exclude_japan():
    """配送ポリシーの発送先から日本を除外する（画面では地域単位でしか選べないため、APIで国単位に設定）"""
    settings = sheet("list").get("settings", {})
    name = str(settings.get("ship_profile") or "").strip()
    base = "https://api.ebay.com/sell/account/v1/fulfillment_policy"
    h = {"Authorization": f"Bearer {get_access_token()}", "Content-Type": "application/json",
         "Content-Language": "en-US", "Accept-Language": "en-US"}
    r = requests.get(base, params={"marketplace_id": "EBAY_US"}, headers=h, timeout=30)
    r.raise_for_status()
    pol = next((x for x in r.json().get("fulfillmentPolicies", []) if x.get("name") == name), None)
    if not pol:
        print(f"配送ポリシー「{name}」が見つかりません")
        return

    def summary(x):
        return {"exclude": [e.get("regionName") for e in (x.get("shipToLocations") or {}).get("regionExcluded", [])],
                "options": [(o.get("optionType"), [(sv.get("shippingServiceCode"), sv.get("freeShipping"),
                             [g.get("regionName") for g in (sv.get("shipToLocations") or {}).get("regionIncluded", [])])
                             for sv in o.get("shippingServices", [])]) for o in x.get("shippingOptions", [])],
                "handling": x.get("handlingTime")}
    print("変更前:", json.dumps(summary(pol), ensure_ascii=False))
    ex = pol.setdefault("shipToLocations", {}).setdefault("regionExcluded", [])
    if any(e.get("regionName") == "JP" for e in ex):
        print("日本はすでに除外されています")
        return
    ex.append({"regionName": "JP", "regionType": "COUNTRY"})
    pid = pol.pop("fulfillmentPolicyId")
    r2 = requests.put(f"{base}/{pid}", headers=h, json=pol, timeout=30)
    print("更新:", r2.status_code, r2.text[:600])
    r3 = requests.get(f"{base}/{pid}", headers=h, timeout=30)
    print("変更後:", json.dumps(summary(r3.json()), ensure_ascii=False))
    sheet("log", message=f"{now()} 配送ポリシーで日本を除外：{'成功' if r2.ok else '失敗'}")


def end_stopped():
    """在庫0で止めたままの出品（売り切れ・本体でない商品など）をeBayで終了する（1回だけ実行）"""
    data = sheet("list")
    token = get_access_token()
    rows = [r for r in data["rows"] if r["ebay_item_id"] and r["status"] == "stopped"
            and "出品を終了しました" not in (r.get("result") or "")]
    updates, ok_n, fail_n = [], 0, 0
    for r in rows:
        ok, msg = stop_on_ebay(r["ebay_item_id"], token)
        if ok:
            ok_n += 1
            updates.append({"row": r["row"], "result": f"{(r.get('result') or '')[:80]} → {msg}"})
        else:
            fail_n += 1
            print("終了できず:", r["ebay_item_id"], msg)
    if updates:
        sheet("update", updates=updates)
    msg = f"{now()} 在庫なしの出品の終了：{ok_n}件／失敗 {fail_n}件"
    print(msg)
    sheet("log", message=msg)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "discover":
            discover()
        elif len(sys.argv) > 1 and sys.argv[1] == "sales":
            sync_sales(get_access_token(), sheet("list").get("settings", {}))   # 売れた直後に管理表から呼ばれる
        elif len(sys.argv) > 1 and sys.argv[1] == "orphans":
            orphans()
        elif len(sys.argv) > 1 and sys.argv[1] == "dupdebug":
            dupdebug(sys.argv[2:])
        elif len(sys.argv) > 1 and sys.argv[1] == "fixdesc":
            fixdesc()
        elif len(sys.argv) > 1 and sys.argv[1] == "redesc":
            fixdesc(end_bad=False)   # 説明文だけ作り直す（出品は終了しない）
        elif len(sys.argv) > 1 and sys.argv[1] == "end":
            tk = get_access_token()
            for iid in sys.argv[2:]:
                print("出品の終了:", iid, stop_on_ebay(iid, tk))
        elif len(sys.argv) > 1 and sys.argv[1] == "feedbackcheck":
            feedbackcheck()
        elif len(sys.argv) > 1 and sys.argv[1] == "thanks":
            thanks(sys.argv[2:])
        elif len(sys.argv) > 1 and sys.argv[1] == "retitle":
            retitle()
        elif len(sys.argv) > 1 and sys.argv[1] == "setsku":
            setsku()
        elif len(sys.argv) > 1 and sys.argv[1] == "excludejp":
            exclude_japan()
        elif len(sys.argv) > 1 and sys.argv[1] == "trim":
            trim()
        elif len(sys.argv) > 1 and sys.argv[1] == "endstopped":
            end_stopped()
        elif len(sys.argv) > 1 and sys.argv[1] == "restore":
            restore()
            fixdesc()
        else:
            main()
    except Exception as e:
        # 通信が一瞬つながらなかっただけのエラーは「失敗」にしない（失敗メールが届かないように）。次の回で自然にやり直される
        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)) or type(e).__name__ == "TimeoutError" \
                or (isinstance(e, RuntimeError) and re.search(r"応答が読めません|\(5\d\d\)|timed out|タイムアウト|too many times|Lock|ロック", str(e))) \
                or "429" in str(e)[:40]:
            print("通信が一時的につながりませんでした。次の回でやり直します:", type(e).__name__)
            try:
                sheet("log", message=f"{now()} 通信エラー（一時的・次の回でやり直し）：{type(e).__name__} {str(e)[:120]}")
            except Exception:
                pass
            sys.exit(0)
        notify(f"【エラー】監視中に問題が起きました: {e}")
        print("エラーが起きました（詳細はスプレッドシートの「設定」シートを確認）:", type(e).__name__)
        try:
            sheet("log", message=f"{now()} エラー：{e}")
        except Exception:
            pass
        sys.exit(1)
