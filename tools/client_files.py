#!/usr/bin/env python3
"""What a 3.3.5a client has, for census.py: its Creature\\ file list and creature DBCs.

    python3 client_files.py --client <WoW folder> --out <dir> [extra patch MPQs, in load order]

Reads every archive of the client in load order (the extra ones last: realm patches the folder
doesn't hold). Writes client-files.json ({"files": {lower-case path: archive that wins},
"stock": [paths the unpatched 3.3.5a archives have]}) and the copies of CreatureDisplayInfo.dbc,
CreatureModelData.dbc and AnimationData.dbc the client ends up using, which are also what
build_patch.py --dbc wants.
"""
import argparse
import ctypes
import json
import os
import sys

STOCK = ("common.mpq", "common-2.mpq", "expansion.mpq", "lichking.mpq", "patch.mpq", "patch-2.mpq", "patch-3.mpq")
DBCS = {"dbfilesclient\\creaturedisplayinfo.dbc": "CreatureDisplayInfo.dbc",
        "dbfilesclient\\creaturemodeldata.dbc": "CreatureModelData.dbc",
        "dbfilesclient\\animationdata.dbc": "AnimationData.dbc"}
H = ctypes.c_void_p


class Find(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 1024), ("plain", ctypes.c_char_p), ("hash", ctypes.c_uint),
                ("block", ctypes.c_uint), ("size", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("packed", ctypes.c_uint), ("time_lo", ctypes.c_uint), ("time_hi", ctypes.c_uint),
                ("locale", ctypes.c_uint)]


def storm():
    lib = ctypes.CDLL(os.environ.get("STORMLIB", "/usr/local/lib/libstorm.dylib"))
    lib.SFileOpenArchive.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(H)]
    lib.SFileFindFirstFile.argtypes = [H, ctypes.c_char_p, ctypes.POINTER(Find), ctypes.c_char_p]
    lib.SFileFindFirstFile.restype = H
    lib.SFileFindNextFile.argtypes = [H, ctypes.POINTER(Find)]
    lib.SFileOpenFileEx.argtypes = [H, ctypes.c_char_p, ctypes.c_uint, ctypes.POINTER(H)]
    lib.SFileGetFileSize.argtypes = [H, ctypes.POINTER(ctypes.c_uint)]
    lib.SFileGetFileSize.restype = ctypes.c_uint
    lib.SFileReadFile.argtypes = [H, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
    for fn in (lib.SFileCloseArchive, lib.SFileFindClose, lib.SFileCloseFile):
        fn.argtypes = [H]
    for fn in (lib.SFileOpenArchive, lib.SFileFindNextFile, lib.SFileOpenFileEx, lib.SFileReadFile):
        fn.restype = ctypes.c_bool
    return lib


def load_order(client):
    """The client's archives, first loaded to last: base, locale, locale patches, patches."""
    data = os.path.join(client, "Data")
    locale = next((os.path.join(data, d) for d in sorted(os.listdir(data))
                   if len(d) == 4 and os.path.isdir(os.path.join(data, d))), None)

    def split(folder):
        # patch.MPQ, then patch-2 .. patch-9, then the lettered ones: name order, "." before "-".
        names = sorted((n for n in os.listdir(folder) if n.lower().endswith(".mpq")),
                       key=lambda n: n.lower().replace(".", " "))
        return ([os.path.join(folder, n) for n in names if not n.lower().startswith("patch")],
                [os.path.join(folder, n) for n in names if n.lower().startswith("patch")])

    base, patches = split(data)
    local, local_patches = split(locale) if locale else ([], [])
    return base + local + local_patches + patches


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("extra", nargs="*")
    args = ap.parse_args()
    lib = storm()
    files, stock, dbcs = {}, set(), {}
    for path in load_order(args.client) + args.extra:
        h = H()
        if not lib.SFileOpenArchive(path.encode(), 0, 0x100, ctypes.byref(h)):
            sys.exit("can't open " + path)
        base, count, fd = os.path.basename(path), 0, Find()
        is_stock = base.lower() in STOCK or os.path.dirname(path) != os.path.join(args.client, "Data") \
            and path not in args.extra
        find = lib.SFileFindFirstFile(h, b"*", ctypes.byref(fd), None)
        while find:
            low = fd.name.decode("latin-1").lower()
            count += 1
            if low.startswith("creature\\"):
                files[low] = base
                if is_stock:
                    stock.add(low)
            if low in DBCS:
                dbcs[low] = (path, fd.name)
            if not lib.SFileFindNextFile(find, ctypes.byref(fd)):
                lib.SFileFindClose(find)
                break
        lib.SFileCloseArchive(h)
        print("%-44s %7d files" % (base, count))
    os.makedirs(args.out, exist_ok=True)
    for low, (path, name) in dbcs.items():
        h, f, got = H(), H(), ctypes.c_uint()
        lib.SFileOpenArchive(path.encode(), 0, 0x100, ctypes.byref(h))
        if not lib.SFileOpenFileEx(h, name, 0, ctypes.byref(f)):
            sys.exit("can't read %s from %s" % (DBCS[low], path))
        size = lib.SFileGetFileSize(f, None)
        buf = ctypes.create_string_buffer(size)
        lib.SFileReadFile(f, buf, size, ctypes.byref(got), None)
        lib.SFileCloseFile(f)
        lib.SFileCloseArchive(h)
        with open(os.path.join(args.out, DBCS[low]), "wb") as out:
            out.write(buf.raw[:got.value])
        print("%s from %s" % (DBCS[low], os.path.basename(path)))
    with open(os.path.join(args.out, "client-files.json"), "w") as out:
        json.dump({"files": files, "stock": sorted(stock)}, out)
    print("%d Creature files, %d of them stock" % (len(files), len(stock)))


if __name__ == "__main__":
    main()
