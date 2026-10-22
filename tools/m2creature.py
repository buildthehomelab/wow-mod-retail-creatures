"""Convert a retail creature M2 (MD21, v272+) with its animations into a 3.3.5a model (MD20, v264)
plus 00.skin.

Kept: the skin-0 mesh, bones with every animation the 3.3.5 client knows (external .anim data is
inlined), attachments, key bones, colours, texture weights and transforms, footstep-style events,
cameras (the unit portrait) and collision.
Dropped: particles, ribbons, lights, sequences with animation ids newer than 3.3.5, and retail
texture layers past the first (env/glow passes have no 3.3.5 shader).
"""
import struct

HEADER_SIZE = 0x130
SEQ_IN_M2 = 0x20
SEQ_ALIAS = 0x40

ARRAYS = [  # header M2Arrays after magic/version, in file order ('u' = plain uint32)
    ("name", "a"), ("global_flags", "u"), ("global_loops", "a"), ("sequences", "a"),
    ("sequence_lookup", "a"), ("bones", "a"), ("key_bone_lookup", "a"), ("vertices", "a"),
    ("num_skins", "u"), ("colors", "a"), ("textures", "a"), ("texture_weights", "a"),
    ("texture_transforms", "a"), ("replaceable_lookup", "a"), ("materials", "a"),
    ("bone_lookup", "a"), ("texture_lookup", "a"), ("texunit_lookup", "a"),
    ("weight_lookup", "a"), ("transform_lookup", "a"),
    ("bbox", "6f"), ("bradius", "f"), ("cbox", "6f"), ("cradius", "f"),
    ("collision_indices", "a"), ("collision_positions", "a"), ("collision_normals", "a"),
    ("attachments", "a"), ("attachment_lookup", "a"), ("events", "a"), ("lights", "a"),
    ("cameras", "a"), ("camera_lookup", "a"), ("ribbons", "a"), ("particles", "a"),
]

SEQUENCE = "<HHIfIhHIIHH6ffhH"  # retail: blendTimeIn/Out as two uint16
TRACK = "<HhIIII"
TRACK_SIZE = 20
MAX_PALETTE = 64  # bones per submesh the 3.3.5 GPU skinning takes (see split_submeshes)
BOUNDS_LIMIT, BOUNDS_SLACK = 1.0, 0.5  # the model's box against its drawn mesh (see fit_bounds)


def chunks(data):
    out, o = {}, 0
    while o + 8 <= len(data):
        tag, size = struct.unpack_from("<4sI", data, o)
        out[tag.decode("latin-1")] = data[o + 8:o + 8 + size]
        o += 8 + size
    return out


