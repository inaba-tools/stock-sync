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
ARCHIVE = "ヤフオク履歴"   # 終了・見送り・負けの行の置き場（同じ出品をもう一度候補にしないために残す）
DONE = ("終了", "見送り", "負け")
HAND = ("落札", "購入済み")   # 手元在庫シートに記録したら、ヤフオク入札からは外す（2026/11/01 本人の希望）
DEVICES = "手元在庫"   # 2026/11/01 名前を変更（旧：端末管理）。落札・購入した端末はここで管理する
PARTS = "部品表"
REPAIR = "修理部品表"   # 2026/11/01 機種×不具合×部品・値段・購入リンク（部品代はここの値段を使う）
JUDGED_FILE = os.path.join(".cache", "ai_judged.json")   # AIで見た出品（1件1回だけ判定する）
RANKMKT = "状態別相場"
HEADERS = {"User-Agent": "stock-sync/1.0 (+https://github.com/inaba-tools/stock-sync)"}
DUTY = 12.5
ACTIVE = ("検討中", "入札中", "上回られた")
MAX_NEW = 20   # 1回に追加する新しい候補の上限（メールが多くなりすぎないように）
YAHOO_MAIL = False   # 2026/11/01 本人の希望：ヤフオク関係のメール（上回られた・終了30分前・落札）は送らない。シートは今まで通り更新する
NEW_MAIL = False   # 2026/11/1 本人の希望：新しい候補（ヤフオク・フリマのジャンク）のメールは送らない。シートに入るだけ。上回られた・30分前・落札のメールは送る
# 2026/11/1 本人の希望で「終了日時」を一番左（A列）へ。終了・見送り・負けの行は「ヤフオク履歴」シートへ移し、残りは終了が近い順に並べる
# 2026/11/1 本人の希望で「商品の状態」を「状態ランク」（[A] の形だけ）と「状態説明」の2列に分けた
COL = {k: i for i, k in enumerate(["終了日時", "状態", "リンク", "用途", "ジャンル", "機種", "色", "状態ランク", "状態説明", "現在価格", "送料", "合計",
                                   "入札数", "参考入札額", "自分の最高入札額", "eBay相場", "利益今", "利益上限",
                                   "メモ", "最終更新", "ID"], start=1)}
M_FORMULA = ('=IF({ID}{r}="","",IF({用途}{r}<>"在庫","AI未判定",IF({相場}{r}="","相場不足",FLOOR({相場}{r}*(1-(VALUE(\'設定\'!$B$7)'
             '+VALUE(\'設定\'!$B$8)+12.5)/100)-VALUE(\'設定\'!$B$6)-N(YAHOO_MIN_PROFIT)-N({送料}{r}),10))))')
P_FORMULA = ('=IF(OR({相場}{r}="",{合計}{r}=""),"",ROUND({相場}{r}*(1-(VALUE(\'設定\'!$B$7)+VALUE(\'設定\'!$B$8)+12.5)/100)'
             '-VALUE(\'設定\'!$B$6)-{合計}{r}))')
Q_FORMULA = ('=IF(OR({相場}{r}="",{最高}{r}=""),"",ROUND({相場}{r}*(1-(VALUE(\'設定\'!$B$7)+VALUE(\'設定\'!$B$8)+12.5)/100)'
             '-VALUE(\'設定\'!$B$6)-{最高}{r}-N({送料}{r})))')



def _letter(n):
    t = ""
    while n:
        n, m = divmod(n - 1, 26)
        t = chr(65 + m) + t
    return t


_L = {"ID": _letter(COL["ID"]), "用途": _letter(COL["用途"]), "相場": _letter(COL["eBay相場"]), "送料": _letter(COL["送料"]),
      "合計": _letter(COL["合計"]), "最高": _letter(COL["自分の最高入札額"]), "価格": _letter(COL["現在価格"])}


def _f(tpl, r):
    """列の文字は COL から決める（列を足しても数式がずれないように）"""
    return tpl.replace("{{", "\x00").replace("}}", "\x01").format(r=r, **_L).replace("\x00", "{").replace("\x01", "}")


def src_url(aid):
    aid = str(aid).strip()
    return aid if aid.startswith("http") else (f"https://auctions.yahoo.co.jp/jp/auction/{aid}" if aid else "")


def total_formula(r):
    return f'=IF({_L["価格"]}{r}="","",{_L["価格"]}{r}+N({_L["送料"]}{r}))'


def link_formula(r, label):
    if label in ("メルカリ", "Yahoo!フリマ"):
        return f'=HYPERLINK({_L["ID"]}{r},"{label}")'
    return f'=HYPERLINK("https://auctions.yahoo.co.jp/jp/auction/"&{_L["ID"]}{r},"ヤフオク")'


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
                      r"フィルム|保護|互換|部品のみ|パーツのみ|基板のみ|外装のみ|ジャケット|シール|\d{3,4}\s*mAh|容量\+")
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
        # 最低利益：ヤフオク（手元在庫になる仕入れ）は設定シートの「ヤフオクの最低利益」（H6）を使う。空なら B14（最低の利益額）
        # 2026/11/01 本人と相談：在庫を抱えるリスクがあるので、無在庫（B14）と分けて決められるようにした
        self.min_profit = float(sget(smap, "ヤフオクの最低利益", 0) or settings.get("min_profit") or sget(smap, "最低の利益額") or 2000)
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
    if not _mkt_loaded[0]:
        _mkt_loaded[0] = True
        _load_mkt()
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
    _mkt_ts[key] = time.time()
    if len(_mkt) % 10 == 0:
        save_mkt()
    return res


RANK_LEVEL = {"S": "高い", "A": "ふつう", "B": "安い", "C": "安い"}
RANK_EN = {"S": "Like new - no visible signs of use", "A": "Excellent - very light signs of use",
           "B": "Good - light scratches and normal signs of use", "C": "Fair - noticeable scratches or wear"}
DECLARED = [("未使用に近い", "S"), ("新品", "S"), ("未使用", "S"), ("目立った傷や汚れなし", "A"), ("やや傷や汚れあり", "B"),
            ("全体的に状態が悪い", "C"), ("傷や汚れあり", "C")]
UNKNOWN_RANK = "B"   # 2026/11/01 状態が分からない出品（メルカリの検索結果など）は B（安めの相場）として判断する
MKT_FILE = os.path.join(".cache", "cond_market.json")
MKT_TTL_H = 12


def declared_rank(cond):
    """出品者が申告した「商品の状態」→ S/A/B/C（AIを使わない）。分からなければ ''"""
    c = unicodedata.normalize("NFKC", str(cond or "")).strip()
    if c.upper() in COND_RANK:
        return COND_RANK[c.upper()]
    for jp, rk in DECLARED:
        if jp in c:
            return rk
    return ""


_TABS = [None]


def rank_market_usd(M, model, title, rank, settings):
    """状態ランクの相場（$・補正後＝実際に売れる値段の目安）と根拠。相場不足なら (None, 理由)"""
    if _TABS[0] is None:
        _TABS[0] = Tabs(M)
    color = color_of(title)
    sale, why = sale_for(M, _TABS[0], model, "" if color == "複数色" else color, rank or UNKNOWN_RANK, settings,
                         sold_mode="direct")   # 無在庫は売れてから仕入れるので、売れた値段をそのまま使う
    if not sale:
        return None, why
    return sale / M.usd_jpy(settings), why


def rank_market_raw_usd(M, model, title, rank, settings):
    """シートの「eBay相場($)」に入れる値（補正前の形。profit() が補正をかけるので割り戻しておく）"""
    usd, why = rank_market_usd(M, model, title, rank, settings)
    if usd is None:
        return None, why
    return round(usd / (float(settings.get("market_adj") or 80) / 100), 2), why


def _load_mkt():
    try:
        with open(MKT_FILE) as f:
            d = json.load(f)
        now = time.time()
        for k, (ts, v) in d.items():
            if now - ts < MKT_TTL_H * 3600:
                m, _, c = k.partition("|")
                _mkt[(m, c)] = v
                _mkt_ts[(m, c)] = ts
    except Exception:
        pass


def save_mkt():
    try:
        os.makedirs(".cache", exist_ok=True)
        with open(MKT_FILE, "w") as f:
            json.dump({f"{m}|{c}": [_mkt_ts.get((m, c), time.time()), v] for (m, c), v in _mkt.items()}, f, ensure_ascii=False)
    except Exception:
        pass


_mkt_ts = {}
_mkt_loaded = [False]


SOLD_RANK = "売れた値段"   # 状態別相場シートの「状態ランク」がこれの行＝eBayで実際に売れた値段（Terapeak・過去90日、月1回更新）
SOLD_MAX_DAYS = 60   # これより古い「売れた値段」は使わない（今まで通りの計算に戻す）


def sold_for(tabs, model, color):
    """状態別相場シートの「売れた値段」の行 → {"安い","ふつう","高い","件数"}（円）。無い・古いなら None。
    色の行（902KCのピンクなど）があればそれ、無ければ色を問わない行（ピンクなど高い色を除いた値段）"""
    mm = market_model(model)
    try:
        rows = [r for r in tabs.get(RANKMKT)[1:] if len(r) >= 8 and str(r[0]) == mm and str(r[2]) == SOLD_RANK]
    except Exception:
        return None
    for want in ([color, ""] if color else [""]):
        for r in rows:
            if str(r[1]) != want:
                continue
            d = to_dt(r[7])
            if not d or (datetime.now(JST) - d).days > SOLD_MAX_DAYS:
                return None
            try:
                v = {"安い": float(r[3]), "ふつう": float(r[4]), "高い": float(r[5]), "件数": int(float(r[6] or 0))}
            except (TypeError, ValueError):
                return None
            return v if v["件数"] >= 3 else None
    return None


def sale_for(M, tabs, model, color, rank, settings, sold_mode="cap"):
    """(円, 根拠)。今まで通りの見込み（_sale_est）に、eBayで実際に売れた値段（sold_for）を合わせる。
    2026/11/01 本人と決定：ヤフオク（先に買う）は売れた値段を上限にする（sold_mode="cap"）、
    無在庫（売れてから買う）は売れた値段をそのまま使う（sold_mode="direct"）"""
    sale, why = _sale_est(M, tabs, model, color, rank, settings)
    if why.startswith("自分の実績"):
        return sale, why
    sold = sold_for(tabs, model, color)
    if not sold:
        return sale, why
    lv = RANK_LEVEL.get(rank, "安い")
    if sold_mode == "direct" or not sale:
        return sold[lv], f"売れた値段の{lv}側（{sold['件数']}件）"
    if sale > sold[lv]:
        return sold[lv], f"売れた値段の{lv}側で上限（{sold['件数']}件）"
    return sale, why


