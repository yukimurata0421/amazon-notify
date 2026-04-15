# Engineering Decisions

このドキュメントは、`amazon-notify` で採用している設計・技術選定の理由をまとめたものです。  
対象は `main` ブランチ時点の実装です。
リリース単位の変更意図は `docs/IMPLEMENTATION_RATIONALE_JA.md` を参照してください。

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
- Gmail/Discord の技術詳細を core から分離し、処理仕様（checkpoint をいつ進めるか）を中心にできる
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

### 書き込み順序
- `checkpoint_advanced` を先に `events.jsonl` へ追記する
- `state.json` 更新はベストエフォート（失敗時は warning のみ）

これにより「正本が更新されず派生だけ進む」状態を避ける。

### 移行戦略
- `events.jsonl` が空で `state.json.last_message_id` がある初回のみ bootstrap
- bootstrap 時に `checkpoint_advanced` を 1 回書いて移行完了

### runtime artifact の役割境界
- `events.jsonl`: checkpoint 正本（判断の一次情報）
- `state.json` / `runs.jsonl`: 正本から派生する互換・監査情報
- `*.index.json`: 再生成可能 cache（正本ではない）
- `.discord_dedupe_state.json` + lock: 通知重複抑止の coordination state

## 4. Ordered Frontier（途中失敗で停止）を採用した理由

### 採用ポリシー
- oldest-first で処理
- `message_detail_failed` / `delivery_failed` 発生時はその run を停止
- checkpoint は成功した最後のメッセージまでしか進めない

### 理由
- 後続メッセージだけ進めると frontier に穴が開き、再現性が落ちる
- 個人運用では throughput より整合性とデバッグ容易性を優先

## 5. 例外をポリシー分類で扱う理由

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
  - 再試行可否
  - alert が必要か
  - checkpoint 進行可否
で判断できる
- 運用ポリシーをコード上で読み取りやすい

## 6. 認証を状態列挙で扱う理由

### 採用
- `AuthStatus`（`domain.py`）
- Gmail 認証状態遷移は `gmail_auth.py`、互換ファサードは `gmail_client.py`

### 理由
- 長い if/except を「遷移結果」に変換して判定を統一できる
- `health-check` と `runs.jsonl` に auth 状態を載せられる
- アラート抑制や incident 管理と組み合わせやすい
- 認証処理本体と API 呼び出し/公開 interface を分離し、変更時の影響範囲を限定できる

## 7. Incident lifecycle を入れた理由

### 採用
- `incident_opened`
- `incident_suppressed`
- `incident_recovered`
- 一時障害アラートの境界制御:
  - `transient_alert_min_duration_seconds`
  - `transient_alert_cooldown_seconds`

### 理由
- 同一障害での alert 連投を抑止し、運用ノイズを減らす
- 「発生中か」「復旧したか」を state と events で追跡できる
- 短時間の自己修復（瞬断）では通知せず、持続障害だけを通知して alert fatigue を抑える

## 8. JSONL durability を強化した理由

### 採用
- 1 レコードごとに `flush + fsync`
- `schema_version` を各レコードに付与
- 起動時に JSONL 末尾破損 1 行のみを無視して復元
- JSONL 中間行破損は fail-fast（`CheckpointError`）
- `state.json` は `tempfile + os.replace` で atomic write

### 理由
- 低コストでファイル破損耐性を上げる
- 将来のフォーマット変更時にマイグレーションしやすくする
- 途中行破損を無視すると checkpoint の解釈が不確定になるため、契約優先で停止する

### 追加判断: ディスク枯渇（ENOSPC）時の扱い

#### 採用
- JSONL 書き込み失敗時に `ENOSPC` を明示判定し、エラーメッセージに「ディスク容量不足の可能性」を含める。
- `failure event` や `run result` の永続化失敗はログに残し、可能な限り run 自体は継続して終了する。
- `run result` を永続化できない場合は `checkpoint_failed` として扱い、通常の障害通知フローに載せる。
- incident 状態書き込みが失敗した場合に備え、プロセス内メモリで同種通知を一時抑制する。