def u32s(blob):
    return list(struct.unpack("<%dI" % (len(blob) // 4), blob))


class Retail:
    def __init__(self, data):
        if data[:4] != b"MD21":
            raise ValueError("not a retail MD21 model")
        self.chunks = chunks(data)
        self.md = self.chunks["MD21"]
        self.version = struct.unpack_from("<I", self.md, 4)[0]
        o, h = 8, {}
        for name, kind in ARRAYS:
            if kind == "a":
                h[name] = struct.unpack_from("<II", self.md, o); o += 8
            elif kind == "u":
                h[name] = struct.unpack_from("<I", self.md, o)[0]; o += 4
            else:
                n = struct.calcsize("<" + kind)
                h[name] = struct.unpack_from("<" + kind, self.md, o); o += n
        if h["global_flags"] & 0x8:
            raise ValueError("texture combiner combos (flag 0x8) not supported")
        self.h = h
        self.sfid = u32s(self.chunks.get("SFID", b""))
        self.txid = u32s(self.chunks.get("TXID", b""))
        afid = self.chunks.get("AFID", b"")
        self.afid = [struct.unpack_from("<HHI", afid, i * 8) for i in range(len(afid) // 8)]
        if "SKID" in self.chunks:
            raise ValueError("shared skeletons (SKID) not supported")

    def arr(self, name, fmt):
        n, off = self.h[name]
        size = struct.calcsize(fmt)
        return [struct.unpack_from(fmt, self.md, off + i * size) for i in range(n)]

    def raw(self, name, size):
        n, off = self.h[name]
        return self.md[off:off + n * size], n


class Skin:
    def __init__(self, data):
        if data[:4] != b"SKIN":
            raise ValueError("not a skin file")
        self.data = data
        a = [struct.unpack_from("<II", data, 4 + i * 8) for i in range(5)]
        self.vmap = self._arr(a[0], "<H")
        self.indices = self._arr(a[1], "<H")
        self.bones = self.data[a[2][1]:a[2][1] + 4 * a[2][0]]
        self.submeshes = self._arr(a[3], "<HHHHHHHHHH3f3ff")
        self.batches = self._arr(a[4], "<BbHHHhHHHHHHH")

    def _arr(self, a, fmt):
        n, off = a
        size = struct.calcsize(fmt)
        out = [struct.unpack_from(fmt, self.data, off + i * size) for i in range(n)]
        return [x[0] for x in out] if len(fmt) == 2 else out


class StaleSkin(ValueError):
    """The skin was made for another version of the model (wago.tools mixes builds)."""


def skin_mismatches(model, skin):
    """How many of the skin's bone influences name another bone than the model's own vertex.
    A skin made for the same model names the same bones everywhere; one made for another
    version of it joins the wrong vertices into triangles, which draws as shards."""
    lookup = [x[0] for x in model.arr("bone_lookup", "<H")]
    verts = model.arr("vertices", "<12x4B4B28x")
    bad = 0
    for sub in skin.submeshes:
        for v in range(sub[2], sub[2] + sub[3]):
            if v >= len(skin.vmap) or skin.vmap[v] >= len(verts):
                bad += 1
                continue
            w, g = verts[skin.vmap[v]][:4], verts[skin.vmap[v]][4:]
            for k in range(4):
                i = sub[7] + skin.bones[4 * v + k]
                if w[k] and (lookup[i] if i < len(lookup) else -1) != g[k]:
                    bad += 1
    return bad


def anim_buffer(data):
    """Raw keyframe data of a retail .anim file (offsets in the model point into it)."""
    if data[:4] == b"AFM2":
        return chunks(data)["AFM2"]
    return data  # pre-Legion .anim files are raw


class Blob:
    def __init__(self):
        self.data = bytearray(HEADER_SIZE)

    def add(self, payload, count):
        if not count:
            return (0, 0)
        while len(self.data) % 16:
            self.data.append(0)
        off = len(self.data)
        self.data += payload
        return (count, off)


def is_prime(n):
    return n > 1 and all(n % d for d in range(2, int(n ** 0.5) + 1))


def sequence_hash(anim_ids):
    """sequenceIdxHashById: open addressing (slot = id % size, quadratic probing), 0xFFFF = empty.
    The 3.3.5 client probes until it finds the id or an empty slot, so it must never be full
    (a full table hangs the client on any animation the model lacks)."""
    unique = []
    for i, a in enumerate(anim_ids):
        if a not in [anim_ids[j] for j in unique]:
            unique.append(i)
    size = 2 * len(unique) + 1
    while not is_prime(size):
        size += 1
    table = [0xFFFF] * size
    for seq_index in unique:
        anim_id = anim_ids[seq_index]
        slot, step = anim_id % size, 1
        while table[slot] != 0xFFFF:
            slot = (slot + step * step) % size
            step += 1
        table[slot] = seq_index
    return table


class Converter:
    def __init__(self, model_data, skin_data, anim_files, valid_anim_ids, name):
        """anim_files: {(anim id, variation): .anim bytes} for the model's AFID entries."""
        self.m = Retail(model_data)
        self.skin = Skin(skin_data)
        self.name = name
        bad = skin_mismatches(self.m, self.skin)
        if bad:
            raise StaleSkin("%s: the skin was made for another version of the model (%d bone "
                            "influences differ)" % (name, bad))
        md = self.m.md
        self.seqs = self.m.arr("sequences", SEQUENCE)
        # Where each old sequence's keyframes live (aliases borrow their target's).
        self.buffers = []
        self.source = []
        for i, s in enumerate(self.seqs):
            j, seen = i, set()
            while self.seqs[j][4] & SEQ_ALIAS and j not in seen:
                seen.add(j)
                j = self.seqs[j][19]
            self.source.append(j)
        for i, s in enumerate(self.seqs):
            if s[4] & SEQ_IN_M2:
                self.buffers.append(md)
            else:
                data = anim_files.get((s[0], s[1]))
                self.buffers.append(anim_buffer(data) if data is not None else None)
        self.dropped = []
        self.bad_tracks = 0
        for i, s in enumerate(self.seqs):
            if not s[4] & SEQ_IN_M2 and self.buffers[i] is not None and not self._fits(i):
                self.buffers[i] = None  # stale .anim (made for an older model): drop that animation
                self.dropped.append(s[0])
        # Keep sequences the 3.3.5 client knows, with their data present.
        self.kept = [i for i, s in enumerate(self.seqs)
                     if s[0] in valid_anim_ids and self.buffers[self.source[i]] is not None]
        self.new_index = {old: new for new, old in enumerate(self.kept)}
        self.blob = Blob()
        self.geosets = {}
        self.skipped_textures = set()
        self.mask_textures = set()  # texture indices that only shape another texture (see pick)
        self.key_combo = {}  # texture index: texture lookup entry of its alpha-key copy (see convert)
        self.tex_lookup_out = None  # the texture lookup the converted model gets
        self.texture_use = {}  # texture index: blend modes of the batches drawing it, after convert

    def _fits(self, seq):
        """Every bone keyframe array of an external sequence lies inside its .anim data."""
        md, buf = self.m.md, self.buffers[seq]
        raw, n = self.m.raw("bones", 88)
        for i in range(n):
            for off, size in ((16, 12), (36, 8), (56, 12)):
                interp, gseq, tsn, tso, vn, vo = struct.unpack_from(TRACK, raw, i * 88 + off)
                if gseq != -1 or seq >= tsn:
                    continue
                cnt, at = struct.unpack_from("<II", md, tso + 8 * seq)
                vcnt, vat = struct.unpack_from("<II", md, vo + 8 * seq)
                if not cnt:
                    continue
                if at + 4 * cnt > len(buf) or vat + size * vcnt > len(buf) or vcnt != cnt:
                    return False
                times = struct.unpack_from("<%dI" % cnt, buf, at)
                if list(times) != sorted(times) or times[-1] > self.seqs[seq][2]:
                    return False
        return True

    # --- tracks -------------------------------------------------------------------------
    def read_track(self, raw, value_size):
        """Track bytes -> (interp, gseq, [(times, values bytes)] per kept sequence or per global array)."""
        interp, gseq, tsn, tso, vn, vo = struct.unpack(TRACK, raw)
        md = self.m.md
        if gseq != -1:
            loops = self.m.arr("global_loops", "<I")
            loop = loops[gseq][0] if gseq < len(loops) else 0
            out = []
            for k in range(tsn):
                n, off = struct.unpack_from("<II", md, tso + 8 * k)
                vn2, voff = struct.unpack_from("<II", md, vo + 8 * k) if value_size else (n, 0)
                times = list(struct.unpack_from("<%dI" % n, md, off))
                values = md[voff:voff + value_size * vn2]
                # Retail leaves keys past the end of a global loop (a lone key at 633 in a loop of
                # 0, on the air totems). The loop never reaches them: keep the ones it does, or
                # the first as a fixed value.
                if times and times[-1] > loop and (not value_size or vn2 == n):
                    keep = max(1, sum(1 for t in times if t <= loop))
                    times = [min(t, loop) for t in times[:keep]]
                    values = values[:value_size * keep]
                out.append((times, values))
            return (interp, gseq, out)
        out = []
        for old in self.kept:
            src = self.source[old]
            buf = self.buffers[src]
            if src >= tsn:
                out.append(([], b""))
                continue
            n, off = struct.unpack_from("<II", md, tso + 8 * src)
            vn2, voff = struct.unpack_from("<II", md, vo + 8 * src) if value_size else (n, 0)
            if n != vn2:
                raise ValueError("%s: track with %d times and %d values" % (self.name, n, vn2))
            if n and (off + 4 * n > len(buf) or voff + value_size * n > len(buf)):
                out.append(([], b""))  # a non-bone track the stale .anim doesn't cover
                continue
            times = list(struct.unpack_from("<%dI" % n, buf, off)) if n else []
            values = bytes(buf[voff:voff + value_size * n]) if n and value_size else b""
            if times and (times != sorted(times) or times[-1] > self.seqs[old][2]):
                self.bad_tracks += 1  # garbage (stale .anim data): leave this track still
                times, values = [], b""
            out.append((times, values))
        return (interp, gseq, out)

    def write_track(self, track, value_size):
        interp, gseq, per = track
        if not per or not any(t for t, _ in per):
            return struct.pack(TRACK, interp, gseq, 0, 0, 0, 0)
        return self._write_arrays(interp, gseq, per, value_size)

    def _write_arrays(self, interp, gseq, per, value_size):
        b = self.blob
        times = [b.add(struct.pack("<%dI" % len(t), *t), len(t)) for t, _ in per]
        ts = b.add(b"".join(struct.pack("<II", *x) for x in times), len(per))
        if value_size is None:  # timestamps only (events)
            return struct.pack("<HhII", interp, gseq, *ts)
        values = [b.add(v, len(t)) for t, v in per]
        vs = b.add(b"".join(struct.pack("<II", *x) for x in values), len(per))
        return struct.pack(TRACK, interp, gseq, *ts, *vs)

    def convert_tracks(self, raw, layout):
        """Rewrite a record: layout = [(offset, value size) for each M2Track in it]."""
        out = bytearray(raw)
        for off, size in layout:
            t = self.read_track(raw[off:off + TRACK_SIZE], size)
            out[off:off + TRACK_SIZE] = self.write_track(t, size)
        return bytes(out)

    def records(self, name, size, layout):
        raw, n = self.m.raw(name, size)
        rows = [self.convert_tracks(raw[i * size:(i + 1) * size], layout) for i in range(n)]
        return self.blob.add(b"".join(rows), n)

    def copy(self, name, size):
        raw, n = self.m.raw(name, size)
        return self.blob.add(raw, n)

    def colors(self):
        """M2Color records (a colour track and an alpha track each). Retail writes a colour that
        doesn't change as one key in the model's first sequence and leaves the rest empty; its
        client goes on using that key. The 3.3.5 client gives a sequence without keys the
        track's default instead, and a colour's default is black: a totem whose first sequence
        isn't Stand stood as a black shape, and a glow went out whenever a form left its first
        animation. So every empty sequence of a colour track gets that first key, or white if
        retail has none. Alpha tracks stay as they are: their default is opaque, which fits how
        retail keys them (fade in at spawn, fade out at death, nothing while standing)."""
        raw, n = self.m.raw("colors", 40)
        rows = b""
        for i in range(n):
            rec = raw[i * 40:(i + 1) * 40]
            interp, gseq, per = self.read_track(rec[:TRACK_SIZE], 12)
            if gseq == -1 and self.kept:
                rest = self.rest_value(rec[:TRACK_SIZE], 12) or struct.pack("<3f", 1.0, 1.0, 1.0)
                per = [(t, v) if t else ([0], rest) for t, v in per]
            rows += self.write_track((interp, gseq, per), 12)
            rows += self.write_track(self.read_track(rec[TRACK_SIZE:], 2), 2)
        return self.blob.add(rows, n)

    def rest_value(self, raw, value_size):
        """The first key of a track's first retail sequence, or None when that one has no keys."""
        interp, gseq, tsn, tso, vn, vo = struct.unpack(TRACK, raw)
        if not tsn or not vn:
            return None
        md = self.m.md
        buf = self.buffers[self.source[0]] if self.seqs else md
        n, off = struct.unpack_from("<II", md, tso)
        vn2, voff = struct.unpack_from("<II", md, vo)
        if not n or n != vn2 or buf is None or voff + value_size > len(buf):
            return None
        return bytes(buf[voff:voff + value_size])

    # --- model --------------------------------------------------------------------------
    def convert(self, textures, geosets=None, materials_mask=0x1F, key_copies=None):
        """textures: list of (type, flags, file name or None) replacing the retail texture list
        (None for creature skin slots 11-13). geosets: {group: variant} from retail
        CreatureDisplayInfoGeosetData (group = GeosetIndex + 1); the 3.3.5 client can't pick
        creature geosets, so the chosen ones are baked in. key_copies: {texture index: (type,
        flags, file name or None)}, a texture the alpha-key batches of that texture draw with
        instead (added after the others), for a texture that blended batches draw too: see
        ALPHA_KEY in build_patch.py. Returns (m2 bytes, skin bytes); texture_use then says
        which blend modes draw each texture index.
        """
        self.geosets = geosets or {}
        key_copies = key_copies or {}
        m, b, h = self.m, self.blob, {}
        nm = (self.name + "\0").encode()
        h["name"] = b.add(nm, len(nm))
        h["global_loops"] = self.copy("global_loops", 4)

        # Sequences.
        rows, ids = b"", []
        for new, old in enumerate(self.kept):
            s = self.seqs[old]
            (aid, var, dur, speed, flags, freq, _pad, rmin, rmax, blend_in, _blend_out,
             x0, y0, z0, x1, y1, z1, radius, var_next, _alias) = s
            nxt = var_next
            while nxt != -1 and nxt not in self.new_index:
                nxt = self.seqs[nxt][18]
            nxt = self.new_index.get(nxt, -1)
            flags = (flags & 0x1F) | SEQ_IN_M2
            rows += struct.pack("<HHIfIhHIII6ffhH", aid, var, dur, speed, flags, freq, 0, rmin, rmax,
                                blend_in, x0, y0, z0, x1, y1, z1, radius, nxt, new)
            ids.append(aid)
        h["sequences"] = b.add(rows, len(self.kept))
        table = sequence_hash(ids)
        h["sequence_lookup"] = b.add(struct.pack("<%dH" % len(table), *table), len(table))

        # Bones: key bone id, flags, parent, submesh, crc, translation, rotation, scale, pivot.
        raw, n = m.raw("bones", 88)
        rows = b""
        for i in range(n):
            r = bytearray(self.convert_tracks(raw[i * 88:(i + 1) * 88], [(16, 12), (36, 8), (56, 12)]))
            flags = struct.unpack_from("<I", r, 4)[0]
            struct.pack_into("<I", r, 4, flags & 0x3FF)
            rows += bytes(r)
        h["bones"] = b.add(rows, n)
        h["key_bone_lookup"] = self.copy("key_bone_lookup", 2)
        h["vertices"] = self.copy("vertices", 48)
        h["colors"] = self.colors()

        # A texture type 3.3.5 can't fill (None) gets a placeholder; its batches are skipped.
        self.skipped_textures = {i for i, t in enumerate(textures) if t[0] is None}
        textures = [(0, t[1], "Textures\\ShaneCube.blp") if t[0] is None else t for t in textures]
        tex_lookup = [x[0] for x in m.arr("texture_lookup", "<h")]
        self.key_combo = {}
        for i in sorted(key_copies):
            self.key_combo[i] = len(tex_lookup)
            tex_lookup.append(len(textures))
            textures.append(key_copies[i])
        self.tex_lookup_out = tex_lookup
        names = []
        for ttype, tflags, path in textures:
            if path:
                s = (path + "\0").encode()
                names.append(b.add(s, len(s)))
            else:
                names.append((0, 0))
        h["textures"] = b.add(b"".join(struct.pack("<IIII", t[0], t[1], *nmx)
                                       for t, nmx in zip(textures, names)), len(textures))
        h["texture_weights"] = self.records("texture_weights", 20, [(0, 2)])
        h["texture_transforms"] = self.records("texture_transforms", 60, [(0, 12), (20, 16), (40, 12)])
        h["replaceable_lookup"] = self.copy("replaceable_lookup", 2)
        mats = m.arr("materials", "<HH")
        h["materials"] = b.add(b"".join(struct.pack("<HH", f & materials_mask, bl if bl <= 6 else 4)
                                        for f, bl in mats), len(mats))
        skin, extra_lookup = self.convert_skin()
        lookup = [x[0] for x in m.arr("bone_lookup", "<H")] + extra_lookup
        h["bone_lookup"] = b.add(struct.pack("<%dH" % len(lookup), *lookup), len(lookup))
        h["texture_lookup"] = b.add(struct.pack("<%dh" % len(tex_lookup), *tex_lookup), len(tex_lookup))
        h["texunit_lookup"] = b.add(struct.pack("<h", 0), 1)
        h["weight_lookup"] = self.copy("weight_lookup", 2)
        h["transform_lookup"] = self.copy("transform_lookup", 2)
        h["collision_indices"] = self.copy("collision_indices", 2)
        h["collision_positions"] = self.copy("collision_positions", 12)
        h["collision_normals"] = self.copy("collision_normals", 12)
        h["attachments"] = self.records("attachments", 40, [(20, 1)])
        h["attachment_lookup"] = self.copy("attachment_lookup", 2)
        h["events"] = self.events()
        h["cameras"] = self.cameras()
        h["camera_lookup"] = self.copy("camera_lookup", 2)

        bounds = self.fit_bounds()
        hdr = bytearray(b"MD20" + struct.pack("<I", 264))
        for key, kind in ARRAYS:
            if kind == "a":
                hdr += struct.pack("<II", *h.get(key, (0, 0)))
            elif key == "global_flags":
                hdr += struct.pack("<I", m.h[key] & 0x7)
            elif key == "num_skins":
                hdr += struct.pack("<I", 1)
            elif key in bounds:
                hdr += struct.pack("<" + kind, *bounds[key])
            else:
                hdr += struct.pack("<" + kind, *(m.h[key] if isinstance(m.h[key], tuple) else (m.h[key],)))
        assert len(hdr) == HEADER_SIZE, hex(len(hdr))
        b.data[:HEADER_SIZE] = hdr
        return bytes(b.data), skin

    def fit_bounds(self):
        """The model's box and radius, with the sides that stray far from the drawn mesh pulled in.
        Retail's box covers every animation: a goblin fire totem is 3 yards tall and its box 26,
        because its death animation launches the rocket, and some flight forms reach 200 yards.
        A 3.3.5 model frame seems to size a creature by this box: that totem's preview tile was
        empty. The 3.3.5 client's own models keep each side within the mesh's largest dimension
        of the mesh, so a side further out than that (BOUNDS_LIMIT) comes in to half of it
        (BOUNDS_SLACK), and the radius shrinks with the box. Each animation keeps its own
        bounds."""
        box, radius = self.m.h["bbox"], self.m.h["bradius"][0]
        verts = self.m.arr("vertices", "<3f36x")
        pts = [verts[v] for v in self.drawn_vertices]
        if not pts:
            return {}
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        size = max(hi[k] - lo[k] for k in range(3))
        new = list(box)
        for k in range(3):
            if lo[k] - box[k] > BOUNDS_LIMIT * size:
                new[k] = lo[k] - BOUNDS_SLACK * size
            if box[3 + k] - hi[k] > BOUNDS_LIMIT * size:
                new[3 + k] = hi[k] + BOUNDS_SLACK * size

        def diagonal(b):
            return sum((b[3 + k] - b[k]) ** 2 for k in range(3)) ** 0.5

        if diagonal(box) > 0:
            radius *= diagonal(new) / diagonal(box)
        return {"bbox": new, "bradius": (radius,)}

    def events(self):
        """Only events that need no retail sound id ($FS* footsteps etc. carry data 0)."""
        raw, n = self.m.raw("events", 36)
        rows = []
        for i in range(n):
            r = raw[i * 36:(i + 1) * 36]
            ident, data = struct.unpack_from("<4sI", r, 0)
            if data:
                continue
            interp, gseq, tsn, tso = struct.unpack_from("<HhII", r, 24)
            t = self.read_track(struct.pack(TRACK, interp, gseq, tsn, tso, 0, 0), 0)
            if not any(times for times, _ in t[2]):
                continue  # never fires in a kept sequence
            rows.append(r[:24] + self._write_arrays(t[0], t[1], t[2], None))
        return self.blob.add(b"".join(rows), len(rows))

    def cameras(self):
        """Retail cameras (FoV track) -> 3.3.5 cameras (one fixed FoV)."""
        raw, n = self.m.raw("cameras", 116)
        rows = b""
        for i in range(n):
            r = raw[i * 116:(i + 1) * 116]
            ctype, far, near = struct.unpack_from("<Iff", r, 0)
            pos = self.convert_tracks(r[12:32], [(0, 36)])
            target = self.convert_tracks(r[44:64], [(0, 36)])
            roll = self.convert_tracks(r[76:96], [(0, 12)])
            fov = 0.7
            fov_track = self.read_track(r[96:116], 12)
            for times, values in fov_track[2]:
                if values:
                    fov = struct.unpack_from("<f", values)[0]
                    break
            rows += (struct.pack("<Ifff", ctype, fov, far, near) + pos + r[32:44] + target
                     + r[64:76] + roll)
        return self.blob.add(rows, n)

    def shows(self, submesh_id):
        """Retail creature geosets: ids below 100 always show; group g shows only g*100 + variant
        (variant 1 when the display doesn't say)."""
        group, variant = divmod(submesh_id, 100)
        return group == 0 or variant == self.geosets.get(group, 1)

    def drawn_batches(self):
        """Retail batches 3.3.5 can draw: first texture pass, shown geosets, known textures."""
        sk = self.skin
        tex_lookup = self.m.arr("texture_lookup", "<h")
        out = []
        for bt in sk.batches:
            sub, layer, tex_combo = bt[3], bt[7], bt[9]
            if layer != 0:
                continue  # retail extra passes (env/glow)
            if not self.shows(sk.submeshes[sub][0]):
                continue
            if tex_lookup[tex_combo + self.pick(bt)][0] in self.skipped_textures:
                continue
            out.append(bt)
        return out

    def pick(self, bt):
        """Which of a batch's textures to draw with, as an offset into its texture combo.
        3.3.5 gets one texture per batch here. On a solid surface that's the first (the diffuse).
        A blended effect mesh multiplies two, and when the first is only a mask (a soft white
        square over a water tile, say) the second is the one worth seeing."""
        count, tex_combo = bt[8], bt[9]
        if count < 2 or not self.mask_textures:
            return 0
        if self.m.arr("materials", "<HH")[bt[6]][1] < 2:
            return 0
        tex_lookup = self.m.arr("texture_lookup", "<h")
        if tex_combo + 1 >= len(tex_lookup):
            return 0
        first, second = tex_lookup[tex_combo][0], tex_lookup[tex_combo + 1][0]
        return 1 if first in self.mask_textures and second not in self.mask_textures else 0

    def batch_textures(self, geosets=None):
        """Indices of the textures the converted mesh draws with (first pass, shown geosets).
        The rest belong to particles, ribbons and extra passes, which don't make the trip."""
        self.geosets = geosets or {}
        tex_lookup = self.m.arr("texture_lookup", "<h")
        return {tex_lookup[bt[9] + self.pick(bt)][0] for bt in self.skin.batches
                if bt[7] == 0 and self.shows(self.skin.submeshes[bt[3]][0])}

    def split_submeshes(self, batches):
        """Rebuilds the skin's vertex map, indices and submeshes with only the submeshes those
        batches draw (hidden geosets and dropped passes go), and keeps every submesh's bone
        palette within MAX_PALETTE: 3.3.5 skins a submesh on the GPU with one palette, and the
        client's vertex shader holds about 75 bones. A bigger palette draws as streaks and
        crashes the client (seen on the 76-bone Haranir stag); retail palettes run past 160. Such
        a submesh is cut into pieces of whole triangles, each with its own palette.
        Returns (vertex map, indices, per-vertex palette indices, submeshes,
        {old submesh: [new submeshes]}, extra bone lookup entries)."""
        sk = self.skin
        lookup = [x[0] for x in self.m.arr("bone_lookup", "<H")]
        verts = self.m.arr("vertices", "<12x4B4B28x")
        if len(sk.vmap) > 0x10000:  # indices are 16 bits, so a vertex start can't carry into Level
            raise ValueError("%s: the retail skin has %d vertices" % (self.name, len(sk.vmap)))
        vmap, idx, bones, subs, extra, sub_map = [], [], bytearray(), [], [], {}
        for si in sorted({bt[3] for bt in batches}):
            sub = sk.submeshes[si]
            start = sub[4] + (sub[1] << 16)  # Level: the high bits of a big skin's index start
            tris = [sk.indices[start + 3 * t:start + 3 * t + 3] for t in range(sub[5] // 3)]
            if sub[6] <= MAX_PALETTE:
                groups = [(tris, None)]
            else:
                groups, cur, cur_set = [], [], set()
                for tri in tris:
                    tb = set()
                    for v in tri:
                        w, g = verts[sk.vmap[v]][:4], verts[sk.vmap[v]][4:]
                        tb.update(g[k] for k in range(4) if w[k])
                    if cur and len(cur_set | tb) > MAX_PALETTE:
                        groups.append((cur, cur_set))
                        cur, cur_set = [], set()
                    cur.append(tri)
                    cur_set |= tb
                if cur:
                    groups.append((cur, cur_set))
            sub_map[si] = []
            for tris_g, used in groups:
                piece = list(sub)
                piece[1] = 0  # Level: the rebuilt skin starts every submesh within 16 bits
                if used is None:
                    pos = None
                else:
                    palette = sorted(used)
                    pos = {bone: i for i, bone in enumerate(palette)}
                    piece[6], piece[7] = len(palette), len(lookup) + len(extra)
                    extra.extend(palette)
                remap, vstart = {}, len(vmap)
                for tri in tris_g:
                    for v in tri:
                        if v in remap:
                            continue
                        remap[v] = len(vmap)
                        vmap.append(sk.vmap[v])
                        if pos is None:
                            bones += sk.bones[4 * v:4 * v + 4]
                        else:
                            w, g = verts[sk.vmap[v]][:4], verts[sk.vmap[v]][4:]
                            bones += bytes(pos[g[k]] if w[k] else 0 for k in range(4))
                piece[2], piece[3] = vstart, len(remap)
                piece[4], piece[5] = len(idx), 3 * len(tris_g)
                idx.extend(remap[v] for tri in tris_g for v in tri)
                subs.append(piece)
                sub_map[si].append(len(subs) - 1)
        if len(vmap) > 0xFFFF or len(idx) > 0xFFFF:
            raise ValueError("%s: the skin overflows 16 bits (%d vertices, %d indices)"
                             % (self.name, len(vmap), len(idx)))
        return vmap, idx, bytes(bones), subs, sub_map, extra

    def convert_skin(self):
        out = bytearray(48)

        def add(payload, count):
            while len(out) % 16:
                out.append(0)
            off = len(out)
            out.extend(payload)
            return (count, off) if count else (0, 0)

        drawn = self.drawn_batches()
        vmap, idx, vbones, submeshes, sub_map, extra = self.split_submeshes(drawn)
        self.drawn_vertices = set(vmap)
        parts = [add(struct.pack("<%dH" % len(vmap), *vmap), len(vmap)),
                 add(struct.pack("<%dH" % len(idx), *idx), len(idx)),
                 add(vbones, len(vmap))]
        subs = b"".join(struct.pack("<HHHHHHHHHH3f3ff", *x) for x in submeshes)
        parts.append(add(subs, len(submeshes)))
        batches = b""
        nb = 0
        transforms = self.m.h["transform_lookup"][0]
        tex_lookup = self.tex_lookup_out or [x[0] for x in self.m.arr("texture_lookup", "<h")]
        materials = self.m.arr("materials", "<HH")
        self.texture_use = {}
        for bt in drawn:
            (flags, prio, shader, sub, geoset, color, mat, layer, count, tex_combo,
             uv_combo, weight_combo, transform_combo) = bt
            if self.pick(bt):  # the second texture, with its own scrolling
                tex_combo += 1
                if transform_combo + 1 < transforms:
                    transform_combo += 1
            blend = materials[mat][1]
            blend = blend if blend <= 6 else 4
            tex = tex_lookup[tex_combo]
            if blend == 1 and tex in self.key_combo:
                tex_combo = self.key_combo[tex]
                tex = tex_lookup[tex_combo]
            self.texture_use.setdefault(tex, set()).add(blend)
            for piece in sub_map[sub]:
                batches += struct.pack("<BbHHHhHHHHHHH", flags, prio, 0, piece, piece, color, mat, 0, 1,
                                       tex_combo, 0, weight_combo, transform_combo)
                nb += 1
        parts.append(add(batches, nb))
        max_bones = max([x[6] for x in submeshes] + [21])
        out[:48] = b"SKIN" + b"".join(struct.pack("<II", *p) for p in parts) + struct.pack("<I", max_bones)
        return bytes(out), extra
