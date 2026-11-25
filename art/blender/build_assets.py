"""RigWatch - procedural hardware art (Blender 5.x, Cycles).

Every model is built with bpy/bmesh, no external assets.

    blender -b --factory-startup -P build_assets.py -- [--only cpu,gpu] [--size 1024]
                                                       [--samples 128] [--cpu]

Outputs ../renders/<name>.png (RGBA beauty render, AgX) and ../renders/<name>_glow.png
(emitters only, exact accent colours).  Run ../export_assets.py afterwards: it merges
the two (true-colour light cores + bloom) and writes the WebP / ICO files the app ships.
Assets: cpu gpu ram ssd network power fan board icon icon_small icon_tiny icon_glyph.
"""
import math
import os
import sys
import time
from math import cos, pi, radians, sin, tau

import bmesh
import bpy
from mathutils import Matrix, Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "renders")

ACCENT = {
    "cpu": "#3987E5",
    "gpu": "#D95926",
    "ram": "#199E70",
    "ssd": "#C98500",
    "network": "#D55181",
    "power": "#9085E9",
    "fan": "#8FA3B8",
}
ICON_GRAD = ("#3987E5", "#9085E9")

LIGHT_GAIN = 0.11      # global exposure trim for the light rig
STRIP = 1.9            # emission strength of thin accent strips / LEDs
BAR = 1.25             # emission strength of large light bars

PARTS = []          # every object that belongs to the model (used for framing)
_MATS = {}


# ───────────────────────────── colour helpers ─────────────────────────────

