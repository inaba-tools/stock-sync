# -*- coding: utf-8 -*-
"""ヤフオク仕入れ（2026/11/01 追加）と Gemini（無料枠）での状態確認。

- 入札・購入は絶対にしない。シートの更新とメールで知らせるだけ（入札は本人がする）
- ブラウザは使わず、普通のHTTPで __NEXT_DATA__ を読む
- 新しい自動実行は増やさない：仕入れ候補さがし（discover・1本目）と在庫の見張り（終了間際の行だけ）の中で動く
- お金の計算は必ず管理表「設定」シートの数字：
    eBay売値 ×（1 −（eBay手数料B7 ＋ その他手数料B8 ＋ 関税12.5）/100）− 送料B6 − 仕入れ合計
- 参考入札額＝利益「ヤフオクの最低利益」（設定シート。最初は2,000円）が残る上限
- Gemini は GEMINI_API_KEY が無ければ何もしない（キー待ち）。1日の回数は設定シートの上限まで・優先順で配分
"""
import base64
import json
import os
import re
import statistics
import time
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

JST = ZoneInfo("Asia/Tokyo")
PT = ZoneInfo("America/Los_Angeles")   # Gemini の1日の回数は太平洋時間の0時（日本の16〜17時）に戻る
TAB = "ヤフオク入札"
DEVICES = "端末管理"
PARTS = "部品表"
RANKMKT = "状態別相場"
HEADERS = {"User-Agent": "stock-sync/1.0 (+https://github.com/inaba-tools/stock-sync)"}
DUTY = 12.5
ACTIVE = ("検討中", "入札中", "上回られた")
MAX_NEW = 20   # 1回に追加する新しい候補の上限（メールが多くなりすぎないように）
COL = {k: i for i, k in enumerate(["状態", "リンク", "用途", "ジャンル", "機種", "色", "商品の状態", "現在価格", "送料", "合計",
                                   "入札数", "終了日時", "参考入札額", "自分の最高入札額", "eBay相場", "利益今", "利益上限",
                                   "メモ", "最終更新", "ID"], start=1)}
M_FORMULA = ('=IF(T{r}="","",IF(C{r}<>"在庫","AI未判定",IF(O{r}="","相場不足",FLOOR(O{r}*(1-(VALUE(\'設定\'!$B$7)'
             '+VALUE(\'設定\'!$B$8)+12.5)/100)-VALUE(\'設定\'!$B$6)-N(YAHOO_MIN_PROFIT)-N(I{r}),10))))')
P_FORMULA = ('=IF(OR(O{r}="",J{r}=""),"",ROUND(O{r}*(1-(VALUE(\'設定\'!$B$7)+VALUE(\'設定\'!$B$8)+12.5)/100)'
             '-VALUE(\'設定\'!$B$6)-J{r}))')
Q_FORMULA = ('=IF(OR(O{r}="",N{r}=""),"",ROUND(O{r}*(1-(VALUE(\'設定\'!$B$7)+VALUE(\'設定\'!$B$8)+12.5)/100)'
             '-VALUE(\'設定\'!$B$6)-N{r}-N(I{r})))')

# ヤフオクの「商品の状態」→ 状態ランク（AIを使わずに決める）
COND_RANK = {"NEW": "S", "USED10": "S", "USED20": "A", "USED40": "B", "USED60": "C", "USED80": "C"}
COND_NAME = {"NEW": "新品", "USED10": "未使用に近い", "USED20": "目立った傷や汚れなし", "USED40": "やや傷や汚れあり",
             "USED60": "傷や汚れあり", "USED80": "全体的に状態が悪い"}
JUNK_WORD = re.compile(r"ジャン[クグ]|部品取り|パーツ取り|訳あり|訳アリ|ワケアリ|故障|不良|割れ|充電(でき|出来)ない|"
                       r"充電(が|は)?(しない|されない|不可)|画面.{0,4}(つかない|点かない|映らない)|液晶.{0,4}(つかない|点かない|映らない)")
NG_ALWAYS = re.compile(r"水没|電源(が|は)?(入らない|入りません|つかない|つきません)|起動(しない|しません)|赤ロム|"
                       r"利用制限\s*[×✕xX△]|ネットワーク(利用)?制限\s*[×✕xX△]|判定\s*[×✕xX△]|再起動(を)?繰り返|基板|文鎮|"
                       r"ロック(解除)?不可|暗証番号(不明|わからない)|初期化(でき|出来)ない")
NG_UNLESS_REPAIRABLE = re.compile(r"充電(でき|出来)ない|充電(が|は)?(しない|されない|不可)|充電口")   # ピンクだけ直してOK
NOT_ITEM = re.compile(r"ケースのみ|カバーのみ|充電器のみ|箱のみ|説明書のみ|モック|ダミー|電池パックのみ|バッテリーのみ|"
                      r"フィルム|保護|互換|部品のみ|パーツのみ|基板のみ|外装のみ|ジャケット|シール")
PINK_MODELS = ("902KC", "903KC")


def now_s():
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- シート（Apps Scriptの汎用の読み書き）
class Tabs:
    def __init__(self, M):
        self.M = M
        self.cache = {}

    def get(self, name, fresh=False):
        if fresh or name not in self.cache:
            self.cache[name] = self.M.sheet("tab_get", name=name).get("values") or []
        return self.cache[name]

    def set(self, name, cells):
        """cells: [(行, 列, 値)]（1から数える）。値が "=" で始まれば数式"""
        for i in range(0, len(cells), 200):
            self.M.sheet("tab_set", name=name, cells=[[r, c, v] for r, c, v in cells[i:i + 200]])

    def append(self, name, rows):
        if rows:
            return self.M.sheet("tab_append", name=name, rows=rows).get("first_row")

    def ensure(self, name, header):
        self.M.sheet("tab_ensure", name=name, header=header)


def setting_map(values):
    """設定シートの「見出し → 右のセル」。シートの行の位置に頼らない"""
    out = {}
    for r, row in enumerate(values, start=1):
        for c in range(len(row) - 1):
            k = str(row[c]).strip()
            if k and k not in out:
                out[k] = (row[c + 1], r, c + 2)
    return out


def sget(smap, prefix, default=None, num=True):
    for k, (v, _, _) in smap.items():
        if k.startswith(prefix):
            if v in (None, ""):
                return default
            if not num:
                return v
            try:
                return float(str(v).replace(",", ""))
            except ValueError:
                return default
    return default


