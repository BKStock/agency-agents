# 必要データ・取得先・利用条件

> **注意**
> - 本書の URL・表番号・提供範囲・利用条件は、作成時点（2026年10月）の知識による案内です。作成環境からは各サイトに接続できず、**実際には確認していません**。
> - 取得時に公式ページで確認し、結果を `regions.source_document`（地域側）と `numbers.raw_source`（番号側）に記録してください。
> - 「要確認」と書いた項目は、存在や粒度を保証しません。

## 番号データ（自動取得：`atlas update`）

| データ | 取得先 | 粒度 | 使い方 | 注意 |
|---|---|---|---|---|
| 固定電話番号の指定状況（市外局番・市内局番・番号区画・指定事業者） | 総務省 電気通信番号関連ページ（`config/sources.json` の `official_pages`。URL とファイル名規則は要確認） | 局番×番号区画 | `numbers.version` / `designation_row` | 見出し語は `config/layouts.json`。未知の表形式は停止する（推測で読まない） |
| 同上の過去版 | Internet Archive（CDX API `https://web.archive.org/cdx/search/cdx`、再生 `.../web/<時刻>id_/<URL>`） | 版 | 時系列 | 3秒以上の間隔を空け、robots を遵守する。CDX の `collapse=digest` で内容の重複を避け、取得済みの時刻は再取得しない |
| 番号区画（MA）→市区町村 | 総務省「市外局番の一覧」等をもとに作る CSV（`atlas import-number-areas`） | 区画×市区町村 | `numbers.number_area_municipality` | 区画が市区町村の一部だけを含む場合は `coverage=part` とする |

## 地域統計（B0）

| データ | 取得先 | 粒度・年次 | 特徴量 | 注意 |
|---|---|---|---|---|
| 国勢調査 小地域集計 | e-Stat（API は appId が必要） | 町丁・字、2010/2015/2020 | 人口、65歳以上、一般世帯、住宅の建て方、所有関係 | 政府標準利用規約（第2.0版、CC BY 4.0 互換）の想定。**建て方×所有関係のクロス表は小地域では得られない想定**（要確認）なので、R3/R4 は外部証拠が必要。列名は表ごとに `config/estat_mapping.example.json` の形式で対応付ける |
| 小地域境界 | e-Stat 統計地理情報システム | 町丁・字ポリゴン（JGD2011） | 面積、`KEY_CODE`、`HTKSYORI`/`HTKSAKI`（秘匿・合算） | 秘匿処理の凡例は定義書で確認し、必要なら `normalize.SECRECY_CODES` を上書きする |
| 人口集中地区 | e-Stat 境界、国土数値情報 A16 | 2020 | `did_flag` | 面積比で判定する（閾値は要承認） |
| 経済センサス‐活動調査（町丁・大字別） | e-Stat | 2021 | `establishments` | 境界版の違いに注意 |
| 住宅・土地統計調査 | e-Stat | 2023、市区町村（町村は公表範囲が限られる。要確認） | `vacation_home_share_muni` | L5 の事前情報だけに使う |

**時系列の組替え**：2010 年・2015 年の人口は、2020 年境界に揃えてから入れます（`pop_2010` など）。手順は次のとおりです。

1. 境界の対応表を使う。
2. 対応しない地域は、500m メッシュ人口を面積按分する。
3. それでも組み替えられなければ missing とする（L6 は資料不足になる）。

## GIS（B1）

| データ | 取得先 | 特徴量 | 注意 |
|---|---|---|---|
| 農業集落境界・農業地域類型 | 農林水産省（農林業センサス） | `agri_settlement_overlap` | 利用条件を確認 |
| 漁業集落 | 農林水産省（漁業センサス） | `fishery_settlement_overlap` | **境界ポリゴンの公的提供は要確認**。代替は漁港（国土数値情報 C09）のバッファ |
| 土地利用細分メッシュ | 国土数値情報 L03-b | `farmland_ratio`、`forest_ratio` | 区分コードは年版の定義書で確認。商用利用の可否はデータセットごとに異なる |
| 用途地域 | 国土数値情報 A29 | `zoning_residential_ratio`、`zoning_commercial_ratio` | 都市計画区域外を 0 と混同しない |
| 地価公示・地価調査 | 国土数値情報 L01/L02 | `land_price_pref_pct` | 町丁目内に地点がなければ missing |
| 数値標高モデル | 国土地理院 基盤地図情報（利用者登録が必要） | `elevation_mean_m`、`slope_mean_deg` | GML を GeoTIFF に変換してから `regions/gis.dem_stats` に渡す |
| 都市雇用圏 | 東京大学 空間情報科学研究センター | `uea_member`、`metro_core_flag` | 研究用データ。利用条件を確認 |
| 過疎地域 | 総務省の公示、国土数値情報 A17 | 参考属性 | 法律上の過疎地域と L6 は別物として扱う |

## 外部証拠（確定用：`atlas import-evidence`）

| 種別 | 対象 | 入手先の例 |
|---|---|---|
| `plateau_condo_use` / `plateau_rental_use` / `plateau_villa_use` | R3 / R4 / L5 | PLATEAU 3D 都市モデル（G空間情報センター）の建物用途・階数 |
| `condo_register` | R3 | 自治体のマンション管理状況届出・分譲マンション一覧 |
| `tenure_by_structure_owner` / `_rental` | R3 / R4 | 町丁目単位の建て方×所有関係のクロス集計（入手方法は要確認） |
| `public_housing_site` / `ur_estate` / `large_estate_register` | R5 | 公営住宅一覧（オープンデータ）、UR の団地一覧（自動取得の可否は利用規約を確認） |
| `vacation_home_register` / `municipal_resort_document` | L5 | 自治体の別荘関係資料 |

## 公開施設の観測（B2：`atlas import-facilities`）

- **対象**：所在地が公的に確認できる施設に限ります。官公署、学校、医療機関、図書館、店舗・法人の公式掲載番号です。
- **保存する内容**：市外局番と市内局番までです。加入者番号は保存しません。`source_url` と `observed_on` は必須です。
- **`verified`**：住所と町丁目の照合を人が確認したときだけ 1 にします。
- **扱わないもの**：個人宅、個人名義の番号、未公開の番号。
