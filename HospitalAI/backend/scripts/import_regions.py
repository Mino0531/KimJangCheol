#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["openpyxl>=3.1,<4", "httpx2[http2,brotli,zstd]>=2,<3"]
# ///
# Usage: uv run --script scripts/import_regions.py --download data/region_coordinates.json
# Or replace --download with a path to a previously downloaded official ZIP.
# Source: https://www.data.go.kr/data/15084084/openapi.do, KMA workbook dated 2026-09-30.
# The source supplies reference coordinates, not the caller's precise location.

"""Import KMA's administrative locality reference points from the official ZIP."""

from __future__ import annotations

import json
import math
import socket
import sys
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Final
from zipfile import ZipFile

import httpx2
from openpyxl import load_workbook

SOURCE_URL: Final = "https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_000000007732424&fileDetailSn=1"
HEADERS: Final = ("구분", "행정구역코드", "1단계", "2단계", "3단계", "격자 X", "격자 Y",
                  "경도(시)", "경도(분)", "경도(초)", "위도(시)", "위도(분)", "위도(초)",
                  "경도(초/100)", "위도(초/100)")


class SourceFormatError(Exception):
    """The downloaded workbook does not match the expected source format."""


@dataclass(frozen=True, slots=True)
class Region:
    code: str
    province: str
    district: str
    locality: str
    latitude: float
    longitude: float


def read_regions(source: Path | BytesIO) -> list[Region]:
    """Read the sole workbook and validate every Korean locality before export."""
    with ZipFile(source) as archive:
        names = [item for item in archive.infolist() if item.filename.lower().endswith(".xlsx")]
        if len(names) != 1 or names[0].file_size > 20_000_000:
            raise SourceFormatError("Expected one bounded XLSX workbook in the archive")
        workbook_bytes = archive.read(names[0])
    workbook = load_workbook(BytesIO(workbook_bytes), read_only=True, data_only=True)
    try:
        rows = workbook.worksheets[0].iter_rows(values_only=True)
        header = next(rows, ())
        if tuple(header[:len(HEADERS)]) != HEADERS:
            raise SourceFormatError("Workbook columns differ from the expected KMA format")
        regions: list[Region] = []
        codes: set[str] = set()
        for row in rows:
            if row[0] != "kor" or not row[2] or not row[3] or not row[4]:
                continue
            code = str(row[1])
            if len(code) != 10 or not code.isascii() or not code.isdigit() or code in codes:
                raise SourceFormatError(f"Invalid or duplicate locality code: {code}")
            try:
                latitude, longitude = float(row[14]), float(row[13])
            except (TypeError, ValueError) as exc:
                raise SourceFormatError(f"Missing locality coordinates: {code}") from exc
            if not (math.isfinite(latitude) and math.isfinite(longitude)
                    and 33 <= latitude <= 39 and 124 <= longitude <= 132):
                raise SourceFormatError(f"Locality coordinates outside expected bounds: {code}")
            codes.add(code)
            regions.append(Region(code=code, province=str(row[2]).strip(), district=str(row[3]).strip(),
                                  locality=str(row[4]).strip(), latitude=round(latitude, 7),
                                  longitude=round(longitude, 7)))
        if not regions:
            raise SourceFormatError("Workbook contains no Korean localities")
        return sorted(regions, key=lambda region: region.code)
    finally:
        workbook.close()


def download_source() -> BytesIO:
    """Download only the published workbook attachment with a bounded response."""
    limits = httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30.0)
    transport = httpx2.HTTPTransport(http2=True, retries=3, limits=limits,
                                    socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
    timeout = httpx2.Timeout(connect=5, read=30, write=10, pool=10)
    data = bytearray()
    with (httpx2.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False) as client,
          client.stream("GET", SOURCE_URL) as response):
        response.raise_for_status()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > 10_000_000:
                raise SourceFormatError("Source archive exceeds the size limit")
    return BytesIO(data)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: import_regions.py SOURCE.zip|--download OUTPUT.json", file=sys.stderr)
        return 2
    output = Path(sys.argv[2])
    try:
        source = download_source() if sys.argv[1] == "--download" else Path(sys.argv[1])
        regions = read_regions(source)
    except (SourceFormatError, OSError, httpx2.HTTPError) as exc:
        print(f"Region import failed: {exc}", file=sys.stderr)
        return 1
    output.write_text(json.dumps([asdict(region) for region in regions], ensure_ascii=False,
                                 separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Exported {len(regions)} locality reference points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
