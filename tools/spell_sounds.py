#!/usr/bin/env python3
"""Retail's cast sounds for 3.3.5a spells: a spell gets its own SpellVisual, a copy of the one it
has, whose creature sound (a mount's summon roar, a druid form's attack sound) is replaced by the
sounds retail plays when the spell is cast.

    RETAIL_SOURCE=cdn python3 spell_sounds.py --patch patch-z80-spells-and-items.MPQ \\
        --sound-dbc <folder with z40's SoundEntries.dbc and CreatureSoundData.dbc> \\
        --cache ~/retail-cache --druid-forms --out patch-z80-spells-and-items-new.MPQ

--class-spells gives class abilities retail's sound in each part of the cast (see give_phases and
retail_phases). --weather-patch and --weather-out make the new z88 with the same rows:

    RETAIL_SOURCE=cdn python3 spell_sounds.py --patch patch-z80-spells-and-items.MPQ \\
        --sound-dbc <folder with z40's SoundEntries.dbc and CreatureSoundData.dbc> \\
        --cache ~/retail-cache --class-spells --out patch-z80-spells-and-items-new.MPQ \\
        --weather-patch patch-z88-weather.MPQ --weather-out patch-z88-weather-new.MPQ

--only limits it to some abilities, and --tables-only leaves the rest of --patch out, for a test
patch that loads after every other (--sound-dbc then holds z88's SoundEntries.dbc).

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
import json
import os
import struct
import subprocess
import sys
import tempfile

from build_patch import RetailSounds, SE_COMBAT, SE_FLAGS, add_string, f32, pack_mpq, read_dbc
from fetch import casc, db2, db2_rows

# A tool's ids: SpellVisual, SpellVisualKit and SoundEntries rows.
Block = collections.namedtuple("Block", "name visuals kits entries")
BLOCKS = {
    "druid-forms": Block("druid-forms", (90900, 90949), (90900, 90949), (993000, 993099)),
    "mounts": Block("mounts", (91000, 92999), (91000, 93999), (993100, 994999)),
    "pets": Block("pets", (94000, 94199), (94000, 94399), (995000, 995499)),  # wow-mod-retail-pets' mounts
    "class-spells": Block("class-spells", (96000, 99999), (96000, 99999), (996000, 999999)),
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
# Retail's StartEvent and the SpellVisual kit columns that play the same part of a cast: precast,
# cast, impact (the kit, then target and caster impact), aura start, aura end, channel.
PHASE_SLOTS = {"1": (1,), "3": (2,), "6": (3, 15, 14), "7": (4,), "8": (5,), "11": (6,)}
SE_LOOPS = 0x200  # SoundEntries.Flags of a precast loop, in retail's SoundKit.Flags too
LOOP_EVENTS = ("1", "7", "11")  # parts that last: the loop is the sound to keep, not a one-shot layered on it
AURA_START = "7"
# Where a one-shot layered on a loop goes when retail has no sound there: a channel's start is the
# cast, an aura's the impact. A precast's has nowhere to go.
LOOP_ONE_SHOT = (("11", "3"), (AURA_START, "6"))
# SpellDuration ids of 20 seconds or less at their longest. An aura that lasts longer plays nothing while
# it lasts: players keep those up for minutes, a loop plays for all that time from everyone who has one,
# and a one-shot may play again when the unit comes into view. Its one-shot plays at the impact instead,
# and takes the place of a stock aura sound. Duration 0 is a spell with no aura, whatever retail's has.
SHORT_DURATIONS = {1, 8, 18, 27, 28, 29, 31, 32, 35, 36, 37, 38, 39, 65, 66, 85, 105, 165, 187, 285, 305, 327, 328,
                   387, 407, 447, 487, 507, 508, 549, 551, 553, 555, 564, 568, 571, 574, 575, 578, 582, 583, 584,
                   589, 592, 593, 596, 597, 598}
F_DURATION, F_FAMILY = 40, 208
# A sound in flight: retail's SpellVisualMissile.SoundEntriesID, where 3.3.5a's SpellVisual has a column.
# Only a loop, since one that plays once stops before a long flight ends.
MISSILE = "missile"
V_HAS_MISSILE, V_MISSILE_MODEL, V_MISSILE_SOUND = 7, 8, 11
# A slot plays one sound, so retail's layers on a one-shot part are mixed into one file: this many
# layers, and this many files for the row to pick from.
MIX_LAYERS, MIX_FILES = 3, 4
KEEP_STOCK_SHARE = 0.8  # of the loudest kept layer's volume, for a stock layer to be more than a background
MIX_FOLDER = "sound\\spells\\retailmix"
# The retail spell an ability sounds like, where the rules in retail_phases pick another; by name, or
# by name and SpellFamilyName where two classes share the name.
CLASS_RETAIL = {"Blast Wave": 157981, "Blizzard": 190356, "Chain Lightning": 12058, "Combustion": 190319, "Dispel Magic": 528, "Drain Soul": 198590,
                "Flame Shock": 188389, "Frost Shock": 196840, "Heal": 2060, "Healing Wave": 77472, "Holy Light": 82326,
                "Holy Nova": 132157, "Lava Burst": 51505, "Lesser Heal": 2060, "Lightning Bolt": 9532, "Serpent Sting": 271788,
                "Starfire": 194153, "Steady Shot": 56641, ("Nature's Swiftness", 11): 378081}
# Abilities whose id retail reused for another spell (a mere rename, Curse of Agony's, keeps it).
REUSED_IDS = {"Circle of Healing", "Cure Disease", "Demoralizing Roar", "Dispel Magic", "Frostfire Bolt", "Holy Wrath",
              "Judgements of the Wise", "Lesser Heal", "Pestilence", "Unholy Presence"}
# Names too common for retail's spells of that name to say what this ability sounds like.
COMMON_NAMES = {"Bash", "Blood Corruption", "Call Pet", "Chilled", "Counterattack", "Cower", "Enrage", "Feed Pet", "Focused",
                "Inferno", "Slam!", "Vigilance"}
# Abilities that keep their sound: retail's is another school's or class's (Drain Mana's is Obliterate's, Cure
# Poison's a monk's brew), or a loop that would play on and on (Ignite, Inferno, Sentry Totem, Amplify Magic).
WRONG_SOUND = {"Amplify Magic", "Backlash", "Cold Blood", "Cure Poison", "Dark Pact", "Detect Invisibility", "Drain Mana",
               "Enraged Regeneration", "Expose Weakness", "Holy Shield", "Holy Wrath", "Ignite", "Impact", "Inferno", "Sentry Totem",
               "Shadow Protection", "Volley", "Water Breathing"}
RETAIL_CLASSES = {"Warrior", "Paladin", "Hunter", "Rogue", "Priest", "Death Knight", "Shaman", "Mage", "Warlock", "Druid"}
FIRST_CUSTOM_SPELL = 80900  # modules' spells: retail's spell of that id is unrelated
STOCK_ENTRIES = 20000  # SoundEntries ids below this are the 3.3.5a game's (Reforged's start at 20190)
STOCK_VISUALS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "class_spell_visuals.json")
SKILL_CATEGORY_CLASS, SKILL_NAME = 7, 3  # SkillLine.CategoryID, and the enUS name's column
NOT_CLASS_SKILLS = {"Companions", "Internal", "Lockpicking", "Mounts"}
SLA_SKILL, SLA_SPELL, SPELL_NAME, SPELL_ATTRIBUTES = 1, 2, 136, 4
ATTR_PASSIVE = 0x40
TABLES = ("Spell", "SpellVisual", "SpellVisualKit")
# Retail's levels are too low for this client: a kit's volume and distances are mixed for retail's
# engine, and the druid's shapeshift (0.35) or a Mechacycle's engine (cut off at 8 yards) can't be
# heard here. A stock summon sound is 0.7-0.8, full within 8 yards and cut off at 45, so a new row
# carries at least that far, and a spell's loudest sound is at least that loud. Its other sounds
# keep retail's volume: most are quiet layers on top of it.
SE_VOLUME, SE_MIN_DISTANCE, SE_DISTANCE_CUTOFF = 24, 26, 27
LOUDEST_VOLUME, MIN_DISTANCE, DISTANCE_CUTOFF = 0.7, 8.0, 45.0
FORM_VOLUME = 1.0  # retail's shapeshift is a soft sound: at 0.7 it is still quiet in game


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
    def __init__(self, files, sound_dbc, cache, block, tmp, audible=False):
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
        base[0][:] = [r for r in base[0] if not inside(r, block.entries)]
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
        self.sounds = RetailSounds(work, cache, {"first_entry": top, "first_data": 1 << 30, "audible": audible})
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
        self._old_stems = None
        self._missiles = None
        self.mixes, self.mix_entries = {}, {}  # client path: [(retail file id, gain)]; layers: row

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

    def _visual_ids(self, retail_spell):
        """retail_spell's visual as it is for everyone (the one with no condition on caster or
        viewer), as a list of none or one."""
        by_spell, _events, _effects = self._load_retail()
        rows = sorted(by_spell.get(str(retail_spell), ()), key=lambda r: int(r["ID"]))
        plain = [r for r in rows if r["DifficultyID"] == "0" and all(
            r[c] == "0" for c in ("CasterPlayerConditionID", "CasterUnitConditionID",
                                  "ViewerPlayerConditionID", "ViewerUnitConditionID"))]
        return [x["SpellVisualID"] for x in plain[:1] or rows[:1]]

    def _events(self, retail_spell):
        _by_spell, events, _effects = self._load_retail()
        return [e for v in self._visual_ids(retail_spell)
                for e in sorted(events.get(v, ()), key=lambda e: int(e["ID"]))]

    def _missile_kit(self, retail_spell):
        if self._missiles is None:
            sounds = {}
            for r in sorted(db2_rows(self.cache, "SpellVisualMissile"), key=lambda r: int(r["ID"])):
                if r["SoundEntriesID"] != "0":
                    sounds.setdefault(r["SpellVisualMissileSetID"], r["SoundEntriesID"])
            self._missiles = {r["ID"]: sounds[r["SpellVisualMissileSetID"]] for r in db2_rows(self.cache, "SpellVisual")
                              if r["SpellVisualMissileSetID"] in sounds}
        return next((self._missiles[v] for v in self._visual_ids(retail_spell) if v in self._missiles), None)

    def _loops(self, kit):
        return int(self.sounds.kits.get(kit, {}).get("Flags", 0)) & SE_LOOPS

    def cast_kits(self, retail_spell):
        """The SoundKits retail plays when retail_spell is cast, the creature's own sounds first."""
        _by_spell, _events, effects = self._load_retail()
        kits = []
        for e in self._events(retail_spell):
            if e["StartEvent"] in CAST_EVENTS:
                for k in effects.get(e["SpellVisualKitID"], ()):
                    if k not in kits:
                        kits.append(k)
        return sorted(kits, key=lambda k: not self._is_creature_kit(k))

    def cast_phases(self, retail_spell):
        """{StartEvent: SoundKits} of retail_spell for the parts of a cast 3.3.5a has, the sound to
        keep first: the loop of a part that lasts, an impact on the target before one on the caster.
        MISSILE is the sound in flight."""
        _by_spell, _events, effects = self._load_retail()
        phases = collections.defaultdict(list)
        for e in self._events(retail_spell):
            if e["StartEvent"] in PHASE_SLOTS:
                for k in effects.get(e["SpellVisualKitID"], ()):
                    pair = (e["TargetType"] == "1", k)
                    if k not in [p[1] for p in phases[e["StartEvent"]]]:
                        phases[e["StartEvent"]].append(pair)
        out = {event: [k for _caster, k in sorted(pairs, key=lambda p: (not self._loops(p[1]) if event in LOOP_EVENTS else p[0]))]
               for event, pairs in phases.items()}
        missile = self._missile_kit(retail_spell)
        if missile:
            out[MISSILE] = [missile]
        return out

    def _load_names(self):
        if self.sounds.names is None:
            from fetch import listfile
            self.sounds.names = listfile(self.cache, {f for fs in self.sounds.kit_files.values() for f, _q in fs})

    def _is_creature_kit(self, kit):
        files = self.sounds.kit_files.get(kit, ())
        if not files:
            return False
        self._load_names()
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

    def _raise_loudest(self, entries, volume):
        loudest = max(entries, key=self._volume, default=None)
        if loudest in self.new_rows and self._volume(loudest) < volume:
            self.new_rows[loudest][SE_VOLUME] = f32(volume)

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

    def _in_other_block(self, visual):
        return any(b.visuals[0] <= visual <= b.visuals[1] for b in BLOCKS.values() if b != self.block)

    def _new_kits(self, phases):
        """phases without the kits no class spell should play: a creature's voice, an object's sound
        (retail's placeholder loops, and one-shots like a grenade on Concussive Shot), and only sounds
        the 3.3.5a game already has (retail reuses an old sound, or re-encodes it)."""
        self._load_names()
        if self._old_stems is None:
            rows, strings = self.sounds.se[0][:self.base_entries], self.sounds.se[1]
            self._old_stems = self.sounds._stems(string_at(strings, r[3 + i]) for r in rows if r[0] < STOCK_ENTRIES
                                                 for i in range(10) if r[3 + i])
        out = {}
        for event, kits in phases.items():
            kept = []
            for k in kits:
                paths = [self.sounds.names.get(f) or str(f) for f, _q in self.sounds.kit_files.get(k, ())]
                if paths and not self._is_creature_kit(k) and not any(p.startswith("sound/doodad/") for p in paths) \
                        and not self.sounds._stems(paths) <= self._old_stems:
                    kept.append(k)
            if kept:
                out[event] = kept
        return out

    def _entry_stems(self, entry):
        row = self.sounds.base_se.get(entry)
        if not row:
            return set()
        return self.sounds._stems(string_at(self.sounds.se[1], row[3 + i]) for i in range(10) if row[3 + i])

    def _stock_stems(self, kit_id):
        kit = self.kit_by_id.get(kit_id)
        return self._entry_stems(kit[KIT_SOUND]) if kit else set()

    def _kit_stems(self, kit):
        return self.sounds._stems(self.sounds.names.get(f) or "" for f, _q in self.sounds.kit_files.get(kit, ()))

    def _mix_entry(self, kits):
        """A row playing kits at once: its files are retail's, mixed with each kit's volume, and its
        levels the loudest kit's."""
        key = "+".join(kits)
        if key in self.mix_entries:
            return self.mix_entries[key]
        if self.sounds.next_entry > self.block.entries[1]:
            sys.exit("block %s: out of SoundEntries ids" % self.block.name)
        kit_rows = [self.sounds.kits[k] for k in kits]
        files = [self.sounds.kit_files[k] for k in kits]
        volumes = [float(k["VolumeFloat"]) ** 0.5 for k in kit_rows]
        se = self.sounds.se
        row = [0] * se[2]
        row[0] = self.sounds.next_entry
        self.sounds.next_entry += 1
        row[1] = int(kit_rows[0]["SoundType"]) if int(kit_rows[0]["SoundType"]) in self.sounds.types else SE_COMBAT
        row[2] = add_string(se[1], "RetailMix" + key)
        for i in range(min(MIX_FILES, max(len(f) for f in files))):
            name = "mix_%s_%d.ogg" % ("_".join(kits), i)
            row[3 + i] = add_string(se[1], name)
            row[13 + i] = 1
            mix = {}  # a file two kits share plays once, not twice as loud
            for layer, v in zip(files, volumes):
                mix.setdefault(layer[i % len(layer)][0], v / max(volumes))
            self.mixes["%s\\%s" % (MIX_FOLDER, name)] = list(mix.items())
        row[23] = add_string(se[1], MIX_FOLDER)
        row[SE_VOLUME] = f32(max(volumes))
        row[25] = int(kit_rows[0]["Flags"]) & SE_FLAGS & ~SE_LOOPS
        row[SE_MIN_DISTANCE] = f32(max([MIN_DISTANCE] + [float(k["MinDistance"]) for k in kit_rows]))
        row[SE_DISTANCE_CUTOFF] = f32(max([DISTANCE_CUTOFF] + [float(k["DistanceCutoff"]) for k in kit_rows]))
        row[28] = int(kit_rows[0]["EAXDef"])
        se[0].append(row)
        self.sounds.added_entries.append(row[0])
        self.new_rows[row[0]] = row
        self.retail_volume[row[0]] = max(volumes)
        self.mix_entries[key] = row[0]
        return row[0]

    def _phase_entry(self, spell_id, event, kits):
        """The row for a slot: the loop of a part that lasts, or the one-shots mixed."""
        layers = [k for k in kits if not self._loops(k)][:MIX_LAYERS]
        if (event in LOOP_EVENTS and self._loops(kits[0])) or not layers:
            if len(kits) > 1:
                self.warnings.append("spell %d event %s: %d of retail's layers left out" % (spell_id, event, len(kits) - 1))
            return self._entry(kits[0])
        return self._mix_entry(layers) if len(layers) > 1 else self._entry(layers[0])

    def _rank_phases(self, spell, phases):
        """(phases as this rank plays them, whether its aura's sound moved to the impact): see
        SHORT_DURATIONS, and a one-shot on a loop moves where it plays alone (LOOP_ONE_SHOT)."""
        out = {event: list(kits) for event, kits in phases.items()}
        moved = False
        if not spell[F_DURATION]:
            out.pop(AURA_START, None)
        elif AURA_START in out and spell[F_DURATION] not in SHORT_DURATIONS:
            one_shots = [k for k in out.pop(AURA_START) if not self._loops(k)]
            if one_shots and "6" not in out:
                out["6"], moved = one_shots, True
        for event, free in LOOP_ONE_SHOT:
            kits = out.get(event, ())
            if len(kits) > 1 and self._loops(kits[0]) and free not in out:
                out[free], out[event] = [k for k in kits[1:] if not self._loops(k)], kits[:1]
        return {event: kits for event, kits in out.items() if kits}, moved

    def _keeps_stock(self, stock_kit, event, kits, phases, unfiltered):
        """Whether a slot keeps its stock sound: it plays the sound retail's slot would, or a layer
        _new_kits left out of phases, unless that's only a quiet layer on a one-shot retail's kept
        kits are the main sound of."""
        stock = self._stock_stems(stock_kit)
        if stock & self._kit_stems(kits[0]):
            return True
        old = [k for k in unfiltered.get(event, ()) if k not in phases.get(event, ()) and stock & self._kit_stems(k)]
        if not old:
            return False
        volume = lambda k: float(self.sounds.kits[k]["VolumeFloat"])
        return event in LOOP_EVENTS or max(map(volume, old)) >= KEEP_STOCK_SHARE * max(map(volume, kits))

    def give_phases(self, spell_id, phases, unfiltered=None, loudest=LOUDEST_VOLUME):
        """Gives the spell its own visual: a copy of the one it has, with retail's sound in each part
        of the cast retail has one for (cast_phases). A kit keeps its effects and only its sound
        changes; a part the visual has no kit for gets one that only plays the sound. A part retail
        has no sound for keeps the stock sound, and so does one that already plays one of retail's
        files there (Reforged's mage and warlock spells, and the old layer unfiltered has where
        _new_kits left it out of phases), or plays the new sound in another part. A slot plays one
        sound, so retail's layers are mixed (_phase_entry). An aura's loop keeps retail's volume, a
        background to the sound that starts it. Returns the visual's id, or 0 if nothing changed."""
        spell = self.spell_by_id.get(spell_id)
        if not spell:
            sys.exit("Spell.dbc has no spell %d" % spell_id)
        if not spell[F_VISUAL]:  # a rank's hidden second half: the rank a player casts has the visual
            return 0
        visual = list(self.visual_by_id[spell[F_VISUAL]])
        entries, changes, missile = [], {}, 0
        self._load_names()
        unfiltered = unfiltered or phases
        everywhere = set().union(*(self._stock_stems(visual[c]) for c in KIT_SLOTS))
        rank_phases, moved = self._rank_phases(spell, phases)
        for event, kits in rank_phases.items():
            if event == MISSILE:
                if visual[V_HAS_MISSILE] and visual[V_MISSILE_MODEL] and self._loops(kits[0]) and \
                        not self._entry_stems(visual[V_MISSILE_SOUND]) & self._kit_stems(kits[0]):
                    missile = self._entry(kits[0])
                continue
            slots = PHASE_SLOTS[event]
            col = next((c for c in slots if visual[c] and self._stock_stems(visual[c])),
                       next((c for c in slots if visual[c]), slots[0]))
            if self._keeps_stock(visual[col], event, kits, phases, unfiltered) or everywhere & self._kit_stems(kits[0]):
                continue
            entry = self._phase_entry(spell_id, event, kits)
            if entry:
                changes[col] = entry
                if not (event == AURA_START and self._loops(kits[0])):
                    entries.append(entry)
        if moved and self._stock_stems(visual[4]) and any(c in changes for c in PHASE_SLOTS["6"]):
            changes[4] = 0
        if not changes and not missile:
            return 0
        self._raise_loudest(entries, loudest)
        if missile:
            visual[V_MISSILE_SOUND] = missile
        for col, entry in changes.items():
            visual[col] = self._kit(visual[col] or SOUND_ONLY_KIT, entry)
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

    def give(self, spell_id, retail_kits, template=None, loudest=LOUDEST_VOLUME):
        """Gives the spell its own visual: a copy of template (default: the visual it has) with its
        creature sound replaced by retail_kits, in priority order, the loudest of them at volume
        `loudest` at least. Returns the visual's id."""
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
        self._raise_loudest(entries, loudest)
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

    def _retail_file(self, fdid):
        path = os.path.join(self.tmp, "%d.ogg" % fdid)
        if not os.path.isfile(path):
            with open(path, "wb") as f:
                f.write(casc(self.cache, fdid))
        return path

    def _mix(self, name, layers):
        """One mono .ogg of the layers: ffmpeg's vorbis encoder can't write mono, so oggenc does."""
        path = os.path.join(self.tmp, name.rsplit("\\", 1)[-1])
        inputs = [a for fdid, _gain in layers for a in ("-i", self._retail_file(fdid))]
        gains = "".join("[%d:a]volume=%.4f[a%d];" % (i, gain, i) for i, (_fdid, gain) in enumerate(layers))
        mix = "".join("[a%d]" % i for i in range(len(layers))) + "amix=inputs=%d:duration=longest:normalize=0,alimiter=limit=0.95:level=0" % len(layers)
        subprocess.run(["ffmpeg", "-v", "error", "-y"] + inputs + ["-filter_complex", gains + mix, "-ac", "1", "-ar", "44100",
                                                                  "-bitexact", path + ".wav"], check=True)
        subprocess.run(["oggenc", "-Q", "-q", "4", "-s", "1", "-o", path, path + ".wav"], check=True)
        return path

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
            self.files[name] = self._retail_file(fdid)
            size += os.path.getsize(self.files[name])
        for name, layers in self.mixes.items():
            self.files[name] = self._mix(name, layers)
            size += os.path.getsize(self.files[name])
        print("block %s: %d SpellVisual, %d SpellVisualKit, %d SoundEntries rows, %d sound files, %d of them mixed (%.1f MB)" % (
            self.block.name, len(self.made_visuals), len(self.made_kits), len(self.sounds.added_entries),
            len(self.sounds.files) + len(self.mixes), len(self.mixes), size / 1e6))
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


