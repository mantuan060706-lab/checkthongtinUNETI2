import os
import numpy as np
import json
import time
import onnxruntime as ort
from PIL import Image

IMG_HEIGHT = 60
IMG_WIDTH = 160


class OCR:
    def __init__(self, model_path=None):
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))

        def find_asset(name):
            # dò file ở: cạnh ocr.py, thư mục cha (root khi deploy Vercel), và CWD
            for d in (BASE_DIR, os.path.dirname(BASE_DIR), os.getcwd()):
                p = os.path.join(d, name)
                if os.path.exists(p):
                    return p
            return os.path.join(BASE_DIR, name)

        if model_path is None:
            model_path = find_asset("best_model.onnx")

        # 1. Bảng giải mã ký tự
        try:
            with open(find_asset("idx2char.json"), "r", encoding="utf-8") as f:
                data = json.load(f)
                self.idx2char = {int(k): v for k, v in data["idx2char"].items()}
            print("✅ Load idx2char.json thành công")
        except Exception as e:
            print(f"❌ Lỗi load idx2char.json: {e}")
            self.idx2char = {}

        # 2. Load ONNX bằng đường dẫn TUYỆT ĐỐI (quan trọng trên serverless:
        #    file .onnx.data ngoài phải được tìm thấy cạnh file model)
        try:
            self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            self.input_name = self.session.get_inputs()[0].name
            print("✅ Load ONNX thành công:", os.path.basename(model_path))
        except Exception as e:
            print(f"❌ Lỗi load ONNX: {e}")
            self.session = None

    def decode_numpy(self, outputs):
        preds = np.argmax(outputs, axis=2)
        preds = preds.T
        result = []
        for seq in preds:
            text = ''
            prev = -1
            for p in seq:
                if p != 0 and p != prev:  # CTC: bỏ blank và ký tự lặp
                    text += self.idx2char.get(p, '')
                prev = p
            result.append(text)
        return result

    def predict_pil(self, pil):
        if self.session is None:
            return "ERROR_MODEL_NOT_LOADED", 0.0
        pil = pil.convert('L')
        pil = pil.resize((IMG_WIDTH, IMG_HEIGHT))
        arr = np.array(pil, dtype=np.float32) / 255.0
        arr = (arr - 0.5) / 0.5
        blob = np.expand_dims(np.expand_dims(arr, axis=0), axis=0)
        start = time.time()
        out = self.session.run(None, {self.input_name: blob})[0]
        decoded = self.decode_numpy(out)
        text = decoded[0] if decoded else ""
        elapsed = (time.time() - start) * 1000
        return text, elapsed