class Calc:
    """設定シートの数字だけで利益・上限を出す（手計算の数字は使わない）"""

    def __init__(self, settings, smap):
        self.ship = float(settings.get("ship_jpy") or sget(smap, "送料（円）"))
        self.fee = float(settings.get("fee_pct") or sget(smap, "eBay手数料"))
        self.other = float(settings.get("other_pct") or sget(smap, "その他の手数料"))
        self.min_profit = sget(smap, "ヤフオクの最低利益", 2000)
        self.parts_default = sget(smap, "ジャンクの部品代の目安", 3000)
        self.adj = float(settings.get("market_adj") or 80) / 100
        self.rate = 1 - (self.fee + self.other + DUTY) / 100

    def net(self, sale_jpy):
        return sale_jpy * self.rate - self.ship

    def profit(self, sale_jpy, total_cost):
        return round(self.net(sale_jpy) - total_cost)

    def cap(self, sale_jpy, shipping, parts=0):
        """利益が最低利益だけ残る、ヤフオクの価格（税込）の上限"""
        return int((self.net(sale_jpy) - self.min_profit - shipping - parts) // 10 * 10)


# ---------------------------------------------------------------- ヤフオクを読む
def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None


def y_search(keyword, page=1):
    """ヤフオクの検索（新着順・オークションと即決の両方）。検索結果の価格・入札数・送料・終了日時を返す"""
    r = requests.get("https://auctions.yahoo.co.jp/search/search",
                     params={"p": keyword, "n": 100, "b": 1 + (page - 1) * 100, "s1": "new", "o1": "d"},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    d = next_data(r.text)
    if not d:
        return []
    try:
        items = d["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]["items"]
    except (KeyError, TypeError):
        return []
    out = []
    for it in items or []:
        try:
            if it.get("isFreeShipping"):
                ship = 0
            elif (it.get("currentPriceShippingFee") or 0) > 0:
                ship = int(it["currentPriceShippingFee"])
            else:
                ship = None   # 送料が検索結果に出ない（着払い・落札後に連絡など）
            out.append({
                "id": it["auctionId"], "title": it.get("title") or "", "price": int(it.get("price") or 0),
                "buynow": int(it.get("buyNowPrice") or 0), "fixed": bool(it.get("isFixedPrice")),
                "bids": int(it.get("bidCount") or 0), "end": it.get("endTime") or "", "ship": ship,
                "cond": it.get("itemCondition") or "", "seller": (it.get("seller") or {}).get("userId") or "",
                "store": bool((it.get("seller") or {}).get("isStore")), "image": it.get("imageUrl") or "",
                "cat": " > ".join(str(c.get("name") or "") for c in (it.get("categoryPath") or []) if isinstance(c, dict)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def y_item(aid):
    """出品ページ（終了間際・終了の確認・AIの確認のときだけ開く）"""
    r = requests.get(f"https://auctions.yahoo.co.jp/jp/auction/{aid}", headers=HEADERS, timeout=30)
    if r.status_code == 404:
        return {"id": aid, "closed": True, "gone": True}
    r.raise_for_status()
    d = next_data(r.text)

    def find(o):
        if isinstance(o, dict):
            if "taxinPrice" in o or ("auctionId" in o and "bids" in o):
                return o
            for v in o.values():
                x = find(v)
                if x:
                    return x
        elif isinstance(o, list):
            for v in o:
                x = find(v)
                if x:
                    return x
        return None

    it = find(d) if d else None
    if not it:
        return None
    ship = 0 if it.get("chargeForShipping") == "seller" else None
    for m in ((it.get("shipping") or {}).get("methods") or []):
        try:
            fee = m.get("shippingFee") if isinstance(m, dict) else None
            fee = fee if fee is not None else (m.get("price") if isinstance(m, dict) else None)
            if fee is not None and (ship is None or int(fee) < ship):
                ship = int(fee)
        except (TypeError, ValueError):
            pass
    end = it.get("endTime") or ""
    try:
        ended = datetime.fromisoformat(end) <= datetime.now(JST)
    except ValueError:
        ended = False
    desc = it.get("description") or []
    return {
        "id": aid, "title": it.get("title") or "", "price": int(it.get("taxinPrice") or it.get("price") or 0),
        "buynow": int(it.get("taxinBidorbuy") or it.get("bidorbuy") or 0), "bids": int(it.get("bids") or 0),
        "end": end, "ship": ship, "cond_name": it.get("conditionName") or "",
        "closed": (it.get("status") not in (None, "open")) or ended,
        "store": bool((it.get("seller") or {}).get("isStore")),
        "seller": (it.get("seller") or {}).get("aucUserId") or "",
        "images": [x.get("image") for x in (it.get("img") or []) if isinstance(x, dict) and x.get("image")][:8],
        "description": "\n".join(desc) if isinstance(desc, list) else str(desc),
    }


# ---------------------------------------------------------------- 色・機種
COLOR_JP = [("ピンク", "Pink"), ("シルバー", "Silver"), ("ブラック", "Black"), ("ホワイト", "White"), ("ブルー", "Blue"),
            ("レッド", "Red"), ("ゴールド", "Gold"), ("グリーン", "Green"), ("ネイビー", "Navy"), ("パープル", "Purple"),
            ("オレンジ", "Orange"), ("イエロー", "Yellow"), ("グレー", "Gray"), ("ブラウン", "Brown")]
COLOR_RE = {"ピンク": r"ピンク|PINK|ローズ|桃", "シルバー": r"シルバー|SILVER|銀", "ブラック": r"ブラック|BLACK|黒",
            "ホワイト": r"ホワイト|WHITE|(?<!ロム)白(?!ロム)", "ブルー": r"ブルー|BLUE|青|水色", "レッド": r"レッド|RED|赤|ワイン|ボルドー",
            "ゴールド": r"ゴールド|GOLD|金|シャンパン", "グリーン": r"グリーン|GREEN|緑", "ネイビー": r"ネイビー|NAVY|紺",
            "パープル": r"パープル|PURPLE|紫", "オレンジ": r"オレンジ|ORANGE", "イエロー": r"イエロー|YELLOW|黄",
            "グレー": r"グレー|GRAY|GREY", "ブラウン": r"ブラウン|BROWN|茶"}


def color_of(title):
    t = unicodedata.normalize("NFKC", title or "").upper().replace("白ロム", "").replace("黒ロム", "")
    found = [jp for jp, _ in COLOR_JP if re.search(COLOR_RE[jp], t)]
    return found[0] if len(found) == 1 else ("" if not found else "複数色")


def color_en(jp):
    return dict(COLOR_JP).get(jp, "")


def market_model(model):
    """902KC と 903KC は eBay でまとめて売られている（同じ相場）"""
    return "902KC" if model.upper() in PINK_MODELS else model


# ---------------------------------------------------------------- eBay 相場（状態・色ごと）
_mkt = {}
_mkt_items = {}   # (機種, 色) → [(eBayの商品ID, 円, タイトル, 写真)]（週1回のAIのランク判定に使う）


def ebay_cond_market(M, model, color, settings):
    """売れた値段は公式APIで自由に取れないため、日本から出品中（中国などの安い・高い出品を除く）の価格×補正を使う。
    戻り値 {"安い","ふつう","高い","件数","根拠"}（円・補正後）。件数が5件未満なら None（相場不足）"""
    key = (market_model(model), color)
    if key in _mkt:
        return _mkt[key]
    gi = M.game_info(model) or {}
    q = gi.get("ebay_q", market_model(model)) + (" " + color_en(color) if color_en(color) else "")
    cat = gi.get("ebay_cat", M.GAME_EBAY_CATEGORY) if gi else "9355"
    res = None
    try:
        r = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
                         params={"q": q, "category_ids": cat, "limit": 100,
                                 "filter": "buyingOptions:{FIXED_PRICE},conditions:{USED},itemLocationCountry:JP"},
                         headers={"Authorization": f"Bearer {M.app_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
                         timeout=30)
        r.raise_for_status()
        fx = M.usd_jpy(settings)
        prices, keep = [], []
        for it in r.json().get("itemSummaries", []):
            if (it.get("seller", {}).get("username") or "").lower() == M.my_seller():
                continue
            t = (it.get("title") or "").upper()
            if not M.has_model(t, market_model(model)) and not (gi and M.match_game(t, model)):
                continue
            if re.search(r"PARTS|NOT WORKING|JUNK|AS IS|BROKEN|FOR REPAIR|CASE ONLY|BOX ONLY|CHARGER", t):
                continue
            if color and color_en(color) and color_en(color).upper() not in t:
                continue
            try:
                p = float(it["price"]["value"])
                s = it.get("shippingOptions", [{}])[0].get("shippingCost", {}).get("value")
                prices.append((p + (float(s) if s else 0)) * fx * float(settings.get("market_adj") or 80) / 100)
                keep.append((it.get("itemId"), prices[-1], it.get("title") or "", (it.get("image") or {}).get("imageUrl", "")))
            except (KeyError, ValueError, IndexError):
                continue
        if len(prices) >= 5:
            prices.sort()
            qs = statistics.quantiles(prices, n=4)
            res = {"安い": round(qs[0]), "ふつう": round(statistics.median(prices)), "高い": round(qs[2]),
                   "件数": len(prices), "根拠": "日本から出品中×補正"}
        _mkt_items[key] = keep
    except Exception as e:
        print("eBay相場（状態別）の取得に失敗:", str(e)[:100])
    _mkt[key] = res
    return res


RANK_LEVEL = {"S": "高い", "A": "ふつう", "B": "安い", "C": "安い"}


def sale_for(M, tabs, model, color, rank, settings):
    """(円, 根拠) 。①売上シートの自分の実績 ②状態別相場シート（AIで判定した分）③日本から出品中×補正。足りなければ (None, 理由)"""
    mm = market_model(model)
    # ① 自分の実績（売上シート：B機種・C商品名・F売れた値段(円)・W状態）
    try:
        vals = tabs.get("売上")
        hit = []
        for row in vals[5:]:
            if len(row) > 22 and M.has_model(str(row[1]) + " " + str(row[2]), mm) and str(row[22]).strip().upper() == rank:
                if not color or color_of(str(row[2])) == color:
                    try:
                        hit.append(float(str(row[5]).replace(",", "")))
                    except ValueError:
                        pass
        if hit:
            return statistics.median(hit), f"自分の実績{len(hit)}件"
    except Exception:
        pass
    # ② 状態別相場シート（機種・色・ランク・安い・ふつう・高い・件数・最終更新・根拠）
    try:
        for row in tabs.get(RANKMKT)[1:]:
            if len(row) >= 7 and str(row[0]) == mm and str(row[1]) == (color or "") and str(row[2]) == rank \
                    and int(float(row[6] or 0)) >= 3:
                return float(row[4]), f"状態別相場{rank}（{row[6]}件）"
    except Exception:
        pass
    # ③ 日本から出品中×補正（ランクごとの区別はまだ無いので、Sは高め・Aは真ん中・B/Cは安めの値段を使う）
    mk = ebay_cond_market(M, model, color, settings)
    if not mk:
        return None, "相場不足"
    return mk[RANK_LEVEL.get(rank, "安い")], f"出品中×補正の{RANK_LEVEL.get(rank, '安い')}側（{mk['件数']}件）"


# ---------------------------------------------------------------- Gemini（無料枠のみ）
TIER_SHARE = {1: 1.0, 2: 0.85, 3: 0.6, 4: 0.4}   # ①出品直前 ②ヤフオク・ジャンク ③週1の相場 ④出品中の確認


class Gemini:
    def __init__(self, tabs):
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.tabs = tabs
        self.loaded = False
        self.used = 0
        self.cap = 400
        self.model = "gemini-flash-lite-latest"
        self.cell = None
        self.blocked = False

    def _load(self):
        if self.loaded:
            return
        self.loaded = True
        smap = setting_map(self.tabs.get("設定"))
        self.cap = int(sget(smap, "Geminiの1日の上限", 400))
        self.model = str(sget(smap, "Geminiのモデル", "gemini-flash-lite-latest", num=False)).strip()
        today = datetime.now(PT).strftime("%Y-%m-%d")
        if "Gemini使用回数" in "".join(smap.keys()):
            for k, (v, r, c) in smap.items():
                if k.startswith("Gemini使用回数"):
                    self.cell = (r, c)
                    d, _, n = str(v or "").partition(":")
                    self.used = int(n) if d == today and n.isdigit() else 0

    def available(self, tier):
        if not self.key or self.blocked:
            return False
        self._load()
        return self.used < self.cap * TIER_SHARE.get(tier, 0.4)

    def _count(self):
        self.used += 1
        if self.cell:
            try:
                self.tabs.set("設定", [(self.cell[0], self.cell[1], f"{datetime.now(PT).strftime('%Y-%m-%d')}:{self.used}")])
            except Exception:
                pass

    def ask(self, tier, prompt, image_urls=()):
        """JSONで答えをもらう。使えない・上限なら None"""
        if not self.available(tier):
            return None
        parts = [{"text": prompt}]
        for u in list(image_urls)[:5]:
            try:
                b = requests.get(u, headers=HEADERS, timeout=20)
                if b.ok and len(b.content) < 4_000_000:
                    parts.append({"inline_data": {"mime_type": b.headers.get("Content-Type", "image/jpeg").split(";")[0],
                                                  "data": base64.b64encode(b.content).decode()}})
            except requests.RequestException:
                pass
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                              headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
                              json={"contents": [{"parts": parts}],
                                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}},
                              timeout=90)
        except requests.RequestException as e:
            print("Geminiにつながりませんでした:", type(e).__name__)
            return None
        self._count()
        if r.status_code == 429:
            print("Geminiの無料枠（1日の回数）を使い切りました。残りは明日に回します")
            self.blocked = True
            return None
        if r.status_code in (401, 403):
            print("GeminiのAPIキーが使えません（", r.status_code, "）")
            self.blocked = True
            return None
        if not r.ok:
            print("Geminiのエラー:", r.status_code, r.text[:150])
            return None
        try:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()))
        except (KeyError, IndexError, ValueError):
            return None


JUNK_PROMPT = """あなたは中古ガラケー・携帯ゲーム機の修理と輸出販売のプロです。日本のオークション出品（{model}・タイトルの色「{color}」）の写真と説明文を見て、JSONだけで答えてください。
タイトル：{title}
説明文：{desc}
答えの形：
{{"color_in_photo":"写真の本体の色（日本語）","color_matches_title":true/false,"exterior_replaced":true/false（外装交換品の記載や形跡）,
"broken":["壊れている部分（液晶/充電口/外装割れ/ヒンジ/ボタン/バッテリー/基板/電源 など）"],"alive_parts":["生きていそうな部品（液晶/充電口/基板/外装/バッテリー/キー など）"],
"exterior_rank":"S/A/B/C（外装のキズ・色あせ・割れから。直らない部分で決める）","back_damage":"背面の状態を短く",
"ng":true/false（水没・電源が入らない・赤ロム/利用制限・再起動を繰り返す等の基板故障っぽい・本体が写っていない のどれか）,
"ng_reason":"短く","needed_parts":["直すのに要る部品"],"note":"気になる点を30字以内"}}"""

LISTING_PROMPT = """中古品をeBayに出品する前の確認です。日本の出品（{model}）の写真と説明文を見て、JSONだけで答えてください。
タイトル：{title}
商品の状態（出品者の申告）：{cond}
説明文：{desc}
答えの形：{{"ok":true/false,"reason":"出品しない理由（ok=falseのとき・30字以内）","rank":"S/A/B/C/ジャンク",
"color_in_photo":"写真の本体の色","note":"気になる点（30字以内）"}}
ok=false にするのは：状態の申告と写真が明らかに違う／タイトルと写真の色が違う／背面などに大きなダメージ／本体が写っていない・本体なしに見える／ジャンク・故障の記載 のどれか。"""

RANK_PROMPT = """eBayで売られている中古品（{model}）の写真と説明文から、状態ランクをJSONだけで答えてください。
S=新品同様、A=キズほぼなし、B=使用感・小キズ、C=目立つキズ・色あせ、ジャンク=故障。
タイトル：{title}
答えの形：{{"rank":"S/A/B/C/ジャンク","color":"本体の色（英語）"}}"""


# ---------------------------------------------------------------- ジャンクの上限（出品ごと）
def parts_cost(tabs, model, parts, calc):
    """部品表（機種・部品・値段）にあればその値段、無ければ部品取りジャンク1台分の目安（仮）"""
    price, note = 0, ""
    table = {}
    try:
        for row in tabs.get(PARTS)[1:]:
            if len(row) >= 4 and str(row[3]).strip():
                try:
                    table[(market_model(str(row[1])), str(row[2]).strip())] = float(str(row[3]).replace(",", ""))
                except ValueError:
                    pass
    except Exception:
        pass
    missing = []
    for p in parts or []:
        v = table.get((market_model(model), p))
        if v is None:
            missing.append(p)
        else:
            price += v
    if missing:
        price += calc.parts_default
        note = "部品代は仮"
    return price, note


def junk_cap(M, tabs, gem, calc, settings, it, model, color, use):
    """ジャンク1件の上限（M列）とメモ。戻り値 (上限 or None, メモ, 判定済みか)"""
    detail = y_item(it["id"]) if not it.get("images") else it
    if not detail:
        return None, "出品ページが読めず", False
    ans = gem.ask(2, JUNK_PROMPT.format(model=model, color=color or "不明", title=detail["title"],
                                        desc=(detail.get("description") or "")[:1500]), detail.get("images", [])[:4])
    if ans is None:
        return None, "AI未判定（" + ("キー待ち" if not gem.key else "今日の回数を使い切り・翌日") + "）", False
    if ans.get("ng"):
        return None, f"NG：{ans.get('ng_reason', '')[:30]}", True
    if not ans.get("color_matches_title", True):
        return None, f"色違い（写真は{ans.get('color_in_photo', '')}）", True
    if ans.get("exterior_replaced"):
        note_ext = "外装交換品／"
    else:
        note_ext = ""
    rank = str(ans.get("exterior_rank") or "C").upper()[:1]
    if rank not in "SABC":
        rank = "C"
    broken = ans.get("broken") or []
    pink = color == "ピンク" and model.upper() in PINK_MODELS
    ok_fix = {"液晶", "外装割れ", "充電口"} if pink else {"液晶", "外装割れ"}
    if use == "部品取り":
        # 生きている部品で直せる相手の1台（端末管理の「部品待ち」）が直ったときの利益から上限を出す
        target = None
        try:
            for row in tabs.get(DEVICES)[1:]:
                if len(row) >= 9 and "待ち" in str(row[7]) or (len(row) >= 9 and "交換中" in str(row[7])):
                    need = str(row[8])
                    if market_model(str(row[1])) == market_model(model) and any(need and need in a for a in ans.get("alive_parts") or []):
                        target = row
                        break
        except Exception:
            target = None
        if not target:
            return None, "部品取り：直せる相手の端末が無い", True
        t_rank = str(target[5] or "B").upper()[:1] if len(target) > 5 else "B"
        sale, why = sale_for(M, tabs, str(target[1]), str(target[2]), t_rank, settings)
        if not sale:
            return None, "相場不足", True
        try:
            t_cost = float(str(target[6]).replace(",", "")) if len(target) > 6 and str(target[6]).strip() else 0
        except ValueError:
            t_cost = 0
        cap = calc.cap(sale, it.get("ship") or 0, t_cost)
        return cap, (f"部品取り→{target[0]}（{target[1]} {target[2]}）を直す／{t_rank}相場{int(sale):,}円（{why}）"
                     f"−相手の仕入{int(t_cost):,}円／上限{cap:,}円"), True
    if any(not any(f in b for f in ok_fix) for b in broken if b):
        return None, "直す範囲外の故障：" + "・".join(broken)[:30], True
    need = ans.get("needed_parts") or broken
    pc, pnote = parts_cost(tabs, model, need, calc)
    sale, why = sale_for(M, tabs, model, color, rank, settings)
    if not sale:
        return None, "相場不足", True
    cap = calc.cap(sale, it.get("ship") or 0, pc)
    memo = (f"{note_ext}{'・'.join(broken) or '故障'}→部品{int(pc):,}円{('（' + pnote + '）') if pnote else ''}／"
            f"外装{rank}→{rank}相場{int(sale):,}円（{why}）／上限{cap:,}円")
    return cap, memo, True


# ---------------------------------------------------------------- メイン
def keywords_for(M, settings):
    kws = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "")) if k.strip()]
    out = []
    for k in kws:
        gi = M.game_info(k)
        if gi is None:
            if re.search(r"\d{3}(KC|SH)$|^(KY|SH|P|F|N|SO|CA)-?\d|^KYF|^A\d{3}", k, re.I):
                out.append((k, "ガラケー", k))
        elif not gi.get("kind"):
            out.append((k, "ゲーム機", gi.get("yahoo_q", k)))
    return out


