"""Render a synthetic terminal recording matching the timestamped example log."""
from pathlib import Path
import os
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from labagent.operation_history import TIME, timestamp
    fonts = [Path(os.environ.get("WINDIR", "")) / "Fonts/consola.ttf", Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")]
    font = next((ImageFont.truetype(str(path), 24) for path in fonts if path.is_file()), ImageFont.load_default(size=24))
    source = ROOT / "data/examples/终端操作回溯.log"
    entries = []
    for line in source.read_text(encoding="utf-8").splitlines():
        match = TIME.match(line)
        if match:
            entries.append((timestamp(match[1]), line[match.end():]))
    output = ROOT / "data/examples/实验操作回放.webm"
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"VP80"), 4, (1280, 720))
    if not writer.isOpened():
        raise RuntimeError("Video encoder unavailable")
    try:
        for index in range(60):
            seconds = index / 4
            image = Image.new("RGB", (1280, 720), "#152236")
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 1280, 55), fill="#21334b")
            draw.text((24, 16), "LabAgent | CLI walkthrough (synthetic)", fill="#c6d7f1", font=font)
            visible = [text for moment, text in entries if moment <= seconds][-18:]
            for row, text in enumerate(visible):
                draw.text((24, 75 + row * 33), text, fill="#ffca82" if "Error:" in text else "#f1f5ff", font=font)
            if index == 16:
                image.save(ROOT / "data/examples/终端报错截图.png")
            writer.write(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
    finally:
        writer.release()
    print(output, flush=True)


if __name__ == "__main__":
    main()
