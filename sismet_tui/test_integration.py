#!/usr/bin/env python3
"""End-to-end tests against in-process stand-ins for SISMET and a SOCKS5 proxy.

The real host answers only from Brazilian IP space, so these exercise the whole
path -- SOCKS5 handshake, HTTP, parsing, chunking, CSV, resume -- against
servers started inside the test process.  Nothing here touches the network
beyond loopback.

    python3 -m unittest test_integration -v
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import os
import select
import socket
import struct
import tempfile
import threading
import unittest

from sismet_client import DATASETS, DateParams, SismetClient, Station
from sismet_download import DownloadJob, run_download
from sismet_net import HttpClient, ProxyConfig, ProxyError, socks5_connect

ORIGIN = dt.date(2024, 1, 1)
STATIONS = [("1", "Guaxupe"), ("7", "Monte Carmelo"), ("13", "Sao Sebastiao do Paraiso")]


# --------------------------------------------------------------------------
# Stand-in SISMET
# --------------------------------------------------------------------------


def make_handler(mode: str, state: dict):
    """Build a request handler.

    ``mode`` picks which flavour of server we are pretending to be:
    ``html`` honours dtInicio/dtFim as dd/mm/YYYY, ``json`` does the same but
    answers with a DRF-style envelope, ``alt`` honours only
    dataInicial/dataFinal as ISO, and ``flaky`` fails a nominated window once.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def _send(self, body: str, ctype: str):
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            import urllib.parse

            parts = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parts.query)
            state.setdefault("requests", []).append(parts.query)

            if parts.path == "/dados/estacao/":
                options = "".join(f'<option value="{c}">{n}</option>' for c, n in STATIONS)
                self._send(
                    f"<html><body><select name='cdEstacao'>{options}</select></body></html>",
                    "text/html; charset=utf-8",
                )
                return

            if parts.path != "/dados/estacao/pesquisarDados/":
                self.send_error(404)
                return

            code = params.get("cdEstacao", ["?"])[0]
            last_day = dt.date.today()
            start, end = ORIGIN, last_day

            if mode == "alt":
                names, fmt = ("dataInicial", "dataFinal"), "%Y-%m-%d"
            else:
                names, fmt = ("dtInicio", "dtFim"), "%d/%m/%Y"
            if names[0] in params and names[1] in params:
                try:
                    start = dt.datetime.strptime(params[names[0]][0], fmt).date()
                    end = dt.datetime.strptime(params[names[1]][0], fmt).date()
                except ValueError:
                    pass  # wrong format: behave as if the filter were absent

            if mode == "flaky":
                window = params.get("dtInicio", [None])[0]
                if state.get("fail_always") == window:
                    self.send_error(503, "Service Unavailable")
                    return
                if state.get("fail_once") == window:
                    state["fail_once"] = None
                    self.send_error(503, "Service Unavailable")
                    return

            day, stop = max(start, ORIGIN), min(end, last_day)
            days = []
            while day <= stop:
                days.append(day)
                day += dt.timedelta(days=1)

            if mode == "json":
                records = [
                    {
                        "estacao": {"cd": int(code), "nome": "x"},
                        "data": d.strftime("%Y-%m-%d"),
                        "tempMedia": 18 + (d.toordinal() % 13) + 0.4,
                    }
                    for d in days
                ]
                self._send(
                    json.dumps({"count": len(records), "next": None, "results": records}),
                    "application/json",
                )
                return

            rows = "".join(
                f"<tr><td>{code}</td><td>{d:%d/%m/%Y}</td>"
                f"<td>{18 + (d.toordinal() % 13)}.4</td></tr>"
                for d in days
            )
            table = (
                "<table><tr><th>Estacao</th><th>Data</th><th>Temp</th></tr>" + rows + "</table>"
            )
            self._send(f"<html><body>{table}</body></html>", "text/html; charset=utf-8")

    return Handler


class StandInSismet:
    def __init__(self, mode: str = "html"):
        self.state: dict = {}
        self.server = http.server.HTTPServer(("127.0.0.1", 0), make_handler(mode, self.state))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


# --------------------------------------------------------------------------
# Stand-in SOCKS5 proxy
# --------------------------------------------------------------------------


