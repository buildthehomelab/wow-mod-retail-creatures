#!/usr/bin/env python3
"""Retail's cast sounds for 3.3.5a spells: a spell gets its own SpellVisual, a copy of the one it
has, whose creature sound (a mount's summon roar, a druid form's attack sound) is replaced by the
sounds retail plays when the spell is cast.

    RETAIL_SOURCE=cdn python3 spell_sounds.py --patch patch-z80-spells-and-items.MPQ \\
        --sound-dbc <folder with z40's SoundEntries.dbc and CreatureSoundData.dbc> \\
        --cache ~/retail-cache --druid-forms --out patch-z80-spells-and-items-new.MPQ

--patch is the realm's spell patch, the one that carries Spell.dbc and the SpellVisual tables
(Reforged's patch-P copies). The new sounds are SoundEntries rows playing retail's .ogg files from
the same patch, like pack option `sounds` does for creatures (RetailSounds in build_patch.py).

SoundEntries.dbc is a whole table, and the client keeps the copy of the patch that loads last, so
the patches that carry one build on each other: --sound-dbc is the table the client has before
this patch (z40's), and this patch's copy is that table, the rows other tools added to this patch
before (kept as they are) and this tool's. A patch that loads after this one and carries the table
too (z88) builds on this patch's copy.

As a library (wow-mod-retail-collection's build_items.py and wow-mod-retail-pets' build_pets.py
give every mount its summon sound):

    sounds = SpellSounds(files, sound_dbc, cache, BLOCKS["mounts"], tmp)
    sounds.give(spell, sounds.cast_kits(retail_spell) or sounds.creature_kits(retail_display))
    sounds.write()

A block is a tool's own ids (BLOCKS): its rows are made again on every build, so running a tool on
its own output changes nothing.
"""
import argparse
import collections
import ctypes
import os
import struct
import sys
import tempfile

from build_patch import RetailSounds, f32, pack_mpq, read_dbc
from fetch import casc, db2, db2_rows

# A tool's ids: SpellVisual, SpellVisualKit and SoundEntries rows.
Block = collections.namedtuple("Block", "name visuals kits entries")
BLOCKS = {
    "druid-forms": Block("druid-forms", (90900, 90949), (90900, 90949), (993000, 993099)),
    "mounts": Block("mounts", (91000, 92999), (91000, 93999), (993100, 994999)),
    "pets": Block("pets", (94000, 94199), (94000, 94399), (995000, 995499)),  # wow-mod-retail-pets' mounts
}

# The druid forms a player shifts into, with the stock visual each has: retail plays one
# shapeshift sound for all of them where the client plays a creature's attack.
DRUID_FORMS = {5487: 653, 9634: 4227, 768: 4228, 783: 4228, 1066: 775, 33943: 8128, 40120: 8128,
               24858: 9302, 33891: 8598}
# Travel Form has no visual of its own in retail; Cat Form's is the one it shares here.
RETAIL_SPELL = {783: 768, 9634: 5487, 40120: 33943}

SPELL_FIELDS, F_VISUAL = 234, 131
VISUAL_FIELDS, KIT_FIELDS, KIT_SOUND = 32, 38, 15
# SpellVisual kit columns: precast, cast, impact, state, state done, channel; caster and target impact.
KIT_SLOTS = (1, 2, 3, 4, 5, 6, 14, 15)
FREE_SLOTS = (2, 3)  # where a sound with no creature sound to replace goes: cast, then impact
SOUND_ONLY_KIT = 2977  # SummonWarhorseImpact: a kit that plays its sound and nothing else
# Retail SpellVisualEvent.StartEvent: 3 the cast, 6 the impact (1 precast and 8 aura end stay stock).
CAST_EVENTS = ("3", "6")
SOUNDKIT_EFFECT = "5"  # SpellVisualKitEffect.EffectType: a SoundKit
TABLES = ("Spell", "SpellVisual", "SpellVisualKit")
# Retail's levels are too low for this client: a kit's volume and distances are mixed for retail's
# engine, and the druid's shapeshift (0.35) or a Mechacycle's engine (cut off at 8 yards) can't be
# heard here. A stock summon sound is 0.7-0.8, full within 8 yards and cut off at 45, so a new row
# carries at least that far, and a spell's loudest sound is at least that loud. Its other sounds
# keep retail's volume: most are quiet layers on top of it.
SE_VOLUME, SE_MIN_DISTANCE, SE_DISTANCE_CUTOFF = 24, 26, 27
LOUDEST_VOLUME, MIN_DISTANCE, DISTANCE_CUTOFF = 0.7, 8.0, 45.0


