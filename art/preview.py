"""Composite the bloomed renders on the app background for a quick visual check.

    python preview.py cpu gpu ...   -> _preview/<name>.png   (large + 96 px + 48 px)
    python preview.py --sheet       -> _preview/sheet.png    (whole family at card size)
    python preview.py --icons       -> _preview/icons.png    (ICO frames, enlarged)
"""
import os
import sys

from PIL import Image

import export_assets as ex

HERE = os.path.dirname(os.path.abspath(__file__))
BG = (15, 18, 22, 255)
PV = os.path.join(HERE, "_preview")


def comp(name, max_side=760):
    im = ex.bloom(name)
    s = max_side / max(im.size)
    big = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    small = im.resize((int(96 * im.width / max(im.size)), int(96 * im.height / max(im.size))), Image.LANCZOS)
    out = Image.new("RGBA", (big.width + 120, big.height), BG)
    out.alpha_composite(big, (0, 0))
    out.alpha_composite(small, (big.width + 12, 12))
    tiny = im.resize((48, int(48 * im.height / im.width)), Image.LANCZOS)
    out.alpha_composite(tiny, (big.width + 36, 130))
    os.makedirs(PV, exist_ok=True)
    p = os.path.join(PV, name + ".png")
    out.convert("RGB").save(p)
    return p


def sheet(cell=300):
    names = [n for n in ex.HARDWARE if n != "board"]
    out = Image.new("RGBA", (cell * 4, cell * 2 + 20), BG)
    for i, n in enumerate(names + ["icon"]):
        if not os.path.exists(os.path.join(ex.RENDERS, n + ".png")):
            continue
        im = ex.bloom(n).resize((cell - 20, cell - 20), Image.LANCZOS)
        out.alpha_composite(im, ((i % 4) * cell + 10, (i // 4) * cell + 10))
    os.makedirs(PV, exist_ok=True)
    p = os.path.join(PV, "sheet.png")
    out.convert("RGB").save(p)
    return p


def icons():
    _, frames = ex.export_icon()
    order = [16, 24, 32, 48, 64, 128]
    out = Image.new("RGBA", (980, 420), BG)
    x = 10
    for n in order:                       # true size on dark, then on light
        out.alpha_composite(frames[n], (x, 10))
        x += n + 14
    light = Image.new("RGBA", (330, 150), (232, 235, 240, 255))
    x = 8
    for n in order:
        light.alpha_composite(frames[n], (x, 10))
        x += n + 14
    out.alpha_composite(light, (640, 0))
    x = 10
    for n in (16, 24, 32, 48):            # enlarged (nearest) to judge legibility
        z = frames[n].resize((n * (192 // n), n * (192 // n)), Image.NEAREST)
        out.alpha_composite(z, (x, 200))
        x += z.width + 30
    os.makedirs(PV, exist_ok=True)
    p = os.path.join(PV, "icons.png")
    out.convert("RGB").save(p)
    return p


def stats(names):
    """Fraction of the frame the solid object covers (ignores shadow / halo)."""
    import numpy as np
    for n in names:
        a = np.asarray(ex._load(n))[..., 3] > 150
        ys, xs = np.where(a)
        h, w = a.shape
        print("%-11s w %.0f%%  h %.0f%%   margins L%.0f%% R%.0f%% T%.0f%% B%.0f%%" % (
            n, 100 * (xs.max() - xs.min()) / w, 100 * (ys.max() - ys.min()) / h,
            100 * xs.min() / w, 100 * (w - 1 - xs.max()) / w, 100 * ys.min() / h, 100 * (h - 1 - ys.max()) / h))


if __name__ == "__main__":
    if "--stats" in sys.argv:
        stats([a for a in sys.argv[1:] if not a.startswith("--")])
        sys.exit()
    for n in sys.argv[1:]:
        print(sheet() if n == "--sheet" else icons() if n == "--icons" else comp(n))
