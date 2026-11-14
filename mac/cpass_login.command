#!/bin/bash
# CPaSSのログインが切れたとき用（最初の1回も）：専用ブラウザでCPaSSが開くので、ログインする（10分以内）
# 「Log in with eBay account」→ eBayのパスワード（Bitwarden）→ 同意画面なら「Agree」。注文の一覧が出たら保存して自動で閉じる
nohup bash "$HOME/.local/stock-sync-mac/ship.sh" --login >/dev/null 2>&1 &
disown
sleep 2
osascript -e "tell application \"Terminal\" to close (every window whose name contains \"$(basename "$0")\")" >/dev/null 2>&1 &
