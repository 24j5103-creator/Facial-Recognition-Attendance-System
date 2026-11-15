# 推論モデルの配置

公開用パッケージには学習済みモデルを同梱しません。

## ライブネス（MiniFASNet）

[配布元の固定コミット](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing/tree/b6d5f04ad78778917853b25c778acef6d5626d15/resources/anti_spoof_models)から次の2ファイルを入手し、この `models/` フォルダに配置してください。ヘルパーコードとApache-2.0ライセンスは同梱しています。

| ファイル | 元ZIP内のモデルのSHA-256 |
|---|---|
| `2.7_80x80_MiniFASNetV2.pth` | `a5eb02e1843f19b5386b953cc4c9f011c3f985d0ee2bb9819eea9a142099bec0` |
| `4_0_0_80x80_MiniFASNetV1SE.pth` | `84ee1d37d96894d5e82de5a57df044ef80a58be2b218b5ed7cdfd875ec2f5990` |

このリポジトリのルートでダウンロードする場合：

```powershell
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/minivision-ai/Silent-Face-Anti-Spoofing/b6d5f04ad78778917853b25c778acef6d5626d15/resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.pth" -OutFile ".\models\2.7_80x80_MiniFASNetV2.pth"
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/minivision-ai/Silent-Face-Anti-Spoofing/b6d5f04ad78778917853b25c778acef6d5626d15/resources/anti_spoof_models/4_0_0_80x80_MiniFASNetV1SE.pth" -OutFile ".\models\4_0_0_80x80_MiniFASNetV1SE.pth"
Get-FileHash .\models\*.pth -Algorithm SHA256
```

## 顔検出・顔特徴抽出（InsightFace buffalo_l）

通常は初回の `manage.py check-models` でInsightFaceがユーザーの `.insightface/models` 以下へ取得します。ネットワーク接続と利用条件の確認が必要です。

InsightFaceのライブラリコードのMITライセンスと、配布学習済みモデルの利用条件は異なります。配布モデルは非商用研究用途向けと案内されています。店舗での商用運用を行う場合は、その用途で利用可能なモデル・契約を確認してください。

[InsightFace公式のライセンス説明](https://github.com/deepinsight/insightface/tree/master/python-package#license)
