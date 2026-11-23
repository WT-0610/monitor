"""Local, low priority image tagging. No UI or network dependency."""
from __future__ import annotations

import datetime as dt
import os
import sys
import io
import sqlite3
import threading
import time
from pathlib import Path

LABELS = {"chat_screenshot", "portrait", "pet", "food", "landscape", "other"}
VERSION = "lite0-3"
MODEL_DIR = (Path(sys._MEIPASS) / "models" if getattr(sys, "frozen", False)
             else Path(__file__).resolve().parent.parent / "models")


def looks_like_chat(image):
    """Conservative classical CV gate for message screenshots."""
    import numpy as np

    w, h = image.size
    if w < 300 or h < 400 or h / w < 1.25:
        return False
    a = np.asarray(image.resize((128, 192)).convert("RGB"), dtype=np.int16)
    neutral = (a.max(2) - a.min(2)) < 20
    light = a.mean(2) > 220
    dark = a.mean(2) < 45
    flat = neutral & (light | dark)
    if float(flat.mean()) < .53:
        return False
    gray = a.mean(2)
    horizontal = np.abs(np.diff(gray, axis=0)).mean(1)
    lines = int((horizontal > 16).sum())
    # Chat UI tends to alternate text rows and blank space across the height.
    occupied = np.abs(np.diff(gray, axis=1)).mean(1)
    bands = int((occupied > 9).sum())
    return lines >= 13 and bands >= 35


class LiteTagger:
    def __init__(self):
        import onnxruntime as ort
        import numpy as np
        from PIL import Image
        self.np = np
        self.Image = Image
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.det = ort.InferenceSession(str(MODEL_DIR / "efficientdet_lite0.onnx"), opts,
                                        providers=["CPUExecutionProvider"])
        self.cls = ort.InferenceSession(str(MODEL_DIR / "efficientnet_lite0.onnx"), opts,
                                        providers=["CPUExecutionProvider"])
        self.classes = (MODEL_DIR / "imagenet_classes.txt").read_text(encoding="utf-8").splitlines()

    def classify(self, path):
        from PIL import ImageOps
        with self.Image.open(path) as original:
            im = ImageOps.exif_transpose(original).convert("RGB")
            if looks_like_chat(im):
                return "chat_screenshot", .92
            small = im.resize((320, 320), self.Image.Resampling.BILINEAR)
            batch = self.np.asarray(small, dtype=self.np.uint8)[None, ...]
            boxes, ids, scores, count = self.det.run(None, {self.det.get_inputs()[0].name: batch})
            found = [(int(ids[0][i]), float(scores[0][i])) for i in range(int(count[0]))
                     if float(scores[0][i]) >= .3]
            pets = [score for cls, score in found if cls in (14, 15, 16)]
            people = [score for cls, score in found if cls == 0]
            pet_score = max(pets, default=0.0)
            person_score = max(people, default=0.0)
            # Costume ears and wings often look like pets to the detector.
            if person_score >= .5 or (person_score >= .3 and person_score >= pet_score * .5):
                return "portrait", person_score
            if pet_score >= .62 and person_score < .3:
                return "pet", pet_score
            foods = [score for cls, score in found if cls in range(46, 56)]
            if foods and max(foods) >= .7 and person_score < .3:
                return "food", max(foods)

            # EfficientNet-Lite0: shorter edge 256, center crop 224, RGB [-1, 1].
            w, h = im.size
            ratio = 256 / min(w, h)
            resized = im.resize((round(w * ratio), round(h * ratio)), self.Image.Resampling.BICUBIC)
            x = (resized.width - 224) // 2
            y = (resized.height - 224) // 2
            crop = resized.crop((x, y, x + 224, y + 224))
            inp = self.np.asarray(crop, dtype=self.np.float32)[None, ...] / 127.5 - 1
            probabilities = self.cls.run(None, {self.cls.get_inputs()[0].name: inp})[0][0]
            for i in self.np.argsort(probabilities)[-5:][::-1]:
                name = self.classes[int(i)].lower()
                score = float(probabilities[i])
                if score < .35:
                    continue
                if person_score < .3 and any(word in name for word in ("pizza", "burger", "sandwich", "plate", "hotdog", "hot dog", "ice cream", "cake", "soup", "fruit", "food", "meat", "noodle", "espresso")):
                    return "food", score
                if person_score < .3 and pet_score < .3 and any(word in name for word in ("seashore", "valley", "mountain", "alp", "lakeside", "cliff", "volcano", "promontory", "coral reef", "sandbar", "forest", "waterfall")):
                    return "landscape", score
            return "other", float(probabilities.max())


class ImageTagWorker:
    def __init__(self, db_path, cfg, log):
        self.db_path = db_path
        self.cfg = cfg
        self.log = log
        self._thread = None
        self._failed = set()
        self.vault = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="image-tagger", daemon=True)
            self._thread.start()

    def _run(self):
        tagger = None
        before_id = 9223372036854775807
        while True:
            if not self.cfg.get("auto_tag_images", False):
                time.sleep(2)
                continue
            try:
                if tagger is None:
                    tagger = LiteTagger()
                with sqlite3.connect(self.db_path, timeout=15) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("""SELECT m.id, m.path, m.size, t.path AS tagged_path,
                        t.size AS tagged_size, t.mtime_ns, t.model_version, t.label AS tagged_label
                        FROM media m LEFT JOIN media_tags t ON t.media_id=m.id
                        WHERE m.kind='image' AND m.status IN ('ok','skipped') AND m.path<>''
                        AND m.id < ? ORDER BY m.id DESC LIMIT 300""", (before_id,)).fetchall()
                if not rows:
                    before_id = 9223372036854775807
                    time.sleep(12)
                    continue
                before_id = rows[-1]["id"]
                pending = False
                for row in rows:
                    if not self.cfg.get("auto_tag_images", False):
                        break
                    path = row["path"]
                    if not os.path.isfile(path):
                        continue
                    stat = os.stat(path)
                    if (row["tagged_path"] == path and row["tagged_size"] == stat.st_size
                            and row["mtime_ns"] == stat.st_mtime_ns
                            and (row["model_version"] == VERSION or
                                 (row["model_version"] in ("lite0-1", "lite0-2") and row["tagged_label"] in ("portrait", "chat_screenshot")))):
                        continue
                    if (row["id"], stat.st_mtime_ns) in self._failed:
                        continue
                    pending = True
                    try:
                        if path.endswith(".enc"):
                            if not self.vault or not self.vault.unlocked:
                                pending = False
                                continue
                            plain, _ = self.vault.read_range(path)
                            label, confidence = tagger.classify(io.BytesIO(plain))
                        else:
                            label, confidence = tagger.classify(path)
                        with sqlite3.connect(self.db_path, timeout=15) as conn:
                            conn.execute("""INSERT INTO media_tags
                                (media_id,path,size,mtime_ns,label,confidence,model_version,tagged_at)
                                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET
                                path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,
                                label=excluded.label,confidence=excluded.confidence,
                                model_version=excluded.model_version,tagged_at=excluded.tagged_at""",
                                (row["id"], path, stat.st_size, stat.st_mtime_ns, label,
                                 confidence, VERSION, dt.datetime.now().isoformat(timespec="seconds")))
                    except Exception as exc:
                        self._failed.add((row["id"], stat.st_mtime_ns))
                        self.log("WARN", f"图片标签跳过 {row['id']}: {exc}")
                    time.sleep(.15)
                time.sleep(4 if pending else 12)
            except Exception as exc:
                self.log("WARN", f"离线图片标签暂停：{exc}")
                tagger = None
                time.sleep(30)


