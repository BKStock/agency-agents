# 受入試験と完成判定基準

テストは 57 件で、すべて合格しています（2026-10-19、Python 3.13）。

- GIS 関連の 2 件は、geopandas・rasterio を入れた環境で合格しました。入れていない環境では skip になります。
- テストはすべて合成データと模擬 HTTP 応答によるもので、**実データでの検証は未実施**です。

## 受入14項目

| # | 項目 | 自動テスト | 合成 | 実データでの確認方法 | 実データ |
|---|---|---|---|---|---|
| 1 | R1〜R6 を個別に判定 | `test_classify.test_r_classes` | 合格 | 大阪府で各分類 20 件以上を目視確認 | 未実施 |
| 2 | L1〜L6 を個別に判定 | `test_l_classes` | 合格 | 農山漁村を含む比較3県で各分類 20 件以上 | 未実施 |
| 3 | L1 と L6 などの複数該当 | `test_l1_l6_overlap_and_axes` | 合格 | `overlap_flag=1` の内訳を確認 | 未実施 |
| 4 | R6 を非表示にしても内部データを保持 | `test_system.test_search_relations_and_priority`、`test_web_pages` | 合格 | 大阪府の住宅系検索の件数差が R6 の件数と一致 | 未実施 |
| 5 | GIS 未取得地域を確定しない | `test_missing_gis_never_classified`、`GisTest.test_overlap_states`（範囲外は missing） | 合格 | GIS を一部の県だけ入れて分類し、未投入県の L1〜L3 が 0 件 | 未実施 |
| 6 | 都市・農村・漁村・山間の実データ検証 | （合成では代替不可） | — | 類型ごとに 50 町丁目を層化抽出し、正解ラベルを付けて評価 | 未実施 |
| 7 | R3/R4 の追加証拠 | `test_r3_requires_cross_evidence`、`RulesTest.test_invariants` | 合格 | PLATEAU のある都市で建物用途を照合 | 未実施 |
| 8 | L5 を世帯数だけで確定しない | `test_l5_not_confirmed_without_evidence` | 合格 | 自治体の別荘資料と照合 | 未実施 |
| 9 | 秘匿・合算の二重計上なし | `test_secrecy`、`CensusImportTest`、`ExportTest`（合算元は該当 0） | 合格 | e-Stat の凡例を確認し、件数を公表値と照合 | 未実施 |
| 10 | 分類別件数を原データから再現 | `ExportTest.test_reconciliation_and_counts`（不一致なら停止） | 合格 | 全国の出力の照合結果を SQL 集計と比較 | 未実施 |
| 11 | 番号指定だけで住宅・法人属性を断定しない | `test_search_relations_and_priority`（区画レベルは「未確認」、観測なしは `no_observation`） | 合格 | 同じことを実データで確認 | 未実施 |
| 12 | ルール更新後も過去結果を再現 | `test_reproduce_after_data_and_rule_change` | 合格 | ルール版を変えた2回の run で `atlas reproduce` | 未実施 |
| 13 | 既存資産を保持 | `SafetyTest.test_protected_legacy_directory`、`test_backup_restore`、`PipelineTest`（手元の原本を変更しない） | 合格 | `~/official_phone_data` のハッシュを実行前後で比較（[operations.md](operations.md)） | 未実施 |
| 14 | 関連性モデルを独立データで評価 | `EvaluateTest`（グループ分割の漏れなし、都道府県ホールドアウト、正解なしなら評価不能） | 合格 | 正解 600 町丁目以上で B0/B1/B2 を比較 | 未実施 |

## 番号側の受入（指示書 §10 を新システムの品質検査として実装）

