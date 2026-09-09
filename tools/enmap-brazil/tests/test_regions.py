from datetime import datetime, timezone

import pytest

from enmap_brazil.regions import (
    BRAZIL_BBOX,
    build_chunks,
    clamp_range,
    geometry_bounds,
    grid_tiles,
    month_windows,
    parse_bbox,
)


def test_named_region_and_explicit_bbox():
    assert parse_bbox("brazil") == BRAZIL_BBOX
    assert parse_bbox("-70,-10,-60,0") == (-70.0, -10.0, -60.0, 0.0)
    assert parse_bbox(" -70 , -10 , -60 , 0 ") == (-70.0, -10.0, -60.0, 0.0)


@pytest.mark.parametrize(
    "bad", ["-70,-10,-60", "a,b,c,d", "-60,-10,-70,0", "-200,-10,-60,0", "-70,-100,-60,0"]
)
def test_bad_bbox_rejected(bad):
    with pytest.raises(ValueError):
        parse_bbox(bad)


def test_brazil_bbox_covers_the_extremes():
    minx, miny, maxx, maxy = BRAZIL_BBOX
    # Serra do Divisor (AC), Chui (RS), Monte Caburai (RR), Martim Vaz islands.
    assert minx <= -73.98 and miny <= -33.75 and maxy >= 5.27 and maxx >= -28.85


def test_month_windows_are_half_open_and_snapped():
    windows = list(
        month_windows(datetime(2024, 1, 15, tzinfo=timezone.utc), datetime(2024, 4, 1, tzinfo=timezone.utc))
    )
    assert len(windows) == 3  # Jan from the 15th, then Feb and Mar; Apr 1 is the exclusive end
    assert windows[0][0] == datetime(2024, 1, 15, tzinfo=timezone.utc)
    for earlier, later in zip(windows, windows[1:]):
        assert earlier[1] == later[0], "windows must tile without gaps or overlap"


def test_month_windows_multi_month_step():
    windows = list(
        month_windows(datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, tzinfo=timezone.utc), months=3)
    )
    assert len(windows) == 4
    assert windows[-1][1] == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_month_windows_empty_when_reversed():
    assert list(month_windows(datetime(2024, 5, 1), datetime(2024, 1, 1))) == []


def test_datetime_range_does_not_double_count_the_boundary():
    chunks = build_chunks(
        (-70, -10, -60, 0),
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 3, 1, tzinfo=timezone.utc),
        window_months=1,
    )
    assert chunks[0].datetime_range == "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"
    assert chunks[1].datetime_range.startswith("2024-02-01T00:00:00Z/")


def test_grid_tiles_cover_the_bbox_without_gaps():
    tiles = grid_tiles((-10, -10, 0, 0), 4)
    assert len(tiles) == 9
    assert min(t[0] for t in tiles) == -10
    assert max(t[2] for t in tiles) == 0
    assert grid_tiles((-10, -10, 0, 0), None) == [(-10, -10, 0, 0)]


def test_chunk_keys_are_stable_and_unique():
    args = ((-70, -10, -60, 0), datetime(2023, 1, 1, tzinfo=timezone.utc), datetime(2023, 6, 1, tzinfo=timezone.utc))
    first = build_chunks(*args, window_months=1)
    second = build_chunks(*args, window_months=1)
    assert [c.key for c in first] == [c.key for c in second]
    assert len({c.key for c in first}) == len(first)


def test_clamp_range_trims_a_ten_year_ask_to_the_mission_lifetime():
    requested = (datetime(2015, 1, 1, tzinfo=timezone.utc), datetime(2025, 1, 1, tzinfo=timezone.utc))
    extent = (datetime(2022, 11, 2, tzinfo=timezone.utc), None)
    clamped = clamp_range(*requested, extent)
    assert clamped is not None
    assert clamped[0] == extent[0]
    assert clamped[1] == requested[1]


def test_clamp_range_returns_none_without_overlap():
    assert (
        clamp_range(
            datetime(2010, 1, 1, tzinfo=timezone.utc),
            datetime(2012, 1, 1, tzinfo=timezone.utc),
            (datetime(2022, 1, 1, tzinfo=timezone.utc), None),
        )
        is None
    )


def test_geometry_bounds_handles_polygons_and_collections():
    poly = {"type": "Polygon", "coordinates": [[[-5, -5], [5, -5], [5, 5], [-5, 5], [-5, -5]]]}
    assert geometry_bounds(poly) == (-5, -5, 5, 5)
    assert geometry_bounds(None) is None
    collection = {"type": "GeometryCollection", "geometries": [poly, {"type": "Point", "coordinates": [10, 10]}]}
    assert geometry_bounds(collection) == (-5, -5, 10, 10)
