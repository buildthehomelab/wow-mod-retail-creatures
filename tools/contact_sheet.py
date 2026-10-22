#!/usr/bin/env python3
"""Review page for a census: every mount and pet look that needs a converted model, drawn from
the converted model, next to a reference picture.

    RETAIL_SOURCE=cdn python3 contact_sheet.py --cache <download dir> --client <dir> \\
        --census <census out dir> --out <page dir> [--families families.tsv] [--only 124511,...]

Each look is converted the way build_patch.py would (geosets baked in, textures pruned), and
the 3.3.5a model that comes out is what gets drawn: posed on the first frame of Stand, with the
look's textures, by a small software renderer. So a tile shows what the converter kept, not
what retail has. It can't show what only a client shows: particles are gone either way, and
animation past the first frame, the rider's seat and sounds are not looked at.

Writes <out>/img/<retail display>.jpg, <out>/drawn.json (what was drawn; --page-only rebuilds
the page from it without drawing again) and <out>/index.html. The reference picture of a look is
Blizzard's own render of that retail display (render.worldofwarcraft.com, linked, not copied;
which displays have one is asked once and kept in <out>/renders.json). The page groups mounts
by the families in --families (what mount_families.py writes: the families of
warcraftmounts.com, whose thumbnail stands in where Blizzard has no render); pets go by pet
type. Clicking a tile flags it; the flagged list can be copied out.

Needs numpy and Pillow (the other tools here are standard library only).
"""
import argparse
import collections
import concurrent.futures
import html
import json
import multiprocessing
import os
import struct
import sys
import urllib.error
import urllib.request

import numpy as np
from PIL import Image

from build_patch import MASK_WORDS
from census import read_dbc_ids
from fetch import casc, customization_textures, db2_rows, listfile
from m2creature import ARRAYS, Converter, Retail

PET_TYPES = ("Humanoid", "Dragonkin", "Flying", "Undead", "Critter", "Magic", "Elemental", "Beast", "Aquatic",
             "Mechanical")
SITE = "https://www.warcraftmounts.com"
RENDER = "https://render.worldofwarcraft.com/us/npcs/zoom/creature-display-%s.jpg"
BACKGROUND = (58, 62, 70)


# --- textures ----------------------------------------------------------------------------------

