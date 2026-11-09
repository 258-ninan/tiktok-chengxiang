"""Write spreadsheet-safe CSVs and publish one complete report bundle."""

import csv
import os
import re
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


BUNDLE_NAME = "reports.zip"
FORMULA_PREFIX = re.compile(r"[\s\u200b\ufeff']*[=+\-@＝＋－＠\x00\t\r\n]")


def csv_value(value):
    if isinstance(value, str) and FORMULA_PREFIX.match(value):
        return "[text] " + value
    return value


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows({field: csv_value(row.get(field)) for field in fields} for row in rows)


@contextmanager
def publish_reports(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=directory, prefix=".reports-", ignore_cleanup_errors=True) as temporary:
        staging = Path(temporary) / "files"
        staging.mkdir()
        yield staging
        archive = Path(temporary) / BUNDLE_NAME
        with archive.open("wb") as stream:
            with ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=3) as bundle:
                for path in sorted(staging.iterdir()):
                    bundle.write(path, arcname=path.name)
            stream.flush()
            os.fsync(stream.fileno())
        archive.replace(directory / BUNDLE_NAME)
