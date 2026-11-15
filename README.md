# Face Attendance Web

新規公開版 v1.0.0。店舗端末と本社管理を分けた顔認証勤怠管理システムです。

Pythonで作成した、学校課題・動作検証向けのWeb勤怠管理システムです。端末のブラウザでカメラ撮影と打刻操作を行い、サーバーのCPUで顔認証・ライブネス判定と勤怠管理を処理します。

## 主な機能

- 顔認証とライブネス判定による出勤・退勤・休憩の記録
- 同日複数勤務、従業員登録・検索・情報修正、店舗異動・応援勤務
- 従業員IDとmanager／hq／adminロールの統合、担当店舗による権限制御
- 修正申請・承認、本社による直接確定、業務連絡と確認状況管理
- 月次集計、過去月調整・繰越、CSV出力、SQLiteバックアップ、操作ログ

従業員番号は登録元店舗コード5桁＋識別番号4桁です。異動後も番号は変わりません。

## 新規プロジェクトとしての導入

このフォルダにはソース・テスト・説明だけを含めています。実際のDB、従業員情報、登録写真・顔特徴、パスワードハッシュ、セッション、操作ログ、バックアップ、仮想環境、学習済みモデルは含めていません。DBは初期設定時に利用者の環境で作成されます。

## 動作環境とセットアップ

Python 3.12を基本とするWindows環境を想定しています。Python環境・モデル・カメラ精度は環境に応じた確認が必要です。

新しく展開した `face-attendance-web` フォルダで実行してください。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-recognition.txt
.\.venv\Scripts\python.exe -m pip install "torch>=2.6" --index-url https://download.pytorch.org/whl/cpu
```

プロジェクトを展開したフォルダで、上記コマンドを実行します。

続いて [モデルの取得・配置](models/README.md) を行います。モデルを配置したら：

```powershell
.\.venv\Scripts\python.exe manage.py check-models
.\.venv\Scripts\python.exe manage.py init --demo
.\.venv\Scripts\python.exe manage.py serve
```

### 架空データを作らずに開始する場合

`init --demo` の代わりに次を実行します。第一店舗名、最初の管理責任者の氏名、店舗共通パスワードを入力します。

```powershell
.\.venv\Scripts\python.exe manage.py init
```

最初の管理責任者の従業員IDは `000010001`、初期パスワードは `123456` です。初回に変更してください。実際の打刻には本人の同意のうえで顔写真を登録する必要があります。

### デモのログイン

`init --demo` は空のDBに架空の3店舗・9人を作成します。実在の顔写真・特徴データは生成しません。店舗共通パスワードは初期設定時に利用者が指定します。

| 従業員ID | ロール |
|---|---|
| `000010001` | 本社管理責任者：admin |
| `000010002` | 本社担当者：hq |
| `000010003` | 店舗責任者：manager |

新規アカウントの初期パスワードはデモ仕様の `123456` です。権限保持者は初回・リセット後に変更が必須で、変更先に `123456` は指定できません。これは保存済みの実アカウントの認証情報ではありません。

- 店舗入口：[http://127.0.0.1:8765/store](http://127.0.0.1:8765/store)
- 本社入口：[http://127.0.0.1:8765/hq](http://127.0.0.1:8765/hq)
- 停止：Ctrl+C

iPhone等の別端末からカメラを使うには、信頼できるHTTPS接続とカメラ利用許可が必要です。認証速度・ライブネス精度は実環境で確認してください。既存DBがある環境で `init --demo` を繰り返さないでください。

## テスト

テストは一時DBと模擬認証器を使用し、実際のカメラ・学習済みモデル・顔写真は不要です。Web依存をインストールすれば実行できます。

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_system tests.test_update
```

実顔認証・ライブネス精度のテストとは別です。

## 構成とドキュメント

```text
app/        サーバー・認証・勤怠・申請・集計・保守（__init__.pyを含む）
static/     HTML/CSS/JavaScript
models/     第三者のヘルパーコード、ライセンス、モデル取得手順
tests/      自動テスト（app/とは別の__init__.pyを含む）
docs/       要件定義・画面遷移・運用説明
manage.py   初期設定・起動・復旧・権限変更コマンド
```

- [運用手順](docs/運用手順.md)
- [機能仕様](docs/機能仕様.md)
- [画面遷移図（Mermaid）](docs/画面遷移図.md)
- [第三者コード・モデルの利用条件](THIRD_PARTY_NOTICES.md)

**InsightFaceの配布学習済みモデルは非商用研究用途向けです。** このコードを公開することと、実店舗で商用利用することは分けて判断してください。[公式説明](https://github.com/deepinsight/insightface/tree/master/python-package#license)

プロジェクト独自コードのライセンスは、この公開準備では新たに指定していません。