def class_abilities(files, spells):
    """{ability name: its ranks' spell ids} of what a class learns: the class skills' abilities,
    without passives. Those skills also list the mounts and companions, pets' spells and a few of
    the game's own; class mask doesn't tell them apart, since talents and many last ranks have none."""
    skills = read_dbc(files[dbc("SkillLine")])
    classes = set()
    for r in skills[0]:
        name = string_at(skills[1], r[SKILL_NAME])
        if r[1] == SKILL_CATEGORY_CLASS and name not in NOT_CLASS_SKILLS and not name.startswith("Pet"):
            classes.add(r[0])
    by_id = {r[0]: r for r in spells[0]}
    abilities = collections.defaultdict(list)
    for r in read_dbc(files[dbc("SkillLineAbility")])[0]:
        if r[SLA_SKILL] in classes and r[SLA_SPELL] in by_id and not by_id[r[SLA_SPELL]][SPELL_ATTRIBUTES] & ATTR_PASSIVE:
            name = string_at(spells[1], by_id[r[SLA_SPELL]][SPELL_NAME])
            if r[SLA_SPELL] not in abilities[name]:
                abilities[name].append(r[SLA_SPELL])
    return abilities


def retail_names(cache):
    """({name: the retail player's spells of that name}, {name: all retail spells of that name}).
    A name goes without what's in brackets: Mangle (Cat) is retail's Mangle."""
    names = {r["ID"]: r["Name_lang"] for r in db2_rows(cache, "SpellName")}
    classes = {r["ID"] for r in db2_rows(cache, "SkillLine") if r["DisplayName_lang"] in RETAIL_CLASSES}
    player, everyone = collections.defaultdict(list), collections.defaultdict(list)
    for r in db2_rows(cache, "SkillLineAbility"):
        if r["SkillLine"] in classes and r["Spell"] in names:
            player[names[r["Spell"]]].append(r["Spell"])
    for spell, name in names.items():
        everyone[name].append(spell)
    return player, everyone


