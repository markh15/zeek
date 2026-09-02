# SISMET historic export

A terminal UI for pulling **historic** weather data out of
[SISMET](https://sismet.cooxupe.com.br:9000/), the meteorological system of
**Cooxupé** (Cooperativa Regional de Cafeicultores em Guaxupé), and writing it
to a CSV file.

Cooxupé runs a network of automatic weather stations and rain gauges across the
coffee belt of Minas Gerais and São Paulo; some stations have been recording
since 2010.

```
$ ./sismet_historic_tui.py
```

```
 SISMET historic export - Cooxupe weather stations
> Dataset        : Estacoes meteorologicas (weather stations)
  Station(s)     : 2 selected: 1 Guaxupe, 7 Monte Carmelo
  Start date     : 2024-01-01
  End date       : 2024-12-31
  Window (days)  : 31
  Output CSV     : ./sismet_historic.csv
  SOCKS5 proxy   : socks5h://127.0.0.1:1080
  Verify TLS     : yes
  Date params    : dtInicio/dtFim as %d/%m/%Y
  Resume/dedupe  : resume on, dedupe on

[##############################--------------------] 61.2%  22/36 windows  671 rows

 UP/DN move  ENTER edit  F2 stations  F3 probe  F4 test proxy  F5 start  ESC cancel  ^S save  q quit
```

## Requirements

Python 3.9 or newer. **No third-party packages** — SOCKS5 is implemented
directly against the standard library, so there is nothing to `pip install`.

## Brazil-only access

SISMET answers requests from Brazilian IP space only. Every request is
therefore sent through a **SOCKS5 proxy**, `socks5h://127.0.0.1:1080` by
default. The `h` matters: the hostname is resolved *at the proxy*, so DNS does
not leak past the tunnel and the name resolves from the exit node's point of
view.

Point it somewhere else with `--proxy host:port`, or turn it off with
`--no-proxy` if you are already in Brazil. `F4` inside the TUI tests the
tunnel on its own and reports precisely what failed.

A typical tunnel:

```bash
ssh -D 1080 -N user@your-brazilian-host
```

## Keys

| Key | Action |
|-----|--------|
| `↑` `↓` | move between fields |
| `ENTER` | edit the selected field (toggles the boolean ones) |
| `F2` | load the station list from the server |
| `F3` | probe which date parameters the server accepts |
| `F4` | test the SOCKS5 tunnel |
| `F5` | start the download |
| `ESC` | cancel the running download (or a field edit) |
| `^S` | save settings to `~/.config/sismet-tui/config.json` |
| `q` | quit |

In the station picker: `SPACE` toggles, `/` filters, `a` selects all, `n`
selects none, `ENTER` confirms.

## Batch use

The same program runs without the TUI, for cron jobs and scripts:

```bash
# What stations exist?
./sismet_historic_tui.py --list-stations

# Which date parameters does the server want? (see "Date parameters" below)
./sismet_historic_tui.py --stations 7 --probe

# Pull a decade for three stations
./sismet_historic_tui.py --batch \
    --stations 1,7,13 \
    --start 2015-01-01 --end 2024-12-31 \
    --window-days 31 \
    --date-params 'dtInicio,dtFim,%d/%m/%Y' \
    -o cooxupe_2015_2024.csv
```

Exit status is non-zero if any window failed.

## Endpoints

| Path (on port 9000) | What it serves |
|---|---|
| `/dados/estacao/` | weather station index |
| `/dados/estacao/pesquisarDados/?cdEstacao=N` | historic station data |
| `/dados/pluviometros/` | rain gauge index |
| `/dados/pluviometros/pesquisarDados/?cdPto=N` | historic rain gauge data |
| `/estacaoEmTempoReal/`, `/estacaoOnline/` | real-time readings |

The identifier parameters — `cdEstacao` for stations, `cdPto` for rain gauges —
are confirmed.

### Date parameters

The names SISMET uses for the date range are not documented anywhere, and the
host cannot be reached from outside Brazil, so they could not be confirmed
while this was written. Rather than shipping a guess, the tool **discovers them
at runtime**:

`F3` (or `--probe`) fetches the endpoint unfiltered to get a baseline, then
tries each candidate in `DATE_PARAM_CANDIDATES` (`dtInicio/dtFim`,
`dataInicial/dataFinal`, …) against each of `DATE_FORMAT_CANDIDATES`
(`%d/%m/%Y`, `%Y-%m-%d`, …). A candidate is accepted only when it returns rows
that *differ from the baseline* and fall inside the requested range — a
parameter the server silently ignores returns the baseline unchanged and is
rejected.

Probe once, then keep the answer: `^S` saves it, or pass `--date-params
start,end,format` on later runs. If none of the candidates work, add the right
one to `DATE_PARAM_CANDIDATES` in `sismet_client.py` — it is a plain list at
the top of the file — or type it into the *Date params* row by hand.

## How the download behaves

- **Chunked.** A long range is split into windows (31 days by default) so no
  single request asks for a decade at once. Windows are contiguous and cover
  every day exactly once.
- **Resumable.** Completed windows are bookmarked in `<output>.state.json`.
  Interrupt the run and start it again — it picks up where it stopped and
  appends. The bookmark is removed after a clean run.
- **De-duplicated.** Identical rows are written once, including across a
  resume, where rows already in the CSV are read back and compared as text.
- **Polite.** Requests are spaced by 0.4 s, and failures back off
  exponentially (2 s, 4 s, 8 s) before being retried.
- **Format-agnostic.** The response is parsed as JSON (bare list, wrapped in
  `results`/`dados`/`data`, or DRF-paginated), as the largest HTML `<table>` on
  the page, or as raw CSV — whichever it turns out to be. Nested JSON objects
  are flattened (`estacao.nome`), and any field that only shows up in a later
  window lands in a trailing `extra_json` column rather than being dropped.

A failed window is logged and the run continues; the exit status and the
summary line report how many failed.

## Files

| File | Contents |
|---|---|
| `sismet_historic_tui.py` | the TUI, the CLI, and settings |
| `sismet_client.py` | endpoints, response parsing, windows, CSV sink |
| `sismet_download.py` | chunked download loop with resume |
| `sismet_net.py` | SOCKS5 + HTTP, standard library only |
| `test_sismet.py` | unit tests: parsers, windows, CSV sink |
| `test_integration.py` | end-to-end tests against in-process stand-ins |
| `test_tui.py` | drives the TUI in a pty |

## Tests

```bash
cd sismet_tui
python3 -m unittest discover -s . -t .          # 57 tests
```

Nothing here reaches beyond loopback — the real host is unreachable from
outside Brazil, so the whole path is exercised against servers started inside
the test process.

| Module | What it covers |
|---|---|
| `test_sismet.py` (33) | response parsing in every format the endpoint might return, window arithmetic, the CSV sink including the resume round-trip, date heuristics |
| `test_integration.py` (20) | station listing, the parameter probe (against servers honouring different spellings), chunked download, cancellation, resume, transient vs. persistent failures, and the SOCKS5 client against a real in-process SOCKS5 proxy — handshake, username/password auth, `socks5h` hostname-at-proxy, a full download through the tunnel, and fast-fail on a dead one |
| `test_tui.py` (4) | the interface itself, driven by real keystrokes in a pty: startup and layout, the full flow from `F2` to a written CSV, refusing to start without date parameters, and reporting a station list it cannot load |

`test_tui.py` needs [`pyte`](https://pypi.org/project/pyte/) to read the
terminal (`pip install pyte`); it skips itself if that is missing. Nothing else
needs anything beyond the standard library, and **the tool itself needs
nothing at all**.

### What the tests caught

- Rows reloaded during a resume are text while freshly parsed JSON rows are
  numbers, so `1` and `"1"` have to hash alike — otherwise a resumed run
  appends every row a second time.
- A curses write filling the last cell of the bottom line raises, which
  crashed the very first render.
