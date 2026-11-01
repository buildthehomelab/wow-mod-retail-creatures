#!/usr/bin/env python3
"""Retail's creature sounds for looks that are already in the client: only the sound parts change,
so the models in the live patches stay as they are (a fresh build from wago.tools changes some).

    RETAIL_SOURCE=cdn python3 look_sounds.py --pack ../mod-retail-library --pack ../mod-retail-pets \\
        --cdi <CreatureDisplayInfo.dbc of z75> --sound-dbc <folder> --cache ~/retail-cache \\
        --work <work dir> --sound-patch patch-z76-look-sounds.MPQ \\
        --sql ../mod-retail-library/data/sql/db-world/mod_retail_library.sql

--sound-dbc holds the SoundEntries.dbc of the realm's spell patch (z80's: the last one before the
weather patch's) and the CreatureSoundData.dbc of the druid forms patch (z40's). Each look of a
pack that has a retail_display gets that display's sound set, with retail's levels lifted for this
client (option audible, see RetailSounds in build_patch.py). Footsteps and impact type stay those
of the stock creature its kind clones (pack.json's `template`).

In --work it leaves:

- CreatureDisplayInfo.dbc: --cdi with the looks' SoundIDs. Put them in z70 and z75 with
  wow-server's respin_patch.py --sound-ids-from; a rebuild of those patches needs this tool again.
- SoundEntries.dbc: the table given plus this tool's rows. It goes into the spell patch and, with
  the thunderstorm row, the weather patch, since the client keeps the last patch's copy.
- CreatureSoundData.dbc: the table given plus this tool's rows, also in --sound-patch.

--sound-patch holds that CreatureSoundData.dbc and the sound files. --sql is a pack's world SQL,
whose creaturedisplayinfo_dbc rows take the new SoundIDs. --only limits it to some displays, for a
test patch. Running it again on its own output changes nothing: its rows (ENTRIES, DATA) go first.
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys

from build_patch import RetailSounds, pack_mpq, read_dbc
from fetch import db2
from spell_sounds import string_at, write_table

ENTRIES, DATA = (1000000, 1019999), (10000, 12999)  # this tool's SoundEntries and CreatureSoundData ids
DRUID_FORM_ENTRIES = (992000, 992999)  # z40's rows: a kit one of them plays is that row
CDI_SOUND = 2


def load_looks(pack_dir):
    """(display, retail display, stock display its kind clones) of each look with a retail display."""
    pack = json.load(open(os.path.join(pack_dir, "pack.json"), encoding="utf-8"))
    with open(os.path.join(pack_dir, pack["looks"]), encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE))
    return [(int(r["display"]), r["retail_display"], pack["template"][r["form"]]) for r in rows if r.get("retail_display")]


def without_our_rows(source, dest, span):
    rows, strings, fields = read_dbc(source)
    rows = [r for r in rows if not span[0] <= r[0] <= span[1]]
    write_table(dest, (rows, strings, fields))


def set_sql_sound_ids(path, sound_ids):
    """The pack's creaturedisplayinfo_dbc rows take the new SoundIDs; nothing else in the file changes."""
    out, inside, changed = [], False, 0
    with open(path, encoding="utf-8", newline="") as f:
        for line in f:
            if line.startswith("INSERT INTO `creaturedisplayinfo_dbc`"):
                inside = True
            elif inside:
                m = re.match(r"\((\d+), (\d+), (\d+),", line)
                if m and int(m.group(1)) in sound_ids and int(m.group(3)) != sound_ids[int(m.group(1))]:
                    line = "(%s, %s, %d,%s" % (m.group(1), m.group(2), sound_ids[int(m.group(1))], line[m.end():])
                    changed += 1
                inside = not line.rstrip().endswith(";")
            out.append(line)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(out)
    print("%s: SoundID changed on %d rows" % (os.path.basename(path), changed))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", action="append", required=True, help="a pack repo; its looks in order")
    ap.add_argument("--cdi", required=True, help="the live CreatureDisplayInfo.dbc that has every pack's looks")
    ap.add_argument("--sound-dbc", required=True, help="folder with SoundEntries.dbc and CreatureSoundData.dbc")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--sound-patch", help="write the patch with the sound files and CreatureSoundData.dbc")
    ap.add_argument("--sql", action="append", default=[], help="a pack's world SQL with creaturedisplayinfo_dbc rows")
    ap.add_argument("--only", help="comma-separated display ids (test builds)")
    args = ap.parse_args()

    base = os.path.join(args.work, "base")
    os.makedirs(base, exist_ok=True)
    without_our_rows(os.path.join(args.sound_dbc, "SoundEntries.dbc"), os.path.join(base, "SoundEntries.dbc"), ENTRIES)
    without_our_rows(os.path.join(args.sound_dbc, "CreatureSoundData.dbc"), os.path.join(base, "CreatureSoundData.dbc"), DATA)
    sounds = RetailSounds(base, args.cache, {"first_entry": ENTRIES[0], "first_data": DATA[0], "audible": True})
    for r in sounds.se[0]:
        if DRUID_FORM_ENTRIES[0] <= r[0] <= DRUID_FORM_ENTRIES[1]:
            name = string_at(sounds.se[1], r[2])
            if name.startswith("RetailKit"):
                sounds.entries.setdefault(name[len("RetailKit"):], r[0])

    cdi_rows, cdi_strings, cdi_fields = read_dbc(args.cdi)
    cdi = {r[0]: r for r in cdi_rows}
    retail_cdi, retail_cmd = db2(args.cache, "CreatureDisplayInfo"), db2(args.cache, "CreatureModelData")
    only = {int(x) for x in args.only.split(",")} if args.only else None
    warnings, sound_ids, stock_kept = [], {}, 0
    for pack_dir in args.pack:
        for display, retail, template in load_looks(pack_dir):
            if only is not None and display not in only:
                continue
            if display not in cdi:
                warnings.append("display %d is not in --cdi" % display)
                continue
            rd = retail_cdi.get(retail)
            rm = retail_cmd.get(rd["ModelID"]) if rd else None
            sound_set = rd["SoundID"] if rd and int(rd["SoundID"]) else (rm["SoundID"] if rm else "0")
            if not int(sound_set):
                stock_kept += 1
                continue
            sound_ids[display] = sounds.row_for(sound_set, cdi[template][CDI_SOUND], display, warnings)
    for display, sound in sound_ids.items():
        cdi[display][CDI_SOUND] = sound

    files = {}
    if sounds.next_entry > ENTRIES[1] + 1 or sounds.next_data > DATA[1] + 1:
        sys.exit("out of this tool's ids")
    sounds.write(args.work, files)
    write_table(os.path.join(args.work, "CreatureDisplayInfo.dbc"), (cdi_rows, cdi_strings, cdi_fields))
    print("%d looks have a retail sound set, %d have none and keep what they have" % (len(sound_ids), stock_kept))
    for w in warnings:
        print("  " + w)
    if args.sound_patch:
        files.pop("DBFilesClient\\SoundEntries.dbc", None)
        pack_mpq(args.sound_patch, [(src, name) for name, (_k, src) in sorted(files.items())])
        print("%s: %d files, %.1f MB" % (args.sound_patch, len(files), os.path.getsize(args.sound_patch) / 1e6))
    for path in args.sql:
        set_sql_sound_ids(path, sound_ids)
    shutil.rmtree(base)


if __name__ == "__main__":
    main()
