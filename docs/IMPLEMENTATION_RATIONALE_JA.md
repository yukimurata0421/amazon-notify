# 実装判断の意図（設計観点）

English version: [IMPLEMENTATION_RATIONALE.en.md](./IMPLEMENTATION_RATIONALE.en.md)

このドキュメントは、`amazon-notify` の直近改善で「何を、なぜ採用したか」を明文化したものです。  
設計の正しさだけでなく、運用時の認知負荷・復旧性・保守性を重視した判断を記録します。
構造全体の設計判断は `docs/engineering-decisions.md` を参照してください。

対象:
- `main` ブランチの実装（v0.4.0 系の改善を含む）
- 単一ホスト運用（systemd + ローカルファイル）前提

## 1. 判断の基準

設計基準として、以下の順で優先度を置きました。

1. **整合性契約を壊さない**
   - ordered frontier（途中失敗で停止）
   - checkpoint は通知成功後のみ進める
2. **自己修復を先に、再起動は最後**
   - アプリ内リトライ/再接続で回復できるものは process 内で直す
   - systemd 再起動は最終フォールバックに置く
3. **アラートは継続障害中心に出す**
   - 一時障害の瞬断は通知ノイズ化させない
   - 継続障害だけ通知する
4. **運用をコード化する**
   - 手順書だけでなく install スクリプト/CLI/検証に落とす
5. **将来拡張より現在運用の確実性**
   - 過剰設計（分散化、DB 化、自前 MQ）を避ける

## 2. 採用した主な判断

### 2.0 今回の更新（概要）
- Discord dedupe state の path 解決を runtime 注入へ統一（`--config` 基準で一貫）。
- Gmail 実装を責務単位で分割（`gmail_auth.py` / `gmail_transient_state.py`）。
- README/運用文書で runtime artifacts の役割を分類して明示。
- StreamingPull 実装に、history 集約・duplicate skip・heartbeat atomic write の意図コメントを追加。
- Polling catch-up をページング走査 + checkpoint-not-found fail-safe に強化。
- `transient_alert_min_duration_seconds` の負値は warning + `0` clamp で継続。
- Discord dedupe state の異常 entry（不正な inflight など）を明示的に除外する方向へ調整。
- Gmail source の loop lambda をデフォルト引数バインドに変更し、将来の実装変更に対する安全性を上げる。
- CI 権限を最小権限化（既定 `contents: read`、coverage badge 更新 job のみ `contents: write`）。

## 2.1 ハイブリッド構成（Pub/Sub メイン + Polling サブ）

### 採用
- メイン系: StreamingPull 常駐（低遅延）
- サブ系: timer polling（フェールオーバー）
- watchdog 判定: `systemd active` + heartbeat freshness

### 意図
- リアルタイム性と取りこぼし回収を両立するため。
- 「main が沈黙しているのにプロセスは生きている」サイレント障害を検知するため。

### 境界
- Pub/Sub は **trigger 経路**
- 真の回収源は Gmail inbox state + checkpoint

## 2.2 systemd は最終手段

### 採用
- StreamingPull 側で再接続バックオフ、連続失敗しきい値を実装
- process 内自己修復を優先し、systemd restart 依存を下げる

### 意図
- restart storm を抑え、障害時の挙動を「再起動任せ」から「制御可能な回復」に寄せるため。

### 失敗時フォールバック
- それでも回復できない場合のみ systemd が再起動/OnFailure 通知を担う。

## 2.3 アラート境界の厳格化（alert fatigue 対策）

### 採用
- `transient_alert_min_duration_seconds`
- `transient_alert_cooldown_seconds`
- transient では即時連投せず、継続時のみ通知
- 通知していない transient には recovery 通知を出さない（silent clear）

### 意図
- 一時的な API/ネットワーク瞬断で通知が過剰にならないようにするため。
- 対応優先度が高い障害（認証切れ、持続障害）を区別しやすくするため。

## 2.4 Checkpoint の正本順序を契約どおりに統一

### 採用
- `events.jsonl` 先書き（正本）
- `state.json` はベストエフォートスナップショット

### 意図
- 「正本が未更新で派生だけ進む」矛盾状態を防ぐため。
- source-of-truth を実装上も一貫させるため。

## 2.5 JSONL durability の強化

### 採用
- append 時 `flush + fsync`
- `state.json` atomic write (`tempfile + os.replace`)
- JSONL 中間破損は fail-fast、末尾1行のみ救済

### 意図
- クラッシュや電源断後に「壊れたまま進む」より「止めて発見」を優先するため。
- frontier 一貫性を維持するため。

## 2.6 Runtime パス依存の段階的解消

### 採用
- `RuntimePaths` 注入経路を Gmail client に拡張
- global 可変依存を段階的に縮小

### 意図
- テスト容易性と将来の構成変更耐性を上げるため。

## 2.7 品質改善

### 採用
- `amazon_pattern` の事前 compile で型と責務を統一
- Gmail API build 時 `cache_discovery=False` を明示
- `structured_logging=true` で JSON ログを任意有効化
- `deployment/systemd/install-systemd.sh` による導入自動化

### 意図
- warning ノイズと手動ミスを減らし、再現性を上げるため。

## 3. 採用しなかったもの（理由付き）

## 3.1 SQL/SQLite への移行

### 現時点では不採用
- このプロジェクトでは **非採用**。

### 理由
- 単一ホスト・単一 frontier の現在要件では JSONL で契約を満たせるため。
- DB 導入は schema/migration/運用責務を追加し、現スコープでは費用対効果が低いため。

### 将来の再検討条件
- multi-instance 同時実行を正式サポートする場合
- 高頻度書き込みで JSONL I/O が律速になる場合
- 複雑クエリ/集計要件が運用上必須になった場合

## 3.2 汎用 DLQ / 永続再送キュー

### 現時点では不採用（段階見送り）
- まず ordered frontier 契約と alert 境界の安定化を優先。

### 理由
- キュー導入は再送契約・重複契約・観測契約の再設計を伴い変更半径が大きい。
- 現状は「checkpoint を進めない再試行」で実害を抑えられるため。

## 4. エラーハンドリング境界（実装上の線引き）

- **Transient（自己修復可能）**
  - process 内リトライ/バックオフ
  - しきい値未満は通知しない
  - しきい値超過で通知
- **Persistent（継続障害）**
  - 通知対象
  - fallback（polling）で回収を継続
- **Fatal/Auth（人手介入）**
  - 即通知 + `--reauth` 導線
  - checkpoint 前進停止
- **Process dead / silent stall**
  - watchdog 判定でサブ系実行
  - 最終的に systemd 回復に委譲

## 5. いまの結論

このリポジトリでは、「最新技術を全部入れる」よりも、次を重視しました。

- 整合性契約を守る
- 通知ノイズを増やさない
- 回復可能な障害は自動回復する
- 手順をコード化して再現性を上げる

これが、現状の運用要件に対する調整方針です。  
将来要件が変わった場合は、`docs/engineering-decisions.md` の非採用項目を再評価して段階的に移行します。
