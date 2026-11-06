import pytest
from fastapi import HTTPException

from Backend.fastapi.routes.stream_routes import parse_range_header, part_math

MB = 1024 * 1024
SIZE = 10 * MB


def test_no_header_is_whole_file():
    assert parse_range_header("", SIZE) == (0, SIZE - 1)


@pytest.mark.parametrize("header,expected", [
    ("bytes=0-0", (0, 0)),
    ("bytes=100-", (100, SIZE - 1)),
    ("bytes=-500", (SIZE - 500, SIZE - 1)),
    ("bytes=5-999999999", (5, SIZE - 1)),
])
def test_parse_range(header, expected):
    assert parse_range_header(header, SIZE) == expected


@pytest.mark.parametrize("header", ["bytes=9-3", "bytes=abc-def", "bytes=1-2-3"])
def test_bad_range_is_416(header):
    with pytest.raises(HTTPException) as e:
        parse_range_header(header, SIZE)
    assert e.value.status_code == 416


@pytest.mark.parametrize("start,end,expected", [
    (0, 0, (0, 0, 1, 1)),                         # players probe with bytes=0-0
    (0, MB - 1, (0, 0, MB, 1)),                   # exactly one chunk
    (0, MB, (0, 0, 1, 2)),                        # first byte of 2nd chunk -> 2 chunks
    (10, 20, (0, 10, 21, 1)),                     # inside one chunk
    (MB - 5, MB + 5, (0, MB - 5, 6, 2)),          # straddles a boundary
    (MB + 7, 3 * MB - 1, (MB, 7, MB, 2)),         # ends on last byte of a chunk
    (0, 2 * MB, (0, 0, 1, 3)),                    # ends on first byte of 3rd chunk
    (SIZE - 1, SIZE - 1, (SIZE - MB, MB - 1, MB, 1)),  # last byte of file
])
def test_part_math(start, end, expected):
    # (offset, first_part_cut, last_part_cut, part_count)
    assert part_math(start, end, MB) == expected


def test_part_math_covers_requested_bytes():
    # chunks fetched must cover [start, end] exactly once
    for start, end in [(0, 0), (5, MB), (MB, 2 * MB), (3, 5 * MB + 9)]:
        offset, first_cut, last_cut, parts = part_math(start, end, MB)
        total = parts * MB - first_cut - (MB - last_cut)
        assert total == end - start + 1
