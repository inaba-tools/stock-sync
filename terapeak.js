// eBayで売れた値段（Terapeak・過去90日）をまとめて取る。terapeak.md の手順で、Terapeakのページの中で実行する。
// 画面は開かず、ページが裏で使っている問い合わせ（graphql の UnifiedSearch）をそのまま使って、機種名だけ入れ替えて読む。見るだけ。
// 使い方：(1) このファイル全体を実行 → (2) await __setup() → (3) __start(B12の文字, 書き始めの行, 残す行) → (4) __st.finished になったら JSON.stringify(__res) をシートに書く
(() => {
  // ガラケー＋PS Vita：[機種, タイトルに含む言葉の正規表現, ピンクを分ける, 検索語（省略可）]
  const FIXED = [["902KC","902KC|903KC",1],["KY-42C","KY-?42C"],["A202KC","A202KC"],["601SH","601SH|602SH",1],["KYF31","KYF31"],["SH-02L","SH-?02L"],
    ["701KC","701KC"],["805SH","805SH"],["SH-01J","SH-?01J"],["A205SH","A205SH"],["KYF42","KYF42"],["A203KC","A203KC"],
    ["602SH","601SH|602SH",1],["806SH","806SH"],["501KC","501KC"],["PCH-2000","vita|2000"],["PCH-1000","vita|1000"]];
  // 設定B12のうち数えないもの（ガラケーは上で扱う・Vita以外のゲーム機はタイトルで機種を分けられない）
  const SKIP = /^(902KC|903KC|KY-42C|A202KC|601SH|602SH|KYF31|SH-02L|701KC|805SH|SH-01J|A205SH|KYF42|A203KC|806SH|501KC|P-01G|PCH-\d+|PSP-.*|DMG-01|MGB-\d+|CGB-001|AGB-001|AGS-001|OXY-001|USG-001|TWL-001|CTR-001|SPR-001|RED-001|UTL-001|WSC-001|NEOP-10|KTR-001|JAN-001|SCT-001)$/i;
  const QUERY = { "IXY 650": "ELPH 360" };   // 海外名で探すもの
  const PREFIX = /^(DSC-|DMC-|EX-|WM-|HDR-|DCR-|D-|SL-|MZ-|FinePix |RICOH )/i;
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const reFor = k => {   // DSC-W830 → (?<![A-Za-z0-9])W830(?![0-9])、IXY 650 → IXY[- ]?650、GW-5000 → GW[- ]?5000
    const core = k.replace(PREFIX, "").split(/[- ]/).map(esc).join("[- ]?");
    return "(?<![A-Za-z0-9])" + core + "(?![0-9])";
  };
  const ACC = /battery|film|protector|glass|case only|carrying case|hard case|game case|cover|charger only|cable|adapter|skin|decal|replacement|parts|lcd|housing|shell|button|joystick|stick|port|motherboard|stand|grip|pouch|bag|strap|mock|dummy|sticker|game only|cartridge only|manual only|box only|empty box|for parts|not working|junk/i;

  // (2) ページが送る問い合わせのひな形を1回つかまえる（「Research」ボタンを押して同じ検索をもう一度出すだけ）
  window.__setup = async () => {
    if (window.__US) return "ok";
    const of = window.__origFetch || window.fetch; window.__origFetch = of;
    window.fetch = function (u, o) {
      try { if (String(u && u.url || u).includes("graphql") && o && typeof o.body === "string" && o.body.includes("UnifiedSearch")) window.__US = JSON.parse(o.body); } catch (e) {}
      return of.apply(this, arguments);
    };
    const btn = [...document.querySelectorAll("button")].find(b => b.innerText.trim() === "Research");
    if (!btn) return "Researchボタンが見つからない（ログイン切れ？）";
    btn.click();
    for (let i = 0; i < 40 && !window.__US; i++) await new Promise(r => setTimeout(r, 250));
    return window.__US ? "ok" : "ひな形をつかめなかった";
  };

  const fetchPage = async (kw, offset) => {
    const b = JSON.parse(JSON.stringify(window.__US)); const v = b.variables;
    const end = new Date(); const start = new Date(end - 90 * 864e5);
    v.terapeakResearchInput.title = kw; v.activeResearchInput.title = kw;
    v.terapeakResearchInput.dateTimeRange = { startDateTime: start.toISOString(), endDateTime: end.toISOString() };
    v.terapeakListingInput.pagination = { limit: 50, offset };
    v.includeActiveListingCount = false; v.includeSoldAggregates = false;
    for (let t = 0; t < 3; t++) {
      try {
        const r = await (window.__origFetch || fetch)("/sh/research/graphql", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b), credentials: "include" });
        const j = await r.json(); const m = j.data && j.data.soldSearchResults && j.data.soldSearchResults.listingMetrics;
        if (m) return m;
      } catch (e) {}
      await new Promise(r => setTimeout(r, 1500 * (t + 1)));
    }
    throw new Error("取れなかった: " + kw);
  };
  const listings = async kw => {
    let out = [];
    for (let p = 0; p < 10; p++) {   // 50件×最大10ページ
      const m = await fetchPage(kw, p * 50);
      out = out.concat((m.listings || []).map(x => ({ t: x.listing.title, p: +x.avgSoldPrice.amount, s: +((x.avgShipping || {}).amount || 0), n: +x.itemSoldCount })));
      if (!m.nextPageAvailable) break;
    }
    return out;
  };
  const q = (a, x) => a[Math.min(a.length - 1, Math.floor(a.length * x))];
  const grp = (con, f) => {
    const c = con.filter(f); const ps = c.flatMap(x => Array(Math.min(x.n, 50)).fill(Math.round((x.p + x.s) * 100) / 100)).sort((a, b) => a - b);
    return ps.length ? [q(ps, .25), q(ps, .5), q(ps, .75), c.reduce((a, x) => a + x.n, 0)] : ["", "", "", 0];   // [安い, ふつう, 高い, 台数]（ドル）
  };

  // (3) 全機種を4つずつ並べて読み、シートにそのまま書ける行を作る
  // b12：設定B12の文字、startRow：書き始める行番号、keep：残す「売れた値段」の行（ここで数えない機種。12列のまま）
  window.__start = (b12, startRow, keep) => {
    const others = b12.split(/[,、]/).map(s => s.trim()).filter(s => s && !SKIP.test(s));
    const M = FIXED.concat([...new Set(others)].map(k => [k, reFor(k), 0, QUERY[k]]));
    window.__st = { done: 0, total: M.length, err: [] }; window.__res = null;
    const now = new Date(Date.now() + 9 * 36e5).toISOString().slice(0, 16).replace("T", " ").replace(/-/g, "/");
    const out = new Array(M.length);
    let i = 0;
    const worker = async () => {
      while (i < M.length) {
        const idx = i++; const [k, re, pink, query] = M[idx];
        try {
          const rx = new RegExp(re, "i");
          const con = (await listings(query || k)).filter(x => !ACC.test(x.t) && x.p >= 40 && rx.test(x.t));   // 本体だけ（付属品・部品・$40未満を除く）
          const note = "Terapeak90日・送料込み";
          if (pink) {
            const both = /^60[12]SH$/.test(k) ? "・601SH/602SHまとめて" : "";
            out[idx] = [[k, "ピンク", grp(con, x => /pink/i.test(x.t)), note + "・ピンクだけ" + both],
                        [k, "", grp(con, x => !/pink/i.test(x.t)), note + "・ピンク以外" + both]];
          } else out[idx] = [[k, "", grp(con, () => true), note]];
        } catch (e) {   // 取れなかった機種は日付を空にする（ツールはその行を使わず、今まで通りの計算に戻る）
          window.__st.err.push(k);
          out[idx] = (pink ? ["ピンク", ""] : [""]).map(c => [k, c, ["", "", "", ""], "今月は取れなかった"]);
        }
        window.__st.done++;
      }
    };
    Promise.all([worker(), worker(), worker(), worker()]).then(() => {
      const rows = out.flat().map(([k, c, g, note]) => [k, c, "売れた値段", "", "", "", g[3], note === "今月は取れなかった" ? "" : now, note, g[0], g[1], g[2]]);
      const key = s => String(s).toUpperCase().replace(/[-\s]/g, "");
      const done = new Set(M.map(m => key(m[0])));
      const all = rows.concat((keep || []).filter(r => !done.has(key(r[0]))).map(r => (r.concat(Array(12).fill(""))).slice(0, 12)));
      // 円（D〜F列）は先頭行の1つの式で全行ぶん計算する（ドル×設定E9の今のレート）。週1回の書き換えで1行ずつの式に戻っても同じ結果
      const last = startRow + all.length - 1;
      window.__res = all.map((r, j) => { r[3] = r[4] = r[5] = ""; return r; });
      if (all.length) window.__res[0][3] = `=ARRAYFORMULA(IF(J${startRow}:L${last}="","",ROUND(J${startRow}:L${last}*'設定'!$E$9)))`;
      window.__last = last;
      window.__st.finished = true;
    });
    return "開始：" + M.length + "機種";
  };
})();
