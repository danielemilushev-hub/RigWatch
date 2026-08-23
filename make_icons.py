"""Generate the embedded PNG icon set for hw_monitor.py (run once, offline)."""
import base64, io
from PIL import Image, ImageDraw

SS = 8  # supersample factor


def new(size):
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def rr(d, box, r, **kw):
    d.rounded_rectangle([c * SS for c in box], radius=r * SS, **kw)


def rect(d, box, **kw):
    d.rectangle([c * SS for c in box], **kw)


def ell(d, box, **kw):
    d.ellipse([c * SS for c in box], **kw)


def line(d, pts, width=1, **kw):
    d.line([c * SS for c in pts], width=int(width * SS), **kw)


def finish(img, size):
    return img.resize((size, size), Image.LANCZOS)


def icon_app(size, accent, base):
    img, d = new(size)
    u = size / 28.0
    rr(d, (2 * u, 2 * u, 26 * u, 26 * u), 6 * u, outline=base, width=int(1.8 * u * SS))
    pts = [4.5 * u, 15 * u, 9 * u, 15 * u, 11.5 * u, 8.5 * u, 15 * u, 20 * u,
           17.5 * u, 13.5 * u, 19.5 * u, 15 * u, 23.5 * u, 15 * u]
    line(d, pts, width=2.0 * u, fill=accent, joint="curve")
    return finish(img, size)


def icon_cpu(size, accent, base):
    img, d = new(size)
    u = size / 28.0
    # pins
    for i in range(3):
        off = (8 + i * 6) * u
        rect(d, (off, 1.5 * u, off + 2 * u, 6 * u), fill=base)
        rect(d, (off, 22 * u, off + 2 * u, 26.5 * u), fill=base)
        rect(d, (1.5 * u, off, 6 * u, off + 2 * u), fill=base)
        rect(d, (22 * u, off, 26.5 * u, off + 2 * u), fill=base)
    rr(d, (5 * u, 5 * u, 23 * u, 23 * u), 3.5 * u, fill=None,
       outline=accent, width=int(2 * u * SS))
    rr(d, (10 * u, 10 * u, 18 * u, 18 * u), 1.6 * u, fill=accent)
    return finish(img, size)


def icon_ram(size, accent, base):
    img, d = new(size)
    u = size / 28.0
    rr(d, (1.5 * u, 7 * u, 26.5 * u, 21 * u), 2 * u, outline=accent,
       width=int(1.8 * u * SS))
    for i in range(4):
        x = (4.5 + i * 5.2) * u
        rr(d, (x, 10 * u, x + 3.6 * u, 16 * u), 0.8 * u, fill=accent)
    # notch + contact pins along the bottom edge
    rect(d, (12 * u, 19.4 * u, 16 * u, 21.6 * u), fill=(0, 0, 0, 0))
    for i in range(9):
        x = (3 + i * 2.6) * u
        if 11 * u < x < 16 * u:
            continue
        rect(d, (x, 21 * u, x + 1.4 * u, 24 * u), fill=base)
    return finish(img, size)


def icon_gpu(size, accent, base):
    img, d = new(size)
    u = size / 28.0
    rr(d, (1.5 * u, 5 * u, 26.5 * u, 22 * u), 2.4 * u, outline=accent,
       width=int(1.8 * u * SS))
    # fan
    cx, cy, r = 9.5 * u, 13.5 * u, 5.2 * u
    ell(d, (cx - r, cy - r, cx + r, cy + r), outline=accent, width=int(1.4 * u * SS))
    ell(d, (cx - 1.3 * u, cy - 1.3 * u, cx + 1.3 * u, cy + 1.3 * u), fill=accent)
    for ang in (0, 120, 240):
        d.pieslice([(cx - r + 1.2 * u) * SS, (cy - r + 1.2 * u) * SS,
                    (cx + r - 1.2 * u) * SS, (cy + r - 1.2 * u) * SS],
                   ang, ang + 46, fill=accent)
    # heatsink fins
    for i in range(4):
        x = (17 + i * 2.3) * u
        rect(d, (x, 8.5 * u, x + 1.1 * u, 18.5 * u), fill=accent)
    # pcie bracket teeth
    for i in range(6):
        x = (3.5 + i * 3.6) * u
        rect(d, (x, 22 * u, x + 2.2 * u, 24.5 * u), fill=base)
    return finish(img, size)


def icon_ssd(size, accent, base):
    img, d = new(size)
    u = size / 28.0
    rr(d, (2 * u, 4 * u, 26 * u, 24 * u), 2.6 * u, outline=accent,
       width=int(1.8 * u * SS))
    for row in range(2):
        for col in range(3):
            x = (5.5 + col * 6.2) * u
            y = (7.5 + row * 7) * u
            rr(d, (x, y, x + 4.6 * u, y + 4.6 * u), 0.8 * u, fill=accent)
    # connector pins on the left edge
    for i in range(3):
        y = (8 + i * 5.6) * u
        rect(d, (0.5 * u, y, 2.5 * u, y + 3 * u), fill=base)
    return finish(img, size)


def icon_bolt(size, accent, base):
    img, d = new(size)
    u = size / 28.0
    rr(d, (2 * u, 2 * u, 26 * u, 26 * u), 6 * u, outline=base,
       width=int(1.6 * u * SS))
    pts = [(16.5 * u, 4.5 * u), (8 * u, 15.5 * u), (13 * u, 15.5 * u),
           (11.5 * u, 23.5 * u), (20 * u, 12.5 * u), (15 * u, 12.5 * u)]
    d.polygon([(x * SS, y * SS) for x, y in pts], fill=accent)
    return finish(img, size)


BASE = (155, 165, 185, 255)
SPECS = {
    "app": (icon_app, (77, 163, 255, 255)),
    "cpu": (icon_cpu, (77, 163, 255, 255)),
    "ram": (icon_ram, (53, 208, 165, 255)),
    "gpu_a": (icon_gpu, (255, 138, 76, 255)),
    "gpu_b": (icon_gpu, (176, 125, 255, 255)),
    "ssd": (icon_ssd, (77, 214, 255, 255)),
    "bolt": (icon_bolt, (255, 209, 102, 255)),
}

out = {}
for name, (fn, accent) in SPECS.items():
    for size in (28, 56):
        buf = io.BytesIO()
        fn(size, accent, BASE).save(buf, "PNG", optimize=True)
        out.setdefault(name, {})[size] = base64.b64encode(buf.getvalue()).decode()

lines = ["ICONS = {"]
for name in SPECS:
    lines.append("    %r: {" % name)
    for size in (28, 56):
        blob = out[name][size]
        lines.append("        %d: (" % size)
        for i in range(0, len(blob), 88):
            lines.append("            %r" % blob[i:i + 88])
        lines.append("        ),")
    lines.append("    },")
lines.append("}")
open("icons_block.py", "w").write("\n".join(lines) + "\n")
print("bytes of base64:", sum(len(v) for d in out.values() for v in d.values()))