def _sale_est(M, tabs, model, color, rank, settings):
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
    if not mk and color:   # 色ごとでは件数が足りない → 色を問わない相場（安い側）で見る
        mk, color = ebay_cond_market(M, model, "", settings), ""
    if not mk:
        return None, "相場不足"
    lv = RANK_LEVEL.get(rank, "安い") if color else "安い"   # 色が分からないときは安い側（色で相場が大きく違うため）
    return mk[lv], f"出品中×補正の{lv}側（{mk['件数']}件{'・色不明' if not color else ''}）"


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
        for u in list(image_urls)[:10]:   # 2026/11/01 写真は10枚まで見る（4枚だと液晶の写真を見落とした）
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
        time.sleep(4.5)   # 無料枠の1分あたりの回数（15回）を超えないように
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
状態ランク：S=新品同様・使用感なし／A=キズほぼなし／B=使用感・小キズあり／C=目立つキズ・色あせ・欠け／ジャンク=故障。写真を優先して厳しめに付ける。
答えの形：{{"ok":true/false,"reason":"出品しない理由（ok=falseのとき・30字以内）","rank":"S/A/B/C/ジャンク",
"color_in_photo":"写真の本体の色","note":"気になる点（30字以内・日本語）",
"note_en":"海外の買い手向けの状態の説明（英語・150字以内。写真から分かるキズや使用感を正直に）",
"title_en":"eBay用の英語タイトル（80文字以内。ブランド・型番・色・Japan・目立つ特徴や付属品を、海外の人が検索する言葉で。誇張しない）",
"highlights_en":["英語の箇条書き（3〜5個。状態・付属品・動作確認の記載など、写真と説明文から確かなことだけ）"],
"color_en":"本体の色（英語1〜2語）","included_en":["写真に写っている付属品（英語。Battery, Charger, Box, Manual, Strap など。本体は含めない）"],
"sim_unlocked":"説明文にSIMフリー・SIMロック解除済みと書いてあれば true、書いていなければ null",
"skip_photos":[eBayに載せないほうがよい写真の番号（送った順に1から）。大きな日本語の文字・値段・宣伝の文字入りの画像、別の商品やほかの出品物が写っている画像、商品と関係ない画像だけ。無ければ空]}}
英語の項目は、説明文と写真から確かなことだけを書く。分からないことは書かない。
ok=false にするのは：状態の申告と写真が明らかに違う／タイトルと写真の色が違う／背面などに大きなダメージ／本体が写っていない・本体なしに見える／ジャンク・故障の記載 のどれか。"""

RANK_PROMPT = """eBayで売られている中古品（{model}）の写真と説明文から、状態ランクをJSONだけで答えてください。
S=新品同様、A=キズほぼなし、B=使用感・小キズ、C=目立つキズ・色あせ、ジャンク=故障。
タイトル：{title}
答えの形：{{"rank":"S/A/B/C/ジャンク","color":"本体の色（英語）"}}"""


# ---------------------------------------------------------------- ジャンクの上限（出品ごと）
def parts_cost(tabs, model, parts, calc):
    """修理部品表（機種・不具合・部品・値段）→ 部品表（取り出した部品）の順に値段を探す。無ければ目安（仮）"""
    price, note = 0, ""
    rows = []   # (機種, 言葉のリスト, 値段)
    try:
        for row in tabs.get(REPAIR)[1:]:
            if len(row) >= 4 and str(row[3]).strip():
                v = num(row[3])
                if v is not None:
                    rows.append((str(row[0]).strip(), [str(row[1]).strip(), str(row[2]).strip()], v))
    except Exception:
        pass
    try:
        for row in tabs.get(PARTS)[1:]:
            if len(row) >= 4 and str(row[3]).strip():
                v = num(row[3])
                if v is not None:
                    rows.append((str(row[1]).strip(), [str(row[2]).strip()], v))
    except Exception:
        pass

    def find(p):
        for m, words, v in rows:
            if market_model(m).upper() == market_model(model).upper() and any(w and (w in p or p in w) for w in words):
                return v
        return None

    missing = []
    for p in parts or []:
        v = find(str(p))
        if v is None:
            missing.append(p)
        else:
            price += v
    if missing:
        price += calc.parts_default
        note = "部品代は仮"
    return price, note


_judged = {}


def load_judged():
    try:
        with open(JUDGED_FILE) as f:
            _judged.update(json.load(f))
    except (OSError, ValueError):
        pass
    old = (datetime.now(JST) - timedelta(days=21)).strftime("%Y-%m-%d")
    for k in [k for k, v in _judged.items() if v[2] < old]:
        del _judged[k]


def save_judged():
    try:
        os.makedirs(".cache", exist_ok=True)
        with open(JUDGED_FILE, "w") as f:
            json.dump(_judged, f, ensure_ascii=False)
    except OSError:
        pass


def junk_cap(M, tabs, gem, calc, settings, it, model, color, use):
    """ジャンク1件の上限（参考入札額の列）とメモ。戻り値 (上限 or None, メモ, 判定済みか)。AIの判定は出品ごとに1回だけ（結果を覚えておく）"""
    if not _judged:
        load_judged()
    if it["id"] in _judged:
        cap, memo, _ = _judged[it["id"]]
        return cap, memo, True
    cap, memo, judged = _junk_cap(M, tabs, gem, calc, settings, it, model, color, use)
    if judged:
        _judged[it["id"]] = [cap, memo, datetime.now(JST).strftime("%Y-%m-%d")]
        save_judged()
    return cap, memo, judged


def _junk_cap(M, tabs, gem, calc, settings, it, model, color, use):
    detail = it if (it.get("images") or not it.get("auction", True)) else y_item(it["id"])
    if not detail:
        return None, "出品ページが読めず", False
    ans = gem.ask(2, JUNK_PROMPT.format(model=model, color=color or "不明", title=detail["title"],
                                        desc=(detail.get("description") or "")[:1500]), detail.get("images", [])[:10])
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
    # 直す範囲：液晶・外装（ガラケーは部品取りと組み合わせ）、ゲーム機はバッテリー・スライドパッド・ボタンも
    ok_fix = {"液晶", "外装割れ", "外装"} | ({"充電口"} if pink else set())
    if M.game_info(model):
        ok_fix |= {"バッテリー", "スライドパッド", "スティック", "ボタン", "十字キー"}
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
        return cap, (f"部品取り→{target[0]}（{target[1]} {target[2]}）を直す／相手の仕入{int(t_cost):,}円"), True
    if any(not any(f in b for f in ok_fix) for b in broken if b):
        return None, "直す範囲外の故障：" + "・".join(broken)[:30], True
    need = ans.get("needed_parts") or broken
    pc, pnote = parts_cost(tabs, model, need, calc)
    sale, why = sale_for(M, tabs, model, color, rank, settings)
    if not sale:
        return None, "相場不足", True
    cap = calc.cap(sale, it.get("ship") or 0, pc)
    memo = (f"{note_ext}{'・'.join(broken) or '故障'}→部品{int(pc):,}円{('（' + pnote + '）') if pnote else ''}")
    return cap, memo, True


# ---------------------------------------------------------------- メイン
YAHOO_GAMES = set()   # ヤフオクで扱うゲーム機。2026/11/01 本人の希望でPS Vitaも外し、ゲーム機は扱わない（無在庫は設定B12のゲーム機を全部扱う）


def keywords_for(M, settings):
    kws = [k.strip() for k in re.split(r"[,、\n]", str(settings.get("keywords") or "")) if k.strip()]
    out = []
    for k in kws:
        gi = M.game_info(k)
        if gi is None:
            if re.search(r"\d{3}(KC|SH)$|^(KY|SH|P|F|N|SO|CA)-?\d|^KYF|^A\d{3}", k, re.I):
                out.append((k, "ガラケー", k))
        elif not gi.get("kind") and k.upper() in YAHOO_GAMES:   # 2026/11/01 ヤフオク（先に買う）のゲーム機はPS Vitaだけ
            out.append((k, "ゲーム機", gi.get("yahoo_q", k)))
    return out


def to_dt(v):
    s = str(v or "").strip().replace("/", "-")
    s = re.sub(r"\.\d+(?=Z|[+-]\d{2}:?\d{2}$|$)", "", s).replace("Z", "+0000")   # 2026-11-01T03:00:00.000Z の形も読む
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{2})", s)   # 年なし（11/1 12:34）
    if m:
        now = datetime.now(JST)
        mo, d, h, mi = map(int, m.groups())
        y = now.year + (1 if mo < now.month - 6 else 0) - (1 if mo > now.month + 6 else 0)
        try:
            return datetime(y, mo, d, h, mi, tzinfo=JST)
        except ValueError:
            return None
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


def run(M, data, mode="discover", flea=None):
    """mode="discover"：行の更新＋新しい出品さがし（1時間ごとの回の1本目）／"soon"：終了2時間前の行だけ（30分ごとの見張りの回）
    flea：flea_junk の結果（フリマの修理用ジャンクの新しい候補・フリマの行の更新）。ヤフオクの新着と1通のメールにまとめる"""
    flea = flea or {}
    tabs = flea.get("tabs") or Tabs(M)
    settings = data.get("settings", {})
    smap = setting_map(tabs.get("設定"))
    calc = Calc(settings, smap)
    gem = flea.get("gem") or Gemini(tabs)
    vals = tabs.get(TAB)
    now_dt = datetime.now(JST)
    cells, mails_outbid, mails_soon, ended_bid, notes = list(flea.get("cells") or []), [], [], [], []
    known = {str(r[COL["ID"] - 1]).strip() for r in vals[1:] if len(r) >= COL["ID"]} | archived_ids(tabs)
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
        st, aid = str(row[COL["状態"] - 1]).strip(), str(row[COL["ID"] - 1]).strip()
        if st not in ACTIVE or not aid or aid.startswith("http"):   # フリマの行は flea_junk で確認
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
        title = f"{row[COL['機種'] - 1]} {row[COL['色'] - 1]} {row[COL['状態ランク'] - 1]}"[:40]
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

    # ---- 新しい候補を追加（フリマの修理用ジャンクも同じシートに）
    new_rows += [x for x in (flea.get("rows") or []) if x[1] not in {a for _, a in new_rows}]
    if new_rows:
        start = len(vals) + 1
        rows = []
        for k, (c, aid) in enumerate(new_rows):
            r = start + k
            link = link_formula(r, c.get("src") or "ヤフオク")
            rows.append([c["終了日時"], c["状態"], link, c["用途"],
                         c["ジャンル"], c["機種"], c["色"], c["状態ランク"], c["状態説明"], c["現在価格"], c["送料"],
                         total_formula(r), c["入札数"],
                         c["上限"] if c.get("上限") is not None else _f(M_FORMULA, r), "", c.get("相場", ""),
                         _f(P_FORMULA, r), _f(Q_FORMULA, r), c["メモ"], now_s(), aid])
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
    if body and YAHOO_MAIL:
        M.alert("【ヤフオク】" + ("上回られました" if mails_outbid else "まもなく終了"), "\n\n".join(body)
                + "\n\n入札はご自身で判断してください（自動入札はしません）")
    if NEW_MAIL and new_rows:
        lines, flines = [], []
        for c, aid in new_rows:
            capt = f"上限 {int(c['上限']):,}円" if isinstance(c.get("上限"), (int, float)) else (
                "参考入札額はシートで確認" if c["用途"] == "在庫" else "上限：AI未判定")
            if c.get("src"):   # フリマ（即決・早い者勝ち）
                flines.append(f"・[{c['src']}] {c['機種']} {c['色']}（{c['用途']}）{c['現在価格']:,}円 → {capt}\n  {c['メモ'][:70]}\n  {aid}")
            else:
                lines.append(f"・{c['機種']} {c['色']} {c['状態ランク']}（{c['用途']}）今 {c['現在価格']:,}円＋送料{c['送料'] if c['送料'] != '' else '不明'}"
                             f" → {capt}\n  {c['メモ'][:60]}\n  https://auctions.yahoo.co.jp/jp/auction/{aid}")
        body = []
        if flines:
            body.append("【フリマの修理用ジャンク（早い者勝ち）】\n" + "\n".join(flines))
        if lines:
            body.append("【ヤフオク】\n" + "\n".join(lines) + ("\n" + "\n".join(notes) if notes else ""))
        subj = "・".join(x for x in [f"フリマのジャンク{len(flines)}件" if flines else "", f"ヤフオク{len(lines)}件" if lines else ""] if x)
        M.alert(f"【仕入れ】新しい候補（{subj}）", "\n\n".join(body)
                + "\n\n入札・購入はご自身で（自動ではしません）。")
    if won and YAHOO_MAIL:
        M.alert(f"【ヤフオク】落札 {len(won)}件", "\n".join(f"・{r[COL['機種'] - 1]} {r[COL['色'] - 1]} {int(p):,}円 "
                                                          f"https://auctions.yahoo.co.jp/jp/auction/{a}" for _, a, r, p, _ in won)
                + "\n\n古物台帳と手元在庫シートに記録しました。")
    if mode in ("discover", "soon"):
        try:   # 2026/11/01 在庫の行をAIで見て、状態ランクの相場に直す（1回10件まで）
            ai_recheck_bids(M, tabs, gem, calc, settings or {}, limit=10)
        except Exception as e:
            print("ヤフオク入札のAI確認でエラー:", type(e).__name__, str(e)[:100])
    if mode in ("discover", "soon"):   # 2026/11/01 30分ごとの見張りの回でも並べ直す（いつも終了が近い順に）
        try:
            tidy(tabs)
        except Exception as e:
            print("ヤフオク入札シートの並べ替えでエラー:", type(e).__name__, str(e)[:100])
    msg = (f"{now_s()} ヤフオク（{mode}）：行の更新{len({c[0] for c in cells})}件／新しい候補{len(new_rows)}件／"
           f"上回られた{len(mails_outbid)}件／落札{len(won)}件／Gemini {'キー待ち' if not gem.key else f'今日{gem.used}回'}")
    print(msg)
    try:
        M.sheet("log", message=msg)
    except Exception:
        pass
    return gem


def archived_ids(tabs):
    try:
        return {str(r[COL["ID"] - 1]).strip() for r in tabs.get(ARCHIVE)[1:] if len(r) >= COL["ID"]}
    except Exception:
        return set()


def _cell_out(v):
    """シートから読んだ値を、書き戻せる形に（日時は 2026/11/01 12:34 の形）"""
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}T", v):
        d = to_dt(v)
        return d.astimezone(JST).strftime("%Y/%m/%d %H:%M") if d else v
    return v


def _row_formulas(row, r):
    """行 r に置くときの数式を入れ直す（リンク・合計・参考入札額・見込み利益）"""
    row = list(row)
    label = str(row[COL["リンク"] - 1]).strip()
    row[COL["リンク"] - 1] = link_formula(r, label)
    row[COL["合計"] - 1] = total_formula(r)
    m = row[COL["参考入札額"] - 1]
    if str(row[COL["用途"] - 1]).strip() == "在庫" or num(m) is None:
        row[COL["参考入札額"] - 1] = _f(M_FORMULA, r)
    row[COL["利益今"] - 1] = _f(P_FORMULA, r)
    row[COL["利益上限"] - 1] = _f(Q_FORMULA, r)
    return row


def ai_recheck_bids(M, tabs, gem, calc, settings, limit=10, pace=0.0):
    """ヤフオク入札の「在庫」の行（入札・検討中）をAIで1回だけ見て、状態ランクでeBay相場の列を出し直す。
    ダメな出品は「見送り」。2026/11/01 本人の希望：相場は状態を加味したものにする"""
    vals = tabs.get(TAB, fresh=True)
    load_judged()
    cells, n = [], 0
    ai_stop = False   # 2026/11/01 AIの回数が無くなっても、AIで見済みの行の利益チェック（AIを使わない）は最後まで続ける
    for i, row in enumerate(vals[1:], start=2):
        can_ai = not ai_stop and n < limit and gem.available(2)
        row = list(row) + [""] * (len(COL) - len(row))
        st, aid, use = str(row[COL["状態"] - 1]).strip(), str(row[COL["ID"] - 1]).strip(), str(row[COL["用途"] - 1]).strip()
        if st not in ACTIVE or not aid or aid.startswith("http"):
            continue
        if use != "在庫":   # ジャンク：キーが無くて「AI未判定」のままの行を、出品ごとの上限で判定し直す
            if str(row[COL["参考入札額"] - 1]).strip() != "AI未判定" or not can_ai:
                continue
            try:
                d = y_item(aid)
            except Exception:
                continue
            if not d or d.get("closed"):
                continue
            cap, memo, judged = junk_cap(M, tabs, gem, calc, settings, dict(d, auction=True),
                                         str(row[COL["機種"] - 1]), str(row[COL["色"] - 1]), use)
            if not judged:
                ai_stop = True
                continue
            n += 1
            if pace:
                time.sleep(pace)
            price = (num(row[COL["現在価格"] - 1]) or 0) + (num(row[COL["送料"] - 1]) or 0)
            if cap is None or price > cap:
                cells += [(i, COL["状態"], "見送り"), (i, COL["メモ"], f"AI：{memo}")]
            else:
                cells += [(i, COL["参考入札額"], cap), (i, COL["メモ"], memo)]
            continue
        # 2026/11/01 今のeBay相場（状態の相場）で利益が残らない行は、AIで見たかどうかに関係なく毎回「見送り」にする
        sale = num(row[COL["eBay相場"] - 1])
        price = (num(row[COL["現在価格"] - 1]) or 0) + (num(row[COL["送料"] - 1]) or 0)
        if sale and price and calc.profit(sale, price) < calc.min_profit:
            cells += [(i, COL["状態"], "見送り"),
                      (i, COL["メモ"], f"今の値段だと利益{int(calc.min_profit):,}円が残らない")]
            continue
        if ("stock:" + aid) in _judged or str(row[COL["状態説明"] - 1]).startswith("AI："):   # AIで見済み
            continue
        if not can_ai:
            continue
        try:
            d = y_item(aid)
        except Exception:
            continue
        if not d or d.get("closed") or not d.get("images"):
            continue
        model, color = str(row[COL["機種"] - 1]), str(row[COL["色"] - 1])
        ans = gem.ask(2, LISTING_PROMPT.format(model=model, title=d["title"], cond=d.get("cond_name", ""),
                                               desc=(d.get("description") or "")[:1500]), d["images"][:10])
        if ans is None:
            ai_stop = True
            continue
        n += 1
        if pace:
            time.sleep(pace)
        rank = str(ans.get("rank") or "").strip().upper()
        note = str(ans.get("note") or ans.get("reason") or "")[:40]
        _judged["stock:" + aid] = [rank, note, datetime.now(JST).strftime("%Y-%m-%d")]
        if ans.get("ok") is False or rank == "ジャンク" or \
                re.search(r"本体ではなく|本体ではない|のみの(商品|出品)|パーツのみ|型番違い|ポスター|箱のみ|説明書のみ", note):
            cells += [(i, COL["状態"], "見送り"), (i, COL["メモ"], f"AI：{ans.get('reason') or note}[{rank}]")]
            continue
        if rank not in ("S", "A", "B", "C"):
            continue
        sale, why = sale_for(M, tabs, model, color, rank, settings)
        if not sale:
            cells += [(i, COL["eBay相場"], ""), (i, COL["メモ"], "eBayの売れた値段のデータが足りない")]
            continue
        cells += [(i, COL["eBay相場"], round(sale)), (i, COL["状態ランク"], f"[{rank}]"), (i, COL["状態説明"], ("AI：" + note)[:40]),
                  (i, COL["メモ"], "")]
        price = (num(row[COL["現在価格"] - 1]) or 0) + (num(row[COL["送料"] - 1]) or 0)
        if price and calc.profit(sale, price) < calc.min_profit:   # 状態の相場だと今の値段でも利益が足りない → 見送り
            cells += [(i, COL["状態"], "見送り"),
                      (i, COL["メモ"], f"今の値段だと利益{int(calc.min_profit):,}円が残らない")]
    save_judged()
    if cells:
        tabs.set(TAB, cells)
    return n


def tidy(tabs):
    """終了・見送り・負け・落札・購入済みの行を「ヤフオク履歴」へ移し、残りを終了が近い順に並べ直す（1時間ごとの回の最後に1回）。
    落札・購入済みは手元在庫シートに記録済みのものだけ移す（無ければ記録してから）"""
    vals = tabs.get(TAB, fresh=True)
    if len(vals) < 2:
        return
    n = len(COL)
    head = vals[0]
    rows = [list(r) + [""] * (n - len(r)) for r in vals[1:]]
    rows = [[_cell_out(v) for v in r[:n]] for r in rows if any(str(x).strip() for x in r)]
    changed = False
    for r in rows:   # 終了日時の書き方をそろえる（日時は 2026/11/01 21:30、フリマの即決は「即決」）
        e = r[COL["終了日時"] - 1]
        d = to_dt(e)
        r[COL["終了日時"] - 1] = d.astimezone(JST).strftime("%Y/%m/%d %H:%M") if d else ("即決" if str(e).startswith("即決") else e)
        changed = changed or (str(e).startswith("即決") and e != "即決")
    # 落札・購入済み：手元在庫シートに無ければ記録してから外す（シートで手で「購入済み」にした分も）
    try:
        dev_txt = " ".join(str(c) for r in tabs.get(DEVICES, fresh=True)[1:] for c in r[:4])
    except Exception:
        dev_txt = None
    for r in rows:
        st, aid = str(r[COL["状態"] - 1]).strip(), str(r[COL["ID"] - 1]).strip()
        if st in HAND and aid and dev_txt is not None and aid not in dev_txt:
            record_win(None, tabs, r, aid, num(r[COL["現在価格"] - 1]) or 0, None)
            dev_txt += " " + aid
    out_st = DONE + (HAND if dev_txt is not None else ())
    done = [r for r in rows if str(r[COL["状態"] - 1]).strip() in out_st]
    keep = [r for r in rows if str(r[COL["状態"] - 1]).strip() not in out_st]
    far = datetime(2100, 1, 1, tzinfo=JST)

    def key(r):
        st = str(r[COL["状態"] - 1]).strip()
        end = to_dt(r[COL["終了日時"] - 1])
        return (0 if st in ACTIVE else 1, 0 if end else 1, end or far)

    ordered = sorted(keep, key=key)
    if not done and not changed and [r[COL["ID"] - 1] for r in ordered] == [r[COL["ID"] - 1] for r in keep]:
        return
    if done:
        tabs.ensure(ARCHIVE, head)
        arch = []
        for r in done:
            r = list(r)
            aid = str(r[COL["ID"] - 1]).strip()
            r[COL["リンク"] - 1] = aid if aid.startswith("http") else f"https://auctions.yahoo.co.jp/jp/auction/{aid}"
            for k in ("合計", "利益今", "利益上限"):
                r[COL[k] - 1] = ""
            if num(r[COL["参考入札額"] - 1]) is None:
                r[COL["参考入札額"] - 1] = ""
            arch.append(r)
        tabs.append(ARCHIVE, arch)
    cells = []
    for i, r in enumerate(ordered, start=2):
        for c, v in enumerate(_row_formulas(r, i), start=1):
            cells.append((i, c, v))
    for i in range(len(ordered) + 2, len(rows) + 2):   # 移した分の下の行を空ける
        for c in range(1, n + 1):
            cells.append((i, c, ""))
    tabs.set(TAB, cells)
    print(f"ヤフオク入札：履歴へ{len(done)}件・残り{len(ordered)}件を終了が近い順に並べ替え")


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
    # 設定シートの除外ワード（ジャンク関係の言葉は、ジャンクを別に判定するので除く）
    ex = [w.strip() for w in re.split(r"[,、\n]", str(settings.get("exclude") or "")) if w.strip()]
    if any(w in t for w in ex if not re.search(r"ジャンク|部品取り|訳あり", w)):
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
    if junk and genre == "ゲーム機":   # 2026/11/01 本人の希望：ゲーム機のジャンク（修理して売る分）は扱わない
        return None
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
        base.update({"用途": "在庫", "状態ランク": f"[{rank}]", "状態説明": f"出品者の申告：{COND_NAME.get(it['cond'], '')}" + ("・即決" if it["fixed"] else ""),
                     "相場": round(sale), "上限": None,
                     "メモ": "送料不明→1,000円で計算" if ship is None else ""})
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
    base.update({"用途": use, "状態ランク": "ジャンク", "状態説明": it["title"][:40], "相場": round(sale) if not judged else "",
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
        src = ("メルカリ" if "mercari" in aid else "Yahoo!フリマ") if str(aid).startswith("http") else "ヤフオク"
        tabs.append("古物台帳", [["=ROW()-5", today, src, aid, "携帯電話" if row[COL["ジャンル"] - 1] == "ガラケー" else "ゲーム機",
                                f"{model} {color} {row[COL['状態ランク'] - 1]} {row[COL['状態説明'] - 1]}"[:80], 1, int(price), f"{src}出品者",
                                "特例" if price < 10000 else "経過措置", "", "", "✓ 落札メール" if mail else "シートで購入済み", f"送料{int(ship)}円・自動記録"]])
        dev = tabs.get(DEVICES, fresh=True)
        no = next_device_no(dev)
        state = "部品取り" if use == "部品取り" else ("修理待ち" if "修理" in str(use) else "検品待ち")
        tabs.append(DEVICES, [[no, model, color, f"{src} {aid}", today, "", int(price + ship), state, "",
                               str(row[COL["メモ"] - 1])[:80], label_text(no, model, color, state)]])
        if use == "部品取り":
            tabs.append(PARTS, [[no, model, "（生きている部品は届いたら確認）", "", "在庫", f"{src} {aid}"]])
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


LAST_AI = {}   # 仕入れ先URL → {"rank","note","note_en","usd"}（出品の直前に見た結果。eBayの状態の説明・相場に使う）


def _list_cells(tabs, row, rank, note, raw_usd):
    """無在庫商品リストの行に、状態ランク（AI）・気になる点（AI）・eBay相場($)（状態の相場）を書く（見出しで列を探す）"""
    try:
        head = [str(h).strip() for h in tabs.get("無在庫商品リスト")[0]]
        cells = []
        for name, v in (("状態ランク（AI）", rank), ("気になる点（AI）", note), ("eBay相場($)", raw_usd)):
            if name in head and v not in (None, ""):
                cells.append((row, head.index(name) + 1, v))
        if cells:
            tabs.set("無在庫商品リスト", cells)
    except Exception as e:
        print("無在庫商品リストへの書き込みでエラー:", type(e).__name__, str(e)[:80])


def listing_gate(M, r, info, detail, settings=None):
    """None：出品してよい（キーが無いときは今まで通り）／("SKIP", 理由)／("WAIT", 理由)
    2026/11/01 AIで状態ランク（S/A/B/C）を決め、そのランクの相場で利益2,000円の値段で売れるかを確かめる。
    結果は無在庫商品リストのI列（状態ランク）・V列（気になる点）・K列（状態の相場）に書き、eBayの状態の説明にも使う"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return None
    if _gate_gem[0] is None:
        _gate_gem[0] = Gemini(Tabs(M))
    gem = _gate_gem[0]
    if not gem.available(1):
        return ("WAIT", "AIの確認の今日の回数を使い切ったため、明日出品します")
    title = (info or {}).get("title", r.get("title", ""))
    ans = gem.ask(1, LISTING_PROMPT.format(model=r.get("query") or "", title=title,
                                           cond=(detail or {}).get("condition", ""),
                                           desc=((detail or {}).get("description") or "")[:1500]),
                  (detail or {}).get("images", [])[:10])
    if ans is None:
        return ("WAIT", "AIの確認ができなかったため、次の回に出品します") if gem.blocked else None
    rank = str(ans.get("rank") or "").strip().upper()
    note = str(ans.get("note") or ans.get("reason") or "")[:60]
    if ans.get("ok") is False:
        _list_cells(gem.tabs, r.get("row"), rank, note, None)
        return ("SKIP", f"【AI確認】出品しません：{str(ans.get('reason', ''))[:40]}（{rank}）")
    if rank == "ジャンク":
        _list_cells(gem.tabs, r.get("row"), rank, note, None)
        return ("SKIP", "【AI確認】出品しません：写真・説明文からジャンク扱い")
    if rank not in ("S", "A", "B", "C"):
        rank = declared_rank((detail or {}).get("condition", "")) or UNKNOWN_RANK
    usd = raw = None
    if settings:
        try:
            model = r.get("query") or ""
            cost = float((info or {}).get("price") or 0)
            usd, why = rank_market_usd(M, model, title, rank, settings)
            raw, _ = rank_market_raw_usd(M, model, title, rank, settings)
            if usd is None:
                _list_cells(gem.tabs, r.get("row"), rank, note, None)
                return ("SKIP", f"【AI確認】状態{rank}の相場が足りないため出品しません（{why}）")
            if cost:
                floor = M.floor_price_usd(cost, model, settings)
                if usd < floor:
                    _list_cells(gem.tabs, r.get("row"), rank, note, raw)
                    return ("SKIP", f"【AI確認】状態{rank}の相場は約{int(usd * M.usd_jpy(settings)):,}円（{why}）で、"
                                    f"利益2,000円の値段（約{int(floor * M.usd_jpy(settings)):,}円）では売れにくいので出品しません")
        except Exception as e:
            print("状態別の相場の確認でエラー（今まで通り出品）:", type(e).__name__, str(e)[:80])
    _list_cells(gem.tabs, r.get("row"), rank, note, raw)
    LAST_AI[r.get("mercari_url", "")] = {"rank": rank, "note": note, "note_en": str(ans.get("note_en") or "")[:300],
                                         "usd": usd, "title_en": str(ans.get("title_en") or "").strip(),
                                         "highlights_en": [str(x)[:200] for x in (ans.get("highlights_en") or []) if str(x).strip()][:5],
                                         "color_en": str(ans.get("color_en") or "").strip()[:30],
                                         "included_en": [str(x)[:30] for x in (ans.get("included_en") or []) if str(x).strip()][:8],
                                         "sim_unlocked": ans.get("sim_unlocked") is True,
                                         "skip_photos": [int(x) for x in (ans.get("skip_photos") or []) if str(x).isdigit()]}
    return None


