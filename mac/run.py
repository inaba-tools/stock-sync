"""月1回の「eBayで売れた値段の更新」をMacだけで行う（2026/11/10 本人の希望：Claudeの予定作業から置き換え）。
launchd（毎月1日 19:47）→ run.sh → このファイル。手順は terapeak.md と同じで、Claudeは動かない。
1. 管理表の「設定」の検索キーワードと「状態別相場」を読む
2. eBayにログイン済みの専用Chrome（~/.local/stock-sync-mac/chrome）で Terapeak を開き、terapeak.js を動かす
3. 結果を「状態別相場」の「売れた値段」の行に書く
4. __discover の候補を Gemini（無料枠）に見せて、足してよい機種を検索キーワードと「機種の情報」シートに足す
5. 失敗したら（3回やり直しても）「更新できませんでした」のメールを1通
鍵（管理表のURL・合い言葉・GeminiのAPIキー）は ~/.local/stock-sync-mac/env にだけ置く（このリポジトリは公開）。"""
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HOME = Path.home() / ".local" / "stock-sync-mac"
JST = ZoneInfo("Asia/Tokyo")
URL = ("https://www.ebay.com/sh/research?marketplace=EBAY-US&keywords=KY-42C&dayRange=90"
       "&categoryId=0&offset=0&limit=50&tabName=SOLD")
RANKMKT = "状態別相場"
LOG = "機種の見直し"
LOG_HEAD = ["日時", "機種", "内容", "理由"]
MODEL_SHEET = "機種の情報"
MODEL_HEAD = ["検索キーワード", "種類", "メーカー", "シリーズ名", "eBayの検索語", "商品名の型番", "メーカー名の書き方",
              "追加日", "メモ"]
GEMINI_MODEL = "gemini-3.1-flash-lite"   # 商品名確認と同じ別枠（月1回・1回だけ使う）
KINDS = {"phone": "ガラケー", "camera": "デジカメ", "md": "MDウォークマン", "cassette": "カセットウォークマン",
         "cd": "CDウォークマン", "video": "ビデオカメラ", "watch": "時計", "film": "フィルムカメラ"}
BRANDS = ["Sony", "Nikon", "Canon", "Fujifilm", "Olympus", "Panasonic", "Casio", "Pentax", "Ricoh", "Kodak",
          "Seiko", "Citizen", "Aiwa", "Victor"]
MIN_SOLD, MIN_USD = 5, 60   # terapeak.md：90日で5台以上・ふつうの値段が$60以上


class LoginError(Exception):
    pass


def stamp():
    return datetime.now(JST).strftime("%Y/%m/%d %H:%M")


def load_env():
    env = {}
    for line in (HOME / "env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = {}


def sheet(action, **payload):
    for attempt in range(3):
        try:
            r = requests.post(ENV["SHEET_API_URL"], headers={"Content-Type": "text/plain"}, timeout=120,
                              data=json.dumps({"key": ENV["SHEET_API_KEY"], "action": action, **payload}))
            data = r.json()
            break
        except (ValueError, requests.RequestException):
            if action == "tab_append":   # 二重に足さない
                return {}
            time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f"管理表につながりません（{action}）")
    if data.get("error"):
        raise RuntimeError(f"管理表のエラー（{action}）: {data['error']}")
    return data


def tab_set(name, cells):
    for i in range(0, len(cells), 200):
        sheet("tab_set", name=name, cells=cells[i:i + 200])


def read_sheets():
    sv = sheet("tab_get", name="設定").get("values") or []
    row = next((i + 1 for i, r in enumerate(sv) if r and str(r[0]).startswith("検索キーワード")), None)
    if not row:
        raise RuntimeError("設定シートに「検索キーワード」の行がありません")
    b12 = str(sv[row - 1][1] if len(sv[row - 1]) > 1 else "")
    rm = sheet("tab_get", name=RANKMKT).get("values") or []
    sold = [i + 1 for i, r in enumerate(rm) if len(r) > 2 and str(r[2]) == "売れた値段"]
    start = sold[0] if sold else len(rm) + 1
    prev_last = sold[-1] if sold else start - 1
    rows = [(list(rm[i - 1]) + [""] * 12)[:12] for i in sold]
    try:
        removed = [str(r[1]) for r in (sheet("tab_get", name=LOG).get("values") or [])[1:]
                   if len(r) > 2 and "外した" in str(r[2])]
    except RuntimeError:
        removed = []
    return row, b12, start, prev_last, rows, removed


