# eBayで売れた値段の更新（月1回・Terapeak）

予定された作業（毎月1日の夜）がこのメモどおりに進める。ツール（GitHub）からはeBayのログインが要るTerapeakを見られないので、本人のMacのChrome（Claude in Chrome）で見る。

## 書き込む先
- 管理表「eBay無在庫輸出 管理表」の「状態別相場」シート
- 「状態ランク」（C列）が `売れた値段` の行だけを書き換える（ほかの行は週1回の自動実行のもの。触らない）
- 列：機種／色／状態ランク＝売れた値段／安い（下から25%）／ふつう（真ん中）／高い（上から25%）／件数（90日に売れた台数）／最終更新（`2026-11-01 20:00` の形）／根拠
- 金額は円（送料込みの売れた値段×その日のドル円）。ドル円は https://api.frankfurter.dev/v1/latest?from=USD&to=JPY
- 色：902KC だけ「ピンク」の行と、色なし（ピンク以外）の行を分ける。ほかは色なしの1行
- 売れた台数が3台未満の機種も、その台数（0〜2）で書く。ツールは「90日で3台未満＝売れていない機種」として、無在庫でもヤフオクでも扱わなくなる
- 60日以上古い行はツールが使わなくなる（今まで通りの計算に戻る）

## 見る機種
- ガラケー＋PS Vita：下の機種リスト
- デジカメ・ウォークマン・MD・CD・ビデオカメラ・時計：「設定」シートB12の検索キーワードのうち、ガラケー・ゲーム機以外の全部。検索語はキーワードそのまま（IXY 650 だけ「ELPH 360」）。タイトルに含む言葉は、型番の頭の記号（DSC- DMC- EX- WM- HDR- DCR- D- SL- MZ- FinePix RICOH）を取った残り（例：DSC-W830→W830、前後に英数字が続かない形 `(?<![A-Za-z0-9])W830(?![0-9])`）。IXYは `IXY[- ]?650` の形、G-SHOCKは `GW[- ]?5000` の形
- PS Vita以外のゲーム機（PSP・DS・3DS・ゲームボーイ等）は数えない（タイトルに複数の機種がまとめて書かれていて正しく分けられない）

## 手順（Chrome）
1. `tabs_context_mcp` → 新しいタブで `https://www.ebay.com/sh/research?marketplace=EBAY-US&keywords=PCH-1000&dayRange=90&categoryId=0&offset=0&limit=50&tabName=SOLD` を開く
2. ログイン画面になったら止めて、本人に「ChromeでeBayにログインしてほしい」と知らせる（パスワードは入れない）
3. ページの中で下の `grab.js` を実行し、`window.__run(機種リスト)` で開始 → `window.__done` が true になるまで数十秒おきに `window.__out` を確認
4. 結果をドル円で円にしてシートに書く。終わったらタブを閉じる

## grab.js
```js
window.__grab = async (kw) => {
  const f = document.createElement('iframe'); f.style.cssText = 'width:1200px;height:800px;position:fixed;left:-3000px';
  f.src = '/sh/research?marketplace=EBAY-US&keywords=' + encodeURIComponent(kw) + '&dayRange=90&categoryId=0&offset=0&limit=50&tabName=SOLD';
  document.body.appendChild(f);
  let rows = [];
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 700));
    try {
      const d = f.contentDocument;
      rows = [...d.querySelectorAll('tr')].map(tr => tr.innerText.replace(/\s+/g, ' ').trim()).filter(t => /Fixed price|Auction/.test(t));
      if (rows.length || /No results|0 results|We couldn't find/i.test(d.body.innerText)) {
        if (rows.length) { await new Promise(r => setTimeout(r, 800)); rows = [...d.querySelectorAll('tr')].map(tr => tr.innerText.replace(/\s+/g, ' ').trim()).filter(t => /Fixed price|Auction/.test(t)); }
        break;
      }
    } catch (e) {}
  }
  f.remove();
  // 画面の「¥」はドル（表示の不具合）。送料込みで数える
  return rows.map(t => { const m = t.match(/image (.*?) Edit [¥$]([\d,.]+) (Fixed price|Auction)(?: [\d,]+ bids?)? [¥$]([\d,.]+) .*?Free shipping ([\d,]+) /);
    return m ? { t: m[1], p: +m[2].replace(/,/g, ''), s: +m[4].replace(/,/g, ''), n: +m[5].replace(/,/g, '') } : null; }).filter(Boolean);
};
window.__ACC = /battery|film|protector|glass|case only|carrying case|hard case|game case|cover|charger only|cable|adapter|skin|decal|replacement|parts|lcd|housing|shell|button|joystick|stick|port|motherboard|stand|grip|pouch|bag|strap|mock|dummy|sticker|game only|cartridge only|manual only|box only|empty box|for parts|not working|junk/i;
window.__run = async (M) => {
  const q = (a, x) => a[Math.min(a.length - 1, Math.floor(a.length * x))];
  window.__out = {}; window.__done = false;
  for (const [k, re, pinkSplit, query] of M) {
    const r = await window.__grab(query || k); const rx = new RegExp(re, 'i');
    const con = r.filter(x => !window.__ACC.test(x.t) && x.p >= 40 && rx.test(x.t));   // 本体だけ（付属品・部品・$40未満を除く）
    const grp = (f) => { const c = con.filter(f); const ps = c.flatMap(x => Array(Math.min(x.n, 50)).fill(x.p + x.s)).sort((a, b) => a - b);
      return ps.length ? [q(ps, .25), q(ps, .5), q(ps, .75), c.reduce((a, x) => a + x.n, 0)] : null; };   // [安い, ふつう, 高い, 台数]（ドル）
    window.__out[k] = pinkSplit ? { all: grp(x => !/pink/i.test(x.t)), pink: grp(x => /pink/i.test(x.t)) } : { all: grp(x => true) };
  }
  window.__done = true;
};
```

## 機種リスト（[機種, タイトルに含む言葉の正規表現, 902KCだけピンクを分ける, 検索語（省略可）]）
今はガラケーとPS Vitaだけ。ほかのゲーム機（PSP・DS・3DS・ゲームボーイ）は、Terapeakのタイトルに複数の機種がまとめて書かれていて（「PSP 1000/2000/3000」など）機種ごとの値段が正しく出ないので、まだ入れていない。入れるときは型番がはっきりタイトルにある出品だけを数える方法を考えてから。P-01Gは売れた数が少なすぎるので外した。
```js
[["902KC","902KC|903KC",1],["KY-42C","KY-?42C"],["A202KC","A202KC"],["601SH","601SH"],["KYF31","KYF31"],["SH-02L","SH-?02L"],
 ["701KC","701KC"],["805SH","805SH"],["SH-01J","SH-?01J"],["A205SH","A205SH"],["KYF42","KYF42"],["A203KC","A203KC"],
 ["602SH","602SH"],["806SH","806SH"],["501KC","501KC"],["PCH-2000","vita|2000"],["PCH-1000","vita|1000"]]
```

## Chromeが使えないとき
Chromeにつながらない・eBayにログインしていない・Terapeakが開けないときは、何も書き換えずに本人へ知らせる（Gmailで本人あてに1通＋作業の結果）。シートの前の値はそのまま（60日たつと自動で使われなくなる）。
