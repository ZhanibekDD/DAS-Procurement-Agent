from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from procurement.db import Database
from procurement.rust_ingest import SpreadsheetIngestError, run_spreadsheet_ingest
from procurement.service import ProcurementService


class RustIngestBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.binary = self.root / "fake-ingest"
        self.binary.write_text(
            """#!/usr/bin/env python3
import json
import pathlib
import sys
input_path = pathlib.Path(sys.argv[sys.argv.index('--input') + 1])
filename = sys.argv[sys.argv.index('--filename') + 1]
print(json.dumps({
    'schema_version': '1.0',
    'source': {'filename': filename, 'size_bytes': input_path.stat().st_size},
    'suppliers': [{'name': 'ТОО Тест'}],
    'summaries': [],
    'line_items': [],
    'warnings': [],
}))
""",
            encoding="utf-8",
        )
        self.binary.chmod(0o700)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_bridge_runs_fixed_binary_without_shell(self):
        source = self.root / "source.xlsx"
        source.write_bytes(b"PK-test")
        result = run_spreadsheet_ingest(
            storage_path=str(source),
            filename="../dangerous name.xlsx",
            binary=str(self.binary),
            timeout_seconds=5,
        )
        self.assertEqual(result["source"]["filename"], "dangerous name.xlsx")
        self.assertEqual(result["suppliers"][0]["name"], "ТОО Тест")

    def test_bridge_rejects_unsupported_schema(self):
        self.binary.write_text(
            "#!/usr/bin/env python3\nprint('{\"schema_version\":\"2.0\"}')\n",
            encoding="utf-8",
        )
        self.binary.chmod(0o700)
        source = self.root / "source.csv"
        source.write_text("name;price\nitem;1", encoding="utf-8")
        with self.assertRaisesRegex(SpreadsheetIngestError, "unsupported schema"):
            run_spreadsheet_ingest(
                storage_path=str(source),
                filename="source.csv",
                binary=str(self.binary),
                timeout_seconds=5,
            )

    def test_service_preview_is_read_only_and_requires_human_review(self):
        database_path = self.root / "procurement.db"
        database = Database(str(database_path))
        database.initialize()
        service = ProcurementService(
            database,
            ingest_binary=str(self.binary),
            ingest_timeout_seconds=5,
        )
        document = service.register_source_document(
            filename="offer.xlsx",
            content=b"PK-test",
            document_type="commercial_offer",
        )
        result = service.preview_spreadsheet(int(document["id"]))
        self.assertEqual(result["document_id"], document["id"])
        self.assertEqual(result["decision"], "human_review_required")
        self.assertFalse(result["persisted"])

    def test_ui_exposes_rust_preview_without_auto_commit(self):
        html = (
            Path(__file__).parents[1] / "procurement" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("previewSpreadsheet", html)
        self.assertIn("extract/spreadsheet-preview", html)
        self.assertIn("Цены по позициям", html)
        self.assertIn("offers.length", html)
        self.assertIn("Без автоматической записи", html)


if __name__ == "__main__":
    unittest.main()