#### 理由
- 「整合性を守って停止する」だけでは、調査時に原因（容量枯渇）へ到達しづらい。
- 永続化失敗のたびに未処理例外化すると、障害検知導線が分断される。
- 状態ファイル自体が書けないケースでは、永続化ベースの抑制だけに依存すると通知連投リスクが残る。

#### 別端末情報の扱い
- 現実装は単体プロセス/単体ホスト前提で、別端末のメトリクス（外部監視）を直接参照しない。
- そのため本判断は「ローカルで観測できる `OSError` とログ」を一次情報として設計している。
- 将来的に別端末からディスク使用率や inode 枯渇を取得できる場合は、そちらを一次根拠にしてアラート精度を上げる余地がある。

## 9. 設定検証を「意味」まで広げた理由

### 採用
- `--validate-config` で:
  - 型/必須キーだけでなく
  - `poll_interval_seconds` の下限
  - runtime path 解決可能性
も確認

### 理由
- 起動後障害を減らし、事前に設定ミスを検知する
- 「読める config」から「運用できる config」へ検証を引き上げる

## 10. テスト戦略を契約寄りにした理由

### 採用
- 関数単体だけでなく、契約を固定:
  - 通知成功時だけ checkpoint が進む
  - delivery/detail failure で frontier が保持される
  - auth failure が記録される
  - bootstrap / source-of-truth / JSONL 復元が成立する

### 理由
- v0.4.0 の価値は機能追加より「仕様固定」
- 実装詳細の変更に強い回帰防止が必要

## 11. CI と品質ゲート

### 採用
- Ruff
- mypy
- pytest
- coverage fail-under (`90%`)
- `structured_logging=true` による JSON 構造化ログ（任意）

### 理由
- 構造化した設計ほど、静的チェックと契約テストで維持コストを下げられる
- coverage を目安ではなく下限にして、仕様の後退を防ぐ
- JSON ログを有効化した場合、障害解析時の検索・集計を機械処理しやすくできる

## 12. あえて採用しなかったもの

- DB（SQLite/ORM）
- 非同期化
- Gmail History API
- 自前メッセージキュー基盤
- 過度なプラグイン化

### 理由
- 個人運用・軽量ツールというプロダクト境界を守るため
- 現在の運用要件では、JSONL + 単一 frontier モデルが最小コストで十分
- DB を導入すると、スキーマ設計・マイグレーション・整合性管理まで必要になり、現スコープでは過剰設計になりやすい
- append-only JSONL は「追記のみ」で非破壊性が高く、障害解析や監査時に履歴をそのまま追いやすい
- このツールの要件では、RDB のテーブル分割・JOIN・複雑クエリが不要で、JSONL で十分に要件を満たせる
- マネージドな Pub/Sub は trigger 経路として利用するが、永続ワークフローや再配送制御を担う自前基盤は導入しない

## 13. JSONL index snapshot を追加した理由

### 採用
- `events.jsonl.checkpoint.index.json`
- `runs.jsonl.summary.index.json`

### 理由
- 長期運用で JSONL 全走査コストが線形に伸びる問題を緩和するため。
- 正本は append-only JSONL のまま維持しつつ、起動時/health 時の読み取りを高速化するため。
- index は再生成可能な cache として扱い、正本性を持たせないため。

## 14. guard 経路の未処理例外を `RunResult` に収束させた理由

### 採用
- `run_once_with_guard` の未処理例外を `report_unhandled_exception` 経由で `source_failed` event + `RunResult` として永続化する。

### 理由
- 「通常 failure path」と「未処理例外 path」で通知・状態更新ポリシーが二重化するのを避けるため。
- incident lifecycle / run summary / alert 導線を同じ契約面に統一するため。

## 15. incident のメモリ抑制を module global から外した理由

### 採用
- 抑制マップを `RuntimeConfig` の mutable フィールドから切り離し、`notifier` 内のプロセスキャッシュ（`state_file` 単位）で管理する。

