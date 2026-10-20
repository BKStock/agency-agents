# 運用手順（Mac）

## 初回セットアップ

```bash
cd projects/landline-atlas
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[excel]'      # GIS 特徴量（fetch-gis）も作るなら '.[excel,gis]'
python -m unittest discover -s tests
atlas init      # ~/landline_atlas_data（変える場合は --data-dir か LANDLINE_ATLAS_DATA）
```

既存の資産は保護されています。

- `~/official_phone_data` 配下をデータ置き場にすると停止します。
- 手元の原本を `--import` で取り込むときは、ファイルを読むだけで変更しません（`raw/` に複製を保管します）。

確認したい場合は、実行前後でハッシュを比べてください。

```bash
find ~/official_phone_data -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/before.txt
atlas update --offline --import ~/official_phone_data/xxx/06.xls
find ~/official_phone_data -type f -print0 | sort -z | xargs -0 shasum -a 256 > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt && echo "unchanged"
```

## 初回の実データ確認（必須）

作成環境から総務省・Wayback に接続できなかったため、次の3点は**実ファイルで未確認**です。

1. **`config/sources.json`**：`official_pages.url` が、固定電話番号の指定状況のページを指しているか。`file_patterns.regex` がファイル名に合い、`series` を正しく取り出せるか。
   - 確認方法：`atlas update`（Wayback なし）を実行し、`run_manifest.json` の `acquire.collected` と `numbers.raw_source.series` を見る。
2. **`config/layouts.json`**：見出し語が実際の表の見出しと一致するか。
   - 一致しない場合は `Q00_unknown_layout` で停止し、`outcome: blocked_by_qa` になります。その場合は見出し語を追加してください。
   - 総務省のページには、携帯・IP電話・フリーダイヤル・IMSI などの固定電話以外の表も並んでいます。見出しが見つからず、題名（見出しより上の行）が `out_of_scope_titles` の語を含む表は、対象外として飛ばします（`Q00b_out_of_scope`、情報。`run_manifest.json` の `ingest.out_of_scope` に一覧）。原本は保管したままです。
   - 新しい種類の表が増えて Q00 で止まったときは、題名を見て固定電話以外なら `out_of_scope_titles` に語を足し、固定電話の表なら見出し語を足してください。
3. **`in_use_values`**：「使用中」を表す実際の表記。
   - 確認方法：`SELECT usage_status, COUNT(*) FROM numbers.designation_row GROUP BY 1` で表記を確かめて設定する。

確認が終わったら、`atlas update --wayback` で過去版を集めます。

## 定期更新（launchd の例：毎月1日 5:10）

`~/Library/LaunchAgents/jp.local.landline-atlas.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>jp.local.landline-atlas</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/projects/landline-atlas/.venv/bin/atlas</string><string>update</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Day</key><integer>1</integer><key>Hour</key><integer>5</integer>
    <key>Minute</key><integer>10</integer></dict>
  <key>StandardOutPath</key><string>/tmp/landline-atlas.log</string>
  <key>StandardErrorPath</key><string>/tmp/landline-atlas.err</string>
</dict></plist>
```

`launchctl load ~/Library/LaunchAgents/jp.local.landline-atlas.plist` で登録します。

毎回の結果は次の場所に残ります。

| 場所 | 内容 |
|---|---|
| `runs/<時刻>/run_manifest.json` | 各段階の件数と品質検査の結果 |
| `exports/<時刻>/` | 出力ファイル |
| `backups/<時刻>/` | バックアップと復旧試験の結果 |

## 品質検査で止まったとき

`outcome: blocked_by_qa` のときは分類・出力を行いません。`atlas qa` で詳細を見てください。

| 検査 | よくある原因 | 対処 |
|---|---|---|
| Q00 | 表の見出しが未知 | `layouts.json` に見出し語を追加し、`atlas update --offline` で再解析する |
| Q01 | 差分の未計算 | 通常は自動で解消する。続く場合は報告する |
| Q08 | 原本の破損・欠落 | `backups/` から DB を戻し、`raw/` を再取得する |
| Q09 | 同一基準日の内容違いの理由が未記録 | `atlas update` の再実行で記録される |
| Q10 | DB の破損 | 最新の `backups/<時刻>/` から戻す |
| Q13（警告） | 同じ書き換え（例：事業者名の表記変更）が一斉に起きた | `atlas changes-report` で一覧を見て、本当の変更か書き方の変更かを確認する |
| Q12 | 隣接する版の局番がほとんど重ならない（別の表同士の比較、または1つの地域が複数ファイルに分かれている） | `atlas qa` の `low_overlap_pairs` を共有して相談する |

## 国勢調査（小地域）を e-Stat から取得する

```bash
atlas fetch-census --pref 27 --probe   # 確認だけ（DB にもディスクにも書かない）
atlas fetch-census --pref 27           # 取得して町丁目マスタ・特徴量に取り込む
atlas classify --census-year 2020 --boundary-version estat-2020
```