class StandInSocks5:
    """A minimal RFC 1928 proxy, enough to prove our client handshake is right.

    Every CONNECT is relayed to one fixed loopback port, so a request naming
    the real SISMET hostname still lands on the stand-in -- which is exactly
    the georestriction-hopping shape the tool is built for.
    """

    def __init__(self, target_port: int, username: str = None, password: str = None):
        self.target_port = target_port
        self.username = username
        self.password = password
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        self.requested_hosts: list = []
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.running = False
        try:
            self.listener.close()
        except OSError:
            pass

    def _serve(self):
        while self.running:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        try:
            head = client.recv(2)
            if len(head) < 2 or head[0] != 0x05:
                return
            methods = client.recv(head[1])
            if self.username:
                if 0x02 not in methods:
                    client.sendall(b"\x05\xff")
                    return
                client.sendall(b"\x05\x02")
                client.recv(1)  # auth version
                ulen = client.recv(1)[0]
                user = client.recv(ulen).decode()
                plen = client.recv(1)[0]
                pwd = client.recv(plen).decode()
                ok = user == self.username and pwd == self.password
                client.sendall(bytes([0x01, 0x00 if ok else 0x01]))
                if not ok:
                    return
            else:
                client.sendall(b"\x05\x00")

            header = client.recv(4)
            if len(header) < 4 or header[1] != 0x01:
                client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                return
            atyp = header[3]
            if atyp == 0x03:
                length = client.recv(1)[0]
                host = client.recv(length).decode()
            elif atyp == 0x01:
                host = socket.inet_ntoa(client.recv(4))
            else:
                client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
                return
            struct.unpack("!H", client.recv(2))[0]
            self.requested_hosts.append(host)

            upstream = socket.create_connection(("127.0.0.1", self.target_port), timeout=10)
            client.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") +
                           struct.pack("!H", self.target_port))
            self._relay(client, upstream)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _relay(a: socket.socket, b: socket.socket):
        try:
            StandInSocks5._pump(a, b)
        finally:
            for sock in (a, b):
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket):
        socks = [a, b]
        while True:
            ready, _, errored = select.select(socks, [], socks, 10)
            if errored or not ready:
                break
            for sock in ready:
                other = b if sock is a else a
                try:
                    data = sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except OSError:
                    return


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


DIRECT = ProxyConfig(enabled=False)


def client_for(server: StandInSismet, proxy: ProxyConfig = DIRECT, log=None) -> SismetClient:
    return SismetClient(
        http=HttpClient(proxy=proxy, timeout=10, retries=2, backoff=0.1),
        base_url=server.base_url,
        log=log,
    )


class StationListingTests(unittest.TestCase):
    def test_stations_come_back_from_the_select(self):
        with StandInSismet() as server:
            stations = client_for(server).list_stations(DATASETS["estacao"])
        self.assertEqual([s.code for s in stations], ["1", "7", "13"])
        self.assertEqual(stations[0].name, "Guaxupe")


