# mod-retail-creatures

Tools that bring retail World of Warcraft creature models to a 3.3.5a client. They download the
retail models, convert them (animations included) to the 3.3.5a model format, and build a client
patch that adds each one as a creature display, along with the SQL a server needs for them.

This is the shared half of the look packs for
[wow-mod-transmog-plus](https://github.com/buildthehomelab/wow-mod-transmog-plus). A pack is its own
repo: a list of looks, a small `pack.json`, and what the build writes from them.

| Pack | Looks | Patch |
| --- | --- | --- |
| [wow-mod-retail-druid-forms](https://github.com/buildthehomelab/wow-mod-retail-druid-forms) | 265 druid form looks | patch-R |
| [wow-mod-retail-shaman-totems](https://github.com/buildthehomelab/wow-mod-retail-shaman-totems) | 80 shaman totem looks | patch-V |
| [wow-mod-retail-warlock-pets](https://github.com/buildthehomelab/wow-mod-retail-warlock-pets) | warlock demon looks | patch-Y |

Nothing from Blizzard is stored here or in the packs; the files come from
[wago.tools](https://wago.tools) at build time.

## How it works

- `tools/m2creature.py` converts a retail model (MD21, v272/274) to 3.3.5a (MD20, v264):
  - It keeps the skin-0 mesh, all bones, and every animation 3.3.5 knows (ids 0-505). Animations
    in external `.anim` files are inlined.
  - It also keeps attachments, key bones, colours, texture animations, footstep events and the
    portrait camera.
  - Retail creature geosets are baked in per look, because the 3.3.5 client can't pick them.
  - Particles, ribbons, lights and the glow and shine passes are kept for a pack with
    `"effects": true`, and dropped otherwise:
    - Retail's particle emitter is 3.3.5's with 16 bytes of multi-texture settings added; a
      multi-texture emitter draws its first texture, and retail's blend mode 7 draws additive.
      Emitters that spawn models are left out, and so are those of a geoset the look hides
      and those that emit only in animations 3.3.5 lacks (the pygmy owl's skyriding trail).
    - The client reads the first value array of every emitter track (bar the enabled one),
      ribbon track (bar visibility) and light track (bar visible) when it sets the effect up,
      without checking there is one: an emitter with no emission rate arrays crashed it
      (ERROR #132 at 0x00833BEB). Such a track keeps one empty array.
    - Ribbons and lights are the same records in both clients. A retail ribbon blends up to
      three textures; every 3.3.5 ribbon has one texture and one material, so it draws the
      first (the ribbon's shape) and the scrolling masks are left out.
    - A retail batch draws one texture in 3.3.5. With effects it reads the coordinates its
      retail shader gives it (an environment-mapped shine moves with the camera), retail's
      extra passes are drawn, and a shader that adds a glowing texture onto the first (the
      AddAlpha family: glowing eyes, runes, a shine) gets that texture as an additive pass of
      its own. The common combo that multiplies an environment map in where the first
      texture's alpha says has no 3.3.5 pass and stays off.
    - Emitters coloured by their display (colour index 11-13) get the look's retail
      ParticleColor row, added to the `ParticleColor.dbc` in `--dbc`.
  - Some retail `.anim` files no longer match their model (left over from an older version).
    Those animations (mostly sit, sleep and emotes) are dropped, and the client falls back to
    another animation.
  - A bounding box far bigger than the mesh is pulled in to it. Retail's covers every animation
    (the goblin fire totem's box is 26 yards tall, because its rocket takes off when it dies),
    and a 3.3.5 model frame seems to size a creature by that box: such a look previewed as a
    speck.
  - A colour that doesn't change gets its key in every animation. Retail writes it once, in the
    model's first animation, and its client keeps using it; the 3.3.5 client gives an animation
    without a key black. A totem whose first animation isn't Stand stood as a black shape, and
    a glow went out whenever a form left its first animation. A colour's alpha track with no
    arrays at all (a colour retail never fades) gets one opaque key: the client's own models
    never leave one empty, and the Tuskarr Hermit Crab's body, drawn through two such colours,
    didn't show.
- `tools/check_m2.py` checks a converted model the way the client reads it. The build fails on
  a model it rejects. That includes triangles the client's mouse hit test would read outside
  its buffer (a submesh's triangle pointing at another submesh's vertices, or a collision
  triangle past the collision vertices): such a model draws fine and crashes the client, now
  and then, when the mouse moves over it. It also rejects an effect track the client reads
  the first value array of when it has none (that one crashes the client as soon as the
  model loads).
- `tools/fetch.py` downloads and caches model files and tables from wago.tools.
- `tools/tact.py` reads the model files straight from Blizzard's CDN instead: set
  `RETAIL_SOURCE=cdn`. Its first use downloads 400 MB of indexes for the live retail build and
  takes a couple of minutes; after that a file is one request, wago.tools can be down, and every
  file comes from the same build, which stays pinned until `tact.py --refresh`. Tables still
  come from wago.tools (cached after the first download).
- `tools/census.py` lists every retail mount and battle pet against a client: which it already
  has, which need only textures, which need a converted model, and what the converter makes of
  each of those (it converts them all, animations and textures included, without building a
  patch). `--also <tsv>` (any file with a `retail_display` column) probes the models of those
  looks too, though the client has them or they are no creature model: for a pack that wants
  them as its own looks (wow-mod-retail-library does, for mounts the client lacks on models it
  has). A display there that no mount or pet wears is listed too, as kind `creature` (named
  after the file's `name` column), for a pack that wants a plain creature's look.
  `tools/client_files.py` writes what it needs to know about the client: the list of
  its `Creature\` files, and the three creature DBCs the client ends up using, which are also
  the ones `build_patch.py --dbc` wants.
- `tools/contact_sheet.py` turns a census into a review page: every look that needs a converted
  model, drawn from the converted 3.3.5a model (posed on the first frame of Stand, with the
  look's textures, by a small software renderer) next to Blizzard's own render of the retail
  look. Mounts are grouped by family (`tools/mount_families.py` gets the families from
  warcraftmounts.com), pets by pet type. Click a look to flag it and copy the flagged list out.
  It shows what the converter kept, one frame of it; particles, the other animations and the
  rider's seat still need a client.
- `tools/turntable.py` draws looks the same way from a ring of angles, on a transparent
  background at one scale, as a WebP strip of frames side by side (36 by default) that a
  launcher or web page can turn, for example a shop's preview of a mount. `--only` takes retail
  display ids: the census's, or any other skin of a model the census converted.
- `tools/build_patch.py` builds a pack: it downloads everything, converts each model once per
  geoset combination, and writes the patch. Each look becomes a CreatureDisplayInfo row on a
  CreatureModelData row cloned from the stock model it replaces, so sounds and footprints stay
  the same. With `--sql` it writes the pack's server SQL: the looks for mod-transmog-plus, one
  preview creature per look (a 3.3.5 model frame can show a creature, not a display id), and
  the server copies of the new DBC rows. Textures larger than `--max-texture` (default 1024)
  lose their top mip levels. Textures that alpha-keyed batches draw (fur, feathers, leaves) get
  their alpha stretched: retail keeps a texel from alpha 128, the 3.3.5 client only from 224,
  so as shipped they come out shredded. Where blended batches draw the same texture, the
  alpha-keyed ones get their own copy (`T<id>k.blp`, in a free skin slot for a slot texture).

## Building a pack

```bash
python3 tools/build_patch.py --pack <pack folder> --dbc <dbc dir> --cache ~/retail-cache --work <work dir> --out patch-X.MPQ --sql <pack folder>/data/sql/db-world/<module>.sql
```

`--pack` is the pack repo's checkout (default: the current folder). `--cache` keeps the downloads
and can be shared by every pack. `--only 95000,95001` builds a few displays for a test.

`--dbc` needs `CreatureDisplayInfo.dbc`, `CreatureModelData.dbc` and `AnimationData.dbc` as the
players' client has them: the copies from the last patch in its load order that ships each one. A
patch replaces whole files, so two packs on one realm stack: the later letter is built on the DBCs
of the earlier pack's finished patch, and is rebuilt whenever that one changes.

### HD creature packs

An HD creature pack ships its own `CreatureDisplayInfo.dbc` and `CreatureModelData.dbc`, so a look
pack's patch has to carry the HD pack's rows too: build it with `--dbc` pointing at the HD pack's
copies, and give it a letter that loads after the HD pack's (the druid forms' patch-R loads after
Project Reforged's patch-C). Built on the wrong DBCs, the HD pack's creatures lose their textures
(white armour on HD kodos, for one). Model ids the base already uses move past the look pack's
block.

A client whose own HD pack isn't the realm's can still use a small DBC-only patch that loads after
the look pack's:

```bash
python3 tools/build_patch.py --pack <pack folder> --dbc <that HD pack's DBCs + AnimationData.dbc> --cache ~/retail-cache --work <work dir> --out patch-Y.MPQ --dbc-only
```

## Writing a pack

A pack repo holds:

- `pack.json`:

  | Field | Meaning |
  | --- | --- |
  | `looks` | the data file, relative to the pack folder |
  | `root` | the folder in the client for the pack's models, such as `Creature\\RetailForms` |
  | `first_display` | CreatureDisplayInfo ids of the converted looks start here |
  | `first_model` | CreatureModelData ids start here (ids the base client uses move past the block) |
  | `preview_base` | creature_template entries for the previews, a block of 500; a look's entry is base + (display - first_display) unless its row has a `preview` |
  | `subname` | subname of the preview creatures; the SQL only ever removes creatures in the block that carry it |
  | `sort_base` | SortOrder of the pack's first look in `mod_transmog_plus_forms` |
  | `prune_textures` | leave out textures nothing converted draws with (the dropped particles and passes, without `effects`), and draw a two-texture effect mesh with the one that isn't a mask |
  | `template` | each kind of look and the stock CreatureDisplayInfo id it clones for sounds, blood, footprints and size |
  | `source` | where the looks come from, for the head of the generated SQL |

  A pack that is a library of models, not looks for a window to pick from, has three more:

  | Field | Meaning |
  | --- | --- |
  | `transmog` | `false`: no rows for mod-transmog-plus and no preview creatures, so the SQL is the DBC rows only (`preview_base`, `subname` and `sort_base` can then be left out) |
  | `plain` | `true`: the new rows carry no sounds and no footprints, since hundreds of unlike models can't all sound like the one stock creature their kind clones |
  | `effects` | `true`: keep particle emitters, ribbons, lights and the glow and shine passes; looks whose particles take the display's colours get ParticleColor rows (put the client's `ParticleColor.dbc` in `--dbc`) |
  | `customization_geosets` | `true`: a customizable model's look (a ChrModel display: dragonriding drakes, Delver's airships) shows the parts of each customization option's first choice, where retail's display names none; without it every alternative part (all the horns at once) is drawn |
  | `archives` | each kind of look and the MPQ its models and textures go into; with `--data-dir`, those archives are written there and `--out` holds the two DBCs only, so the big archives don't change when another patch's DBC rows do. Two kinds can name the same archive |

- The data file, tab-separated, one look per row: `display` (the 3.3.5 display id; never change
  one a player may have picked), `retail_display` (empty for a look the client already has: no
  model, only the SQL), `form` (the kind, a key of `template`), `tier` (the individual
  progression phase that unlocks it, 0 = from the start), `model` (names the model's folder and
  file), `name`, and optionally `textures` (three file ids overriding the retail display's),
  `folder` and `preview`.
- A look can also be a display the client has, at another size or in other colours: leave
  `retail_display` empty and give `client_display` (the client's display id), with
  `display_scale` and/or `textures` (three retail file ids, 0 for none). The look's `display` is
  then a new row copied from the client's, on the client's own model: no model is converted and
  the client's own effects stay. The textures are stored next to that model. Use it where the
  client only has a model at the wrong size (a boss's), since the server swaps a display and not
  a scale, and where an HD client's model is retail's own, so retail's other colours fit it. A
  model row past the stock 3.3.5a table (an HD pack's) is given to the server with the look.
- `display_scale` on a converted look sets its display's scale in place of retail's.
- `hide` on a converted look (retail texture file ids, comma-separated) leaves out every mesh
  drawn with those textures: a mount's saddle, say, which retail always draws (it's no geoset).
  The look gets a model of its own, so other looks on that retail model keep those meshes.
  `s0`, `s1` or `s2` in it names a creature skin slot instead: for a look that leaves that slot
  empty, where retail draws nothing (a horse without the banner its model carries), in place of
  borrowing another look's texture for it.
- A script that writes the data file from retail's tables (`fetch.py` has the downloads), if the
  list isn't kept by hand.
- For the server: the generated SQL under `data/sql/db-world/`, and `src/<module>_loader.cpp`
  with an empty `Add<module>Scripts()`, which AzerothCore calls for every folder in `modules/`.

`form` has to be a kind mod-transmog-plus knows how to put on (a druid form, a totem element): a
pack for something new needs that module to learn the kind first. Pick id blocks no other pack or
module uses.

## Requirements

- Python 3.10+ (standard library only; `contact_sheet.py` and `turntable.py` also need numpy and Pillow).
- [StormLib](https://github.com/ladislav-zezula/StormLib) as a shared library. The default is
  `/usr/local/lib/libstorm.dylib`; point `STORMLIB` at yours.

## Troubleshooting

- **A creature shows as nothing, or a white model**: that client lacks the pack's patch, or
  another patch that ships the creature DBCs loads after it. Build the small `--dbc-only` patch
  above for that client.
- **One pack's looks vanish after adding another pack's patch**: the later patch was built on
  DBCs without the earlier pack's rows. Build it with `--dbc` from the earlier patch.
- **A look's preview tile is empty, or the look is tiny in a model frame**: its bounding box is
  far bigger than its mesh. The converter fits the box now; rebuild the patch.
- **A look is a black shape, in a preview tile and in the world**: its colour track has no key
  for the animation playing. The converter fills those now; rebuild the patch.
- **A converted model has no flames, drips or sparks**: those are particle effects, which the
  converter drops unless the pack has `"effects": true`. Mesh glows and scrolling effects stay.
- **A look's particles are the wrong colour, or white**: its emitters take the display's
  ParticleColor row, and `--dbc` had no `ParticleColor.dbc` to add it to (the build warns).
  Copy the client's (or the last patch's that ships one) into `--dbc` and rebuild.
- **White armour on other creatures after adding a pack's patch**: an HD creature pack's DBCs
  were replaced. Use the `--dbc-only` patch.
- **wago.tools answers 502/504**: the fetcher retries; rerun if it still gives up. Downloads are
  cached in `--cache`. If it stays down, build with `RETAIL_SOURCE=cdn`.
- **A model's skin or animations don't fit it** (an index out of range in the converter, or many
  animations dropped as stale): wago.tools doesn't serve every file from the same build. Build
  with `RETAIL_SOURCE=cdn`. A skin made for another version of its model draws as shards in
  the client; the converter refuses one (`StaleSkin`), and `build_patch.py` then takes that
  model, its skin and animations from Blizzard's CDN on its own (a warning says so), so the
  CDN's indexes get downloaded the first time it happens.
- **"shared skeleton ... not supported"**: the model keeps its bones and animations in a
  `.skel` file. The converter takes them in (the Tauralus mounts, and armoured ones whose
  skeleton is a child of the plain one's) unless the skeleton's animations are in `.anim`
  files or its bones differ from its parent's (the gnome skeleton pet).

## Credits

- Model and table data: Blizzard Entertainment, downloaded at build time from
  [wago.tools](https://wago.tools) or Blizzard's CDN; file names from the
  [wowdev community listfile](https://github.com/wowdev/wow-listfile).
- Keys of encrypted files: [wowdev/TACTKeys](https://github.com/wowdev/TACTKeys).
- Reference pictures on the review page: Blizzard's render server and
  [Warcraft Mounts](https://www.warcraftmounts.com), linked from the page, not stored.
- M2 and TACT format notes: [wowdev.wiki](https://wowdev.wiki/M2) and
  [whoa](https://github.com/thunderbrewhq/whoa).

## License

MIT. See [LICENSE](LICENSE).

## Delve map feasibility audit

`tools/map_audit.py` creates an offline, hash-recorded inventory for an **explicit**
retail map ID. Supply a Map DB2 CSV export and community listfile; optionally inspect
an already downloaded WDT. It does not download, convert, or install game assets.

```bash
python3 tools/map_audit.py --map-csv Map.csv --listfile community-listfile.csv \
  --map-id 2680 --wdt earthcrawl.wdt --out earthcrawl-audit.json
python3 -m unittest discover -s tools -p 'test_map_audit.py'
```

The tool cross-checks the Map row's WDT file ID against its named map directory,
rejects truncated chunk streams, records input hashes, and lists directory files.
The optional WDT's build/file identity remains the caller's responsibility: chunk
validation alone cannot authenticate that identity. Earthcrawl has multiple map
entries in modern metadata; never select one by display name alone.

This is **not a complete dependency manifest**: world models, textures, doodads,
file-ID references and other external dependencies still need recursive inspection.
A community listfile does not prove a file exists in a particular build. Use the
pinned CDN retrieval path documented above for reproducible conversion inputs;
unpinned metadata/file-service downloads are discovery evidence only. A valid WDT
also does not establish compatibility with the 3.3.5 client or server extractors.
