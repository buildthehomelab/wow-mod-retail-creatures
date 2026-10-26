#!/usr/bin/env python3
"""Build a client patch: every look in a pack's data file as a 3.3.5a creature display.

    python3 build_patch.py --pack <pack dir> --dbc <dir> --cache <download dir> --work <dir> \\
        --out patch-R.MPQ [--sql forms.sql] [--max-texture 1024]

--pack is a folder with a pack.json: the checkout of a pack repo, such as
wow-mod-retail-druid-forms (patch-R) or wow-mod-retail-shaman-totems (patch-V). It names the
pack's data file and id blocks; the README lists every field.

--dbc holds the CreatureDisplayInfo.dbc, CreatureModelData.dbc and AnimationData.dbc the realm's
players already have. A patch MPQ replaces whole files, so if another patch the players use ships
those DBCs (an HD creature pack, the other pack's patch), pass its copies, or its creatures lose
their textures.

Each retail model is downloaded from wago.tools with its skin, external animations and textures,
converted by m2creature.py (one copy per geoset combination a look needs) and stored under the
pack's folder. Each look gets a CreatureDisplayInfo row (id from the data file) on a
CreatureModelData row cloned from the stock model it replaces, so sounds and footprints stay the
same. --sql writes the mod-transmog-plus rows (mod_transmog_plus_forms + preview creatures).

A look can also be a display the client already has, at another size or with other textures
(client_display, display_scale and textures in the data file): it gets a CreatureDisplayInfo row copied
from the client's, on the client's own model, and no model is converted.
"""
import argparse
import collections
import ctypes
import hashlib
import json
import os
import struct
import sys

import fetch
from check_m2 import check
from fetch import casc, customization_textures, db2, db2_rows, listfile
from m2creature import Converter, Retail, Skin, SkinOverflow, StaleSkin, inline_skeleton, skin_mismatches

# A pack's pack.json: its data file, where its models go in the client, its id blocks and the
# stock display each kind of look clones for sounds, blood, footprints and size class.
#   looks           the data file, relative to the pack folder
#   root            folder in the client for the pack's models
#   first_display   CreatureDisplayInfo ids of the ported looks start here
#   first_model     CreatureModelData ids (ids the base client uses move past the block)
#   preview_base    creature_template entries for the addon's previews (500 per pack); a look's
#                   entry is base + (display - first_display) unless its row has a preview column
#   subname         subname of those preview creatures
#   sort_base       SortOrder of the pack's first look in mod_transmog_plus_forms
#   prune_textures  leave out textures only the dropped particles and extra passes used, and
#                   draw a two-texture effect mesh with its second texture when the first is
#                   only a mask (MASK_WORDS in its file name)
#   template        {kind of look: stock CreatureDisplayInfo id it clones}
#   source          where the looks come from, for the head of the generated SQL
# Optional, for a pack that is a library of models and not a set of looks to pick in a window:
#   transmog        false: no rows for mod-transmog-plus and no preview creatures (preview_base,
#                   subname and sort_base can then be left out); the SQL is the DBC rows only
#   plain           true: the new rows carry no sounds and no footprints. Hundreds of unlike
#                   models can't all sound like the one stock creature their kind clones
#   archives        {kind of look: MPQ file name}: with --data-dir, each kind's models and textures
#                   go into their own archive there, and --out holds the two DBCs only. The big
#                   archives then never change when another patch's DBC rows do
#   effects         true: keep particle emitters, ribbons, lights and the glow and shine passes
#                   (see m2creature.py). Looks whose emitters take the display's particle colours
#                   get their retail ParticleColor rows, added to the ParticleColor.dbc in --dbc
#                   (the client's own copy, or the one a patch before this one ships)
PACK_FIELDS = ("looks", "root", "first_display", "first_model", "prune_textures", "template", "source")
TRANSMOG_FIELDS = ("preview_base", "subname", "sort_base")


def load_pack(folder):
    path = os.path.join(folder, "pack.json")
    if not os.path.isfile(path):
        sys.exit("no pack.json in %s: --pack takes a pack repo's folder" % folder)
    with open(path, encoding="utf-8") as f:
        pack = json.load(f)
    pack.setdefault("transmog", True)
    missing = [k for k in PACK_FIELDS + (TRANSMOG_FIELDS if pack["transmog"] else ()) if k not in pack]
    if missing:
        sys.exit("%s: missing %s" % (path, ", ".join(missing)))
    pack["folder"] = folder
    return pack


MASK_WORDS = ("mask", "square_soft", "gradient")

# The last CreatureModelData id of the stock 3.3.5a client, which is what the server has. A model
# row past it that a look uses is an HD pack's, and has to be given to the server too.
STOCK_MAX_MODEL = 3440

# The server's creaturemodeldata_dbc.ModelName is varchar(100): a longer model path stops its
# database import, and the worldserver with it.
MODEL_PATH_MAX = 100


# --- DBC ---------------------------------------------------------------------------------------

def read_dbc(path):
    data = open(path, "rb").read()
    magic, count, fields, size, strsize = struct.unpack_from("<4s4I", data, 0)
    assert magic == b"WDBC" and size == fields * 4, path
    rows = [list(struct.unpack_from("<%dI" % fields, data, 20 + i * size)) for i in range(count)]
    strings = bytearray(data[20 + count * size:20 + count * size + strsize])
    return rows, strings, fields


def write_dbc(path, rows, strings, fields):
    rows = sorted(rows, key=lambda r: r[0])
    with open(path, "wb") as f:
        f.write(struct.pack("<4s4I", b"WDBC", len(rows), fields, fields * 4, len(strings)))
        for r in rows:
            f.write(struct.pack("<%dI" % fields, *r))
        f.write(strings)


def add_string(strings, text):
    at = len(strings)
    strings.extend(text.encode("utf-8") + b"\0")
    return at


