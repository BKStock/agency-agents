# 運用手順（Mac）

## 初回セットアップ

```bash
cd projects/landline-atlas
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[excel]'
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
