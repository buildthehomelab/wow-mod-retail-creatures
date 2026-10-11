#!/usr/bin/env python3
"""Sanity-check a 3.3.5a (MD20 v264) model + skin the way the client reads them: every array
inside the file, every animated track with one sub-array per sequence, keyframes sorted and
inside their sequence, skin indexes in range, a bounding box that fits the mesh, and triangles
the mouse hit test can read (each submesh's inside its own vertices, the collision mesh's inside
the collision vertices).

    python3 check_m2.py model.m2 model00.skin
"""
import struct
import sys

ARRAYS = [("name", "a"), ("global_flags", "u"), ("global_loops", "a"), ("sequences", "a"),
          ("sequence_lookup", "a"), ("bones", "a"), ("key_bone_lookup", "a"), ("vertices", "a"),
          ("num_skins", "u"), ("colors", "a"), ("textures", "a"), ("texture_weights", "a"),
          ("texture_transforms", "a"), ("replaceable_lookup", "a"), ("materials", "a"),
          ("bone_lookup", "a"), ("texture_lookup", "a"), ("texunit_lookup", "a"),
          ("weight_lookup", "a"), ("transform_lookup", "a"), ("bbox", 28), ("cbox", 28),
          ("collision_indices", "a"), ("collision_positions", "a"), ("collision_normals", "a"),
          ("attachments", "a"), ("attachment_lookup", "a"), ("events", "a"), ("lights", "a"),
          ("cameras", "a"), ("camera_lookup", "a"), ("ribbons", "a"), ("particles", "a")]
SIZES = {"global_loops": 4, "sequences": 64, "sequence_lookup": 2, "bones": 88, "key_bone_lookup": 2,
         "vertices": 48, "colors": 40, "textures": 16, "texture_weights": 20, "texture_transforms": 60,
         "replaceable_lookup": 2, "materials": 4, "bone_lookup": 2, "texture_lookup": 2,
         "texunit_lookup": 2, "weight_lookup": 2, "transform_lookup": 2, "collision_indices": 2,
         "collision_positions": 12, "collision_normals": 12, "attachments": 40, "attachment_lookup": 2,
         "events": 36, "cameras": 100, "camera_lookup": 2, "name": 1}


