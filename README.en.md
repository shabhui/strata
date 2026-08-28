# Strata

[中文](README.md) · **English**

See **when** your C: and D: drives filled up, and **what** filled them.

Disk analyzers are everywhere, but they all answer "who is big right now". The question you actually have is usually a different one: **where did the 40 GB from the last three days come from?** Strata records every directory's size day by day, stacked up like geological strata, so what you scroll through is a timeline, not a file tree.

- Pure Python standard library, zero runtime dependencies
- Local web UI, binds `127.0.0.1` only, nothing leaves the machine
- Shows the last few days of growth on the day you install it (see the two history layers below)
- Reads the NTFS MFT directly, so a full-drive scan usually takes seconds; falls back to a normal walk when it can't get the privilege
- UI in English and Chinese. It follows your browser's language on first launch, and the button in the top bar (which shows the language you would switch *to*) overrides that and is remembered

## Two history layers

A freshly installed tool has no history, which is the standing weakness of this whole category of program. Strata works around it with two layers of data, and **never mixes the two in the UI**:

| | Retro layer `retro` | Measured layer `measured` |
|---|---|---|
| Source | Buckets files that exist now by creation date | Subtracts two consecutive snapshots |
| Available on day one | Yes | No, needs a second snapshot |
| Sees deletions | **No** | Yes |
| Means | Bytes written that day that are **still on disk now** | True net change |
| Style on the chart | Hatched fill | Solid fill |

The retro layer's blind spot is real, and it skews in both directions:

- **Skews low**: a 10 GB file downloaded last week and deleted yesterday does not exist as far as the retro layer is concerned.
- **Skews high**: when what got deleted that day was created earlier, the retro layer cannot see it at all, so it paints even a net-negative day as positive. Measured on this machine, one day: retro said `+8.35 GB`, measured said `-0.74 GB`, 9 GB apart.

Adding retro values across days is also not "how much it grew over this period". That sum equals "the part of the bytes now on disk whose creation date falls inside the window", which is an **upper bound** on true net growth — equal only if not a single byte was ever deleted. So the UI only reports retro as "how much of the last N days of writes is still on disk", and lets the measured layer be the only thing that states net change. Once you have two days of snapshots, the measured layer takes over.

With administrator rights it also reads the USN change journal, which feeds deletion events into the measured layer.

## Using it

**Handing it to someone else**: download `Strata.exe` from [Releases](../../releases) and double-click. It raises a UAC prompt (reading the MFT needs it), then opens your browser.

**From source** (Python 3.11+, Windows):

```bash
python -m strata
```

With no arguments that means `serve --admin`: elevate, start `http://127.0.0.1:8731`, open the browser.

Other subcommands:

```bash
python -m strata scan --drives C: D:    # scan once, no UI
python -m strata schedule on --at 12:30 # register the daily snapshot task
python -m strata doctor                 # checkup: privileges, drives, database, logs, task
```

The timeline can only grow if there is at least one snapshot per day, so leaving the scheduled task on is the recommendation. `schedule on` uses the Windows Task Scheduler, so there is no background process sitting around.

## The UI

- **Strata treemap**: box area is size, color is growth or shrinkage over the period. Click to drill in, right-click to open in File Explorer.
- **Daily change**: a timeline of each day's net change. `Ctrl + wheel` to zoom, drag to pan, `0` for the full view. When one day jumps by tens of GB and squashes every other day into a flat line, press `L` for a log axis (base 1 MB, so zero still draws).
- **Hotspots**: the fastest-growing directories, aggregated by owning layer, so you don't get `Users` nested inside `Users\alice` nested further down as duplicate rows.

## Where the data lives

One SQLite database: `%LOCALAPPDATA%\Strata\strata.db`. Logs in the same directory. Nothing is written to the program directory or the temp directory, so moving the exe elsewhere does not affect your history.

Deleting that directory is a reset. It does not touch the actual files on your disk — Strata only reads, it never deletes anything of yours.

## Development

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -q
python tools/build_exe.py        # package dist/Strata.exe
python tools/verify_exe.py       # launch the exe and exercise its API and routes
```

Design notes are in [docs/plan.md](docs/plan.md) (in Chinese), including why snapshots store directories rather than files, and the potholes hit while parsing the MFT.

## Known limits

- Windows NTFS only. The MFT and USN paths depend on NTFS structures; other filesystems fall back to a normal walk.
- The retro layer depends on file creation times. Some installers rewrite that timestamp, and the dates for that chunk of growth are then simply wrong.
- The retro layer records a file's size **now**, filed under its **creation** date. So a virtual disk or database file created last year that grew to 50 GB last week puts all 50 GB on that day last year — you will not find it in last week's bar. Logs, images, pack files, anything that "grows in place" pays this cost, and only the measured layer can see it.
- Not verified: the unit tests for the MFT / USN code paths use synthetic samples, and those paths have not been run across a range of real hardware.

## License

GPL-3.0. See [LICENSE](LICENSE).
