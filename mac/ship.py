"""発送の自動化（2026/11/11 本人の希望）。launchd（5分ごと）→ ship.sh → このファイル。Claudeは動かない。
売上シート：Y=重さ(g)・Z=梱包済み・AA=発送の状況・AB=追跡番号・AC=集荷
設定欄：Z3=ラベル縦ずらし(mm、＋で下へ)・Z4=横ずらし(mm、＋で右へ)・AB3=次のシール位置(1〜4)・AB4=テスト印刷
ラベル：CPaSS（FedEx）のPDFは1件2ページ（表に貼るラベル・裏に貼るAWB）。各ページの上半分の4×6インチを切り抜き、
A4 4面シール（uni+ 145.5×105mm、上下3mmの余白）の1面に1枚ずつ、縮小して並べて家のプリンターで印刷する。
③ CPaSS：配送の割り当て→追跡番号→ラベル印刷→集荷（翌営業日15〜18時）→eBay反映（ship_live）。初回はAA列「本番で進める」で1件ずつ。鍵は ~/.local/stock-sync-mac/env（run.pyと同じ）。"""
import base64
import io
import json
import re
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

HOME = Path.home() / ".local" / "stock-sync-mac"
LABELS = HOME / "labels"
JST = ZoneInfo("Asia/Tokyo")
SALES = "売上"
HEAD_ROW = 5
C_WEIGHT, C_PACKED, C_STATUS, C_TRACK, C_PICKUP = 25, 26, 27, 28, 29   # Y〜AC（1始まり）
C_REPRINT = 30   # AD：ハコペンの「ラベルを印刷し直す」ボタン（11/12）
C_PICKWANT = 32  # AF：集荷の希望（ハコペンで選ぶ。例 2026/11/14 09:00〜12:00。空なら翌営業日15:00〜18:00）
DY, DX, NEXT, TEST = (3, 26), (4, 26), (3, 28), (4, 28)                  # Z3・Z4・AB3・AB4
DOC_REQ = (3, 32)   # AF3：ハコペンの利益表からの「書類の印刷」（書類|年月。AE3は見出し）11/12

# A4 4面シール（uni+ クリックポスト対応 145.5×105mm）。mm
PAGE_W, PAGE_H = 210.0, 297.0
STICK_W, STICK_H = 105.0, 145.5
STICK_TOP = (PAGE_H - 2 * STICK_H) / 2          # 3mm
INNER = 1.5                                       # シールの端から内側にあける余白
DPI = 600


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
            time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f"管理表につながりません（{action}）")
    if data.get("error"):
        raise RuntimeError(f"管理表のエラー（{action}）: {data['error']}")
    return data


def cell(values, rc):
    r, c = rc
    try:
        return values[r - 1][c - 1]
    except IndexError:
        return ""


def truthy(v):
    return v is True or str(v).strip().upper() == "TRUE"


def num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip() or default)
    except ValueError:
        return default


def notify_mac(text, title="eBay発送"):
    t = text.replace('"', "'")[:120]
    os.system(f"osascript -e 'display notification \"{t}\" with title \"{title}\"' >/dev/null 2>&1")


# ---------- ラベルの切り抜きと4面への並べ方 ----------
def mm(v):
    return int(round(v / 25.4 * DPI))


def label_images(pdf_path):
    """PDFの各ページから、上半分のラベル部分（4×6インチ）を切り抜いて縦向きの画像にする"""
    import pypdfium2 as pdfium
    from PIL import Image, ImageOps
    out = []
    pdf = pdfium.PdfDocument(str(pdf_path))
    for i in range(len(pdf)):
        img = pdf[i].render(scale=DPI / 72).to_pil().convert("L")
        w, h = img.size
        top = img.crop((0, 0, w, int(h * 0.48)))                  # 点線より上だけ
        box = ImageOps.invert(top).point(lambda v: 255 if v > 55 else 0).getbbox()
        if not box:
            continue
        pad = mm(1)
        box = (max(0, box[0] - pad), max(0, box[1] - pad), min(w, box[2] + pad), min(top.size[1], box[3] + pad))
        lab = top.crop(box)
        if lab.size[0] > lab.size[1]:                              # 横長なら縦に起こす（文字が正しい向きになる回し方）
            lab = lab.rotate(90, expand=True)
        out.append(lab)
    pdf.close()
    if not out:
        raise RuntimeError("ラベルPDFからラベル部分が見つかりませんでした")
    return out


def slot_box(slot, dx=0.0, dy=0.0):
    """シールの位置（1=左上 2=右上 3=左下 4=右下）の枠（mm）"""
    col, row = (slot - 1) % 2, (slot - 1) // 2
    x, y = col * STICK_W + dx, STICK_TOP + row * STICK_H + dy
    return x, y, STICK_W, STICK_H


def compose(images, start_slot, dx=0.0, dy=0.0):
    """画像をstart_slotから順に並べたA4のページ（PIL画像のリスト）と、次のシール位置を返す"""
    from PIL import Image
    pages, page, slot = [], None, start_slot
    for lab in images:
        if page is None:
            page = Image.new("L", (mm(PAGE_W), mm(PAGE_H)), 255)
        x, y, w, h = slot_box(slot, dx, dy)
        bw, bh = w - 2 * INNER, h - 2 * INNER
        s = min(mm(bw) / lab.size[0], mm(bh) / lab.size[1])
        im = lab.resize((int(lab.size[0] * s), int(lab.size[1] * s)), Image.LANCZOS)
        px = mm(x + INNER) + (mm(bw) - im.size[0]) // 2
        py = mm(y + INNER) + (mm(bh) - im.size[1]) // 2
        page.paste(im, (px, py))
        slot += 1
        if slot > 4:
            pages.append(page)
            page, slot = None, 1
    if page is not None:
        pages.append(page)
    return pages, slot


def test_page(dx=0.0, dy=0.0):
    """位置合わせ用：4面の切れ目の線と、内側3mmの枠・中心の十字・番号"""
    from PIL import Image, ImageDraw, ImageFont
    page = Image.new("L", (mm(PAGE_W), mm(PAGE_H)), 255)
    d = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", mm(12))
    except OSError:
        font = ImageFont.load_default()
    lw = max(1, mm(0.25))
    for slot in range(1, 5):
        x, y, w, h = slot_box(slot, dx, dy)
        d.rectangle([mm(x), mm(y), mm(x + w), mm(y + h)], outline=0, width=lw)            # シールの切れ目
        d.rectangle([mm(x + 3), mm(y + 3), mm(x + w - 3), mm(y + h - 3)], outline=120, width=lw)
        cx, cy = mm(x + w / 2), mm(y + h / 2)
        d.line([cx - mm(10), cy, cx + mm(10), cy], fill=0, width=lw)
        d.line([cx, cy - mm(10), cx, cy + mm(10)], fill=0, width=lw)
        d.text((cx + mm(4), cy + mm(4)), str(slot), fill=0, font=font)
        for k in range(0, 21):                                                          # 上の辺に1mm目盛り
            t = mm(2 if k % 5 else 4)
            d.line([mm(x + w / 2 - 10 + k), mm(y + 3), mm(x + w / 2 - 10 + k), mm(y + 3) + t], fill=0, width=lw)
    return [page]


