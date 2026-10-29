#!/usr/bin/env python3
"""Census of retail mounts and battle pets against a 3.3.5a client: what is already there, what
needs only textures, what needs a converted model, and whether the converter takes it.

    python3 census.py --cache <download dir> --client <dir> --out <dir> [--models] [--workers 6]

--client is the folder client_files.py wrote: the client's Creature\\ file list and its
AnimationData.dbc.

Without --models only the retail tables and the listfile are read. With it, every model the
client lacks is downloaded with its skin, animations and textures and converted, as a patch
build would. That is tens of thousands of files: run it with RETAIL_SOURCE=cdn.
--also names tsv files with a retail_display column (a pack's wish list, such as
wow-mod-retail-collection's wanted-looks.tsv): the models of those looks are probed too, even
when the client has them or they are no creature model, for a pack that wants them as its own.
A display there that is no mount or pet look (a plain creature's, such as a rare's) is listed
too, as kind "creature", named after the file's name column when it has one.
Writes displays.tsv (one row per mount, pet or creature display), models.tsv (one row per model) and
models.json (everything probe() found).
"""
import argparse
import collections
import csv
import concurrent.futures
import json
import os
import struct
import zlib

from build_patch import shrink_blp
from check_m2 import check
from fetch import casc, db2, db2_rows, listfile
from m2creature import ARRAYS, Converter, Retail, SkinOverflow, chunks, inline_skeleton
from tact import Encrypted

PET_HIDDEN = 0x20  # BattlePetSpecies.Flags: not in the pet journal


def read_dbc_ids(path):
    data = open(path, "rb").read()
    count, fields, size = struct.unpack_from("<3I", data, 4)
    return {struct.unpack_from("<I", data, 20 + i * size)[0] for i in range(count)}


def header(md):
    o, h = 8, {}
    for name, kind in ARRAYS:
        if kind == "a":
            h[name] = struct.unpack_from("<II", md, o); o += 8
        elif kind == "u":
            h[name] = struct.unpack_from("<I", md, o)[0]; o += 4
        else:
            o += struct.calcsize("<" + kind)
    return h


