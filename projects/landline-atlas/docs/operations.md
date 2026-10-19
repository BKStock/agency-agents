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
atlas update --offline --import ~/official_phone_data/xxx/06.xls:06
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

## 復旧

1. `atlas` を止めます。
2. `~/landline_atlas_data/db/` を別名に退避します（削除はしません）。
3. `backups/<時刻>/*.sqlite3` を `db/` に複製します。
4. `atlas qa` を実行し、Q08・Q10 が合格することを確認します。