def retail_phases(sounds, name, family, ranks, player, everyone):
    """(retail spell, its cast_phases) of the spell that is this ability in retail: its own id, unless
    retail reused that; else the retail class spell of its name; else the sounds most spells of its
    name share, since retail's talent trees aren't in its skill tables and NPCs' copies of a spell
    keep its visual."""
    if name in WRONG_SOUND:
        return None, {}
    base = name.split(" (")[0]
    fixed = CLASS_RETAIL.get((name, family)) or CLASS_RETAIL.get(name)
    tries = [fixed] if fixed else []
    if name not in REUSED_IDS:
        tries += [s for s in ranks if s < FIRST_CUSTOM_SPELL]
    tries += player.get(base, ())
    for spell in tries:
        phases = sounds.cast_phases(spell)
        if phases:
            return spell, phases
    if name in COMMON_NAMES:
        return None, {}
    counts, first = collections.Counter(), {}
    for spell in sorted(everyone.get(base, ()), key=int):
        phases = sounds.cast_phases(spell)
        if phases:
            key = repr(sorted(phases.items()))
            counts[key] += 1
            first.setdefault(key, spell)
    if not counts:
        return None, {}
    spell = first[counts.most_common(1)[0][0]]
    return spell, sounds.cast_phases(spell)


def give_class_spells(sounds, files, only):
    """Every rank of each ability. STOCK_VISUALS keeps the visual each changed rank had, because a
    run on this tool's own output has no other way to tell (their rows go first, see SpellSounds)."""
    stock = {}
    if os.path.isfile(STOCK_VISUALS):
        with open(STOCK_VISUALS) as f:
            stock = {int(k): v for k, v in json.load(f).items()}
    for spell in sounds.spells[0]:
        if sounds.block.visuals[0] <= spell[F_VISUAL] <= sounds.block.visuals[1]:
            if spell[0] not in stock:
                sys.exit("spell %d has a visual of block %s and isn't in %s" % (spell[0], sounds.block.name, STOCK_VISUALS))
            spell[F_VISUAL] = stock[spell[0]]
    abilities = class_abilities(files, sounds.spells)
    player, everyone = retail_names(sounds.cache)
    for name in only or sorted(abilities):
        ranks = sorted(abilities.get(name, ()))
        if not ranks:
            sys.exit("no class ability is called %r" % name)
        ranks = [s for s in ranks if not sounds._in_other_block(sounds.spell_by_id[s][F_VISUAL])]
        families = collections.defaultdict(list)  # two classes' abilities can share a name: Death Coil
        for s in ranks:
            families[sounds.spell_by_id[s][F_FAMILY]].append(s)
        for family, family_ranks in families.items():
            retail, unfiltered = retail_phases(sounds, name, family, family_ranks, player, everyone)
            phases = sounds._new_kits(unfiltered)
            if not phases:
                print("  %s: %s" % (name, "no new sound in retail spell %s" % retail if retail else "keeps its sound"))
                continue
            changed = 0
            for s in family_ranks:
                had = sounds.spell_by_id[s][F_VISUAL]
                if sounds.give_phases(s, phases, unfiltered):
                    stock[s] = had
                    changed += 1
            print("  %s: retail spell %s, %d of %d ranks changed, events %s" % (name, retail, changed, len(family_ranks), sorted(phases)))
    with open(STOCK_VISUALS, "w") as f:
        json.dump({str(k): stock[k] for k in sorted(stock)}, f, indent=0)
        f.write("\n")


