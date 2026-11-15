$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { py -3 -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw '仮想環境を作成できませんでした。Pythonのインストールを確認してください。' }
}
& ./.venv/Scripts/python.exe -m pip install -r requirements-web.txt
if ($LASTEXITCODE -ne 0) { throw 'Webライブラリの導入に失敗しました。' }
Write-Output '認証ライブラリが未導入なら、READMEのCPU環境構築を行ってください。'
Write-Output '初期設定: .\.venv\Scripts\python.exe manage.py init --demo'
Write-Output '起動: .\.venv\Scripts\python.exe manage.py serve'
