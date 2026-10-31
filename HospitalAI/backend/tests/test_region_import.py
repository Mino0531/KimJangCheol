"""Test the official region workbook import through its real CLI."""

import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

Workbook = pytest.importorskip("openpyxl", reason="optional importer dependency").Workbook

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import_regions.py"
HEADER = ["구분", "행정구역코드", "1단계", "2단계", "3단계", "격자 X", "격자 Y",
          "경도(시)", "경도(분)", "경도(초)", "위도(시)", "위도(분)", "위도(초)",
          "경도(초/100)", "위도(초/100)"]


def make_archive(tmp_path: Path, rows: list[list[str | float]]) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
    xlsx = tmp_path / "source.xlsx"
    workbook.save(xlsx)
    workbook.close()
    archive = tmp_path / "source.zip"
    with ZipFile(archive, "w") as zipped:
        zipped.write(xlsx, "source.xlsx")
    return archive


def test_region_import_preserves_korean_names_and_ignores_non_locality(tmp_path: Path) -> None:
    # Given
    base = ["kor", "1111053000", "서울특별시", "종로구", "사직동", "", "", "", "", "", "", "", "", 126.970955555, 37.573269444]
    district = ["kor", "1111000000", "서울특별시", "종로구", "", "", "", "", "", "", "", "", "", 126.98164, 37.57038]
    archive = make_archive(tmp_path, [base, district])
    output = tmp_path / "regions.json"
    # When
    result = subprocess.run([sys.executable, str(SCRIPT), str(archive), str(output)], capture_output=True, text=True, check=False)
    # Then
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == [{"code": "1111053000", "province": "서울특별시", "district": "종로구", "locality": "사직동", "latitude": 37.5732694, "longitude": 126.9709556}]


def test_region_import_rejects_duplicate_ids_without_writing_output(tmp_path: Path) -> None:
    # Given
    row = ["kor", "1111053000", "서울특별시", "종로구", "사직동", "", "", "", "", "", "", "", "", 126.97, 37.57]
    archive = make_archive(tmp_path, [row, row])
    output = tmp_path / "regions.json"
    # When
    result = subprocess.run([sys.executable, str(SCRIPT), str(archive), str(output)], capture_output=True, text=True, check=False)
    # Then
    assert result.returncode != 0
    assert not output.exists()


def test_region_import_rejects_out_of_bounds_coordinates_without_writing_output(tmp_path: Path) -> None:
    # Given
    row = ["kor", "1111053000", "서울특별시", "종로구", "사직동", "", "", "", "", "", "", "", "", 90.0, 37.57]
    archive = make_archive(tmp_path, [row])
    output = tmp_path / "regions.json"
    # When
    result = subprocess.run([sys.executable, str(SCRIPT), str(archive), str(output)], capture_output=True, text=True, check=False)
    # Then
    assert result.returncode != 0
    assert not output.exists()
