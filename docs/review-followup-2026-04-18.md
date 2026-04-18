# レビュー対応状況（2026-04-18）

この文書は、2026-04-18 時点の指摘事項に対する対応状況を記録します。

## 対応表

| # | 指摘内容 | 状況 | 補足 |
|---|---|---|---|
| 1 | 指摘番号に対応した follow-up 文書がない | 対応済み | 本文書を追加。項目ごとに状態と判断理由を明記。 |
| 2 | `pipeline.run_once` の責務が集中して追いにくい | 対応済み | 例外処理・結果組み立て・永続化をヘルパーへ分割。 |
| 3 | `discord_client` が `requests.post(..., timeout=10)` 固定 | 対応済み | `requests.Session` 再利用 + timeout を `(connect, read)` に分離。 |
| 4 | `RuntimeConfig.__getattr__` に移行促進の警告がない | 対応済み | `DeprecationWarning` を追加（同一属性は重複警告抑制）。 |

## 実装メモ

- `amazon_notify/pipeline.py`
  - `run_once` を整理し、以下の補助関数へ分離:
    - `_process_envelope`
    - `_handle_pipeline_error`
    - `_handle_unexpected_error`
    - `_build_run_result`
    - `_persist_run_result`
  - 既存の失敗時動作（イベント記録、retry/alert フラグ、auth 再評価）は維持。

- `amazon_notify/discord_client.py`
  - モジュールレベル `requests.Session` を導入。
  - timeout を `(_connect_timeout, _read_timeout)` 形式へ変更。

- `amazon_notify/runtime.py`
  - 旧 flat 属性参照時に `DeprecationWarning` を発行。
  - 警告は属性ごとに 1 回のみ発行。

## 未対応

現時点でこのレビューセットに対する未対応項目はありません。
