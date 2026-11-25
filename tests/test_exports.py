import os
import re
import time

import pandas as pd
import pytest

from backend.services.exports import ExportStore

HOUR = 3600


@pytest.fixture
def store(tmp_path):
    return ExportStore(tmp_path / "exports", public_base_url="https://agent.example.com")


def make_old(path, hours):
    old = time.time() - hours * HOUR
    os.utime(path, (old, old))


def test_export_dir_is_created(tmp_path):
    ExportStore(tmp_path / "nested" / "exports")

    assert (tmp_path / "nested" / "exports").is_dir()


def test_csv_is_utf8_with_bom_and_has_no_index(store):
    df = pd.DataFrame({"city": ["München", "Bengaluru"], "amount": [1500, 250]}, index=[7, 8])

    file_name = store.save_csv(df)

    assert re.fullmatch(r"[0-9a-f]{32}\.csv", file_name)
    raw = store.path_for(file_name).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8-sig").splitlines() == ["city,amount", "München,1500", "Bengaluru,250"]


def test_png_is_saved_under_a_random_name(store):
    first = store.save_png(b"\x89PNG fake")
    second = store.save_png(b"\x89PNG fake")

    assert re.fullmatch(r"[0-9a-f]{32}\.png", first)
    assert first != second
    assert store.path_for(first).read_bytes() == b"\x89PNG fake"


def test_url_uses_the_public_base_url(tmp_path):
    with_base = ExportStore(tmp_path, public_base_url="https://agent.example.com/")
    relative = ExportStore(tmp_path)

    assert with_base.url_for("abc.csv") == "https://agent.example.com/downloads/abc.csv"
    assert relative.url_for("abc.csv") == "/downloads/abc.csv"


@pytest.mark.parametrize(
    "name",
    [
        "../secret.csv",
        "/etc/passwd",
        "notes.txt",
        "",
        "a" * 32 + ".txt",
        "A" * 32 + ".csv",  # upper-case hex is never generated
        "a" * 31 + ".csv",
        "a" * 32 + ".csv\n",  # "$" in a regex would let this through
        "a" * 32 + ".csv/../../x.csv",
    ],
)
def test_malformed_names_are_refused(store, name):
    assert store.path_for(name) is None


def test_files_outside_the_store_are_not_served(store, tmp_path):
    (tmp_path / "secret.csv").write_text("password", encoding="utf-8")

    assert store.path_for("../secret.csv") is None


def test_missing_file_is_none(store):
    assert store.path_for("0" * 32 + ".csv") is None


def test_expired_files_are_hidden_then_deleted(store):
    fresh = store.save_csv(pd.DataFrame({"a": [1]}))
    expired = store.save_png(b"old chart")
    make_old(store.export_dir / expired, hours=25)

    assert store.path_for(expired) is None
    assert store.path_for(fresh) is not None

    assert store.cleanup() == 1
    assert not (store.export_dir / expired).exists()
    assert (store.export_dir / fresh).exists()
    assert store.cleanup() == 0


def test_cleanup_only_touches_its_own_files(store):
    keep = store.export_dir / ".gitkeep"
    keep.write_text("", encoding="utf-8")
    make_old(keep, hours=100)

    assert store.cleanup() == 0
    assert keep.exists()


def test_ttl_comes_from_the_store(tmp_path):
    store = ExportStore(tmp_path, ttl_hours=1)
    file_name = store.save_png(b"chart")
    make_old(store.export_dir / file_name, hours=2)

    assert store.path_for(file_name) is None