def f32(v):
    return struct.unpack("<I", struct.pack("<f", float(v)))[0]


def row_of(rows, row_id):
    for r in rows:
        if r[0] == row_id:
            return list(r)
    sys.exit("row %d missing from the base DBC" % row_id)


def texture_names_of(row, strings):
    """A CreatureDisplayInfo row's three texture names."""
    return [strings[row[6 + k]:strings.index(b"\0", row[6 + k])].decode("latin-1") if row[6 + k] else ""
            for k in range(3)]


# --- textures ----------------------------------------------------------------------------------

def shrink_blp(data, max_size):
    """Drop top mip levels of a mipmapped BLP2 until it fits max_size (lossless for the rest)."""
    if data[:4] != b"BLP2" or not max_size:
        return data
    ctype, enc, adepth, atype, mips = struct.unpack_from("<I4B", data, 4)
    w, h = struct.unpack_from("<II", data, 12)
    offs = list(struct.unpack_from("<16I", data, 20))
    sizes = list(struct.unpack_from("<16I", data, 84))
    drop = 0
    while max(w >> drop, h >> drop) > max_size and drop + 1 < 16 and sizes[drop + 1]:
        drop += 1
    if not drop or not mips:
        return data
    header = 148 + (1024 if enc == 1 else 0)
    body, new_offs, new_sizes, at = bytearray(), [], [], header
    for i in range(drop, 16):
        if not sizes[i]:
            break
        body += data[offs[i]:offs[i] + sizes[i]]
        new_offs.append(at)
        new_sizes.append(sizes[i])
        at += sizes[i]
    new_offs += [0] * (16 - len(new_offs))
    new_sizes += [0] * (16 - len(new_sizes))
    out = bytearray(data[:header])
    struct.pack_into("<II", out, 12, max(1, w >> drop), max(1, h >> drop))
    struct.pack_into("<16I", out, 20, *new_offs)
    struct.pack_into("<16I", out, 84, *new_sizes)
    return bytes(out + body)


# Alpha key (blend mode 1) cuts at a different alpha in each client. Retail keeps a texel from
# alpha 128; the 3.3.5 client only from 224. Retail fur, feathers and leaves have most of their
# alpha in between, so in 3.3.5 they come out shredded (the Chaos-Forged Gryphon's wings).
# A texture alpha-key batches draw gets its alpha stretched so 128 lands on 224 (0-127 onto
# 0-223, 128-255 onto 224-255): the same texels pass. Blended batches read alpha as opacity, so
# a texture both kinds draw gets a separate copy for the alpha-key ones (key_copies in convert).
ALPHA_KEY = (128, 224)


