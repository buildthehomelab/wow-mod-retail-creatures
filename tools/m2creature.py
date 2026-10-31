"""Convert a retail creature M2 (MD21, v272+) with its animations into a 3.3.5a model (MD20, v264)
plus 00.skin.

Kept: the skin-0 mesh, bones with every animation the 3.3.5 client knows (external .anim data is
inlined), attachments, key bones, colours, texture weights and transforms, footstep-style events,
cameras (the unit portrait), collision, and with effects on: particle emitters, ribbons, lights and
the retail glow and shine passes (see passes).
Dropped: sequences with animation ids newer than 3.3.5, emitters that spawn models, and retail
shader combos 3.3.5 has no pass for (a batch keeps its first texture).
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

# Effects. Retail's particle emitter is 3.3.5's plus 16 bytes of multi-texture parameters at the
# end (checked against the client's own phoenix and fire elemental); ribbons and lights are the
# same size in both. Offsets of the M2Tracks in each record, with their value sizes:
PARTICLE_SIZE, PARTICLE_RETAIL = 476, 492
PARTICLE_TRACKS = [(52, 4), (72, 4), (92, 4), (112, 4), (132, 4), (152, 4), (176, 4), (200, 4),
                   (220, 4), (240, 4), (456, 1)]
PARTICLE_RATE = 176
PARTICLE_BLOCKS = [(260, 12), (276, 2), (292, 8), (316, 2), (332, 2)]  # fixed16 times + values
PARTICLE_MULTI_TEXTURE = 0x10000000  # texture holds three 5-bit indices; 3.3.5 draws one
# Retail packs gravity into the float's 4 bytes (int8 x, int8 y, int16 z: a direction and a
# strength) where this flag says; 3.3.5 has only the downward pull, as a float. The scale is
# from emitters both clients have: old 0.6944 = retail z -16 (x, y 0).
PARTICLE_PACKED_GRAVITY, GRAVITY_SCALE = 0x800000, 0.04238648
RIBBON_SIZE = 176
RIBBON_TRACKS = [(36, 12), (56, 2), (76, 4), (96, 4), (132, 2), (152, 1)]
LIGHT_SIZE = 156
LIGHT_TRACKS = [(16, 12), (36, 4), (56, 12), (76, 4), (96, 4), (116, 4), (136, 1)]
# Tracks the client reads sequence 0 of when it sets an effect up, without checking there is one
# (an emitter with no emission rate arrays crashed it: ERROR #132 at 0x00833BEB). Every track but
# the enabled/visibility one; the client's own models never leave these without arrays.
PARTICLE_REQUIRED = {off for off, _size in PARTICLE_TRACKS if off != 456}
RIBBON_REQUIRED = {off for off, _size in RIBBON_TRACKS if off != 152}
LIGHT_REQUIRED = {off for off, _size in LIGHT_TRACKS if off != 136}

# Retail batches pick a shader by shader_id. 3.3.5 works one out from the texture count and the
# texture unit lookup (0 = first UV set, 1 = second, -1 = environment map), and draws one texture
# per batch here. For shader_id & 0x8000, the coordinates each texture reads, from retail's shader
# table (VS_Diffuse_T1_Env_T1 = UV 1, environment, UV 1):
UV1, UV2, ENV = 0, 1, -1
COMBO_COORDS = {
    0x00: (UV1, ENV), 0x01: (UV1, ENV), 0x02: (UV1, ENV), 0x03: (UV1, ENV, UV1), 0x04: (UV1, ENV),
    0x05: (UV1, UV1), 0x06: (UV1, UV1), 0x07: (UV1, ENV), 0x08: (UV1, ENV), 0x09: (UV1, ENV, UV1),
    0x0A: (UV1, UV1), 0x0B: (UV1, ENV), 0x0C: (UV1, ENV), 0x0D: (UV1, ENV), 0x0E: (UV1, UV1),
    0x0F: (UV1, UV2), 0x10: (UV1, ENV), 0x11: (UV1, ENV, UV1), 0x12: (UV1, UV1, UV1), 0x13: (UV1,),
    0x14: (UV1, ENV, UV2), 0x15: (UV1, UV2), 0x16: (UV1, UV1, UV1, UV2), 0x17: (UV1, UV1),
    0x18: (UV1, ENV, UV2), 0x19: (ENV,), 0x1A: (UV1, UV2, UV1), 0x1B: (UV1, UV2), 0x1C: (UV1, UV2, UV1),
    0x1D: (UV1, UV1),
}
# Combos that add a texture onto the first: {combo: (which texture, 3.3.5 blend mode)}. The
# AddAlpha family adds it by its own alpha (a glowing eye, a shine), which is exactly a second
# pass in blend mode 4 (the _Wgt ones scale it by its texture weight, which the pass keeps).
# Mod_Add and Mod_Add_Alpha add it whole, but onto a blended batch whose alpha still cuts the
# sum, so their pass is mode 4 too: mode 3 ignores alpha and draws a card's whole rectangle.
# Left off: the _Alpha variants (0x02, 0x07, 0x14), whose
# glow only shows where the first texture's alpha says, and the common 0x8000 (Mod2xNA_Alpha, an
# environment map multiplied in there): a pass of its own can't read another texture's alpha.
BLEND_ADD = 4
COMBO_GLOW = {0x01: 1, 0x03: 2, 0x04: 1, 0x05: 1, 0x06: 1, 0x0A: 1, 0x0B: 1, 0x0D: 1, 0x0E: 1, 0x0F: 1}
TEXUNITS = (UV1, UV2, ENV)  # the converted model's texture unit lookup, with effects on
NO_DEPTH_WRITE = 0x10  # material flag: a glow pass lies on its mesh and must not hide it


def chunks(data):
    out, o = {}, 0
    while o + 8 <= len(data):
        tag, size = struct.unpack_from("<4sI", data, o)
        out[tag.decode("latin-1")] = data[o + 8:o + 8 + size]
        o += 8 + size
    return out


def u32s(blob):
    return list(struct.unpack("<%dI" % (len(blob) // 4), blob))


def header_offsets():
    """{header array name: its offset in the MD21 chunk}."""
    out, o = {}, 8
    for name, kind in ARRAYS:
        out[name] = o
        o += 8 if kind == "a" else 4 if kind == "u" else struct.calcsize("<" + kind)
    return out


BONE_SIZE, BONE_TRACKS = 88, (16, 36, 56)
ATTACHMENT_SIZE, ATTACHMENT_TRACKS = 40, (20,)


def inline_skeleton(data, load):
    """A model whose bones, sequences and attachments are in a shared skeleton file (SKID: the
    Tauralus mounts) as one that has them itself. load(file data id) gives a file's bytes.

    The skeleton's chunks (SKS1 global loops, sequences and their lookup; SKB1 bones and key
    bone lookup; SKA1 attachments and their lookup) go after the model's own data, every offset
    in them moved by where they land: their arrays, the tracks' arrays, and the keyframes of
    the sequences stored in the skeleton (keyframes in .anim files stay where they are). A
    skeleton with a parent (SKPD) is the parent's rig with sequences of its own; it is taken
    as the parent, whose sequences are the full set, when every bone is the same in both (key
    bone, parent, submesh, name hash and pivot; the flags may differ).
    Skeletons whose animations are in .anim files are refused."""
    ch = chunks(data)
    if "SKID" not in ch:
        return data
    skel = chunks(load(struct.unpack_from("<I", ch["SKID"], 0)[0]))
    if "SKPD" in skel:
        parent = chunks(load(struct.unpack_from("<I", skel["SKPD"], 8)[0]))
        if "SKPD" in parent:
            raise ValueError("shared skeleton with a grandparent not supported")

        def rig(sk):
            n, off = struct.unpack_from("<II", sk["SKB1"], 0)
            b = sk["SKB1"]
            return [(b[off + i * BONE_SIZE:off + i * BONE_SIZE + 4], b[off + i * BONE_SIZE + 8:off + i * BONE_SIZE + 16],
                     b[off + i * BONE_SIZE + 76:off + (i + 1) * BONE_SIZE]) for i in range(n)]
        if rig(skel) != rig(parent):
            raise ValueError("shared skeleton whose bones differ from its parent's not supported")
        skel = parent
    if "AFID" in skel:
        raise ValueError("shared skeleton with .anim files not supported")
    md = bytearray(ch["MD21"])
    at = header_offsets()

    def put(blob):
        while len(md) % 16:
            md.append(0)
        base = len(md)
        md.extend(blob)
        return base

    def array(base, field, name):
        n, off = struct.unpack_from("<II", md, base + field)
        struct.pack_into("<II", md, at[name], n, base + off if n else 0)
        return n, base + off

    def tracks(base, n, rec_at, size, offsets, in_file):
        for i in range(n):
            for t in offsets:
                p = rec_at + i * size + t
                _interp, gseq, tsn, tso, vn, vo = struct.unpack_from(TRACK, md, p)
                struct.pack_into("<IIII", md, p + 4, tsn, base + tso if tsn else 0, vn, base + vo if vn else 0)
                for arr_n, arr_at in ((tsn, base + tso), (vn, base + vo)):
                    for k in range(arr_n):
                        cnt, off = struct.unpack_from("<II", md, arr_at + 8 * k)
                        if cnt and (gseq != -1 or in_file(k)):
                            struct.pack_into("<I", md, arr_at + 8 * k + 4, base + off)

    seqs = []
    if "SKS1" in skel:
        base = put(skel["SKS1"])
        array(base, 0, "global_loops")
        n, seq_at = array(base, 8, "sequences")
        seqs = [struct.unpack_from(SEQUENCE, md, seq_at + i * struct.calcsize(SEQUENCE)) for i in range(n)]
        array(base, 16, "sequence_lookup")

    def in_file(k):
        return k < len(seqs) and seqs[k][4] & SEQ_IN_M2
    if "SKB1" in skel:
        base = put(skel["SKB1"])
        n, bones_at = array(base, 0, "bones")
        array(base, 8, "key_bone_lookup")
        tracks(base, n, bones_at, BONE_SIZE, BONE_TRACKS, in_file)
    if "SKA1" in skel:
        base = put(skel["SKA1"])
        n, att_at = array(base, 0, "attachments")
        array(base, 8, "attachment_lookup")
        tracks(base, n, att_at, ATTACHMENT_SIZE, ATTACHMENT_TRACKS, in_file)
    out = b""
    for tag, blob in ch.items():
        if tag == "SKID":
            continue
        if tag == "MD21":
            blob = bytes(md)
        out += struct.pack("<4sI", tag.encode("latin-1"), len(blob)) + blob
    return out


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
        # Where the skin's vertices start in the model's: 0 on skin 0; a lower-detail skin
        # draws its own vertices, stored after those of the skins before it.
        start = struct.unpack_from("<I", data, 44)[0]
        self.vmap = [start + v for v in self._arr(a[0], "<H")]
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


class SkinOverflow(ValueError):
    """The mesh drawn needs more than the 16 bits a 3.3.5 skin counts in. Retail lists
    lower-detail skins after skin 0 (SFID); callers try the next one."""


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
    def __init__(self, model_data, skin_data, anim_files, valid_anim_ids, name, effects=False):
        """anim_files: {(anim id, variation): .anim bytes} for the model's AFID entries.
        effects: keep particles, ribbons, lights and the glow and shine passes."""
        self.m = Retail(model_data)
        self.skin = Skin(skin_data)
        self.name = name
        self.effects = effects
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
        self.hidden_sections = set()  # skin sections (submesh indices) the look leaves out
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
        if self.effects and tsn == 1 and len(self.seqs) > 1 and self.kept:
            # One array for every sequence: most retail tracks besides bones (particle rates,
            # colours, texture weights and scrolling). The 3.3.5 client reads such a track the
            # same way (its own emitters do it 11,299 times out of 12,347), so it stays one: spread
            # over the kept sequences, every sequence after the first fell to the default (an
            # emitter that only emits while standing).
            empty = (interp, gseq, [([], b"")])  # still one sequence's worth: colors() fills it
            buf = self.buffers[self.source[0]]
            n, off = struct.unpack_from("<II", md, tso)
            vn2, voff = struct.unpack_from("<II", md, vo) if value_size else (n, 0)
            if buf is None or n != vn2 or off + 4 * n > len(buf) or voff + value_size * n > len(buf):
                return empty  # its data was in the first sequence's .anim, which is gone
            times = list(struct.unpack_from("<%dI" % n, buf, off)) if n else []
            values = bytes(buf[voff:voff + value_size * n]) if n and value_size else b""
            if times != sorted(times):
                self.bad_tracks += 1
                return empty
            # Keys past the longest kept sequence are never reached: keep the ones that are, or
            # the first as a fixed value (like a global loop's).
            limit = max(self.seqs[i][2] for i in self.kept)
            if times and times[-1] > limit:
                keep = max(1, sum(1 for t in times if t <= limit))
                times = [min(t, limit) for t in times[:keep]]
                values = values[:value_size * keep]
            return (interp, gseq, [(times, values)])
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

    def convert_tracks(self, raw, layout, required=()):
        """Rewrite a record: layout = [(offset, value size) for each M2Track in it]. A track at
        an offset in required keeps one (empty) array when it has no keys."""
        out = bytearray(raw)
        for off, size in layout:
            t = self.read_track(raw[off:off + TRACK_SIZE], size)
            w = self.write_track(t, size)
            if off in required and not struct.unpack_from(TRACK, w)[2]:
                w = self._write_arrays(t[0], t[1], [([], b"")], size)
            out[off:off + TRACK_SIZE] = w
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
        retail has none. Alpha tracks stay as they are: a sequence without keys is opaque, which
        fits how retail keys them (fade in at spawn, fade out at death, nothing while standing).
        But an alpha track with no arrays at all, which retail writes for a colour that is never
        faded, gets one opaque key: the client's own models never leave one empty, and the
        Tuskarr Hermit Crab's body, drawn through two such colours, didn't show."""
        raw, n = self.m.raw("colors", 40)
        rows = b""
        for i in range(n):
            rec = raw[i * 40:(i + 1) * 40]
            interp, gseq, per = self.read_track(rec[:TRACK_SIZE], 12)
            if gseq == -1 and self.kept:
                rest = self.rest_value(rec[:TRACK_SIZE], 12) or struct.pack("<3f", 1.0, 1.0, 1.0)
                per = [(t, v) if t else ([0], rest) for t, v in per]
            rows += self.write_track((interp, gseq, per), 12)
            alpha = self.read_track(rec[TRACK_SIZE:], 2)
            if not any(t for t, _v in alpha[2]):
                alpha = (alpha[0], alpha[1], [([0], struct.pack("<h", 0x7FFF))])
            rows += self.write_track(alpha, 2)
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
        self.materials_out = [(f & materials_mask, bl if bl <= 6 else BLEND_ADD)
                              for f, bl in m.arr("materials", "<HH")]
        skin, extra_lookup = self.convert_skin()  # adds the glow passes' materials
        mats = self.materials_out
        h["materials"] = b.add(b"".join(struct.pack("<HH", *x) for x in mats), len(mats))
        lookup = [x[0] for x in m.arr("bone_lookup", "<H")] + extra_lookup
        h["bone_lookup"] = b.add(struct.pack("<%dH" % len(lookup), *lookup), len(lookup))
        h["texture_lookup"] = b.add(struct.pack("<%dh" % len(tex_lookup), *tex_lookup), len(tex_lookup))
        units = TEXUNITS if self.effects else (UV1,)
        h["texunit_lookup"] = b.add(struct.pack("<%dh" % len(units), *units), len(units))
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
        if self.effects:
            h["lights"] = self.lights()
            h["ribbons"] = self.ribbons()
            h["particles"] = self.particles()

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
        (variant 1 when the display doesn't say). A customizable model (a ChrModel) sets group 0
        too: then only id 0 and the chosen one of ids below 100 show."""
        group, variant = divmod(submesh_id, 100)
        if group == 0:
            return 0 not in self.geosets or variant in (0, self.geosets[0])
        return variant == self.geosets.get(group, 1)

    def drawn_batches(self):
        """Retail batches 3.3.5 can draw: first texture pass, shown geosets, known textures."""
        sk = self.skin
        tex_lookup = self.m.arr("texture_lookup", "<h")
        out = []
        for bt in sk.batches:
            sub, layer, tex_combo = bt[3], bt[7], bt[9]
            if layer != 0 and not self.effects:
                continue  # retail extra passes (env/glow)
            if sub in self.hidden_sections or not self.shows(sk.submeshes[sub][0]):
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

    def coords(self, shader, count, k):
        """The coordinates (UV1, UV2 or ENV) texture k of a retail batch reads, by its shader_id
        (retail's M2GetVertexShaderID, and COMBO_COORDS for the combos)."""
        if shader & 0x8000:
            c = COMBO_COORDS.get(shader & 0x7FFF, ())
            return c[k] if k < len(c) else UV1
        if k == 0:
            if shader & 0x80:
                return ENV
            return UV2 if count == 1 and shader & 0x4000 else UV1
        if shader & 0x8:
            return ENV
        return UV2 if shader & 0x4000 else UV1

    def passes(self, bt):
        """The 3.3.5 batches a retail batch becomes: [(texture offset in its combo, coordinates,
        blend mode or None for the batch's own material, material layer)]. The first draws one
        texture (see pick). With effects on it reads the coordinates retail's shader gives it (an
        environment-mapped shine moves with the camera), and a combo that adds a glowing texture
        onto the first gets that texture as an additive pass of its own, a layer up."""
        shader, layer, count = bt[2], bt[7], bt[8]
        k = self.pick(bt)
        if not self.effects:
            return [(k, UV1, None, 0)]
        out = [(k, self.coords(shader, count, k), None, layer)]
        if shader & 0x8000:
            glow = COMBO_GLOW.get(shader & 0x7FFF)
        elif count >= 2 and shader & 7 in (3, 7):  # Opaque_AddAlpha, Mod_Add, Mod_AddNA
            glow = 1
        else:
            glow = None
        # Retail cuts the sum by the first texture's alpha; a pass of its own isn't cut. So only
        # over an opaque batch, or a blended one whose glow is its own texture (the same alpha).
        # Never over alpha key: it would glow in the holes, and blended use of a keyed texture
        # stops its ALPHA_KEY stretch (build_patch.py).
        if glow and not k and glow < count:
            blend = self.m.arr("materials", "<HH")[bt[6]][1]
            tex_lookup = self.m.arr("texture_lookup", "<h")
            own = bt[9] + glow < len(tex_lookup) and tex_lookup[bt[9] + glow] == tex_lookup[bt[9]]
            if blend == 0 or (blend >= 2 and own):  # (retail's 7 is blended too)
                out.append((glow, self.coords(shader, count, glow), BLEND_ADD, layer + 1))
        return out

    def batch_textures(self, geosets=None, hidden=()):
        """Indices of the textures the converted model draws with (shown geosets): the first pass,
        and with effects on the extra passes, glows, particles and ribbons too. hidden: texture
        indices the look leaves out (their batches go)."""
        self.geosets = geosets or {}
        tex_lookup = [x[0] for x in self.m.arr("texture_lookup", "<h")]
        shown = [bt for bt in self.skin.batches
                 if (bt[7] == 0 or self.effects) and bt[3] not in self.hidden_sections
                 and self.shows(self.skin.submeshes[bt[3]][0]) and tex_lookup[bt[9] + self.pick(bt)] not in hidden]
        skip = self.glow_skips(shown, tex_lookup)
        out = {tex_lookup[bt[9] + k] for bt in shown for k, _c, glow, _l in self.passes(bt)
               if bt[9] + k < len(tex_lookup) and (glow is None or tex_lookup[bt[9] + k] not in skip)}
        return out | self.effect_textures()

    def glow_skips(self, batches, tex_lookup):
        """Textures a glow pass leaves alone: one the model alpha-keys (a blended use stops its
        ALPHA_KEY stretch in build_patch.py), and a creature skin slot no first pass draws (it
        would take the slot an alpha-key copy may need)."""
        mats = self.m.arr("materials", "<HH")
        base, keyed = set(), set()
        for bt in batches:
            at = bt[9] + self.pick(bt)
            if at < len(tex_lookup):
                base.add(tex_lookup[at])
                if mats[bt[6]][1] == 1:
                    keyed.add(tex_lookup[at])
        types = [t[0] for t in self.m.arr("textures", "<IIII")]
        return keyed | {i for i, t in enumerate(types) if 11 <= t <= 13 and i not in base}

    # --- effects ------------------------------------------------------------------------
    def emitters(self):
        """The retail particle emitters 3.3.5 can draw: plane, sphere and spline ones that spawn
        no model, of a geoset shown (PGD1 ties an emitter to a geoset, 0 for none)."""
        if not self.effects:
            return []
        raw, n = self.m.raw("particles", PARTICLE_RETAIL)
        geo = []
        if "PGD1" in self.m.chunks:
            pgd = self.m.chunks["PGD1"]
            cnt, at = struct.unpack_from("<II", pgd, 0)
            geo = struct.unpack_from("<%dH" % cnt, pgd, at)
        types = [t[0] for t in self.m.arr("textures", "<IIII")]
        out = []
        for i in range(n):
            r = raw[i * PARTICLE_RETAIL:(i + 1) * PARTICLE_RETAIL]
            model, recursion = struct.unpack_from("<I", r, 24)[0], struct.unpack_from("<I", r, 32)[0]
            if not 1 <= r[41] <= 3 or model > 1 or recursion > 1:
                continue
            if i < len(geo) and geo[i] and not self.shows(geo[i]):
                continue
            tex = self.particle_texture(i)
            if tex < len(types) and 11 <= types[tex] <= 13:
                continue  # none of the client's own emitters draws a creature skin slot
            rate = self.read_track(r[PARTICLE_RATE:PARTICLE_RATE + TRACK_SIZE], 4)
            if not any(t for t, _v in rate[2]):
                continue  # emits only in animations 3.3.5 lacks (the owl's skyriding trail)
            out.append(i)
        return out

    def particle_texture(self, i):
        raw, _n = self.m.raw("particles", PARTICLE_RETAIL)
        flags, = struct.unpack_from("<I", raw, i * PARTICLE_RETAIL + 4)
        tex, = struct.unpack_from("<H", raw, i * PARTICLE_RETAIL + 22)
        return tex & 0x1F if flags & PARTICLE_MULTI_TEXTURE else tex

    def ribbon_textures(self, i):
        raw, _n = self.m.raw("ribbons", RIBBON_SIZE)
        n, at = struct.unpack_from("<II", raw, i * RIBBON_SIZE + 20)
        return list(struct.unpack_from("<%dH" % n, self.m.md, at))

    def effect_textures(self):
        """Textures the kept particle emitters and ribbons draw with."""
        if not self.effects:
            return set()
        out = {self.particle_texture(i) for i in self.emitters()}
        for i in range(self.m.h["ribbons"][0]):
            out.update(self.ribbon_textures(i)[:1])
        return out

    def relocate(self, rec, off, size):
        """Copy an M2Array of a record (records point into the model, not into .anim files)."""
        n, at = struct.unpack_from("<II", rec, off)
        return self.blob.add(self.m.md[at:at + n * size], n)

    def particles(self):
        """3.3.5 emitters: the record without retail's multi-texture tail, one texture (the
        first of a multi-texture emitter's three), retail's blend mode 7 as additive, and a
        particle colour index only where it means the display's ParticleColor (11-13)."""
        raw, _n = self.m.raw("particles", PARTICLE_RETAIL)
        rows, n = b"", 0
        for i in self.emitters():
            tex = self.particle_texture(i)
            if tex in self.skipped_textures or tex >= self.m.h["textures"][0]:
                continue
            src = raw[i * PARTICLE_RETAIL:i * PARTICLE_RETAIL + PARTICLE_SIZE]
            r = bytearray(self.convert_tracks(src, PARTICLE_TRACKS, PARTICLE_REQUIRED))
            flags, = struct.unpack_from("<I", r, 4)
            if flags & PARTICLE_PACKED_GRAVITY:
                self.unpack_gravity(r)
            struct.pack_into("<I", r, 4, flags & ~(PARTICLE_MULTI_TEXTURE | PARTICLE_PACKED_GRAVITY))
            struct.pack_into("<H", r, 22, tex)
            r[44:46] = b"\0\0"  # retail's multi-texture settings; 3.3.5's particle type and head/tail
            if r[40] > 6:
                r[40] = BLEND_ADD
            if not 11 <= struct.unpack_from("<H", r, 42)[0] <= 13:
                struct.pack_into("<H", r, 42, 0)
            for off in (24, 32):  # model names: empty, as "" like the client's own
                struct.pack_into("<II", r, off, *self.blob.add(b"\0", 1))
            struct.pack_into("<II", r, 448, *self.relocate(src, 448, 12))
            for off, size in PARTICLE_BLOCKS:
                struct.pack_into("<II", r, off, *self.relocate(src, off, 2))
                struct.pack_into("<II", r, off + 8, *self.relocate(src, off + 8, size))
            rows += bytes(r)
            n += 1
        return self.blob.add(rows, n)

    def unpack_gravity(self, r):
        """Rewrites a converted emitter's gravity track (offset 132) from retail's packed vectors
        to 3.3.5's downward pull: the vector's z, negated. A sideways part is lost."""
        _i, _g, tn, to, vn, vo = struct.unpack_from(TRACK, r, 132)
        data = self.blob.data
        for k in range(vn):
            n, at = struct.unpack_from("<II", data, vo + 8 * k)
            for j in range(n):
                x, y, z = struct.unpack_from("<bbh", data, at + 4 * j)
                up = max(0.0, 1.0 - (x / 128.0) ** 2 - (y / 128.0) ** 2) ** 0.5
                struct.pack_into("<f", data, at + 4 * j, -up * z * GRAVITY_SCALE)

    def ribbons(self):
        """3.3.5 ribbons: one texture and one material each, as every one of the client's own
        has. Retail blends up to three textures into a ribbon (a shape and scrolling masks);
        3.3.5 draws the first, the shape, and the rest would be drawn as passes of their own
        with materials past the ribbon's one (the pygmy owl's hard white wing streaks)."""
        raw, n = self.m.raw("ribbons", RIBBON_SIZE)
        rows, kept = b"", 0
        for i in range(n):
            if self.ribbon_textures(i)[0] in self.skipped_textures:
                continue
            src = raw[i * RIBBON_SIZE:(i + 1) * RIBBON_SIZE]
            r = bytearray(self.convert_tracks(src, RIBBON_TRACKS, RIBBON_REQUIRED))
            for off in (20, 28):  # textures, materials
                _n, at = struct.unpack_from("<II", src, off)
                struct.pack_into("<II", r, off, *self.blob.add(self.m.md[at:at + 2], 1))
            r[174:176] = b"\0\0"  # retail's colour index and transform: padding in 3.3.5
            rows += bytes(r)
            kept += 1
        return self.blob.add(rows, kept)

    def lights(self):
        raw, n = self.m.raw("lights", LIGHT_SIZE)
        rows = b"".join(self.convert_tracks(raw[i * LIGHT_SIZE:(i + 1) * LIGHT_SIZE], LIGHT_TRACKS,
                                            LIGHT_REQUIRED)
                        for i in range(n))
        return self.blob.add(rows, n)

    def material(self, flags, blend):
        """Index of a material in the converted model, added when it has none like it."""
        if (flags, blend) not in self.materials_out:
            self.materials_out.append((flags, blend))
        return self.materials_out.index((flags, blend))

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
            raise SkinOverflow("%s: the retail skin has %d vertices" % (self.name, len(sk.vmap)))
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
            raise SkinOverflow("%s: the skin overflows 16 bits (%d vertices, %d indices)"
                               % (self.name, len(vmap), len(idx)))
        if vmap and max(vmap) > 0xFFFF:
            raise SkinOverflow("%s: the skin draws vertex %d, past 16 bits" % (self.name, max(vmap)))
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
        weights = self.m.h["weight_lookup"][0]
        tex_lookup = self.tex_lookup_out or [x[0] for x in self.m.arr("texture_lookup", "<h")]
        self.texture_use = {}  # the mesh's batches only: particles read an ALPHA_KEY stretch fine
        skip = self.glow_skips(drawn, tex_lookup)
        for bt in drawn:
            (flags, prio, shader, sub, geoset, color, mat, layer, count, tex_combo,
             uv_combo, weight_combo, transform_combo) = bt
            for k, unit, glow_blend, out_layer in self.passes(bt):
                combo, transform, weight, material = tex_combo + k, transform_combo, weight_combo, mat
                if k and transform_combo + k < transforms:  # a later texture, with its own scrolling
                    transform += k
                if glow_blend is not None and weight_combo + k < weights:  # and its own weight
                    weight += k
                if combo >= len(tex_lookup):
                    continue
                if glow_blend is not None:
                    if tex_lookup[combo] in self.skipped_textures or tex_lookup[combo] in skip:
                        continue
                    material = self.material(self.materials_out[mat][0] | NO_DEPTH_WRITE, glow_blend)
                blend = self.materials_out[material][1]
                tex = tex_lookup[combo]
                if blend == 1 and tex in self.key_combo:
                    combo = self.key_combo[tex]
                    tex = tex_lookup[combo]
                self.texture_use.setdefault(tex, set()).add(blend)
                for piece in sub_map[sub]:
                    batches += struct.pack("<BbHHHhHHHHHHH", flags, prio, 0, piece, piece, color, material,
                                           out_layer, 1, combo, TEXUNITS.index(unit), weight, transform)
                    nb += 1
        parts.append(add(batches, nb))
        max_bones = max([x[6] for x in submeshes] + [21])
        out[:48] = b"SKIN" + b"".join(struct.pack("<II", *p) for p in parts) + struct.pack("<I", max_bones)
        return bytes(out), extra
