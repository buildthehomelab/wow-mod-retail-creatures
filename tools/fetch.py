"""Downloads of retail files and tables, cached on disk.

Tables (DB2 as CSV) come from wago.tools. Files come from wago.tools too, or with
RETAIL_SOURCE=cdn straight from Blizzard's CDN (tact.py): that needs 400 MB of indexes on its
first use, then is much faster, works when wago.tools is down, and takes every file from one
pinned retail build. wago.tools pins nothing: of 60 files compared on 2026-10-12, 16 were not
the live retail build's copy, and six models came with a skin made for another version of
them. The two sources keep separate caches (<cache>/casc and <cache>/cdn), so they never mix.
"""
import collections
import csv
import io
import os
import threading
import time
import urllib.error
import urllib.request

CASC = "https://wago.tools/api/casc/{}"
DB2 = "https://wago.tools/db2/{}/csv"
LISTFILE = "https://github.com/wowdev/wow-listfile/releases/latest/download/community-listfile.csv"
UA = {"User-Agent": "mod-retail-druid-forms/1.0"}
SOURCE = os.environ.get("RETAIL_SOURCE", "wago")  # wago or cdn
_tact, _tact_lock = {}, threading.Lock()


def fetch(url, path, tries=6):
    if os.path.exists(path) and os.path.getsize(path) > 64:
        return open(path, "rb").read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                data = r.read()
            if len(data) <= 64 and data.startswith(b"error code"):
                raise urllib.error.URLError(data.decode())
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return data
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == tries - 1:
                raise RuntimeError("%s: %s" % (url, e))
            time.sleep(2 + 3 * attempt)


def tact(cache):
    """The pinned CDN build of this cache (indexed on first use)."""
    with _tact_lock:
        if cache not in _tact:
            from tact import Tact
            _tact[cache] = Tact(cache)
        return _tact[cache]


def casc(cache, fdid, source=None):
    """A retail file's bytes, from source (wago or cdn; default RETAIL_SOURCE). From the CDN:
    KeyError if the build lacks it, tact.Encrypted if it is unreleased content."""
    if (source or SOURCE) != "cdn":
        return fetch(CASC.format(fdid), os.path.join(cache, "casc", str(fdid)))
    path = os.path.join(cache, "cdn", str(fdid))
    if os.path.exists(path):
        return open(path, "rb").read()
    data = tact(cache).read(fdid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.part" % (path, threading.get_ident())
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return data


def casc_path(cache, fdid):
    casc(cache, fdid)
    return os.path.join(cache, "cdn" if SOURCE == "cdn" else "casc", str(fdid))


def db2(cache, table):
    """Retail DB2 table as {ID: row dict} (latest build)."""
    text = fetch(DB2.format(table), os.path.join(cache, "db2", table + ".csv")).decode("utf-8")
    return {r["ID"]: r for r in csv.DictReader(io.StringIO(text))}


def db2_rows(cache, table):
    text = fetch(DB2.format(table), os.path.join(cache, "db2", table + ".csv")).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def listfile(cache, wanted, prefixes=None):
    """{fdid: path} for the file data ids in wanted, or the paths under the given prefixes."""
    path = os.path.join(cache, "community-listfile.csv")
    fetch(LISTFILE, path)
    wanted = {str(x) for x in wanted or ()}
    prefixes = tuple(prefixes or ())
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            i, _, p = line.rstrip("\n").partition(";")
            if i in wanted or (prefixes and p.startswith(prefixes)):
                out[int(i)] = p
    return out


def _chosen_elements(cache):
    """({ChrModel id: row}, {ChrModel id: [ChrCustomizationElement rows of its chosen choices]}):
    each customization option of a ChrModel set to its first choice."""
    models = {r["ID"]: r for r in db2_rows(cache, "ChrModel")}
    model_of = {o["ID"]: o["ChrModelID"] for o in db2_rows(cache, "ChrCustomizationOption")
                if o["ChrModelID"] in models}
    first = {}  # option: its first choice
    for c in db2_rows(cache, "ChrCustomizationChoice"):
        o = c["ChrCustomizationOptionID"]
        if o in model_of and (o not in first or int(c["OrderIndex"]) < int(first[o]["OrderIndex"])):
            first[o] = c
    chosen = {c["ID"]: model_of[o] for o, c in first.items()}
    elements = collections.defaultdict(list)
    for e in db2_rows(cache, "ChrCustomizationElement"):
        related = e["RelatedChrCustomizationChoiceID"]
        if e["ChrCustomizationChoiceID"] in chosen and (related == "0" or related in chosen):
            elements[chosen[e["ChrCustomizationChoiceID"]]].append(e)
    return models, elements


def customization_textures(cache):
    """{retail display: {texture type: file id}} for displays that are a ChrModel (a customizable
    mount like the Steel Prototype Panther MK-0). Their model leaves its body texture (type 1)
    without a file, and the display names none: retail fills it from the model's customization
    options, each set to its first choice. Creature skin slots (types 11-13) are in it too, for
    a display that leaves its slot empty (the Algarian Stormrider's)."""
    models, elements = _chosen_elements(cache)
    layer_type = {}  # (texture layout, texture target): texture type, lowest layer first
    for r in sorted(db2_rows(cache, "ChrModelTextureLayer"), key=lambda r: int(r["Layer"])):
        for k in range(2):
            target = r["ChrModelTextureTargetID_%d" % k]
            if target != "0":
                layer_type.setdefault((r["CharComponentTextureLayoutsID"], target), int(r["TextureType"]))
    material_of = {m: [e["ChrCustomizationMaterialID"] for e in es if e["ChrCustomizationMaterialID"] != "0"]
                   for m, es in elements.items()}
    if not any(material_of.values()):
        return {}
    materials = db2(cache, "ChrCustomizationMaterial")
    texture_of = {}
    for t in db2_rows(cache, "TextureFileData"):
        texture_of.setdefault(t["MaterialResourcesID"], int(t["FileDataID"]))
    out = {}
    for model_id, mats in material_of.items():
        model = models[model_id]
        for mat in mats:
            m = materials.get(mat)
            ttype = m and layer_type.get((model["CharComponentTextureLayoutID"], m["ChrModelTextureTargetID"]))
            fd = m and texture_of.get(m["MaterialResourcesID"])
            if ttype and fd:
                out.setdefault(model["DisplayID"], {}).setdefault(ttype, fd)
    return out


def customization_geosets(cache):
    """{retail display: {geoset group: variant}} for displays that are a ChrModel: the geosets
    of each customization option's first choice (a drake's horns, an airship's hull). Unlike a
    creature's, a ChrModel's group 0 holds alternatives too: only 0 and the chosen one show
    (see m2creature.Converter.shows)."""
    models, elements = _chosen_elements(cache)
    geosets = {r["ID"]: r for r in db2_rows(cache, "ChrCustomizationGeoset")}
    out = {}
    for model_id, es in elements.items():
        for e in es:
            g = geosets.get(e["ChrCustomizationGeosetID"])
            if g and e["ChrCustomizationGeosetID"] != "0":
                out.setdefault(models[model_id]["DisplayID"], {}).setdefault(int(g["GeosetType"]), int(g["GeosetID"]))
    # Variant 1 of a group past 0 is what a look shows anyway: left out, so a look whose choices
    # are all defaults converts the same as before (and shares its model).
    out = {d: {k: v for k, v in g.items() if k == 0 or v != 1} for d, g in out.items()}
    return {d: g for d, g in out.items() if g}