def dxt_colors(blocks, opaque):
    """Colour part of DXT blocks (n, 8 bytes) -> (n, 4, 4, 4) RGBA."""
    c = blocks[:, :4].copy().view("<u2").astype(np.int32)  # (n, 2)
    rgb = np.stack(((c >> 11) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31), -1)  # (n, 2, 3)
    c0, c1 = rgb[:, 0], rgb[:, 1]
    four = (c[:, 0] > c[:, 1]) | opaque
    table = np.zeros((len(blocks), 4, 4), np.int32)
    table[:, 0, :3], table[:, 1, :3] = c0, c1
    table[:, 2, :3] = np.where(four[:, None], (2 * c0 + c1) // 3, (c0 + c1) // 2)
    table[:, 3, :3] = np.where(four[:, None], (c0 + 2 * c1) // 3, 0)
    table[:, :, 3] = 255
    table[:, 3, 3] = np.where(four, 255, 0)
    bits = blocks[:, 4:8].copy().view("<u4")[:, 0]
    idx = (bits[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3
    return table[np.arange(len(blocks))[:, None], idx].reshape(-1, 4, 4, 4)


def blp_image(data, target=256):
    """A BLP2 texture as (h, w, 4) uint8 RGBA, from the mip level nearest to target pixels."""
    if data[:4] != b"BLP2":
        raise ValueError("not a BLP2 texture")
    _type, enc, alpha_depth, alpha_enc, _mips = struct.unpack_from("<I4B", data, 4)
    w, h = struct.unpack_from("<II", data, 12)
    offs, sizes = struct.unpack_from("<16I", data, 20), struct.unpack_from("<16I", data, 84)
    level = 0
    while max(w >> level, h >> level) > target and level + 1 < 16 and sizes[level + 1]:
        level += 1
    w, h = max(1, w >> level), max(1, h >> level)
    raw = np.frombuffer(data, np.uint8, sizes[level], offs[level])
    if enc == 2:
        bw, bh = (w + 3) // 4, (h + 3) // 4
        size = 8 if alpha_enc == 0 else 16
        blocks = raw[:bw * bh * size].reshape(-1, size)
        px = dxt_colors(blocks[:, size - 8:], alpha_enc != 0 or alpha_depth == 0)
        if alpha_enc == 1:  # DXT3: 4 bits of alpha per pixel
            bits = blocks[:, :8].copy().view("<u8")[:, 0]
            a = ((bits[:, None] >> (4 * np.arange(16, dtype=np.uint64))) & 15).astype(np.int32) * 17
            px[..., 3] = a.reshape(-1, 4, 4)
        elif alpha_enc == 7:  # DXT5: two alphas and 3-bit steps between them
            a0, a1 = blocks[:, 0].astype(np.int32), blocks[:, 1].astype(np.int32)
            bits = np.zeros(len(blocks), np.uint64)
            for k in range(6):
                bits |= blocks[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
            idx = ((bits[:, None] >> (3 * np.arange(16, dtype=np.uint64))) & 7).astype(np.int32)
            eight = (a0 > a1)[:, None]
            k = idx.clip(2, 7)
            ramp = np.where(eight, ((8 - k) * a0[:, None] + (k - 1) * a1[:, None]) // 7,
                            ((6 - k) * a0[:, None] + (k - 1) * a1[:, None]) // 5)
            ramp = np.where(~eight & (idx == 6), 0, np.where(~eight & (idx == 7), 255, ramp))
            px[..., 3] = np.where(idx == 0, a0[:, None], np.where(idx == 1, a1[:, None], ramp)).reshape(-1, 4, 4)
        img = px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)[:h, :w]
        return img.astype(np.uint8)
    if enc == 1:
        palette = np.frombuffer(data, np.uint8, 1024, 148).reshape(256, 4)
        img = np.empty((h, w, 4), np.uint8)
        img[..., :3] = palette[raw[:w * h], 2::-1].reshape(h, w, 3)
        rest = raw[w * h:]
        if alpha_depth == 8:
            img[..., 3] = rest[:w * h].reshape(h, w)
        elif alpha_depth == 4:
            a = np.stack((rest & 15, rest >> 4), -1).reshape(-1)[:w * h]
            img[..., 3] = (a * 17).reshape(h, w)
        elif alpha_depth == 1:
            img[..., 3] = (np.unpackbits(rest, bitorder="little")[:w * h] * 255).reshape(h, w)
        else:
            img[..., 3] = 255
        return img
    if enc == 3:
        img = raw[:w * h * 4].reshape(h, w, 4)[..., [2, 1, 0, 3]].copy()
        if alpha_depth == 0:
            img[..., 3] = 255
        return img
    raise ValueError("BLP encoding %d" % enc)


def sample(tex, u, v, wrap):
    h, w = tex.shape[:2]
    x, y = np.floor(u * w).astype(np.int64), np.floor(v * h).astype(np.int64)
    x = x % w if wrap & 1 else x.clip(0, w - 1)
    y = y % h if wrap & 2 else y.clip(0, h - 1)
    return tex[y, x].astype(np.float32) / 255.0


# --- the converted model -----------------------------------------------------------------------

def quaternion(q):
    x, y, z, w = [(v + 32768 if v < 0 else v - 32767) / 32767.0 for v in q]
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


class Model:
    """A 3.3.5a model and its skin, posed on the first frame of an animation."""

    def __init__(self, m2, skin, anim=0):
        self.md = m2
        o, h = 8, {}
        for name, kind in ARRAYS:
            if kind == "a":
                h[name] = struct.unpack_from("<II", m2, o); o += 8
            else:
                o += 4 if kind == "u" else struct.calcsize("<" + kind)
        self.h = h
        seqs = self.arr("sequences", "<HH60x")
        self.seq = next((i for i, s in enumerate(seqs) if s[0] == anim), 0)
        v = np.frombuffer(m2, np.dtype([("pos", "<f4", 3), ("w", "u1", 4), ("b", "u1", 4), ("n", "<f4", 3),
                                        ("uv", "<f4", 2), ("uv2", "<f4", 2)]), h["vertices"][0], h["vertices"][1])
        mats = self.bone_matrices()
        if len(mats):
            w = v["w"].astype(np.float32) / 255.0
            blend = (mats[v["b"].clip(0, len(mats) - 1)] * w[:, :, None, None]).sum(1)  # (n, 4, 4)
            blend[w.sum(1) == 0] = np.eye(4)
            self.pos = np.einsum("nij,nj->ni", blend[:, :3, :3], v["pos"]) + blend[:, :3, 3]
            self.normal = np.einsum("nij,nj->ni", blend[:, :3, :3], v["n"])
        else:
            self.pos, self.normal = v["pos"].astype(np.float64), v["n"].astype(np.float64)
        self.uv = v["uv"]
        a = [struct.unpack_from("<II", skin, 4 + i * 8) for i in range(5)]
        vmap = np.frombuffer(skin, "<u2", a[0][0], a[0][1])
        idx = np.frombuffer(skin, "<u2", a[1][0], a[1][1])
        subs = [struct.unpack_from("<HHHHHHHHHH3f3ff", skin, a[3][1] + i * 48) for i in range(a[3][0])]
        self.textures = self.arr("textures", "<IIII")
        tex_lookup = [x[0] for x in self.arr("texture_lookup", "<h")]
        weight_lookup = [x[0] for x in self.arr("weight_lookup", "<h")]
        materials = self.arr("materials", "<HH")
        self.batches = []  # (triangles as vertex indices, texture, blend, two sided, alpha, colour)
        for i in range(a[4][0]):
            (_flags, _prio, _shader, sub, _sub2, color, mat, _layer, _count, tex_combo, _uv, weight_combo,
             _transform) = struct.unpack_from("<BbHHHhHHHHHHH", skin, a[4][1] + i * 24)
            s = subs[sub]
            tris = vmap[idx[s[4]:s[4] + s[5]]].reshape(-1, 3)
            alpha, rgb = 1.0, (1.0, 1.0, 1.0)
            if weight_combo < len(weight_lookup) and 0 <= weight_lookup[weight_combo] < h["texture_weights"][0]:
                key = self.key(h["texture_weights"][1] + weight_lookup[weight_combo] * 20, 2)
                if key:
                    alpha *= struct.unpack("<h", key)[0] / 32767.0
            if 0 <= color < h["colors"][0]:
                key = self.key(h["colors"][1] + color * 40, 12)
                if key:
                    rgb = struct.unpack("<3f", key)
                key = self.key(h["colors"][1] + color * 40 + 20, 2)
                if key:
                    alpha *= struct.unpack("<h", key)[0] / 32767.0
            flags, blend = materials[mat] if mat < len(materials) else (0, 0)
            self.batches.append((tris, tex_lookup[tex_combo] if tex_combo < len(tex_lookup) else -1, blend,
                                 bool(flags & 4), alpha, rgb, bool(flags & 1)))

    def arr(self, name, fmt):
        n, off = self.h[name]
        size = struct.calcsize(fmt)
        return [struct.unpack_from(fmt, self.md, off + i * size) for i in range(n)]

    def key(self, track, size):
        """First key of a track in the posed animation, or None."""
        _interp, gseq, tn, to, vn, vo = struct.unpack_from("<HhIIII", self.md, track)
        i = 0 if gseq != -1 else self.seq
        if i >= tn or i >= vn:
            return None
        n, off = struct.unpack_from("<II", self.md, vo + 8 * i)
        return self.md[off:off + size] if n else None

    def bone_matrices(self):
        n, off = self.h["bones"]
        local, parent = [], []
        for i in range(n):
            o = off + i * 88
            parent.append(struct.unpack_from("<h", self.md, o + 8)[0])
            pivot = np.array(struct.unpack_from("<3f", self.md, o + 76))
            m = np.eye(4)
            m[:3, 3] = pivot
            t, r, s = self.key(o + 16, 12), self.key(o + 36, 8), self.key(o + 56, 12)
            if t:
                m[:3, 3] += struct.unpack("<3f", t)
            if r:
                m[:3, :3] = m[:3, :3] @ quaternion(struct.unpack("<4h", r))
            if s:
                m[:3, :3] = m[:3, :3] * np.array(struct.unpack("<3f", s))[None, :]
            m[:3, 3] -= m[:3, :3] @ pivot
            local.append(m)
        world = [None] * n

        def solve(i, depth=0):
            if world[i] is None:
                p = parent[i]
                world[i] = local[i] if p < 0 or p >= n or depth > 256 else solve(p, depth + 1) @ local[i]
            return world[i]

        return np.array([solve(i) for i in range(n)]) if n else np.zeros((0, 4, 4))


# --- drawing -----------------------------------------------------------------------------------

LIGHT = np.array([0.35, 0.45, 0.82]) / np.linalg.norm([0.35, 0.45, 0.82])


def camera(azimuth, elevation):
    """(towards the camera, screen right, screen up) for a camera looking at the origin."""
    az, el = np.radians(azimuth), np.radians(elevation)
    cam = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    right = np.cross(-cam, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    return cam, right, np.cross(right, -cam)


def drawn_batches(model, textures):
    drawn = [b for b in model.batches if b[1] in textures and b[4] > 0.02 and len(b[0])]
    if not drawn:
        raise ValueError("nothing to draw")
    return drawn, np.unique(np.concatenate([b[0].reshape(-1) for b in drawn]))


def draw(model, textures, size=256, scale=2, azimuth=40.0, elevation=12.0):
    """textures: {texture index: (RGBA array, wrap flags)}. Returns (size, size, 3) uint8."""
    px = size * scale
    cam, right, up = camera(azimuth, elevation)
    _drawn, used = drawn_batches(model, textures)
    sx, sy = model.pos @ right, -(model.pos @ up)
    lo = np.array([sx[used].min(), sy[used].min()])
    hi = np.array([sx[used].max(), sy[used].max()])
    background = np.array(BACKGROUND, np.float32)[None, None] / 255.0 * np.linspace(1.12, 0.82, px)[:, None, None]
    image, _covered = draw_raw(model, textures, px, azimuth, elevation, (lo + hi) / 2,
                               max((hi - lo).max(), 1e-6) * 1.08, LIGHT, background)
    image = (image.clip(0, 1) * 255).astype(np.uint8)
    return image.reshape(size, scale, size, scale, 3).mean((1, 3)).astype(np.uint8)


def draw_raw(model, textures, px, azimuth, elevation, mid, span, light, background):
    """Draws onto a copy of background ((px, px, 3) floats, 0-1) with the screen point mid (in
    model units, right and down) at the centre and span model units across. light is in world
    space. Returns (image, the pixels a solid batch covered)."""
    cam, right, up = camera(azimuth, elevation)
    drawn, _used = drawn_batches(model, textures)
    sx, sy, depth = model.pos @ right, -(model.pos @ up), model.pos @ cam
    X = (sx - mid[0]) / span * px + px / 2
    Y = (sy - mid[1]) / span * px + px / 2
    nrm = model.normal / np.maximum(np.linalg.norm(model.normal, axis=1, keepdims=True), 1e-6)
    shade = 0.5 + 0.5 * np.clip(nrm @ light, 0, 1) + 0.12 * np.clip(nrm @ cam, 0, 1)
    U, V = model.uv[:, 0], model.uv[:, 1]

    image = np.empty((px, px, 3), np.float32)
    image[:] = background
    zbuf = np.full((px, px), -np.inf)
    gx = np.arange(px) + 0.5

    def triangles(batch, solid):
        tris, tex_index, blend, _two, alpha, rgb, unlit = batch
        tex, wrap = textures[tex_index]
        tint = np.array(rgb, np.float32)
        for a, b, c in tris:
            x0, y0, x1, y1, x2, y2 = X[a], Y[a], X[b], Y[b], X[c], Y[c]
            area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
            if abs(area) < 1e-9:
                continue
            xa, xb = max(int(min(x0, x1, x2)), 0), min(int(max(x0, x1, x2)) + 1, px)
            ya, yb = max(int(min(y0, y1, y2)), 0), min(int(max(y0, y1, y2)) + 1, px)
            if xa >= xb or ya >= yb:
                continue
            xs, ys = gx[xa:xb][None, :], gx[ya:yb][:, None]
            w0 = ((x1 - xs) * (y2 - ys) - (x2 - xs) * (y1 - ys)) / area
            w1 = ((x2 - xs) * (y0 - ys) - (x0 - xs) * (y2 - ys)) / area
            w2 = 1.0 - w0 - w1
            z = w0 * depth[a] + w1 * depth[b] + w2 * depth[c]
            zs = zbuf[ya:yb, xa:xb]
            mask = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & (z > zs)
            if not mask.any():
                continue
            w0, w1, w2 = w0[mask], w1[mask], w2[mask]
            t = sample(tex, w0 * U[a] + w1 * U[b] + w2 * U[c], w0 * V[a] + w1 * V[b] + w2 * V[c], wrap)
            lit = 1.0 if unlit else (w0 * shade[a] + w1 * shade[b] + w2 * shade[c])[:, None]
            src = t[:, :3] * tint * lit
            out = image[ya:yb, xa:xb]
            if solid:
                if blend == 1:
                    keep = t[:, 3] > 0.5
                    full = np.zeros_like(mask)
                    full[mask] = keep
                    mask, src = full, src[keep]
                    if not keep.any():
                        continue
                    zs[mask] = z[mask]
                else:
                    zs[mask] = z[mask]
                out[mask] = src
            else:
                al = (t[:, 3] * alpha)[:, None]
                dst = out[mask]
                if blend == 2:
                    out[mask] = src * al + dst * (1 - al)
                elif blend == 3:
                    out[mask] = dst + src * alpha
                elif blend == 4:
                    out[mask] = dst + src * al
                elif blend == 5:
                    out[mask] = dst * src
                else:
                    out[mask] = dst * src * 2

    for batch in drawn:
        if batch[2] <= 1:
            triangles(batch, True)
    covered = np.isfinite(zbuf)
    for batch in drawn:
        if batch[2] > 1:
            triangles(batch, False)
    return image, covered


# --- the census's looks ------------------------------------------------------------------------

def convert_variant(cache, valid, fdid, geosets, stem, chosen=()):
    """Converts one retail model with one geoset choice the way build_patch.py does (chosen:
    the display's customization textures, as (texture type, file id) pairs). Returns the
    posed 3.3.5a model and {texture index: (file id, flags)} for its fixed (non-look) textures."""
    raw = casc(cache, fdid)
    m = Retail(raw)
    anims = {}
    for a, s, f in m.afid:
        if f and a in valid:
            try:
                anims[(a, s)] = casc(cache, f)
            except Exception:
                pass
    conv = Converter(raw, casc(cache, m.sfid[0]), anims, valid, stem)
    names = listfile(cache, m.txid)
    conv.mask_textures = {i for i, fd in enumerate(m.txid)
                          if any(w in os.path.basename(names.get(fd, "")) for w in MASK_WORDS)}
    drawn = conv.batch_textures(dict(geosets))
    textures = []
    for i, (ttype, tflags, _n, _o) in enumerate(m.arr("textures", "<IIII")):
        fd = (m.txid[i] if i < len(m.txid) else 0) or dict(chosen).get(ttype, 0)
        if i not in drawn or (ttype not in (11, 12, 13) and not fd):
            textures.append((None, tflags, None))
        elif ttype in (11, 12, 13):
            textures.append((ttype, tflags, None))
        else:
            textures.append((0, tflags, "T%d.blp" % fd))
    m2, skin = conv.convert(textures, dict(geosets))
    model = Model(m2, skin)
    fixed = {}
    for i, (ttype, tflags, _n, _o) in enumerate(model.textures):
        if ttype == 0 and textures[i][2] and textures[i][0] is not None:
            fixed[i] = (int(textures[i][2][1:-4]), tflags)
    return model, fixed


def look_textures(model, fixed, tex, fallback, image):
    """The textures draw() takes for one look (tex: its three skin file ids; image(file id) ->
    RGBA or None), and notes on what it had to make up."""
    use, note = {}, []
    for i, (ttype, tflags, _n, _o) in enumerate(model.textures):
        if i in fixed:
            fd = fixed[i][0]
        elif 11 <= ttype <= 13:
            fd = tex[ttype - 11] or fallback.get(ttype - 11, 0)
            if not tex[ttype - 11]:
                note.append("slot %d %s" % (ttype - 11, "borrowed" if fd else "empty"))
        else:
            continue
        img = image(fd) if fd else None
        if img is None:
            img = np.full((4, 4, 4), 255, np.uint8)  # the client draws a missing texture white
            if fd:
                note.append("texture %d unreadable" % fd)
        use[i] = (img, tflags)
    return use, note


def blp_reader(cache, target=256):
    """image(file id) -> RGBA array or None, each file read once."""
    images = {}

    def image(fd):
        if fd not in images:
            try:
                images[fd] = blp_image(casc(cache, fd), target)
            except Exception:
                images[fd] = None
        return images[fd]

    return image


def render_variant(job):
    """One model with one geoset choice: convert it, then draw each of its looks."""
    cache, valid, out, fdid, geosets, stem, chosen, looks, fallback = job
    done = []
    try:
        model, fixed = convert_variant(cache, valid, fdid, geosets, stem, chosen)
    except Exception as e:
        return [(d, "convert: %s" % (str(e)[:80] or type(e).__name__)) for d, _t in looks]
    image = blp_reader(cache)
    for display, tex in looks:
        try:
            use, note = look_textures(model, fixed, tex, fallback, image)
            Image.fromarray(draw(model, use)).save(os.path.join(out, "img", "%s.jpg" % display), quality=86)
            done.append((display, "; ".join(note)))
        except Exception as e:
            done.append((display, "draw: %s" % (str(e)[:80] or type(e).__name__)))
    return done


def has_render(display):
    """Whether Blizzard's render server has a picture of this retail display (None: no answer)."""
    for _ in range(3):
        try:
            req = urllib.request.Request(RENDER % display, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return False
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
    return None


def renders(out, displays):
    """{display: has a render}, asking only for the displays not asked about before."""
    path = os.path.join(out, "renders.json")
    known = json.load(open(path)) if os.path.exists(path) else {}
    ask = [d for d in displays if known.get(d) is None]
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        known.update(zip(ask, ex.map(has_render, ask)))
    with open(path, "w") as f:
        json.dump(known, f)
    return known


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--client", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--families")
    ap.add_argument("--only", help="comma-separated retail display ids")
    ap.add_argument("--page-only", action="store_true", help="rebuild the page from drawn.json, drawing nothing")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "img"), exist_ok=True)
    valid = read_dbc_ids(os.path.join(args.client, "AnimationData.dbc"))
    geo = collections.defaultdict(dict)
    for g in db2_rows(args.cache, "CreatureDisplayInfoGeosetData"):
        geo[g["CreatureDisplayInfoID"]][int(g["GeosetIndex"]) + 1] = int(g["GeosetValue"])
    pet_type = {p["ID"]: int(p["PetTypeEnum"]) for p in db2_rows(args.cache, "BattlePetSpecies")}
    custom = customization_textures(args.cache)
    family = {}
    if args.families:
        for line in open(args.families, encoding="utf-8").read().splitlines()[1:]:
            fam, site_id, name, thumb = line.split("\t")
            family[name.lower()] = (fam, site_id, thumb)
    models = json.load(open(os.path.join(args.census, "models.json")))
    only = set(args.only.split(",")) if args.only else None

    tiles = {}  # retail display -> tile
    variants = collections.defaultdict(list)
    fallback = collections.defaultdict(dict)
    for line in open(os.path.join(args.census, "displays.tsv"), encoding="utf-8").read().splitlines()[1:]:
        kind, thing, name, display, fdid, path, status, tex, _geosets = line.split("\t")
        if status != "new model" or (only and display not in only):
            continue
        tex = [int(t) for t in tex.split(",")]
        for k in range(3):
            if tex[k]:
                fallback[fdid].setdefault(k, tex[k])
        result = models.get(fdid, {})
        if display not in tiles:
            stem = os.path.basename(path)[:-3]
            tiles[display] = {"d": display, "model": stem, "names": [], "kinds": [], "group": None,
                              "result": result.get("result", "?"), "fx": result.get("particles", 0),
                              "ribbons": result.get("ribbons", 0), "tex4": bool(tex[3]),
                              "geo": bool(geo.get(display)), "nomove": result.get("no_basic") or []}
            if result.get("result") == "ok":
                variants[(int(fdid), tuple(sorted(geo.get(display, {}).items())), stem,
                          tuple(sorted(custom.get(display, {}).items())))].append((display, tex[:3]))
        t = tiles[display]
        if name not in t["names"]:
            t["names"].append(name)
        if kind not in t["kinds"]:
            t["kinds"].append(kind)
        if kind == "mount":
            fam = family.get(name.lower())
            if fam and not t.get("ref"):
                t["group"], t["ref"], t["page"] = fam[0], fam[2], fam[1]
            t["group"] = t["group"] or ("Mounts in no family" if family else "Mounts")
        elif not t["group"]:
            t["group"] = "Pets: " + PET_TYPES[pet_type.get(thing, 0) % len(PET_TYPES)]

    jobs = [(args.cache, valid, args.out, fdid, geosets, stem, chosen, looks, fallback[str(fdid)])
            for (fdid, geosets, stem, chosen), looks in sorted(variants.items())]
    print("%d looks on %d model variants" % (sum(len(j[7]) for j in jobs), len(jobs)), flush=True)
    drawn_path = os.path.join(args.out, "drawn.json")
    if args.page_only:
        notes = json.load(open(drawn_path))
    else:
        notes = {}
        with multiprocessing.Pool(args.workers) as pool:
            for n, done in enumerate(pool.imap_unordered(render_variant, jobs), 1):
                notes.update(done)
                if n % 100 == 0:
                    print("  %d/%d" % (n, len(jobs)), flush=True)
        if not only:
            with open(drawn_path, "w") as f:
                json.dump(notes, f)
    for display, note in notes.items():
        if display in tiles:
            tiles[display]["note"] = note
            tiles[display]["drawn"] = not note.startswith(("convert:", "draw:"))
    bad = [t for t in tiles.values() if t["result"] == "ok" and not t.get("drawn")]
    print("%d tiles, %d drawn, %d the converter refuses, %d failed to draw" % (
        len(tiles), sum(1 for t in tiles.values() if t.get("drawn")),
        sum(1 for t in tiles.values() if t["result"] != "ok"), len(bad)))
    for t in bad[:20]:
        print("  %s %s: %s" % (t["d"], t["model"], t.get("note")))
    if only:
        return
    have = renders(args.out, sorted(tiles))
    for t in tiles.values():
        t["blz"] = bool(have.get(t["d"]))
    print("%d of %d looks have a Blizzard render; %d more a Warcraft Mounts thumbnail; %d no reference" % (
        sum(1 for t in tiles.values() if t["blz"]), len(tiles),
        sum(1 for t in tiles.values() if not t["blz"] and t.get("ref")),
        sum(1 for t in tiles.values() if not t["blz"] and not t.get("ref"))))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "contact_sheet.html"), encoding="utf-8") as f:
        page = f.read()
    data = sorted(tiles.values(), key=lambda t: (t["kinds"] != ["mount"] and "mount" not in t["kinds"],
                                                 t["group"], t["model"], t["names"][0]))
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page.replace("/*DATA*/[]", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
                .replace("{{SITE}}", html.escape(SITE)).replace("{{RENDER}}", html.escape(RENDER)))
    print("wrote %s" % os.path.join(args.out, "index.html"))


if __name__ == "__main__":
    sys.exit(main())
