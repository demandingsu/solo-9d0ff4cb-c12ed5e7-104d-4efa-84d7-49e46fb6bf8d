import pytest

from app.csv_parse import CsvError, parse_csv

HEADER = "样品编号,检测项目,结果,单位"


def test_parse_ok():
    raw = (
        f"{HEADER}\r\n"
        "S-1,铅Pb,0.02,mg/kg\r\n"
        "S-1,镉Cd,0.01,mg/kg\r\n"
    ).encode("utf-8")
    records = parse_csv(raw, "a.csv")
    assert len(records) == 2
    assert records[0].sample_no == "S-1"
    assert records[1].unit == "mg/kg"


def test_parse_bom_and_blank_lines():
    raw = ("﻿" + f"{HEADER}\n\nS-1,铅,0.02,mg/kg\n\n").encode("utf-8")
    records = parse_csv(raw, "a.csv")
    assert len(records) == 1


def test_bad_header():
    raw = "样品编号,检测项目,结果\nS-1,铅,0.02\n".encode("utf-8")
    with pytest.raises(CsvError) as exc:
        parse_csv(raw, "a.csv")
    assert "表头" in str(exc.value)


def test_empty_field_rejected():
    raw = (f"{HEADER}\nS-1,铅,,mg/kg\n").encode("utf-8")
    with pytest.raises(CsvError):
        parse_csv(raw, "a.csv")


def test_wrong_column_count():
    raw = (f"{HEADER}\nS-1,铅,0.02\n").encode("utf-8")
    with pytest.raises(CsvError):
        parse_csv(raw, "a.csv")


def test_non_utf8_rejected():
    with pytest.raises(CsvError):
        parse_csv(b"\xff\xfe" + b"\x00\x01", "a.csv")
