#!/bin/bash
# 1回だけ：Macに月1回の自動実行を入れる（ダブルクリックで動く）
D="$HOME/.local/stock-sync-mac"
mkdir -p "$D" "$HOME/Library/LaunchAgents"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
RAW="https://raw.githubusercontent.com/inaba-tools/stock-sync/main"
for f in mac/run.sh mac/run.py mac/browser.sh mac/login.command mac/test.command terapeak.js; do curl -fsSL "$RAW/$f" -o "$D/$(basename $f)"; done
chmod +x "$D/run.sh" "$D/browser.sh" "$D"/*.command
P="$HOME/Library/LaunchAgents/com.stocksync.terapeak.plist"
cat > "$P" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.stocksync.terapeak</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$D/run.sh</string></array>
<key>StartCalendarInterval</key><dict><key>Day</key><integer>1</integer><key>Hour</key><integer>19</integer><key>Minute</key><integer>47</integer></dict>
<key>StandardOutPath</key><string>$D/launchd.txt</string>
<key>StandardErrorPath</key><string>$D/launchd.txt</string>
</dict></plist>
PL
launchctl bootout gui/$(id -u) "$P" 2>/dev/null
launchctl bootstrap gui/$(id -u) "$P" && echo "launchd OK" > "$D/setup_result.txt" || echo "launchd NG" > "$D/setup_result.txt"
# Chrome for Testing（無料・Playwright付属）を入れて、場所を覚える
uv run --quiet --python 3.12 --with playwright==1.55.0 --with requests python -m playwright install chromium >> "$D/setup_result.txt" 2>&1
EXE="$(uv run --quiet --python 3.12 --with playwright==1.55.0 --with requests python "$D/run.py" --path)"
echo "${EXE%%.app/*}.app" > "$D/browser_app.txt"
echo "browser: $(cat "$D/browser_app.txt")" >> "$D/setup_result.txt"
echo "おわり。このウィンドウは閉じてOKです"
# 終わったらこのターミナルの窓を閉じる（2026/11/10 本人の希望）
osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$(basename "$0")\")" >/dev/null 2>&1 &
