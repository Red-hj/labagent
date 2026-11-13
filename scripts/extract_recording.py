"""Extract terminal text from sampled video frames with local CPU OCR."""
from pathlib import Path
import argparse
import json
import math
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from labagent.recordings import changed_lines, write_status


def create_ocr():
    from rapidocr import RapidOCR
    models = ROOT / "runtime/ocr-models"
    models.mkdir(parents=True, exist_ok=True)
    return RapidOCR(params={"Global.model_root_dir": str(models), "Global.use_cls": False,
                            "Rec.rec_batch_num": 1,
                            "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                            "EngineConfig.onnxruntime.inter_op_num_threads": 1})


def clock(seconds):
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes):02d}:{remainder:06.3f}"


def recognize(ocr, frame):
    result = ocr(frame, use_cls=False)
    if result.txts is None:
        return []
    # Reading order within a terminal follows vertical position, then column.
    rows = sorted(zip(result.boxes, result.txts, result.scores), key=lambda row: (float(row[0][:, 1].mean()), float(row[0][:, 0].min())))
    return [str(text).strip() for _, text, score in rows if score >= 0.55 and str(text).strip()]


def extract(folder):
    import cv2
    import numpy as np
    cv2.setNumThreads(1)
    status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    def update(**values):
        status.update(values)
        write_status(folder, status)
    update(status="processing", message="正在加载本地文字识别组件")
    video = cv2.VideoCapture(str(folder / ("video" + status["suffix"])))
    try:
        if not video.isOpened():
            raise ValueError("无法读取这份录屏，请导出为MP4后重试")
        fps = video.get(cv2.CAP_PROP_FPS)
        duration = video.get(cv2.CAP_PROP_FRAME_COUNT) / fps if fps > 0 else 0
        if not math.isfinite(duration) or not 0 < duration <= 600:
            raise ValueError("请提供10分钟以内、包含清晰终端画面的录屏")
        ocr = create_ocr()
        transcript = ["录屏文字识别：时间表示该行首次出现在采样画面的时刻；采样间隔0.5秒。"]
        previous_lines, previous_image = [], None
        captures, samples = 0, math.ceil(duration * 2)
        (folder / "frames").mkdir(exist_ok=True)
        for index in range(samples):
            seconds = index / 2
            video.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
            ok, frame = video.read()
            if not ok:
                continue
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (320, 180))
            changed = previous_image is None or float(np.abs(small.astype(float) - previous_image.astype(float)).mean()) > 0.12
            if changed:
                lines = recognize(ocr, frame)
                additions = changed_lines(previous_lines, lines)
                if additions:
                    captures += 1
                    cv2.imwrite(str(folder / "frames" / f"{int(seconds * 1000):07d}.jpg"), frame)
                    transcript.extend(f"[{clock(seconds)}] {line}" for line in additions)
                    previous_lines = lines
                previous_image = small
            update(progress=round((index + 1) / samples * 100), duration=round(duration, 3), sampled_frames=index + 1,
                   message=f"正在读取 {clock(seconds)} / {clock(duration)}，已提取{len(transcript) - 1}行文字")
        if len(transcript) == 1:
            raise ValueError("画面中未提取到清晰文字，请提供放大的终端录屏或操作日志")
        (folder / "transcript.log").write_text("\n".join(transcript) + "\n", encoding="utf-8")
        update(status="ready", progress=100, duration=round(duration, 3), captured_frames=captures,
               material_name=f"{Path(status['name']).stem[:40]}-{status['id'][:6]}-录屏识别.log", message="录屏文字已提取，可以回溯操作并开始诊断")
    except Exception as error:
        update(status="error", message=str(error)[:200])
        raise
    finally:
        video.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path, nargs="?")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (400, 100), "white")
        ImageDraw.Draw(image).text((15, 25), "LabAgent 123", fill="black", font=ImageFont.load_default(size=28))
        create_ocr()(np.asarray(image), use_cls=False)
        print("OCR ready", flush=True)
    elif args.folder:
        extract(args.folder.resolve())
    else:
        parser.error("Provide a recording folder or --prepare")
