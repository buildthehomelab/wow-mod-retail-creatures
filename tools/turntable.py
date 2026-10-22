#!/usr/bin/env python3
"""Turntable pictures of converted looks: each look drawn from a ring of angles around it, as one
picture strip a launcher or a web page can spin by showing one frame at a time.

    RETAIL_SOURCE=cdn python3 turntable.py --cache <download dir> --client <dir> \\
        --census <census out dir> --out <dir> --only 124511,47634 [--frames 36] [--width 440 --height 360]

Each look is converted and drawn the way contact_sheet.py draws it (the 3.3.5a model the
converter makes, posed on the first frame of Stand), but on a transparent background, at the
same scale in every frame, turning about the middle of its footprint with the light turning
along with the camera. Frame 0 is contact_sheet.py's angle.

Writes <out>/<retail display>.webp: --frames frames of --width by --height pixels side by side,
left to right, each turned 360/frames degrees further (the camera goes round anticlockwise seen
from above, so the model seems to turn clockwise). The model is as large as the frame allows at
its widest angle.

Needs numpy and Pillow, like contact_sheet.py.
"""
import argparse
import collections
import json
import multiprocessing
import os
import sys

import numpy as np
from PIL import Image

from census import read_dbc_ids
from contact_sheet import LIGHT, blp_reader, camera, convert_variant, draw_raw, drawn_batches, look_textures
from fetch import customization_textures, db2_rows, listfile

AZIMUTH = 40.0  # contact_sheet.py's angle for frame 0


def rotate_z(v, degrees):
    a = np.radians(degrees)
    c, s = np.cos(a), np.sin(a)
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1], v[2]])


def turntable(model, textures, frames, width, height, elevation, scale=2):
    """(height, width * frames, 4) uint8 RGBA."""
    pw, ph = width * scale, height * scale
    _drawn, used = drawn_batches(model, textures)
    # It turns about the middle of its footprint: a model's origin can sit far to one end (a
    # mount's chest, with the tail behind), which would swing it round the frame.
    centre = (model.pos[used].min(0) + model.pos[used].max(0)) / 2
    centre[2] = 0.0
    model.pos = model.pos - centre
    angles = [AZIMUTH + 360.0 * i / frames for i in range(frames)]
    # One framing for every frame: as wide as the model ever gets either side of that middle,
    # as tall as it ever gets, so it neither grows nor slides while it turns.
    half, lo, hi = 0.0, np.inf, -np.inf
    for az in angles:
        _cam, right, up = camera(az, elevation)
        sx, sy = model.pos[used] @ right, -(model.pos[used] @ up)
        half = max(half, np.abs(sx).max())
        lo, hi = min(lo, sy.min()), max(hi, sy.max())
    # Model units per pixel: whichever of width and height is tighter, with a small margin.
    unit = max(2 * half / pw, (hi - lo) / ph, 1e-9) * 1.05
    px = max(pw, ph)
    span = unit * px
    mid = (0.0, (lo + hi) / 2)
    y0, x0 = (px - ph) // 2, (px - pw) // 2
    black = np.zeros((px, px, 3), np.float32)
    white = np.ones((px, px, 3), np.float32)
    strip = np.zeros((height, width * frames, 4), np.uint8)
    for i, az in enumerate(angles):
        light = rotate_z(LIGHT, az - AZIMUTH)
        # Drawn over black and over white: over black the picture is its own premultiplied
        # colour, and how much of the white still shows through is how transparent it is.
        # Additive glow outside the body keeps its light as alpha.
        b, covered = draw_raw(model, textures, px, az, elevation, mid, span, light, black)
        w, _ = draw_raw(model, textures, px, az, elevation, mid, span, light, white)
        b, w, covered = (a[y0:y0 + ph, x0:x0 + pw] for a in (b, w, covered))
        b = b.clip(0, 1)
        alpha = np.maximum(1 - (w.clip(0, 1) - b).mean(2), b.max(2)).clip(0, 1)
        alpha[covered] = 1.0
        premul = b.reshape(height, scale, width, scale, 3).mean((1, 3))
        alpha = alpha.reshape(height, scale, width, scale).mean((1, 3))
        rgb = np.where(alpha[..., None] > 1e-4, premul / np.maximum(alpha[..., None], 1e-4), 0).clip(0, 1)
        strip[:, i * width:(i + 1) * width, :3] = (rgb * 255 + 0.5).astype(np.uint8)
        strip[:, i * width:(i + 1) * width, 3] = (alpha * 255 + 0.5).astype(np.uint8)
    return strip


