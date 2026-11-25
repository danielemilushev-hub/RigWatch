"""Final pass over the exported hardware art in ``rigwatch/ui/assets/hw``.

Run after ``export_assets.py``:

1. Downscale to the size the UI actually shows (512 px; the board hero 1000 px).
2. Fade glow / shadow haze to zero at the frame edge so no faint box shows
   behind an illustration. Solid pixels of the object itself are untouched.
3. Write a light-surface cut to ``hw/light``: the soft glows only work on dark
   backgrounds, so that cut keeps just the object and its anti-aliased edge.

Usage (any Python with Pillow and NumPy):  python art/finalize_assets.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

HW = Path(__file__).resolve().parent.parent / "rigwatch" / "ui" / "assets" / "hw"
ASSETS = ["cpu", "gpu", "ram", "ssd", "network", "power", "fan", "board"]


def smooth(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


def main() -> None:
    (HW / "light").mkdir(exist_ok=True)
    for name in ASSETS:
        path = HW / f"{name}.webp"
        im = Image.open(path).convert("RGBA")
        target = 1000 if name == "board" else 512
        if im.width > target:
            im = im.resize((target, round(im.height * target / im.width)), Image.LANCZOS)
        px = np.asarray(im).astype(np.float32)
        alpha = px[..., 3] / 255.0
        h, w = alpha.shape

        band = 0.16
        yy = np.minimum(np.arange(h), np.arange(h)[::-1])[:, None] / (h * band)
        xx = np.minimum(np.arange(w), np.arange(w)[::-1])[None, :] / (w * band)
        vignette = smooth(yy) * smooth(xx)
        solid = smooth((alpha - 0.4) / 0.4)
        # thin out the near-black ground-shadow haze (it reads as a dark box on a card) but keep coloured glow
        lum = (0.2126 * px[..., 0] + 0.7152 * px[..., 1] + 0.0722 * px[..., 2]) / 255
        shadow = 1 - smooth((lum - 0.06) / 0.14)
        keep = 1 - 0.7 * shadow * (1 - solid)
        dark = px.copy()
        dark[..., 3] = alpha * (vignette + (1 - vignette) * solid) * keep * 255
        Image.fromarray(dark.astype(np.uint8), "RGBA").save(path, "WEBP", quality=88, method=6)

        light = px.copy()
        light[..., 3] = smooth((alpha - 0.30) / 0.48) * 255
        Image.fromarray(light.astype(np.uint8), "RGBA").save(HW / "light" / f"{name}.webp", "WEBP", quality=88, method=6)
        print(f"{name}: {w}x{h}")


if __name__ == "__main__":
    main()
