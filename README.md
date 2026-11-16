# 顔認証勤怠管理システム

ブラウザで撮影・操作し、Pythonサーバーで本人認証、ライブネス、勤怠、申請、集計を処理するデモです。店舗・部署・エリアを管理でき、個人の所属と管理権限を分離しています。

## 起動

```powershell
cd 'C:\Projects\Facial Recognition Attendance System'
.\.venv\Scripts\python.exe manage.py serve
```

- 店舗端末： http://127.0.0.1:8765/store
- 本社部署端末： http://127.0.0.1:8765/office
- 本社管理者： http://127.0.0.1:8765/admin
- 停止：実行したターミナルで Ctrl+C

別端末・iPhoneからカメラを使う場合は、サーバーへ到達できるHTTPSのURLを使用してください。SSHのポート転送だけでiPhone用HTTPSが用意されるわけではありません。

## 作成できるデモデータ

個人初期パスワードは全員 `123456`。権限保持者は最初のログイン時に、6文字以上の別の値に変更します。一般従業員は個人で管理画面へログインせず、端末の顔認証で打刻します。

|従業員ID|氏名（すべて架空）|ロール|
|---|---|---|
|000010001|本社 管理|admin|
|000010002|人事 担当|hq|
|000010003|部署 責任者|department_manager（人事部）|
|000010004|部署 一般|一般従業員|
|001010001|店舗 責任者|store_manager（南大沢店）|
|001010002|店舗 一般|一般従業員|
|000020001|エリア 責任者|area_manager（多摩エリア）|
|000020002|兼任 担当|department_manager（総務部）＋store_manager（八王子店）|
|001020001|八王子 一般|一般従業員|
|001030001|立川 一般|一般従業員|

公開用ファイルにはDBや端末パスワードを同梱していません。`init --demo` 実行時に、店舗・本社部署の共通パスワードをそれぞれ設定してください。ソースの `example-store-pass`／`example-office-pass` はテスト専用の例です。顔写真・顔特徴量は未登録です。勤務データは昨日と直前3か月の架空記録で、直前3か月を確定済みにします。

店舗／部署端末で所属を選び、表示された名称を再入力してログインします。複数部署は同じ有効パスワードの場合だけ一緒に選べます。責任者は端末の「管理者」から、自分の従業員IDでログインします。hq/adminは `/admin` からログインできます。

## 新しい空環境での設定

Python 3.12／3.13を想定しています。仮想環境を他のPCからコピーせず、そのPCで作成してください。冒頭の `C:\Projects\...` は配置先の例です。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-recognition.txt
.\.venv\Scripts\python.exe -m pip install 'torch>=2.6' --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe manage.py init --demo
.\.venv\Scripts\python.exe manage.py check-models
.\.venv\Scripts\python.exe manage.py serve
```

`init --demo` は空のDB専用です。店舗・部署の共通パスワードをそれぞれ入力します。`init`（--demoなし）では最初の本社と店舗、初期adminを作成します。新規環境では初期設定を実行してください。既存環境を更新する場合、現用の `data`・仮想環境・接続設定をこの公開用ファイルで置き換えないでください。旧バージョンのDBの自動移行は行いません。

## 操作

- 顔写真：正面マスクなし・ありの2枚。対象者氏名／従業員番号／所属と写真の確認画面を通します。写真更新は別のhq/adminが承認します。
- 打刻：画面のボタン、Space（出退勤）、Enter（休憩開始・終了）。クリック時にも新しいフレームで本人確認します。認証結果は受信から5秒、認証開始間隔は約4秒です。
- 従業員一覧：氏名・番号で検索し、詳細から情報編集・写真更新・所属異動を行えます。
- 役割：従業員IDへ複数のロールと範囲を付与します。新規付与・再付与は123456へ戻し初回変更必須。範囲だけの変更では既存パスワードを維持します。
- 有給：責任者から申請、本社は直接確定。店舗一般は確定済み直前3か月平均（上限8時間）、部署所属・権限保持者は半休4時間／全休8時間です。
- 月次集計：実働、有給、過去月調整を分離します。給与金額・有給残日数の計算は行いません。
- 操作履歴：hq/adminが日時・操作者・対象従業員・所属・月・操作から検索し、CSV出力できます。
- パスワード：端末共通／個別の変更は、別のhq/admin承認後に反映します。
- バックアップ：日次CSV30日、月次SQLite、指定月の任意上書き、adminによる復元に対応します。

## サーバーでの復旧操作

サーバー停止中に実行します。

```powershell
.\.venv\Scripts\python.exe manage.py reset-password --username 000010001
.\.venv\Scripts\python.exe manage.py set-role --username 000010002 --role admin
.\.venv\Scripts\python.exe manage.py set-role --username 000010002 --role admin --revoke
```

権限変更前に対象者・変更前後を表示し、従業員番号の再入力を要求します。操作ログへ記録しセッションを無効化します。最後の有効なadminは解除できません。manager系には `--scopes 3,4` のように内部所属ID（area_managerならエリアID）を指定します。UIで確認できる5桁の所属コードとは別です。

## 構成・確認

`app/` がサーバー、`static/` がブラウザUI、`models/` が既存ライブネスモデル、`data/attendance.db` が現用DBです。認証処理は既存のCPU版を維持しています。GPU導入はこの更新の対象外です。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

テストは一時DBと模擬認証器を使用します。実際の顔認証精度、写真・画面への耐性、iPhone実機のカメラと通信は別途確認が必要です。詳しくは `docs/検証結果.md` を参照してください。

日本の祝日は [JPHoliday](https://github.com/Lalcs/jpholiday) を使用しています。将来の祝日変更時はライブラリ更新と検証が必要です。第三者コードは `THIRD_PARTY_NOTICES.md` と `models/LICENSE`、`models/SOURCE.txt` を参照してください。
