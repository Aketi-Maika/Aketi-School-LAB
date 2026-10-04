# MkDocs Upload Guard

指定ディレクトリを監視し、次の処理を自動実行するPython 3サービスです。

- 許可した拡張子かつUTF-8テキストであるファイルだけを保存
- バイナリ、未許可拡張子、シンボリックリンクを自動削除
- 削除・受付・エラーをログおよび任意のWebhookへ通知
- Markdownファイルのディレクトリ階層から`mkdocs.yml`の`nav`を自動生成
- Zabbix senderプロトコルで監視メトリクスを送信

外部Pythonパッケージは使用しません。

## ファイル

- `upload_guard.py`: 本体
- `config.example.json`: 設定例
- `test_upload_guard.py`: 自動テスト

## 導入

Ubuntu上で配置先を作成します。

```bash
mkdir -p "$HOME/mkdocs-upload-guard"
cp upload_guard.py config.example.json "$HOME/mkdocs-upload-guard/"
cd "$HOME/mkdocs-upload-guard"
cp config.example.json config.json
nano config.json
```

最低限、次の値を実環境に合わせます。

```json
{
  "watch_dir": "/home/aketimaika/Mkdocs/docs",
  "mkdocs_config": "/home/aketimaika/Mkdocs/mkdocs.yml"
}
```

設定を検証します。

```bash
python3 upload_guard.py --config config.json --check-config
```

既存ファイルを1回だけ検査する場合は次を実行します。

```bash
python3 upload_guard.py --config config.json --once
```

常駐監視を開始します。

```bash
python3 upload_guard.py --config config.json
```

`Ctrl+C`または`SIGTERM`で安全に停止できます。

## ファイル判定

初期設定では次の拡張子だけを許可します。

```text
.md
.markdown
.txt
```

拡張子だけでなく、ファイル全体が指定文字コードでデコードできること、およびNULバイトを含まないことも確認します。アップロード中のファイルを誤って削除しないよう、サイズと更新日時が`settle_seconds`以上変化しなかった後に検査します。

許可対象を増やす場合は`allowed_text_extensions`を編集します。Markdown以外のテキストファイルは保存されますが、MkDocsのナビゲーションには追加されません。

## `mkdocs.yml`の更新

初回更新時に、既存設定を残したまま次の管理ブロックを追加します。

```yaml
# BEGIN AUTO-GENERATED NAV
nav:
  - "公開トップ": "index.md"
  - "private":
      - "制限ページ": "private/index.md"
# END AUTO-GENERATED NAV
```

以後は、このマーカー間だけを更新します。マーカー外に既存の`nav:`がある場合は、手動設定の消失を防ぐため更新せずエラー通知します。

制限ページの本文を公開検索インデックスへ含めないため、現在の構成では`mkdocs.yml`に次の設定も残してください。

```yaml
plugins: []
```

## 通知

標準出力と`log_file`へ、次のイベントを記録します。

- `file_accepted`
- `file_deleted`
- `file_delete_failed`
- `nav_updated`
- `nav_update_failed`

`notification_webhook_url`へURLを設定すると、同じイベントをJSONのHTTP POSTとして送信します。空文字列の場合はWebhookを使用しません。

送信例は次のとおりです。

```json
{
  "event": "file_deleted",
  "message": "non-text or disallowed file deleted",
  "path": "private/example.exe",
  "reason": "extension .exe is not allowed",
  "severity": "warning",
  "timestamp": 1791158400
}
```

## Zabbix

Zabbixサーバー側に、設定したホスト名と次のZabbix trapperアイテムを作成します。

```text
mkdocs.upload.accepted_total
mkdocs.upload.deleted_total
mkdocs.upload.errors_total
mkdocs.upload.nav_updates_total
mkdocs.upload.files_current
mkdocs.upload.last_event_unixtime
mkdocs.upload.heartbeat
```

続いて`config.json`を変更します。

```json
{
  "zabbix": {
    "enabled": true,
    "server": "192.0.2.10",
    "port": 10051,
    "host": "school-lab",
    "timeout_seconds": 5,
    "key_prefix": "mkdocs.upload"
  }
}
```

`host`はZabbix上のホスト名と完全に一致させてください。通信先はZabbix serverまたはproxyのtrapperポートです。この実装は平文のsender通信を使用するため、閉域ネットワーク内に限定してください。

メトリクスは、ファイル受付・削除・ナビゲーション更新などのイベント直後と、`heartbeat_seconds`で指定した間隔の両方で送信します。

## テスト

```bash
python3 -m unittest -v test_upload_guard.py
```

テストでは、テキスト／バイナリ判定、階層からのナビゲーション生成、設定保護、バイナリ削除、およびローカルTCPサーバーを使ったZabbixパケット送受信を確認します。

## 注意事項

- 削除は即時かつ復元不能です。最初はテスト用ディレクトリで`--once`を実行してください。
- 監視ディレクトリおよび`mkdocs.yml`を定期的にバックアップしてください。
- File Browser、MkDocs、Nginxをインターネットへ直接公開しないでください。
- `mkdocs.yml`は一時ファイルへ書き込んだ後、原子的に置換します。
