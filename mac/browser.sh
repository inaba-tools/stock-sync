#!/bin/bash
# 専用ブラウザ（Chrome for Testing）を開く。eBayのログイン・Bitwardenなど本人が使うとき用（自動実行と同じ起動の仕方にしないとログインが読めない）
D="$HOME/.local/stock-sync-mac"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
APP="$(cat "$D/browser_app.txt")"
# もう開いているときは前に出すだけ（同じページのタブが増えないように・2026/11/10 本人の希望）
if pgrep -f "user-data-dir=$D/browser" >/dev/null 2>&1; then
  bash "$D/ship.sh" --show   # 11/12 ふだんは見えないところで動いているので、15分だけ画面に出す
  exit 0
fi
touch "$D/showing"
# 前回のタブは持ち越さず、eBayのページを1つだけ開く（ログインは残る）
uv run --quiet --python 3.12 --with playwright==1.55.0 --with requests python "$D/run.py" --prefs
open -na "$APP" --args --user-data-dir="$D/browser" --use-mock-keychain --password-store=basic --no-first-run --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 "${1:-https://www.ebay.com/sh/research?marketplace=EBAY-US&tabName=SOLD}"
