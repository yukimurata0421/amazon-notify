# 軽量 Docker ガイド

この Docker イメージは、意図的に最小スコープに限定しています。

## 位置づけ
- 本番主系は Linux 単一ホスト + systemd-first 運用です。
- Docker は、クイックスタートとローカル再現の補助導線です。
- つまり「Docker 対応」はするが、「Docker 前提アーキテクチャ」にはしません。

## 入れるもの
- Python ランタイム
- `amazon-notify` 本体
- `pyproject.toml` の通常依存（必須 pip 依存）
- 次の CLI 実行経路:
  - `amazon-notify --help`
  - `amazon-notify --validate-config`
  - `amazon-notify --once --dry-run`

## 入れないもの
- `systemd` 運用
- hybrid HA 構成
- watchdog/fallback オーケストレーション
- 永続ボリューム設計の本格化
- 本番監視や restart policy の作り込み
- 複数コンテナ構成
- Discord/Gmail の本番向け secret 管理設計

## ビルド
```bash
docker build -t amazon-notify:slim .
```

## 試すコマンド
### 1) help
```bash
docker run --rm amazon-notify:slim --help
```

### 2) validate-config
`amazon-notify` は `config.json` のあるディレクトリ基準で runtime パスを解決します。

```bash
docker run --rm \
  -v "$(pwd):/work" \
  amazon-notify:slim \
  --config /work/config.json \
  --validate-config
```

### 3) one-shot dry-run
```bash
docker run --rm \
  -v "$(pwd):/work" \
  amazon-notify:slim \
  --config /work/config.json \
  --once --dry-run
```

## host 側の運用責務
- `config.json` / `credentials.json` / `token.json` の配置と管理
- ログと runtime artifact の保持・掃除
- 本番運用（`systemd`、watchdog、監視、restart 戦略）は host 側で設計・実施
