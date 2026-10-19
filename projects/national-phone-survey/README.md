# 全国固定電話番号調査システム：12分類エンジンと v1.2 監査ツール

全国の町丁目・大字を R1〜R6・L1〜L6 の12分類で評価し、総務省の固定電話番号指定データと根拠付きで照合するための、参照実装と設計書です。

本書は、次の2つの依頼に対応します。

- 指示書「全国固定電話番号調査システム 完成に向けた技術監査・12分類エンジン開発指示書（改訂2版）」
- v1.2 の監査

> **このディレクトリの位置づけ**
> 作成した環境からは、v1.2 本体（`~/official_phone_data/national_tool/bin/national_phone.py`）、DB、原本ファイルを参照できませんでした。
> - v1.2 の監査は「手順＋読み取り専用の検査スクリプト」として提供します。監査結果は**未実施**です。
> - 分類エンジンのテストはすべて**合成データ**によるものです。実データでの検証は、[docs/04_acceptance_tests.md](docs/04_acceptance_tests.md) の手順に沿って Mac 上で行ってください。

## 構成

| パス | 内容 |
|---|---|
| [docs/00_audit_and_design.md](docs/00_audit_and_design.md) | 指示書 §14 の必須回答 1〜12（監査・設計・ロードマップ・代替案） |
| [docs/01_class_definitions.md](docs/01_class_definitions.md) | R1〜L6 定義台帳（閾値の出所と承認状態） |
| [docs/02_data_sources.md](docs/02_data_sources.md) | 必要データ・取得先・利用条件・未確認事項 |
| [docs/03_v12_audit_procedure.md](docs/03_v12_audit_procedure.md) | v1.2 監査10項目の手順と結果記入欄 |
| [docs/04_acceptance_tests.md](docs/04_acceptance_tests.md) | 受入14項目とテストの対応・完成判定基準 |
| `regional_classifier/` | 分類エンジン（コアは標準ライブラリのみ。Python 3.13 / 3.14） |
| `rules/rules_v0.1.json` | 分類ルール v0.1（全分類が未承認のため、確定は出力されない） |
| `audit/` | v1.2 の読み取り専用監査・バックアップ・スキーマ出力 |
| `tests/` | unittest（合成フィクスチャ） |

## 安全原則

1. **v1.2 の DB は常に `file:...?mode=ro` で開きます。** 実行前後で SHA-256 を比べ、変化がないことを監査レポートに記録します。
2. **監査・バックアップの出力先は、新しいディレクトリに限ります。** 既存のディレクトリを指定すると停止します。
3. **地域分類 DB は追記専用です。** 分類結果・特徴量・外部証拠・番号指定の写しへの UPDATE / DELETE は、トリガーで拒否されます。再分類は新しい run として追記されます。
4. **大阪府の R6 除外は、検索時の表示フィルタにすぎません。** 内部データは保持されます（`--include-r6` で表示できます）。
5. **欠損を 0 で埋めません。** 値は `observed / zero / missing / suppressed / merged / undefined` の状態付きで保持します。
6. **個人宅の番号は扱いません。** 公開施設の観測は、`facility_type` で施設種別に制限しています。総当たり探索や未公開番号の生成は行いません。

## 使い方（Mac）

```bash
cd projects/national-phone-survey

# 0. テスト（GIS テストは geopandas が無ければ skip）
python3 -m unittest discover -s tests -v

# 1. v1.2 のバックアップと保全検査（v1.2 の処理を止めてから実行）
python3 audit/backup_and_verify.py \
  --db ~/official_phone_data/national_tool/data/index.sqlite3 \
  --raw-dir ~/official_phone_data \
  --out ~/official_phone_data_audit/backup_$(date +%Y%m%d)

# 2. スキーマ確認 → 監査設定を作成 → 監査
python3 audit/inspect_schema.py --db ~/official_phone_data/national_tool/data/index.sqlite3 > /tmp/v12_schema.md
cp audit/audit_config.example.json audit/audit_config.json   # SQL を実スキーマに合わせて編集
python3 audit/audit_v12.py --db ~/official_phone_data/national_tool/data/index.sqlite3 \
  --config audit/audit_config.json --out ~/official_phone_data_audit/audit_$(date +%Y%m%d)

# 3. 地域分類 DB（v1.2 とは別ファイル）
DB=~/official_phone_data_regional/regional.sqlite3
python3 -m regional_classifier init --db $DB
python3 -m regional_classifier import-towns --db $DB --csv towns.csv \
  --census-year 2020 --boundary-version estat-2020 --source-id census2020_boundary
python3 -m regional_classifier import-features --db $DB --csv features_long.csv \
  --census-year 2020 --boundary-version estat-2020 --source-id census2020_small_area \
  --source-url https://www.e-stat.go.jp/ --source-year 2020
python3 -m regional_classifier classify --db $DB --rules rules/rules_v0.1.json \
  --census-year 2020 --boundary-version estat-2020 --prefecture 27
python3 -m regional_classifier search --db $DB --prefecture 27 --mode residential   # R6 は非表示
python3 -m regional_classifier export --db $DB --out ~/official_phone_data_regional/export
python3 -m regional_classifier reproduce --db $DB --run-id 1                         # 過去 run の再現確認
```

入力 CSV の形式：

- `towns.csv`：`town_key, municipality_code, town_name, hierarchy_level, HTKSYORI, HTKSAKI`
- `features_long.csv`：`town_key, feature, value`
  - `value` は e-Stat の表記（`-`、`X`、`…`）のままで構いません。
  - 特徴量名の一覧は `regional_classifier/features.py` の `RAW_FEATURES` にあります。
- 外部証拠 CSV（`import-evidence`）：`town_key, evidence_type, supports_class, grade, source_id, source_url, value, note`

GIS 特徴量は `regional_classifier/gis.py` で計算し、`store.put_features` で保存します。この計算には、別途 `pip install geopandas rasterio` が必要です。
