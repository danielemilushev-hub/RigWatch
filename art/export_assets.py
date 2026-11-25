"""RigWatch - turn the raw Blender renders into the files the app ships.

    python export_assets.py [name ...]

For every render in ./renders/<name>.png (+ <name>_glow.png, the emissive-only pass)
it adds a soft bloom around the accent lights, trims nothing (framing is baked into
the render) and writes  ../rigwatch/ui/assets/hw/<name>.webp  (RGBA, q88).
The icon is also written as  ../rigwatch/ui/assets/icon.png  (256) and  icon.ico.
"""
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
RENDERS = os.path.join(HERE, "renders")
ASSETS = os.path.normpath(os.path.join(HERE, "..", "rigwatch", "ui", "assets"))
HW = os.path.join(ASSETS, "hw")

HARDWARE = ["cpu", "gpu", "ram", "ssd", "network", "power", "fan", "board"]
ICONS = ["icon", "icon_small", "icon_tiny", "icon_glyph"]
BLOOM = {"icon": 0.55, "icon_small": 0.3, "icon_tiny": 0.2, "icon_glyph": 0.5}   # halo strength, default 0.8
CORE = {"icon": 0.88, "icon_small": 0.92, "icon_tiny": 0.95, "icon_glyph": 0.88}      # how much the true colour replaces the lit core


def _load(name):
    return Image.open(os.path.join(RENDERS, name + ".png")).convert("RGBA")


def bloom(name, strength=None):
    """Beauty render + blurred emissive pass -> RGBA image with glow halos."""
    im = _load(name)
    gpath = os.path.join(RENDERS, name + "_glow.png")
    if not os.path.exists(gpath):
        return im
    k = BLOOM.get(name, 0.8) if strength is None else strength
    a = np.asarray(im, dtype=np.float32) / 255.0
    g = np.asarray(Image.open(gpath).convert("RGBA").resize(im.size, Image.LANCZOS), dtype=np.float32) / 255.0
    glow = Image.fromarray((g[..., :3] * g[..., 3:4] * 255).astype(np.uint8), "RGB")
    s = max(im.size) / 1024.0
    acc = np.zeros(a.shape[:2] + (3,), dtype=np.float32)
    for radius, w in ((5, 0.50), (16, 0.42), (44, 0.30)):
        b = np.asarray(glow.filter(ImageFilter.GaussianBlur(radius * s)), dtype=np.float32) / 255.0
        acc = acc + b * w
    acc = np.clip(acc * k, 0.0, 1.0)
    pre = a[..., :3] * a[..., 3:4]                     # premultiplied beauty
    core = g[..., :3] * g[..., 3:4]                    # emitters in their exact accent colour
    m = np.clip(core.max(axis=2, keepdims=True), 0.0, 1.0) * CORE.get(name, 1.0)
    pre = pre * (1.0 - m) + core * CORE.get(name, 1.0)
    pre = np.clip(pre, 0.0, 1.0)
    pre = pre + acc * (1.0 - pre)                      # screen the halo over it
    cover = acc.max(axis=2, keepdims=True)
    alpha = a[..., 3:4] + cover * (1.0 - a[..., 3:4])
    rgb = np.where(alpha > 1e-4, pre / np.maximum(alpha, 1e-4), 0.0)
    out = np.concatenate([np.clip(rgb, 0, 1), np.clip(alpha, 0, 1)], axis=2)
    return Image.fromarray((out * 255.0 + 0.5).astype(np.uint8), "RGBA")


def export_hw(name):
    im = bloom(name)
    os.makedirs(HW, exist_ok=True)
    path = os.path.join(HW, name + ".webp")
    im.save(path, "WEBP", quality=88, method=6, exact=False)
    return path, im.size


def export_icon():
    """icon.png (256), icon.ico (16..256) and the emblem as webp for in-app use."""
    big = bloom("icon")
    small = bloom("icon_small") if os.path.exists(os.path.join(RENDERS, "icon_small.png")) else big
    tiny = bloom("icon_tiny") if os.path.exists(os.path.join(RENDERS, "icon_tiny.png")) else small
    glyph = bloom("icon_glyph") if os.path.exists(os.path.join(RENDERS, "icon_glyph.png")) else big
    os.makedirs(ASSETS, exist_ok=True)

    def fit(src, px, sharpen=0.0):
        im = src.resize((px, px), Image.LANCZOS)
        if sharpen:
            im = im.filter(ImageFilter.UnsharpMask(radius=0.6, percent=int(sharpen), threshold=0))
        return im

    frames = {
        256: fit(big, 256), 128: fit(big, 128), 64: fit(big, 64, 40), 48: fit(big, 48, 60),
        32: fit(small, 32, 70), 24: fit(small, 24, 80), 16: fit(tiny, 16, 90),
    }
    out = []
    p = os.path.join(ASSETS, "icon.png")
    frames[256].save(p, "PNG", optimize=True)
    out.append((p, (256, 256)))
    p = os.path.join(ASSETS, "icon.ico")
    order = [256, 128, 64, 48, 32, 24, 16]
    frames[256].save(p, format="ICO", sizes=[(n, n) for n in order],
                     append_images=[frames[n] for n in order[1:]])
    out.append((p, "16,24,32,48,64,128,256"))
    for nm, im in (("icon", big), ("icon_glyph", glyph)):
        p = os.path.join(HW, nm + ".webp")
        os.makedirs(HW, exist_ok=True)
        im.save(p, "WEBP", quality=90, method=6)
        out.append((p, im.size))
    return out, frames


def main(names):
    names = names or (HARDWARE + ["icon"])
    for n in names:
        if n in ICONS:
            continue
        if os.path.exists(os.path.join(RENDERS, n + ".png")):
            print("%s %s" % export_hw(n))
    if any(n in ICONS for n in names) and os.path.exists(os.path.join(RENDERS, "icon.png")):
        for p, size in export_icon()[0]:
            print(p, size)


if __name__ == "__main__":
    main(sys.argv[1:])
