[![CI](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/yukimurata0421/amazon-notify/main/.github/badges/coverage.json)](https://github.com/yukimurata0421/amazon-notify/blob/main/.github/badges/coverage.json)
[![Lint](https://img.shields.io/badge/lint-ruff-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)
[![Types](https://img.shields.io/badge/types-mypy-brightgreen?style=flat-square)](https://github.com/yukimurata0421/amazon-notify/actions/workflows/ci.yml)

# Amazon Notify (日本語)

Amazon.co.jp の配送関連メールを Gmail API で検出し、Discord Webhook に通知する自己ホスト向けツールです。
通知速度より運用上の一貫性と復旧容易性を優先しています。
設計上の最優先は checkpoint/frontier の整合性維持です。

補足: `main` ブランチは最新 GitHub Release より先行している場合があります。

運用モードは 2 つあります。
- 単純な定期ポーリング
- Gmail Watch + Pub/Sub StreamingPull による準リアルタイム運用

English README: [README.md](./README.md)

## できること

- Ordered Frontier（oldest-first、途中失敗時はそこで停止）
- `events.jsonl` を checkpoint 正本とし、`state.json` は互換スナップショット、`runs.jsonl` は監査ログとして運用
- Gmail/Discord の一時障害に対するリトライと復旧通知
- 一時障害アラートの境界制御（継続時間しきい値 + クールダウン）
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
```

1. `config.json` の `discord_webhook_url` を設定
2. `credentials.json` を `config.json` と同じ場所に配置
3. 初回認証と疎通確認

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

- 運用手順: [docs/OPERATIONS.md](./docs/OPERATIONS.md)
- ハイブリッド構成の詳細記事: [docs/HYBRID_ARCHITECTURE_JA.md](./docs/HYBRID_ARCHITECTURE_JA.md)
- 設計判断と根拠: [docs/engineering-decisions.md](./docs/engineering-decisions.md)
- 英語版 README: [README.md](./README.md)
- 言語ポリシー: 現在の運用手順とハイブリッド設計記事は日本語です。
- `structured_logging=true` で JSON 構造化ログを有効化できます。

## セキュリティ

次のローカルファイルはコミットしないでください。

- `credentials.json`
- `token.json`
- `config.json`
- `state.json`
- `events.jsonl`
- `runs.jsonl`
- `logs/`

## ライセンス

MIT License（詳細は [LICENSE](./LICENSE)）。
