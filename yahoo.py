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
DEVICES = "ジャンク品在庫"   # 2026/11/01 名前を変更（旧：手元在庫・端末管理）。落札・購入した端末はここで管理する
# 2026/11/1 本人の希望：A列に「終了」のチェック（終わった行はグレー＋取り消し線）。ツールはA列を飛ばして読む（dev_rows）
PARTS = "部品表"
REPAIR = "修理部品表"   # 2026/11/01 機種×不具合×部品・値段・購入リンク（部品代はここの値段を使う）
JUDGED_FILE = os.path.join(".cache", "ai_judged.json")   # AIで見た出品（1件1回だけ判定する）
RANKMKT = "状態別相場"
HEADERS = {"User-Agent": "stock-sync/1.0 (+https://github.com/inaba-tools/stock-sync)"}
DUTY = 12.5
# 出品の「商品の状態」→ 状態ランク（AIを使わずに決める）
COND_RANK = {"NEW": "S", "USED10": "S", "USED20": "A", "USED40": "B", "USED60": "C", "USED80": "C"}
NOT_ITEM = re.compile(r"ケースのみ|カバーのみ|充電器のみ|箱のみ|説明書のみ|モック|ダミー|電池パックのみ|バッテリーのみ|"
                      r"フィルム|保護|互換|部品のみ|パーツのみ|基板のみ|外装のみ|ジャケット|シール|\d{3,4}\s*mAh|容量\+")
PINK_MODELS = ("902KC", "903KC")


