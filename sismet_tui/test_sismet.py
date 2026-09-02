#!/usr/bin/env python3
"""Offline tests: python3 -m unittest discover -s sismet_tui -v

SISMET answers only from Brazilian IP space, so none of this talks to the real
host.  What is exercised here is everything that does not need the network:
the response parsers (the exact thing that has to cope with a payload shape we
could not confirm in advance), window arithmetic, and the CSV sink.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import tempfile
import unittest

from sismet_client import (
    CsvSink,
    DateParams,
    Station,
    _rows_within_range,
    extract_date,
    find_timestamp_key,
    iter_windows,
    parse_options,
    parse_records,
)
from sismet_net import ProxyConfig, Response


def make_response(body: str, content_type: str = "text/html; charset=utf-8") -> Response:
    return Response(
        status=200,
        reason="OK",
        headers={"content-type": content_type},
        body=body.encode("utf-8"),
        url="http://example.invalid/dados/estacao/pesquisarDados/",
    )


class ParseJsonTests(unittest.TestCase):
    def test_bare_list(self):
        payload = json.dumps([{"data": "01/02/2024", "temp": 21.5}])
        rows = parse_records(make_response(payload, "application/json"))
        self.assertEqual(rows, [{"data": "01/02/2024", "temp": 21.5}])

    def test_drf_pagination_envelope(self):
        payload = json.dumps(
            {"count": 2, "next": None, "results": [{"a": 1}, {"a": 2}]}
        )
        rows = parse_records(make_response(payload, "application/json"))
        self.assertEqual(rows, [{"a": 1}, {"a": 2}])

    def test_portuguese_envelope(self):
        payload = json.dumps({"dados": [{"chuva": 3}], "status": "ok"})
        self.assertEqual(parse_records(make_response(payload, "application/json")),
                         [{"chuva": 3}])

    def test_nested_objects_are_flattened(self):
        payload = json.dumps([{"estacao": {"cd": 7, "nome": "Guaxupe"}, "temp": 20}])
        rows = parse_records(make_response(payload, "application/json"))
        self.assertEqual(rows, [{"estacao.cd": 7, "estacao.nome": "Guaxupe", "temp": 20}])

    def test_lists_become_json_text(self):
        payload = json.dumps([{"leituras": [1, 2, 3]}])
        rows = parse_records(make_response(payload, "application/json"))
        self.assertEqual(rows[0]["leituras"], "[1, 2, 3]")

    def test_single_record_object(self):
        payload = json.dumps({"data": "2024-01-01", "temp": 19})
        self.assertEqual(len(parse_records(make_response(payload, "application/json"))), 1)


class ParseHtmlTests(unittest.TestCase):
    HTML = """
    <html><body>
      <table class="nav"><tr><td>ignore me</td></tr></table>
      <table>
        <tr><th>Estacao</th><th>Data</th><th>Temp</th></tr>
        <tr><td>7</td><td>01/01/2024</td><td>21,4</td></tr>
        <tr><td>7</td><td>02/01/2024</td><td>22,0</td></tr>
      </table>
    </body></html>
    """

    def test_largest_table_wins(self):
        rows = parse_records(make_response(self.HTML))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Data"], "01/01/2024")
        self.assertEqual(rows[1]["Temp"], "22,0")

    def test_entities_and_whitespace(self):
        html = (
            "<table><tr><th>Munic&iacute;pio</th></tr>"
            "<tr><td>  S&atilde;o   Sebasti&atilde;o  </td></tr></table>"
        )
        rows = parse_records(make_response(html))
        self.assertEqual(rows[0]["Município"], "São Sebastião")

    def test_duplicate_headers_are_disambiguated(self):
        html = "<table><tr><th>x</th><th>x</th></tr><tr><td>1</td><td>2</td></tr></table>"
        rows = parse_records(make_response(html))
        self.assertEqual(rows[0], {"x": "1", "x_1": "2"})

    def test_page_without_table(self):
        self.assertEqual(parse_records(make_response("<html><body>nada</body></html>")), [])

    def test_station_options(self):
        html = "<select><option value=''>--</option><option value='7'>Guaxupe</option></select>"
        self.assertEqual(parse_options(make_response(html)), [("7", "Guaxupe")])


class ParseCsvTests(unittest.TestCase):
    def test_semicolon_delimited(self):
        body = "Estacao;Data;Temp\n7;01/01/2024;21,4\n"
        rows = parse_records(make_response(body, "text/csv"))
        self.assertEqual(rows[0]["Temp"], "21,4")

    def test_comma_delimited(self):
        body = "a,b\n1,2\n"
        self.assertEqual(parse_records(make_response(body, "text/csv")), [{"a": "1", "b": "2"}])


class EncodingTests(unittest.TestCase):
    def test_latin1_body_without_declared_charset(self):
        response = Response(200, "OK", {"content-type": "text/html"},
                            "<table><tr><th>Esta\xe7\xe3o</th></tr>"
                            "<tr><td>ok</td></tr></table>".encode("latin-1"),
                            "http://example.invalid/")
        rows = parse_records(response)
        self.assertEqual(rows[0]["Estação"], "ok")


class WindowTests(unittest.TestCase):
    def test_exact_division(self):
        windows = list(iter_windows(dt.date(2024, 1, 1), dt.date(2024, 1, 10), 5))
        self.assertEqual(windows, [
            (dt.date(2024, 1, 1), dt.date(2024, 1, 5)),
            (dt.date(2024, 1, 6), dt.date(2024, 1, 10)),
        ])

    def test_ragged_tail_is_clamped_to_end(self):
        windows = list(iter_windows(dt.date(2024, 1, 1), dt.date(2024, 1, 7), 5))
        self.assertEqual(windows[-1], (dt.date(2024, 1, 6), dt.date(2024, 1, 7)))

    def test_windows_are_contiguous_and_cover_every_day(self):
        start, end = dt.date(2023, 3, 1), dt.date(2024, 6, 15)
        days = set()
        previous_end = None
        for win_start, win_end in iter_windows(start, end, 31):
            if previous_end is not None:
                self.assertEqual(win_start - previous_end, dt.timedelta(days=1))
            previous_end = win_end
            day = win_start
            while day <= win_end:
                days.add(day)
                day += dt.timedelta(days=1)
        self.assertEqual(len(days), (end - start).days + 1)

    def test_single_day_range(self):
        self.assertEqual(list(iter_windows(dt.date(2024, 5, 5), dt.date(2024, 5, 5), 31)),
                         [(dt.date(2024, 5, 5), dt.date(2024, 5, 5))])

    def test_reversed_range_rejected(self):
        with self.assertRaises(ValueError):
            list(iter_windows(dt.date(2024, 5, 5), dt.date(2024, 1, 1), 31))

    def test_zero_window_rejected(self):
        with self.assertRaises(ValueError):
            list(iter_windows(dt.date(2024, 1, 1), dt.date(2024, 5, 5), 0))


class CsvSinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "out.csv")

    def read(self):
        with open(self.path, encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_writes_header_and_rows(self):
        with CsvSink(self.path) as sink:
            sink.write([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
        rows = self.read()
        self.assertEqual([r["a"] for r in rows], ["1", "3"])

    def test_late_fields_go_to_extra_json_rather_than_being_dropped(self):
        with CsvSink(self.path) as sink:
            sink.write([{"a": 1}])
            sink.write([{"a": 2, "surprise": "novo"}])
        rows = self.read()
        self.assertEqual(json.loads(rows[1]["extra_json"]), {"surprise": "novo"})

    def test_duplicates_are_skipped(self):
        with CsvSink(self.path, dedupe=True) as sink:
            sink.write([{"a": 1}, {"a": 1}, {"a": 2}])
            self.assertEqual(sink.duplicates_skipped, 1)
        self.assertEqual(len(self.read()), 2)

    def test_dedupe_can_be_disabled(self):
        with CsvSink(self.path, dedupe=False) as sink:
            sink.write([{"a": 1}, {"a": 1}])
        self.assertEqual(len(self.read()), 2)

    def test_append_reloads_existing_rows_for_dedupe(self):
        with CsvSink(self.path) as sink:
            sink.write([{"a": 1}])
        with CsvSink(self.path, append=True, dedupe=True) as sink:
            sink.write([{"a": 1}, {"a": 2}])
            self.assertEqual(sink.duplicates_skipped, 1)
        rows = self.read()
        self.assertEqual([r["a"] for r in rows], ["1", "2"])

    def test_no_file_created_when_no_rows(self):
        with CsvSink(self.path) as sink:
            sink.write([])
        self.assertFalse(os.path.exists(self.path))


class DateHeuristicTests(unittest.TestCase):
    def test_finds_timestamp_column(self):
        self.assertEqual(find_timestamp_key({"cd": 1, "dataLeitura": "x"}), "dataLeitura")
        self.assertEqual(find_timestamp_key({"cd": 1, "DATA": "x"}), "DATA")
        self.assertIsNone(find_timestamp_key({"cd": 1, "temp": 2}))

    def test_extracts_both_date_orders(self):
        self.assertEqual(extract_date("01/02/2024"), dt.date(2024, 2, 1))
        self.assertEqual(extract_date("2024-02-01"), dt.date(2024, 2, 1))
        self.assertEqual(extract_date("2024-02-01 13:45:00"), dt.date(2024, 2, 1))
        self.assertIsNone(extract_date("sem data"))

    def test_range_check_rejects_out_of_range_rows(self):
        start, end = dt.date(2024, 1, 1), dt.date(2024, 1, 31)
        in_range = [{"data": "05/01/2024"}, {"data": "20/01/2024"}]
        out_of_range = [{"data": "05/01/2024"}, {"data": "20/06/2023"}]
        self.assertTrue(_rows_within_range(in_range, start, end))
        self.assertFalse(_rows_within_range(out_of_range, start, end))

    def test_range_check_is_permissive_without_a_date_column(self):
        self.assertTrue(_rows_within_range([{"temp": 1}], dt.date(2024, 1, 1),
                                           dt.date(2024, 1, 31)))


class ConfigTests(unittest.TestCase):
    def test_date_params_render_both_formats(self):
        params = DateParams("dtInicio", "dtFim", "%d/%m/%Y")
        self.assertEqual(params.build(dt.date(2024, 1, 2), dt.date(2024, 3, 4)),
                         {"dtInicio": "02/01/2024", "dtFim": "04/03/2024"})
        iso = DateParams("start", "end", "%Y-%m-%d")
        self.assertEqual(iso.build(dt.date(2024, 1, 2), dt.date(2024, 3, 4)),
                         {"start": "2024-01-02", "end": "2024-03-04"})

    def test_proxy_description(self):
        self.assertEqual(ProxyConfig().describe(), "socks5h://127.0.0.1:1080")
        self.assertEqual(ProxyConfig(enabled=False).describe(), "direct (no proxy)")

    def test_station_display(self):
        self.assertIn("Guaxupe", str(Station("7", "Guaxupe")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
