"""Real Nexus file lists, captured 2026-08-28.

Kept as fixtures rather than fetched: these are the exact shapes that make
variant matching hard (nine sibling variants, same-version pairs, a version
field that disagrees with its own filename), and they must not change under a
test run.
"""

MAP_FOR_GOBLINS = [
    {"file_id": 48311, "version": "v2.0.5", "category_name": "OLD_VERSION",
     "file_name": "Vanilla or Randomizer - v2.0.5 10062 v2.0.5 2026-07-14T10-47Z ZTHtIFwXR.zip"},
    {"file_id": 48931, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "Graceborne - v2.1.2 10062 v2.1.2 2026-08-06T08-51Z ZTHtIFfdq.zip"},
    {"file_id": 48932, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "Elden Vins - v2.1.2 10062 v2.1.2 2026-08-06T08-52Z SitYwbVNx.zip"},
    {"file_id": 48933, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "Reborn - v2.1.2 10062 v2.1.2 2026-08-06T08-52Z Ls9XqlxeQ.zip"},
    {"file_id": 48934, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "GoldenAge (v3.6.1) - v2.1.2 10062 v2.1.2 2026-08-06T08-52Z KeUMabcde.zip"},
    {"file_id": 48935, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "GoldenAge (v3.6.5) - v2.1.2 10062 v2.1.2 2026-08-06T08-52Z hIDyEfZXz.zip"},
    {"file_id": 48936, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "ERTE - v2.1.2 10062 v2.1.2 2026-08-06T08-53Z IDyEfZXzP.zip"},
    {"file_id": 48937, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "Convergence 3.x - v2.1.2 10062 v2.1.2 2026-08-06T08-53Z KeUMi2m9p.zip"},
    {"file_id": 48938, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "ERR - v2.1.2 10062 v2.1.2 2026-08-06T08-53Z i2m9p03hh.zip"},
    {"file_id": 48939, "version": "v2.1.2", "category_name": "MAIN",
     "file_name": "Vanilla or Randomizer - v2.1.2 10062 v2.1.2 2026-08-06T08-53Z ZTHtIFwXQ.zip"},
]

NOFALLDEAD = [
    {"file_id": 48339, "version": "1", "category_name": "MAIN",
     "file_name": "NOFALLDEAD 10402 1 2026-07-15T03-58Z 2fNK8JduR.zip"},
    {"file_id": 48340, "version": "1", "category_name": "MAIN",
     "file_name": "NoFallDead Longtail Cat Version 10402 1 2026-07-15T04-00Z HqR4yjEgC.zip"},
]

BOSS_RESURRECTION = [
    {"file_id": 24924, "version": "2.0.1", "category_name": "MAIN",
     "file_name": "Boss Resurrection-2790-2-0-1-1720450828.zip"},
    {"file_id": 24925, "version": "2.0.1", "category_name": "MAIN",
     "file_name": "Boss Resurrection - Lite-2790-2-0-1-1720450846.zip"},
]

FOREVER_BUFFS = [
    {"file_id": 39767, "version": "v1.0", "category_name": "MAIN",
     "file_name": "CSV Included to merge-8644-v1-0-1756694897.rar"},
    {"file_id": 39807, "version": "v1.0", "category_name": "MAIN",
     "file_name": "Forever Buffs N all kinds of Buff included-8644-v1-0-1756805123.zip"},
]

CLEVERS = [
    {"file_id": 34558, "version": "25.0", "category_name": "OLD_VERSION",
     "file_name": "moveset_modpack 25.0-1928-25-0-1738532654.zip"},
    {"file_id": 48590, "version": "26.0", "category_name": "OLD_VERSION",
     "file_name": "moveset_modpack 26.0 1928 26.0 2026-07-23T18-55Z sVKWduz6C.zip"},
    {"file_id": 49639, "version": "26.1", "category_name": "MAIN",
     "file_name": "moveset_modpack 26.1 1928 26.1 2026-08-27T23-54Z xzSjGqMUM.zip"},
]

# The trap: the mod page reads version 1.5, but the only MAIN is the 1.2
# regulation. The 1.5 files are param source spreadsheets, not installable.
BETTER_BOWS = [
    {"file_id": 41004, "version": "1.2", "category_name": "MAIN",
     "file_name": "Regulation.bin-4628-1-2-1760355506.7z"},
    {"file_id": 41005, "version": "1.5", "category_name": "ARCHIVED",
     "file_name": "CSV files-4628-1-5-1760355583.7z"},
    {"file_id": 41410, "version": "1.5", "category_name": "OPTIONAL",
     "file_name": "CSV files-4628-1-5-1761489294.7z"},
]
