#!/bin/bash
# 1回だけ：発送の自動化（5分ごとの見張り）をMacに入れる（ダブルクリックで動く）。月1回の setup.command が済んでいる前提
D="$HOME/.local/stock-sync-mac"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p "$D/labels" "$HOME/Library/LaunchAgents"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
RAW="https://raw.githubusercontent.com/inaba-tools/stock-sync/main"
for f in mac/ship.sh mac/ship.py; do curl -fsSL "$RAW/$f" -o "$D/$(basename $f)"; done
chmod +x "$D/ship.sh"
P="$HOME/Library/LaunchAgents/com.stocksync.ship.plist"
cat > "$P" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.stocksync.ship</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>$D/ship.sh</string></array>
<key>StartInterval</key><integer>300</integer>
<key>StandardOutPath</key><string>$D/launchd_ship.txt</string>
<key>StandardErrorPath</key><string>$D/launchd_ship.txt</string>
</dict></plist>
PL
launchctl bootout gui/$(id -u) "$P" 2>/dev/null
R="$D/ship_setup_result.txt"
launchctl bootstrap gui/$(id -u) "$P" && echo "launchd OK" > "$R" || echo "launchd NG" > "$R"
echo "printer: $(lpstat -d 2>&1)" >> "$R"
[ -f "$D/env" ] && echo "env OK" >> "$R" || echo "env なし" >> "$R"
# 初回は部品の準備に少し時間がかかるので、ここで1回動かしておく
bash "$D/ship.sh"; tail -3 "$D/ship_log.txt" >> "$R" 2>/dev/null
echo "おわり。このウィンドウは閉じてOKです"
osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$(basename "$0")\")" >/dev/null 2>&1 &