class ProbeTests(unittest.TestCase):
    def _probe(self, mode: str):
        end = dt.date.today() - dt.timedelta(days=1)
        with StandInSismet(mode) as server:
            return client_for(server).probe_date_parameters(
                DATASETS["estacao"], "7", end - dt.timedelta(days=6), end
            )

    def test_finds_the_default_spelling(self):
        params = self._probe("html")
        self.assertEqual((params.start_name, params.end_name, params.date_format),
                         ("dtInicio", "dtFim", "%d/%m/%Y"))

    def test_finds_a_different_spelling_and_format(self):
        params = self._probe("alt")
        self.assertEqual((params.start_name, params.end_name, params.date_format),
                         ("dataInicial", "dataFinal", "%Y-%m-%d"))

    def test_probe_works_against_a_json_endpoint(self):
        self.assertIsNotNone(self._probe("json"))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "out.csv")
        self.params = DateParams("dtInicio", "dtFim", "%d/%m/%Y")

    def _job(self, server, codes=("7",), start=None, end=None, days=10, **kw):
        return DownloadJob(
            dataset=DATASETS["estacao"],
            stations=[Station(c, "") for c in codes],
            start=start or dt.date(2024, 1, 1),
            end=end or dt.date(2024, 1, 31),
            output=self.out,
            window_days=days,
            date_params=self.params,
            pause_seconds=0,
            **kw,
        )

    def _rows(self):
        import csv
        with open(self.out, encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_every_day_arrives_exactly_once(self):
        with StandInSismet() as server:
            progress = run_download(client_for(server), self._job(server),
                                    lambda p: None, lambda m: None, threading.Event())
        rows = self._rows()
        self.assertEqual(progress.rows, 31)
        self.assertEqual(len(rows), 31)
        self.assertEqual(len({r["Data"] for r in rows}), 31)
        self.assertEqual(progress.failures, 0)

    def test_multiple_stations(self):
        with StandInSismet() as server:
            run_download(client_for(server), self._job(server, codes=("1", "7", "13")),
                         lambda p: None, lambda m: None, threading.Event())
        rows = self._rows()
        self.assertEqual(len(rows), 93)
        self.assertEqual({r["Estacao"] for r in rows}, {"1", "7", "13"})

    def test_cancellation_stops_early_and_keeps_what_it_had(self):
        cancel = threading.Event()

        def on_progress(progress):
            if progress.done >= 1:
                cancel.set()

        with StandInSismet() as server:
            progress = run_download(client_for(server), self._job(server),
                                    on_progress, lambda m: None, cancel)
        self.assertLess(progress.done, progress.total)
        self.assertGreater(len(self._rows()), 0)

    def test_resume_completes_without_duplicating(self):
        cancel = threading.Event()

        def stop_after_two(progress):
            if progress.done >= 2:
                cancel.set()

        with StandInSismet() as server:
            run_download(client_for(server), self._job(server),
                         stop_after_two, lambda m: None, cancel)
            partial = len(self._rows())
            self.assertTrue(os.path.exists(self.out + ".state.json"))

            run_download(client_for(server), self._job(server),
                         lambda p: None, lambda m: None, threading.Event())
        rows = self._rows()
        self.assertGreater(partial, 0)
        self.assertLess(partial, 31)
        self.assertEqual(len(rows), 31)
        self.assertEqual(len({r["Data"] for r in rows}), 31)
        self.assertFalse(os.path.exists(self.out + ".state.json"))

    def test_resume_across_a_json_round_trip_dedupes_numerics(self):
        """Rows reload from CSV as text; fresh JSON rows are floats."""
        with StandInSismet("json") as server:
            run_download(client_for(server), self._job(server),
                         lambda p: None, lambda m: None, threading.Event())
            first = len(self._rows())
            # Pretend only the opening window was bookmarked, forcing the rest
            # to be fetched again and compared against what is already there.
            with open(self.out + ".state.json", "w", encoding="utf-8") as handle:
                json.dump({"completed": ["7|2024-01-01|2024-01-10"]}, handle)
            run_download(client_for(server), self._job(server),
                         lambda p: None, lambda m: None, threading.Event())
        self.assertEqual(len(self._rows()), first)

    def test_a_transient_failure_is_retried_and_recovers(self):
        with StandInSismet("flaky") as server:
            server.state["fail_once"] = "11/01/2024"  # second window, fails once
            progress = run_download(client_for(server), self._job(server),
                                    lambda p: None, lambda m: None, threading.Event())
        self.assertEqual(progress.failures, 0)
        self.assertEqual(len(self._rows()), 31)  # the retry filled the gap

    def test_a_persistent_failure_is_counted_and_the_run_continues(self):
        with StandInSismet("flaky") as server:
            server.state["fail_always"] = "11/01/2024"  # second window, never works
            progress = run_download(client_for(server), self._job(server),
                                    lambda p: None, lambda m: None, threading.Event())
        self.assertEqual(progress.failures, 1)
        self.assertEqual(progress.done, progress.total)   # the run carried on
        self.assertEqual(len(self._rows()), 21)           # 31 days minus the lost window
        # A run with a failure keeps its bookmark so the gap can be retried later.
        self.assertTrue(os.path.exists(self.out + ".state.json"))

    def test_retrying_a_failed_run_fills_the_gap(self):
        with StandInSismet("flaky") as server:
            server.state["fail_always"] = "11/01/2024"
            run_download(client_for(server), self._job(server),
                         lambda p: None, lambda m: None, threading.Event())
            self.assertEqual(len(self._rows()), 21)
            server.state["fail_always"] = None  # the outage clears
            progress = run_download(client_for(server), self._job(server),
                                    lambda p: None, lambda m: None, threading.Event())
        self.assertEqual(progress.failures, 0)
        self.assertEqual(len(self._rows()), 31)
        self.assertEqual(len({r["Data"] for r in self._rows()}), 31)
        self.assertFalse(os.path.exists(self.out + ".state.json"))

    def test_json_endpoint_flattens_nested_objects(self):
        with StandInSismet("json") as server:
            run_download(client_for(server), self._job(server),
                         lambda p: None, lambda m: None, threading.Event())
        rows = self._rows()
        self.assertIn("estacao.cd", rows[0])
        self.assertEqual(rows[0]["estacao.cd"], "7")


class Socks5Tests(unittest.TestCase):
    def test_requests_reach_the_server_through_the_proxy(self):
        with StandInSismet() as server:
            with StandInSocks5(server.port) as proxy:
                config = ProxyConfig(host="127.0.0.1", port=proxy.port)
                stations = client_for(server, config).list_stations(DATASETS["estacao"])
                self.assertEqual(len(stations), 3)
                self.assertGreaterEqual(proxy.connections, 1)

    def test_the_hostname_is_resolved_at_the_proxy(self):
        """socks5h behaviour: we must send the name, not a resolved address."""
        with StandInSismet() as server:
            with StandInSocks5(server.port) as proxy:
                config = ProxyConfig(host="127.0.0.1", port=proxy.port)
                http = HttpClient(proxy=config, timeout=10, retries=1)
                client = SismetClient(http=http, base_url="http://sismet.cooxupe.com.br:9000")
                client.list_stations(DATASETS["estacao"])
        self.assertEqual(proxy.requested_hosts, ["sismet.cooxupe.com.br"])

    def test_a_full_download_runs_through_the_proxy(self):
        directory = tempfile.mkdtemp()
        out = os.path.join(directory, "proxied.csv")
        with StandInSismet() as server:
            with StandInSocks5(server.port) as proxy:
                config = ProxyConfig(host="127.0.0.1", port=proxy.port)
                job = DownloadJob(
                    dataset=DATASETS["estacao"],
                    stations=[Station("7", "")],
                    start=dt.date(2024, 1, 1),
                    end=dt.date(2024, 1, 20),
                    output=out,
                    window_days=10,
                    date_params=DateParams("dtInicio", "dtFim", "%d/%m/%Y"),
                    pause_seconds=0,
                )
                result = run_download(client_for(server, config), job,
                                      lambda p: None, lambda m: None, threading.Event())
        self.assertEqual(result.rows, 20)
        self.assertGreaterEqual(proxy.connections, 2)

    def test_username_password_authentication(self):
        with StandInSismet() as server:
            with StandInSocks5(server.port, username="cafe", password="s3cr3t") as proxy:
                config = ProxyConfig(host="127.0.0.1", port=proxy.port,
                                     username="cafe", password="s3cr3t")
                self.assertEqual(len(client_for(server, config).list_stations(
                    DATASETS["estacao"])), 3)

    def test_wrong_password_is_reported_clearly(self):
        with StandInSismet() as server:
            with StandInSocks5(server.port, username="cafe", password="s3cr3t") as proxy:
                config = ProxyConfig(host="127.0.0.1", port=proxy.port,
                                     username="cafe", password="wrong")
                with self.assertRaises(ProxyError) as caught:
                    socks5_connect(config, "sismet.cooxupe.com.br", 9000, timeout=5)
        self.assertIn("username/password", str(caught.exception))

    def test_a_missing_tunnel_fails_fast_with_advice(self):
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        dead_port = spare.getsockname()[1]
        spare.close()

        config = ProxyConfig(host="127.0.0.1", port=dead_port)
        with self.assertRaises(ProxyError) as caught:
            socks5_connect(config, "sismet.cooxupe.com.br", 9000, timeout=3)
        message = str(caught.exception)
        self.assertIn("cannot reach SOCKS5 proxy", message)
        self.assertIn("Is the tunnel running?", message)

    def test_a_dead_tunnel_is_not_retried_by_the_http_client(self):
        """A missing tunnel will not fix itself, so it must not sit through backoff."""
        spare = socket.socket()
        spare.bind(("127.0.0.1", 0))
        dead_port = spare.getsockname()[1]
        spare.close()

        http = HttpClient(proxy=ProxyConfig(host="127.0.0.1", port=dead_port),
                          timeout=3, retries=3, backoff=5)
        started = dt.datetime.now()
        with self.assertRaises(ProxyError):
            http.get("https://sismet.cooxupe.com.br:9000/")
        self.assertLess((dt.datetime.now() - started).total_seconds(), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