def wait(page, flag, limit):
    for _ in range(limit // 5):
        if page.evaluate(f"!!(window.{flag} && window.{flag}.finished)"):
            return
        time.sleep(5)
    raise RuntimeError(f"Terapeakの読み込みが{limit // 60}分で終わりませんでした")


PROFILE = HOME / "browser"


def clear_tabs():
    """前回のタブを持ち越さない（開くたびにタブが増えていくため・2026/11/10 本人の希望）。
    「前回の続きから開く」の設定はそのままなので、ログインのクッキーは消えない。ブラウザが動いている間は触らない"""
    if os.system(f"pgrep -f 'user-data-dir={PROFILE}' >/dev/null 2>&1") == 0:
        return
    for d in (PROFILE / "Default" / "Sessions",):
        for x in d.glob("*") if d.exists() else []:
            try:
                x.unlink()
            except OSError:
                pass


def set_prefs():
    """閉じてもeBayのログイン（セッションのクッキー）が消えないよう「前回の続きから開く」にし、名前も付ける"""
    clear_tabs()
    f = PROFILE / "Default" / "Preferences"
    if not f.exists():
        return
    try:
        p = json.loads(f.read_text())
        p.setdefault("session", {})["restore_on_startup"] = 1
        p.setdefault("profile", {})["name"] = "eBay自動更新用"
        p["profile"]["exit_type"] = "Normal"
        f.write_text(json.dumps(p))
    except (ValueError, OSError) as e:
        print("ブラウザの設定を書けませんでした:", type(e).__name__)


PORT = 9333


def attach(p):
    """専用ブラウザ（開きっぱなし）に横からつなぐ。開いていなければつなぐ口を付けて裏で開く（11/12 発送の自動化と共通）"""
    url = f"http://127.0.0.1:{PORT}"
    try:
        return p.chromium.connect_over_cdp(url, timeout=5000).contexts[0]
    except Exception:
        pass
    if os.system(f"pgrep -f 'user-data-dir={PROFILE}' >/dev/null 2>&1") == 0:   # 口なしで開いている → 開き直す
        os.system(f"pkill -f 'user-data-dir={PROFILE}' >/dev/null 2>&1")
        time.sleep(5)
    set_prefs()
    app = (HOME / "browser_app.txt").read_text().strip()
    subprocess.run(["open", "-g", "-j", "-na", app, "--args", f"--user-data-dir={PROFILE}", "--use-mock-keychain",
                    "--password-store=basic", "--no-first-run", f"--remote-debugging-port={PORT}",
                    "--remote-debugging-address=127.0.0.1", "--window-position=-32000,-32000", "about:blank"])   # 11/12 見えないように開く
    for _ in range(30):
        time.sleep(2)
        try:
            return p.chromium.connect_over_cdp(url, timeout=5000).contexts[0]
        except Exception:
            pass
    raise RuntimeError("専用ブラウザにつなげませんでした")


def run_terapeak(b12, start, rows, removed):
    from playwright.sync_api import sync_playwright
    js = (HOME / "terapeak.js").read_text()
    with sync_playwright() as p:
        # 2026/11/10 本人の希望でDockのアイコンを分けるため、いつものChromeではなく Chrome for Testing（別アプリ）を使う。
        # ログインはランチャー（eBay自動更新用ブラウザ.app）と同じ起動の仕方（--use-mock-keychain）で保存される
        ctx = attach(p)
        page = ctx.new_page()   # 新しいタブで作業して、終わったらそのタブだけ閉じる（ブラウザは閉じない）
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
            for _ in range(30):   # 「Research」ボタンが出るまで（最大60秒）
                if "signin" in page.url:
                    raise LoginError("eBayのログインが切れています")
                if page.evaluate("[...document.querySelectorAll('button')].some(b => b.innerText.trim() === 'Research')"):
                    break
                time.sleep(2)
            page.evaluate(js)
            s = page.evaluate("__setup()")
            if s != "ok":
                if "ログイン" in s:
                    raise LoginError(s)
                raise RuntimeError("Terapeakが使えません：" + s)
            keep = page.evaluate("rows => __keepRows(rows)", rows)
            print(page.evaluate("([b, s, k]) => __start(b, s, k)", [b12, start, keep]))
            wait(page, "__st", 1800)
            res, last, err = page.evaluate("[__res, __last, __st.err]")
            print(page.evaluate("([b, r]) => __discover(b, r)", [b12, removed]))
            try:
                wait(page, "__dst", 900)
                cand = page.evaluate("__dres") or []
            except RuntimeError:
                cand = []
            return res, last, err, cand
        finally:
            page.close()


def write_prices(res, start, last, prev_last):
    cells = [[start + i, c + 1, v] for i, r in enumerate(res) for c, v in enumerate(r)]
    cells += [[r, c, ""] for r in range(last + 1, prev_last + 1) for c in range(1, 13)]
    tab_set(RANKMKT, cells)


GEM_PROMPT = """日本から海外のeBayへ中古品を無在庫で売っています。eBayで過去90日によく売れていて、まだ扱っていない機種の候補です。
1つずつ、検索キーワードに足してよいかを判定し、足すならメーカー・種類も答えてください。
ルール：
- 型番の書き違い・色違いの記号（GW-M5610U と GW-M5610 など）・今のキーワードと同じ機種は「見送り」
- 本体ではなく部品・付属品の型番、別の商品と混ざりやすい短すぎる型番は「見送り」
- 種類は次のどれか：phone（日本のガラケー）, camera（コンパクトデジカメ）, md（MDウォークマン）, cassette（カセットウォークマン）, cd（CDウォークマン）, video（ビデオカメラ）, watch（腕時計）, film（フィルムカメラ）。どれにも当てはまらない新しい種類なら new（足さない）
- メーカーは次の書き方のどれか：{brands}。ここにないメーカーなら「メーカー名の書き方」に、日本語と英語でメーカー名・シリーズ名を | でつないだ正規表現（大文字）を書く
- 検索キーワード：日本のメルカリで探す言葉（例：DSC-W690, IXY 650, FinePix F31fd, GW-M5610）。今のキーワードの書き方にそろえる
- 商品名の型番：日本語の商品名に出てくる型番（例：FinePix F31fd なら F31fd）。検索キーワードと同じなら空
今の検索キーワード：{known}
候補（機種／90日の台数／ふつうの値段$／例のタイトル）：
{lines}
JSONだけで答える：[{{"機種":"","判定":"足す か 見送り","理由":"30字以内","種類":"","検索キーワード":"","メーカー":"","シリーズ名":"英語（例：Nikon Coolpix）","eBayの検索語":"英語","商品名の型番":"","メーカー名の書き方":""}}]"""


def gemini(prompt):
    key = ENV.get("GEMINI_API_KEY")
    if not key:
        return None
    for t in range(6):   # 混雑（503）のときは待って、別のモデルでもやり直す
        model = (GEMINI_MODEL, "gemini-flash-lite-latest")[t % 2]
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key, "Content-Type": "application/json"}, timeout=120,
                              json={"contents": [{"parts": [{"text": prompt}]}],
                                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}})
            if r.ok:
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(txt)
            print("Geminiのエラー:", r.status_code, r.text[:120])
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            print("Geminiにつながりませんでした:", type(e).__name__)
        time.sleep(30 * (t + 1))
    return None