def ai_title(ai, model, fallback):
    """AIのタイトルを使ってよいか確かめる（80文字以内・型番が入っている・おかしな文字がない）。だめなら今までのタイトル"""
    t = re.sub(r"\s+", " ", str((ai or {}).get("title_en") or "")).strip()
    if not t or len(t) > 80 or not t.isascii():
        return fallback
    t = re.sub(r"\s*\[[^\]]*\]\s*", " ", t).strip()   # [AS-IS] などの括弧書きは付けない
    key = re.sub(r"[^A-Z0-9]", "", str(model).upper())
    pat = r"[\s\-_/]*".join(re.escape(c) for c in key)
    if key and not re.search(r"(?<![A-Z0-9])" + pat + r"(?![0-9])", t.upper()):
        return fallback   # 型番が無い、または別の型番（602SH と V602SH など）
    if re.search(r"\bAS[- ]?IS\b|\bN\.?A\.?\b", t.upper()):
        return fallback
    if re.search(r"\b(NEW|MINT|PERFECT|L@@K)\b", t.upper()) and (ai or {}).get("rank") != "S":
        return fallback   # 誇張した言葉は使わない
    return t


def pick_photos(images, skip):
    """AIが「載せないほうがよい」とした写真を外す（1枚目は残す・最低3枚は残す）"""
    skip = {n for n in (skip or []) if 2 <= n <= min(10, len(images))}
    keep = [u for n, u in enumerate(images, start=1) if n not in skip]
    return keep if len(keep) >= 3 else images


