# Engineering Decisions (v0.2.0)

このドキュメントは、`amazon-notify` で採用している設計・技術選定の理由をまとめたものです。  
対象は v0.2.0 時点の実装です。

## 1. プロダクト前提

- 個人運用向けの軽量通知ツール
- Gmail から Amazon 配送系メールを検出し Discord に通知する
- 最優先は「通知の取りこぼし防止」よりも「frontier（処理境界）の整合性保持」
- 単一プロセス・単一インスタンス運用を前提

## 2. コア設計をパイプライン化した理由

### 採用
- `NotificationPipeline`（`pipeline.py`）
- ドメイン型（`domain.py`）:
  - `MailEnvelope`
  - `NotificationCandidate`
  - `Checkpoint`
  - `AuthStatus`
  - `RunResult`
- 抽象境界:
  - `MailSource`
  - `Classifier`
  - `Notifier`
  - `CheckpointStore`

### 理由
- Gmail/Discord の技術詳細を core から分離し、業務仕様（checkpoint をいつ進めるか）を中心にできる
- `run_once` をトランザクションとして扱い、成功/失敗時の挙動を固定しやすくなる
- 将来の差し替え（通知先追加、入力ソース差し替え）を低コストにする

## 3. Checkpoint を `events.jsonl` 正本にした理由

### 採用
- 正本: `events.jsonl`（`checkpoint_advanced`）
- 派生:
  - `state.json`（互換スナップショット）
  - `runs.jsonl`（監査・可観測性）

### 理由
- source of truth を 1 つに絞り、静かな不整合を防ぐ
- append-only で監査可能な履歴を残せる
- rollback 時の互換性確保のため `state.json` は継続更新する

### 移行戦略
- `events.jsonl` が空で `state.json.last_message_id` がある初回のみ bootstrap
- bootstrap 時に `checkpoint_advanced` を 1 回書いて移行完了

## 4. Ordered Frontier（途中失敗で停止）を採用した理由

### 採用ポリシー
- oldest-first で処理
- `message_detail_failed` / `delivery_failed` 発生時はその run を停止
- checkpoint は成功した最後のメッセージまでしか進めない

### 理由
- 後続メッセージだけ進めると frontier に穴が開き、再現性が落ちる
- 個人運用では throughput より整合性とデバッグ容易性を優先

## 5. 例外を業務分類で扱う理由

### 採用
- `errors.py`
  - `TransientSourceError`
  - `PermanentAuthError`
  - `MessageDecodeError`
  - `DeliveryError`
  - `CheckpointError`
  - `ConfigError`

### 理由
- `HttpError` / `TimeoutError` のような技術例外ではなく、
  - 再試行すべきか
  - alert が必要か
  - checkpoint を進めるべきか
 で判断できる
- 運用ポリシーをコード上で読み取りやすい

## 6. 認証を状態列挙で扱う理由

### 採用
- `AuthStatus`（`domain.py`）
- Gmail 認証処理は `gmail_client.py` で状態を返す

### 理由
- 長い if/except を「遷移結果」に変換して判定を統一できる
- `health-check` と `runs.jsonl` に auth 状態を載せられる
- アラート抑制や incident 管理と組み合わせやすい

## 7. Incident lifecycle を入れた理由

### 採用
- `incident_opened`
- `incident_suppressed`
- `incident_recovered`

### 理由
- 同一障害での alert 連投を抑止し、運用ノイズを減らす
- 「発生中か」「復旧したか」を state と events で追跡できる

## 8. JSONL durability を強化した理由

### 採用
- 1 レコードごとに `flush + fsync`
- `schema_version` を各レコードに付与
- 起動時に JSONL 末尾破損 1 行を無視して復元

### 理由
- 低コストでファイル破損耐性を上げる
- 将来のフォーマット変更時にマイグレーションしやすくする

## 9. 設定検証を「意味」まで広げた理由

### 採用
- `--validate-config` で:
  - 型/必須キーだけでなく
  - `poll_interval_seconds` の下限
  - runtime path 解決可能性
 も確認

### 理由
- 起動後障害を減らし、事前に運用ミスを検知する
- 「読める config」から「運用できる config」へ検証を引き上げる

## 10. テスト戦略を契約寄りにした理由

### 採用
- 関数単体だけでなく、契約を固定:
  - 通知成功時だけ checkpoint が進む
  - delivery/detail failure で frontier が保持される
  - auth failure が記録される
  - bootstrap / source-of-truth / JSONL 復元が成立する

### 理由
- v0.2.0 の価値は機能追加より「仕様固定」
- 実装詳細の変更に強い回帰防止が必要

## 11. CI と品質ゲート

### 採用
- Ruff
- mypy
- pytest
- coverage fail-under (`90%`)

### 理由
- 構造化した設計ほど、静的チェックと契約テストで維持コストを下げられる
- coverage を目安ではなく下限にして、仕様の後退を防ぐ

## 12. あえて採用しなかったもの

- DB（SQLite/ORM）
- 非同期化
- Gmail History API
- メッセージキュー
- 過度なプラグイン化

### 理由
- 個人運用・軽量ツールというプロダクト境界を守るため
- 現在の運用要件では、JSONL + 単一 frontier モデルが最小コストで十分
- DB を導入すると、スキーマ設計・マイグレーション・整合性管理まで必要になり、現スコープでは過剰設計になりやすい
- append-only JSONL は「追記のみ」で非破壊性が高く、障害解析や監査時に履歴をそのまま追いやすい
- このツールの要件では、RDB のテーブル分割・JOIN・複雑クエリが不要で、JSONL で十分に要件を満たせる
