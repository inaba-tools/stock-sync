#!/bin/bash
# 試しに今すぐ1回動かす（失敗してもメールは出さない）
NO_MAIL=1 bash "$HOME/.local/stock-sync-mac/run.sh"; tail -5 "$HOME/.local/stock-sync-mac/log.txt"
# 終わったらこのターミナルの窓を閉じる（2026/11/10 本人の希望）
osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$(basename "$0")\")" >/dev/null 2>&1 &