def key_alpha_value(a):
    retail, client = ALPHA_KEY
    if a < retail:
        return min(client - 1, (a * client + retail // 2) // retail)
    return client + ((a - retail) * (255 - client) + (255 - retail) // 2) // (255 - retail)


KEY_ALPHA = bytes(key_alpha_value(a) for a in range(256))


def key_alpha_nibbles():
    """4-bit alpha (DXT3, 4-bit palette alpha): the stretched value, kept on its side of the cut."""
    out = []
    for v in range(16):
        q = (KEY_ALPHA[v * 17] + 8) // 17
        if v * 17 >= ALPHA_KEY[0] and q * 17 < ALPHA_KEY[1]:
            q += 1
        elif v * 17 < ALPHA_KEY[0] and q * 17 >= ALPHA_KEY[1]:
            q -= 1
        out.append(q)
    return out


KEY_NIBBLES = key_alpha_nibbles()


def dxt5_values(a0, a1):
    if a0 > a1:
        return [a0, a1] + [((7 - k) * a0 + k * a1) // 7 for k in range(1, 7)]
    return [a0, a1] + [((5 - k) * a0 + k * a1) // 5 for k in range(1, 5)] + [0, 255]


_dxt5_cache = {}


def dxt5_key_block(a0, a1):
    """New endpoints and {old index: new index} for a DXT5 alpha block with these endpoints."""
    hit = _dxt5_cache.get((a0, a1))
    if hit:
        return hit
    old = dxt5_values(a0, a1)
    n0, n1 = KEY_ALPHA[a0], KEY_ALPHA[a1]
    if a0 > a1 and n0 == n1:  # the stretch can merge close endpoints: then all eight are one value
        n1 = n0 - 1 if n0 else 0
        n0 = max(n0, n1 + 1)
    new = dxt5_values(n0, n1)
    remap = []
    for v in old:
        target, side = KEY_ALPHA[v], v >= ALPHA_KEY[0]
        same = [j for j in range(8) if (new[j] >= ALPHA_KEY[1]) == side] or range(8)
        remap.append(min(same, key=lambda j: abs(new[j] - target)))
    _dxt5_cache[(a0, a1)] = hit = (n0, n1, remap)
    return hit


def key_alpha_blp(data):
    """The BLP2 with every mip's alpha stretched for 3.3.5's alpha key (see ALPHA_KEY)."""
    if data[:4] != b"BLP2":
        return data
    enc, adepth, atype = struct.unpack_from("<3B", data, 8)
    w, h = struct.unpack_from("<II", data, 12)
    offs = struct.unpack_from("<16I", data, 20)
    sizes = struct.unpack_from("<16I", data, 84)
    out = bytearray(data)
    for i in range(16):
        if not sizes[i]:
            break
        mw, mh = max(1, w >> i), max(1, h >> i)
        o, n = offs[i], sizes[i]
        if enc == 2 and atype == 7:  # DXT5
            for b in range(o, o + n - 15, 16):
                n0, n1, remap = dxt5_key_block(out[b], out[b + 1])
                bits = int.from_bytes(out[b + 2:b + 8], "little")
                nb = 0
                for p in range(16):
                    nb |= remap[(bits >> (3 * p)) & 7] << (3 * p)
                out[b], out[b + 1] = n0, n1
                out[b + 2:b + 8] = nb.to_bytes(6, "little")
        elif enc == 2 and atype == 1:  # DXT3: 4-bit alpha
            for b in range(o, o + n - 15, 16):
                for k in range(8):
                    v = out[b + k]
                    out[b + k] = KEY_NIBBLES[v & 15] | KEY_NIBBLES[v >> 4] << 4
        elif enc == 1 and adepth == 8:  # palette, then one alpha byte per texel
            a = o + mw * mh
            out[a:a + mw * mh] = bytes(out[a:a + mw * mh]).translate(KEY_ALPHA)
        elif enc == 1 and adepth == 4:
            a = o + mw * mh
            for k in range(a, a + (mw * mh + 1) // 2):
                v = out[k]
                out[k] = KEY_NIBBLES[v & 15] | KEY_NIBBLES[v >> 4] << 4
        elif enc == 3:  # BGRA
            out[o + 3:o + n:4] = bytes(out[o + 3:o + n:4]).translate(KEY_ALPHA)
        # DXT1 and 1-bit alpha are all or nothing already.
    return bytes(out)


# --- MPQ ---------------------------------------------------------------------------------------

def pack_mpq(out, files):
    lib = ctypes.CDLL(os.environ.get("STORMLIB", "/usr/local/lib/libstorm.dylib"))
    H = ctypes.c_void_p
    lib.SFileCreateArchive.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(H)]
    lib.SFileAddFileEx.argtypes = [H, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    lib.SFileCloseArchive.argtypes = [H]
    for fn in (lib.SFileCreateArchive, lib.SFileAddFileEx, lib.SFileCloseArchive):
        fn.restype = ctypes.c_bool
    if os.path.exists(out):
        os.remove(out)
    h = H()
    # With a listfile and attributes. Past 2 GB of files the archive is format 2, like every big
    # archive the client ships with: format 1 keeps 32-bit offsets, and StormLib itself no longer
    # lists a format 1 archive whose tables lie past 2 GB by name.
    big = sum(os.path.getsize(src) for src, _name in files) > 0x7FFFFFFF
    if not lib.SFileCreateArchive(out.encode(), 0x00300000 | (0x01000000 if big else 0), max(64, len(files) * 2),
                                  ctypes.byref(h)):
        sys.exit("can't create " + out)
    for src, name in files:
        if not lib.SFileAddFileEx(h, src.encode(), name.encode(), 0x80000200, 0x02, 0x02):
            lib.SFileCloseArchive(h)
            os.remove(out)
            sys.exit("can't add " + name)
    if not lib.SFileCloseArchive(h):
        os.remove(out)
        sys.exit("can't finish " + out)


# --- build -------------------------------------------------------------------------------------

def load_looks(pack):
    path = os.path.join(pack["folder"], pack["looks"])
    lines = open(path, encoding="utf-8").read().splitlines()
    head = lines[0].split("\t")
    return [dict(zip(head, l.split("\t"))) for l in lines[1:]]


def load_model(cache, fdid, stem, warnings):
    """A retail model with its skins and animations: skins(n) is skin n (retail lists
    lower-detail skins after skin 0). wago.tools sometimes serves a skin made for another
    version of the model (it mixes builds); such a model is taken from Blizzard's CDN instead,
    model, skins and animations together, since they have to come from one build."""
    for source in (None, "cdn"):
        raw = inline_skeleton(casc(cache, fdid, source), lambda f: casc(cache, f, source))
        m = Retail(raw)
        skin_raw = casc(cache, m.sfid[0], source)
        if not skin_mismatches(m, Skin(skin_raw)):
            break
        if source == "cdn" or fetch.SOURCE == "cdn":
            raise StaleSkin("%s: the skin was made for another version of the model" % stem)
        warnings.append("%s: wago.tools served a skin made for another version of the model: "
                        "model, skin and animations taken from Blizzard's CDN" % stem)
    anims = {(a, s): casc(cache, f, source) for a, s, f in m.afid if f}
    return raw, m, lambda n: casc(cache, m.sfid[n], source) if n else skin_raw, anims


class ParticleColors:
    """The base ParticleColor.dbc and the retail rows the looks need. A particle emitter with
    colour index 11-13 takes its colours from its display's ParticleColor row. Retail's ids are
    the client's for the rows both have (281-586), so a retail row keeps its id, or takes a base
    row with the same colours; only an id the base gives other colours moves past the last.
    New rows go at the end and the base's own stay in their order (see write)."""

    def __init__(self, dbc_dir, cache):
        path = os.path.join(dbc_dir, "ParticleColor.dbc")
        self.rows = read_dbc(path) if os.path.isfile(path) else None
        self.retail = db2(cache, "ParticleColor")
        self.added = []
        self.got = {}  # retail id: the id the looks use

    def row_for(self, retail_id, display, warnings):
        if not int(retail_id or 0):
            return 0
        if self.rows is None:
            warnings.append("%d: no ParticleColor.dbc in --dbc: its particle colours are left off" % display)
            return 0
        if retail_id not in self.got:
            r = self.retail.get(retail_id)
            if not r:
                warnings.append("%d: retail ParticleColor %s is missing" % (display, retail_id))
                return 0
            values = [int(r["%s_%d" % (k, i)]) & 0xFFFFFFFF for k in ("Start", "MID", "End") for i in range(3)]
            rows, want = self.rows[0], int(retail_id)
            base = {x[0]: x[1:] for x in rows}
            same = [i for i, v in base.items() if v == values]
            if base.get(want) == values:
                got = want
            elif same:
                got = same[0]
            else:
                got = want if want not in base else max(base) + 1
                rows.append([got] + values)
                self.added.append(got)
            self.got[retail_id] = got
        return self.got[retail_id]

    def write(self, path):
        """Rows as they are: write_dbc sorts by id, and a client table read in file order
        changes with that (see SkillRaceClassInfo)."""
        rows, strings, fields = self.rows
        with open(path, "wb") as f:
            f.write(struct.pack("<4s4I", b"WDBC", len(rows), fields, fields * 4, len(strings)))
            for r in rows:
                f.write(struct.pack("<%dI" % fields, *r))
            f.write(strings)


def folder_name(model):
    return "".join(p[:1].upper() + p[1:] for p in model.split("_"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbc", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pack", default=".", help="the pack's folder (holds pack.json); default: here")
    ap.add_argument("--sql")
    ap.add_argument("--max-texture", type=int, default=1024)
    ap.add_argument("--only", help="comma-separated display ids (test builds)")
    ap.add_argument("--data-dir", help="for a pack with archives: write them here, and only the DBCs to --out")
    ap.add_argument("--dbc-only", action="store_true",
                    help="pack only the two DBCs: a small patch that loads after the full one for "
                         "clients whose own patch (an HD creature pack) ships these DBCs; build it "
                         "from that patch's copies with --dbc")
    args = ap.parse_args()
    os.makedirs(args.work, exist_ok=True)
    pack = load_pack(args.pack)

    looks = load_looks(pack)
    if args.only:
        only = {int(x) for x in args.only.split(",")}
        looks = [l for l in looks if int(l["display"]) in only]
    cdi_r = db2(args.cache, "CreatureDisplayInfo")
    cmd_r = db2(args.cache, "CreatureModelData")
    geo = collections.defaultdict(dict)
    for g in db2_rows(args.cache, "CreatureDisplayInfoGeosetData"):
        geo[g["CreatureDisplayInfoID"]][int(g["GeosetIndex"]) + 1] = int(g["GeosetValue"])
    custom = customization_textures(args.cache)

    valid = {r[0] for r in read_dbc(os.path.join(args.dbc, "AnimationData.dbc"))[0]}
    cdi_rows, cdi_str, cdi_fields = read_dbc(os.path.join(args.dbc, "CreatureDisplayInfo.dbc"))
    cmd_rows, cmd_str, cmd_fields = read_dbc(os.path.join(args.dbc, "CreatureModelData.dbc"))
    taken = {r[0] for r in cdi_rows}
    colors = ParticleColors(args.dbc, args.cache) if pack.get("effects") else None
    if colors and colors.rows:
        # A patch that ships ParticleColor.dbc replaces the whole table: built on the client's
        # copy instead of the one a patch before it ships, it drops that patch's rows.
        have = {r[0] for r in colors.rows[0]}
        lost = sorted({r[13] for r in cdi_rows if r[13] and r[13] not in have})
        if lost:
            sys.exit("CreatureDisplayInfo.dbc in --dbc uses ParticleColor rows its ParticleColor.dbc lacks (%s): "
                     "put the ParticleColor.dbc of the patch those displays come from in --dbc"
                     % ", ".join(map(str, lost[:10])))
    # A look without a retail display is one the client already has: no model, only the SQL
    # (and, with a client_display, a row of its own: that display at another size).
    for l in looks:
        if l.get("client_display") and l["retail_display"]:
            sys.exit("%s: a look has a retail_display or a client_display, not both" % l["display"])
    clash = [l["display"] for l in looks
             if (l["retail_display"] or l.get("client_display")) and int(l["display"]) in taken]
    if clash:
        sys.exit("display ids already in the base CreatureDisplayInfo.dbc: %s" % ", ".join(clash[:10]))
    base_models = {r[0] for r in cmd_rows}

    # Textures per model slot, for looks retail leaves blank (Haranir: the player's own skin).
    slot_fallback = collections.defaultdict(dict)
    for l in looks:
        if not l["retail_display"]:
            continue
        rd = cdi_r[l["retail_display"]]
        override = [int(x) for x in l.get("textures", "").split(",") if x]
        for k in range(3):
            fd = override[k] if override else int(rd["TextureVariationFileDataID_%d" % k])
            if fd:
                slot_fallback[l["model"]].setdefault(k, fd)

    # File names of the pack's model textures, to tell masks from the textures they shape.
    texture_names = {}
    if pack["prune_textures"]:
        fdids = set()
        for l in looks:
            if l["retail_display"]:
                model = Retail(inline_skeleton(casc(args.cache, int(cmd_r[cdi_r[l["retail_display"]]["ModelID"]]["FileDataID"])),
                                               lambda f: casc(args.cache, f)))
                fdids.update(model.txid)
        texture_names = listfile(args.cache, fdids)

    files = {}  # archive name -> source path
    folder_kind = {}  # a model's folder -> the kind of the first look on it (which archive it goes to)
    variants = {}  # (fdid, geosets, hide, chosen textures) -> (model id, folder, stem)
    texture_slots = {}
    texture_use = collections.defaultdict(set)  # texture file: blend modes drawing it
    slot_use = collections.defaultdict(lambda: collections.defaultdict(set))  # model key: {slot: modes}
    texture_key_slots = {}  # model key: {slot holding an alpha-key copy: the slot it copies}
    warnings = []
    sql_rows = []
    new_cmd, new_cdi = [], []  # rows added to the client DBCs, mirrored into the server's *_dbc tables
    client_cmd = {}  # the client's own model rows the server lacks, by id: mirrored too
    for l in looks:
        display = int(l["display"])
        if pack["transmog"]:
            preview = int(l["preview"]) if l.get("preview") else pack["preview_base"] + display - pack["first_display"]
            sql_rows.append((display, l["form"], l["name"], int(l["tier"]), preview))
        if l.get("client_display"):
            # The client's own display at another size: a copy of its row on the same model.
            row = row_of(cdi_rows, int(l["client_display"]))
            row[0] = display
            if l.get("display_scale"):
                row[4] = f32(l["display_scale"])
            model_row = row_of(cmd_rows, row[1])
            model_path = cmd_str[model_row[2]:cmd_str.index(b"\0", model_row[2])].decode("latin-1")
            # Other textures (retail file ids, 0 for none) go next to the client's model.
            override = [int(x) for x in l.get("textures", "").split(",") if x]
            if override:
                if len(override) != 3:
                    sys.exit("%d %s: textures takes three file ids (0 for none)" % (display, l["name"]))
                for k, fd in enumerate(override):
                    if fd:
                        files["%s\\T%d.blp" % (model_path.rsplit("\\", 1)[0], fd)] = ("tex", fd)
                        row[6 + k] = add_string(cdi_str, "T%d" % fd)
                    else:
                        row[6 + k] = 0
            if row[1] > STOCK_MAX_MODEL:
                client_cmd[row[1]] = (model_row, model_path)
            cdi_rows.append(row)
            new_cdi.append((row, texture_names_of(row, cdi_str)))
            continue
        if not l["retail_display"]:
            continue
        rd = cdi_r[l["retail_display"]]
        rm = cmd_r[rd["ModelID"]]
        fdid = int(rm["FileDataID"])
        geosets = tuple(sorted(geo.get(l["retail_display"], {}).items()))
        # Meshes drawn with these retail textures are left out (a mount's saddle, for a look
        # without it): retail draws them always, they're no geoset the client could hide.
        hide = tuple(sorted({int(x) for x in l.get("hide", "").split(",") if x}))
        folder = "%s\\%s" % (pack["root"], l.get("folder") or folder_name(l["model"]))
        # A customizable mount's body textures come from its customization choices (see fetch.py).
        chosen = custom.get(l["retail_display"], {})
        key = (fdid, geosets, hide, tuple(sorted(chosen.items())))
        if key not in variants:
            stem = folder_name(l["model"])
            if any(k[0] == fdid for k in variants):
                stem += "_" + hashlib.md5(repr(key if key[3] else (geosets, hide) if hide else geosets)
                                          .encode()).hexdigest()[:6]
            raw, m, skins, anims = load_model(args.cache, fdid, stem, warnings)
            lod = 0

            def converter():
                c = Converter(raw, skins(lod), anims, valid, stem, effects=bool(pack.get("effects")))
                if pack["prune_textures"]:
                    c.mask_textures = {i for i, fd in enumerate(m.txid) if any(
                        w in os.path.basename(texture_names.get(fd, "")) for w in MASK_WORDS)}
                return c

            missing = set(hide) - set(m.txid)
            if missing:
                sys.exit("%d %s: hide names textures the model doesn't have: %s" % (
                    display, l["name"], ", ".join(map(str, sorted(missing)))))
            # Skin 0 unless the mesh drawn overflows it: then the next, lower-detail skin.
            while True:
                conv = converter()
                hidden = {i for i, fd in enumerate(m.txid) if fd in hide}
                drawn = conv.batch_textures(dict(geosets), hidden) if pack["prune_textures"] else None
                textures, slots, tex_files, tex_warnings = [], set(), {}, []
                for i, (ttype, tflags, _n, _o) in enumerate(m.arr("textures", "<IIII")):
                    fd = (m.txid[i] if i < len(m.txid) else 0) or chosen.get(ttype, 0)
                    if i < len(m.txid) and m.txid[i] in hide:
                        textures.append((None, tflags, None))  # not drawn: its meshes go
                        continue
                    if drawn is not None and i not in drawn:
                        textures.append((None, tflags, None))  # only particles or extra passes used it
                        continue
                    if ttype not in (0, 11, 12, 13):
                        if not fd:
                            textures.append((None, tflags, None))  # a retail-only texture slot: not drawn
                            continue
                        ttype = 0
                    if ttype == 0:
                        if not fd:
                            textures.append((0, tflags, "Textures\\ShaneCube.blp"))
                            tex_warnings.append("%s: texture %d has no file" % (stem, i))
                            continue
                        name = "%s\\T%d.blp" % (folder, fd)
                        tex_files[name] = ("tex", fd)
                        textures.append((0, tflags, name))
                    else:
                        textures.append((ttype, tflags, None))
                        if 11 <= ttype <= 13:
                            slots.add(ttype - 11)
                try:
                    m2, skin = conv.convert(textures, dict(geosets))
                    break
                except SkinOverflow as e:
                    if lod + 1 == len(m.sfid):
                        raise
                    lod += 1
                    overflow = e
            files.update(tex_files)
            warnings += tex_warnings
            if lod:
                warnings.append("%s; drawn from the retail skin %d" % (overflow, lod))
            # A texture alpha-key and blended batches both draw: the alpha-key ones get a copy
            # (see ALPHA_KEY), in a free creature skin slot when the texture is a slot's.
            copies, key_slots = {}, {}
            for i, use in sorted(conv.texture_use.items()):
                if 1 not in use or max(use) < 2:
                    continue
                ttype, tflags, name = textures[i]
                if ttype == 0:
                    copy = name[:-4] + "k.blp"
                    files[copy] = ("keytex", files[name][1])
                    copies[i] = (0, tflags, copy)
                    continue
                free = [k for k in range(3) if k not in slots and k not in key_slots]
                if not free:
                    warnings.append("%s: texture %d is alpha-keyed and blended, and no skin slot is free "
                                    "for a copy: left shredded" % (stem, i))
                    continue
                key_slots[free[0]] = ttype - 11
                copies[i] = (11 + free[0], tflags, None)
            if copies:
                conv = converter()
                if drawn is not None:
                    conv.batch_textures(dict(geosets), hidden)
                m2, skin = conv.convert(textures, dict(geosets), key_copies=copies)
            for i, use in conv.texture_use.items():
                ttype, _f, name = (textures + [copies[k] for k in sorted(copies)])[i]
                if ttype == 0:
                    texture_use[name] |= use
                else:
                    slot_use[key][ttype - 11] |= use
            texture_key_slots[key] = key_slots
            errors, info = check(m2, skin)
            if errors:
                sys.exit("%s fails the 3.3.5 checks: %s" % (stem, errors[:5]))
            mp = os.path.join(args.work, stem + ".m2")
            sp = os.path.join(args.work, stem + "00.skin")
            open(mp, "wb").write(m2)
            open(sp, "wb").write(skin)
            files["%s\\%s.m2" % (folder, stem)] = ("file", mp)
            files["%s\\%s00.skin" % (folder, stem)] = ("file", sp)
            model_id = pack["first_model"] + len(variants)
            row = row_of(cmd_rows, row_of(cdi_rows, pack["template"][l["form"]])[1])
            row[0] = model_id
            model_path = "%s\\%s.mdx" % (folder, stem)
            if len(model_path) > MODEL_PATH_MAX:
                sys.exit("%s is %d characters; the server's creaturemodeldata_dbc holds %d. Give the look "
                         "a shorter model name or folder, or the pack a shorter root" % (
                             model_path, len(model_path), MODEL_PATH_MAX))
            row[2] = add_string(cmd_str, model_path)
            row[4] = f32(rm["ModelScale"] or 1)
            # Retail marks an unset collision size with -20000000: keep the stock model's then.
            if float(rm["CollisionWidth"]) >= 0 and float(rm["CollisionHeight"]) >= 0:
                row[14] = f32(rm["CollisionWidth"])
                row[15] = f32(rm["CollisionHeight"])
            row[16] = f32(rm["MountHeight"])
            if pack.get("plain"):
                row[6] = row[13] = 0  # FootprintTextureID, SoundID
            for k in range(6):
                row[17 + k] = f32(rm["GeoBox_%d" % k])
            cmd_rows.append(row)
            new_cmd.append((row, model_path))
            variants[key] = (model_id, folder, stem)
            texture_slots[key] = slots
            print("%-34s %2d/%2d anims, %3d bones, %d batches" % (
                stem, len(conv.kept), len(conv.seqs), m.h["bones"][0], info["batches"]))
            if conv.bad_tracks:
                warnings.append("%s: %d keyframe arrays out of order, left still" % (stem, conv.bad_tracks))
            if conv.dropped:
                warnings.append("%s: stale .anim files, animations %s dropped" % (stem, sorted(set(conv.dropped))))
        model_id, folder, stem = variants[key]
        folder_kind.setdefault(folder, l["form"])
        row = row_of(cdi_rows, pack["template"][l["form"]])
        row[0] = display
        row[1] = model_id
        row[3] = 0
        if pack.get("plain"):
            row[2] = row[12] = 0  # SoundID, NPCSoundID
        row[4] = f32(l.get("display_scale") or rd["CreatureModelScale"] or 1)
        row[5] = int(rd["CreatureModelAlpha"]) or 255
        override = [int(x) for x in l.get("textures", "").split(",") if x]
        for k in range(3):
            if k in texture_key_slots[key]:
                row[6 + k] = 0  # an alpha-key copy goes here, below
                continue
            fd = override[k] if override else int(rd["TextureVariationFileDataID_%d" % k])
            if not fd and k in texture_slots[key]:
                fd = slot_fallback[l["model"]].get(k, 0)
                if fd:
                    warnings.append("%d %s: texture slot %d borrowed from another look" % (display, l["name"], k))
                else:
                    warnings.append("%d %s: texture slot %d has no texture (renders white)" % (display, l["name"], k))
            if fd:
                files["%s\\T%d.blp" % (folder, fd)] = ("tex", fd)
                row[6 + k] = add_string(cdi_str, "T%d" % fd)
                texture_use["%s\\T%d.blp" % (folder, fd)] |= slot_use[key][k]
            else:
                row[6 + k] = 0
        for k, src in texture_key_slots[key].items():
            name = texture_names_of(row, cdi_str)[src]
            if name:
                files["%s\\%sk.blp" % (folder, name)] = ("keytex", int(name[1:]))
                row[6 + k] = add_string(cdi_str, name + "k")
        row[9] = 0
        row[13] = colors.row_for(rd["ParticleColorID"], display, warnings) if colors else 0
        row[14] = 0
        row[15] = 0
        cdi_rows.append(row)
        new_cdi.append((row, texture_names_of(row, cdi_str)))

    # Textures (top mips dropped above --max-texture).
    tex_dir = os.path.join(args.work, "textures")
    os.makedirs(tex_dir, exist_ok=True)
    keyed = 0
    for name, (kind, src) in list(files.items()):
        if kind in ("tex", "keytex"):
            data = shrink_blp(casc(args.cache, src), args.max_texture)
            use = texture_use.get(name, set())
            stretch = kind == "keytex" or 1 in use
            if stretch and kind == "tex" and max(use) >= 2:
                warnings.append("%s: alpha-keyed by one model, blended by another: left shredded" % name)
                stretch = False
            if stretch:
                data = key_alpha_blp(data)
                keyed += 1
            p = os.path.join(tex_dir, "%d_%d%s.blp" % (src, args.max_texture, "k" if stretch else ""))
            open(p, "wb").write(data)
            files[name] = ("file", p)
    print("%d textures with alpha-key alpha" % keyed)

    # Models whose id the base client already uses (Reforged HD has 9511 and 9571) move past the
    # block, to the next ids free in both, so a rerun on the same base picks the same ids.
    ours = {row[0] for row, _n in new_cmd}
    after = max(ours, default=0)
    for row, _n in new_cmd:
        if row[0] in base_models:
            while after in ours or after in base_models:
                after += 1
            print("model %d is taken by the base client: using %d" % (row[0], after))
            for cdi, _t in new_cdi:
                if cdi[1] == row[0]:
                    cdi[1] = after
            row[0] = after
            ours.add(after)

    write_dbc(os.path.join(args.work, "CreatureDisplayInfo.dbc"), cdi_rows, cdi_str, cdi_fields)
    write_dbc(os.path.join(args.work, "CreatureModelData.dbc"), cmd_rows, cmd_str, cmd_fields)
    files["DBFilesClient\\CreatureDisplayInfo.dbc"] = ("file", os.path.join(args.work, "CreatureDisplayInfo.dbc"))
    files["DBFilesClient\\CreatureModelData.dbc"] = ("file", os.path.join(args.work, "CreatureModelData.dbc"))
    if colors and colors.added:
        path = os.path.join(args.work, "ParticleColor.dbc")
        colors.write(path)
        files["DBFilesClient\\ParticleColor.dbc"] = ("file", path)
        print("%d ParticleColor rows added" % len(colors.added))
    data = {}  # archive file name -> its files
    if args.dbc_only or (args.data_dir and pack.get("archives")):
        if not args.dbc_only:
            first = next(iter(pack["archives"].values()))
            for name in files:
                if not name.startswith("DBFilesClient\\"):
                    kind = folder_kind.get(name.rsplit("\\", 1)[0])
                    data.setdefault(pack["archives"].get(kind, first), {})[name] = files[name]
        files = {n: f for n, f in files.items() if n.startswith("DBFilesClient\\")}
    pack_mpq(args.out, [(src, name) for name, (_k, src) in sorted(files.items())])
    print("%s: %d looks, %d models, %d files, %.1f MB" % (
        args.out, len(looks), len(variants), len(files), os.path.getsize(args.out) / 1e6))
    for archive, part in sorted(data.items()):
        os.makedirs(args.data_dir, exist_ok=True)
        path = os.path.join(args.data_dir, archive)
        pack_mpq(path, [(src, name) for name, (_k, src) in sorted(part.items())])
        print("%s: %d files, %.1f MB" % (path, len(part), os.path.getsize(path) / 1e6))
    for w in warnings:
        print("warning:", w)
    if args.sql:
        write_sql(args.sql, pack, sql_rows, new_cdi, new_cmd, [client_cmd[k] for k in sorted(client_cmd)])


def sql_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def si(v):
    """A DBC uint32 as the signed int the server's *_dbc columns hold (-1, not 4294967295)."""
    return struct.unpack("<i", struct.pack("<I", v))[0]


def fl(v):
    return "%g" % struct.unpack("<f", struct.pack("<I", v))[0]


# mod-transmog-plus's own table (its data/sql/world/mod_transmog_plus_2026_10_08_forms.sql): keep
# the two the same. A pack's SQL makes it too, because the server applies every module's SQL in
# file name order and a pack's can come before mod-transmog-plus's.
FORMS_TABLE = """CREATE TABLE IF NOT EXISTS `mod_transmog_plus_forms` (
    `DisplayId` int(10) unsigned NOT NULL COMMENT 'CreatureDisplayInfo id from the client patch',
    `Form` varchar(16) NOT NULL COMMENT 'bear, cat, travel, aquatic, flight, moonkin or tree',
    `Name` varchar(80) NOT NULL,
    `UnlockPhase` tinyint(3) unsigned NOT NULL DEFAULT 0 COMMENT 'individual progression phase cleared (quest 66000+n); 0 = from the start',
    `PreviewCreature` int(10) unsigned NOT NULL DEFAULT 0 COMMENT 'creature_template entry with this display, for the addon preview',
    `SortOrder` int(10) unsigned NOT NULL DEFAULT 0,
    PRIMARY KEY (`DisplayId`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def write_sql(path, pack, rows, new_cdi, new_cmd, client_cmd=()):
    """The pack's world SQL, which its repo ships as a server module: the looks for
    mod-transmog-plus, one creature per look for the addon's preview (3.3.5 model frames can show
    a creature entry, not a display id), and the patch's new DBC rows for the server's stores."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("-- Generated by buildthehomelab/wow-mod-retail-creatures tools/build_patch.py from\n")
        f.write("-- %s\n" % pack["source"])
        if pack["transmog"]:
            write_transmog_sql(f, pack, rows)
        write_dbc_sql(f, new_cdi, new_cmd, client_cmd)
    print("wrote", path)


def write_transmog_sql(f, pack, rows):
    """The looks for mod-transmog-plus and their preview creatures."""
    previews = [(preview, d, name) for d, _form, name, _tier, preview in rows]
    first, last = pack["preview_base"], pack["preview_base"] + 499
    if any(not first <= entry <= last for entry, _d, _n in previews):
        sys.exit("preview creature entries outside %d-%d" % (first, last))
    if len({entry for entry, _d, _n in previews}) != len(previews):
        sys.exit("two looks share a preview creature entry")
    kinds = ", ".join(sql_str(k) for k in pack["template"])
    f.write("-- The looks show in mod-transmog-plus's Transmogrify window. That module owns the table; it is\n"
            "-- made here too so this file applies whichever of the two comes first.\n")
    f.write(FORMS_TABLE)
    # Only this pack's rows: their preview creatures are in its block. Another module can have
    # looks of the same forms (mod-retail-pets' Whoofle Bramblewing is a flight form look).
    f.write("DELETE FROM `mod_transmog_plus_forms` WHERE `Form` IN (%s) AND `PreviewCreature` BETWEEN %d AND %d;\n" % (
        kinds, first, last))
    f.write("INSERT INTO `mod_transmog_plus_forms` (`DisplayId`, `Form`, `Name`, `UnlockPhase`, `PreviewCreature`, `SortOrder`) VALUES\n")
    f.write(",\n".join("(%d, %s, %s, %d, %d, %d)" % (d, sql_str(form), sql_str(name), tier, preview,
                                                   pack["sort_base"] + i + 1)
                       for i, (d, form, name, tier, preview) in enumerate(rows)) + ";\n\n")
    # Only this pack's own creatures go: another module may have taken an entry in the block
    # (mod-lost-legendaries' Shade of Highlord Mograine is 9501300, inside the druid forms').
    mine = "BETWEEN %d AND %d AND t.`subname` = %s" % (first, last, sql_str(pack["subname"]))
    f.write("-- Preview creatures, in %d-%d with subname %s: never spawned.\n" % (first, last, sql_str(pack["subname"])))
    f.write("DELETE m FROM `creature_template_model` m JOIN `creature_template` t ON t.`entry` = m.`CreatureID`\n"
            "WHERE t.`entry` %s;\n" % mine)
    f.write("DELETE t FROM `creature_template` t WHERE t.`entry` %s;\n" % mine)
    f.write("INSERT INTO `creature_template` (`entry`, `name`, `subname`, `minlevel`, `maxlevel`, `faction`, `type`, `unit_class`) VALUES\n")
    f.write(",\n".join("(%d, %s, %s, 1, 1, 35, 1, 1)" % (e, sql_str(n), sql_str(pack["subname"]))
                       for e, _d, n in previews) + ";\n")
    f.write("INSERT INTO `creature_template_model` (`CreatureID`, `Idx`, `CreatureDisplayID`, `DisplayScale`, `Probability`) VALUES\n")
    f.write(",\n".join("(%d, 0, %d, 1, 1)" % (e, d) for e, d, _n in previews) + ";\n\n")


def write_dbc_sql(f, new_cdi, new_cmd, client_cmd):
    """The patch's new DBC rows, as rows of the server's *_dbc tables."""
    if not new_cdi:
        return
    # The server checks creature displays against its own DBC stores: give it the patch's rows.
    ids = [r[0] for r, _t in new_cdi]
    f.write("-- The patch's CreatureDisplayInfo and CreatureModelData rows, for the server's DBC stores.\n")
    f.write("DELETE FROM `creaturedisplayinfo_dbc` WHERE `ID` BETWEEN %d AND %d;\n" % (min(ids), max(ids)))
    f.write("INSERT INTO `creaturedisplayinfo_dbc` (`ID`, `ModelID`, `SoundID`, `ExtendedDisplayInfoID`, `CreatureModelScale`, `CreatureModelAlpha`, `TextureVariation_1`, `TextureVariation_2`, `TextureVariation_3`, `PortraitTextureName`, `BloodLevel`, `BloodID`, `NPCSoundID`, `ParticleColorID`, `CreatureGeosetData`, `ObjectEffectPackageID`) VALUES\n")
    f.write(",\n".join("(%d, %d, %d, %d, %s, %d, %s, %s, %s, '', %d, %d, %d, %d, %d, %d)" % (
        r[0], si(r[1]), si(r[2]), si(r[3]), fl(r[4]), si(r[5]), sql_str(t[0]), sql_str(t[1]), sql_str(t[2]),
        si(r[10]), si(r[11]), si(r[12]), si(r[13]), si(r[14]), si(r[15])) for r, t in new_cdi) + ";\n")
    # The models: the patch's own block, and any of the client's own rows past the stock table
    # (an HD pack's) that a look sits on, which the server has to know as well.
    cmd_insert = "INSERT INTO `creaturemodeldata_dbc` (`ID`, `Flags`, `ModelName`, `SizeClass`, `ModelScale`, `BloodID`, `FootprintTextureID`, `FootprintTextureLength`, `FootprintTextureWidth`, `FootprintParticleScale`, `FoleyMaterialID`, `FootstepShakeSize`, `DeathThudShakeSize`, `SoundID`, `CollisionWidth`, `CollisionHeight`, `MountHeight`, `GeoBoxMinX`, `GeoBoxMinY`, `GeoBoxMinZ`, `GeoBoxMaxX`, `GeoBoxMaxY`, `GeoBoxMaxZ`, `WorldEffectScale`, `AttachedEffectScale`, `MissileCollisionRadius`, `MissileCollisionPush`, `MissileCollisionRaise`) VALUES\n"
    def cmd_values(pairs):
        return ",\n".join("(%d, %d, %s, %d, %s, %d, %d, %s, %s, %s, %d, %d, %d, %d, %s)" % (
            r[0], si(r[1]), sql_str(n), si(r[3]), fl(r[4]), si(r[5]), si(r[6]), fl(r[7]), fl(r[8]), fl(r[9]),
            si(r[10]), si(r[11]), si(r[12]), si(r[13]), ", ".join(fl(x) for x in r[14:28])) for r, n in pairs) + ";\n"
    mids = [r[0] for r, _n in new_cmd]
    if mids:
        f.write("DELETE FROM `creaturemodeldata_dbc` WHERE `ID` BETWEEN %d AND %d;\n" % (min(mids), max(mids)))
        f.write(cmd_insert + cmd_values(new_cmd))
    if client_cmd:
        f.write("-- The client's own model rows these looks use that the stock tables lack.\n")
        f.write("DELETE FROM `creaturemodeldata_dbc` WHERE `ID` IN (%s);\n" % ", ".join(str(r[0]) for r, _n in client_cmd))
        f.write(cmd_insert + cmd_values(client_cmd))
    f.write("DELETE FROM `creature_model_info` WHERE `DisplayID` BETWEEN %d AND %d;\n" % (min(ids), max(ids)))
    f.write("INSERT INTO `creature_model_info` (`DisplayID`, `BoundingRadius`, `CombatReach`, `Gender`, `DisplayID_Other_Gender`) VALUES\n")
    f.write(",\n".join("(%d, 1, 1.5, 2, 0)" % d for d in ids) + ";\n")


if __name__ == "__main__":
    main()
