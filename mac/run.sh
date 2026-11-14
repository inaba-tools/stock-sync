#!/bin/bash
# launchd から毎月1日 19:47 に呼ばれる。最新の run.py・terapeak.js をGitHubから取り直してから動かす（Mac側の作り直し不要）
D="$HOME/.local/stock-sync-mac"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$D" || exit 1
RAW="https://raw.githubusercontent.com/inaba-tools/stock-sync/main"
for f in mac/run.py terapeak.js mac/browser.sh; do
  curl -fsSL --retry 3 "$RAW/$f" -o "$D/$(basename $f).new" && mv "$D/$(basename $f).new" "$D/$(basename $f)"
done
# 専用ブラウザは閉じない（閉じるとCPaSSのログインが消えるため・11/12）。run.py は開いているブラウザに横からつなぐ
caffeinate -i uv run --quiet --python 3.12 --with playwright==1.55.0 --with requests python "$D/run.py" "$@" >> "$D/log.txt" 2>&1
