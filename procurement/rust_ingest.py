from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class SpreadsheetIngestError(RuntimeError):
    pass


def run_spreadsheet_ingest(
    *,
    storage_path: str,
    filename: str,
    binary: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    path = Path(storage_path)
    if not path.is_file():
        raise SpreadsheetIngestError("stored spreadsheet is unavailable")
    if path.stat().st_size > 25 * 1024 * 1024:
        raise SpreadsheetIngestError("spreadsheet exceeds the 25 MB ingest limit")

    try:
        completed = subprocess.run(
            [binary, "--input", str(path), "--filename", Path(filename).name],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise SpreadsheetIngestError("spreadsheet ingest binary is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpreadsheetIngestError("spreadsheet ingest timed out") from exc

    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()[-1:] or [
            "spreadsheet ingest failed"
        ]
        raise SpreadsheetIngestError(message[0][:500])
    if len(completed.stdout.encode("utf-8")) > 10 * 1024 * 1024:
        raise SpreadsheetIngestError("spreadsheet ingest output exceeds 10 MB")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SpreadsheetIngestError("spreadsheet ingest returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise SpreadsheetIngestError(
            "spreadsheet ingest returned an unsupported schema"
        )
    return payload