def now_s():
    return datetime.now(JST).strftime("%Y/%m/%d %H:%M")   # 2026/11/1 本人の希望：日付は全部 2026/11/01 22:51 の形


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
        self.min_profit = float(settings.get("min_profit") or sget(smap, "最低の利益額") or 2000)
        self.parts_default = sget(smap, "ジャンクの部品代の目安", 3000)
        self.adj = float(settings.get("market_adj") or 80) / 100
        self.rate = 1 - (self.fee + self.other + DUTY) / 100

    def net(self, sale_jpy):
        return sale_jpy * self.rate - self.ship

    def profit(self, sale_jpy, total_cost):
        return round(self.net(sale_jpy) - total_cost)

    def cap(self, sale_jpy, shipping, parts=0):
        """利益が最低利益だけ残る仕入れ値の上限"""
        return int((self.net(sale_jpy) - self.min_profit - shipping - parts) // 10 * 10)


# ---------------------------------------------------------------- ヤフオクを読む




def _num_or_none(v):
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None




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


def _mkey(m):
    return re.sub(r"[-\s]", "", str(m or "").upper())


SOLD_MIN = 3   # 2026/11/01 本人決定：90日で売れたのがこれ未満の機種は、無在庫でもヤフオクでも扱わない


def sold_for(tabs, model, color):
    """状態別相場シートの「売れた値段」の行 → {"安い","ふつう","高い","件数"}（円）。行が無い・古いなら None。
    色の行（902KCのピンクなど）があればそれ、無ければ色を問わない行（ピンクなど高い色を除いた値段）。
    件数が SOLD_MIN 未満の行は {"件数": n} だけ返す（＝売れていない機種）"""
    mm = _mkey(market_model(model))
    try:
        rows = [r for r in tabs.get(RANKMKT)[1:] if len(r) >= 8 and _mkey(r[0]) == mm and str(r[2]) == SOLD_RANK]
    except Exception:
        return None
    for want in ([color, ""] if color else [""]):
        for r in rows:
            if str(r[1]) != want:
                continue
            d = to_dt(r[7])
            if not d or (datetime.now(JST) - d).days > SOLD_MAX_DAYS:
                return None
            n = int(float(r[6] or 0)) if str(r[6]).strip() else 0
            if n < SOLD_MIN:
                return {"件数": n}
            try:
                return {"安い": float(r[3]), "ふつう": float(r[4]), "高い": float(r[5]), "件数": n}
            except (TypeError, ValueError):
                return None
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
    if sold["件数"] < SOLD_MIN:   # eBayでほとんど売れていない機種は扱わない
        return None, f"eBayで90日に{sold['件数']}台しか売れていない"
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
















def mark_listed_junk(tabs):
    """2026/11/1 本人の希望：ジャンク品在庫はジャンクだけ。eBayに出品した端末（直して出したもの）は自動で「終了」にチェック。
    目印：無在庫商品リストの手元在庫の行にその管理番号がある、または状態の欄に「eBay出品中」とある"""
    dev = tabs.get(DEVICES, fresh=True)
    if len(dev) < 2:
        return
    try:
        listed_txt = " ".join(" ".join(str(c) for c in r) for r in tabs.get("無在庫商品リスト")[1:]
                              if any(str(c).strip().startswith("手元在庫") for c in r))
    except Exception:
        listed_txt = ""
    cells = []
    for i, r in enumerate(dev[1:], start=2):
        r = list(r) + [""] * 12
        if str(r[0]).strip().upper() == "TRUE":
            continue
        no = str(r[1]).strip()
        if not re.match(r"K-\d+", no):
            continue
        if re.search(rf"{re.escape(no)}(?!\d)", listed_txt) or "eBay出品中" in str(r[8]):
            cells.append((i, 1, True))
    if cells:
        tabs.set(DEVICES, cells)
        print(f"ジャンク品在庫：eBayに出品済みの{len(cells)}台を終了に")






# ---------------------------------------------------------------- 落札したら記録


def dev_rows(dev):
    """ジャンク品在庫の2行目以降を、A列（終了のチェック）を外した形で返す（管理番号が先頭）"""
    return [list(r[1:]) for r in (dev or [])[1:]]


def next_device_no(dev):
    n = 0
    for row in dev_rows(dev):
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
        if not str(row[col["商品仕入元URL"]]).strip().startswith("http"):
            continue   # 2026/11/01 手元在庫（仕入れ先なし）はAIの確認・自動終了の対象にしない
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


# ---------------------------------------------------------------- ジャンク組み合わせ（2026/11/01 作り直し）
# 本人の希望：フリマ（メルカリ・Yahoo!フリマ）のジャンクを1台2,000円以下で集め、部品ごとの良い・悪いをAIで見て、
# 「単品で直る」「この2台で1台にできる」を見込み利益つきで「ジャンク組み合わせ」シートに提案する（購入は本人がする）。
# ヤフオクの入札の仕組みは同じ日にやめて削除した。ネットワーク利用制限（赤ロム）は解除できるものがあるので外さない。
COMBO = "ジャンク組み合わせ"
COMBO_HEAD = ["提案", "組", "役割", "見込み利益", "機種", "色", "値段", "外装ランク", "良い部品", "悪い部品",
              "メモ", "リンク", "出品元", "見つけた日時", "最終確認", "ID"]
CI = {k: i for i, k in enumerate(COMBO_HEAD)}
# 機種 → 商品名の書き方（型番か愛称のどちらかが入っていればその機種）。2機種目はここに足す
JUNK_MODELS = {"903KC": ["903KC", "DIGNOケータイ3", "ディグノケータイ3"], "902KC": ["902KC"],   # 2026/11/01 本人OK：3機種追加
               "A202KC": ["A202KC"], "KY-42C": ["KY-42C"],
               "601SH": ["601SH"], "602SH": ["602SH"], "A203KC": ["A203KC"]}   # 2026/11/01 本人OK：3機種追加
# 部品が共通で、組み合わせてよい機種のまとまり。ここに無い機種は同じ機種同士だけ
# 902KC/903KC：修理部品表のメモ。A202KC/A203KC：DIGNO ケータイ4の会社違い。601SH/602SH：AQUOS ケータイ2の会社違い（2026/11/01 調べ）
# KY-42CもDIGNO ケータイ4のドコモ版だが外装のデザインが違うので単独
PART_GROUPS = [{"902KC", "903KC"}, {"A202KC", "A203KC"}, {"601SH", "602SH"}]


def _same_parts(a, b):
    return a == b or any(a in g and b in g for g in PART_GROUPS)
JUNK_MAX = 3000            # 探す範囲の上限（送料込み）。2026/11/01 本人の希望で2,000→3,000円（2,000円に値下げ交渉できるかもしれないため）
JUNK_BUY = 2000            # 実際に買う値段の上限（3,000円までの出品は2,000円に値下げしてもらえた前提で利益を計算）。設定シートに「ジャンクの上限」があればそちら
PINK_JUNK_MAX = 5000       # 2026/11/01 本人の希望：ピンクの902KC・903KCのジャンクだけ上限5,000円（設定「ピンクのジャンクの上限」があればそちら）
PINK_SEEN_FILE = os.path.join(".cache", "pink_seen.json")   # ピンクのお知らせを送った出品（1つの出品につき1回だけ）
JUNK_PROFIT_MIN = 2000     # 提案する見込み利益の下限（ジャンクの利益ライン）
PART_NAMES = ["外装（表）", "外装（裏）", "メイン液晶", "サブ液晶", "基板", "キー", "ヒンジ", "充電口", "バッテリー", "カメラ"]
EXTERIOR = ("外装（表）", "外装（裏）")
FLEA_DETAIL_MAX = 8        # 1回にAIで見る新しい出品は8件まで（無料枠）
FLEA_RECHECK_H = 3         # シートの行は3時間ごとに売り切れを確認
FLEA_RECHECK_MAX = 15

COMBO_PROMPT = """あなたはガラケーの修理のプロです。日本のフリマに出ているジャンク品（{model}）の写真と説明文を見て、部品ごとの状態をJSONだけで答えてください。
2台のジャンクの部品を組み合わせて1台に直すための判定です。
タイトル：{title}
説明文：{desc}
部品：{parts}
各部品は "良"（写真や説明で問題なさそう）・"悪"（壊れている・割れ・映らない・欠品）・"不明" のどれか。
・基板：電源が入り起動するか。電源が入らない・再起動を繰り返す・水没は "悪"。電池の問題だけと分かる場合は "不明"。
・ネットワーク利用制限・赤ロム・SIMロックは部品の故障ではないので判定に入れない（noteに書く）。
・バッテリー：膨らみ・持ちが悪い・電池なしは "悪"。
答えの形：{{"is_body":true/false（本体が写っていて本体の出品か）,"color_in_photo":"写真の本体の色（日本語）",
"parts":{{"外装（表）":"良/悪/不明", ...全部の部品}},"exterior_rank":"S/A/B/C（外装のキズ・色あせ・割れ）","note":"気になる点を30字以内"}}"""


def _mercari_junk(M, page, q, cat, price_max):
    url = ("https://jp.mercari.com/search?status=on_sale&sort=created_time&order=desc"
           + (f"&category_id={cat}" if cat else "") + "&keyword=" + requests.utils.quote(q)
           + (f"&price_max={int(price_max)}" if price_max else "") + M.MERCARI_FIXED.split("&item_condition_id")[0])   # 通常出品のみ（状態は全部）
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_selector('a[href*="/item/m"], a[href*="/shops/product/"]', timeout=25000)
    except Exception:
        return []   # 0件
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
    if (info or {}).get("reviews") == 0:   # 2026/11/01 取引実績のない出品者は候補にしない
        st = "no_record"
    return st, (info or {}).get("price"), (info or {}).get("seller", ""), (detail or {}).get("images", []), \
        (detail or {}).get("description", "")


def _model_of(title):
    t = re.sub(r"\s", "", unicodedata.normalize("NFKC", title or "")).upper()
    t_alnum = re.sub(r"[-‐－―]", "", t)   # KY-42C と KY42C を同じに扱う
    for m, names in JUNK_MODELS.items():
        for n in names:
            n2 = re.sub(r"\s", "", unicodedata.normalize("NFKC", n)).upper()
            if re.fullmatch(r"[-0-9A-Z]+", n2):
                if re.search(r"(?<![0-9])" + re.escape(n2.replace("-", "")) + r"(?![A-Z0-9])", t_alnum):
                    return m
            elif n2 in t:
                return m
    return None


def _split(v):
    return [x for x in re.split(r"[・、,\s]+", str(v or "")) if x]


def _unit(row):
    row = list(row) + [""] * (len(COMBO_HEAD) - len(row))
    u = {k: row[i] for k, i in CI.items()}
    u["good"], u["bad"] = set(_split(u["良い部品"])), set(_split(u["悪い部品"]))
    u["price"] = num(u["値段"]) or 0
    u["rank"] = (str(u["外装ランク"]).strip().upper()[:1] or "C") if str(u["外装ランク"]).strip()[:1] in "SABCsabc" else "C"
    return u


def _plans(M, tabs, calc, settings, units):
    """直し方の案（単品・2台）を全部出して、見込み利益の高い順。1台は1つの案にだけ使う"""
    buy_cache = {}

    def buy(model, p):   # 修理部品表で買える部品なら値段、無ければ None
        k = (model, p)
        if k not in buy_cache:
            price, note = parts_cost(tabs, model, [p], calc)
            buy_cache[k] = None if note else price
        return buy_cache[k]

    def profit(model, color, rank, cost):
        sale, why = sale_for(M, tabs, model, color, rank, settings)
        if not sale:
            return None, why
        return calc.profit(sale, cost), why

    plans = []
    for b in units:
        if "基板" in b["bad"]:
            continue
        # 単品：悪い部品が全部、修理部品表で買える
        cost_parts = [buy(b["機種"], p) for p in b["bad"]]
        if all(c is not None for c in cost_parts):
            pf, why = profit(b["機種"], b["色"], b["rank"], b.get("buy", b["price"]) + sum(cost_parts))
            if pf is not None:
                plans.append({"kind": "単品", "units": [b], "profit": pf, "color": b["色"], "rank": b["rank"],
                              "memo": ("部品" + "・".join(sorted(b["bad"])) + f"を買う（{int(sum(cost_parts)):,}円）"
                                       if b["bad"] else "悪い部品なし（動作の確認を）") + f"／{why}"})
        # 2台：悪い部品は相手の良い部品か、買える部品で埋める
        for d in units:
            if d is b or not _same_parts(d["機種"], b["機種"]):
                continue
            take, bought, ok = [], 0, True
            for p in b["bad"]:
                if p in d["good"]:
                    take.append(p)
                else:
                    c = buy(b["機種"], p)
                    if c is None:
                        ok = False
                        break
                    bought += c
            options = []
            if ok and take:
                ext_from_d = any(p in EXTERIOR for p in take)
                options.append((take, d if ext_from_d else b))
            # 外装を相手のもの（ピンクなど）に入れ替える案（相手の外装がどちらも良のとき）
            if ok and all(p in d["good"] for p in EXTERIOR) and d["色"] != b["色"]:
                options.append((sorted(set(take) | set(EXTERIOR)), d))
            for tk, ext in options:
                cost = b.get("buy", b["price"]) + d.get("buy", d["price"]) + bought
                pf, why = profit(b["機種"], ext["色"], ext["rank"], cost)
                if pf is None:
                    continue
                plans.append({"kind": "2台", "units": [b, d], "profit": pf, "color": ext["色"], "rank": ext["rank"],
                              "memo": f"部品取りから{'・'.join(tk)}を移す" + (f"＋部品{int(bought):,}円" if bought else "")
                                      + f"→{ext['色']}・外装{ext['rank']}／{why}"})
    plans.sort(key=lambda x: -x["profit"])
    used, chosen = set(), []
    for p in plans:
        ids = [u["ID"] for u in p["units"]]
        if p["profit"] < JUNK_PROFIT_MIN or any(i in used for i in ids):
            continue
        used.update(ids)
        chosen.append(p)
    return chosen


def junk_combo(M, data, page):
    """候補さがしの1本目の中で：フリマのジャンクを集めてシートの行を更新し、組み合わせを作り直す"""
    tabs = Tabs(M)
    settings = data.get("settings", {})
    smap = setting_map(tabs.get("設定"))
    calc = Calc(settings, smap)
    gem = Gemini(tabs)
    limit = sget(smap, "ジャンクの上限", JUNK_MAX) or JUNK_MAX
    pink_limit = sget(smap, "ピンクのジャンクの上限", PINK_JUNK_MAX) or PINK_JUNK_MAX

    buy_limit = sget(smap, "ジャンクの買う上限", JUNK_BUY) or JUNK_BUY
    room = max(0, limit - buy_limit)   # 値下げ交渉の余地（探す範囲−買う上限＝1,000円）
    # 2026/11/02 本人と決定：機種ごとに「eBayで売れた値段（安い側）で、2台買っても利益2,000円残る1台の値段」を買う上限にする
    # （設定の買う上限より下にはしない）。探す範囲はそれ＋交渉の余地。売れた値段は月1回更新されるので自動で追従する
    buy_by_model = {}
    for m in JUNK_MODELS:
        b = buy_limit
        try:
            sold = sold_for(tabs, m, "")
            if sold and sold.get("安い"):
                b = max(buy_limit, int((calc.net(sold["安い"]) - JUNK_PROFIT_MIN) / 2 // 1000 * 1000))   # 2026/11/02 本人の希望：1,000円単位で切り捨て
        except Exception:
            pass
        buy_by_model[m] = b
    print("ジャンクの買う上限（機種ごと）:", buy_by_model)

    def is_pink(model, color):
        return color == "ピンク" and str(model).upper() in PINK_MODELS

    def buy_of(model, color):
        return pink_limit if is_pink(model, color) else buy_by_model.get(model, buy_limit)

    def lim(model, color):
        return buy_of(model, color) + room   # 2026/11/02 本人の希望：ピンクも交渉の余地つき（買う5,000円・探す6,000円）
    tabs.ensure(COMBO, COMBO_HEAD)
    vals = tabs.get(COMBO, fresh=True)
    units = [_unit(r) for r in vals[1:] if len(r) > CI["ID"] and str(r[CI["ID"]]).strip().startswith("http")]
    now_dt = datetime.now(JST)
    excluded = M.excluded_sellers(data)
    load_judged()
    known = {u["ID"] for u in units} | set(_judged)

    # ---- 今ある行：売り切れ・削除・値上がりした出品は消す
    n, keep = 0, []
    for u in units:
        last = to_dt(u["最終確認"])
        if n < FLEA_RECHECK_MAX and not (last and (now_dt - last).total_seconds() < FLEA_RECHECK_H * 3600):
            n += 1
            try:
                st, price, _, _, _ = _flea_status(M, page, u["ID"])
            except Exception:
                st, price = "error", None
            if st in ("sold", "deleted", "no_record"):
                continue
            if st == "on_sale":
                u["最終確認"] = now_s()
                if price:
                    u["price"] = int(price)
                    if u["price"] > lim(u["機種"], u["色"]):
                        continue
            time.sleep(1)
        keep.append(u)
    units = keep

    # ---- 新しいジャンクをさがす（機種名・愛称で、2,000円以下。ピンクの902KC・903KCは値段に関係なく探してお知らせ）
    opened, added = 0, 0
    try:
        with open(PINK_SEEN_FILE) as f:
            pink_seen = set(json.load(f))
    except (OSError, ValueError):
        pink_seen = set()
    pink_new = []
    for model, names in JUNK_MODELS.items():
        items = []
        qs = [(q, lim(model, "")) for q in names]
        if model.upper() in PINK_MODELS:
            qs += [(f"{q} ピンク", None) for q in names]   # ピンクは上限なしで探す（お知らせ用）
        for q, pmax in qs:
            try:
                items += _mercari_junk(M, page, q, "", pmax)
            except Exception as e:
                print("メルカリのジャンク検索に失敗:", q, type(e).__name__)
            try:
                items += _yahoo_junk(M, q)
            except Exception as e:
                print("Yahoo!フリマのジャンク検索に失敗:", q, type(e).__name__)
            time.sleep(2)
        for it in items:
            u = it["url"].split("?")[0]
            if not it.get("price"):
                continue
            t = unicodedata.normalize("NFKC", it.get("title") or "")
            if _model_of(t) != model or M.reserved_reason(t) or NOT_ITEM.search(t):
                continue
            if it.get("seller") and M.seller_id(it["seller"]) in excluded:
                continue
            if M.looks_like_accessory(t) and not any(w in t for w in M.BODY_WORDS):
                continue
            color = color_of(t)
            if color == "ピンク" and model.upper() in PINK_MODELS and u not in pink_seen:
                pink_seen.add(u)
                pink_new.append(f"・[{it['src']}] {model} {int(it['price']):,}円 {t[:40]}\n  {u}")
            if u in known or int(it["price"]) > lim(model, color):
                continue
            if color == "複数色" or opened >= FLEA_DETAIL_MAX:
                continue
            known.add(u)
            opened += 1
            try:
                st, p2, seller, images, desc = _flea_status(M, page, u)
            except Exception:
                known.discard(u)
                continue
            if st != "on_sale" or (seller and M.seller_id(seller) in excluded):
                continue
            ans = gem.ask(2, COMBO_PROMPT.format(model=model, title=t, desc=(desc or "")[:1500], parts="・".join(PART_NAMES)),
                          images[:10])
            if ans is None:   # キー待ち・今日の回数を使い切り → 次の回に見る
                known.discard(u)
                opened = FLEA_DETAIL_MAX
                continue
            if not ans.get("is_body", True):
                _judged[u] = [None, "本体ではない", now_dt.strftime("%Y-%m-%d")]
                continue
            parts = ans.get("parts") or {}
            good = [p for p in PART_NAMES if parts.get(p) == "良"]
            bad = [p for p in PART_NAMES if parts.get(p) == "悪"]
            rank = str(ans.get("exterior_rank") or "C").upper()[:1]
            units.append(_unit([""] * CI["機種"] + [model, ans.get("color_in_photo") and color_of(ans["color_in_photo"]) or color,
                                int(p2 or it["price"]), rank if rank in "SABC" else "C", "・".join(good), "・".join(bad),
                                str(ans.get("note") or "")[:40], "", it["src"], now_s(), now_s(), u]))
            added += 1
    save_judged()
    if pink_new:   # 2026/11/01 本人の希望：ピンクの902KC・903KCが出たら、ジャンクでも値段に関係なくすぐメール
        M.alert(f"【ピンク】902KC・903KCが{len(pink_new)}件出ました", "\n".join(pink_new)
                + "\n\n購入はご自身で（自動では買いません）。ジャンクは5,000円以下ならジャンク組み合わせシートにも入ります。")
    try:
        os.makedirs(".cache", exist_ok=True)
        with open(PINK_SEEN_FILE, "w") as f:
            json.dump(sorted(pink_seen)[-3000:], f)
    except OSError:
        pass

    # ---- 組み合わせを作り直してシートを書き直す
    for u in units:
        u["提案"], u["組"], u["役割"], u["見込み利益"], u["plan_memo"] = "", "", "", "", ""
        pink = u["色"] == "ピンク" and str(u["機種"]).upper() in PINK_MODELS
        u["buy"] = min(u["price"], buy_of(u["機種"], u["色"]))
    plans = _plans(M, tabs, calc, settings, units)
    out, used = [], set()
    for k, p in enumerate(plans, start=1):
        for j, u in enumerate(p["units"]):
            used.add(u["ID"])
            u["提案"] = "◎単品で直る" if p["kind"] == "単品" else "○2台で1台"
            u["組"] = f"組{k}"
            u["役割"] = "本体" if j == 0 else "部品取り"
            u["見込み利益"] = p["profit"] if j == 0 else ""
            u["plan_memo"] = p["memo"] if j == 0 else ""
            out.append(u)
    rest = [u for u in units if u["ID"] not in used]
    for u in rest:
        u["提案"] = "相手待ち" if "基板" not in u["bad"] else "部品取り用"
    rest.sort(key=lambda u: (u["提案"] != "相手待ち", u["機種"], u["price"]))
    rows = [COMBO_HEAD]
    for r, u in enumerate(out + rest, start=2):
        label = u["出品元"] or ("Yahoo!フリマ" if "paypay" in u["ID"] else "メルカリ")
        nego = f"要交渉→{int(u['buy']):,}円" if u["buy"] < u["price"] else ""
        memo = " ／ ".join(x for x in (nego, u.get("plan_memo"), str(u["メモ"]).split(" ／ ")[-1] if u["メモ"] else "") if x)
        rows.append([u["提案"], u["組"], u["役割"], u["見込み利益"], u["機種"], u["色"], u["price"], u["rank"],
                     "・".join(p for p in PART_NAMES if p in u["good"]), "・".join(p for p in PART_NAMES if p in u["bad"]),
                     memo, f'=HYPERLINK(P{r},"{label}")', label, u["見つけた日時"], u["最終確認"] or now_s(), u["ID"]])
    M.sheet("tab_replace", name=COMBO, values=rows)
    try:
        mark_listed_junk(tabs)
    except Exception as e:
        print("ジャンク品在庫の整理でエラー:", type(e).__name__, str(e)[:100])
    msg = (f"{now_s()} ジャンク組み合わせ：{len(units)}台（新しく{added}台）／提案{len(plans)}件／"
           f"Gemini {'キー待ち' if not gem.key else f'今日{gem.used}回'}")
    print(msg)
    try:
        M.sheet("log", message=msg)
    except Exception:
        pass


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