def k_(s):
    return re.sub(r"[-\s]", "", str(s or "").upper())


def add_models(cand, b12, b12_row):
    """候補をGeminiで判定して、足せるものを検索キーワード・機種の情報シートに足す。戻り値：(足した, 見送り)"""
    cand = [c for c in cand if c and c[1] >= MIN_SOLD and c[2] >= MIN_USD]
    if not cand:
        return [], []
    words = [w.strip() for w in re.split(r"[,、\n]", b12) if w.strip()]
    lines = "\n".join(f"{c[0]}／{c[1]}台／${c[2]}／{c[3]}" for c in cand)
    ans = gemini(GEM_PROMPT.format(brands=", ".join(BRANDS), known=", ".join(words), lines=lines))
    if not isinstance(ans, list):
        return [], [f"{c[0]}（AIの判定が取れず見送り）" for c in cand]
    info = {k_(c[0]): c for c in cand}
    known = {k_(w) for w in words}
    added, skipped, model_rows, logs = [], [], [], []
    for a in ans:
        if not isinstance(a, dict):
            continue
        c = info.get(k_(a.get("機種")))
        kw = str(a.get("検索キーワード") or a.get("機種") or "").strip()
        kind = str(a.get("種類") or "").strip()
        brand = str(a.get("メーカー") or "").strip()
        why = str(a.get("理由") or "")[:40]
        if not c or not kw:
            continue
        if a.get("判定") != "足す":
            skipped.append(f"{kw}（{why}）")
            continue
        if kind not in KINDS:
            skipped.append(f"{kw}（新しいジャンルのため報告だけ：{why}）")
            continue
        if k_(kw) in known:
            continue
        brand_re = str(a.get("メーカー名の書き方") or "").strip()
        if kind != "phone" and brand not in BRANDS:
            try:
                re.compile(brand_re)
            except re.error:
                brand_re = ""
            if not brand_re:
                skipped.append(f"{kw}（メーカーが分からず見送り）")
                continue
        known.add(k_(kw))
        words.append(kw)
        added.append(kw)
        note = f"90日で{c[1]}台・ふつう${c[2]}"
        logs.append([stamp(), kw, "検索キーワードに足した", f"{note}（{KINDS[kind]}・AI判定）"])
        if kind != "phone":
            model_rows.append([kw, kind, brand, str(a.get("シリーズ名") or ""), str(a.get("eBayの検索語") or ""),
                               str(a.get("商品名の型番") or "") if k_(a.get("商品名の型番")) != k_(kw) else "",
                               brand_re if brand not in BRANDS else "", stamp(), note])
    if added:
        if model_rows:   # 先に機種の情報を書く（検索キーワードだけ先に入ると「携帯電話本体ではない」で外れるため）
            sheet("tab_ensure", name=MODEL_SHEET, header=MODEL_HEAD)
            sheet("tab_append", name=MODEL_SHEET, rows=model_rows)
        tab_set("設定", [[b12_row, 2, ", ".join(words)]])
        sheet("tab_ensure", name=LOG, header=LOG_HEAD)
        sheet("tab_append", name=LOG, rows=logs)
    return added, skipped


