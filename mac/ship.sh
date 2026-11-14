#!/bin/bash
# launchd から5分ごとに呼ばれる（発送の自動化）。最新の ship.py をGitHubから取り直してから動かす
D="$HOME/.local/stock-sync-mac"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$D" || exit 1
# 前の回がまだ動いていたら何もしない（2時間以上前のロックは壊れたものとして外す）。ログイン用（--login）は別に動いてよい
if [ "$1" != "--login" ] && [ "$1" != "--show" ] && ! mkdir "$D/ship.lock" 2>/dev/null; then
  find "$D/ship.lock" -maxdepth 0 -mmin +120 -exec rmdir {} \; 2>/dev/null
  exit 0
fi
[ "$1" != "--login" ] && [ "$1" != "--show" ] && trap 'rmdir "$D/ship.lock" 2>/dev/null' EXIT
RAW="https://raw.githubusercontent.com/inaba-tools/stock-sync/main"
curl -fsSL --retry 2 "$RAW/mac/ship.py" -o "$D/ship.py.new" && mv "$D/ship.py.new" "$D/ship.py"
# ログが大きくなりすぎないように（1MBを超えたら古い分を捨てる）
[ -f "$D/ship_log.txt" ] && [ "$(wc -c < "$D/ship_log.txt")" -gt 1000000 ] && tail -c 200000 "$D/ship_log.txt" > "$D/ship_log.tmp" && mv "$D/ship_log.tmp" "$D/ship_log.txt"
caffeinate -i uv run --quiet --python 3.12 --with playwright==1.55.0 --with requests --with pypdfium2 --with pillow \
  python "$D/ship.py" "$@" >> "$D/ship_log.txt" 2>&1