def condition_text_en(rank, note_en, declared_en=""):
    """eBayの状態の説明（ConditionDescription）"""
    parts = [f"Condition rank {rank}: {RANK_EN.get(rank, '')}."]
    if note_en:
        parts.append(note_en.strip())
    if declared_en:
        parts.append(f"Seller's note: {declared_en.strip().rstrip('.')}.")
    parts.append("Please check all photos for the exact condition.")
    return " ".join(parts)[:900]


# ---------------------------------------------------------------- 見られていない出品のタイトルを作り直す（2026/11/01）
RETITLE_PROMPT = """eBayに出品している中古品のタイトルを、海外の人が検索で見つけやすいものに作り直してください。JSONだけで答えてください。
2週間たってもウォッチが0です。型番（{model}）は必ずそのまま入れる。80文字以内。誇張しない（NEW・MINT・PERFECTは使わない）。括弧書きは付けない。
今のタイトル：{title}
日本の出品の商品名：{jp}
状態ランク：{rank}（S=新品同様 A=キズほぼなし B=使用感あり C=目立つキズ）
答えの形：{{"title_en":"新しいタイトル"}}"""
RETITLE_MARK = "タイトル見直し済み"


def retitle_slow(M, per_run=3, days=14):
    """出品から14日たってもウォッチ0の出品は、AIにタイトルを考え直させる（1つの出品につき1回だけ）"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return 0
    tabs = Tabs(M)
    gem = Gemini(tabs)
    if not gem.available(3):
        return 0
    token = M.get_access_token()
    act = M.ebay_active_items(token)
    vals = tabs.get("無在庫商品リスト")
    head = [str(h).strip() for h in vals[0]]
    ci, cm, ct, cq = head.index("eBay出品番号"), head.index("メモ"), head.index("商品名"), head.index("検索語")
    cr = head.index("状態ランク（AI）") if "状態ランク（AI）" in head else None
    ns = M.NS
    n, cells = 0, []
    for i, row in enumerate(vals[1:], start=2):
        if n >= per_run:
            break
        row = list(row) + [""] * (len(head) - len(row))
        iid = str(row[ci]).strip()
        a = act.get(iid)
        if not a or a.get("watch") or (a.get("days") or 0) < days or RETITLE_MARK in str(row[cm]):
            continue
        _, _, r = M.trading_call_root("GetItem", f"<ItemID>{iid}</ItemID><OutputSelector>Title</OutputSelector>", token)
        old = r.findtext(".//e:Item/e:Title", default="", namespaces=ns) or ""
        rank = str(row[cr]).strip() if cr is not None else ""
        model = str(row[cq]).strip()
        ans = gem.ask(3, RETITLE_PROMPT.format(model=model, title=old, jp=str(row[ct])[:100], rank=rank or "不明"))
        if ans is None:
            break
        n += 1
        new = ai_title({"title_en": ans.get("title_en"), "rank": rank}, model, old)
        if new and new != old:
            res = M.trading_call("ReviseFixedPriceItem", f"<Item><ItemID>{iid}</ItemID><Title>{_xml(new)}</Title></Item>", token)
            print("タイトル見直し:", iid, old, "→", new, res)
        cells.append((i, cm + 1, (str(row[cm]) + f"／{RETITLE_MARK}({datetime.now(JST).strftime('%m/%d')})").lstrip("／")))
    if cells:
        tabs.set("無在庫商品リスト", cells)
    return n


# ---------------------------------------------------------------- 出品中の商品のタイトル・説明をAIで作り直す（2026/11/01 本人の希望）
AI_MARK = "filled in automatically by AI"


def ai_rewrite_active(M, limit=200, pace=4.5):
    """出品中で、説明にまだAIの箇条書きが無いものを、新しい出品と同じ形（AIのタイトル・箇条書き・色・付属品・SIMフリー）に書き換える。
    すでに書き換え済みのものは飛ばすので、何回動かしても大丈夫。値段・状態ランク・写真は変えない"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        print("Geminiのキーが無いため止めます")
        return 0
    tabs = Tabs(M)
    gem = Gemini(tabs)
    vals = tabs.get("無在庫商品リスト")
    head = [str(h).strip() for h in vals[0]]
    col = {k: head.index(k) for k in ("商品仕入元URL", "eBay出品番号", "状態", "商品名", "検索語", "状態ランク（AI）") if k in head}
    token = M.get_access_token()
    import xml.etree.ElementTree as ET
    ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
    pw = browser = page = None
    done = skipped = 0
    try:
        for i, row in enumerate(vals[1:], start=2):
            if done >= limit or not gem.available(2):
                break
            row = list(row) + [""] * (len(head) - len(row))
            iid = str(row[col["eBay出品番号"]]).strip()
            if str(row[col["状態"]]).strip() not in ("active", "") or not iid:
                continue
            ack, errs, root = M.trading_call_root("GetItem", f"<ItemID>{iid}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
                                                  "<IncludeItemSpecifics>true</IncludeItemSpecifics>", token)
            if ack not in ("Success", "Warning"):
                continue
            if root.findtext(".//e:SellingStatus/e:ListingStatus", default="", namespaces=ns) not in ("Active", ""):
                continue
            desc_html = root.findtext(".//e:Item/e:Description", default="", namespaces=ns) or ""
            if AI_MARK in desc_html:
                skipped += 1
                continue
            url, model, title = str(row[col["商品仕入元URL"]]), str(row[col["検索語"]]), str(row[col["商品名"]])
            try:
                if "mercari" in url:
                    if page is None:
                        from playwright.sync_api import sync_playwright
                        pw = sync_playwright().start()
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page(locale="ja-JP", user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
                    st, _, _, images, desc = _flea_status(M, page, url)
                    detail = {"images": images, "description": desc,
                              "condition": (page.evaluate(M.DETAIL_JS) or {}).get("condition", "") if st == "on_sale" else ""}
                else:
                    _, _, detail = M.check_yahoo(url)
            except Exception:
                continue
            if not detail or not detail.get("images"):
                continue
            ans = gem.ask(2, LISTING_PROMPT.format(model=model, title=title, cond=detail.get("condition", ""),
                                                   desc=(detail.get("description") or "")[:1500]), detail.get("images", [])[:10])
            if not ans:
                break
            done += 1
            time.sleep(pace)   # 1分15回までの決まりに収める
            rank = str(row[col["状態ランク（AI）"]]).strip().upper() if "状態ランク（AI）" in col else ""
            ai = {"rank": rank or str(ans.get("rank") or "").upper(), "title_en": str(ans.get("title_en") or "").strip()}
            old_title = root.findtext(".//e:Item/e:Title", default="", namespaces=ns) or ""
            new_title = ai_title(ai, model, old_title)
            bullets = [str(x)[:200] for x in (ans.get("highlights_en") or []) if str(x).strip()][:5]
            inc = [str(x)[:30] for x in (ans.get("included_en") or []) if str(x).strip()][:8]
            if inc:
                bullets.append(f"Included: {', '.join(inc)} (only the items shown in the photos)")
            if not bullets:
                continue
            block = ("<h3>Item details</h3><p><i>The details below were filled in automatically by AI from the photos and "
                     "the original Japanese description. Please check the photos as well.</i></p><ul>"
                     + "".join(f"<li>{_xml(b)}</li>" for b in bullets) + "</ul>")
            new_desc = re.sub(r"<h2>.*?</h2>", f"<h2>{_xml(new_title)}</h2>", desc_html, count=1)
            if "<h3>Notes from the original Japanese description" in new_desc:
                new_desc = new_desc.replace("<h3>Notes from the original Japanese description", block + "<h3>Notes from the original Japanese description", 1)
            elif "<p>Ships from Japan." in new_desc:
                new_desc = new_desc.replace("<p>Ships from Japan.", block + "<p>Ships from Japan.", 1)
            else:
                new_desc = block + new_desc
            if ans.get("sim_unlocked") is True:
                new_desc = new_desc.replace("Not stated by the original seller. Please check compatibility before purchase.", "Unlocked")
            # 商品の詳しい情報（Item specifics）：今の内容を残して、色・付属品・SIMフリーだけ足す
            specs = []
            for nv in root.findall(".//e:Item/e:ItemSpecifics/e:NameValueList", ns):
                n = nv.findtext("e:Name", default="", namespaces=ns)
                vs = [v.text or "" for v in nv.findall("e:Value", ns)]
                specs.append([n, vs])
            names = [n for n, _ in specs]
            color = str(ans.get("color_en") or "").strip()[:30].title()
            for sp in specs:
                if sp[0] == "Color" and color and sp[1] in (["As Pictured"], [""], []):
                    sp[1] = [color]
                if ans.get("sim_unlocked") is True and sp[0] == "Network":
                    sp[1] = ["Unlocked"]
                if ans.get("sim_unlocked") is True and sp[0] == "Lock Status":
                    sp[1] = ["Network Unlocked"]
            if inc:
                if "Included" in names:
                    specs[names.index("Included")][1] = [", ".join(inc)[:65]]
                else:
                    specs.append(["Included", [", ".join(inc)[:65]]])
            nvl = "".join("<NameValueList><Name>" + _xml(n) + "</Name>" + "".join(f"<Value>{_xml(v)}</Value>" for v in vs)
                          + "</NameValueList>" for n, vs in specs)
            body = (f"<Item><ItemID>{iid}</ItemID><Title>{_xml(new_title)}</Title>"
                    f"<Description><![CDATA[{new_desc}]]></Description><ItemSpecifics>{nvl}</ItemSpecifics></Item>")
            res = M.trading_call("ReviseFixedPriceItem", body, token)
            print(iid, "|", old_title, "→", new_title, "|", str(res)[:120])
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
    print(f"書き換え {done}件（書き換え済みで飛ばした {skipped}件）")
    return done


# ---------------------------------------------------------------- ⑩ 出品中の商品の状態（余った回数で少しずつ）
def check_active_listings(M, per_run=5, settings=None):
    """出品中の商品をAIで見て、状態ランク・気になる点・状態の相場（eBay相場$）を無在庫商品リストに書く。
    2026/11/01 eBayの出品の「状態の説明」もAIのランクに合わせて書き換える。
    問題あり（NG・ジャンク・本体ではない・型番違い）と、状態の相場で利益2,000円の値段だと売れにくいものは出品終了（本人の指示）"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return
    tabs = Tabs(M)
    gem = Gemini(tabs)
    vals = tabs.get("無在庫商品リスト")
    if not vals:
        return
    head = [str(h).strip() for h in vals[0]]
    if "状態ランク（AI）" not in head:
        return
    rc, nc = head.index("状態ランク（AI）") + 1, head.index("気になる点（AI）") + 1
    uc = head.index("eBay相場($)") + 1 if "eBay相場($)" in head else None
    col = {k: head.index(k) for k in ("商品仕入元URL", "eBay出品番号", "状態", "商品名", "仕入れ値(円)", "検索語") if k in head}
    todo = []
    for i, row in enumerate(vals[1:], start=2):
        row = list(row) + [""] * (len(head) - len(row))
        if str(row[col["状態"]]).strip() not in ("active", "") or not str(row[col["eBay出品番号"]]).strip() or str(row[rc - 1]).strip():
            continue
        todo.append((i, row))
    if not todo:
        return
    if settings is None:
        try:
            settings = M.sheet("list").get("settings", {})
        except Exception:
            settings = {}
    token = None
    pw = browser = page = None
    done, cells, flagged, revised = 0, [], [], 0
    stops = []   # 2026/11/01 本人の希望：問題のある出品（AIがNG・ジャンク・本体ではない）は出品終了する
    try:
        for i, row in todo:
            if done >= per_run or not gem.available(4 if per_run < 50 else 2):
                break
            url = str(row[col["商品仕入元URL"]])
            try:
                if "mercari" in url:
                    if page is None:
                        from playwright.sync_api import sync_playwright
                        pw = sync_playwright().start()
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page(locale="ja-JP", user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
                    st, _, _, images, desc = _flea_status(M, page, url)
                    detail = {"images": images, "description": desc,
                              "condition": (page.evaluate(M.DETAIL_JS) or {}).get("condition", "") if st == "on_sale" else ""}
                else:
                    _, _, detail = M.check_yahoo(url)
            except Exception:
                continue
            if not detail or not detail.get("images"):
                continue
            model, title = str(row[col["検索語"]]), str(row[col["商品名"]])
            ans = gem.ask(4 if per_run < 50 else 2,
                          LISTING_PROMPT.format(model=model, title=title, cond=detail.get("condition", ""),
                                                desc=(detail.get("description") or "")[:1500]), detail.get("images", [])[:10])
            if not ans:
                break
            done += 1
            if per_run >= 50:
                time.sleep(4.5)   # 1分15回までの決まりに収める
            rank = str(ans.get("rank") or "").strip().upper()
            note = str(ans.get("note") or ans.get("reason") or "")[:50]
            if rank not in ("S", "A", "B", "C"):
                rank = rank or declared_rank(detail.get("condition", "")) or UNKNOWN_RANK
            raw = None
            slow = ""
            if rank in ("S", "A", "B", "C") and settings:
                try:
                    usd, why = rank_market_usd(M, model, title, rank, settings)
                    raw, _ = rank_market_raw_usd(M, model, title, rank, settings)
                    cost = float(str(row[col["仕入れ値(円)"]]).replace(",", "") or 0)
                    if usd is not None and cost and usd < M.floor_price_usd(cost, model, settings):
                        slow = f"状態{rank}の相場約{int(usd * M.usd_jpy(settings)):,}円では利益2,000円の値段で売れにくい"
                        note = f"【状態的に売れにくい】{rank}相場約{int(usd * M.usd_jpy(settings)):,}円／" + note
                        pass
                except Exception as e:
                    print("状態の相場でエラー:", type(e).__name__, str(e)[:80])
            bad = ans.get("ok") is False or rank == "ジャンク" or re.search(r"本体ではなく|のみの(商品|出品)|パーツのみ|型番違い", note)
            if not bad and slow:   # 2026/11/01 本人の希望：状態的に売れにくい出品も終了する
                stops.append((i, str(row[col["eBay出品番号"]]), slow))
                flagged.append(f"・{title[:30]}（eBay {row[col['eBay出品番号']]}）：{slow} → 出品終了")
            if bad:
                why = str(ans.get("reason") or note)[:40]
                note = f"【問題あり・出品終了】{why}／" + note
                stops.append((i, str(row[col["eBay出品番号"]]), why))
                flagged.append(f"・{title[:30]}（eBay {row[col['eBay出品番号']]}）：{why} → 出品終了")
            cells += [(i, rc, rank), (i, nc, note[:80])]
            if uc and raw:
                cells.append((i, uc, raw))
            # eBayの出品の状態の説明を書き換える（新品は説明を付けられないので触らない）
            if not bad and rank in RANK_EN and "新品" not in detail.get("condition", ""):
                try:
                    token = token or M.get_access_token()
                    text = condition_text_en(rank, str(ans.get("note_en") or ""))
                    M.trading_call("ReviseFixedPriceItem", f"<Item><ItemID>{row[col['eBay出品番号']]}</ItemID>"
                                   f"<ConditionDescription>{_xml(text)}</ConditionDescription></Item>", token)
                    revised += 1
                except Exception as e:
                    print("eBayの状態の説明の書き換えでエラー:", type(e).__name__, str(e)[:100])
            if len(cells) >= 60:
                tabs.set("無在庫商品リスト", cells)
                cells = []
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
        save_mkt()
    if cells:
        tabs.set("無在庫商品リスト", cells)
    if stops:
        token = token or M.get_access_token()
        ups = []
        for i, iid, why in stops:
            ok, msg = M.stop_on_ebay(iid, token)
            ups.append({"row": i, "status": "stopped" if ok else None, "result": f"AIの確認で問題あり（{why}）のため出品を終了 → {msg}"})
        M.sheet("update", updates=ups)
    print(f"出品中の状態の確認：{done}件（eBayの状態の説明を書き換え{revised}件・問題ありで出品終了{len(stops)}件）")
    if flagged and False:   # 2026/11/01 本人の希望：このメールは送らない（結果は無在庫商品リスト・履歴シートに残る）
        M.alert("【AI確認】出品中の商品で問題があったもの", "\n".join(flagged[:40]) +
                "\n\n問題あり（本体ではない・型番違い・故障など）と、状態の相場では利益2,000円の値段で売れにくいものは、出品を終了しました。")


def _xml(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ranks = [None]


RANK_ORDER = {"S": 4, "A": 3, "B": 2, "C": 1, "ジャンク": 0}


def sale_recheck(M, r, title, detail):
    """eBayで売れたときに、仕入れ先の今の写真と説明をAIでもう一度見る（2026/11/01）。
    出品したときより状態が悪く見える・ジャンク・本体なしなどのときだけ注意の文を返す。問題なければ「AI再確認OK」"""
    if not os.environ.get("GEMINI_API_KEY", "").strip() or not (detail or {}).get("images"):
        return ""
    gem = Gemini(Tabs(M))
    if not gem.available(1):
        return ""
    ans = gem.ask(1, LISTING_PROMPT.format(model=r.get("query") or "", title=title, cond=detail.get("condition", ""),
                                           desc=(detail.get("description") or "")[:1500]), detail["images"][:10])
    if not ans:
        return ""
    rank = str(ans.get("rank") or "").strip().upper()
    if rank == "ジャンク".upper():
        rank = "ジャンク"
    before = ai_rank(M, r.get("ebay_item_id", ""))
    note = str(ans.get("note") or ans.get("reason") or "")[:40]
    if ans.get("ok") is False or rank == "ジャンク":
        return f"⚠️AI再確認：買う前に確かめて（{ans.get('reason') or note}・{rank}）。"
    if before in RANK_ORDER and rank in RANK_ORDER and RANK_ORDER[rank] < RANK_ORDER[before]:
        return f"⚠️AI再確認：出品したとき{before}→今は{rank}に見える（{note}）。"
    return f"AI再確認OK（状態{rank or '?'}）。"


def ai_rank(M, ebay_id):
    """商品リストの「状態ランク（AI）」（⑩で記録したもの）"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return ""
    if _ranks[0] is None:
        _ranks[0] = {}
        vals = Tabs(M).get("無在庫商品リスト")
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
    out += [list(r) for r in vals[1:] if len(r) > 2 and str(r[2]) == SOLD_RANK]   # 月1回の「売れた値段」の行は残す
    M.sheet("tab_replace", name=RANKMKT, values=out)


# ---------------------------------------------------------------- フリマ（メルカリ・Yahoo!フリマ）の修理用ジャンク（2026/11/01）
# (検索語, ジャンル, 機種の候補, メルカリのカテゴリ)。1時間ごとに新着1ページだけ見る（自動出品・購入はしない）
FLEA_QUERIES = [   # 2026/11/01 本人の希望でゲーム機（PS Vita以外）を扱わなくなったので、ゲーム機のジャンク探しはやめた
    ("902KC ジャンク", "ガラケー", ["902KC"], ""),
    ("903KC ジャンク", "ガラケー", ["903KC"], ""),
]
FLEA_DETAIL_MAX = 8      # 1回に出品ページを開く（AIで見る）のは8件まで
FLEA_RECHECK_H = 3       # フリマの行は3時間ごとに売り切れを確認
FLEA_RECHECK_MAX = 10


def _mercari_junk(M, page, q, cat):
    url = ("https://jp.mercari.com/search?status=on_sale&sort=created_time&order=desc"
           + (f"&category_id={cat}" if cat else "") + "&keyword=" + requests.utils.quote(q)
           + M.MERCARI_FIXED.split("&item_condition_id")[0])   # 通常出品のみ（状態は全部）
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector('a[href*="/item/m"], a[href*="/shops/product/"]', timeout=25000)
    return [dict(it, src="メルカリ") for it in page.evaluate(M.SEARCH_JS)
            if "/shops/product/" not in it["url"] and not it.get("auction")]


def _yahoo_junk(M, q):
    r = requests.get(f"{M.YAHOO_API}/v1/search", headers=M.YAHOO_HEADERS, timeout=30,
                     params={"query": q, "results": 100, "sort": "price", "order": "asc", "itemStatus": "open"})
    r.raise_for_status()
    return [{"url": f"https://paypayfleamarket.yahoo.co.jp/item/{it['id']}", "title": it.get("title", ""),
             "price": it.get("price"), "seller": it.get("sellerId", ""), "src": "Yahoo!フリマ"}
            for it in r.json().get("items", []) if (it.get("category") or {}).get("id") in M.YAHOO_OK_CATEGORIES]


def _flea_status(M, page, url):
    """(状態, 値段, 出品者, 写真, 説明文)"""
    if "paypayfleamarket" in url:
        st, info, detail = M.check_yahoo(url)
    else:
        st, info = M.check_mercari(page, url)
        detail = page.evaluate(M.DETAIL_JS) if st == "on_sale" else {}
    return st, (info or {}).get("price"), (info or {}).get("seller", ""), (detail or {}).get("images", []), \
        (detail or {}).get("description", "")


def flea_junk(M, data, page):
    """候補さがしの1本目の中で：フリマの修理用ジャンクを探す・フリマの行の売り切れを確認。
    戻り値は run(flea=...) に渡す（新しい候補とメールはヤフオクの回で1通にまとめる）"""
    tabs = Tabs(M)
    settings = data.get("settings", {})
    calc = Calc(settings, setting_map(tabs.get("設定")))
    gem = Gemini(tabs)
    vals = tabs.get(TAB)
    now_dt = datetime.now(JST)
    known = {str(r[COL["ID"] - 1]).strip() for r in vals[1:] if len(r) >= COL["ID"]} | archived_ids(tabs)
    excluded = M.excluded_sellers(data)
    cells, rows, opened = [], [], 0
    load_judged()

    # ---- 今あるフリマの行：売り切れ・削除なら「終了」、値段が変われば更新
    n = 0
    for i, row in enumerate(vals[1:], start=2):
        row = list(row) + [""] * (20 - len(row))
        aid = str(row[COL["ID"] - 1]).strip()
        if str(row[0]).strip() != "検討中" or not aid.startswith("http") or n >= FLEA_RECHECK_MAX:
            continue
        last = to_dt(row[COL["最終更新"] - 1])
        if last and (now_dt - last).total_seconds() < FLEA_RECHECK_H * 3600:
            continue
        n += 1
        try:
            st, price, _, _, _ = _flea_status(M, page, aid)
        except Exception:
            continue
        if st in ("sold", "deleted"):
            cells += [(i, 1, "終了"), (i, COL["最終更新"], now_s())]
        elif st == "on_sale":
            cells.append((i, COL["最終更新"], now_s()))
            if price:
                cells.append((i, COL["現在価格"], price))
        time.sleep(1)

    # ---- 新しいジャンクをさがす
    for q, genre, models, cat in FLEA_QUERIES:
        items = []
        try:
            items += _mercari_junk(M, page, q, cat)
        except Exception as e:
            print("メルカリのジャンク検索に失敗:", q, type(e).__name__)
        try:
            items += _yahoo_junk(M, q.replace(" ジャンク", " 本体 ジャンク") if genre == "ゲーム機" else q)   # 安い順だとソフトばかりになるため
        except Exception as e:
            print("Yahoo!フリマのジャンク検索に失敗:", q, type(e).__name__)
        time.sleep(2)
        for it in items:
            u = it["url"].split("?")[0]
            if u in known or not it.get("price") or len(rows) >= MAX_NEW:
                continue
            t = unicodedata.normalize("NFKC", it.get("title") or "")
            if M.reserved_reason(t) or NOT_ITEM.search(t) or NG_ALWAYS.search(t):
                continue
            if it.get("seller") and M.seller_id(it["seller"]) in excluded:
                continue
            model = next((m for m in models if (M.has_model(t, m) if genre == "ガラケー" else M.match_game(t, m))), None)
            if not model:
                continue
            if M.looks_like_accessory(t) and not any(w in t for w in M.BODY_WORDS):
                continue
            color = color_of(t)
            if color == "複数色":
                continue
            pink = color == "ピンク" and model.upper() in PINK_MODELS
            if NG_UNLESS_REPAIRABLE.search(t) and not pink and genre == "ガラケー":
                continue
            price = int(it["price"])
            # ふるい（AIの前）：外装Cで直した場合の相場から、明らかに高すぎる出品を外す
            sale, why = sale_for(M, tabs, model, color, "C", settings)
            if not sale:
                continue
            rough = calc.cap(sale, 0, calc.parts_default)
            if price > rough:
                continue
            known.add(u)
            if u not in _judged and opened >= FLEA_DETAIL_MAX:
                continue
            images, desc = [], ""
            if u not in _judged:   # 出品ページはAIで見る前に1回だけ開く
                opened += 1
                try:
                    st, p2, seller, images, desc = _flea_status(M, page, u)
                except Exception:
                    continue
                if st != "on_sale":
                    continue
                if seller and M.seller_id(seller) in excluded:
                    continue
                if NG_ALWAYS.search(unicodedata.normalize("NFKC", desc or "")):
                    _judged[u] = [None, "説明文にNGの記載", now_dt.strftime("%Y-%m-%d")]
                    continue
                price = int(p2 or price)
            use = "修理して在庫" if (pink or genre == "ゲーム機" or re.search(r"液晶|画面|割れ|充電", t)) else "部品取り"
            cap, memo, judged = junk_cap(M, tabs, gem, calc, settings,
                                         {"id": u, "title": t, "images": images, "description": desc, "ship": 0,
                                          "auction": False}, model, color, use)
            if judged and (cap is None or price > cap):
                continue
            rows.append(({"状態": "検討中", "src": it["src"], "用途": use, "ジャンル": genre, "機種": model, "色": color,
                          "状態ランク": "ジャンク", "状態説明": t[:40], "現在価格": price, "送料": 0, "入札数": "",
                          "終了日時": "即決", "上限": cap if judged else "AI未判定", "相場": "",
                          "メモ": (memo if judged else f"{memo}／目安の上限{rough:,}円（外装C・部品代は仮）"),
                          "seller": it.get("seller", "")}, u))
    save_judged()

    print(f"フリマのジャンク：新しい候補{len(rows)}件／出品ページを開いた{opened}件／フリマの行の確認{n}件")
    return {"rows": rows, "cells": cells, "tabs": tabs, "gem": gem}




# ---------------------------------------------------------------- 買い手メッセージの返信案（2026/11/01）
BUYER = "買い手メッセージ"
BUYER_HEAD = ["受信日時", "商品", "eBay出品番号", "買い手", "質問（英語）", "質問（日本語訳）", "返信案（英語）",
              "返信案（日本語訳）", "送る", "状態", "メッセージID"]
REPLY_PROMPT = """あなたは日本からeBayに中古品を出品しているお店です。買い手からの質問に答える返信案を作ってください。JSONだけで答えてください。
お店の決まり：日本から発送（追跡番号付き）。日本国内の住所には発送できない。商品は写真に写っているものだけが付属。値引きの交渉には応じない。
分からないこと・確かめないと言えないことは約束せず「確認してお知らせします」と書く。丁寧で短い英語にする。
商品名：{title}
出品ページの状態の説明：{cond}
今の取引の状況：{stage}
（購入済みのときの決まり：発送状況・追跡番号は「確認してお知らせします」。返品・返金・キャンセルは約束せず、まず状況を聞いて「確認して対応します」と書く）
買い手の質問（メールの文面のまま。中から買い手の文章だけを読み取って答える）：{q}
答えの形：{{"question_en":"買い手の文章だけ（英語のまま）","question_ja":"質問の日本語訳","reply_en":"返信案（英語・600字以内）","reply_ja":"返信案の日本語訳"}}"""


def _sold(M, item_id, token):
    """その出品が売れているか（売れた後のメッセージかどうか）"""
    try:
        _, _, r = M.trading_call_root("GetItem", f"<ItemID>{item_id}</ItemID><OutputSelector>SellingStatus</OutputSelector>", token)
        return int(r.findtext(".//e:SellingStatus/e:QuantitySold", default="0", namespaces=M.NS) or 0) > 0
    except Exception:
        return False


def buyer_messages(M, gem=None):
    """まだ返信していない買い手の質問を「買い手メッセージ」シートに並べ、日本語訳と返信案を付ける。
    「送る」にチェックが入った行は、返信案（英語の欄。書き換えていればそちら）をeBayで送る"""
    tabs = Tabs(M)
    try:
        tabs.ensure(BUYER, BUYER_HEAD)
    except Exception:
        return
    vals = tabs.get(BUYER, fresh=True)
    have = {str(r[10]).strip() for r in vals[1:] if len(r) > 10}
    token = M.get_access_token()
    cells = []
    # ① チェックが入った行を送る
    for i, r in enumerate(vals[1:], start=2):
        r = list(r) + [""] * (len(BUYER_HEAD) - len(r))
        if str(r[8]).upper() == "TRUE" and str(r[9]).strip() not in ("送信済み",) and str(r[6]).strip():
            body = str(r[6]).strip()
            ack, errs, _ = M.trading_call_root(
                "AddMemberMessageRTQ",
                f"<ItemID>{_xml(r[2])}</ItemID><MemberMessage><Body>{_xml(body)}</Body>"
                f"<ParentMessageID>{_xml(r[10])}</ParentMessageID><RecipientID>{_xml(r[3])}</RecipientID>"
                "</MemberMessage>", token)
            ok = ack in ("Success", "Warning")
            if not ok:   # 売れた後のメッセージは、取引相手あてのメッセージとして送る
                ack, errs2, _ = M.trading_call_root(
                    "AddMemberMessageAAQToPartner",
                    f"<ItemID>{_xml(r[2])}</ItemID><MemberMessage><Body>{_xml(body)}</Body><QuestionType>General</QuestionType>"
                    f"<RecipientID>{_xml(r[3])}</RecipientID><Subject>Re: {_xml(str(r[1])[:50])}</Subject></MemberMessage>", token)
                ok = ack in ("Success", "Warning")
                errs = errs + errs2
            cells.append((i, 10, "送信済み" if ok else f"送れませんでした：{' / '.join(errs)[:80]}"))
    # ② 新しい質問を取ってくる（3日以内・未返信）
    now = datetime.now(JST)
    try:
        ack, errs, root = M.trading_call_root(
            "GetMemberMessages",
            "<MailMessageType>AskSellerQuestion</MailMessageType><MessageStatus>Unanswered</MessageStatus>"
            f"<StartCreationTime>{(now - timedelta(days=3)).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</StartCreationTime>"
            f"<EndCreationTime>{now.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</EndCreationTime>", token)
    except Exception as e:
        print("買い手メッセージの取得でエラー:", type(e).__name__, str(e)[:80])
        root = None
    rows = []
    ns = M.NS
    for ex in (root.findall(".//e:MemberMessageExchange", ns) if root is not None else []):
        mid = ex.findtext("e:Question/e:MessageID", default="", namespaces=ns)
        if not mid or mid in have:
            continue
        item_id = ex.findtext("e:Item/e:ItemID", default="", namespaces=ns)
        title = ex.findtext("e:Item/e:Title", default="", namespaces=ns)
        sender = ex.findtext("e:Question/e:SenderID", default="", namespaces=ns)
        q = ex.findtext("e:Question/e:Body", default="", namespaces=ns)
        when = ex.findtext("e:CreationDate", default="", namespaces=ns) or ex.findtext("e:Question/e:CreationDate", default="", namespaces=ns)
        cond = ""
        try:
            _, _, ir = M.trading_call_root("GetItem", f"<ItemID>{item_id}</ItemID><OutputSelector>ConditionDescription</OutputSelector>", token)
            cond = ir.findtext(".//e:ConditionDescription", default="", namespaces=ns) or ""
        except Exception:
            pass
        sold = _sold(M, item_id, token)
        gem = gem or Gemini(tabs)
        ans = gem.ask(1, REPLY_PROMPT.format(title=title, cond=cond[:600], q=q[:1500],
                                             stage="購入済み（売れた後）" if sold else "購入前の質問")) if gem.available(1) else None
        d = to_dt(when)
        rows.append([d.astimezone(JST).strftime("%Y/%m/%d %H:%M") if d else now.strftime("%Y/%m/%d %H:%M"),
                     ("【購入済み】" if sold else "") + title[:60], item_id, sender,
                     q[:1500], (ans or {}).get("question_ja", "（AIが使えないため訳なし）"),
                     (ans or {}).get("reply_en", ""), (ans or {}).get("reply_ja", ""), False, "未送信", mid])
        have.add(mid)
    # ②' 売れた後のメッセージ（受信箱。質問の形でないものも）2026/11/01
    replied = set()
    try:
        _, _, hr = M.trading_call_root(
            "GetMyMessages", "<DetailLevel>ReturnHeaders</DetailLevel><FolderID>0</FolderID>"
            f"<StartTime>{(now - timedelta(days=3)).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</StartTime>"
            f"<EndTime>{now.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</EndTime>", token)
        heads = []
        for m in hr.findall(".//e:Messages/e:Message", ns):
            g = lambda k: m.findtext("e:" + k, default="", namespaces=ns)
            ext, my = g("ExternalMessageID"), "my:" + g("MessageID")
            if g("Replied") == "true":
                replied |= {ext, my}
                continue
            if g("Sender").lower() == "ebay" or not g("ItemID") or ext in have or my in have:
                continue
            heads.append((g("MessageID"), ext, my, g("ItemID"), g("Sender"), g("Subject"), g("ReceiveDate")))
        print(f"受信箱：{len(hr.findall('.//e:Messages/e:Message', ns))}件（返信まだ・商品あり {len(heads)}件）")
        for k in range(0, len(heads), 10):
            part = heads[k:k + 10]
            _, _, br = M.trading_call_root(
                "GetMyMessages", "<DetailLevel>ReturnMessages</DetailLevel><MessageIDs>"
                + "".join(f"<MessageID>{x[0]}</MessageID>" for x in part) + "</MessageIDs>", token)
            texts = {m.findtext("e:MessageID", default="", namespaces=ns): m.findtext("e:Text", default="", namespaces=ns)
                     for m in br.findall(".//e:Messages/e:Message", ns)}
            for mid0, ext, my, item_id, sender, subj, when in part:
                raw = re.sub(r"<style.*?</style>|<script.*?</script>", " ", texts.get(mid0, ""), flags=re.S)
                q = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:3000]
                if not q:
                    continue
                sold = _sold(M, item_id, token)
                if not sold:
                    continue   # 購入前の質問は上（②）で取っている
                gem = gem or Gemini(tabs)
                ans = gem.ask(1, REPLY_PROMPT.format(title=subj, cond="", q=q, stage="購入済み（売れた後）")) if gem.available(1) else None
                d = to_dt(when)
                rows.append([d.astimezone(JST).strftime("%Y/%m/%d %H:%M") if d else now.strftime("%Y/%m/%d %H:%M"),
                             "【購入済み】" + subj[:60], item_id, sender,
                             str((ans or {}).get("question_en") or q)[:1500], (ans or {}).get("question_ja", "（AIが使えないため訳なし）"),
                             (ans or {}).get("reply_en", ""), (ans or {}).get("reply_ja", ""), False, "未送信", ext or my])
                have |= {ext, my}
    except Exception as e:
        print("売れた後のメッセージの取得でエラー:", type(e).__name__, str(e)[:100])
    if cells:
        tabs.set(BUYER, cells)
    if rows:   # 「送る」のチェック欄があると最終行の判定がずれるので、A列が空の最初の行から書く
        cur = tabs.get(BUYER, fresh=True)
        start = next((i for i, r in enumerate(cur[1:], start=2) if not (r and str(r[0]).strip())), len(cur) + 1)
        tabs.set(BUYER, [(start + k, c, v) for k, r in enumerate(rows) for c, v in enumerate(r, start=1)])
    # ③ 返信が済んだ行（ここから送った・eBayで直接返信した）は消す（2026/11/01 本人の希望）
    try:
        answered = set()
        _, _, ar = M.trading_call_root(
            "GetMemberMessages",
            "<MailMessageType>AskSellerQuestion</MailMessageType><MessageStatus>Answered</MessageStatus>"
            f"<StartCreationTime>{(now - timedelta(days=7)).astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</StartCreationTime>"
            f"<EndCreationTime>{now.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</EndCreationTime>", token)
        for ex in ar.findall(".//e:MemberMessageExchange", ns):
            answered.add(ex.findtext("e:Question/e:MessageID", default="", namespaces=ns))
        answered |= replied
        cur = tabs.get(BUYER, fresh=True)
        body = [list(r) + [""] * (len(BUYER_HEAD) - len(r)) for r in cur[1:] if any(str(c).strip() for c in r)]
        left = [r for r in body if str(r[9]).strip() != "送信済み" and str(r[10]).strip() not in answered]
        if len(left) < len(body):
            out = []
            for i, r in enumerate(left, start=2):
                out += [(i, c, (str(v).upper() == "TRUE" if c == 9 else v)) for c, v in enumerate(r[:len(BUYER_HEAD)], start=1)]
            for i in range(len(left) + 2, len(body) + 2):
                out += [(i, c, (False if c == 9 else "")) for c in range(1, len(BUYER_HEAD) + 1)]
            tabs.set(BUYER, out)
            print(f"買い手メッセージ：返信済みの{len(body) - len(left)}件を消しました")
    except Exception as e:
        print("返信済みの行を消す処理でエラー:", type(e).__name__, str(e)[:80])
    print(f"買い手メッセージ：新しい質問{len(rows)}件／送信{sum(1 for c in cells if c[2] == '送信済み')}件")