def probe(cache, fdid, valid, display_textures, effects=False):
    """One model: what it is made of, and what the converter makes of it (animations and
    textures included, so the sizes are the patch's)."""
    r = {"fdid": fdid}
    try:
        raw = casc(cache, fdid)
    except Exception as e:
        r["result"] = unavailable(e)
        return r
    r["m2_bytes"] = len(raw)
    if raw[:4] != b"MD21":
        r["result"] = "not MD21"
        return r
    ch = chunks(raw)
    h = header(ch["MD21"])
    sfid = struct.unpack("<%dI" % (len(ch.get("SFID", b"")) // 4), ch.get("SFID", b""))
    r.update(version=struct.unpack_from("<I", ch["MD21"], 4)[0], vertices=h["vertices"][0],
             bones=h["bones"][0], sequences=h["sequences"][0], particles=h["particles"][0],
             ribbons=h["ribbons"][0], lights=h["lights"][0], textures=h["textures"][0],
             combos=bool(h["global_flags"] & 0x8), skel="SKID" in ch, anims=len(ch.get("AFID", b"")) // 8,
             chunks=",".join(sorted(k for k in ch if k not in ("MD21", "SFID", "TXID", "AFID"))))
    reasons = []
    if r["combos"]:
        reasons.append("texture combiner combos")
    if r["skel"]:  # converted with its skeleton inlined (inline_skeleton)
        try:
            sk = chunks(casc(cache, struct.unpack("<I", ch["SKID"][:4])[0]))
            if "SKB1" in sk:
                r["bones"] = struct.unpack_from("<I", sk["SKB1"], 0)[0]
            if "SKS1" in sk:
                r["sequences"] = struct.unpack_from("<I", sk["SKS1"], 8)[0]
            r["anims"] = len(sk.get("AFID", b"")) // 8
            r["skel_parent"] = struct.unpack_from("<I", sk["SKPD"], 8)[0] if "SKPD" in sk else 0
        except Exception as e:
            r["skel_error"] = unavailable(e)
    if not sfid:
        r["result"] = "no skin file"
        return r
    if reasons:
        r["result"] = " + ".join(reasons)
        return r
    try:
        raw = inline_skeleton(raw, lambda f: casc(cache, f))
        m = Retail(raw)
        anims, lost = {}, 0
        for a, s, f in m.afid:
            if f and a in valid:
                try:
                    anims[(a, s)] = casc(cache, f)
                except Exception:
                    lost += 1
        for lod, skin_id in enumerate(m.sfid):  # skin 0, or a lower-detail one when it overflows
            conv = Converter(raw, casc(cache, skin_id), anims, valid, "census", effects=effects)
            drawn = conv.batch_textures({})
            textures, tex = [], set(display_textures)
            for i, (ttype, tflags, _n, _o) in enumerate(m.arr("textures", "<IIII")):
                if ttype in (11, 12, 13):
                    textures.append((ttype, tflags, None))
                elif i in drawn and i < len(m.txid) and m.txid[i]:
                    textures.append((0, tflags, "Creature\\Census\\T%d.blp" % m.txid[i]))
                    tex.add(m.txid[i])
                else:
                    textures.append((None, tflags, None))
            try:
                m2, sk = conv.convert(textures, {})
                break
            except SkinOverflow:
                if lod + 1 == len(m.sfid):
                    raise
        errors, info = check(m2, sk)
        kept = {conv.seqs[i][0] for i in conv.kept}
        r.update(skin=lod, batches=info.get("batches"), known_anims=len({s[0] for s in conv.seqs if s[0] in valid}),
                 kept_anims=len(kept), no_basic=[a for a in (0, 4, 5) if a not in kept],
                 stale_anims=len(set(conv.dropped)), lost_anims=lost, bad_tracks=conv.bad_tracks,
                 out_bytes=len(m2) + len(sk), out_packed=len(zlib.compress(m2)) + len(zlib.compress(sk)),
                 tex={})
        for t in sorted(tex):
            try:
                blp = casc(cache, t)
            except Exception as e:
                r["tex"][t] = unavailable(e)
                continue
            big, small = shrink_blp(blp, 1024), shrink_blp(blp, 512)
            r["tex"][t] = (len(big), len(zlib.compress(big)), len(small), len(zlib.compress(small)))
        r["result"] = "ok" if not errors else "check: %s" % str(errors[0])[:80]
    except Exception as e:
        msg = str(e) or type(e).__name__
        for key in ("retail skin has", "overflows 16 bits", "past 16 bits", "track with", "shared skeleton"):
            if key in msg:
                msg = key
        r["result"] = unavailable(e) if isinstance(e, (KeyError, Encrypted)) else "convert: %s" % msg[:80]
    return r


def unavailable(e):
    if isinstance(e, Encrypted):
        return "encrypted (unreleased)"
    if isinstance(e, KeyError):
        return "not in this build"
    return "download: %s" % str(e)[-60:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--effects", action="store_true",
                    help="convert with effects, as a pack with \"effects\": true builds (the library does)")
    ap.add_argument("--also", action="append", default=[],
                    help="tsv with a retail_display column: probe these looks' models too (repeatable)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    client = json.load(open(os.path.join(args.client, "client-files.json")))
    have, stock = client["files"], set(client["stock"])
    valid = read_dbc_ids(os.path.join(args.client, "AnimationData.dbc"))

    cdi = db2(args.cache, "CreatureDisplayInfo")
    cmd = db2(args.cache, "CreatureModelData")
    creature = db2(args.cache, "Creature")
    geo = collections.Counter(g["CreatureDisplayInfoID"] for g in db2_rows(args.cache, "CreatureDisplayInfoGeosetData"))

    looks = []  # (kind, id, name, display)
    mounts = db2(args.cache, "Mount")
    seen = set()
    for x in db2_rows(args.cache, "MountXDisplay"):
        mt = mounts.get(x["MountID"])
        if mt and (x["MountID"], x["CreatureDisplayInfoID"]) not in seen:
            seen.add((x["MountID"], x["CreatureDisplayInfoID"]))
            looks.append(("mount", x["MountID"], mt["Name_lang"], x["CreatureDisplayInfoID"]))
    pets_total = pets_hidden = pets_nocreature = 0
    for p in db2_rows(args.cache, "BattlePetSpecies"):
        pets_total += 1
        if int(p["Flags"]) & PET_HIDDEN:
            pets_hidden += 1
            continue
        c = creature.get(p["CreatureID"])
        if not c:
            pets_nocreature += 1
            continue
        for k in range(4):
            d = c["DisplayID_%d" % k]
            if d != "0" and ("pet", p["ID"], d) not in seen:
                seen.add(("pet", p["ID"], d))
                looks.append(("pet", p["ID"], c["Name_lang"], d))
    # A display a pack asks for that no mount or pet wears: a creature look of its own.
    also, listed = set(), {d for _k, _i, _n, d in looks}
    for path in args.also:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter="\t"):
                also.add(r["retail_display"])
                if r["retail_display"] not in listed:
                    listed.add(r["retail_display"])
                    looks.append(("creature", r["retail_display"], r.get("name") or "display " + r["retail_display"],
                                  r["retail_display"]))

    fdids = set()
    for kind, _id, _name, d in looks:
        row = cdi.get(d)
        if row and row["ModelID"] in cmd:
            fdids.add(int(cmd[row["ModelID"]]["FileDataID"]))
            fdids.update(int(row["TextureVariationFileDataID_%d" % k]) for k in range(4))
    fdids.discard(0)
    paths = listfile(args.cache, fdids)

    def where(path):
        low = path.lower().replace("/", "\\")
        if low in stock:
            return "stock"
        return "hd" if low in have else ""

    rows, models = [], collections.defaultdict(lambda: {"mount": set(), "pet": set(), "creature": set(), "displays": set()})
    for kind, _id, name, d in looks:
        row = cdi.get(d)
        if not row or row["ModelID"] not in cmd:
            rows.append((kind, _id, name, d, "", "", "no display", "", ""))
            continue
        fd = int(cmd[row["ModelID"]]["FileDataID"])
        path = paths.get(fd, "")
        tex = [int(row["TextureVariationFileDataID_%d" % k]) for k in range(4)]
        if not path:
            status = "unnamed model"
        elif not path.startswith("creature/"):
            status = "not a creature model"
        elif int(row["ExtendedDisplayInfoID"]):
            status = "dressed character"
        elif where(path):
            folder = path.rsplit("/", 1)[0]
            missing = [t for t in tex[:3] if t and not where("%s/%s" % (folder, os.path.basename(paths.get(t, "?"))))]
            status = "client has it" if not missing else "client model, new textures"
        else:
            status = "new model"
        m = models[fd]
        m[kind].add(_id)
        m["displays"].add(d)
        m["path"], m["status"] = path, status if status in ("new model", "unnamed model") else m.get("status", status)
        m["tex4"] = m.get("tex4", 0) + (1 if tex[3] else 0)
        m.setdefault("textures", set()).update(t for t in tex[:3] if t)
        m["geosets"] = m.get("geosets", 0) + (1 if geo.get(d) else 0)
        rows.append((kind, _id, name, d, fd, path, status, ",".join(str(t) for t in tex), geo.get(d, 0)))
    with open(os.path.join(args.out, "displays.tsv"), "w", encoding="utf-8") as f:
        f.write("kind\tid\tname\tretail_display\tmodel_fdid\tmodel\tstatus\ttextures\tgeosets\n")
        for r in rows:
            f.write("\t".join(str(x) for x in r) + "\n")

    print("mounts: %d in Mount, %d with a display" % (len(mounts), len({r[1] for r in rows if r[0] == "mount"})))
    print("pets: %d species, %d hidden from the journal, %d without a Creature row, %d listed" % (
        pets_total, pets_hidden, pets_nocreature, len({r[1] for r in rows if r[0] == "pet"})))
    for kind in ("mount", "pet") + (("creature",) if any(r[0] == "creature" for r in rows) else ()):
        by = collections.Counter(r[6] for r in rows if r[0] == kind)
        things = collections.defaultdict(set)
        for r in rows:
            if r[0] == kind:
                things[r[6]].add(r[1])
        print("\n%s displays by status (displays / distinct %ss):" % (kind, kind))
        for s, n in by.most_common():
            print("  %-28s %5d / %5d" % (s, n, len(things[s])))
    new = {fd: m for fd, m in models.items() if m["status"] in ("new model", "unnamed model")}
    print("\nmodels: %d in all, %d the client lacks (%d mount only, %d pet only, %d both)" % (
        len(models), len(new), sum(1 for m in new.values() if m["mount"] and not m["pet"]),
        sum(1 for m in new.values() if m["pet"] and not m["mount"]),
        sum(1 for m in new.values() if m["pet"] and m["mount"])))
    if not args.models:
        return
    # The models of looks a pack asked for by name are probed whatever their status.
    probed = dict(new)
    probed.update((fd, m) for fd, m in models.items() if m["displays"] & also)
    if also:
        print("%d more models probed for the looks in %s" % (len(probed) - len(new), ", ".join(args.also)))

    results = {}
    with concurrent.futures.ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(probe, args.cache, fd, valid, probed[fd]["textures"], args.effects): fd for fd in sorted(probed)}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            results[futs[fut]] = fut.result()
            if n % 50 == 0:
                print("  probed %d/%d" % (n, len(futs)), flush=True)
    json.dump(results, open(os.path.join(args.out, "models.json"), "w"))
    cols = ("result", "version", "vertices", "bones", "sequences", "known_anims", "kept_anims", "no_basic",
            "anims", "stale_anims", "lost_anims", "particles", "ribbons", "lights", "textures", "skin", "batches",
            "m2_bytes", "out_bytes", "out_packed", "chunks")
    with open(os.path.join(args.out, "models.tsv"), "w", encoding="utf-8") as f:
        f.write("model_fdid\tmodel\tmounts\tpets\tdisplays\tfourth_texture\tgeoset_displays\t" + "\t".join(cols) + "\n")
        for fd in sorted(probed):
            m, r = probed[fd], results[fd]
            f.write("\t".join(str(x) for x in (
                fd, m["path"], len(m["mount"]), len(m["pet"]), len(m["displays"]), m["tex4"], m["geosets"],
                *(r.get(c, "") for c in cols))) + "\n")
    by = collections.Counter(r["result"] for r in results.values())
    print("\nconverter verdict per model (models / mounts / pets it carries):")
    for s, n in by.most_common():
        fds = [fd for fd, r in results.items() if r["result"] == s]
        print("  %-52s %4d / %4d / %4d" % (s, n, len(set().union(*(probed[fd]["mount"] for fd in fds))),
                                              len(set().union(*(probed[fd]["pet"] for fd in fds)))))
    ok = {fd: r for fd, r in results.items() if r["result"] == "ok"}
    tex = {}
    for r in ok.values():
        tex.update({t: v for t, v in r["tex"].items() if not isinstance(v, str)})
    lost = sum(1 for r in ok.values() for v in r["tex"].values() if isinstance(v, str))
    mb = 1 / 1e6
    print("\nthe %d models that convert: %.0f MB of models (%.0f packed), %d textures: %.0f MB at 1024 "
          "(%.0f packed), %.0f MB at 512 (%.0f packed); %d textures unavailable" % (
              len(ok), sum(r["out_bytes"] for r in ok.values()) * mb, sum(r["out_packed"] for r in ok.values()) * mb,
              len(tex), sum(v[0] for v in tex.values()) * mb, sum(v[1] for v in tex.values()) * mb,
              sum(v[2] for v in tex.values()) * mb, sum(v[3] for v in tex.values()) * mb, lost))
    print("  %d with stale animation files, %d without Stand, Walk or Run, %d with particles (%d with 15 or more)" % (
        sum(1 for r in ok.values() if r["stale_anims"]), sum(1 for r in ok.values() if r["no_basic"]),
        sum(1 for r in ok.values() if r["particles"]), sum(1 for r in ok.values() if r["particles"] >= 15)))


if __name__ == "__main__":
    main()