def add_rows(source, block, patch, out, tmp):
    """The weather patch (z88) with block's rows made again and the other SoundEntries rows of source
    (a new z80's table) it lacks, at the end of its own table: everything else in it stays as it is,
    Claws of Shirvallah too."""
    folder = os.path.join(tmp, "weather")
    os.makedirs(folder)
    files = unpack(patch, folder)
    rows, strings, fields = read_dbc(files[dbc("SoundEntries")])
    rows[:] = [r for r in rows if not block.entries[0] <= r[0] <= block.entries[1]]
    have = {r[0] for r in rows}
    theirs = read_dbc(source)
    added = 0
    for r in theirs[0]:
        if r[0] not in have:
            r = list(r)
            for col in list(range(2, 13)) + [23]:  # name, files, folder
                r[col] = SpellSounds._string(strings, string_at(theirs[1], r[col]))
            rows.append(r)
            added += 1
    write_table(files[dbc("SoundEntries")], (rows, strings, fields))
    pack_mpq(out, [(local, name) for name, local in sorted(files.items())])
    print("%s: %d SoundEntries rows added, %.1f MB" % (out, added, os.path.getsize(out) / 1e6))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patch", required=True, help="the realm's spell patch (Spell.dbc and the SpellVisual tables)")
    ap.add_argument("--sound-dbc", required=True,
                    help="folder with the SoundEntries.dbc and CreatureSoundData.dbc the client has before this patch")
    ap.add_argument("--cache", required=True, help="the download folder")
    ap.add_argument("--druid-forms", action="store_true", help="the druid forms get retail's shapeshift sound")
    ap.add_argument("--class-spells", action="store_true", help="class abilities get retail's sound in each part of the cast")
    ap.add_argument("--only", help="with --class-spells: these abilities, by name, separated by commas")
    ap.add_argument("--tables-only", action="store_true", help="leave the rest of --patch out of --out: a test patch")
    ap.add_argument("--out", required=True)
    ap.add_argument("--weather-patch", help="the realm's z88, which carries the SoundEntries.dbc the client keeps")
    ap.add_argument("--weather-out", help="where --weather-patch goes with this patch's new sound rows")
    args = ap.parse_args()
    if args.druid_forms == args.class_spells:
        sys.exit("give one of --druid-forms and --class-spells")
    with tempfile.TemporaryDirectory() as tmp:
        files = unpack(args.patch, tmp)
        if args.class_spells:
            sounds = SpellSounds(files, args.sound_dbc, args.cache, BLOCKS["class-spells"], tmp, audible=True)
            give_class_spells(sounds, files, args.only.split(",") if args.only else None)
        else:
            sounds = SpellSounds(files, args.sound_dbc, args.cache, BLOCKS["druid-forms"], tmp)
            for spell, stock in sorted(DRUID_FORMS.items()):
                kits = sounds.cast_kits(RETAIL_SPELL.get(spell, spell))
                if not kits:
                    sys.exit("retail plays no sound when spell %d is cast" % spell)
                sounds.give(spell, kits, template=stock, loudest=FORM_VOLUME)
        sounds.write()
        if args.tables_only:
            keep = {dbc(t) for t in TABLES} | {dbc("SoundEntries")} | set(sounds.sounds.files) | set(sounds.mixes)
            files = {name: local for name, local in files.items() if name in keep}
        pack_mpq(args.out, [(local, name) for name, local in sorted(files.items())])
        print("%s: %d files, %.1f MB" % (args.out, len(files), os.path.getsize(args.out) / 1e6))
        if args.weather_patch:
            add_rows(files[dbc("SoundEntries")], sounds.block, args.weather_patch, args.weather_out, tmp)


if __name__ == "__main__":
    main()