def dbc(table):
    return "DBFilesClient\\%s.dbc" % table


def write_table(path, table):
    rows, strings, fields = table
    with open(path, "wb") as f:
        f.write(struct.pack("<4s4I", b"WDBC", len(rows), fields, fields * 4, len(strings)))
        for r in rows:
            f.write(struct.pack("<%dI" % fields, *r))
        f.write(strings)


def string_at(strings, at):
    return strings[at:strings.index(b"\0", at)].decode("latin-1")


def float_of(value):
    return struct.unpack("<f", struct.pack("<I", value))[0]


class SpellSounds:
    def __init__(self, files, sound_dbc, cache, block, tmp):
        for t in TABLES:
            if dbc(t) not in files:
                sys.exit("the spell patch has no %s.dbc" % t)
        self.files, self.cache, self.block, self.tmp = files, cache, block, tmp
        self.spells = read_dbc(files[dbc("Spell")])
        self.visuals = read_dbc(files[dbc("SpellVisual")])
        self.kits = read_dbc(files[dbc("SpellVisualKit")])
        for table, fields, name in ((self.spells, SPELL_FIELDS, "Spell"), (self.visuals, VISUAL_FIELDS, "SpellVisual"),
                                    (self.kits, KIT_FIELDS, "SpellVisualKit")):
            if table[2] != fields:
                sys.exit("%s.dbc has %d fields, not %d" % (name, table[2], fields))
        # This block's rows go: they are made again from what the caller gives, and written back
        # where they were (a table is never re-sorted; new rows go at the end).
        self.order = {t: {r[0]: i for i, r in enumerate(table[0])} for t, table in
                      (("SpellVisual", self.visuals), ("SpellVisualKit", self.kits))}
        inside = lambda r, span: span[0] <= r[0] <= span[1]
        self.visuals[0][:] = [r for r in self.visuals[0] if not inside(r, block.visuals)]
        self.kits[0][:] = [r for r in self.kits[0] if not inside(r, block.kits)]
        self.visual_by_id = {r[0]: r for r in self.visuals[0]}
        self.kit_by_id = {r[0]: r for r in self.kits[0]}
        self.spell_by_id = {r[0]: r for r in self.spells[0]}

        # The sound table: the one the client has before this patch, then the rows other tools
        # put in this patch's copy, then this block's.
        base = read_dbc(os.path.join(sound_dbc, "SoundEntries.dbc"))
        have = {r[0] for r in base[0]}
        self.base_entries = len(base[0])
        self.order["SoundEntries"] = {}
        if dbc("SoundEntries") in files:
            mine = read_dbc(files[dbc("SoundEntries")])
            self.order["SoundEntries"] = {r[0]: i for i, r in enumerate(mine[0])}
            for r in mine[0]:
                if r[0] not in have and not inside(r, block.entries):
                    r = list(r)
                    for col in list(range(2, 13)) + [23]:  # name, files, folder
                        r[col] = self._string(base[1], string_at(mine[1], r[col]))
                    base[0].append(r)
        work = os.path.join(tmp, "sound-dbc-" + block.name)
        os.makedirs(work, exist_ok=True)
        write_table(os.path.join(work, "SoundEntries.dbc"), base)
        with open(os.path.join(sound_dbc, "CreatureSoundData.dbc"), "rb") as src, \
                open(os.path.join(work, "CreatureSoundData.dbc"), "wb") as dst:
            dst.write(src.read())
        top = max(r[0] for r in base[0]) + 1
        self.sounds = RetailSounds(work, cache, {"first_entry": top, "first_data": 1 << 30})
        self.sounds.next_entry = block.entries[0]
        # A retail kit an earlier patch already made a row for (druid form sounds in z40) is that row.
        # Only that patch's rows: another block's would go or move when its tool runs again.
        for r in base[0][:self.base_entries]:
            name = string_at(base[1], r[2])
            if name.startswith("RetailKit"):
                self.sounds.entries.setdefault(name[len("RetailKit"):], r[0])

        self.next_visual, self.next_kit = block.visuals[0], block.kits[0]
        self.made_visuals, self.made_kits = {}, {}
        self.new_rows, self.retail_volume = {}, {}
        self.warnings = []
        self._retail = None

    @staticmethod
    def _string(strings, text):
        if not text:
            return 0
        at = len(strings)
        strings.extend(text.encode("latin-1") + b"\0")
        return at

    # --- retail ---------------------------------------------------------------------------------

    def _load_retail(self):
        if self._retail:
            return self._retail
        by_spell = collections.defaultdict(list)
        for r in db2_rows(self.cache, "SpellXSpellVisual"):
            by_spell[r["SpellID"]].append(r)
        events = collections.defaultdict(list)
        for r in db2_rows(self.cache, "SpellVisualEvent"):
            events[r["SpellVisualID"]].append(r)
        effects = collections.defaultdict(list)
        for r in db2_rows(self.cache, "SpellVisualKitEffect"):
            if r["EffectType"] == SOUNDKIT_EFFECT:
                effects[r["ParentSpellVisualKitID"]].append(r["Effect"])
        self._retail = by_spell, events, effects
        return self._retail

    def cast_kits(self, retail_spell):
        """The SoundKits retail plays when retail_spell is cast, as it is for everyone (the visual
        with no condition on caster or viewer), the creature's own sounds first."""
        by_spell, events, effects = self._load_retail()
        rows = sorted(by_spell.get(str(retail_spell), ()), key=lambda r: int(r["ID"]))
        plain = [r for r in rows if r["DifficultyID"] == "0" and all(
            r[c] == "0" for c in ("CasterPlayerConditionID", "CasterUnitConditionID",
                                  "ViewerPlayerConditionID", "ViewerUnitConditionID"))]
        kits = []
        for x in plain[:1] or rows[:1]:
            for e in sorted(events.get(x["SpellVisualID"], ()), key=lambda e: int(e["ID"])):
                if e["StartEvent"] in CAST_EVENTS:
                    for k in effects.get(e["SpellVisualKitID"], ()):
                        if k not in kits:
                            kits.append(k)
        return sorted(kits, key=lambda k: not self._is_creature_kit(k))

    def _is_creature_kit(self, kit):
        files = self.sounds.kit_files.get(kit, ())
        if not files:
            return False
        if self.sounds.names is None:
            from fetch import listfile
            self.sounds.names = listfile(self.cache, {f for fs in self.sounds.kit_files.values() for f, _q in fs})
        return any((self.sounds.names.get(f) or "").startswith("sound/creature/") for f, _q in files)

    def creature_kits(self, retail_display):
        """For a spell retail gives no cast sound: the aggro sound (or alert, or attack) of the
        retail creature display it summons."""
        cdi = self.sounds.retail_cdi if hasattr(self.sounds, "retail_cdi") else None
        if cdi is None:
            cdi = self.sounds.retail_cdi = db2(self.cache, "CreatureDisplayInfo")
            self.sounds.retail_cmd = db2(self.cache, "CreatureModelData")
        d = cdi.get(str(retail_display))
        if not d:
            return []
        sound = d["SoundID"]
        if sound == "0":
            m = self.sounds.retail_cmd.get(d["ModelID"])
            sound = m["SoundID"] if m else "0"
        s = self.sounds.retail.get(sound)
        if not s:
            return []
        for field in ("SoundAggroID", "SoundAlertID", "SoundExertionID"):
            if s[field] != "0":
                return [s[field]]
        return []

    # --- 3.3.5a rows ----------------------------------------------------------------------------

    def _creature_sound(self, kit_id):
        kit = self.kit_by_id.get(kit_id)
        entry = kit and self.sounds.base_se.get(kit[KIT_SOUND])
        if not entry:
            return False
        folder = string_at(self.sounds.se[1], entry[23]).lower().replace("/", "\\")
        return folder.startswith("sound\\creature")

    def _entry(self, kit):
        made = len(self.sounds.added_entries)
        entry = self.sounds.entry_for(kit, self.warnings)
        if self.sounds.next_entry > self.block.entries[1] + 1:
            sys.exit("block %s: out of SoundEntries ids" % self.block.name)
        if len(self.sounds.added_entries) > made:
            row = self.new_rows[entry] = self.sounds.se[0][-1]
            self.retail_volume[entry] = float_of(row[SE_VOLUME])
            row[SE_MIN_DISTANCE] = f32(max(float_of(row[SE_MIN_DISTANCE]), MIN_DISTANCE))
            row[SE_DISTANCE_CUTOFF] = f32(max(float_of(row[SE_DISTANCE_CUTOFF]), DISTANCE_CUTOFF))
        return entry

    def _volume(self, entry):
        """A row's volume as retail or the client has it, whatever an earlier spell raised it to."""
        if entry in self.retail_volume:
            return self.retail_volume[entry]
        row = self.sounds.base_se.get(entry)
        return float_of(row[SE_VOLUME]) if row else 0.0

    def _raise_loudest(self, entries):
        loudest = max(entries, key=self._volume, default=None)
        if loudest in self.new_rows and self._volume(loudest) < LOUDEST_VOLUME:
            self.new_rows[loudest][SE_VOLUME] = f32(LOUDEST_VOLUME)

    def _kit(self, source, entry):
        key = (source, entry)
        if key not in self.made_kits:
            if self.next_kit > self.block.kits[1]:
                sys.exit("block %s: out of SpellVisualKit ids" % self.block.name)
            row = list(self.kit_by_id[source])
            row[0], row[KIT_SOUND] = self.next_kit, entry
            self.next_kit += 1
            self.kits[0].append(row)
            self.made_kits[key] = row[0]
        return self.made_kits[key]

    def give(self, spell_id, retail_kits, template=None):
        """Gives the spell its own visual: a copy of template (default: the visual it has) with its
        creature sound replaced by retail_kits, in priority order. Returns the visual's id."""
        spell = self.spell_by_id.get(spell_id)
        if not spell:
            sys.exit("Spell.dbc has no spell %d" % spell_id)
        source = template or spell[F_VISUAL]
        if self.block.visuals[0] <= source <= self.block.visuals[1]:
            sys.exit("spell %d already has a visual of block %s: give its stock one as template"
                     % (spell_id, self.block.name))
        visual = list(self.visual_by_id[source])
        creature = [c for c in KIT_SLOTS if visual[c] and self._creature_sound(visual[c])]
        free = [c for c in FREE_SLOTS if not visual[c]]
        # A sound gets a row only when a slot takes it: retail's third and later are left out.
        kits, entries = list(retail_kits), []
        while kits and len(entries) < len(creature) + len(free):
            entry = self._entry(kits.pop(0))
            if entry:
                entries.append(entry)
        self._raise_loudest(entries)
        left = len(kits)
        for col in creature:  # the creature sound: retail's first sound, or none
            visual[col] = self._kit(visual[col], entries.pop(0) if entries else 0)
        for col in free:  # retail's other sounds, in the cast and impact slots the visual leaves empty
            if entries:
                visual[col] = self._kit(SOUND_ONLY_KIT, entries.pop(0))
        if left:
            self.warnings.append("spell %d: %d of retail's sounds left out (no free kit slot)" % (spell_id, left))
        key = tuple(visual[1:])
        if key not in self.made_visuals:
            if self.next_visual > self.block.visuals[1]:
                sys.exit("block %s: out of SpellVisual ids" % self.block.name)
            visual[0] = self.next_visual
            self.next_visual += 1
            self.visuals[0].append(visual)
            self.made_visuals[key] = visual[0]
        spell[F_VISUAL] = self.made_visuals[key]
        return spell[F_VISUAL]

    def write(self):
        """The four tables and the sound files, back into files."""
        for t, table in (("SpellVisual", self.visuals), ("SpellVisualKit", self.kits)):
            before = self.order[t]
            table[0].sort(key=lambda r: (r[0] not in before, before.get(r[0], r[0])))
        # The client's table before this patch stays first, in its order; the rest as this patch had them.
        rows, before = self.sounds.se[0], self.order["SoundEntries"]
        rows[self.base_entries:] = sorted(rows[self.base_entries:], key=lambda r: (r[0] not in before, before.get(r[0], 0)))
        for t, table in (("Spell", self.spells), ("SpellVisual", self.visuals), ("SpellVisualKit", self.kits)):
            write_table(self.files[dbc(t)], table)
        se = os.path.join(self.tmp, "SoundEntries-%s.dbc" % self.block.name)
        write_table(se, self.sounds.se)
        self.files[dbc("SoundEntries")] = se
        size = 0
        for name, fdid in self.sounds.files.items():
            path = os.path.join(self.tmp, "%d.ogg" % fdid)
            if not os.path.isfile(path):
                with open(path, "wb") as f:
                    f.write(casc(self.cache, fdid))
            size += os.path.getsize(path)
            self.files[name] = path
        print("block %s: %d SpellVisual, %d SpellVisualKit, %d SoundEntries rows, %d sound files (%.1f MB)" % (
            self.block.name, len(self.made_visuals), len(self.made_kits), len(self.sounds.added_entries),
            len(self.sounds.files), size / 1e6))
        for w in self.warnings:
            print("  " + w)


