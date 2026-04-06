[![CI](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/yukimurata0421/amazon-notify/main/.github/badges/coverage.json)](https://github.com/yukimurata0421/amazon-notify/blob/main/.github/badges/coverage.json)
[![Lint](https://img.shields.io/badge/lint-ruff-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
[![Types](https://img.shields.io/badge/types-mypy-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)

# Amazon Notify (日本語)

Amazon.co.jp の配送関連メールを Gmail API で検出し、Discord Webhook に通知する自己ホスト向けツールです。
通知速度より運用上の一貫性と復旧容易性を優先しています。
設計上の最優先は checkpoint/frontier の整合性維持です。
対象プラットフォームは Linux 単一ホストで、運用は systemd 中心です。

運用モードは 2 つあります。
- 単純な定期ポーリング
- Gmail Watch + Pub/Sub StreamingPull による準リアルタイム運用

English README: [README.md](./README.md)

## できること

補足: `main` ブランチは最新 GitHub Release より先行している場合があります。

- Ordered Frontier（oldest-first、途中失敗時はそこで停止）
- `events.jsonl` を checkpoint 正本とし、`state.json` は互換スナップショット、`runs.jsonl` は監査ログとして運用
- 長期運用でも起動/状態読み取りコストを抑えるため、再生成可能な index snapshot（`events.jsonl.checkpoint.index.json`、`runs.jsonl.summary.index.json`）を併用
- Gmail/Discord の一時障害に対するリトライと復旧通知
- 一時障害アラートの境界制御（継続時間しきい値 + クールダウン）
- guard 経路の未処理例外を `RunResult` / `source_failed` として永続化し、障害経路を一本化
- `fcntl` 非対応環境では Discord dedupe lock を fail-fast し、`--health-check` の `dedupe_lock_supported` で可視化
- Pub/Sub StreamingPull によるリアルタイム通知
- StreamingPull の自己復旧（systemd 依存を最小化）:
  - trigger 失敗時の指数バックオフ + 連続失敗しきい値
  - ストリーム切断時のプロセス内再接続バックオフ
- ハイブリッド高可用性構成:
  - メイン系: StreamingPull 常駐
  - サブ系: timer 起動ポーリング（watchdog 判定）
- `systemd` の再起動ループ抑止 + OnFailure 通知

## 保証すること

- 通知成功後にのみ処理済みとして扱います。
- Ordered Frontier（oldest-first、途中失敗時停止）を維持します。
- Pub/Sub は trigger として使い、取りこぼし回収は Gmail 側状態で行います。

## 非目標

- 複数インスタンスでの分散処理。
- Pub/Sub message 単位の厳密な永続ワークフロー管理。
- 汎用メール転送プラットフォーム化。

## クイックスタート

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
cp config.example.json config.json
# Pub/Sub や詳細パラメータを使う場合は config.full.example.json をベースにしてください
```

1. `config.json` の `discord_webhook_url` を設定
2. `credentials.json` を `config.json` と同じ場所に配置
3. `amazon-notify --reauth` を実行し、表示されたブラウザ OAuth を完了
4. `amazon-notify --once` で疎通確認

```bash
amazon-notify --reauth
amazon-notify --once
```

## よく使うコマンド

```bash
# 通常ポーリング常駐
amazon-notify

# 単発実行
amazon-notify --once

# 副作用なし確認
amazon-notify --once --dry-run

# Pub/Sub StreamingPull
amazon-notify --streaming-pull --pubsub-subscription projects/PROJECT/subscriptions/SUB

# Gmail watch 登録
amazon-notify --setup-watch --pubsub-topic projects/PROJECT/topics/TOPIC

# fallback watchdog つき単発
amazon-notify --once --fallback-watchdog
```

## 軽量 Docker で試す

```bash
docker build -t amazon-notify:slim .
docker run --rm amazon-notify:slim --help
docker run --rm -v "$(pwd):/work" amazon-notify:slim --config /work/config.json --validate-config
docker run --rm -v "$(pwd):/work" amazon-notify:slim --config /work/config.json --once --dry-run
```

位置づけ:
- 本番主系: Linux 単一ホスト + systemd-first 運用。
- Docker: クイック評価・再現テスト・CLI/runtime 境界の移植性確認用の補助導線。

補足: この軽量イメージは CLI 起動確認用です。`systemd`、hybrid HA/watchdog、複数コンテナ構成、本番 secret/監視設計はスコープ外です。

## ヘルスチェック補足

- `amazon-notify --health-check` に `dedupe_lock_supported` が含まれます。
- `fcntl` 非対応環境では `false` となり、dedupe lock 経路は非対応として fail-fast 動作になります。

## 追加依存

Pub/Sub を使う場合:

```bash
pip install -e .[pubsub]
```

開発用:

```bash
pip install -e .[dev]
```

## ドキュメント

- ハイブリッド導入手順（コピペ手順・エラー対処）: [docs/HYBRID_QUICKSTART_JA.md](./docs/HYBRID_QUICKSTART_JA.md)
- Hybrid quickstart (English): [docs/HYBRID_QUICKSTART.en.md](./docs/HYBRID_QUICKSTART.en.md)
- 環境依存パラメータ一覧（移植チェックリスト）: [docs/PORTABILITY_PARAMS_JA.md](./docs/PORTABILITY_PARAMS_JA.md)
- Portability parameters (English): [docs/PORTABILITY_PARAMS.en.md](./docs/PORTABILITY_PARAMS.en.md)
- 運用手順: [docs/OPERATIONS.md](./docs/OPERATIONS.md)
- 運用手順（英語）: [docs/OPERATIONS.en.md](./docs/OPERATIONS.en.md)
- 軽量 Docker ガイド: [docs/DOCKER.md](./docs/DOCKER.md)
- Minimal Docker guide (English): [docs/DOCKER.en.md](./docs/DOCKER.en.md)
- ハイブリッド構成の詳細記事: [docs/HYBRID_ARCHITECTURE_JA.md](./docs/HYBRID_ARCHITECTURE_JA.md)
- Hybrid architecture guide (English): [docs/HYBRID_ARCHITECTURE.en.md](./docs/HYBRID_ARCHITECTURE.en.md)
- 設計判断と根拠: [docs/engineering-decisions.md](./docs/engineering-decisions.md)
- 設計判断と根拠（英語）: [docs/engineering-decisions.en.md](./docs/engineering-decisions.en.md)
- 実装判断の意図（なぜこの選択をしたか）: [docs/IMPLEMENTATION_RATIONALE_JA.md](./docs/IMPLEMENTATION_RATIONALE_JA.md)
- Implementation rationale (English): [docs/IMPLEMENTATION_RATIONALE.en.md](./docs/IMPLEMENTATION_RATIONALE.en.md)
- 英語版 README: [README.md](./README.md)
- 言語ポリシー: 運用/Docker/設計ドキュメントは英語版（`*.en.md`）と日本語版（`*.md`）を併記しています。この README では日本語版を優先しつつ英語版も併記しています。
- `structured_logging=true` で JSON 構造化ログを有効化できます。

## セキュリティ

次のローカルファイルはコミットしないでください。

- `credentials.json`
- `token.json`
- `config.json`
- `state.json`
- `events.jsonl`
- `events.jsonl.checkpoint.index.json`
- `runs.jsonl`
- `runs.jsonl.summary.index.json`
- `.state.json.lock`
- `.discord_dedupe_state.json`
- `.discord_dedupe_state.lock`
- `logs/`

## ライセンス

MIT License（詳細は [LICENSE](./LICENSE)）。
