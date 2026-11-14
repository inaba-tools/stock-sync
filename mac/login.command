#!/bin/bash
# eBayのログインが切れたとき用：専用ブラウザでeBayを開く（ログインして「サインインしたままにする」にチェック）
bash "$HOME/.local/stock-sync-mac/browser.sh"
# 終わったらこのターミナルの窓を閉じる（2026/11/10 本人の希望）
osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$(basename "$0")\")" >/dev/null 2>&1 &
