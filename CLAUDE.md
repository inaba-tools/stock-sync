# stock-sync（公開側・薄い形）

ハコペン（eBay無在庫輸出ツール）の**中身と作業メモは非公開リポジトリ inaba-tools/stock-sync-core** にある。作業するときは、まずそちらを取ってきて、そちらの CLAUDE.md を読むこと。

ここに置いているのは公開が必要なものだけ：
- `.github/workflows/`：Actions（公開側で動かす＝無料）。最初に Secrets `CORE_TOKEN`（読み取り専用）で非公開側を取ってきて動かす。ログには中身を出さない
- `docs/`：ハコペンの画面（GitHub Pages）
- `mac/`・`terapeak.js`・`terapeak.md`：Macが公開URLから取り直して使う

**このリポジトリは公開**なので、個人名・メールアドレス・キー・管理表のID・仕組みの詳細は書かない。
