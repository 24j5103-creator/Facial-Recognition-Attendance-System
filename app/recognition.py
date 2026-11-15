import base64
import threading
import time

from .db import ROOT


class Recognition:
    def __init__(self, database):
        self.db = database
        self.lock = threading.Lock()
        self.app = None
        self.live = None

    def load(self):
        if self.app is None:
            from insightface.app import FaceAnalysis
            from .liveness import LivenessDetector
            self.app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"], allowed_modules=["detection", "recognition"])
            self.app.prepare(ctx_id=-1, det_size=(640, 640))
            self.live = LivenessDetector(threshold=self.db.setting("liveness_threshold"), model_dir=ROOT / "models")

    def decode(self, encoded):
        import cv2
        import numpy as np
        from .jpeg import jpeg_size
        if len(encoded) > 4_000_000:
            raise ValueError("画像が大きすぎます。")
        try:
            data = base64.b64decode(encoded.split(",")[-1], validate=True)
        except Exception:
            raise ValueError("画像形式が不正です。") from None
        if len(data) > 3_000_000:
            raise ValueError("画像が大きすぎます。")
        w, h = jpeg_size(data)
        frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape[:2] != (h, w):
            raise ValueError("画像を読み込めません。")
        return data, frame

    def register(self, images):
        if set(images) != {"normal", "mask"}:
            raise ValueError("正面マスクなし・ありの2枚が必要です。")
        result = {}
        with self.lock:
            self.load()
            for kind, encoded in images.items():
                data, frame = self.decode(encoded)
                faces = self.app.get(frame)
                if len(faces) != 1:
                    raise ValueError("写真ごとに顔がちょうど1人検出できる必要があります。")
                result[kind] = (data, faces[0].normed_embedding.astype("float32").tobytes())
        return result

    def check(self, encoded):
        import numpy as np
        if not self.lock.acquire(blocking=False):
            raise ValueError("認証処理中です。少し待ってください。")
        start = time.monotonic()
        try:
            self.load()
            _, frame = self.decode(encoded)
            faces = self.app.get(frame)
            result = {"authenticated": False, "employee_id": None, "name": "Unknown", "box": None, "width": frame.shape[1], "height": frame.shape[0], "message": "顔を正面に向けてください。"}
            if not faces:
                return result
            faces.sort(key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)
            face = faces[0]
            area = lambda f: max(0, f.bbox[2] - f.bbox[0]) * max(0, f.bbox[3] - f.bbox[1])
            if len(faces) > 1 and area(faces[1]) >= area(face) * self.db.setting("face_ratio"):
                result["message"] = "1人ずつカメラに近づいてください。"
                return result
            result["box"] = [int(x) for x in face.bbox]
            with self.db.connect() as db:
                photos = db.execute("SELECT p.employee_id,p.vector,e.name FROM photos p JOIN employees e ON p.employee_id=e.id WHERE e.active=1").fetchall()
            best = (-1.0, None, None)
            for row in photos:
                score = float(np.dot(np.frombuffer(row["vector"], dtype=np.float32), face.normed_embedding))
                if score > best[0]:
                    best = (score, row["employee_id"], row["name"])
            if best[0] < self.db.setting("face_threshold"):
                result["message"] = "登録された本人を確認できません。店舗責任者へ報告してください。"
                return result
            live, score = self.live.check(frame, face.bbox)
            if not live:
                result["message"] = "ライブネスを確認できません。撮影し直してください。"
                return result
            result.update(authenticated=True, employee_id=best[1], name=best[2], message="本人認証OK", live_score=score, inference_ms=round((time.monotonic() - start) * 1000))
            return result
        finally:
            self.lock.release()