def lin(h):
    """sRGB hex -> linear RGB tuple."""
    h = h.lstrip("#")

    def c(v):
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return tuple(c(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def vivid(rgb, sat=1.25):
    """Push a linear colour away from its grey point (same hue, more punch)."""
    g = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    out = [max(0.0, g + (c - g) * sat) for c in rgb]
    m = max(out) or 1.0
    return tuple(c / m for c in out)


# ───────────────────────────── materials ─────────────────────────────

def _principled(name):
    m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:
        pass
    nt = m.node_tree
    bsdf = next((n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    if bsdf is None:
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        nt.links.new(bsdf.outputs[0], out.inputs[0])
    return m, nt, bsdf


def _set(bsdf, **kw):
    for k, v in kw.items():
        if k in bsdf.inputs:
            bsdf.inputs[k].default_value = v


def mat(name, color, metallic=0.0, rough=0.5, coat=0.0, coat_rough=0.1,
        emit=None, strength=0.0, spec=0.5, brushed=None, grain=0.0):
    if name in _MATS:
        return _MATS[name]
    m, nt, b = _principled(name)
    _set(b, **{"Base Color": (*color, 1.0), "Metallic": metallic, "Roughness": rough,
               "Coat Weight": coat, "Coat Roughness": coat_rough,
               "Specular IOR Level": spec})
    if emit is not None:
        _set(b, **{"Emission Color": (*emit, 1.0), "Emission Strength": strength})
    if brushed or grain:
        tc = nt.nodes.new("ShaderNodeTexCoord")
        mp = nt.nodes.new("ShaderNodeMapping")
        nz = nt.nodes.new("ShaderNodeTexNoise")
        bp = nt.nodes.new("ShaderNodeBump")
        if brushed:
            mp.inputs["Scale"].default_value = brushed
            nz.inputs["Scale"].default_value = 14.0
            nz.inputs["Detail"].default_value = 6.0
            bp.inputs["Strength"].default_value = 0.10
        else:
            mp.inputs["Scale"].default_value = (1, 1, 1)
            nz.inputs["Scale"].default_value = grain
            nz.inputs["Detail"].default_value = 3.0
            bp.inputs["Strength"].default_value = 0.06
        bp.inputs["Distance"].default_value = 0.02
        nt.links.new(tc.outputs["Object"], mp.inputs["Vector"])
        nt.links.new(mp.outputs["Vector"], nz.inputs["Vector"])
        nt.links.new(nz.outputs[0], bp.inputs["Height"])
        nt.links.new(bp.outputs["Normal"], b.inputs["Normal"])
    _MATS[name] = m
    return m


class M:
    """The shared material family."""

    @staticmethod
    def graphite():
        return mat("graphite", lin("#3b414b"), metallic=0.75, rough=0.36, coat=0.35, coat_rough=0.25)

    @staticmethod
    def graphite_dark():
        return mat("graphite_dark", lin("#22262d"), metallic=0.6, rough=0.42, coat=0.3, coat_rough=0.3)

    @staticmethod
    def graphite_lite():
        return mat("graphite_lite", lin("#59616d"), metallic=0.8, rough=0.33, coat=0.3, coat_rough=0.2)

    @staticmethod
    def metal(axis="x"):
        sc = {"x": (1.0, 70.0, 70.0), "y": (70.0, 1.0, 70.0), "z": (70.0, 70.0, 1.0)}[axis]
        return mat("metal_" + axis, (0.66, 0.68, 0.72), metallic=1.0, rough=0.27, brushed=sc)

    @staticmethod
    def steel():
        return mat("steel", (0.50, 0.52, 0.56), metallic=1.0, rough=0.34)

    @staticmethod
    def alu():
        return mat("alu", (0.30, 0.32, 0.35), metallic=1.0, rough=0.4)

    @staticmethod
    def gold():
        return mat("gold", (1.0, 0.70, 0.28), metallic=1.0, rough=0.24)

    @staticmethod
    def pcb():
        return mat("pcb", lin("#141b22"), metallic=0.0, rough=0.45, coat=0.35, coat_rough=0.3, spec=0.5)

    @staticmethod
    def chip():
        return mat("chip", lin("#15171b"), rough=0.36, spec=0.6)

    @staticmethod
    def plastic():
        return mat("plastic", lin("#1d2026"), rough=0.5, spec=0.5)

    @staticmethod
    def slot():
        return mat("slot", lin("#2a2e36"), rough=0.45, spec=0.5)

    @staticmethod
    def blade():
        return mat("blade", lin("#525a66"), metallic=0.2, rough=0.38, coat=0.5, coat_rough=0.2)

    @staticmethod
    def gloss():
        return mat("gloss", lin("#12151a"), rough=0.12, coat=1.0, coat_rough=0.04, spec=0.7)

    @staticmethod
    def cap():
        return mat("cap", lin("#a88f66"), rough=0.55)

    @staticmethod
    def glow(hexcol, strength=4.0):
        c = vivid(lin(hexcol))
        return mat("glow_%s_%.1f" % (hexcol, strength), c, rough=0.4, emit=c, strength=strength)

    @staticmethod
    def glow_grad(c0, c1, strength, direction, half_len):
        name = "grad_%s_%s" % (c0, c1)
        if name in _MATS:
            return _MATS[name]
        m, nt, b = _principled(name)
        tc = nt.nodes.new("ShaderNodeTexCoord")
        dot = nt.nodes.new("ShaderNodeVectorMath")
        dot.operation = "DOT_PRODUCT"
        d = Vector(direction).normalized()
        dot.inputs[1].default_value = d
        ma = nt.nodes.new("ShaderNodeMath")
        ma.operation = "MULTIPLY_ADD"
        ma.inputs[1].default_value = 0.5 / half_len
        ma.inputs[2].default_value = 0.5
        ramp = nt.nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = (*vivid(lin(c0)), 1.0)
        ramp.color_ramp.elements[1].color = (*vivid(lin(c1)), 1.0)
        nt.links.new(tc.outputs["Object"], dot.inputs[0])
        nt.links.new(dot.outputs["Value"], ma.inputs[0])
        nt.links.new(ma.outputs[0], ramp.inputs[0])
        nt.links.new(ramp.outputs["Color"], b.inputs["Base Color"])
        nt.links.new(ramp.outputs["Color"], b.inputs["Emission Color"])
        _set(b, **{"Emission Strength": strength, "Roughness": 0.3, "Coat Weight": 0.6,
                   "Coat Roughness": 0.08})
        _MATS[name] = m
        return m


# ───────────────────────────── geometry helpers ─────────────────────────────

def mesh_obj(name, bm, material=None, loc=(0, 0, 0), rot=(0, 0, 0)):
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    if material is not None:
        me.materials.append(material)
    ob.location = loc
    ob.rotation_euler = rot
    PARTS.append(ob)
    return ob


def finish(ob, bevel=0.02, seg=3, angle=35.0):
    """Bevel hard edges (or mark them sharp) and fix up the shading normals."""
    me = ob.data
    if bevel and bevel > 0:
        m = ob.modifiers.new("Bevel", "BEVEL")
        m.width = bevel
        m.segments = seg
        m.limit_method = "ANGLE"
        m.angle_limit = radians(angle)
        m.use_clamp_overlap = True
    else:
        try:
            me.set_sharp_from_angle(angle=radians(angle))
        except Exception:
            for p in me.polygons:
                p.use_smooth = False
    w = ob.modifiers.new("WN", "WEIGHTED_NORMAL")
    w.keep_sharp = True
    w.weight = 60
    return ob


def rrect_pts(sx, sy, r, n=10, cx=0.0, cy=0.0):
    hx, hy = sx / 2, sy / 2
    r = min(r, hx, hy)
    pts = []
    for ox, oy, a0 in ((hx - r, hy - r, 0), (-hx + r, hy - r, 90),
                       (-hx + r, -hy + r, 180), (hx - r, -hy + r, 270)):
        for i in range(n + 1):
            a = radians(a0 + 90.0 * i / n)
            pts.append((cx + ox + r * cos(a), cy + oy + r * sin(a)))
    return pts


def rot_pts(pts, ang, cx=0.0, cy=0.0):
    ca, sa = cos(ang), sin(ang)
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in pts]


def prism(name, pts, z0, z1, material=None, bevel=0.02, seg=3, loc=(0, 0, 0),
          rot=(0, 0, 0), defer=False, angle=35.0):
    """Extrude a 2D outline (XY) from z0 to z1."""
    bm = bmesh.new()
    vs = [bm.verts.new((x, y, z0)) for x, y in pts]
    f = bm.faces.new(vs)
    r = bmesh.ops.extrude_face_region(bm, geom=[f])
    top = [e for e in r["geom"] if isinstance(e, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, vec=(0, 0, z1 - z0), verts=top)
    ob = mesh_obj(name, bm, material, loc, rot)
    if not defer:
        finish(ob, bevel, seg, angle)
    return ob


def box(name, sx, sy, sz, loc, material=None, bevel=0.02, seg=3, rot=(0, 0, 0), defer=False):
    """Box whose base centre sits at `loc`."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=(sx, sy, sz), verts=bm.verts[:])
    bmesh.ops.translate(bm, vec=(0, 0, sz / 2), verts=bm.verts[:])
    ob = mesh_obj(name, bm, material, loc, rot)
    if not defer:
        finish(ob, bevel, seg)
    return ob


def multi_box(name, items, material, bevel=0.0, seg=2):
    """Many boxes in one mesh. item = (sx, sy, sz, x, y, zbase[, rot_z])."""
    bm = bmesh.new()
    for it in items:
        sx, sy, sz, x, y, z = it[:6]
        rz = it[6] if len(it) > 6 else 0.0
        vs = bmesh.ops.create_cube(bm, size=1.0)["verts"]
        bmesh.ops.scale(bm, vec=(sx, sy, sz), verts=vs)
        if rz:
            bmesh.ops.rotate(bm, cent=(0, 0, 0), matrix=Matrix.Rotation(rz, 3, "Z"), verts=vs)
        bmesh.ops.translate(bm, vec=(x, y, z + sz / 2), verts=vs)
    ob = mesh_obj(name, bm, material)
    finish(ob, bevel, seg)
    return ob


def cyl(name, r, h, loc, material=None, bevel=0.02, seg=3, segs=64, rot=(0, 0, 0),
        r2=None, defer=False):
    """Cylinder / cone whose base centre sits at `loc`."""
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segs,
                          radius1=r, radius2=r if r2 is None else r2, depth=h)
    bmesh.ops.translate(bm, vec=(0, 0, h / 2), verts=bm.verts[:])
    ob = mesh_obj(name, bm, material, loc, rot)
    if not defer:
        finish(ob, bevel, seg)
    return ob


def multi_cyl(name, items, material, bevel=0.0, seg=2, segs=24):
    """item = (r, h, x, y, zbase)."""
    bm = bmesh.new()
    for r, h, x, y, z in items:
        vs = bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segs,
                                   radius1=r, radius2=r, depth=h)["verts"]
        bmesh.ops.translate(bm, vec=(x, y, z + h / 2), verts=vs)
    ob = mesh_obj(name, bm, material)
    finish(ob, bevel, seg)
    return ob


def revolve(name, profile, material=None, a0=0.0, a1=tau, segs=128, bevel=0.0, seg=2,
            loc=(0, 0, 0), rot=(0, 0, 0)):
    """Sweep a closed (radius, z) profile around Z."""
    full = abs((a1 - a0) - tau) < 1e-6
    count = segs if full else segs + 1
    bm = bmesh.new()
    rings = []
    for i in range(count):
        a = a0 + (a1 - a0) * i / segs
        ca, sa = cos(a), sin(a)
        rings.append([bm.verts.new((r * ca, r * sa, z)) for r, z in profile])
    m = len(profile)
    for i in range(segs):
        A, B = rings[i], rings[(i + 1) % count]
        for j in range(m):
            bm.faces.new((A[j], A[(j + 1) % m], B[(j + 1) % m], B[j]))
    if not full:
        bm.faces.new(rings[0])
        bm.faces.new(rings[-1])
    ob = mesh_obj(name, bm, material, loc, rot)
    finish(ob, bevel, seg)
    return ob


def ring(name, r_in, r_out, z0, z1, material, loc=(0, 0, 0), a0=0.0, a1=tau, bevel=0.0, segs=128):
    return revolve(name, [(r_in, z0), (r_out, z0), (r_out, z1), (r_in, z1)], material,
                   a0=a0, a1=a1, bevel=bevel, loc=loc, segs=segs)


def torus(name, R, r, z, material, loc=(0, 0, 0), n=10):
    prof = [(R + r * cos(tau * i / n), z + r * sin(tau * i / n)) for i in range(n)]
    return revolve(name, prof, material, loc=loc, segs=96, bevel=0.0)


def sphere(name, r, loc, material):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=r)
    ob = mesh_obj(name, bm, material, loc)
    return ob


def cut(ob, cutters):
    """Boolean-subtract `cutters` from `ob` destructively (cutters are removed)."""
    for c in cutters:
        m = ob.modifiers.new("Cut", "BOOLEAN")
        m.operation = "DIFFERENCE"
        m.object = c
        try:
            m.solver = "EXACT"
        except Exception:
            pass
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    old = ob.data
    ob.modifiers.clear()
    ob.data = me
    bpy.data.meshes.remove(old)
    for c in cutters:
        if c in PARTS:
            PARTS.remove(c)
        bpy.data.objects.remove(c)
    for p in ob.data.polygons:
        p.use_smooth = True
    return ob


def tube(name, pts, radius, material, round_ends=True, res=6):
    """A round tube following a 3D polyline (uniform thickness, rounded ends)."""
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = radius
    cu.bevel_resolution = res
    cu.use_fill_caps = True
    sp = cu.splines.new("POLY")
    sp.points.add(len(pts) - 1)
    for p, co in zip(sp.points, pts):
        p.co = (co[0], co[1], co[2], 1.0)
    ob = bpy.data.objects.new(name, cu)
    bpy.context.scene.collection.objects.link(ob)
    cu.materials.append(material)
    PARTS.append(ob)
    if round_ends:
        sphere(name + "_e0", radius, pts[0], material)
        sphere(name + "_e1", radius, pts[-1], material)
    return ob


def fillet(pts, radius, n=8):
    """Round the corners of a 2D polyline."""
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        p0, p1, p2 = Vector(pts[i - 1]), Vector(pts[i]), Vector(pts[i + 1])
        d0, d1 = (p0 - p1), (p2 - p1)
        r = min(radius, d0.length * 0.45, d1.length * 0.45)
        a, b = p1 + d0.normalized() * r, p1 + d1.normalized() * r
        for k in range(n + 1):
            t = k / n
            out.append(tuple((1 - t) ** 2 * a + 2 * (1 - t) * t * p1 + t ** 2 * b))
    out.append(pts[-1])
    return out


def rotor(name, c, r_hub, r_tip, n, hub_h, blade_mat=None, hub_mat=None, sweep=40.0,
          chord0=0.40, chord1=0.66, pitch0=38.0, pitch1=20.0, thick=None):
    """Axial fan rotor (hub + swept, pitched blades). c = centre of hub base."""
    cx, cy, cz = c
    zmid = cz + hub_h * 0.5
    bm = bmesh.new()
    NU, NV = 14, 8
    r0 = r_hub * 0.9
    for b in range(n):
        base = tau * b / n
        grid = []
        for i in range(NU + 1):
            u = i / NU
            r = r0 + u * (r_tip - r0)
            ch = (chord0 + (chord1 - chord0) * sin(u * pi / 2)) * r_tip
            if u > 0.86:
                ch *= math.sqrt(max(0.0, 1.0 - ((u - 0.86) / 0.14) ** 2 * 0.80))
            thc = base - radians(sweep) * (u ** 1.3)
            pit = radians(pitch0 + (pitch1 - pitch0) * u)
            row = []
            for j in range(NV + 1):
                v = j / NV - 0.5
                th = thc + v * ch * cos(pit) / r
                z = zmid + v * ch * sin(pit)
                row.append(bm.verts.new((cx + r * cos(th), cy + r * sin(th), z)))
            grid.append(row)
        for i in range(NU):
            for j in range(NV):
                bm.faces.new((grid[i][j], grid[i + 1][j], grid[i + 1][j + 1], grid[i][j + 1]))
    blades = mesh_obj(name + "_blades", bm, blade_mat or M.blade())
    s = blades.modifiers.new("Solid", "SOLIDIFY")
    s.thickness = thick or r_tip * 0.03
    s.offset = 0.0
    sub = blades.modifiers.new("Sub", "SUBSURF")
    sub.levels = 1
    sub.render_levels = 1
    hub = cyl(name + "_hub", r_hub, hub_h, (cx, cy, cz), hub_mat or M.graphite_dark(),
              bevel=min(0.06, hub_h * 0.25), seg=4, segs=96)
    return blades, hub


# ───────────────────────────── models ─────────────────────────────

def build_cpu(acc):
    prism("substrate", rrect_pts(4.0, 4.0, 0.14), 0.0, 0.16, M.pcb(), bevel=0.025)
    pads = []
    for k in range(4):
        ang = k * pi / 2
        for d in (1.80, 1.91):
            for i in range(32):
                x, y = -1.74 + 3.48 * i / 31, d
                xr, yr = x * cos(ang) - y * sin(ang), x * sin(ang) + y * cos(ang)
                if xr < -1.45 and yr < -1.45:
                    continue
                pads.append((0.066, 0.066, 0.012, xr, yr, 0.16))
    multi_box("pads", pads, M.gold())
    prism("marker", [(-1.94, -1.94), (-1.50, -1.94), (-1.94, -1.50)], 0.16, 0.174, M.gold(), bevel=0)

    a, c, d, w, o = 1.66, 0.34, 0.30, 0.44, 0.74
    side = [(a - c, a), (o + w / 2, a), (o + w / 2, a - d), (o - w / 2, a - d), (o - w / 2, a),
            (-o + w / 2, a), (-o + w / 2, a - d), (-o - w / 2, a - d), (-o - w / 2, a), (-(a - c), a)]
    pts = []
    for k in range(4):
        pts += rot_pts(side, k * pi / 2)
    prism("ihs_flange", pts, 0.16, 0.37, M.metal("x"), bevel=0.03, seg=3)
    caps = []
    for k in range(4):
        ang = k * pi / 2
        for sx in (-o, o):
            for dx in (-0.1, 0.1):
                x, y = sx + dx, a - d / 2 + 0.01
                caps.append((0.11, 0.20, 0.09, x * cos(ang) - y * sin(ang),
                             x * sin(ang) + y * cos(ang), 0.16, ang))
    multi_box("caps", caps, M.cap(), bevel=0.012)
    prism("glow", rrect_pts(2.74, 2.74, 0.32), 0.37, 0.40, M.glow(acc, STRIP), bevel=0)
    prism("ihs_top", rrect_pts(2.52, 2.52, 0.24), 0.37, 0.62, M.metal("x"), bevel=0.07, seg=5)


def build_gpu(acc):
    g = M.graphite()
    prism("backplate", rrect_pts(8.0, 3.2, 0.12), 0.0, 0.07, M.graphite_lite(), bevel=0.02)
    box("pcb", 7.9, 3.1, 0.06, (0, 0, 0.07), M.pcb(), bevel=0)
    box("tab_a", 0.55, 0.44, 0.06, (-2.95, -1.76, 0.07), M.pcb(), bevel=0)
    box("tab_b", 2.90, 0.44, 0.06, (-1.10, -1.76, 0.07), M.pcb(), bevel=0)
    fingers = []
    for x0, x1 in ((-3.19, -2.71), (-2.51, 0.31)):
        n = int((x1 - x0) / 0.085)
        for i in range(n + 1):
            fingers.append((0.05, 0.30, 0.012, x0 + (x1 - x0) * i / n, -1.80, 0.13))
    multi_box("fingers", fingers, M.gold())

    multi_box("fins", [(0.035, 2.9, 0.5, -3.75 + i * 0.1, 0, 0.13) for i in range(76)], M.alu())

    sh = prism("shroud", rrect_pts(8.0, 3.2, 0.34), 0.13, 1.02, g, defer=True)
    fx = (-2.62, 0.0, 2.62)
    cut(sh, [cyl("c%d" % i, 1.21, 1.3, (x, 0, 0.0), defer=True, segs=96) for i, x in enumerate(fx)])
    finish(sh, 0.05, 4)
    for i, x in enumerate(fx):
        rotor("fan%d" % i, (x, 0, 0.54), 0.36, 1.15, 9, 0.36)
        ring("hubcap%d" % i, 0.0, 0.26, 0.90, 0.915, M.graphite_lite(), loc=(x, 0, 0), segs=64)
        ring("fanglow%d" % i, 1.21, 1.27, 1.02, 1.036, M.glow(acc, STRIP), loc=(x, 0, 0))
    hour = [(-0.30, -1.46), (0.30, -1.46), (0.075, -0.3), (0.075, 0.3), (0.30, 1.46),
            (-0.30, 1.46), (-0.075, 0.3), (-0.075, -0.3)]
    for i, x in enumerate((-1.31, 1.31)):
        prism("inlay%d" % i, [(px + x, py) for px, py in hour], 1.02, 1.04, M.metal("x"), bevel=0.008)
    box("strip", 4.6, 0.03, 0.06, (0.9, -1.6, 0.66), M.glow(acc, STRIP), bevel=0)

    box("bracket", 0.05, 3.9, 1.22, (-4.045, -0.25, 0.0), M.steel(), bevel=0.012)
    ports = [(0.03, 0.52, 0.18, -4.07, y, 0.20) for y in (-1.05, -0.35, 0.35)]
    ports.append((0.03, 0.55, 0.22, -4.07, 1.08, 0.18))
    ports += [(0.03, 0.42, 0.06, -4.07, -1.0 + i * 0.5, zz) for i in range(5) for zz in (0.70, 0.86)]
    multi_box("ports", ports, M.chip(), bevel=0.01)
    multi_box("pwr", [(0.62, 0.22, 0.34, 2.0, 1.66, 0.42), (0.62, 0.22, 0.34, 2.72, 1.66, 0.42)],
              M.plastic(), bevel=0.02)


def _ram_stick(tag, x0, y0, acc, deco=True):
    def xz(name, pts, t, material, bevel=0.01, seg=2):
        return prism(tag + name, pts, -t / 2, t / 2, material, bevel=bevel, seg=seg,
                     loc=(x0, y0, 0), rot=(radians(90), 0, 0))
    xz("pcb", [(-3, 0.0), (-0.44, 0.0), (-0.44, 0.15), (-0.28, 0.15), (-0.28, 0.0), (3, 0.0),
               (3, 1.45), (-3, 1.45)], 0.06, M.pcb(), bevel=0)
    fingers = []
    x = -2.93
    while x <= 2.93:
        if not (-0.50 < x < -0.22):
            for side in (-1, 1):
                fingers.append((0.045, 0.012, 0.22, x0 + x, y0 + side * 0.034, 0.02))
        x += 0.075
    multi_box(tag + "fingers", fingers, M.gold())
    xz("hs", [(-2.96, 0.32), (2.96, 0.32), (2.96, 1.20), (2.70, 1.50), (-2.70, 1.50), (-2.96, 1.20)],
       0.27, M.graphite(), bevel=0.03, seg=3)
    xz("inlay", [(-2.62, 0.50), (0.85, 0.50), (1.40, 1.14), (-2.62, 1.14)], 0.30, M.graphite_lite(), bevel=0.012)
    for k in range(4):
        y = 0.615 + k * 0.14
        xa, xb = 0.85 + (y - 0.50) * 0.859 - 0.14, 0.85 + (y + 0.04 - 0.50) * 0.859 - 0.14
        xz("groove%d" % k, [(-2.46, y), (xa, y), (xb, y + 0.04), (-2.46, y + 0.04)], 0.306, M.chip(), bevel=0)
    if deco:
        xz("slash", [(1.12, 0.50), (1.30, 0.50), (1.85, 1.14), (1.67, 1.14)], 0.30, M.glow(acc, STRIP), bevel=0)
        xz("slash2", [(1.57, 0.50), (1.75, 0.50), (2.30, 1.14), (2.12, 1.14)], 0.30, M.metal("x"), bevel=0.008)
    xz("bar", [(-2.70, 1.50), (2.70, 1.50), (2.56, 1.71), (-2.56, 1.71)], 0.20, M.glow(acc, BAR),
       bevel=0.04, seg=4)


def build_ram(acc):
    _ram_stick("a_", -0.30, -0.62, acc)
    _ram_stick("b_", 0.30, 0.62, acc, deco=False)   # its face is hidden behind the front stick


def build_ssd(acc):
    pts = [(-4, -1.1), (4, -1.1), (4, -0.28)]
    pts += [(4 + 0.28 * cos(radians(a)), 0.28 * sin(radians(a))) for a in range(255, 104, -15)]
    pts += [(4, 0.28), (4, 1.1), (-4, 1.1), (-4, 0.74), (-3.60, 0.74), (-3.60, 0.56), (-4, 0.56)]
    prism("pcb", pts, 0.0, 0.07, M.pcb(), bevel=0.012)
    fingers = []
    y = -1.03
    while y <= 1.04:
        if not (0.50 < y < 0.80):
            fingers.append((0.32, 0.036, 0.008, -3.82, y, 0.07))
        y += 0.068
    multi_box("fingers", fingers, M.gold())
    multi_box("chips", [(1.15, 1.15, 0.11, -2.35, 0.0, 0.07), (0.72, 1.2, 0.09, -0.95, 0.0, 0.07),
                        (0.36, 0.36, 0.07, -3.2, -0.62, 0.07)], M.chip(), bevel=0.02)
    box("ctrl_lid", 0.8, 0.8, 0.012, (-2.35, 0.0, 0.18), M.metal("x"), bevel=0.004)
    caps = [(0.16, 0.09, 0.06, x, y, 0.07) for x, y in
            ((-3.25, 0.2), (-3.25, 0.0), (-3.05, 0.88), (-2.75, 0.88), (-1.6, 0.85), (-1.6, -0.85),
             (-1.35, -0.85), (-0.25, 0.8), (-0.25, 0.55), (-0.25, -0.8), (-3.1, -0.2))]
    multi_box("caps", caps, M.cap(), bevel=0.01)
    prism("plate", rrect_pts(3.55, 1.96, 0.10, cx=1.75), 0.07, 0.21, M.graphite(), bevel=0.03)
    prism("label", rrect_pts(1.9, 0.9, 0.05, cx=1.5, cy=0.25), 0.21, 0.222, M.graphite_lite(), bevel=0.004)
    multi_box("label_lines", [(1.5 - i * 0.35, 0.05, 0.004, 1.5 - i * 0.175 - 0.02, 0.45 - i * 0.2, 0.222) for i in range(3)], M.chip())
    box("strip", 3.1, 0.07, 0.014, (1.75, -0.68, 0.21), M.glow(acc, STRIP), bevel=0)
    box("led", 0.14, 0.14, 0.04, (3.7, 0.72, 0.07), M.glow(acc, STRIP), bevel=0.01)
    ring("screw_pad", 0.28, 0.42, 0.07, 0.078, M.gold(), loc=(4.0, 0, 0), a0=radians(100), a1=radians(260), segs=32)


def build_network(acc):
    g = M.graphite()
    prism("body", rrect_pts(5.2, 3.2, 0.55, n=14), 0.0, 0.78, g, bevel=0.10, seg=5)
    prism("top", rrect_pts(4.5, 2.5, 0.34, n=14), 0.78, 0.80, M.gloss(), bevel=0.008)
    gl = M.glow(acc, STRIP)
    for i, r in enumerate((0.52, 0.92, 1.32)):
        ring("arc%d" % i, r - 0.075, r + 0.075, 0.80, 0.818, gl, loc=(0, -0.72, 0),
             a0=radians(45), a1=radians(135), segs=48)
    cyl("dot", 0.14, 0.018, (0, -0.72, 0.80), gl, bevel=0, segs=32)
    leds = [(0.16, 0.03, 0.05, -1.5 + i * 0.36, -1.6, 0.34) for i in range(5)]
    multi_box("leds", leds, gl)
    box("ledbar", 1.2, 0.03, 0.05, (1.2, -1.6, 0.34), M.chip(), bevel=0)
    for i, (x, tilt) in enumerate(((-2.05, 13), (0.0, 0), (2.05, -13))):
        cyl("ant_base%d" % i, 0.19, 0.42, (x, 1.42, 0.5), M.graphite_dark(), bevel=0.04, segs=32)
        rot = (radians(-9), radians(-tilt), 0)
        prism("ant%d" % i, rrect_pts(0.40, 0.17, 0.08, n=6), 0.0, 2.7, M.graphite_dark(), bevel=0.05,
              seg=4, loc=(x, 1.42, 0.86), rot=rot)
        prism("ant_glow%d" % i, rrect_pts(0.42, 0.19, 0.09, n=6), 2.28, 2.34, gl, bevel=0,
              loc=(x, 1.42, 0.86), rot=rot)
    multi_box("vents", [(0.5, 0.05, 0.02, -1.9 + (i % 2) * 3.3, 0.55 + (i // 2) * 0.16, 0.80)
                        for i in range(8)], M.chip())


def build_power(acc):
    g = M.graphite()
    body = box("body", 5.0, 4.8, 2.9, (0, 0, 0), g, defer=True)
    cut(body, [cyl("c", 1.95, 1.0, (0, 0.15, 2.35), defer=True, segs=128)])
    finish(body, 0.07, 4)
    cyl("well", 1.94, 0.02, (0, 0.15, 2.35), M.chip(), bevel=0, segs=96)
    rotor("fan", (0, 0.15, 2.39), 0.55, 1.86, 9, 0.36)
    gl = M.glow(acc, STRIP)
    ring("fanglow", 1.95, 2.02, 2.90, 2.916, gl, loc=(0, 0.15, 0))
    for i, r in enumerate((0.62, 0.95, 1.28, 1.61, 1.93)):
        torus("grille%d" % i, r, 0.026, 2.905, M.steel(), loc=(0, 0.15, 0))
    multi_box("spokes", [(3.9, 0.07, 0.05, 0, 0.15, 2.88, radians(a)) for a in (45, 135)], M.steel())
    cyl("grille_cap", 0.42, 0.06, (0, 0.15, 2.88), M.graphite_dark(), bevel=0.02, segs=64)
    ring("cap_glow", 0.26, 0.31, 2.94, 2.95, gl, loc=(0, 0.15, 0), segs=64)

    # modular connector panel on the front (-y) face
    box("panel", 4.5, 0.04, 2.3, (0, -2.41, 0.3), M.graphite_dark(), bevel=0.012)
    socks, pins = [], []
    rows = ((1.9, (1.5, 0.62, 0.62, 0.62)), (1.2, (0.62, 0.62, 0.62, 0.62, 0.62)),
            (0.5, (0.86, 0.86, 0.62, 0.62)))
    for z, widths in rows:
        total = sum(widths) + 0.22 * (len(widths) - 1)
        x = -total / 2
        for wdt in widths:
            cx = x + wdt / 2
            socks.append((wdt, 0.12, 0.40, cx, -2.47, z))
            n = max(2, int(round(wdt / 0.145)))
            for i in range(n):
                for zz in (0.07, 0.23):
                    pins.append((0.085, 0.03, 0.10, cx - wdt / 2 + (i + 0.5) * wdt / n, -2.53, z + zz))
            x += wdt + 0.22
    multi_box("sockets", socks, M.slot(), bevel=0.02)
    multi_box("pins", pins, M.chip())
    # accent strips on the visible side
    box("strip_side", 0.03, 4.3, 0.07, (-2.5, 0, 0.42), gl, bevel=0)
    box("strip_front", 4.5, 0.03, 0.05, (0, -2.43, 0.16), gl, bevel=0)
    slits = [(0.03, 0.55, 0.07, -2.5, -1.6 + i * 0.8, 1.2 + j * 0.3) for i in range(5) for j in range(4)]
    multi_box("side_vents", slits, M.chip(), bevel=0.01)


def build_fan(acc):
    g = M.graphite()
    fr = prism("frame", rrect_pts(4.8, 4.8, 0.5, n=14), 0.0, 1.0, g, defer=True)
    cutters = [cyl("c", 2.26, 1.4, (0, 0, -0.2), defer=True, segs=128)]
    for sx in (-1, 1):
        for sy in (-1, 1):
            cutters.append(cyl("s%d%d" % (sx, sy), 0.17, 1.4, (sx * 2.06, sy * 2.06, -0.2), defer=True, segs=32))
    cut(fr, cutters)
    finish(fr, 0.05, 4)
    gl = M.glow(acc, STRIP)
    ring("frame_glow", 2.26, 2.33, 1.0, 1.016, gl)
    ring("inner_glow", 2.235, 2.262, 0.42, 0.58, gl)
    multi_box("struts", [(4.5, 0.2, 0.12, 0, 0, 0.02, radians(a)) for a in (45, 135)], M.graphite_dark(), bevel=0.02)
    cyl("stator", 0.78, 0.16, (0, 0, 0.0), M.graphite_dark(), bevel=0.03, segs=64)
    rotor("fan", (0, 0, 0.2), 0.74, 2.16, 9, 0.72)
    ring("hub_plate", 0.0, 0.52, 0.92, 0.936, M.metal("x"), segs=64)
    ring("hub_glow", 0.56, 0.62, 0.92, 0.934, gl, segs=64)
    pads = [(0.34, 0.012, sx * 2.06, sy * 2.06, 1.0) for sx in (-1, 1) for sy in (-1, 1)]
    for i, (r, h, x, y, z) in enumerate(pads):
        ring("pad%d" % i, 0.17, r, z, z + h, M.plastic(), loc=(x, y, 0), segs=32)


def build_board(_acc):
    A = ACCENT
    prism("pcb", rrect_pts(12.2, 9.8, 0.12), 0.0, 0.09, M.pcb(), bevel=0.02)
    z = 0.09
    g, gd = M.graphite(), M.graphite_dark()
    # faint copper traces
    tr = []
    for i in range(16):
        tr.append((2.0, 0.018, 0.004, -0.6, 0.6 + i * 0.17, z))
        tr.append((0.018, 2.4, 0.004, -3.3 + i * 0.13, -0.6, z))
        tr.append((2.6, 0.018, 0.004, 3.9, 1.2 + i * 0.15, z))
    multi_box("traces", tr, mat("trace", lin("#27323c"), rough=0.35, coat=0.5))

    # rear I/O shroud + VRM heatsinks
    prism("io", [(-6.0, -0.6), (-4.55, -0.6), (-4.25, -0.2), (-4.25, 4.75), (-6.0, 4.75)], z, 1.25, g, bevel=0.07, seg=4)
    prism("io_glow", [(-5.75, 0.2), (-5.60, 0.2), (-4.75, 4.2), (-4.90, 4.2)], 1.25, 1.265, M.glow(A["network"], STRIP), bevel=0)
    prism("io_inlay", [(-5.35, -0.2), (-4.75, -0.2), (-4.55, 0.2), (-4.55, 2.6), (-4.78, 2.6)], 1.25, 1.262, M.metal("y"), bevel=0.005)
    box("vrm_top", 3.5, 0.95, 0.72, (-2.25, 4.2, z), g, bevel=0.05, seg=4)
    multi_box("vrm_fins", [(0.07, 0.99, 0.06, -3.85 + i * 0.2, 4.2, z + 0.72) for i in range(17)], gd, bevel=0.012)
    box("vrm_glow", 3.5, 0.03, 0.06, (-2.25, 3.725, z + 0.40), M.glow(A["cpu"], STRIP), bevel=0)
    box("vrm_left", 0.55, 3.0, 0.62, (-3.9, 1.95, z), g, bevel=0.05, seg=4)

    # CPU socket
    sx, sy = -2.25, 1.9
    fr = prism("socket", rrect_pts(2.5, 2.5, 0.1, cx=sx, cy=sy), z, z + 0.14, M.steel(), defer=True)
    cut(fr, [box("c", 1.95, 1.95, 0.6, (sx, sy, 0.0), defer=True)])
    finish(fr, 0.02, 2)
    box("cpu_sub", 1.9, 1.9, 0.06, (sx, sy, z), M.pcb(), bevel=0)
    prism("cpu_ihs", rrect_pts(1.55, 1.55, 0.16, cx=sx, cy=sy), z + 0.06, z + 0.22, M.metal("x"), bevel=0.04, seg=4)
    box("lever", 0.06, 2.3, 0.06, (sx + 1.36, sy - 0.1, z + 0.05), M.steel(), bevel=0.015)
    multi_cyl("caps", [(0.13, 0.36, sx - 1.2 + i * 0.34, 3.42, z) for i in range(8)]
              + [(0.13, 0.36, -3.38, 0.75 + i * 0.36, z) for i in range(6)], gd, bevel=0.02)
    multi_cyl("cap_tops", [(0.10, 0.012, sx - 1.2 + i * 0.34, 3.42, z + 0.36) for i in range(8)]
              + [(0.10, 0.012, -3.38, 0.75 + i * 0.36, z + 0.36) for i in range(6)], M.steel())
    multi_box("chokes", [(0.30, 0.30, 0.26, sx - 1.2 + i * 0.4, 0.12, z) for i in range(7)], M.slot(), bevel=0.03)

    # RAM slots + two lit sticks
    slots, latches = [], []
    for i, x in enumerate((0.55, 1.03, 1.51, 1.99)):
        slots.append((0.27, 5.5, 0.36, x, 1.7, z))
        latches += [(0.31, 0.22, 0.5, x, 4.5, z), (0.31, 0.22, 0.5, x, -1.1, z)]
    multi_box("ram_slots", slots, M.plastic(), bevel=0.02)
    multi_box("ram_latch", latches, M.slot(), bevel=0.02)
    for x in (1.03, 1.99):
        box("stick%.2f" % x, 0.19, 5.25, 1.22, (x, 1.7, z + 0.2), g, bevel=0.03)
        box("stick_bar%.2f" % x, 0.15, 5.0, 0.17, (x, 1.7, z + 1.42), M.glow(A["ram"], BAR), bevel=0.03)

    # 24-pin + EPS power
    box("atx24", 0.52, 2.5, 0.5, (5.55, 2.2, z), M.plastic(), bevel=0.03)
    multi_box("atx24_pins", [(0.14, 0.14, 0.02, 5.44 + j * 0.22, 1.1 + i * 0.2, z + 0.5) for i in range(12) for j in range(2)], M.chip())
    box("eps", 1.0, 0.5, 0.5, (-0.2, 4.45, z), M.plastic(), bevel=0.03)
    multi_box("dbg", [(0.10, 0.10, 0.05, 4.7, 3.6 + i * 0.26, z) for i in range(4)], M.glow(A["power"], STRIP))

    # PCIe slots, M.2 heatsinks, chipset
    for i, y in enumerate((-0.95, -3.5)):
        box("pcie%d" % i, 5.3, 0.34, 0.42, (-1.7, y, z), M.steel(), bevel=0.03)
        box("pcie_slit%d" % i, 5.0, 0.10, 0.02, (-1.7, y, z + 0.42), M.chip(), bevel=0)
        box("pcie_latch%d" % i, 0.3, 0.42, 0.5, (1.1, y, z), M.slot(), bevel=0.03)
    box("pcie_x1", 1.3, 0.3, 0.4, (-3.7, -2.25, z), M.plastic(), bevel=0.03)
    box("pcie_glow", 5.3, 0.03, 0.05, (-1.7, -1.135, z + 0.2), M.glow(A["gpu"], STRIP), bevel=0)
    prism("m2", rrect_pts(3.9, 0.98, 0.12, cx=-0.75, cy=-2.25), z, z + 0.26, g, bevel=0.04, seg=4)
    box("m2_glow", 3.3, 0.06, 0.012, (-0.75, -2.55, z + 0.26), M.glow(A["ssd"], STRIP), bevel=0)
    multi_box("m2_grooves", [(0.05, 0.6, 0.012, -2.2 + i * 0.22, -2.15, z + 0.26) for i in range(8)], gd)
    prism("chipset", [(2.35, -3.9), (4.75, -3.9), (5.05, -3.6), (5.05, -1.25), (2.65, -1.25), (2.35, -1.55)],
          z, z + 0.42, g, bevel=0.05, seg=4)
    prism("chip_glow", [(2.75, -3.5), (2.92, -3.5), (4.65, -1.65), (4.48, -1.65)], z + 0.42, z + 0.434,
          M.glow(A["power"], STRIP), bevel=0)
    prism("chip_inlay", [(3.25, -3.5), (4.65, -3.5), (4.65, -2.0)], z + 0.42, z + 0.432, M.graphite_lite(), bevel=0.004)

    # edge connectors, battery, audio
    multi_box("sata", [(0.62, 1.05, 0.5, 5.6, -1.9, z), (0.62, 1.05, 0.5, 5.6, -3.15, z)], M.plastic(), bevel=0.03)
    hdr = [(0.6, 0.2, 0.26, -5.2 + i * 0.95, -4.6, z) for i in range(11) if i not in (3,)]
    multi_box("headers", hdr, M.plastic(), bevel=0.02)
    multi_box("hdr_pins", [(0.5, 0.1, 0.02, x, y, zz + 0.26) for (_, _, _, x, y, zz) in hdr], M.gold())
    cyl("battery", 0.40, 0.09, (1.9, -2.25, z), M.steel(), bevel=0.02, segs=48)
    box("audio", 0.9, 1.3, 0.5, (-5.5, -3.4, z), g, bevel=0.04)
    multi_cyl("audio_caps", [(0.11, 0.30, -4.75 + (i % 2) * 0.3, -3.9 + (i // 2) * 0.32, z) for i in range(6)], M.gold(), bevel=0.02)
    multi_box("io_ports", [(0.5, 0.7, 0.6, -5.85, -1.3, z), (0.5, 0.6, 0.45, -5.85, -2.2, z)], M.steel(), bevel=0.03)
    holes = [(-5.8, 4.6), (5.8, 4.6), (-5.8, -4.6), (5.8, -4.6), (0.1, -4.2), (3.0, 4.6)]
    for i, (x, y) in enumerate(holes):
        ring("hole%d" % i, 0.09, 0.19, z, z + 0.006, M.steel(), loc=(x, y, 0), segs=24)


def arc_band_pts(r, w, a0, a1, n=120, cap_n=14):
    """Outline of a thick arc with round end caps."""
    ri, ro = r - w / 2, r + w / 2
    pts = [(ro * cos(a0 + (a1 - a0) * i / n), ro * sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]
    c = (r * cos(a1), r * sin(a1))
    pts += [(c[0] + w / 2 * cos(a1 + pi * k / cap_n), c[1] + w / 2 * sin(a1 + pi * k / cap_n))
            for k in range(1, cap_n)]
    pts += [(ri * cos(a1 + (a0 - a1) * i / n), ri * sin(a1 + (a0 - a1) * i / n)) for i in range(n + 1)]
    c = (r * cos(a0), r * sin(a0))
    pts += [(c[0] + w / 2 * cos(a0 + pi + pi * k / cap_n), c[1] + w / 2 * sin(a0 + pi + pi * k / cap_n))
            for k in range(1, cap_n)]
    return pts


def _icon_common(ring_w, ring_r, pulse_r, pulse_pts, tile=True, strength=1.35):
    gl = M.glow_grad(ICON_GRAD[0], ICON_GRAD[1], strength, (1.0, 0.6, 0.0), 1.7)
    z0 = 0.0
    if tile:
        prism("tile", rrect_pts(4.0, 4.0, 1.0, n=24), 0.0, 0.42, M.gloss(), bevel=0.14, seg=8)
        prism("tile_in", rrect_pts(3.56, 3.56, 0.80, n=24), 0.42, 0.435,
              mat("tile_in", lin("#161a23"), rough=0.3, coat=1.0, coat_rough=0.22), bevel=0.01)
        z0 = 0.435
    if ring_w:
        prism("gauge", arc_band_pts(ring_r, ring_w, radians(-52), radians(232)), z0, z0 + 0.15, gl,
              bevel=0.045, seg=4)
    pts = [(x, y, z0 + pulse_r * 0.9) for x, y in fillet(pulse_pts, pulse_r * 0.7, n=6)]
    tube("pulse", pts, pulse_r, gl)


def build_icon(_acc):
    _icon_common(0.36, 1.20, 0.105,
                 [(-1.66, -0.06), (-0.62, -0.06), (-0.36, 0.52), (0.02, -0.78), (0.34, 0.30),
                  (0.52, -0.06), (1.66, -0.06)])


def build_icon_small(_acc):
    # 24-32 px: fat ring, one clean spike kept inside the ring
    _icon_common(0.50, 1.22, 0.17,
                 [(-0.74, -0.05), (-0.44, -0.05), (-0.17, 0.52), (0.17, -0.62), (0.42, -0.05), (0.74, -0.05)])


def build_icon_tiny(_acc):
    # 16 px: the tile and a bold pulse only
    _icon_common(0.0, 0.0, 0.25,
                 [(-1.40, -0.08), (-0.78, -0.08), (-0.30, 0.92), (0.30, -1.00), (0.78, -0.08), (1.40, -0.08)])


def build_icon_glyph(_acc):
    # no tile: the bare emblem for in-app use (sidebar logo, about box)
    _icon_common(0.46, 1.50, 0.15,
                 [(-2.06, -0.07), (-0.78, -0.07), (-0.45, 0.66), (0.03, -0.98), (0.43, 0.38),
                  (0.66, -0.07), (2.06, -0.07)], tile=False)


# ───────────────────────────── studio ─────────────────────────────

def _dir(az, el):
    az, el = radians(az), radians(el)
    return Vector((cos(el) * sin(az), -cos(el) * cos(az), sin(el)))


def _aim(ob, d):
    ob.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()


def _area(name, center, R, az, el, dist, size, power, color=(1, 1, 1)):
    ld = bpy.data.lights.new(name, "AREA")
    ld.shape = "DISK"
    ld.size = size * R
    ld.energy = power * R * R * LIGHT_GAIN
    ld.color = color
    ob = bpy.data.objects.new(name, ld)
    bpy.context.scene.collection.objects.link(ob)
    d = _dir(az, el)
    ob.location = center + d * dist * R
    _aim(ob, d)
    try:
        ob.visible_camera = False
    except Exception:
        pass
    return ob


def studio(accent_hex, elev=36.0, azim=-32.0, lens=80.0, fill=0.86, catcher=True,
           key=1.0, rim=1.0, top=1.0, topdown=False):
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    coords = []
    mn, mx = Vector((1e9,) * 3), Vector((-1e9,) * 3)
    for ob in PARTS:
        if ob.type == "CURVE":      # curve bound boxes are unreliable before evaluation
            rad = ob.data.bevel_depth
            pts = [ob.matrix_world @ (Vector(p.co[:3]) + Vector((sx * rad, sy * rad, sz * rad)))
                   for sp in ob.data.splines for p in sp.points
                   for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
        else:
            ev = ob.evaluated_get(dg)
            pts = [ev.matrix_world @ Vector(c) for c in ev.bound_box]
        for w in pts:
            coords.extend(w)
            for i in range(3):
                mn[i], mx[i] = min(mn[i], w[i]), max(mx[i], w[i])
    center = (mn + mx) / 2
    R = (mx - mn).length / 2

    cd = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cd)
    scene.collection.objects.link(cam)
    scene.camera = cam
    d = _dir(azim, elev)
    cam.location = center + d * R * 8
    _aim(cam, d)
    cd.clip_start, cd.clip_end = 0.05, 2000
    cd.lens = lens / fill
    bpy.context.view_layer.update()
    loc, _scale = cam.camera_fit_coords(dg, coords)
    cam.location = loc
    cd.lens = lens

    acc = lin(accent_hex)
    tint = mix((1, 1, 1), vivid(acc), 0.38)
    if topdown:   # icon: light in screen space around a top-down camera
        _area("key", center, R, azim - 40, 46, 4.0, 3.2, 420 * key)
        _area("fill", center, R, azim + 120, 36, 4.5, 3.0, 110)
        _area("rim", center, R, azim + 180, 28, 4.0, 2.0, 380 * rim, tint)
        _area("gloss", center, R, azim + 180, 64, 3.4, 2.6, 60)
    else:
        _area("key", center, R, azim - 52, 54, 4.2, 3.4, 620 * key)
        _area("fill", center, R, azim + 62, 22, 4.6, 3.2, 180)
        _area("rim", center, R, azim + 158, 32, 4.0, 2.2, 900 * rim, tint)
        _area("kick", center, R, azim - 142, 18, 4.2, 1.8, 460 * rim, mix((1, 1, 1), tint, 0.5))
        _area("top", center, R, azim, 88, 3.6, 3.6, 160 * top)

    if catcher:
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=R * 40)
        me = bpy.data.meshes.new("Ground")
        bm.to_mesh(me)
        bm.free()
        gnd = bpy.data.objects.new("Ground", me)
        gnd.location = (center.x, center.y, mn.z - 0.0005)
        scene.collection.objects.link(gnd)
        gnd.is_shadow_catcher = True

    w = bpy.data.worlds.new("World")
    try:
        w.use_nodes = True
    except Exception:
        pass
    bg = next((n for n in w.node_tree.nodes if n.bl_idname == "ShaderNodeBackground"), None)
    if bg:
        bg.inputs[0].default_value = (0.045, 0.05, 0.06, 1.0)
        bg.inputs[1].default_value = 1.0
    scene.world = w
    return center, R


def setup_render(size, samples, use_gpu=True):
    scene = bpy.context.scene
    r = scene.render
    r.engine = "CYCLES"
    r.resolution_x, r.resolution_y = size
    r.resolution_percentage = 100
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    r.image_settings.color_depth = "8"
    cy = scene.cycles
    cy.samples = samples
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = 0.01
    cy.use_denoising = True
    try:
        cy.denoiser = "OPENIMAGEDENOISE"
    except Exception as e:
        print("denoiser:", e)
    cy.sample_clamp_indirect = 6.0
    cy.max_bounces = 6
    cy.caustics_reflective = False
    cy.caustics_refractive = False
    dev = "CPU"
    if use_gpu:
        try:
            p = bpy.context.preferences.addons["cycles"].preferences
            for t in ("HIP", "OPTIX", "CUDA", "ONEAPI", "METAL"):
                try:
                    p.compute_device_type = t
                except Exception:
                    continue
                try:
                    p.refresh_devices()
                except Exception:
                    p.get_devices()
                gpus = [d for d in p.devices if d.type == t]
                if gpus:
                    for d in p.devices:
                        d.use = d.type == t
                    cy.device = "GPU"
                    dev = "%s (%s)" % (t, gpus[0].name)
                    break
        except Exception as e:
            print("gpu setup failed:", e)
    if dev == "CPU":
        cy.device = "CPU"
    vs = scene.view_settings
    for vt in ("AgX", "Filmic", "Standard"):
        try:
            vs.view_transform = vt
            break
        except Exception:
            continue
    for look in ("AgX - Medium High Contrast", "Medium High Contrast", "None"):
        try:
            vs.look = look
            break
        except Exception:
            continue
    vs.exposure = 0.0
    vs.gamma = 1.0
    return dev


def render_glow_pass(path):
    """Second render with only the emissive parts lit - export_assets.py turns it into bloom."""
    scene = bpy.context.scene
    black, _nt, b = _principled("glowpass_black")
    _set(b, **{"Base Color": (0, 0, 0, 1), "Roughness": 1.0, "Specular IOR Level": 0.0})
    for ob in scene.objects:
        if ob.type == "LIGHT":
            ob.hide_render = True
        elif ob.type in ("MESH", "CURVE"):
            if ob.name == "Ground":
                ob.hide_render = True
                continue
            mats = ob.data.materials
            for i, m in enumerate(mats):
                if m is None or not m.name.startswith(("glow_", "grad_")):
                    mats[i] = black
    # emitters at unit strength under the Standard transform -> exact, saturated accent colours
    for m in bpy.data.materials:
        if m.name.startswith(("glow_", "grad_")):
            pb = next((n for n in m.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
            if pb:
                big = abs(pb.inputs["Emission Strength"].default_value - BAR) < 1e-4
                pb.inputs["Emission Strength"].default_value = 0.80 if big else 1.0
    bg = next((n for n in scene.world.node_tree.nodes if n.bl_idname == "ShaderNodeBackground"), None)
    if bg:
        bg.inputs[1].default_value = 0.0
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except Exception as e:
        print("glow pass view transform:", e)
    scene.cycles.samples = 24
    scene.cycles.max_bounces = 0
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def reset():
    PARTS.clear()
    _MATS.clear()
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.curves, bpy.data.materials,
                 bpy.data.lights, bpy.data.cameras, bpy.data.worlds):
        for b in list(coll):
            coll.remove(b)


# name: (builder, accent, kwargs for studio(), (w, h) as a fraction of --size; board = 1600x1000 at 1024)
ASSETS = {
    "cpu": (build_cpu, ACCENT["cpu"], dict(elev=40, azim=-34, fill=0.84), (1, 1)),
    "gpu": (build_gpu, ACCENT["gpu"], dict(elev=38, azim=-30), (1, 1)),
    "ram": (build_ram, ACCENT["ram"], dict(elev=30, azim=-36), (1, 1)),
    "ssd": (build_ssd, ACCENT["ssd"], dict(elev=50, azim=-30), (1, 1)),
    "network": (build_network, ACCENT["network"], dict(elev=30, azim=-30), (1, 1)),
    "power": (build_power, ACCENT["power"], dict(elev=34, azim=-36), (1, 1)),
    "fan": (build_fan, ACCENT["fan"], dict(elev=44, azim=-30), (1, 1)),
    "board": (build_board, ACCENT["cpu"], dict(elev=44, azim=-24, fill=0.90, top=0.45), (1.5625, 0.9765625)),
    "icon": (build_icon, ICON_GRAD[1], dict(elev=84, azim=0, fill=0.94, catcher=False, topdown=True), (1, 1)),
    "icon_small": (build_icon_small, ICON_GRAD[1], dict(elev=89, azim=0, fill=0.94, catcher=False, topdown=True), (1, 1)),
    "icon_tiny": (build_icon_tiny, ICON_GRAD[1], dict(elev=89, azim=0, fill=0.94, catcher=False, topdown=True), (1, 1)),
    "icon_glyph": (build_icon_glyph, ICON_GRAD[1], dict(elev=89, azim=0, fill=0.94, catcher=False, topdown=True), (1, 1)),
}


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    only, size, samples, use_gpu, save_blend = None, 1024, 128, True, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--only":
            only = [s.strip() for s in argv[i + 1].split(",") if s.strip()]
            i += 1
        elif a == "--size":
            size = int(argv[i + 1])
            i += 1
        elif a == "--samples":
            samples = int(argv[i + 1])
            i += 1
        elif a == "--cpu":
            use_gpu = False
        elif a == "--blend":
            save_blend = True
        i += 1
    os.makedirs(OUT, exist_ok=True)
    names = only or list(ASSETS)
    for name in names:
        if name not in ASSETS:
            print("unknown asset:", name)
            continue
        t0 = time.time()
        builder, accent, kw, aspect = ASSETS[name]
        reset()
        builder(accent)
        px = (int(round(size * aspect[0] / 2) * 2), int(round(size * aspect[1] / 2) * 2))
        dev = setup_render(px, samples, use_gpu)      # resolution must be set before framing
        studio(accent, **kw)
        path = os.path.join(OUT, name + ".png")
        bpy.context.scene.render.filepath = path
        if save_blend:
            bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, name + ".blend"))
        bpy.ops.render.render(write_still=True)
        render_glow_pass(os.path.join(OUT, name + "_glow.png"))
        print("ASSET %s -> %s  %dx%d  %s  %.1fs" % (name, path, px[0], px[1], dev, time.time() - t0))


if __name__ == "__main__":
    main()
