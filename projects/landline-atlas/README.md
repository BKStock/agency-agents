# landline_atlas — 全国固定電話番号調査システム（独立版）

総務省の固定電話番号指定データの取得から 12 地域分類・検索までを、一つの新しいシステムとして実装したものです。既存の v1.2（`national_phone.py`・`index.sqlite3`）には依存しません。

扱う範囲は次のとおりです。

- 総務省の固定電話番号指定データの取得・原本保管・版の履歴
- 全国の町丁目への R1〜R6・L1〜L6 地域分類
- 公開施設の電話番号による観測
- 指定済み局番の調査優先順位
- 検索画面と Excel/CSV 出力

> **状態**
> - 最初の作成環境では、soumu.go.jp・e-stat.go.jp・web.archive.org への接続がネットワーク方針で拒否されました。総務省の原本はその後 Mac 上で取得しています。e-Stat の小地域データは `atlas fetch-census` で取得します（手順は [docs/operations.md](docs/operations.md)）。
> - テストは、合成データと模擬 HTTP 応答によるものです（実データで出た事例を合成データに写したものを含む）。
> - 2026-10-20 に、Mac 上で取得済みの総務省の原本（固定電話 9 系統の過去版を含む）に対して `atlas update --offline` が `outcome: ok` で通りました（固定電話以外の表は対象外として飛ばす。[docs/operations.md](docs/operations.md)）。同日、e-Stat から 2020 年小地域（大阪府・岩手県・長野県・高知県）を取り込み、最初の分類 run を作りました。続けて同じ4府県で `atlas fetch-gis`（DID・用途地域・土地利用・地価公示・農業集落・標高傾斜）を取り込み、2 回目の分類 run を作りました。さらに農業地域類型・漁業集落境界・三大都市圏・二次的住宅比率・経済センサスの事業所数を取り込み、3 回目の分類 run を作りました（L1 は都市的地域でない農業集落だけを数える）。都市雇用圏は利用条件が確認できず未取得なので L4 は、借家の内訳・2010年人口が無いので R4・R5・L6 は「資料不足」のままです。
> - 12分類のルールは全分類が未承認のため、**確定は出力されません**。

## 何をするか

```
総務省ページ ─┐                           ┌─ 品質検査（§10 の10項目＋α。error があれば止まる）
Wayback CDX  ─┼─ 取得（間隔・再試行・robots）─ 原本保管（SHA-256、同一内容は1つ）
手元のファイル ┘        │
                      解析（見出し語で列特定・文字列のまま・補正は記録）
                      版選択（系統×基準日。理由を記録）→ 隣接版の差分（種別・延べ／一意）
e-Stat 小地域・GIS ─ 町丁目マスタ／特徴量（値＋状態）─ 12分類（6状態・R/L軸・追記専用 run）
公開施設の観測 ────────────────────────────── 局番との関連（区画レベル／観測）
                      → 調査優先順位 → 出力（CSV/XLSX、照合付き）→ 検索画面 → バックアップ
```

## 使い方（Mac）

```bash
cd projects/landline-atlas
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[excel]'          # GIS も使うなら '.[excel,gis]'
python -m unittest discover -s tests

atlas init                                   # 既定のデータ置き場は ~/landline_atlas_data
atlas update --wayback                       # 取得 → … → 出力 → バックアップ
atlas update --offline --import ~/Downloads/06.xls      # 手元の原本を取り込む（元ファイルは変更しない）
atlas fetch-census --pref 27 --probe                    # e-Stat の国勢調査を確認だけ（DBに書かない）
atlas fetch-census --pref 27                            # 取得して町丁目マスタ・特徴量に取り込む
atlas fetch-gis --pref 27 --probe                       # 国土数値情報・農業集落境界を確認だけ（DBに書かない）
atlas fetch-gis --pref 27                               # GIS 特徴量（DID・用途地域・土地利用・地価・農業集落・標高・漁業集落・大都市圏・二次的住宅・事業所数）を取り込む
atlas import-census --mapping config/estat_mapping.example.json \
  --towns towns.csv --features pop.csv --source-id census2020 \
  --census-year 2020 --boundary-version estat-2020
atlas import-number-areas --csv number_areas.csv
atlas classify --census-year 2020 --boundary-version estat-2020
atlas search --prefecture 27 --mode residential          # 大阪府 R6 は非表示（--include-r6 で表示）
atlas code 06 6200                                       # 局番の指定履歴・変更・区画・観測
atlas priority --top 50
atlas serve                                              # http://127.0.0.1:8765/
atlas reproduce --run-id 1
```

`~/official_phone_data` 配下をデータ置き場に指定すると、エラーで停止します（既存資産の保護）。

系統（地域 01〜09）は、ファイル名ではなく表の中の市外局番から決めます。総務省のファイル名は連番 ID で、地域を表さないためです。1つのファイルに複数の地域があれば、地域ごとに分けて比較します。

## 構成

| パス | 内容 |
|---|---|
| `landline_atlas/acquire/` | HTTP 取得（間隔・再試行・robots）、総務省ページのリンク発見、Wayback CDX、原本保管 |
| `landline_atlas/ingest/` | 総務省表（XLS/XLSX/ZIP/CSV）、e-Stat 小地域、番号区画→市区町村 |
| `landline_atlas/history/` | 版選択、隣接版の差分 |
| `landline_atlas/regions/` | 特徴量（値＋状態）、GIS（任意依存） |
| `landline_atlas/classify/` | 12分類ルールの検証、判定エンジン |
| `landline_atlas/evidence/`、`link/` | 公開施設の観測、局番と町丁目の関連 |
| `landline_atlas/discover/` | 調査優先順位 |
| `landline_atlas/search/`、`web/`、`export/` | 検索、画面、出力 |
| `landline_atlas/evaluate/` | B0/B1/B2 比較 |
| `landline_atlas/qa/` | 品質検査、バックアップ |
| `landline_atlas/pipeline.py` | `atlas update` の一括処理 |
| `landline_atlas/db/*.sql` | numbers / regions / evidence / analysis の4DB |
| `config/` | 取得元、表の見出し語、12分類ルール、e-Stat の列対応の例 |
| `docs/` | 設計書（§14 の回答）、分類定義、データ源、受入試験、運用 |