def render_look(job):
    cache, valid, out, fdid, geosets, stem, chosen, display, tex, fallback, frames, width, height, elevation, texture = job
    try:
        model, fixed = convert_variant(cache, valid, fdid, geosets, stem, chosen)
        use, note = look_textures(model, fixed, tex, fallback, blp_reader(cache, texture))
        strip = turntable(model, use, frames, width, height, elevation)
        Image.fromarray(strip, "RGBA").save(os.path.join(out, "%s.webp" % display), quality=90, alpha_quality=100,
                                            method=6)
        return display, "; ".join(note) or "ok"
    except Exception as e:
        return display, "failed: %s" % (str(e)[:120] or type(e).__name__)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--client", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", required=True, help="comma-separated retail display ids")
    ap.add_argument("--frames", type=int, default=36)
    ap.add_argument("--width", type=int, default=440, help="pixels across a frame")
    ap.add_argument("--height", type=int, default=360, help="pixels down a frame")
    ap.add_argument("--elevation", type=float, default=12.0, help="degrees the camera looks down")
    ap.add_argument("--texture", type=int, default=512, help="texture size drawn from (nearest mip level)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args()
    if args.frames < 1 or args.width < 1 or args.height < 1:
        sys.exit("--frames, --width and --height have to be at least 1")
    if args.width * args.frames > 16383 or args.height > 16383:
        sys.exit("a WebP picture can't be over 16383 pixels either way: fewer --frames or a smaller --width or --height")
    os.makedirs(args.out, exist_ok=True)
    valid = read_dbc_ids(os.path.join(args.client, "AnimationData.dbc"))
    geo = collections.defaultdict(dict)
    for g in db2_rows(args.cache, "CreatureDisplayInfoGeosetData"):
        geo[g["CreatureDisplayInfoID"]][int(g["GeosetIndex"]) + 1] = int(g["GeosetValue"])
    custom = customization_textures(args.cache)
    models = json.load(open(os.path.join(args.census, "models.json")))
    only = {d.strip() for d in args.only.split(",") if d.strip()}

    jobs, fallback, seen = [], collections.defaultdict(dict), set()
    rows = open(os.path.join(args.census, "displays.tsv"), encoding="utf-8").read().splitlines()[1:]
    for line in rows:
        _kind, _thing, _name, display, fdid, path, status, tex, _geosets = line.split("\t")
        if status == "new model":
            for k, t in enumerate(int(t) for t in tex.split(",")[:3]):
                if t:
                    fallback[fdid].setdefault(k, t)
    for line in rows:
        _kind, _thing, _name, display, fdid, path, status, tex, _geosets = line.split("\t")
        if display not in only or display in seen:
            continue
        seen.add(display)
        if status != "new model" or models.get(fdid, {}).get("result") != "ok":
            print("%s: not a converted look (%s)" % (display, status))
            continue
        jobs.append((args.cache, valid, args.out, int(fdid), tuple(sorted(geo.get(display, {}).items())),
                     os.path.basename(path)[:-3], tuple(sorted(custom.get(display, {}).items())), display,
                     [int(t) for t in tex.split(",")][:3], fallback[fdid],
                     args.frames, args.width, args.height, args.elevation, args.texture))
    # Displays the census doesn't list (not a journal mount or pet: another skin of a model it
    # has) are read from the retail tables, when the census converted their model.
    missing = only - seen
    if missing:
        model_file = {r["ID"]: r["FileDataID"] for r in db2_rows(args.cache, "CreatureModelData")}
        for row in db2_rows(args.cache, "CreatureDisplayInfo"):
            display = row["ID"]
            if display not in missing:
                continue
            missing.discard(display)
            fdid = model_file.get(row["ModelID"], "0")
            if models.get(fdid, {}).get("result") != "ok":
                print("%s: its model %s isn't a converted one" % (display, fdid))
                continue
            path = listfile(args.cache, [fdid]).get(int(fdid), "%s.m2" % fdid)
            jobs.append((args.cache, valid, args.out, int(fdid), tuple(sorted(geo.get(display, {}).items())),
                         os.path.basename(path)[:-3], tuple(sorted(custom.get(display, {}).items())), display,
                         [int(row["TextureVariationFileDataID_%d" % k]) for k in range(3)], fallback[fdid],
                         args.frames, args.width, args.height, args.elevation, args.texture))
    for display in sorted(missing):
        print("%s: not a retail display" % display)
    with multiprocessing.Pool(args.workers) as pool:
        for display, note in pool.imap_unordered(render_look, jobs):
            print("%s: %s" % (display, note), flush=True)


if __name__ == "__main__":
    sys.exit(main())