# ---------------------------------------------------------------- 候補の商品名をAIでまとめて確かめる（2026/11/01）
TITLE_MODEL = "gemini-3.1-flash-lite"   # 状態の確認（3.5 Flash Lite）とは別の無料枠を使う
TITLE_PROMPT = """日本のフリマの出品の商品名です。eBayで「{kind}の本体（動くもの）」として転売してよいかを判定してください。
外すもの：本体以外（ケース・カバー・電池だけ・充電器だけ・説明書・箱だけ・部品・基盤・液晶だけ・きせかえプレート等）、
ジャンク・故障・訳あり・動作未確認、複数台のまとめ売り、型番が「{q}」と違うもの、予約・専用出品。迷うものは外す。
JSONだけで答える：{{"ng":[外す番号の配列],"why":{{"番号":"理由（10字以内）"}}}}
{lines}"""


def ai_title_filter(M, found, batch=30):
    """キーワードの除外を通った候補の商品名を、AIでまとめて（30件ずつ1回）確かめる。キーが無い・枠が無いときはそのまま"""
    if not found or not os.environ.get("GEMINI_API_KEY", "").strip():
        return found
    gem = Gemini(Tabs(M))
    gem._load()
    gem.model, gem.cell, gem.used, gem.cap = TITLE_MODEL, None, 0, 450
    keep, dropped = [], 0
    by_q = {}
    for x in found:
        by_q.setdefault(x["query"], []).append(x)
    for q, xs in by_q.items():
        gi = M.game_info(q) or {}
        kind = {"camera": "デジカメ", "md": "MDプレーヤー", "cassette": "カセットプレーヤー", "cd": "CDプレーヤー",
                "video": "ビデオカメラ", "watch": "腕時計"}.get(gi.get("kind"), "ゲーム機" if gi else "携帯電話")
        for i in range(0, len(xs), batch):
            part = xs[i:i + batch]
            ans = gem.ask(1, TITLE_PROMPT.format(kind=kind, q=q, lines="\n".join(f"{n}. {x['title'][:80]}" for n, x in enumerate(part))))
            if ans is None:
                keep += part
                continue
            ng = {int(n) for n in (ans.get("ng") or []) if str(n).isdigit()}
            for n, x in enumerate(part):
                if n in ng:
                    dropped += 1
                else:
                    keep.append(x)
    print(f"商品名のAI確認：{len(found)}件のうち{dropped}件を外しました")
    return keep
