# erm — Elden Ring mod manager

`erm` is a local, git-tracked mod manager and CLI for running Elden Ring Seamless Co-op
(ERSC) on Linux without risking a ban. Mod decisions (which mod, which version, which
hash) live in git as a lockfile; the actual downloaded archives live outside git in
`vendor/`. A friend clones the repo, runs `erm fetch`, and gets the exact same mod set
you're running.

## The ban model, in three sentences

The only thing that gets accounts banned is tampered save data (or a mod-injected DLL)
reaching FromSoft's EasyAntiCheat-gated online servers. Seamless Co-op never touches that
vector: its launcher starts `eldenring.exe` directly so EAC never loads, it matches
players over Steam Datagram Relay instead of FromSoft's matchmaking, and it writes to a
separate `.co2` save file that vanilla can't even read. The residual risk is entirely
human — accidentally taking an old or modded save into a **vanilla, EAC-on, online**
session — which is exactly what `erm doctor` and `erm quarantine` exist to stop.

## Quick start

```bash
cp secrets.env.example secrets.env
# edit secrets.env, set COOP_PASSWORD to your group's shared password

python3 erm fetch seamless-only     # downloads + sha256-verifies Seamless Co-op
python3 erm launch-option           # prints the Steam launch-option string
python3 erm apply                   # installs the mod into Game/, injects the password
python3 erm doctor                  # confirms it's safe to launch
```

Then in Steam: ELDEN RING → Properties → Launch Options → paste the string from
`erm launch-option`. Launch through Steam as normal. **No EAC splash screen on launch
means the mod loaded.** The title screen prints both the game build and the Seamless
Co-op version in the bottom-right corner — screenshot it before a session to confirm
everyone's in lockstep.

## Proton and GPU notes

Use **GE-Proton10-31** or **Proton-Experimental**. The Seamless Co-op docs still say
"Proton 8.0/7.0" — that's stale advice from before the mod was verified against newer
Proton builds. Only fall back to Proton 8.0 if `ersc.dll` fails to inject on 10.x, and if
that happens, it's worth a bug report since it hasn't been seen on current RDNA4/Mesa.

On a dual-GPU box, run once with `MESA_VK_DEVICE_SELECT=list %command%` to list the
Vulkan devices, find the discrete GPU's id, then prepend
`MESA_VK_DEVICE_SELECT=<vendor>:<device>` to the launch option so the game doesn't end up
on the integrated GPU. `erm launch-option` prints a commented reminder for this.

## The three profiles

Profiles are plain TOML files under `profiles/`, listing which mods to fetch and how.

| Profile | What it is | Lockstep requirement |
|---|---|---|
| `seamless-only` | Seamless Co-op by itself. The safe default. | Everyone needs the same ERSC version — nothing else. |
| `seamless-extras` | ERSC plus client-side cosmetic/QoL mods (loader, skip-intro, remove-vignette, etc.) | None — these are per-player and don't need to match across the group. |
| `seamless-randomizer` | Full rebuild of a randomizer co-op run: ERSC + me3 + a freshly generated `regulation.bin` | **Everyone** needs the byte-identical generated regulation.bin, the same seed, and matching mod versions, or worlds desync. |

Item spawners (Glorious Merchant, ErdTools) are deliberately in **no** profile. They're
the one mod category that can put illegitimate items into a save, which is the thing the
whole ban model is built to avoid. `erm doctor` warns if it finds one installed.

Run `python3 erm fetch <profile>` to pick a profile other than the default.

## Commands

| Command | What it does |
|---|---|
| `erm doctor` | Read-only safety report: EAC armed/disarmed, forbidden DLLs or spawner mods in `Game/`, launch-option sanity. Run before every play session and after any Steam update. |
| `erm fetch [profile]` | Downloads and sha256-verifies GitHub-sourced mods into `vendor/`, updates `mods.lock.toml`. Prints manual download steps for Nexus-sourced mods (scripted download would violate Nexus's ToS for free accounts, so `erm` never pretends to do it). |
| `erm apply [profile]` | Installs the locked mod set into `Game/`. Re-injects `COOP_PASSWORD` into `ersc_settings.ini` every time, since an ERSC update blanks it. Idempotent. |
| `erm verify` | Re-hashes everything in `vendor/` against `mods.lock.toml` and reports mismatches. Integrity check, not a ban-safety check. |
| `erm audit [save]` | Runs forensic checks on a save file for decisive tampering signatures (impossible stats, bad checksums, corrupt inventory handles). Always prints a caveat: it can rule out careless tampering, not certify a save clean. |
| `erm backup [--label X]` | Snapshots the current `.co2` save to `backups/`. Steam Cloud does not back up `.co2`, so this is the only backup path for co-op saves. |
| `erm restore <name>` | Restores a snapshot from `backups/`, taking a pre-restore backup of whatever's currently there first. |
| `erm quarantine` | Moves `ER0000.sl2` (the vanilla save) out of the Steam prefix into `backups/quarantine/` and prints the Steam Cloud steps needed to stop it re-syncing. Refuses to run while Steam is up. |
| `erm status` | One-screen summary: install state, cloud saves present, applied profile. |
| `erm launch-option` | Prints the exact Steam launch-option string, plus a validator variant that dumps the real argv so you can confirm the substitution worked before trusting it. |

## Before you ever launch vanilla online

**Never take an old or modded `ER0000.sl2` into a vanilla, EAC-on, online session.** This
repo already has two "Silverie" characters sitting in the Steam Cloud from an earlier
modded install (made with an item-spawner mod present). `erm audit` finds no decisive
tampering in them, but it can't certify them clean either — FromSoft doesn't publish
detection criteria, so a careful edit passes every check that's parseable from the save
format.

Run `python3 erm quarantine` before your first vanilla-online launch. It backs up
`ER0000.sl2`, moves it out of the Steam prefix, and walks you through purging the two
cloud copies so they don't silently re-sync back in.

**The game is installed now** — this isn't a someday problem, the ban window is open as
soon as you launch. Run `erm quarantine` before you touch vanilla online, and use
`seamless-only` for everything else.

## For Windows friends

The lockfile (`mods.lock.toml`) is the shared source of truth — it pins the exact ERSC
version everyone in the group needs to be running. A Windows friend doesn't need `erm` at
all: they read the version out of `mods.lock.toml`, download the matching ERSC release
themselves, and drop it in the normal way. The launch step differs (no Steam
launch-option trick needed on Windows — ERSC ships its own launcher exe), but the
lockstep rule is the same: everyone's ERSC version and, for the randomizer profile,
everyone's `regulation.bin` must match, or co-op won't connect or worlds will desync.

## Testing

`python3 -m pytest -q` runs the test suite (unit tests against a fixture copy of a real
save, recorded GitHub API responses — no live network in tests, no touching the real save
or game install).
