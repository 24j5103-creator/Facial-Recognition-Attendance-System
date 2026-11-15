# 第三者コード・モデル

## 同梱するコード

- `models/MiniFASNet.py` と `models/generate_patches.py`：minivision-ai / Silent-Face-Anti-Spoofing由来。著作者表記と `models/LICENSE`（Apache-2.0）を保持しています。
- `models/SOURCE.txt` に元リポジトリと参照コミットを記載しています。
- これら第三者コードは元ZIPから保持し、今回の公開準備ではコード本体を変更していません。

## 別途インストール・取得するもの

- InsightFace / ONNX Runtime / PyTorch / OpenCV / NumPy / FastAPI等はrequirementsから導入し、ライブラリ本体を再配布しません。
- InsightFaceのコードと配布モデルは利用条件が異なります。配布モデルは非商用研究用途向けです。[公式説明](https://github.com/deepinsight/insightface/tree/master/python-package#license)
- MiniFASNetの学習済み重みは同梱しません。取得元・配置方法・照合用ハッシュは `models/README.md` に記載しています。

## このプロジェクト独自部分

この公開準備では独自コードに新しいライセンスを指定していません。独自コードの公開ライセンスは作者が選択してください。第三者コードのライセンスをプロジェクト全体のライセンスとして扱わないでください。