- 取得先の URL と表 ID は `config/estat.json` にあります。2026-10-20 に大阪府（27）・岩手県（03）・長野県（20）・高知県（39）の実物で確認しました。ほかの都道府県でも、まず `--probe` の結果を確認してください。
- 列は、2行目の日本語の見出し（例：「65歳以上」「持ち家」）で探します。1つの列に決まらない項目は取り込みません。必須の表（人口総数）で決まらなければ停止します。
- 取り込むのは、町丁・字等のいちばん細かい階層の行だけです。上位の階層（大字の合計など）は二重計上を防ぐため除きます。
- 境界データからは面積（`area_km2`）だけを取り込みます。人口は統計表と照合し、合わない町丁目の数を表示します。
- 住宅の建て方は T001086（住宅の建て方別主世帯数）から取ります。戸建率・共同住宅率の分母は同じ表の主世帯数です。
- 町丁・字等の所有関係の表（T001085）には公営等の借家・給与住宅の列がありません。そのため、借家率・公営借家率を使う分類（R4・R5）は「資料不足」になります。
- 秘匿は表ごとに付きます（人口総数の表には秘匿が無い）。`--probe` の「秘匿（全ての表を合わせて判定）」で件数を確認します。表の間で秘匿の状態が食い違うときは、取り込まずに停止します。

## GIS 特徴量を取得する

```bash
atlas fetch-gis --pref 27 --probe   # 取得・属性の確認・計算まで（DB にもディスクにも書かない）
atlas fetch-gis --pref 27           # 原本を raw/gis に保管し、特徴量を取り込む
atlas fetch-gis --pref 27 --datasets did,zoning   # 一部のデータだけ（取り直しなど）
atlas classify --census-year 2020 --boundary-version estat-2020
```

- 先に `atlas fetch-census` で同じ都道府県の小地域境界を取り込んでおきます。町丁目の形はその原本（raw/census）から読みます。
- 取得先・属性・コードは `config/gis_sources.json` にあります。属性名・コード・座標系・年が書いてあるものと違えば、どのデータも取り込まずに停止します。新しい年版に替えるときは、定義書でコードを確かめてから config を直してください。
- 原本はデータセットごとに SHA-256 で保管し（`regions.raw_gis`）、出典と利用条件を `source_document` に記録します。メッシュ単位のデータは、その都道府県で使ったファイルを束ねた出典を1つ作り、特徴量はそれを指します。
- 取り込みは毎回新しい load として追記します。前の値は消えず、分類は町丁目・特徴量ごとに最新の load を使います。
- missing になるのは次の町丁目です。
  - 用途地域：その市区町村の用途地域ファイルが配布されていない（公開不可・資料の提供なし・用途地域なしを区別できない）
  - 地価：町丁目の中に地価公示の地点が無い
  - そのほか：町丁目の面積のうち値が分かっている部分が `min_coverage` 未満（水面の調査区、海岸線の描き方の違い、農業集落の区分が定義書から決まらない部分など）
- 用途地域は市区町村ごとに利用条件が違います（`Situation_of_the_data_collection_RestrictedZoneData.pdf`）。出力を外に出す前に確認してください。

## 差分の内訳を確認する

```bash
atlas changes-report            # 個別の変更と一括変更の件数、一括変更の上位、比べなかった列
```

- **一括変更**：1つの組で同じ「旧の値 → 新の値」が `bulk_min_rows`（既定 30 行、`config/layouts.json`）以上ある変更です。事業者名の表記変更や区画コードの付け替えなど、個々の局番の変化ではないことが多いため、個別の変更とは分けて数えます（元の行は残ります）。
- **比べなかった列**：片方の年の表に無い列（全行空欄）は比較しません。「空欄 → 値」を変更と数えないためです。
- **事業者名の変化**は次のように分けて数えます。「別の事業者へ」だけが本当の移転の候補です。

  | 分類 | 意味 |
  |---|---|
  | 書き方の違い | 全角半角・空白・「株式会社」の有無だけの違い（自動判定） |
  | 社名変更・略称 | 同じ会社の別名（`config/carriers.json` の `groups`） |
  | 合併等の引継ぎ | 合併などで別の会社から引き継いだ（`config/carriers.json` の `successions`） |
  | 事業者なし | 事業者が空欄になった（番号の返却など） |
  | 新たに指定 | 空欄だった事業者が入った |
  | 別の事業者へ | 上のどれにも当たらない変化 |

  `config/carriers.json` の `verified: false` の項目は作成者の知識による初期案です。内容を確認し、足りない会社名があれば追加してください。

  「別の事業者へ」に残る変化は、本当の移転か、対応表にまだ無い別名かのどちらかです。`atlas changes-report` の一覧（`carrier_transfers.csv`）を見て判断してください。
  - 同じ会社の別名なら `groups` に、合併なら `successions` に追加します。
  - 本当の移転と考えられるもの（例：ジュピターテレコム → KDDI。J:COM は別会社として存続している）は、対応表に入れずに残します。
- 差分の計算方法を改めたときは、次の `atlas update --offline` で差分だけが自動で作り直されます。原本と解析済みの版には触れません。

## 旧バージョン（2026-10-19 版）で集めたデータの引き継ぎ

旧バージョンは、系統をファイル名の数字から決めていたため、別地域の表同士を比べていました。その DB は使わず、原本だけを新しい置き場に写して解析し直します。再ダウンロードは不要です。

```bash
cd ~/agency-agents && git pull && cd projects/landline-atlas && source .venv/bin/activate
mv ~/landline_atlas_data ~/landline_atlas_data_old     # 消さずに名前を変えて残す
atlas init
atlas migrate-raw --from ~/landline_atlas_data_old     # 原本と入手経路・取得記録を写す
atlas update --offline                                 # 写した原本を解析し直す
atlas qa
```

## 復旧

1. `atlas` を止めます。
2. `~/landline_atlas_data/db/` を別名に退避します（削除はしません）。
3. `backups/<時刻>/*.sqlite3` を `db/` に複製します。
4. `atlas qa` を実行し、Q08・Q10 が合格することを確認します。
