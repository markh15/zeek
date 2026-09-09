# enmap-brazil

A terminal UI that downloads the **EnMAP** hyperspectral scene catalogue for all
of Brazil and stores it in a single, queryable SQLite database.

EnMAP is DLR's *Environmental Mapping and Analysis Program* — a hyperspectral
(224-band, 30 m) satellite whose archive is published through the
[DLR EOC Geoservice](https://geoservice.dlr.de) STAC API.

> **On "the past 10 years":** EnMAP launched in April 2022 and began routine
> acquisitions later that year, so roughly three years of data exist, not ten.
> The tool asks for ten years by default and **clamps the request to each
> collection's advertised temporal extent**, so you get everything that is
> actually available without hand-editing dates or hammering the API with
> windows that cannot contain data.

## Install

```bash
cd tools/enmap-brazil
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # add '.[parquet]' for Parquet export
```

Requires Python 3.10+. Dependencies: `httpx` and `textual`.

## Use

```bash
enmap-br                    # launch the TUI (default)
```

| Key | Action |
| --- | --- |
| `d` | Discover the catalogue's EnMAP collections and their real date ranges |
| `s` | Start the harvest with the settings in the left-hand panel |
| `x` | Stop after the current page (progress is saved; `s` resumes) |
| `r` | Refresh statistics |
| `e` | Export every scene to CSV next to the database |
| `q` | Quit |

The **Harvest** tab holds the settings and a live log; **Collections** lists what
the catalogue offers; **Browse** runs read-only SQL (with canned queries in the
dropdown) against what you have harvested; **Downloads** manages optional
product files.

Everything is also available headlessly, which is what you want for a first
full-Brazil sweep in a `tmux` window or a cron job:

```bash
enmap-br collections                            # what exists, and for what dates
enmap-br harvest                                # all of Brazil, 10y (auto-clamped)
enmap-br harvest --bbox amazonia-legal --window-months 1
enmap-br harvest --start 2023-01-01 --end 2024-01-01 -v
enmap-br stats
enmap-br query by-year
enmap-br query "SELECT item_id, datetime FROM scenes WHERE cloud_cover < 5"
enmap-br export scenes.geojson
enmap-br export scenes.parquet                  # needs the [parquet] extra
```

Named regions for `--bbox`: `brazil` (default, includes the oceanic islands),
`brazil-mainland`, `amazonia-legal`, `cerrado`, `caatinga`, `mata-atlantica`,
`pantanal`, `pampa`. Or pass `minx,miny,maxx,maxy` directly.

## The stored format

One SQLite file (default `~/.local/share/enmap-brazil/enmap_brazil.sqlite`),
readable by the `sqlite3` shell, pandas, DuckDB, R, and QGIS without this
package installed.

| Table / view | What's in it |
| --- | --- |
| `scenes` | One row per STAC item: id, timestamps, bbox, centroid, cloud cover, sun angles, platform, processing level, GSD — **plus** the untouched `geometry`, `assets`, `links` and `properties` JSON, so nothing the API returned is lost |
| `scenes_bbox` | R\*Tree index over the footprints, kept in sync by triggers |
| `collections` | Catalogue metadata, including each collection's true temporal extent |
| `harvest_chunks` | Per-chunk progress — this is what makes a run resumable |
| `harvest_runs` | Provenance: when, what scope, how many items |
| `assets` | Optional product-download queue with status and sha256 |
| `v_scenes`, `v_scenes_by_month`, `v_scenes_by_year` | Flattened / pre-aggregated views |

Typical queries:

```sql
-- Clear-sky scenes over the Amazon, most recent first
SELECT item_id, datetime, cloud_cover
FROM scenes
WHERE centroid_lon BETWEEN -74 AND -44
  AND centroid_lat BETWEEN -18 AND 5
  AND cloud_cover <= 10
ORDER BY datetime DESC;

-- Fast spatial filter via the R*Tree
SELECT s.item_id, s.datetime
FROM scenes s JOIN scenes_bbox r ON r.id = s.scene_key
WHERE r.maxx >= -47.0 AND r.minx <= -46.0
  AND r.maxy >= -16.0 AND r.miny <= -15.0;

-- How coverage builds up year by year
SELECT * FROM v_scenes_by_year;
```

Re-running a harvest is safe and cheap: scenes are upserted on
`(collection, item_id)`, so nothing duplicates, and chunks already marked `done`
are skipped entirely. `enmap-br reset-chunks` forces a full refetch.

## How the harvest works

Work is split into `(spatial tile × time window)` chunks — three months per
chunk by default, one tile unless you pass `--grid-deg`. Each chunk is recorded
as `pending → running → done` before and after it runs, so an interrupted sweep
resumes at the first incomplete chunk rather than starting over. Chunking also
keeps every individual search well under server result caps.

The STAC client probes `POST /search`, then `GET /search`, then
`GET /collections/{id}/items`, and sticks with whichever the server supports.
Collections are matched by substring rather than a hard-coded id, so a renamed
or newly added EnMAP product level is picked up automatically. Transient 429 and
5xx responses are retried with exponential backoff honouring `Retry-After`, and
requests are rate limited (`--rate-limit`, default 0.2 s apart).

## Downloading the products themselves

The catalogue is metadata. Fetching the actual hyperspectral products is
opt-in — they are large, and DLR requires an account:

```bash
export ENMAP_BR_USER=... ENMAP_BR_PASSWORD=...
enmap-br harvest --queue-assets     # queue 'data'-role assets while harvesting
enmap-br download --limit 5         # fetch a few, resumable
```

Downloads stream to a `.part` file, resume with HTTP `Range`, are verified with
sha256, and are only renamed into place once complete.

## Tests

```bash
pip install -e '.[dev]'
python -m pytest
```

The suite runs an in-process fake STAC server, so it needs no network: it covers
pagination, the endpoint fallback, retry/backoff, date clamping, resume, chunk
failure isolation, the R\*Tree triggers, download resume, the CLI, and the TUI
itself under Textual's headless pilot.
