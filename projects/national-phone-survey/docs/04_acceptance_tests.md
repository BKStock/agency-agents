# 受入テストと完成判定基準

## 1. 受入14項目の対応表

「合成データ」欄は、本リポジトリの `python3 -m unittest discover -s tests` の結果です（2026-10-19、Python 3.13、全50件合格。GIS 5件は geopandas / rasterio を入れた環境で合格、入れていない環境では skip）。

「実データ」欄は、Mac 上で行う検証です。**すべて未実施**です。

| # | 受入項目 | 合成データのテスト | 合成 | 実データでの検証手順 | 実データ |
|---|---|---|---|---|---|
| 1 | R1〜R6 を個別に判定 | `test_engine.EngineTest.test_r_classes_individually` | 合格 | 大阪府で R1〜R6 のそれぞれに該当する町丁目を、各 20 件以上目視確認する（地図・統計表で） | 未実施 |
| 2 | L1〜L6 を個別に判定 | `test_l_classes_individually` | 合格 | 農村・漁村・山間・地方都市・別荘地を含む比較用の3県（例：岩手・長野・高知）で、各分類 20 件以上を確認する | 未実施 |
| 3 | L1 と L6 などの複数該当を保持 | `test_l1_and_l6_overlap`、`test_tie_leaves_primary_on_hold`、`test_r_and_l_axes_independent` | 合格 | 実データで `overlap_flag=1` の町丁目を抽出し、内訳を確認する | 未実施 |
| 4 | R6 を検索から除外しても内部データを保持 | `test_query_export.test_osaka_r6_hidden_but_kept` | 合格 | 大阪府で `--mode residential` の有無による件数差が、R6 の件数と一致すること | 未実施 |
| 5 | GIS 未取得地域を確定分類しない | `test_gis_missing_is_not_classified`、`test_missing_is_not_zero`、`test_gis.test_outside_coverage_is_missing_not_zero` | 合格 | GIS を一部の県だけ取り込んだ状態で分類し、未取込の県に L1〜L3 の該当が 0 件であること | 未実施 |
| 6 | 都市部・農村・漁村・山間部でそれぞれ実データ検証 | （合成データでは代替不可） | — | 都市・農村・漁村・山間の各類型から、各 50 町丁目を層化抽出して正解ラベルを付け、分類別の適合率・再現率を出す | 未実施 |
| 7 | R3/R4 の分譲・賃貸に必要な追加証拠を検証 | `test_r3_needs_tenure_by_structure_evidence`、`test_rules_normalize.test_evidence_requirement_cannot_be_removed`、`test_marginal_census_tables_cannot_confirm_r3` | 合格 | PLATEAU のある都市で、R3/R4 候補の建物用途の内訳を確認する | 未実施 |
| 8 | L5 を一般世帯数だけで確定しない | `test_l5_not_confirmed_from_household_stats` | 合格 | 別荘地のある自治体の資料と照合する | 未実施 |
| 9 | 秘匿・合算地域を二重計上しない | `test_secrecy_and_merged_areas`、`test_export_reconciles_with_master`（合算元は該当数 0） | 合格 | e-Stat の `HTKSYORI` の凡例を確認したうえで、合算元・合算先の件数をダッシュボードで照合する | 未実施 |
| 10 | 全国の分類別件数を原データから再現 | `test_export_reconciles_with_master`（`ReconciliationError` で不一致を停止） | 合格 | 全国で分類 → 出力 → `reconciliation` の件数を、`regional_classification` への直接 SQL 集計と照合する | 未実施 |
| 11 | 番号指定データだけで住宅・法人属性を断定しない | `test_phone_link_is_read_only_and_independent`（番号を取り込んでも分類入力のハッシュが不変。区画レベルの関連は「未確認」と注記。観測なしは `no_observation`） | 合格 | 実 DB で同じことを確認する | 未実施 |
| 12 | ルールを更新しても過去結果を再現 | `test_rule_update_keeps_past_results_reproducible`、`test_results_are_append_only` | 合格 | 実データで run を2回（ルール版を変えて）実行し、`reproduce --run-id <旧>` が `reproduced: true` になること | 未実施 |
| 13 | 既存 SQLite・XLS・CSV を完全に保持 | `test_audit.test_backup_restore_and_manifest`、`test_audit_detects_planted_issues_without_modifying_db` | 合格 | `backup_and_verify.py` の `ok: true` と、前回マニフェストとの差分 0 | 未実施 |
| 14 | 関連性モデルを独立したテストデータで評価 | `test_evaluate.*`（グループ分割で漏れがないこと、都道府県ホールドアウト、正解なしなら評価不能） | 合格 | 正解ラベル 600 町丁目以上（分類あたり 50 以上）で B0/B1/B2 を比較する | 未実施 |

## 2. モデル評価の基準（§13）

### 比較するベースライン

| 記号 | 内容 |
|---|---|
| B0 | 町丁目の人口・住宅・地域統計 |
| B1 | B0 ＋ 番号区画・GIS |
| B2 | B1 ＋ 公開施設で観測した市内局番 |

### 評価の設定

- **正解**：`gold_label` に記録したラベル。判定者と根拠は必須です。判定者間で食い違う町丁目は除外し、件数を報告します。
- **分割**：既定は市区町村単位のグループ分割（k=5）です。補助として、都道府県ホールドアウトも行います。同じ町丁目・施設が学習と評価の両方に入らないことを、コード内の assert で保証しています。
- **指標**：
  - Top1 / Top3 正解率
  - macro-F1
  - 分類別の適合率・再現率
  - 混同行列
- **改善の判定**：B2 − B1 の macro-F1 差について、グループ単位ブートストラップの 95% 区間の下限が 0 を超える場合だけ、「局番情報で改善」とします。それ以外は「有意差なし」として扱い、改善とは呼びません。
- **確率の校正**：参照実装のモデル（最近傍重心）は確率を出しません。そのため、校正指標（Brier・ECE）は確率モデル（多項ロジスティック回帰など）を導入する P3 で追加します。

## 3. 完成判定基準（Definition of Done）

### P0 完了

- 監査 A01〜A10 の結果欄が埋まっている。
- 報告値（23 / 2,676 / 3,004）と再計算値の差がすべて説明されている。
- バックアップの復旧試験が合格している。
- 定義台帳 v0.1 が利用者にレビューされている。

### P1 完了

- 全国 47 都道府県の 2020 年小地域について、`town_master` と B0 の特徴量が取り込まれている。
- 欠損・秘匿の件数が都道府県別に出力されている。
- 一次判定の run がある（確定 0、候補・保留・資料不足の件数）。

### P2 完了

- L1〜L6 の GIS 特徴量が、取得できた範囲で取り込まれている。
- 取得できなかった範囲が `missing` として可視化されている。
- 閾値が承認され（rule_version 1.0）、受入 1〜10、12、13 が実データで合格している。

### P3 完了

- 公開施設の観測が統合されている。
- 受入 11、14 が実データで合格している。
- B0/B1/B2 の比較結果が報告されている（改善の有無を含む）。
- 調査優先順位リストが出力されている。

### 最終完成

P0〜P3 がすべて完了し、次の2点を出力・画面で確認できること。

- 「いつ・どの番号区画で・どの局番が指定され・どの町丁目（12分類）との関連が、どの根拠レベルで観測されているか」を、根拠付きで示せる。
- 未取得・分類不能・未確認の関連が、推定値で埋められずに可視化されている。