def to_pdf(pages, path):
    pages = [p.convert("RGB") for p in pages]
    pages[0].save(path, "PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    return path


def print_pdf(path):
    """原寸（拡大縮小なし）でA4に印刷。プリンターはMacの「デフォルトのプリンタ」"""
    r = subprocess.run(["lp", "-o", "media=A4", "-o", "print-scaling=none", "-o", "fit-to-page=false", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("印刷できませんでした：" + (r.stderr or r.stdout).strip()[:200])
    return r.stdout.strip()


def print_labels(pdf_paths, values, dry=False):
    """ラベルPDF（何件でも）を次のシール位置から並べて印刷し、次のシール位置を更新する"""
    start = int(num(cell(values, NEXT), 1)) if 1 <= num(cell(values, NEXT), 1) <= 4 else 1
    dx, dy = num(cell(values, DX)), num(cell(values, DY))
    imgs = [im for p in pdf_paths for im in label_images(p)]
    pages, nxt = compose(imgs, start, dx, dy)
    LABELS.mkdir(parents=True, exist_ok=True)
    out = to_pdf(pages, LABELS / f"print_{datetime.now(JST):%Y%m%d_%H%M%S}.pdf")
    if not dry:
        print_pdf(out)
        sheet("tab_set", name=SALES, cells=[[NEXT[0], NEXT[1], nxt]])
    return out, len(pages), nxt


# ---------- CPaSS（専用ブラウザ） ----------
PROFILE = HOME / "browser"
CPASS = "https://cpass.ebay.com/jp"
CPASS_HOME = "https://cpass.ebay.com/jp/order/paid"   # ログイン後に戻る先。トップ（/jp）はログインが消えるらしいので開かない（11/12）
LOGTAB = "発送ログ"


def log_rows(rows):
    sheet("tab_ensure", name=LOGTAB, header=["日時", "種類", "URL", "内容"])
    sheet("tab_append", name=LOGTAB, rows=[[stamp(), k, u[:500], str(t)[:20000]] for k, u, t in rows])


PORT = 9333   # 専用ブラウザを開きっぱなしにして、プログラムはこの口から横からつなぐ（11/12）


class Busy(Exception):
    pass


def browser_running():
    return subprocess.run(["pgrep", "-f", f"user-data-dir={PROFILE}"], capture_output=True).returncode == 0


MODE = HOME / "browser_mode.txt"   # 11/12 普段は "headless"（画面もDockアイコンも無い）。ログインなど本人が見るときだけ "visible"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")


def launch_browser(visible):
    """専用ブラウザを開く。普段は見えないブラウザ（headless）、visible=True のときだけ普通の窓。
    閉じて開き直してもCPaSSのログインは残る（11/12確認：サインインしたままにする が効いている）"""
    app = (HOME / "browser_app.txt").read_text().strip()
    exe = str(Path(app) / "Contents" / "MacOS" / Path(app).stem.replace("ハコペンのブラウザ", "Google Chrome for Testing"))
    if not Path(exe).exists():
        exe = next((str(x) for x in (Path(app) / "Contents" / "MacOS").iterdir()), exe)
    args = [exe, f"--user-data-dir={PROFILE}", "--use-mock-keychain", "--password-store=basic", "--no-first-run",
            f"--remote-debugging-port={PORT}", "--remote-debugging-address=127.0.0.1", f"--user-agent={UA}"]
    if visible:
        args += ["--window-position=80,60", "--window-size=1280,860"]
    else:
        args += ["--headless=new", "--window-size=1280,860"]
    subprocess.Popen(args + ["about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    MODE.write_text("visible" if visible else "headless")


def attach(p, force=False, visible=False):
    """専用ブラウザにつなぐ。開いていなければ開く（普段は見えないブラウザ）。visible=True なら普通の窓で開き直す。
    戻り値は普段使っている窓（コンテキスト）。作業は新しいタブで行い、終わったらそのタブだけ閉じる"""
    url = f"http://127.0.0.1:{PORT}"
    mode = MODE.read_text().strip() if MODE.exists() else ""
    want = "visible" if (visible or showing()) else "headless"
    if mode == want:
        try:
            return hide_unless_showing(p.chromium.connect_over_cdp(url, timeout=5000).contexts[0])
        except Exception:
            pass
    if browser_running():   # 見え方が違う（または前の開き方）→ 開き直す。ログインは残る
        if user_browsing() and not force and want == "headless" and mode == "visible":
            try:
                return hide_unless_showing(p.chromium.connect_over_cdp(url, timeout=5000).contexts[0])
            except Exception:
                raise Busy("専用ブラウザを使用中")
        os.system(f"pkill -f 'user-data-dir={PROFILE}' >/dev/null 2>&1")
        time.sleep(5)
    launch_browser(want == "visible")
    for _ in range(30):
        time.sleep(2)
        try:
            return hide_unless_showing(p.chromium.connect_over_cdp(url, timeout=5000).contexts[0])
        except Exception:
            pass
    raise RuntimeError("専用ブラウザにつなげませんでした")


force_plain = [False]


def cpass_tab(ctx):
    """CPaSSを開いているタブ（無ければ作る）。このタブは開いたままにしておく"""
    tabs = [pg for pg in ctx.pages if "cpass.ebay.com" in pg.url]
    for pg in tabs:   # ログインしているタブを優先（ログインはタブごとに続く）
        if "/login" not in pg.url:
            return pg
    if tabs:
        return tabs[0]
    pg = ctx.new_page()
    pg.goto(CPASS, wait_until="domcontentloaded", timeout=90_000)
    pg.wait_for_timeout(6000)
    return pg


KEEP_SIGNED_IN = """() => {
  const c = [...document.querySelectorAll('input[type=checkbox]')]
    .find(i => (i.closest('label,div,span')?.innerText || i.parentElement?.parentElement?.innerText || '').includes('サインインしたまま'));
  if (c && !c.checked) { c.click(); if (!c.checked) { c.checked = true; c.dispatchEvent(new Event('change', {bubbles: true})); } }
  return c ? c.checked : null;
}"""


EBAY_KMSI = """() => { const c = document.querySelector('#kmsi-checkbox, input[name=kmsi]');
  if (c && !c.checked) c.click(); return c ? c.checked : null; }"""


def tidy_tabs(ctx, keep):
    """余分なタブ（eBayのログイン画面の残り・空のタブ・2つ目以降のCPaSS）を閉じる。keep（使うCPaSSのタブ）は閉じない"""
    n = 0
    for pg in list(ctx.pages):
        if pg == keep:
            continue
        u = pg.url or ""
        if u in ("about:blank", "chrome://newtab/") or "signin.ebay.com" in u or "cpass.ebay.com" in u:
            try:
                pg.close()
                n += 1
            except Exception:
                pass
    return n


def login_mode():
    """CPaSSログイン.command から：専用ブラウザ（開きっぱなし）でCPaSSを前に出し、本人のログインを待つ。ブラウザは閉じない"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = attach(p, force=True, visible=True)
        page = cpass_tab(ctx)
        tidy_tabs(ctx, page)
        via = "CPaSSのアカウント"
        if "/login" not in page.url:
            page.reload(wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(5000)
        show_browser(ctx, page)
        for _ in range(300):   # 最大10分待つ
            try:
                if "/login" in page.url:   # 「サインインしたままにする」に自動でチェック（見えにくいため）
                    page.evaluate(KEEP_SIGNED_IN)
                elif "signin.ebay.com" in page.url:   # eBay経由のときは eBay の「Stay signed in」にもチェック（次から自動で入り直せるように）
                    via = "eBayアカウント"
                    page.evaluate(EBAY_KMSI)
            except Exception:
                pass
            if "cpass.ebay.com" in page.url and "/login" not in page.url:
                page.wait_for_timeout(3000)
                set_state("ok")
                tidy_tabs(ctx, page)
                log_rows([("ログイン", page.url, f"CPaSSにログインしました（{via}で・専用ブラウザは裏で開いたまま）")])
                end_showing(ctx)
                notify_mac("CPaSSにログインしました。ブラウザは見えないところで動き続けます")
                print(stamp(), "CPaSSにログイン")
                return 0
            page.wait_for_timeout(2000)
        end_showing(ctx)
        notify_mac("CPaSSのログインが10分で終わりませんでした")
        return 1


STATE = HOME / "cpass_state.txt"


def set_state(v):
    STATE.write_text(f"{v} {int(time.time())}")


def get_state():
    try:
        v, t = STATE.read_text().split()
        return v, int(t)
    except Exception:
        return "", 0


def keep_alive():
    """10分ごと（CPaSSは約30分なにもしないとログインが切れるため・11/12）：CPaSSのタブを読み直してログインを保つ。切れたらeBay経由で入り直しを試し、だめなら1回だけ知らせる"""
    v, t = get_state()
    if time.time() - t < 9 * 60:
        return v == "ok"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = attach(p)
        page = cpass_tab(ctx)
        tidy_tabs(ctx, page)
        page.reload(wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(6000)
        ok, why = ensure_login(page)
        if not ok and "signin.ebay.com" in page.url:
            try:
                page.evaluate(EBAY_KMSI)
            except Exception:
                pass
    try:   # 2026/11/13 最後に確かめた時刻と結果を発送ログの右上（F1:G1）に毎回上書き＝Macが動いているかシートで分かる
        sheet("tab_set", name=LOGTAB, cells=[[1, 6, "最後の確認"], [1, 7, f"{stamp()} {'ログインOK' if ok else 'ログイン切れ'}・ブラウザpid {browser_pid()}"]])
    except Exception:
        pass
    if ok:
        if v != "ok":
            log_rows([("ログイン", CPASS, "ログインできています")])
        set_state("ok")
        return True
    if v != "ng":   # 切れたときだけ1回知らせて、ログイン画面を前に出しておく（本人はログインするだけ）
        msg = f"{why}。前に出ている専用ブラウザでログインしてください（閉じてしまったら「書類」→「eBay発送」の「CPaSSログイン」）"
        log_rows([("ログイン切れ", CPASS, msg)])
        notify_mac("CPaSSのログインが切れました。前に出たブラウザでログインしてください")
        try:
            subprocess.Popen(["/bin/bash", str(HOME / "ship.sh"), "--login"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    set_state("ng")
    print(stamp(), "CPaSSのログイン切れ:", why)
    return False


PAGE_INFO = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const t = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  return JSON.stringify({
    url: location.href, title: document.title,
    text: document.body.innerText.slice(0, 4000),
    links: [...document.querySelectorAll('a[href]')].filter(vis).map(a => [t(a.innerText), a.getAttribute('href')]).slice(0, 80),
    buttons: [...document.querySelectorAll('button,[role=button],[role=tab]')].filter(vis).map(b => t(b.innerText || b.getAttribute('aria-label'))).filter(x => x).slice(0, 80),
    inputs: [...document.querySelectorAll('input,select,textarea')].filter(vis).map(i => [i.tagName, i.type, i.name, i.id, i.placeholder, t(i.closest('label,div')?.innerText)]).slice(0, 60)
  });
}"""


def ensure_login(page):
    """CPaSSのログインが切れていたら「Log in with eBay account」で入り直す（eBayのログインは専用ブラウザに残っている）。
    eBayのパスワードや同意（Agree）を求められたら自動では進めず、止めて知らせる。戻り値：(ログインできたか, 理由)"""
    if "/login" not in page.url:
        return True, ""
    try:
        page.evaluate("""() => { const c = [...document.querySelectorAll('input[type=checkbox]')]
          .find(i => (i.closest('label,div')?.innerText || '').includes('サインインしたまま')); if (c && !c.checked) c.click(); }""")
        page.goto("https://cpass.ebay.com/api/EbayUser/GoToAuthorize/1/jp", wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        return False, f"ログインのボタンが押せません（{type(e).__name__}）"
    for _ in range(45):
        page.wait_for_timeout(2000)
        u = page.url
        if "cpass.ebay.com" in u and "/login" not in u:
            page.wait_for_timeout(4000)
            return True, ""
    u = page.url
    txt = page.evaluate("document.body.innerText.slice(0, 300)")
    if "signin" in u:
        return False, "eBayのログインが切れています（パスワードが必要）"
    if "Agree" in txt or "consent" in u.lower():
        return False, "CPaSSの同意画面が出ました（Agreeは本人が押す）"
    return False, f"ログインできませんでした：{u[:120]}"


MODAL_INFO = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const t = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 100);
  const boxes = [...document.querySelectorAll('.ant-modal, .ant-drawer, [role=dialog]')].filter(vis);
  if (!boxes.length) return JSON.stringify({modal: false, url: location.href, text: document.body.innerText.slice(0, 8000)});
  const m = boxes[boxes.length - 1];
  const lab = el => {
    const item = el.closest('.ant-form-item, .ant-row, td, tr, div');
    return t(item ? item.innerText : '');
  };
  return JSON.stringify({
    modal: true, url: location.href,
    text: m.innerText.slice(0, 8000),
    inputs: [...m.querySelectorAll('input,select,textarea')].filter(vis).map(i => [i.tagName, i.type, i.name || i.id, i.value, i.placeholder, lab(i)]).slice(0, 120),
    selects: [...m.querySelectorAll('.ant-select')].filter(vis).map(s => [t(s.innerText), lab(s)]).slice(0, 60),
    buttons: [...m.querySelectorAll('button')].filter(vis).map(b => t(b.innerText)).filter(x => x)
  });
}"""

PRIVATE = re.compile(r"contact|company|street|phone|email|postcode|address|city|district|province|buyer|name$|pickupData|UserID$", re.I)


def scrub(o):
    """住所・名前・電話・メールなどを消して、仕組みが分かる項目だけ残す"""
    if isinstance(o, dict):
        return {k: ("*" if PRIVATE.search(k) and isinstance(v, (str, int, float)) else scrub(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [scrub(x) for x in o[:10]]
    return o


DETAIL_JS = r"""async (item) => {
  const H = {'Content-Type': 'application/json', 'X-CPaSS-Site-Code': 'JP'};
  const post = async (u, b) => (await fetch(u, {method: 'POST', headers: H, credentials: 'include', body: JSON.stringify(b)})).json();
  const s = await post('/api/SalesPackage/Search', {selectAll: false, sortField: 'ebaySoldDate', sortDirection: 'desc', tabMenu: 0,
    leftMenuStatus: 0, isGlobal: false, platform: 0, isRequestPickuped: null, authenticityGuarantee: [], beginTime: '2026-01-01 00:00:00',
    endTime: '2030-12-31 23:59:59', categories: [], consignList: [], shipBagCodeList: [], hubIds: [], progAGExportsDDP: null, flag: [],
    pager: {currentPage: 1, pageSize: 100}});
  const hit = (s.listData || []).find(x => JSON.stringify(x).includes(item));
  if (!hit) return null;
  return await post('/api/SalesPackage/Detail/Get', {salesPackageKey: hit.salesPackageKey});
}"""


def cpass_detail(page, item):
    """出品番号からCPaSSのパッケージの詳細（追跡番号・送料・ラベルURL・集荷など）を読む。見つからなければNone"""
    return page.evaluate(DETAIL_JS, str(item))


def fetch_label(ctx, detail, dest):
    """詳細の labelURL からラベルPDFを保存する。戻り値：保存先（取れなければ例外）"""
    url = ((detail or {}).get("carrierEntity") or {}).get("labelURL") or (detail or {}).get("salesPackage", {}).get("labelURL")
    if not url:
        raise RuntimeError("ラベルのURLがまだありません")
    r = ctx.request.get(url, timeout=60_000, max_redirects=10)
    body = r.body()
    if not body.startswith(b"%PDF"):
        raise RuntimeError(f"PDFではありません（{r.status} {r.headers.get('content-type', '')} {body[:120]!r}）")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest


# ---------- ③ CPaSSで発送手続き（11/12 作成。最初の1件はClaudeが様子を見て仕上げる） ----------
LIVE = HOME / "ship_live.ok"   # これがある（またはAA列が「本番で進める」）ときだけ、送料がかかる操作まで進める
GO = "本番で進める"
IN_PROGRESS = ("割り当て済", "追跡番号取得", "ラベル印刷済", "集荷予約済", "集荷待ち")   # 途中の状態（次の回に続きから）              # Claude（または本人）がAA列に書くと、その行だけ最後まで進める
FEDEX_CODE = "EX"               # eBay SpeedPAK-Ship Via FedEx @International Connect Plus
BOX = (22, 13, 6)               # 箱の大きさ(cm)。CPaSSに入っていないときに使う

# CPaSSの裏の窓口（ログイン済みタブの中から呼ぶ）。本文＝画面のプログラムの paramsObj
API_JS = r"""async ([act, a]) => {
  const H = {'Content-Type': 'application/json', 'X-CPaSS-Site-Code': 'JP'};
  const post = async (u, b) => { const r = await fetch(u, {method: 'POST', headers: H, credentials: 'include', body: JSON.stringify(b)});
    try { return await r.json(); } catch (e) { return {isSuccess: false, errorMessage: 'HTTP ' + r.status}; } };
  const search = async () => (await post('/api/SalesPackage/Search', {selectAll: false, sortField: 'ebaySoldDate', sortDirection: 'desc', tabMenu: 0,
    leftMenuStatus: 0, isGlobal: false, platform: 0, isRequestPickuped: null, authenticityGuarantee: [], beginTime: '2026-01-01 00:00:00',
    endTime: '2030-12-31 23:59:59', categories: [], consignList: [], shipBagCodeList: [], hubIds: [], progAGExportsDDP: null, flag: [],
    pager: {currentPage: 1, pageSize: 100}})).listData || [];
  const find = async (item) => (await search()).find(x => (x.salesPackageItemList || []).some(i => String((i.ebaySalesOrder || {}).ebayItemID) === item)
                                   || JSON.stringify(x).includes(item));
  const keyList = h => [{salesPackageKey: h.salesPackageKey, updatedDate: h.updatedDate, shipFromCountryCode: h.shipFromCountryCode || 'JP'}];
  if (act === 'state') {
    const h = await find(a.item); if (!h) return null;
    const d = await post('/api/SalesPackage/Detail/Get', {salesPackageKey: h.salesPackageKey});
    return {hit: h, detail: d};
  }
  if (act === 'quote') {   // ① 送料の候補
    const h = await find(a.item); if (!h) return {isSuccess: false, errorMessage: 'not found'};
    const so = ((h.salesPackageItemList || [])[0] || {}).ebaySalesOrder || {};
    return await post('/api/SalesPackage/SelectShippingAndShippingFeeQuery', {salesPackageSearchEntity: h, ebayUserKey: so.ebayUserKey || h.ebayUserKey,
      shipFromCountryCode: h.shipFromCountryCode || 'JP', shipToCountryCode: (h.shipToAddress || {}).countryCode || h.shipToCountryCode,
      weight: a.weight, length: a.length, width: a.width, height: a.height, isAG: false});
  }
  if (act === 'assign') {  // ② 配送の割り当て
    const h = await find(a.item); if (!h) return {isSuccess: false, errorMessage: 'not found'};
    const c = a.c;
    return await post('/api/SalesPackage/Shipping/Select', {carrierName: c.carrierName, serviceName: (c.serviceName || '').replace('\n', ''),
      serviceCode: c.serviceCode, shippingFee: c.shippingFee, shippingFeeCCode: c.shippingFeeCCode, packageKeyAndDateList: keyList(h), searchEntity: null});
  }
  if (act === 'pickupTemplate') {   // 前に集荷を頼んだ分の集荷先（住所の番号など）を使い回す
    const list = (await search()).filter(x => (x.pickupDate || '').slice(0, 4) > '1900' && x.salesPackageKey !== a.key);
    for (const x of list.slice(0, 5)) {
      const d = await post('/api/SalesPackage/Detail/Get', {salesPackageKey: x.salesPackageKey});
      const pd = ((d || {}).salesPackage || {}).pickupData;
      if (pd) return pd;
    }
    return null;
  }
  if (act === 'pickup') return await post('/api/SalesPackage/PickUp', {searchEntity: null, salesPackageKeys: [a.key], pickUpTime: a.time});
  if (act === 'markShipped') return await post('/api/SalesPackage/MarkAsShipped', {salesPackageKeys: [a.key]});
  return null;
}"""

MODALS = "() => [...document.querySelectorAll('.ant-modal, .ant-drawer, [role=dialog]')].filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)).length"


def next_pickup():
    """集荷は翌営業日（土日を除く）の15:00〜18:00（本人の決まり）"""
    from datetime import timedelta
    d = datetime.now(JST).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def want_pickup(row):
    """ハコペンで選んだ集荷の日時（AF列）。無い・過ぎている・読めないときは翌営業日15:00〜18:00"""
    try:
        v = str((sheet("tab_get", name=SALES).get("values") or [])[row - 1][C_PICKWANT - 1]).strip()
        m = re.match(r"(\d{4})\D(\d{1,2})\D(\d{1,2})\D+(\d{1,2}):(\d{2})\D+(\d{1,2}):(\d{2})", v)
        if m:
            from datetime import date
            y, mo, d, a1, a2, b1, b2 = map(int, m.groups())
            day = date(y, mo, d)
            now = datetime.now(JST)
            if day > now.date() or (day == now.date() and now.hour < a1 - 1):
                return day, f"{a1:02d}:{a2:02d}", f"{b1:02d}:{b2:02d}"
    except Exception:
        pass
    return next_pickup(), "15:00", "18:00"


def ship_live(ctx, page, row, item, weight, st, rows):
    """送料がかかる工程：配送の割り当て → 追跡番号の取得 → ラベル印刷 → 集荷予約 → eBayへ反映。
    1工程ずつCPaSSの中身で確かめて進む。途中で止まったら「止まった：〜」を返す（次の回は触らない）"""
    def api(act, **a):
        return page.evaluate(API_JS, [act, {"item": item, **a}])

    def state():
        s = api("state")
        if not s or not s.get("detail"):
            raise RuntimeError("CPaSSの注文が読めない")
        return s["hit"], s["detail"], s["detail"].get("salesPackage") or {}

    hit, d, sp = state()
    key = sp.get("salesPackageKey")
    # ① ② 配送の割り当て（FedEx International Connect Plus）
    if not sp.get("shippingTrackCode") and sp.get("shippingServiceCode") != FEDEX_CODE:
        w = max(num(sp.get("packageWeight")), weight / 1000)
        dims = [num(sp.get(k)) or BOX[i] for i, k in enumerate(("packageLength", "packageWidth", "packageHeight"))]
        q = api("quote", weight=round(w, 3), length=dims[0], width=dims[1], height=dims[2])
        cands = q.get("successMessage") or [] if q else []
        rows.append(("送料の候補", "", json.dumps(q, ensure_ascii=False)[:5000]))
        c = next((x for x in cands if x.get("serviceCode") == FEDEX_CODE or "International Connect Plus" in (x.get("serviceName") or "")), None)
        if not c:
            return "止まった：FedExの送料が出ない（発送ログに候補）"
        a = api("assign", c=c)
        rows.append(("割り当て", "", json.dumps(a, ensure_ascii=False)[:3000]))
        if not (a or {}).get("isSuccess"):
            return "止まった：配送の割り当てに失敗（発送ログ）"
        hit, d, sp = state()
        ship_status(row, f"割り当て済（送料{int(num(c.get('shippingFee')))}円）")
    # ③ 追跡番号の取得（送料がかかる）。送る中身が画面の編集内容から作られるので、画面のボタンを押す
    if not sp.get("shippingTrackCode"):
        if num(sp.get("packageWeight")) <= 0:
            return "止まった：重さが0（CPaSSのSKU設定に機種が無い）"
        page.goto("https://cpass.ebay.com/jp/order/readytoship", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(7000)
        res = page.evaluate(CARD_JS, [item, "追跡番号を取得"])
        if res != "clicked":
            res = "編集:" + page.evaluate(CARD_JS, [item, "編集"])
            page.wait_for_timeout(4000)
            res += " / " + str(click_text(page, "追跡番号を取得", "modal"))
        page.wait_for_timeout(3000)
        rows.append(("追跡番号ボタン", page.url, f"{res} / {page.evaluate(MODAL_INFO)[:6000]}"))
        for _ in range(2):   # 確認の小窓（サインの確認など）が出たら進める
            if page.evaluate(MODALS) and any(click_text(page, t, "modal") for t in ("確認", "OK", "はい", "続行")):
                page.wait_for_timeout(4000)
        for _ in range(12):
            hit, d, sp = state()
            if sp.get("shippingTrackCode"):
                break
            page.wait_for_timeout(5000)
        else:
            rows.append(("追跡番号", page.url, page.evaluate(MODAL_INFO)[:6000]))
            click_text(page, "閉じる", "modal")
            return "止まった：追跡番号が取れない（発送ログに画面）"
        click_text(page, "閉じる", "modal")
        track = sp["shippingTrackCode"]
        sheet("tab_set", name=SALES, cells=[[row, C_TRACK, track]])
        ship_status(row, f"追跡番号取得（送料{int(num(sp.get('shippingFee')))}円）")
        st = "追跡番号取得"
    # ④ ラベル（1件2面）を4面シールに印刷。1回だけ
    if not any(x in st for x in ("ラベル印刷済", "集荷予約済", "集荷待ち")):
        f = None
        for _ in range(6):
            try:
                f = fetch_label(ctx, d, LABELS / f"{item}.pdf")
                break
            except RuntimeError:
                page.wait_for_timeout(10000)
                hit, d, sp = state()
        if not f:
            return "止まった：ラベルPDFが取れない"
        print_labels([f], sheet("tab_get", name=SALES).get("values") or [])
        ship_status(row, "ラベル印刷済")
        notify_mac(f"{row}行目のラベルを印刷しました（2面）")
    # ⑤ 集荷（翌営業日 15:00〜18:00）
    if (sp.get("pickupDate") or "1900")[:4] <= "1900":
        tpl = api("pickupTemplate", key=key)
        if not tpl:
            return "止まった：集荷先が分からない（前の集荷の記録なし）"
        t = {k: v for k, v in json.loads(tpl).items() if k not in ("pickupConfirmationCode", "pickUpLocation")}
        day, h1, h2 = want_pickup(row)
        t["pickUpReadyTime"] = f"{day:%Y-%m-%d}T{h1}:00+0900"
        t["pickUpCloseTime"] = f"{day:%Y-%m-%d}T{h2}:00+0900"
        p = api("pickup", key=key, time=json.dumps(t, ensure_ascii=False, separators=(",", ":")))
        rows.append(("集荷", "", json.dumps(p, ensure_ascii=False)[:3000]))
        if not (p or {}).get("isSuccess"):
            return "止まった：集荷の予約に失敗（発送ログ）"
        sheet("tab_set", name=SALES, cells=[[row, C_PICKUP, f"{day:%-m/%-d} {h1}〜{h2}"]])
        ship_status(row, "集荷予約済")
        hit, d, sp = state()
    # ⑥ eBayへ追跡番号を反映（集荷の受付番号が付いてから）
    so = ((sp.get("salesPackageItemList") or [{}])[0].get("ebaySalesOrder") or {})
    if not so.get("ebayIsMarkshipped"):
        if not sp.get("shippingHandoverID"):
            return "集荷待ち（eBay反映はこのあと自動）"
        m = api("markShipped", key=key)
        rows.append(("eBay反映", "", json.dumps(m, ensure_ascii=False)[:3000]))
        if not (m or {}).get("isSuccess"):
            return "集荷待ち（eBay反映に失敗・次の回にやり直し）"
    if not LIVE.exists():   # 11/12 本人の希望：1件目が最後まで通ったら、次からは「梱包済み」だけで印刷・集荷まで全部進める
        LIVE.write_text(stamp())
        log_rows([("本番", "", "1件目の発送が最後まで通ったので、次からは梱包済みだけで全部自動で進めます")])
    return "済"
CARD_JS = r"""([id, btn]) => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const hit = [...document.querySelectorAll('body *')].filter(e => vis(e) && e.children.length === 0 && (e.textContent || '').includes('(' + id + ')'));
  for (const h of hit) {
    let p = h;
    while (p && !(p.innerText || '').includes('パッケージ番号')) p = p.parentElement;
    if (!p) continue;
    if (!btn) return 'found';
    const b = [...p.querySelectorAll('button, a')].find(x => vis(x) && (x.innerText || '').replace(/\s/g, '') === btn);
    if (b) { b.click(); return 'clicked'; }
    return 'nobutton';
  }
  return 'none';
}"""


def click_text(page, text, scope="body"):
    """見えているボタン・リンク・メニューを文字で探して押す（空白は無視）"""
    return page.evaluate("""([t, sc]) => {
      const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const root = sc === 'modal' ? [...document.querySelectorAll('.ant-modal, .ant-drawer, [role=dialog]')].filter(vis).pop() : document.body;
      if (!root) return false;
      const b = [...root.querySelectorAll('button, a, li, [role=menuitem], .ant-dropdown-menu-item')].find(x => vis(x) && (x.innerText || '').replace(/\\s/g, '') === t);
      if (b) { b.click(); return true; } return false; }""", [text, scope])


def ship_status(row, text):
    sheet("tab_set", name=SALES, cells=[[row, C_STATUS, text]])


def ship_order(row, r, st=""):
    """売上シートの1行（梱包済み）をCPaSSで発送まで進める。戻り値：結果の短い文（AA列に書く）"""
    from playwright.sync_api import sync_playwright
    item = re.sub(r"\D", "", str((r + [""] * 30)[3]))   # D列＝eBayの出品番号
    weight = num((r + [""] * 30)[C_WEIGHT - 1])
    if not item:
        return "eBayの出品番号が読めない"
    if weight <= 0:
        return "重さ(g)が空"
    calls = []

    def on_resp(x):
        try:
            if x.request.resource_type in ("xhr", "fetch") and "/api/" in x.url and len(calls) < 80 \
                    and not re.search(r"Notification|Site/Get|LeftMenu|CurrencyExchange|GetCurrentUser|login|token|auth|DHLECommerce", x.url, re.I):
                calls.append(("通信", f"{x.request.method} {x.url}", f"{x.status} 送信:{(x.request.post_data or '')[:2500]} 受信:{x.text()[:400] if x.ok else ''}"))
        except Exception:
            pass

    rows = []
    with sync_playwright() as p:
        ctx = attach(p)
        page = cpass_tab(ctx)
        if "/login" in page.url:
            return "ログイン待ち"
        page.on("response", on_resp)
        try:
            page.goto(CPASS_HOME, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(7000)
            if "/login" in page.url:
                return "ログイン待ち"
            live = LIVE.exists() or st.startswith(GO) or st.startswith(IN_PROGRESS)
            if live:   # 途中まで進んでいる（割り当て済み・追跡番号あり）なら、そこから続ける
                cur = page.evaluate(API_JS, ["state", {"item": item}])
                sp0 = ((cur or {}).get("detail") or {}).get("salesPackage") or {}
                if sp0.get("shippingTrackCode") or sp0.get("shippingServiceCode") == FEDEX_CODE:
                    return ship_live(ctx, page, row, item, weight, st, rows)
            where = ""
            if page.evaluate(CARD_JS, [item, ""]) == "found":
                where = "paid"
            else:   # 発送手続き待ちに無い → 同期してもう一度
                if click_text(page, "注文を同期"):
                    page.wait_for_timeout(1200)
                    click_text(page, "すべての注文を同期")
                    page.wait_for_timeout(1500)
                    click_text(page, "確認", "modal") or click_text(page, "OK", "modal")
                    page.wait_for_timeout(25000)
                    page.goto(CPASS_HOME, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(7000)
                    if page.evaluate(CARD_JS, [item, ""]) == "found":
                        where = "paid"
            if not where:   # もう発送手続きタブにあるか
                page.goto("https://cpass.ebay.com/jp/order/readytoship", wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(7000)
                if page.evaluate(CARD_JS, [item, ""]) == "found":
                    where = "ready"
            if not where:
                rows.append(("発送", page.url, f"{row}行目：CPaSSに注文が見つからない（出品番号{item}）"))
                return "CPaSSに注文が見つからない（「注文を同期」→「1件の注文を同期」が必要かも）"
            if where == "paid":
                res = page.evaluate(CARD_JS, [item, "発送手続き"])
                page.wait_for_timeout(2500)
                rows.append(("発送手続き", page.url, f"{row}行目 {res} / 画面:{page.evaluate(MODAL_INFO)[:3000]}"))
                for t in ("発送手続き", "確認", "OK", "はい"):   # 確認の小窓が出たら進める（発送手続きタブへ移すだけ・送料はかからない）
                    if click_text(page, t, "modal"):
                        page.wait_for_timeout(3000)
                        break
                page.goto("https://cpass.ebay.com/jp/order/readytoship", wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(7000)
                if page.evaluate(CARD_JS, [item, ""]) != "found":
                    return "発送手続きタブに移せなかった"
            # 発送手続きタブ：「編集」を開いて中身を記録
            res = page.evaluate(CARD_JS, [item, "編集"])
            page.wait_for_timeout(4000)
            rows.append(("編集", page.url, f"{row}行目 {res} / {page.evaluate(MODAL_INFO)}"))
            click_text(page, "閉じる", "modal")
            if not live:   # 本番の合図が無い：ここで止める（送料がかかる操作はしない）
                return "確認待ち（初回はClaudeが仕上げる）"
            return ship_live(ctx, page, row, item, weight, st, rows)
        finally:
            try:
                page.remove_listener("response", on_resp)
                if "/login" not in page.url and "/order/" not in page.url:
                    page.goto(CPASS_HOME, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                pass
            log_rows(rows + calls)


def user_browsing():
    """本人が専用ブラウザを開いて15分以内なら使っている最中とみなして触らない"""
    r = subprocess.run(["pgrep", "-f", f"user-data-dir={PROFILE}"], capture_output=True, text=True)
    pids = r.stdout.split()
    if not pids:
        return False
    e = subprocess.run(["ps", "-o", "etime=", "-p", pids[0]], capture_output=True, text=True).stdout.strip()
    parts = [int(x) for x in e.replace("-", ":").split(":") if x.isdigit()]
    secs = sum(v * 60 ** i for i, v in enumerate(reversed(parts[-3:]))) + (parts[0] * 86400 if len(parts) == 4 else 0)
    return secs < 15 * 60


# ---------- 見張り ----------
# ずっと使うログイン用のショートカットは「書類/eBay発送」に置く（11/12 ダウンロードから移した）
SHORTCUT_DIR = Path.home() / "Applications" / "eBay発送"
SHORTCUT = r'''#!/bin/bash
# CPaSSのログインが切れたとき用：専用ブラウザでCPaSSが開くので、ログインする（10分以内）
nohup bash "$HOME/.local/stock-sync-mac/ship.sh" --login >/dev/null 2>&1 &
disown
sleep 2
osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$(basename "$0")\")" >/dev/null 2>&1 &
'''


def place_shortcut():
    """「書類」フォルダの「eBay発送」に置く（11/12 本人の希望）。前に置いた場所の古いものは消す"""
    for d in (Path.home() / "Documents" / "eBay発送",):
        dst = d / "CPaSSログイン.command"
        try:
            if not dst.exists() or dst.read_text() != SHORTCUT:
                d.mkdir(parents=True, exist_ok=True)
                dst.write_text(SHORTCUT)
                dst.chmod(0o755)
                print(stamp(), "ログイン用ショートカットを置きました:", dst)
            break
        except Exception as e:
            print(stamp(), "ショートカットを置けませんでした:", d, type(e).__name__)
    else:
        return
    for od in (Path.home() / "Downloads", Path.home() / "Applications" / "eBay発送", Path("/Applications/eBay発送")):
        try:
            old = od / "CPaSSログイン.command"
            if old.exists():
                old.unlink()
            if od.name == "eBay発送" and od.exists() and not [x for x in od.iterdir() if x.name != ".DS_Store"]:
                (od / ".DS_Store").unlink(missing_ok=True)
                od.rmdir()
        except Exception:
            pass


RAW = "https://raw.githubusercontent.com/inaba-tools/stock-sync/main"


def refresh_helpers():
    """Mac側の小さな起動ファイルも最新にしておく（Mac側の作り直しを不要にするため。1時間に1回）"""
    mark = HOME / "helpers_at"
    if mark.exists() and time.time() - mark.stat().st_mtime < 3600:
        return
    for f in ("run.sh", "browser.sh", "ship.sh"):
        try:
            r = requests.get(f"{RAW}/mac/{f}", timeout=30)
            if r.ok and r.text.startswith("#!/bin/bash"):
                tmp = HOME / (f + ".new")
                tmp.write_text(r.text)
                tmp.chmod(0o755)
                tmp.replace(HOME / f)
        except Exception as e:
            print(stamp(), f"{f}を更新できませんでした:", type(e).__name__)
    mark.write_text(stamp())



# ---------- 専用ブラウザを見えなくする（11/12 本人の希望：Dockにも出さない・ウインドウも出さない） ----------
# フクリの Chrome for Testing をコピーし、Info.plist に LSUIElement（Dockに出さない）を付けたものを使う。
# ウインドウは画面の外に置く。ログインのとき（--login）・本人が開いたとき（--show）だけ画面に出し、15分たったらまた隠す
OFF = -32000
HIDDEN_APP = HOME / "ハコペンのブラウザ.app"
SHOWING = HOME / "showing"


def showing():
    try:
        return time.time() - SHOWING.stat().st_mtime < 15 * 60
    except FileNotFoundError:
        return False


def place_windows(ctx, show):
    """専用ブラウザのウインドウを画面に出す（show）／画面の外に置く"""
    seen = set()
    for pg in list(ctx.pages):
        try:
            s = ctx.new_cdp_session(pg)
            wid = s.send("Browser.getWindowForTarget")["windowId"]
            if wid in seen:
                s.detach()
                continue
            seen.add(wid)
            s.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            b = {"left": 80, "top": 60, "width": 1280, "height": 860} if show else {"left": OFF, "top": OFF}
            s.send("Browser.setWindowBounds", {"windowId": wid, "bounds": b})
            s.detach()
        except Exception:
            pass


def browser_pid():
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-nP", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=10)
        return int(r.stdout.split()[0]) if r.stdout.strip() else 0
    except Exception:
        return 0


def hide_app():
    """専用ブラウザ（ポート9333で待っているもの）を隠す（⌘Hと同じ）。閉じないのでCPaSSのログインは残る。
    11/12：画面の外へ移すだけだとmacOSに画面内へ戻されて見えたままになるため。様子は発送ログに1回だけ書く"""
    pid = browser_pid()
    if not pid:
        return
    js = ("ObjC.import('AppKit');var r=$.NSRunningApplication.runningApplicationWithProcessIdentifier(%d);"
          "if(!r){'none'}else{var h=r.hide;JSON.stringify({path:ObjC.unwrap(r.bundleURL.path),policy:r.activationPolicy,"
          "hidden:r.hidden,hideOk:h})}" % pid)
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True, timeout=20)
        info = (out.stdout or out.stderr).strip()[:400]
        mark = HOME / "dock_diag.txt"
        old = mark.read_text() if mark.exists() else ""
        if info != old:
            mark.write_text(info)
            log_rows([("ブラウザの様子", str(pid), info)])
    except Exception:
        pass


def hide_unless_showing(ctx):
    if not showing():
        place_windows(ctx, False)
        hide_app()
    return ctx


def show_browser(ctx, page=None):
    SHOWING.touch()
    place_windows(ctx, True)
    if page is not None:
        page.bring_to_front()
    if MODE.exists() and MODE.read_text().strip() == "visible":
        subprocess.run(["open", "-a", (HOME / "browser_app.txt").read_text().strip()])


def end_showing(ctx):
    SHOWING.unlink(missing_ok=True)
    place_windows(ctx, False)
    hide_app()


def make_hidden_app(src):
    """フクリの Chrome for Testing（src）から、Dockに出ないコピーを作る。作れなければ src をそのまま返す"""
    import shutil
    mark = HOME / "hidden_app_src.txt"
    if (HOME / "hidden_app_broken").exists():   # 一度開けなかったら作り直さない（元のまま使う）
        return src
    if HIDDEN_APP.exists() and mark.exists() and mark.read_text().strip() == src:
        return str(HIDDEN_APP)
    tmp = HOME / "hidden_app_tmp.app"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(["ditto", src, str(tmp)], check=True, timeout=600)
        plist = str(tmp / "Contents" / "Info.plist")
        subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Delete :LSUIElement", plist], capture_output=True)
        subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Add :LSUIElement bool true", plist], check=True)
        subprocess.run(["xattr", "-cr", str(tmp)], capture_output=True)
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(tmp)], check=True, capture_output=True, timeout=600)
        subprocess.run(["codesign", "--verify", "--deep", str(tmp)], check=True, capture_output=True, timeout=600)
        if browser_running() and HIDDEN_APP.exists():   # 使用中のものは差し替えない
            shutil.rmtree(tmp, ignore_errors=True)
            return str(HIDDEN_APP)
        shutil.rmtree(HIDDEN_APP, ignore_errors=True)
        tmp.rename(HIDDEN_APP)
        mark.write_text(src + "\n")
        print(stamp(), "Dockに出ない専用ブラウザを作りました:", HIDDEN_APP)
        return str(HIDDEN_APP)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        print(stamp(), "Dockに出ない専用ブラウザを作れませんでした（今のまま使います）:", type(e).__name__, str(e)[:200])
        try:
            log_rows([("ブラウザ", "", f"Dockに出ない版を作れませんでした：{type(e).__name__} {str(e)[:300]}")])
        except Exception:
            pass
        return src


def busy_shipping(values):
    """発送が途中まで進んでいる行があるか（切り替えでCPaSSのログインが消えると止まるので、その間は切り替えない）"""
    for i, r in enumerate(values):
        r = r + [""] * 30
        st = str(r[C_STATUS - 1]).strip()
        if i + 1 > HEAD_ROW and truthy(r[C_PACKED - 1]) and st and not re.match(r"済|止まった|確認待ち|手作業", st):
            return True
    return False

FUKURI_PY = Path.home() / ".fukuri" / "venv" / "bin" / "python"


def retry_hidden(values):
    """11/12 本人OK（今やる）：Dockに出ない版でもう一度だけ開き直す。開けなければ元に戻り、理由を発送ログへ"""
    done = HOME / "retry_hidden.done"
    if True or done.exists() or showing() or busy_shipping(values or []):
        return
    done.write_text(stamp())
    (HOME / "hidden_app_broken").unlink(missing_ok=True)
    src = HOME / "hidden_app_src.txt"
    base = src.read_text().strip() if src.exists() else ""
    if not base:
        cur = (HOME / "browser_app.txt").read_text().strip() if (HOME / "browser_app.txt").exists() else ""
        base = cur if cur and cur != str(HIDDEN_APP) else ""
    if not base:
        log_rows([("ブラウザ", "", "開き直し：元のChrome for Testingの場所がわかりません")])
        return
    (HOME / "hidden_app_src.txt").unlink(missing_ok=True)   # 作り直させる
    if browser_running():
        os.system(f"pkill -f 'user-data-dir={PROFILE}' >/dev/null 2>&1")
        time.sleep(5)
    app = make_hidden_app(base)
    (HOME / "browser_app.txt").write_text(app + "\n")
    set_state("")
    log_rows([("ブラウザ", "", f"Dockに出ない版で開き直します（{'Dockに出ない版' if app == str(HIDDEN_APP) else '作れず元のまま'}）。CPaSSのログイン画面が出たらログイン")])


def unify_browser(values=None):
    """専用ブラウザをフクリと同じ Chrome for Testing（~/.fukuri の Playwright に付いている版）にそろえる（11/12 本人の希望）。
    開いている間は切り替えない（CPaSSのログインが消えるため）。次に専用ブラウザが開くときから新しい版になる"""
    if not FUKURI_PY.exists():
        return
    r = subprocess.run([str(FUKURI_PY), "-c", "from playwright.sync_api import sync_playwright as s; p=s().start(); print(p.chromium.executable_path); p.stop()"],
                       capture_output=True, text=True, timeout=120)
    exe = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if ".app/" not in exe or not Path(exe).exists():
        return
    app = make_hidden_app(exe.split(".app/")[0] + ".app")
    f = HOME / "browser_app.txt"
    if f.exists() and f.read_text().strip() == app:
        return
    if browser_running():   # 11/12 本人OK：今の古いブラウザ（Chromium）を閉じて切り替える。発送の途中・本人が使っている間は待つ
        if showing() or busy_shipping(values or []):
            print(stamp(), "発送の途中か使用中なので、ブラウザの切り替えは次の回にします")
            return
        os.system(f"pkill -f 'user-data-dir={PROFILE}' >/dev/null 2>&1")
        time.sleep(5)
        set_state("")   # 次の回にログインを確かめ、切れていればログイン画面を出す
    f.write_text(app + "\n")
    print(stamp(), "専用ブラウザを切り替えました:", app)
    try:
        log_rows([("ブラウザ", "", "専用ブラウザを Dockに出ない Chrome for Testing に切り替え（CPaSSはログインし直し）")])
        notify_mac("専用ブラウザを切り替えました。CPaSSのログイン画面が出たらログインしてください")
    except Exception:
        pass


APP_URL = "https://inaba-tools.github.io/stock-sync/"


def send_app_link():
    """非公開Webアプリ「輸出帳」の合い鍵リンクを、本人のメールに1回だけ送る（11/12）。鍵は # の後ろなのでサーバーには送られない"""
    mark = HOME / "app_link_sent"
    if mark.exists():
        return
    from urllib.parse import quote
    link = f"{APP_URL}#u={quote(ENV['SHEET_API_URL'], safe='')}&k={quote(ENV['SHEET_API_KEY'], safe='')}"
    body = ("eBay無在庫の自分専用アプリ「輸出帳」の合い鍵です。\n"
            "iPhoneでこのリンクを1回タップすると開けるようになります（次からはそのまま開けます）。\n"
            "Safariの共有ボタン→「ホーム画面に追加」でアプリになります。\n\n"
            f"輸出帳を開く：\n{link}\n\n"
            "このリンクは人に送らないでください（管理表を読み書きできる鍵が入っています）。\n"
            "このメールは読んだら消して大丈夫です。")
    sheet("mail", subject="輸出帳（eBay無在庫アプリ）の合い鍵", body=body)
    mark.write_text(stamp())
    print(stamp(), "輸出帳の合い鍵リンクをメールしました")


def once():
    place_shortcut()
    try:
        pass   # send_app_link()  11/12 本人と相談中（Googleログイン方式に変えるか）のため止めた
    except Exception as e:
        print(stamp(), "輸出帳のリンクを送れませんでした:", type(e).__name__)
    refresh_helpers()
    try:
        retry_hidden(sheet("tab_get", name=SALES).get("values") or [])
    except Exception as e:
        print(stamp(), "開き直しで失敗:", type(e).__name__, str(e)[:200])
    try:
        unify_browser(sheet("tab_get", name=SALES).get("values") or [])
    except Exception as e:
        print(stamp(), "ブラウザの切り替えで失敗:", type(e).__name__, str(e)[:200])
    if not showing():
        hide_app()
    try:
        keep_alive()
    except Busy:
        pass
    except Exception as e:   # ログインの確認で失敗しても、印刷などはそのまま続ける
        print(stamp(), "ログインの確認で失敗:", type(e).__name__, str(e)[:200])
    for f in HOME.glob("probe_*"):   # 11/12 調査（probe）は終わったので、目印のファイルを片付ける
        f.unlink(missing_ok=True)
    values = sheet("tab_get", name=SALES).get("values") or []
    if truthy(cell(values, TEST)):
        LABELS.mkdir(parents=True, exist_ok=True)
        print_pdf(to_pdf(test_page(num(cell(values, DX)), num(cell(values, DY))), LABELS / "test.pdf"))
        sheet("tab_set", name=SALES, cells=[[TEST[0], TEST[1], False]])
        notify_mac("位置確認用を1枚印刷しました")
        print(stamp(), "テスト印刷")
    # ハコペンの利益表から「書類の印刷」（AF3＝"sales|2026/11" のように 書類|年月）。管理表でその月のPDFを作って家のプリンターで印刷（11/12）
    req = str(cell(values, DOC_REQ)).strip()
    if req:
        sheet("tab_set", name=SALES, cells=[[DOC_REQ[0], DOC_REQ[1], ""]])
        kind, _, ym = req.partition("|")
        try:
            d = sheet("pdf_bytes", kind=kind, ym=ym)
            LABELS.mkdir(parents=True, exist_ok=True)
            f = LABELS / "document.pdf"
            f.write_bytes(base64.b64decode(d["b64"]))
            print_pdf(f)
            notify_mac(f"{d.get('name', '書類')}を印刷しました")
            log_rows([("書類の印刷", req, f"{d.get('name', '')}（{d.get('n', 0)}件）を印刷しました")])
        except Exception as e:
            log_rows([("書類の印刷", req, f"印刷できませんでした：{type(e).__name__} {str(e)[:150]}")])
    # ハコペンから「ラベルを印刷し直す」が押された行（AD列）。手元に残っているラベルPDFを印刷する（11/12）
    for i, r in enumerate(values):
        r = r + [""] * 31
        if i + 1 <= HEAD_ROW or not truthy(r[C_REPRINT - 1]):
            continue
        item = re.sub(r"\D", "", str(r[3]))
        f = LABELS / f"{item}.pdf"
        sheet("tab_set", name=SALES, cells=[[i + 1, C_REPRINT, False]])
        if item and f.exists():
            print_labels([f], values)
            notify_mac(f"{i + 1}行目のラベルを印刷し直しました")
            log_rows([("再印刷", item, "ラベルを印刷し直しました")])
        else:
            log_rows([("再印刷", item, "ラベルPDFが手元にないため印刷できませんでした")])
    # ③：梱包済みの行 → CPaSSで発送（追跡番号が空の行と、途中まで進んだ行）。手作業で済ませた行（追跡番号あり・状況が空）は触らない
    todo = []
    for i, r in enumerate(values):
        r = r + [""] * 30
        if i + 1 <= HEAD_ROW or not truthy(r[C_PACKED - 1]):
            continue
        st = str(r[C_STATUS - 1]).strip()
        if st.startswith(("確認待ち", "済", "止まった")):
            continue   # Claudeの確認待ち・済み・止まった行は、人が状況を書き換えるまで触らない（送料の二重取りを防ぐ）
        if str(r[C_TRACK - 1]).strip() and not st.startswith(IN_PROGRESS):
            continue
        todo.append(i + 1)
    for row in todo[:3]:
        r = values[row - 1]
        st = str((r + [""] * 30)[C_STATUS - 1]).strip()
        try:
            res = ship_order(row, r, st)
        except Exception as e:
            res = f"止まった：{type(e).__name__} {str(e)[:80]}"
            log_rows([("失敗", "", traceback.format_exc()[-3000:])])
        if res != st:
            ship_status(row, res)
            if res.startswith(("済", "止まった", "確認待ち")):
                notify_mac(f"{row}行目：{res}")
        print(stamp(), "発送", row, res)


def main():
    global ENV
    if "--login" in sys.argv:
        ENV = load_env()
        return login_mode()
    if "--show" in sys.argv:   # 本人が専用ブラウザを使うとき：15分だけ画面に出す
        from playwright.sync_api import sync_playwright
        SHOWING.touch()
        with sync_playwright() as p:
            show_browser(attach(p, force=True, visible=True))
        return 0
    if "--print" in sys.argv:   # 手で試す：ship.py --print ラベル.pdf [--dry]
        ENV = load_env() if (HOME / "env").exists() else {}
        files = [a for a in sys.argv[1:] if a.endswith(".pdf")]
        values = sheet("tab_get", name=SALES).get("values") if ENV else []
        print(print_labels(files, values or [], dry="--dry" in sys.argv))
        return 0
    ENV = load_env()
    try:
        once()
        return 0
    except Exception as e:
        traceback.print_exc()
        notify_mac(f"止まりました：{str(e)[:80]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