### 理由
- 設定オブジェクトに mutable state を混在させず、設定責務と実行時メモリ責務を分離するため。
- `state_file` ごとの分離を保ちながら、同一 runtime 内では抑制状態を継続利用できるようにするため。
- テスト分離性を高め、fixture 依存の隠れた副作用を減らすため。

## 16. Discord dedupe lock を fail-fast にした理由

### 採用
- `fcntl` が使えない環境では dedupe lock 経路を fail-fast とし、`--health-check` の `dedupe_lock_supported` で可視化する。

### 理由
- lock が静かに劣化すると、重複通知が断続的に発生して原因追跡が難しくなるため。
- 非対応環境を明示したほうが運用上の誤解を減らせるため。

## 17. Discord dedupe state path を runtime 注入へ統一した理由

### 採用
- `.discord_dedupe_state.json` の解決を `discord_client.py` 内の暗黙 path 解決から外し、`RuntimeConfig` の `discord_dedupe_state_file` を明示注入する。
- `--test-discord`、通常通知、alert/recovery の全経路で同じ runtime 基準 path を使う。

### 理由
- `--config` 切り替え時の runtime artifact 配置規則を統一し、状態参照先の不一致を避けるため。
- dedupe だけ別系統で path 解決すると、再現しづらい運用不整合が残るため。

## 18. Gmail 実装を auth / transient state / facade に分割した理由

### 採用
- `gmail_auth.py`: OAuth/credential/refresh/auth-state 遷移
- `gmail_transient_state.py`: transient/token issue lifecycle と state 更新
- `gmail_client.py`: 互換ファサードと公開 API の集約

### 理由
- 認証、障害状態管理、API 呼び出しを分離してレビュー/テスト境界を明確化するため。
- 既存呼び出し側の import 面を壊さずに内部責務を再編できるため。

## 19. StreamingPull の集約/重複スキップ意図をコードコメントで明示した理由

### 採用
- `history_id` の latest 集約、duplicate skip、heartbeat atomic write の意図を実装箇所に明示する。

### 理由
- Pub/Sub を durable workflow queue ではなく trigger 経路として扱う前提を、コード上で読み取れるようにするため。
- Gmail catch-up 前提の設計意図をコメントとして固定し、将来の誤修正を減らすため。

## 20. Polling catch-up で paginated listing + checkpoint-not-found fail-safe を採用した理由

### 採用
- Gmail 一覧取得をページング対応し、checkpoint に到達するまで oldest-first で走査する。
- 一覧上で checkpoint が見つからない場合でも、未知境界を飛び越えて checkpoint を進めない（fail-safe）。

### 理由
- backlog が大きい状況でも frontier の穴を作らないため。
- 「一覧 API の窓から落ちた checkpoint」に対して安全側に倒し、未確認領域を既読扱いしないため。

## 21. 一時障害しきい値の負値を warning + clamp で扱う理由

### 採用
- `transient_alert_min_duration_seconds < 0` は例外停止ではなく warning を出し、`0` にクランプして継続する。

### 理由
- 設定ミスを原因に通知パイプライン全体が停止するリスクを避けるため。
- fail-fast より「安全側で継続 + 可視化」を優先するため。

## 22. 長期運用向けに JSONL の rotation / archive / restore drill を文章化した理由

### 採用
- `docs/OPERATIONS.md` / `OPERATIONS.en.md` に、append-only 正本と `rebuild-indexes` 前提のまま、次を明示する節を追加した。
  - rotation 方針（正本の途中削除を避ける、index は再生成可）
  - アーカイブ形式（同一タイムスタンプの events/runs、gzip、任意 manifest）
  - restore 手順（停止 → 復元 → rebuild → `--doctor` → 起動）
  - 削除してよいものの表（index は可、正本 truncate は不可 等）
  - `restore drill`（年に一度でもよい検証手順）