# --- MPQ ---------------------------------------------------------------------------------------

def unpack(mpq, folder):
    """Every file of the patch: {archive name: local path}."""
    lib = ctypes.CDLL(os.environ.get("STORMLIB", "/usr/local/lib/libstorm.dylib"))
    H = ctypes.c_void_p
    lib.SFileOpenArchive.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(H)]
    lib.SFileFindFirstFile.argtypes = [H, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.SFileFindFirstFile.restype = H
    lib.SFileFindNextFile.argtypes = [H, ctypes.c_char_p]
    lib.SFileFindClose.argtypes = [H]
    lib.SFileExtractFile.argtypes = [H, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    lib.SFileCloseArchive.argtypes = [H]
    h = H()
    if not lib.SFileOpenArchive(mpq.encode(), 0, 0x100, ctypes.byref(h)):
        sys.exit("can't open " + mpq)
    names, found = [], ctypes.create_string_buffer(2048)  # SFILE_FIND_DATA starts with the name
    find = lib.SFileFindFirstFile(h, b"*", found, None)
    while find:
        name = found.value.decode("latin-1")
        if name not in ("(listfile)", "(attributes)", "(signature)"):
            names.append(name)
        if not lib.SFileFindNextFile(find, found):
            lib.SFileFindClose(find)
            break
    files = {}
    for i, name in enumerate(names):
        local = os.path.join(folder, "%05d" % i)
        if not lib.SFileExtractFile(h, name.encode("latin-1"), local.encode(), 0):
            sys.exit("can't extract %s from %s" % (name, mpq))
        files[name] = local
    lib.SFileCloseArchive(h)
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch", required=True, help="the realm's spell patch (Spell.dbc and the SpellVisual tables)")
    ap.add_argument("--sound-dbc", required=True,
                    help="folder with the SoundEntries.dbc and CreatureSoundData.dbc the client has before this patch")
    ap.add_argument("--cache", required=True, help="the download folder")
    ap.add_argument("--druid-forms", action="store_true", help="the druid forms get retail's shapeshift sound")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not args.druid_forms:
        sys.exit("nothing to do: give --druid-forms")
    with tempfile.TemporaryDirectory() as tmp:
        files = unpack(args.patch, tmp)
        sounds = SpellSounds(files, args.sound_dbc, args.cache, BLOCKS["druid-forms"], tmp)
        for spell, stock in sorted(DRUID_FORMS.items()):
            kits = sounds.cast_kits(RETAIL_SPELL.get(spell, spell))
            if not kits:
                sys.exit("retail plays no sound when spell %d is cast" % spell)
            sounds.give(spell, kits, template=stock)
        sounds.write()
        pack_mpq(args.out, [(local, name) for name, local in sorted(files.items())])
        print("%s: %d files, %.1f MB" % (args.out, len(files), os.path.getsize(args.out) / 1e6))


if __name__ == "__main__":
    main()