def check(m2, skin):
    errors = []
    assert m2[:4] == b"MD20" and struct.unpack_from("<I", m2, 4)[0] == 264
    o, h = 8, {}
    for name, kind in ARRAYS:
        if kind == "a":
            h[name] = struct.unpack_from("<II", m2, o); o += 8
        elif kind == "u":
            h[name] = struct.unpack_from("<I", m2, o)[0]; o += 4
        else:
            o += kind
    for name, size in SIZES.items():
        n, off = h[name]
        if n and (off + n * size > len(m2) or off < 0x130):
            errors.append("%s: %d x %d at %d is outside the file" % (name, n, size, off))
    for name in ("lights", "ribbons", "particles"):
        if h[name][0]:
            errors.append("%s present (converter should drop them)" % name)
    nseq = h["sequences"][0]
    seqs = [struct.unpack_from("<HHIfIhHIII6ffhH", m2, h["sequences"][1] + 64 * i) for i in range(nseq)]
    loops = list(struct.unpack_from("<%dI" % h["global_loops"][0], m2, h["global_loops"][1]))

    def track(where, off, vsize):
        interp, gseq, tn, to = struct.unpack_from("<HhII", m2, off)
        if vsize:
            vn, vo = struct.unpack_from("<II", m2, off + 12)
        if tn == 0:
            return
        if gseq == -1 and tn != nseq:
            errors.append("%s: %d timestamp arrays for %d sequences" % (where, tn, nseq))
        if gseq != -1 and gseq >= len(loops):
            errors.append("%s: global sequence %d of %d" % (where, gseq, len(loops)))
        if interp > 3:
            errors.append("%s: interpolation %d" % (where, interp))
        for k in range(tn):
            n, p = struct.unpack_from("<II", m2, to + 8 * k)
            ts = struct.unpack_from("<%dI" % n, m2, p) if n else ()
            if n and p + 4 * n > len(m2):
                errors.append("%s seq %d: timestamps outside the file" % (where, k))
            if list(ts) != sorted(ts):
                errors.append("%s seq %d: timestamps not sorted" % (where, k))
            limit = loops[gseq] if gseq != -1 else seqs[k][2]
            if ts and ts[-1] > limit:
                errors.append("%s seq %d: key at %d past duration %d" % (where, k, ts[-1], limit))
            if vsize:
                vn2, vp = struct.unpack_from("<II", m2, vo + 8 * k)
                if vn2 != n or vp + vsize * vn2 > len(m2):
                    errors.append("%s seq %d: %d values for %d times" % (where, k, vn2, n))

    nb, bo = h["bones"]
    for i in range(nb):
        b = bo + 88 * i
        parent = struct.unpack_from("<h", m2, b + 8)[0]
        if parent >= nb or parent >= i and parent != -1:
            errors.append("bone %d: parent %d" % (i, parent))
        track("bone %d translation" % i, b + 16, 12)
        track("bone %d rotation" % i, b + 36, 8)
        track("bone %d scale" % i, b + 56, 12)
    for i in range(h["colors"][0]):
        track("color %d" % i, h["colors"][1] + 40 * i, 12)
        track("alpha %d" % i, h["colors"][1] + 40 * i + 20, 2)
        # A colour track's sequence without keys is black in this client (alpha's is opaque).
        interp, gseq, tn, to = struct.unpack_from("<HhII", m2, h["colors"][1] + 40 * i)
        if gseq == -1 and tn:
            empty = [k for k in range(tn) if not struct.unpack_from("<I", m2, to + 8 * k)[0]]
            if empty:
                errors.append("color %d: no key in sequence %s (draws black)" % (i, empty[:5]))
    for i in range(h["texture_weights"][0]):
        track("weight %d" % i, h["texture_weights"][1] + 20 * i, 2)
    for i in range(h["attachments"][0]):
        a = h["attachments"][1] + 40 * i
        if struct.unpack_from("<H", m2, a + 4)[0] >= nb:
            errors.append("attachment %d: bad bone" % i)
        track("attachment %d" % i, a + 20, 1)
    for i in range(h["events"][0]):
        track("event %d" % i, h["events"][1] + 36 * i + 24, 0)
    for i in range(h["cameras"][0]):
        c = h["cameras"][1] + 100 * i
        track("camera %d pos" % i, c + 16, 36)
        track("camera %d target" % i, c + 48, 36)
        track("camera %d roll" % i, c + 80, 12)
    for i, s in enumerate(seqs):
        if s[18] != -1 and not 0 <= s[18] < nseq:
            errors.append("sequence %d: variationNext %d" % (i, s[18]))
        if s[4] & 0x40 or not s[4] & 0x20:
            errors.append("sequence %d: flags %#x" % (i, s[4]))
    table = struct.unpack_from("<%dH" % h["sequence_lookup"][0], m2, h["sequence_lookup"][1])
    if 0xFFFF not in table:
        errors.append("sequence lookup is full (hangs the client)")
    for v in table:
        if v != 0xFFFF and v >= nseq:
            errors.append("sequence lookup points at %d" % v)
    bl = struct.unpack_from("<%dH" % h["bone_lookup"][0], m2, h["bone_lookup"][1])
    if any(x >= nb for x in bl):
        errors.append("bone lookup out of range")
    ntex = h["textures"][0]
    tl = struct.unpack_from("<%dh" % h["texture_lookup"][0], m2, h["texture_lookup"][1])
    nverts = h["vertices"][0]
    for i in range(nverts):
        v = h["vertices"][1] + 48 * i
        w = m2[v + 12:v + 16]
        idx = m2[v + 16:v + 20]
        if any(idx[k] >= nb for k in range(4) if w[k]):
            errors.append("vertex %d: bone index out of range" % i)
            break

    # The client's mouse hit test of a model that is tested by its collision mesh (Wow.exe
    # 0x81DD50, build 12340) copies the collision vertices into a buffer of that many entries and
    # reads the triangles from it without a check.
    ncv = h["collision_positions"][0]
    ctris = struct.unpack_from("<%dH" % h["collision_indices"][0], m2, h["collision_indices"][1])
    if len(ctris) % 3:
        errors.append("collision mesh: %d indices is not a whole number of triangles" % len(ctris))
    if any(x >= ncv for x in ctris):
        errors.append("collision mesh: a triangle points past its %d vertices (the mouse hit test reads "
                      "outside its buffer: client crash at 0x0081D559)" % ncv)

    # skin
    assert skin[:4] == b"SKIN"
    a = [struct.unpack_from("<II", skin, 4 + 8 * i) for i in range(5)]
    vmap = struct.unpack_from("<%dH" % a[0][0], skin, a[0][1])
    idx = struct.unpack_from("<%dH" % a[1][0], skin, a[1][1])
    if any(x >= nverts for x in vmap):
        errors.append("skin vertex map past the model's vertices")
    if any(x >= len(vmap) for x in idx):
        errors.append("skin index past the vertex map")
    subs = [struct.unpack_from("<HHHHHHHHHH3f3ff", skin, a[3][1] + 48 * i) for i in range(a[3][0])]
    # A 3.3.5 model frame seems to size a creature by its box: far past the mesh (the client's
    # own models stay within the mesh's size), the look is a speck.
    pts = [struct.unpack_from("<3f", m2, h["vertices"][1] + 48 * v) for v in set(vmap) if v < nverts]
    if pts:
        box = struct.unpack_from("<6f", m2, 0xA0)
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        size = max(hi[k] - lo[k] for k in range(3))
        if any(lo[k] - box[k] > size or box[3 + k] - hi[k] > size for k in range(3)):
            errors.append("bounding box reaches more than the mesh's size past the mesh")
    for i, s in enumerate(subs):
        if s[6] > 64:
            errors.append("submesh %d: %d bones in its palette (3.3.5 GPU skinning crashes past ~75)" % (i, s[6]))
        if s[1] or s[2] + s[3] > len(vmap) or s[4] + s[5] > len(idx) or s[7] + s[6] > len(bl):
            errors.append("submesh %d out of range %s" % (i, s[:8]))
            continue
        # For any other model the hit test (0x81DAF0) animates one submesh's vertices into a
        # buffer of vertexCount entries and reads buffer[index - vertexStart] for each of that
        # submesh's triangles. Drawing never shows a triangle that points at another submesh's
        # vertices (the graphics card has them all); the hit test reads outside its buffer.
        used = idx[s[4]:s[4] + s[5]]
        if s[5] % 3:
            errors.append("submesh %d: %d indices is not a whole number of triangles" % (i, s[5]))
        out = [k for k, v in enumerate(used) if not s[2] <= v < s[2] + s[3]]
        if out:
            k = out[0] // 3 * 3
            errors.append("submesh %d: triangle %s points outside its vertices %d-%d, %d indices in all (the "
                          "mouse hit test reads outside its buffer: client crash at 0x0081D559)"
                          % (i, list(used[k:k + 3]), s[2], s[2] + s[3] - 1, len(out)))
    for i in range(a[4][0]):
        bt = struct.unpack_from("<BbHHHhHHHHHHH", skin, a[4][1] + 24 * i)
        if bt[3] >= len(subs):
            errors.append("batch %d: submesh %d" % (i, bt[3]))
        if bt[6] >= h["materials"][0]:
            errors.append("batch %d: material %d" % (i, bt[6]))
        if bt[9] >= len(tl) or tl[bt[9]] >= ntex:
            errors.append("batch %d: texture combo %d" % (i, bt[9]))
        if bt[11] >= h["weight_lookup"][0]:
            errors.append("batch %d: weight combo %d" % (i, bt[11]))
        if bt[12] >= h["transform_lookup"][0]:
            errors.append("batch %d: transform combo %d" % (i, bt[12]))
    return errors, dict(sequences=nseq, bones=nb, vertices=nverts, submeshes=len(subs), batches=a[4][0],
                        attachments=h["attachments"][0], events=h["events"][0], cameras=h["cameras"][0],
                        anim_ids=sorted({s[0] for s in seqs}))


if __name__ == "__main__":
    errs, info = check(open(sys.argv[1], "rb").read(), open(sys.argv[2], "rb").read())
    print(info)
    for e in errs[:40]:
        print("ERROR", e)
    sys.exit(1 if errs else 0)