def once():
    b12_row, b12, start, prev_last, rows, removed = read_sheets()
    res, last, err, cand = run_terapeak(b12, start, rows, removed)
    if not res:
        raise RuntimeError("Terapeakの結果が空でした")
    if len(err) > len(res) // 2:
        raise RuntimeError(f"半分以上の機種が取れませんでした（{len(err)}機種）")
    write_prices(res, start, last, prev_last)
    added, skipped = [], []
    try:
        added, skipped = add_models(cand, b12, b12_row)
    except Exception as e:   # 機種の追加が失敗しても、売れた値段の更新はできている
        skipped = [f"機種の追加でエラー（{type(e).__name__}）"]
    msg = f"{len(res)}行を更新" + (f"・取れなかった：{', '.join(err)}" if err else "")
    msg += f"／足した機種：{', '.join(added) or 'なし'}"
    if skipped:
        msg += f"／見送り：{'、'.join(skipped[:8])}"
    sheet("tab_ensure", name=LOG, header=LOG_HEAD)
    sheet("tab_append", name=LOG, rows=[[stamp(), "（月1回の自動更新）", "売れた値段を更新", msg[:900]]])
    print(msg)


def notify_mac(text):
    os.system(f"osascript -e 'display notification \"{text[:80]}\" with title \"eBay売れた値段の更新\"'")


def main():
    global ENV
    if "--prefs" in sys.argv:   # ランチャーから：設定だけ書く
        set_prefs()
        return 0
    if "--path" in sys.argv:   # セットアップから：Chrome for Testing の場所
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            print(p.chromium.executable_path)
        return 0
    ENV = load_env()
    last_err = ""
    for attempt in range(3):   # 一時的な失敗は10分あけてやり直す（失敗メールを出さないため）
        try:
            once()
            return 0
        except LoginError as e:
            last_err = str(e)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:300]}"
            traceback.print_exc()
            if attempt < 2:
                time.sleep(600)
    body = (f"毎月の「eBayで売れた値段」の更新ができませんでした（{stamp()}）。\n理由：{last_err}\n\n"
            "管理表の前の値はそのままです（60日たつと自動で使われなくなります）。\n")
    if "ログイン" in last_err:
        body += "Claudeに「eBayの専用ブラウザのログインを開いて」と言ってください（Macでログインし直すだけで直ります）。"
    else:
        body += "Claudeに「売れた値段の更新が失敗した」と伝えてください。"
    if os.environ.get("NO_MAIL"):   # 試運転のときはメールを出さない
        print(body)
        return 1
    try:
        sheet("mail", subject="eBay売れた値段：更新できませんでした", body=body)
    except Exception:
        notify_mac("更新できませんでした（メールも送れませんでした）")
    print(body)
    return 1


if __name__ == "__main__":
    sys.exit(main())