| 検査 | 内容 | テスト |
|---|---|---|
| Q01 | 比較組が同一系統・隣接基準日だけ | `test_versions_and_diffs_separate_gross_and_distinct` |
| Q02/Q03 | 変更種別の内訳。延べと一意を分けて記録 | 同上（延べ 5 行、新たに使用中になった局番 2 種類） |
| Q04 | 版内の重複キー、複数の組での変更 | 同上、`test_duplicate_keys_and_repeated_content_are_flagged` |
| Q05 | 表記ゆれが過半を占める組（表形式変更の疑い） | 同上（全角の表記ゆれを `cosmetic_only` に分離） |
| Q06 | 同一内容が別の基準日に選ばれていないか | `test_duplicate_keys_and_repeated_content_are_flagged` |
| Q07 | 取得に一度も成功しない URL、再試行 | `FetchTest`、`CollectTest` |
| Q08 | 原本の SHA-256 の再検証 | `test_content_addressed_dedup_and_tamper_detection` |
| Q09 | 同一基準日で内容が違う版の選択理由 | `test_same_date_different_content_is_explained` |
| Q10 | 4つの DB の整合性 | パイプラインの各テスト |
| Q00 | 未知の表形式があれば分類に進まない | `test_unknown_layout_blocks_classification` |
| 差分の列 | 片方の年に無い列（全行空欄）は比較せず、`skipped_columns` に記録 | `test_empty_column_is_not_compared` |
| Q13 | 同じ「旧 → 新」が 30 行以上ある変更を「一括変更」として個別の変更と分けて数え、一覧を警告で出す | `test_bulk_rename_is_separated_from_individual_changes`、`test_changes_report` |
| 事業者名の変化 | 書き方の違い・社名変更・合併等の引継ぎ・返却・新たな指定・別の事業者への変化に分けて数える（`config/carriers.json`） | `CarrierClassificationTest`（実データの Q13 に出た変化で確認） |
| Q06（分割） | 隣り合う日付の同一内容は「変更なしの再掲」（情報）、それ以外は警告 | `test_unchanged_republication_is_info_not_warning` |
| Q12 | 隣接する版の重なり率（両方にある局番の割合）が 50% 未満なら停止（別の表同士の比較の疑い） | `test_q12_blocks_comparison_of_unrelated_tables` |
| 系統の決定 | ファイル名ではなく市外局番から地域を決める。複数地域のファイルは分割し、0120・050 などは別の系統にする | `test_numeric_id_filenames_do_not_mix_regions`、`test_one_file_with_two_regions_splits`、`test_non_geographic_numbers_get_their_own_series` |

## モデル評価の基準

- **正解**：`gold_label`（判定者・根拠付き）だけを使います。判定者間で一致しない町丁目は除外します。
- **分割**：市区町村単位のグループ分割（k=5）を基本とし、都道府県ホールドアウトを補助に使います。
- **指標**：Top1・Top3、macro-F1、分類別の適合率・再現率、混同行列。
- **改善の判定**：B2 − B1 の 95% ブートストラップ区間（グループ単位）の下限が 0 を超える場合だけ、「局番情報で改善」とします。
- **確率の校正**：参照モデル（最近傍重心）は確率を出しません。校正指標は、確率モデルを入れる段階で追加します。

## 完成判定

| 段階 | 完了条件 |
|---|---|
| P0 | 実データで `atlas update` が `outcome: ok`。`layouts.json` / `sources.json` を実ファイルで確認済み。定義台帳をレビュー済み |
| P1 | 47都道府県の 2020 年小地域マスタと B0 特徴量が入っている。秘匿・合算の件数が公表値と一致。一次判定の run がある |
| P2 | GIS 特徴量（取得できた範囲）と外部証拠が入っている。閾値を承認済み（rule 1.0）。受入 1〜10・12・13 が実データで合格 |
| P3 | 公開施設の観測が入っている。受入 11・14 が実データで合格。B0/B1/B2 の比較を報告済み。調査優先順位を出力済み |
| 最終 | 「いつ・どの番号区画で・どの局番が指定され・どの町丁目（12分類）との関連が、どの根拠レベルで観測されているか」を根拠付きで示せる。未取得・分類不能・未確認が、推定値で埋められずに可視化されている |