def to_dt(v):
    s = str(v or "").strip().replace("/", "-")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(s[:25] if "T" in s else s[:19], f)
            return d if d.tzinfo else d.replace(tzinfo=JST)
        except ValueError:
            continue
    return None


def fmt_end(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(JST).strftime("%Y/%m/%d %H:%M")
    except ValueError:
        return ""


def num(v):
    try:
        return float(str(v).replace(",", "").replace("円", "").strip())
    except ValueError:
        return None


def due(row, now_dt, soon_only):
    """更新する時期か（終了まで1日以上：4時間ごと／2時間〜1日：1時間ごと／2時間未満：30分ごと）"""
    end = to_dt(row[COL["終了日時"] - 1]) if len(row) >= COL["終了日時"] else None
    last = to_dt(row[COL["最終更新"] - 1]) if len(row) >= COL["最終更新"] else None
    if not end:
        return not soon_only
    left = (end - now_dt).total_seconds() / 3600
    if soon_only:
        return left < 2.0
    gap = 4 if left >= 24 else (1 if left >= 2 else 0.4)
    return last is None or (now_dt - last).total_seconds() / 3600 >= gap - 0.05


def run(M, data, mode="discover"):
    """mode="discover"：行の更新＋新しい出品さがし（1時間ごとの回の1本目）／"soon"：終了2時間前の行だけ（30分ごとの見張りの回）"""
    tabs = Tabs(M)
    settings = data.get("settings", {})
    smap = setting_map(tabs.get("設定"))
    calc = Calc(settings, smap)
    gem = Gemini(tabs)
    vals = tabs.get(TAB)
    now_dt = datetime.now(JST)
    cells, mails_outbid, mails_soon, ended_bid, notes = [], [], [], [], []
    known = {str(r[COL["ID"] - 1]).strip() for r in vals[1:] if len(r) >= COL["ID"]}
    found_in_search = {}

    # ---- 新しい出品をさがす（discoverの回だけ。3時間で全キーワードを1周）
    new_rows = []
    if mode == "discover":
        kws = keywords_for(M, settings)
        hour = now_dt.hour
        part = [k for i, k in enumerate(kws) if i % 3 == hour % 3]
        pink_q = [("902KC ピンク", "ガラケー", "902KC"), ("903KC ピンク", "ガラケー", "903KC"),
                  ("DIGNOケータイ3 ピンク", "ガラケー", "903KC")]
        for kw, genre, q in [(q, g, k) for k, g, q in part] + pink_q:
            try:
                items = y_search(q)
            except Exception as e:
                print("ヤフオクの検索に失敗:", q, type(e).__name__)
                continue
            time.sleep(1.5)
            for it in items:
                found_in_search[it["id"]] = it
                if len(new_rows) >= MAX_NEW:
                    continue
                if it["id"] in known or any(it["id"] == r[1] for r in new_rows):
                    continue
                cand = consider(M, tabs, gem, calc, settings, it, kw, genre)
                if cand:
                    new_rows.append((cand, it["id"]))
                    known.add(it["id"])

    # ---- 今ある行の更新（見送り・負け・終了・落札・購入済みは触らない）
    for i, row in enumerate(vals[1:], start=2):
        row = list(row) + [""] * (20 - len(row))
        st, aid = str(row[0]).strip(), str(row[COL["ID"] - 1]).strip()
        if st not in ACTIVE or not aid:
            continue
        info = None
        if aid in found_in_search and not (mode == "soon"):
            s = found_in_search[aid]
            end = to_dt(fmt_end(s["end"]))
            if end and (end - now_dt).total_seconds() > 7200:   # 終了間際でなければ検索結果の数字で十分
                info = {"price": s["price"], "bids": s["bids"], "end": s["end"], "ship": s["ship"], "closed": False}
        if info is None:
            if not due(row, now_dt, mode == "soon"):
                continue
            try:
                info = y_item(aid)
            except Exception as e:
                print("出品ページを読めませんでした:", aid, type(e).__name__)
                continue
            time.sleep(1)
        if not info:
            continue
        upd = [(i, COL["最終更新"], now_s())]
        if info.get("price"):
            upd.append((i, COL["現在価格"], info["price"]))
        if info.get("bids") is not None and not info.get("gone"):
            upd.append((i, COL["入札数"], info["bids"]))
        if info.get("ship") is not None:
            upd.append((i, COL["送料"], info["ship"]))
        if info.get("end"):
            upd.append((i, COL["終了日時"], fmt_end(info["end"])))
        price = info.get("price") or num(row[COL["現在価格"] - 1]) or 0
        mymax = num(row[COL["自分の最高入札額"] - 1])
        cap = num(row[COL["参考入札額"] - 1])
        title = f"{row[COL['機種'] - 1]} {row[COL['色'] - 1]} {row[COL['商品の状態'] - 1]}"[:40]
        link = f"https://auctions.yahoo.co.jp/jp/auction/{aid}"
        if info.get("closed"):
            if st in ("入札中", "上回られた"):
                ended_bid.append((i, aid, row, price, mymax))
            else:
                upd.append((i, 1, "終了"))
        else:
            if mymax and price > mymax and st != "上回られた":
                upd.append((i, 1, "上回られた"))
                if cap and cap > price:
                    adv = f"参考入札額まであと{int(cap - price):,}円上げられます（参考入札額 {int(cap):,}円）"
                else:
                    adv = "参考入札額を超えたので見送り推奨"
                mails_outbid.append(f"・{title}\n  今 {int(price):,}円（自分の上限 {int(mymax):,}円）→ {adv}\n  {link}")
            end = to_dt(fmt_end(info.get("end") or "")) or to_dt(row[COL["終了日時"] - 1])
            if st in ("入札中", "上回られた") and end and 0 < (end - now_dt).total_seconds() <= 40 * 60:
                memo = str(row[COL["メモ"] - 1])
                if "30分前メール済" not in memo:
                    win = "勝っています" if (mymax and price <= mymax) else "負けています"
                    mails_soon.append(f"・{title}：今 {int(price):,}円で{win}（自分の上限 {int(mymax or 0):,}円・"
                                      f"参考 {int(cap) if cap else '-'}円）終了 {end.strftime('%H:%M')}\n  {link}")
                    upd.append((i, COL["メモ"], (memo + " 30分前メール済").strip()))
        cells += upd

    # ---- 入札していた出品が終わった：落札メール（Gmail）を見て「落札」か「負け」に
    won = []
    for i, aid, row, price, mymax in ended_bid:
        res = {}
        try:
            res = M.sheet("gmail_find", q=f'"{aid}" newer_than:7d', max=5)
        except Exception as e:
            print("Gmailを確認できませんでした:", str(e)[:80])
        hits = [m for m in (res.get("messages") or []) if "落札" in (m.get("subject") or "")
                and not re.search(r"できません|されません|残念|キャンセル|削除", m.get("subject") or "")]
        if hits:
            cells.append((i, 1, "落札"))
            won.append((i, aid, row, price, hits[0]))
        elif mymax and price > mymax:
            cells.append((i, 1, "負け"))
        else:
            end = to_dt(row[COL["終了日時"] - 1])
            if end and (now_dt - end).total_seconds() > 24 * 3600:
                cells.append((i, 1, "終了"))
                cells.append((i, COL["メモ"], (str(row[COL["メモ"] - 1]) + " 落札メールが見つからず").strip()))

    if cells:
        tabs.set(TAB, cells)
    for i, aid, row, price, mail in won:
        record_win(M, tabs, row, aid, price, mail)

    # ---- 新しい候補を追加
    if new_rows:
        start = len(vals) + 1
        rows = []
        for k, (c, aid) in enumerate(new_rows):
            r = start + k
            rows.append([c["状態"], f'=HYPERLINK("https://auctions.yahoo.co.jp/jp/auction/"&T{r},"開く")', c["用途"],
                         c["ジャンル"], c["機種"], c["色"], c["商品の状態"], c["現在価格"], c["送料"],
                         f'=IF(H{r}="","",H{r}+N(I{r}))', c["入札数"], c["終了日時"],
                         c["上限"] if c.get("上限") is not None else M_FORMULA.format(r=r), "", c.get("相場", ""),
                         P_FORMULA.format(r=r), Q_FORMULA.format(r=r), c["メモ"], now_s(), aid])
        tabs.append(TAB, rows)
        # 同じ出品者の候補が複数 → 同梱で送料が安くなるかも
        by_seller = {}
        for c, aid in new_rows:
            if c.get("seller"):
                by_seller.setdefault(c["seller"], []).append(aid)
        notes = [f"同じ出品者：{', '.join(v)}（同梱で送料が安くなるかも）" for v in by_seller.values() if len(v) > 1]

    # ---- メール（「上回られた」と「終了30分前」は1通にまとめる）
    body = []
    if mails_outbid:
        body.append("【上回られた】\n" + "\n".join(mails_outbid))
    if mails_soon:
        body.append("【まもなく終了（30分前）】\n" + "\n".join(mails_soon))
    if body:
        M.alert("【ヤフオク】" + ("上回られました" if mails_outbid else "まもなく終了"), "\n\n".join(body)
                + "\n\n入札はご自身で判断してください（自動入札はしません）")
    if new_rows:
        lines = []
        for c, aid in new_rows:
            capt = f"参考入札額 {int(c['上限']):,}円" if isinstance(c.get("上限"), (int, float)) else (
                "参考入札額はシートで確認" if c["用途"] == "在庫" else "参考入札額：AI未判定")
            lines.append(f"・{c['機種']} {c['色']} {c['商品の状態']}（{c['用途']}）今 {c['現在価格']:,}円＋送料{c['送料'] if c['送料'] != '' else '不明'}"
                         f" → {capt}\n  {c['メモ'][:60]}\n  https://auctions.yahoo.co.jp/jp/auction/{aid}")
        M.alert(f"【ヤフオク】新しい候補 {len(new_rows)}件", "\n".join(lines) + ("\n\n" + "\n".join(notes) if notes else "")
                + "\n\n「ヤフオク入札」シートで参考入札額を見て、入札はご自身で。")
    if won:
        M.alert(f"【ヤフオク】落札 {len(won)}件", "\n".join(f"・{r[COL['機種'] - 1]} {r[COL['色'] - 1]} {int(p):,}円 "
                                                          f"https://auctions.yahoo.co.jp/jp/auction/{a}" for _, a, r, p, _ in won)
                + "\n\n古物台帳と端末管理に記録しました。")
    msg = (f"{now_s()} ヤフオク（{mode}）：行の更新{len({c[0] for c in cells})}件／新しい候補{len(new_rows)}件／"
           f"上回られた{len(mails_outbid)}件／落札{len(won)}件／Gemini {'キー待ち' if not gem.key else f'今日{gem.used}回'}")
    print(msg)
    try:
        M.sheet("log", message=msg)
    except Exception:
        pass
    return gem


def consider(M, tabs, gem, calc, settings, it, kw, genre):
    """検索結果1件 → 候補にするなら行の中身（dict）、しないなら None"""
    t = unicodedata.normalize("NFKC", it["title"])
    model = kw
    if genre == "ガラケー":
        if not M.has_model(t, kw):
            return None
    elif not M.match_game(t, kw):
        return None
    if NOT_ITEM.search(t) or M.reserved_reason(t) or M.no_body_reason(t):
        return None
    # 本体のカテゴリだけ（ケース・フィルムなどの「新品・1,000円」が候補に入らないように）
    cat = it.get("cat") or ""
    if genre == "ガラケー" and "携帯電話本体" not in cat:
        return None
    if genre == "ゲーム機" and re.search(r"ソフト|周辺機器|アクセサリ|ケース|カバー|バッテリー|充電器|攻略", cat):
        return None
    # オークションは終了まで2日以内のものだけ（始まったばかりの1円などは値段の意味がない）。即決はいつでも
    if not it["fixed"]:
        try:
            if (datetime.fromisoformat(it["end"]) - datetime.now(JST)).total_seconds() > 48 * 3600:
                return None
        except ValueError:
            return None
    if M.looks_like_accessory(t) and not any(w in t for w in M.BODY_WORDS):
        return None
    color = color_of(t)
    if color == "複数色":
        return None
    junk = bool(JUNK_WORD.search(t)) or it["cond"] == "USED80"
    pink = color == "ピンク" and model.upper() in PINK_MODELS
    ship = it["ship"]
    ship_v = ship if ship is not None else 1000   # 送料が分からないときは1,000円と見て計算（メモに書く）
    price = it["buynow"] if it["fixed"] and it["buynow"] else it["price"]
    end = fmt_end(it["end"])
    base = {"状態": "検討中", "ジャンル": genre, "機種": model, "色": color, "現在価格": price,
            "送料": ship if ship is not None else "", "入札数": it["bids"], "終了日時": end, "seller": it["seller"]}
    if not junk:
        if not it["cond"]:
            return None
        rank = COND_RANK.get(it["cond"], "C")
        sale, why = sale_for(M, tabs, model, color, rank, settings)
        if not sale:
            return None   # 相場不足は入れない
        p_now = calc.profit(sale, price + ship_v)
        if p_now < calc.min_profit:
            return None
        base.update({"用途": "在庫", "商品の状態": f"{rank}：{COND_NAME.get(it['cond'], '')}" + ("・即決" if it["fixed"] else ""),
                     "相場": round(sale), "上限": None,
                     "メモ": f"{rank}相場{int(sale):,}円（{why}）" + ("／送料不明→1,000円で計算" if ship is None else "")})
        return base
    # ---- ジャンク：NGの記載は外す（ピンクの902KC/903KCだけ「充電できない」はOK）
    if NG_ALWAYS.search(t):
        return None
    if NG_UNLESS_REPAIRABLE.search(t) and not pink:
        return None
    # ふるい：外装Cで直した場合の相場から、明らかに高すぎる出品を外す（AIの前に。回数を使わない）
    sale, why = sale_for(M, tabs, model, color, "C", settings)
    if not sale:
        return None
    rough = calc.cap(sale, ship_v, calc.parts_default)
    if price > rough:
        return None
    use = "修理して在庫" if (pink or re.search(r"液晶|画面|割れ|充電", t)) else "部品取り"
    if not pink and color != "ピンク" and use == "修理して在庫" and genre == "ゲーム機":
        use = "修理して在庫"
    cap, memo, judged = junk_cap(M, tabs, gem, calc, settings, it, model, color, use)
    if judged and cap is None:
        return None   # AIで見てNG・相場不足・直す範囲外
    if judged and price > cap:
        return None
    base.update({"用途": use, "商品の状態": "ジャンク：" + (it["title"][:30]), "相場": round(sale) if not judged else "",
                 "上限": cap if judged else "AI未判定",
                 "メモ": (memo if judged else f"{memo}／目安の上限{rough:,}円（外装C・部品代は仮）")
                 + ("／送料不明→1,000円で計算" if ship is None else "")})
    return base


# ---------------------------------------------------------------- 落札したら記録
def record_win(M, tabs, row, aid, price, mail):
    """古物台帳に1行、端末管理に1台（管理番号つき）。部品取りなら生きている部品を部品表に"""
    try:
        ship = num(row[COL["送料"] - 1]) or 0
        model, color, use = row[COL["機種"] - 1], row[COL["色"] - 1], row[COL["用途"] - 1]
        today = datetime.now(JST).strftime("%Y/%m/%d")
        tabs.append("古物台帳", [["=ROW()-5", today, "ヤフオク", aid, "携帯電話" if row[COL["ジャンル"] - 1] == "ガラケー" else "ゲーム機",
                                f"{model} {color} {row[COL['商品の状態'] - 1]}"[:80], 1, int(price), "ヤフオク出品者",
                                "特例" if price < 10000 else "経過措置", "", "", "✓ 落札メール", f"送料{int(ship)}円・自動記録"]])
        dev = tabs.get(DEVICES, fresh=True)
        no = next_device_no(dev)
        state = "部品取り" if use == "部品取り" else ("修理待ち" if "修理" in str(use) else "検品待ち")
        tabs.append(DEVICES, [[no, model, color, f"ヤフオク {aid}", today, "", int(price + ship), state, "",
                               str(row[COL["メモ"] - 1])[:80], label_text(no, model, color, state)]])
        if use == "部品取り":
            tabs.append(PARTS, [[no, model, "（生きている部品は届いたら確認）", "", "在庫", f"ヤフオク {aid}"]])
    except Exception as e:
        print("落札の記録に失敗:", str(e)[:120])


def next_device_no(dev):
    n = 0
    for row in dev[1:]:
        m = re.match(r"K-(\d+)", str(row[0] if row else ""))
        if m:
            n = max(n, int(m.group(1)))
    return f"K-{n + 1:03d}"


def label_text(no, model, color, state):
    return f"{no} {model} {color} {state}"[:24]


# ---------------------------------------------------------------- ⑨ eBayに出品する直前の確認（メルカリ・Yahoo!フリマ）
_gate_gem = [None]


def listing_gate(M, r, info, detail):
    """None：出品してよい（キーが無いときは今まで通り）／("SKIP", 理由)／("WAIT", 理由)"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return None
    if _gate_gem[0] is None:
        _gate_gem[0] = Gemini(Tabs(M))
    gem = _gate_gem[0]
    if not gem.available(1):
        return ("WAIT", "AIの確認の今日の回数を使い切ったため、明日出品します")
    ans = gem.ask(1, LISTING_PROMPT.format(model=r.get("query") or "", title=(info or {}).get("title", r.get("title", "")),
                                           cond=(detail or {}).get("condition", ""),
                                           desc=((detail or {}).get("description") or "")[:1500]),
                  (detail or {}).get("images", [])[:4])
    if ans is None:
        return ("WAIT", "AIの確認ができなかったため、次の回に出品します") if gem.blocked else None
    if ans.get("ok") is False:
        return ("SKIP", f"【AI確認】出品しません：{str(ans.get('reason', ''))[:40]}（{ans.get('rank', '')}）")
    return None


# ---------------------------------------------------------------- ⑩ 出品中の商品の状態（余った回数で少しずつ）
def check_active_listings(M, per_run=5):
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return
    tabs = Tabs(M)
    gem = Gemini(tabs)
    vals = tabs.get("商品リスト")
    if not vals:
        return
    head = [str(h).strip() for h in vals[0]]
    if "状態ランク（AI）" not in head:
        return
    rc, nc = head.index("状態ランク（AI）") + 1, head.index("気になる点（AI）") + 1
    done, cells, flagged = 0, [], []
    for i, row in enumerate(vals[1:], start=2):
        if done >= per_run or not gem.available(4):
            break
        row = list(row) + [""] * (len(head) - len(row))
        if str(row[3]) != "active" or not str(row[2]).strip() or str(row[rc - 1]).strip():
            continue
        url = str(row[1])
        try:
            if "yahoo" in url:
                _, info, detail = M.check_yahoo(url)
            else:
                continue   # メルカリはブラウザが要るので、Yahoo!フリマの出品から（メルカリ分は見張りの回で順次）
        except Exception:
            continue
        ans = gem.ask(4, LISTING_PROMPT.format(model=row[12], title=row[7], cond=(detail or {}).get("condition", ""),
                                               desc=((detail or {}).get("description") or "")[:1500]),
                      (detail or {}).get("images", [])[:4])
        if not ans:
            break
        done += 1
        cells += [(i, rc, ans.get("rank", "")), (i, nc, str(ans.get("note") or ans.get("reason") or "")[:60])]
        if ans.get("ok") is False:
            flagged.append(f"・{row[7][:30]}（eBay {row[2]}）：{ans.get('reason', '')}")
    if cells:
        tabs.set("商品リスト", cells)
    if flagged:
        M.alert("【確認して】出品中の商品で状態が気になるもの", "\n".join(flagged) +
                "\n\n出品の終了・値下げはしていません。必要ならご自身で判断してください。")


_ranks = [None]


def ai_rank(M, ebay_id):
    """商品リストの「状態ランク（AI）」（⑩で記録したもの）"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return ""
    if _ranks[0] is None:
        _ranks[0] = {}
        vals = Tabs(M).get("商品リスト")
        head = [str(h).strip() for h in (vals[0] if vals else [])]
        if "状態ランク（AI）" in head:
            rc = head.index("状態ランク（AI）")
            for row in vals[1:]:
                if len(row) > rc and str(row[rc]).strip():
                    _ranks[0][str(row[2]).strip()] = str(row[rc]).strip()
    return _ranks[0].get(str(ebay_id).strip(), "")


# ---------------------------------------------------------------- ⑪ 状態別相場（週1回）
def weekly_rank_market(M, data):
    tabs = Tabs(M)
    vals = tabs.get(RANKMKT)
    last = None
    for row in vals[1:]:
        d = to_dt(row[7] if len(row) > 7 else "")
        if d and (last is None or d > last):
            last = d
    if last and (datetime.now(JST) - last).days < 7:
        return
    settings = data.get("settings", {})
    gem = Gemini(tabs)
    models = [("902KC", c) for c in ("ピンク", "シルバー", "ブラック")] + [("501KC", "")]
    for k in [k for k, g, _ in keywords_for(M, settings) if g == "ガラケー"]:
        if market_model(k) != "902KC" and (k, "") not in models:
            models.append((k, ""))
    cache_f = os.path.join(".cache", "rank.json")
    try:
        graded = json.load(open(cache_f, encoding="utf-8"))
    except (OSError, ValueError):
        graded = {}
    out = [["機種", "色", "状態ランク", "安い", "ふつう", "高い", "件数", "最終更新", "根拠"]]
    for model, color in models[:40]:
        mk = ebay_cond_market(M, model, color, settings)
        if not mk:
            out.append([model, color, "全体", "", "", "", 0, now_s(), "相場不足"])
            continue
        out.append([model, color, "全体", mk["安い"], mk["ふつう"], mk["高い"], mk["件数"], now_s(),
                    mk["根拠"] + ("（AIのランク判定はキー待ち）" if not gem.key else "")])
        if not gem.key:
            continue
        by_rank = {}
        for iid, yen, title, img in _mkt_items.get((market_model(model), color), []):
            if iid not in graded and img and gem.available(3):   # 判定は1件1回（毎週は新しく増えた分だけ）
                ans = gem.ask(3, RANK_PROMPT.format(model=model, title=title), [img])
                if ans and ans.get("rank"):
                    graded[iid] = str(ans["rank"])[:3]
            if iid in graded:
                by_rank.setdefault(graded[iid].upper()[:1], []).append(yen)
        for rk in "SABC":
            v = sorted(by_rank.get(rk, []))
            if len(v) >= 3:
                q = statistics.quantiles(v, n=4)
                out.append([market_model(model), color, rk, round(q[0]), round(statistics.median(v)), round(q[2]), len(v),
                            now_s(), "日本から出品中×補正・AIでランク判定"])
            elif v:
                out.append([market_model(model), color, rk, "", "", "", len(v), now_s(), "相場不足"])
    try:
        os.makedirs(".cache", exist_ok=True)
        json.dump(graded, open(cache_f, "w", encoding="utf-8"))
    except OSError:
        pass
    M.sheet("tab_replace", name=RANKMKT, values=out)