### 理由
- 「動く」だけでなく、**寿命管理と障害復旧の物語**がドキュメントにないと、長期運用で次に効くのは堅牢性より**運用の再現性**だから。
- 実装は既に append-only + 派生 state/index であるため、**運用の完成度**は「何を消してよいか」「どう戻すか」が言語化されているかで決まる。
- 本リポジトリは自動デプロイしない前提のため、手順は README ではなく **運用ガイドに集約**する。

## 23. fault-injection の scenario harness を CLI で持つ理由

### 採用
- `--scenario-harness` / `--scenario-names`
- 実装: `amazon_notify/scenario_harness.py`
- 検証シナリオ:
  - Gmail transient failure
  - Discord 429 / timeout retry
  - checkpoint 更新前後での state/event 不整合窓
  - truncated / corrupted JSONL
  - read-only / ENOSPC 近辺
  - stale incident state

### 理由
- 単体テストは関数契約を守れるが、運用では複合条件で壊れる。  
  そのため「設計思想が複合障害でも崩れないこと」を、1コマンドで定期確認できる面を持つ。
- CI に閉じた検証だけでなく、運用者が現場で再実行できる診断導線として価値がある。

## 24. `--verify-state` を `--doctor` と分けた理由

### 採用
- `--doctor`: runtime 状態の広い診断（人間向け詳細 JSON）
- `--verify-state`: append-only 正本と派生物の追加監査（定期実行向け JSON）
  - checkpoint event timestamp monotonicity
  - incident event lifecycle validity

### 理由
- `doctor` は「今の状態説明」、`verify-state` は「静かな破損を検査」の役割で分離したほうが運用設計が明確になる。
- 同じ JSON でも用途が異なるため、cron / external monitor で意図が伝わるコマンド名を持たせた。

## 25. 最小運用メトリクスを外部へ出す理由

### 採用
- `--metrics`（JSON）
- `--metrics-plain`（簡易テキスト）
- `--metrics-window`（直近 run 集計窓）
- 実装: `amazon_notify/status.py` の `build_metrics_report()`

### 理由
- 本プロジェクトは大規模監視基盤を前提にしないため、薄い exporter 面を先に用意する方が費用対効果が高い。
- 「状態説明（status/doctor）」と「傾向把握（metrics）」を分けることで、運用者が障害兆候を早期に掴みやすくなる。

## 26. retention / archive / restore drill をコマンド化した理由

### 採用
- `--archive-runtime` / `--archive-label` / `--archive-dir` / `--archive-no-gzip`
- `--restore-runtime` / `--restore-label`
- `--restore-drill`
- 実装: `amazon_notify/retention.py`
  - snapshot archive + manifest
  - restore 後に index rebuild + verify
  - 一時ディレクトリでの非破壊 drill

### 理由
- append-only 設計は「壊れにくさ」には強いが、長期運用では「寿命管理」と「復元手順の再現性」が支配的になる。
- ドキュメント手順だけでは drift するため、実際にコマンドとして保持し、定期的に drill 可能にした。

## 27. Gmail Source の依存注入を Protocol + Adapter に集約した理由

### 採用
- `GmailMailSource` に分散していた関数注入を `GmailClient` Protocol で束ね、既定実装として `GmailClientAdapter` を導入した。
- notifier 側は Gmail 境界（service/status, list/detail, retry 判定, transient/recovery 通知）を 1 オブジェクト注入に統一した。

### 理由
- コンストラクタ引数の肥大化を抑え、テスト差し替え境界を明確にするため。
- Gmail 境界を明示的な型契約に閉じ込めることで、将来変更の影響範囲を制御しやすくするため。

## 28. StreamingPull trigger 実行経路を共通化した理由

### 採用
- idle trigger と message trigger に重複していた成功/失敗/heartbeat/backoff 更新を `_run_trigger_once` に集約した。

### 理由
- 同種ロジックの分岐重複を減らし、運用時の failure semantics のズレを防ぐため。
- heartbeat と連続失敗カウントの扱いを 1 箇所に固定し、回帰時の検証コストを下げるため。
