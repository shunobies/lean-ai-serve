"""Dataset management — upload, validate, store, preview."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from lean_ai_serve.config import Settings
from lean_ai_serve.db import Database
from lean_ai_serve.training.schemas import DatasetFormat, DatasetInfo

logger = logging.getLogger(__name__)


class DatasetValidationError(Exception):
    """Raised when a dataset fails format validation."""


class DatasetManager:
    """Manages training dataset lifecycle — upload, validate, store, list, delete."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._dataset_dir = Path(settings.training.dataset_directory)
        self._dataset_dir.mkdir(parents=True, exist_ok=True)
        self._max_size = settings.training.max_dataset_size_mb * 1024 * 1024

    async def upload(
        self,
        name: str,
        fmt: DatasetFormat,
        content: bytes,
        uploaded_by: str,
        description: str = "",
        *,
        source: str = "manual_upload",
    ) -> DatasetInfo:
        """Upload and validate a dataset.

        Raises DatasetValidationError on invalid format.
        Raises ValueError on duplicate name or size limit exceeded.
        """
        # Check size limit
        if len(content) > self._max_size:
            raise ValueError(
                f"Dataset exceeds max size "
                f"({len(content)} > {self._max_size} bytes)"
            )

        # Check for duplicate name
        existing = await self._db.fetchone(
            "SELECT name FROM datasets WHERE name = ?", (name,)
        )
        if existing:
            raise ValueError(f"Dataset '{name}' already exists")

        # Validate content
        row_count = self._validate_and_count(content, fmt)

        # Store to filesystem
        ext = self._format_extension(fmt)
        dataset_dir = self._dataset_dir / name
        dataset_dir.mkdir(parents=True, exist_ok=True)
        data_path = dataset_dir / f"data.{ext}"
        data_path.write_bytes(content)

        now = datetime.now(UTC).isoformat()
        info = DatasetInfo(
            name=name,
            path=str(data_path),
            format=fmt,
            row_count=row_count,
            size_bytes=len(content),
            uploaded_by=uploaded_by,
            created_at=datetime.now(UTC),
            description=description,
        )

        # Persist metadata to DB
        await self._db.execute(
            """
            INSERT INTO datasets (name, path, format, row_count, size_bytes,
                                  uploaded_by, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(data_path),
                fmt.value,
                row_count,
                len(content),
                uploaded_by,
                now,
                json.dumps({"description": description, "source": source}),
            ),
        )
        await self._db.commit()

        logger.info(
            "Dataset uploaded: %s (%s, %d rows, %d bytes)",
            name, fmt.value, row_count or 0, len(content),
        )
        return info

    async def create_empty_jsonl(
        self,
        name: str,
        fmt: DatasetFormat,
        uploaded_by: str,
        *,
        source: str,
        description: str = "",
    ) -> DatasetInfo:
        """Create an empty JSONL-backed dataset for later appends.

        Used by the lean_ai ingestor to pre-allocate a dataset that rows will
        be appended to incrementally. Raises ValueError on duplicate name.
        """
        if fmt not in (DatasetFormat.JSONL, DatasetFormat.DPO):
            raise ValueError(f"create_empty_jsonl requires JSONL or DPO format, got {fmt}")

        existing = await self._db.fetchone(
            "SELECT name FROM datasets WHERE name = ?", (name,)
        )
        if existing:
            raise ValueError(f"Dataset '{name}' already exists")

        ext = self._format_extension(fmt)
        dataset_dir = self._dataset_dir / name
        dataset_dir.mkdir(parents=True, exist_ok=True)
        data_path = dataset_dir / f"data.{ext}"
        data_path.touch()

        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            """
            INSERT INTO datasets (name, path, format, row_count, size_bytes,
                                  uploaded_by, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(data_path),
                fmt.value,
                0,
                0,
                uploaded_by,
                now,
                json.dumps({"description": description, "source": source}),
            ),
        )
        await self._db.commit()

        return DatasetInfo(
            name=name,
            path=str(data_path),
            format=fmt,
            row_count=0,
            size_bytes=0,
            uploaded_by=uploaded_by,
            created_at=datetime.now(UTC),
            description=description,
        )

    async def append_jsonl(self, name: str, rows: list[dict]) -> int:
        """Append rows to an existing JSONL/DPO-formatted dataset.

        Returns the number of rows appended. Updates ``row_count`` and
        ``size_bytes`` atomically. Raises ValueError if the dataset is not
        JSONL/DPO or does not exist.
        """
        if not rows:
            return 0

        row = await self._db.fetchone(
            "SELECT path, format, row_count, size_bytes "
            "FROM datasets WHERE name = ?",
            (name,),
        )
        if row is None:
            raise ValueError(f"Dataset not found: {name}")

        fmt = DatasetFormat(row["format"])
        if fmt not in (DatasetFormat.JSONL, DatasetFormat.DPO):
            raise ValueError(
                f"append_jsonl requires JSONL or DPO format, got {fmt}"
            )

        data_path = Path(row["path"])
        payload = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
        payload_bytes = payload.encode("utf-8")

        new_size = (row["size_bytes"] or 0) + len(payload_bytes)
        if new_size > self._max_size:
            raise ValueError(
                f"Append would exceed max size "
                f"({new_size} > {self._max_size} bytes)"
            )

        with data_path.open("ab") as f:
            f.write(payload_bytes)

        new_count = (row["row_count"] or 0) + len(rows)
        await self._db.execute(
            "UPDATE datasets SET row_count = ?, size_bytes = ? WHERE name = ?",
            (new_count, new_size, name),
        )
        await self._db.commit()
        return len(rows)

    async def replace_jsonl(self, name: str, rows: list[dict]) -> int:
        """Atomically replace an existing JSONL/DPO dataset with the given rows.

        Used by the memories ingestion path, where lean_ai exposes a
        snapshot endpoint with no cursor: the full curated set is pulled
        each cycle and we want the landed file to mirror the remote.
        Writes to a temporary file in the same directory and ``os.replace``s
        it into place, so partial failure leaves the previous version
        intact. Returns the number of rows written.
        """
        row = await self._db.fetchone(
            "SELECT path, format FROM datasets WHERE name = ?", (name,)
        )
        if row is None:
            raise ValueError(f"Dataset not found: {name}")
        fmt = DatasetFormat(row["format"])
        if fmt not in (DatasetFormat.JSONL, DatasetFormat.DPO):
            raise ValueError(
                f"replace_jsonl requires JSONL or DPO format, got {fmt}"
            )

        data_path = Path(row["path"])
        payload = "".join(
            json.dumps(r, separators=(",", ":")) + "\n" for r in rows
        )
        payload_bytes = payload.encode("utf-8")
        if len(payload_bytes) > self._max_size:
            raise ValueError(
                f"Replace would exceed max size "
                f"({len(payload_bytes)} > {self._max_size} bytes)"
            )

        tmp_path = data_path.with_suffix(data_path.suffix + ".tmp")
        tmp_path.write_bytes(payload_bytes)
        import os
        os.replace(tmp_path, data_path)

        await self._db.execute(
            "UPDATE datasets SET row_count = ?, size_bytes = ? WHERE name = ?",
            (len(rows), len(payload_bytes), name),
        )
        await self._db.commit()
        return len(rows)

    async def list_datasets(self) -> list[DatasetInfo]:
        """List all datasets."""
        rows = await self._db.fetchall(
            "SELECT * FROM datasets ORDER BY created_at DESC"
        )
        return [self._row_to_info(row) for row in rows]

    async def get(self, name: str) -> DatasetInfo | None:
        """Get a dataset by name."""
        row = await self._db.fetchone(
            "SELECT * FROM datasets WHERE name = ?", (name,)
        )
        if row is None:
            return None
        return self._row_to_info(row)

    async def delete(self, name: str) -> bool:
        """Delete a dataset from DB and filesystem."""
        row = await self._db.fetchone(
            "SELECT path FROM datasets WHERE name = ?", (name,)
        )
        if row is None:
            return False

        # Remove from filesystem
        data_path = Path(row["path"])
        if data_path.exists():
            data_path.unlink()
        # Remove parent dir if empty
        parent = data_path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

        await self._db.execute("DELETE FROM datasets WHERE name = ?", (name,))
        await self._db.commit()
        logger.info("Dataset deleted: %s", name)
        return True

    async def preview(self, name: str, limit: int = 5) -> list[dict]:
        """Return the first N rows of a dataset for preview."""
        row = await self._db.fetchone(
            "SELECT path, format FROM datasets WHERE name = ?", (name,)
        )
        if row is None:
            return []

        data_path = Path(row["path"])
        if not data_path.exists():
            return []

        content = data_path.read_bytes()
        fmt = DatasetFormat(row["format"])
        return self._read_rows(content, fmt, limit)

    async def get_path(self, name: str) -> str | None:
        """Get filesystem path for a dataset."""
        row = await self._db.fetchone(
            "SELECT path FROM datasets WHERE name = ?", (name,)
        )
        return row["path"] if row else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_and_count(self, content: bytes, fmt: DatasetFormat) -> int:
        """Validate dataset content and return row count.

        Raises DatasetValidationError on invalid format.
        """
        text = content.decode("utf-8")

        if fmt == DatasetFormat.SHAREGPT:
            return self._validate_sharegpt(text)
        elif fmt == DatasetFormat.ALPACA:
            return self._validate_alpaca(text)
        elif fmt == DatasetFormat.JSONL:
            return self._validate_jsonl(text)
        elif fmt == DatasetFormat.CSV:
            return self._validate_csv(text)
        elif fmt == DatasetFormat.DPO:
            return self._validate_dpo(text)
        else:
            raise DatasetValidationError(f"Unknown format: {fmt}")

    def _validate_sharegpt(self, text: str) -> int:
        """Validate ShareGPT format — JSON array of conversations."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise DatasetValidationError(f"Invalid JSON: {e}") from e

        if not isinstance(data, list):
            raise DatasetValidationError("ShareGPT must be a JSON array")

        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise DatasetValidationError(
                    f"ShareGPT item {i} must be an object"
                )
            if "conversations" not in item:
                raise DatasetValidationError(
                    f"ShareGPT item {i} missing 'conversations' key"
                )
            convs = item["conversations"]
            if not isinstance(convs, list) or len(convs) == 0:
                raise DatasetValidationError(
                    f"ShareGPT item {i}: 'conversations' must be a non-empty array"
                )

        return len(data)

    def _validate_alpaca(self, text: str) -> int:
        """Validate Alpaca format — JSON array with instruction/output."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise DatasetValidationError(f"Invalid JSON: {e}") from e

        if not isinstance(data, list):
            raise DatasetValidationError("Alpaca must be a JSON array")

        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise DatasetValidationError(
                    f"Alpaca item {i} must be an object"
                )
            if "instruction" not in item:
                raise DatasetValidationError(
                    f"Alpaca item {i} missing 'instruction' key"
                )
            if "output" not in item:
                raise DatasetValidationError(
                    f"Alpaca item {i} missing 'output' key"
                )

        return len(data)

    def _validate_jsonl(self, text: str) -> int:
        """Validate JSONL format — one JSON object per line."""
        lines = [ln for ln in text.strip().split("\n") if ln.strip()]
        if not lines:
            raise DatasetValidationError("JSONL file is empty")

        for i, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise DatasetValidationError(
                    f"JSONL line {i + 1} invalid JSON: {e}"
                ) from e
            if not isinstance(obj, dict):
                raise DatasetValidationError(
                    f"JSONL line {i + 1} must be a JSON object"
                )

        return len(lines)

    def _validate_dpo(self, text: str) -> int:
        """Validate DPO JSONL — each line must have prompt + chosen + rejected."""
        lines = [ln for ln in text.strip().split("\n") if ln.strip()]
        if not lines:
            # Empty DPO file is valid (ingestor starts empty).
            return 0

        required = {"prompt", "chosen", "rejected"}
        for i, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise DatasetValidationError(
                    f"DPO line {i + 1} invalid JSON: {e}"
                ) from e
            if not isinstance(obj, dict):
                raise DatasetValidationError(
                    f"DPO line {i + 1} must be a JSON object"
                )
            missing = required - obj.keys()
            if missing:
                raise DatasetValidationError(
                    f"DPO line {i + 1} missing required keys: {sorted(missing)}"
                )
        return len(lines)

    def _validate_csv(self, text: str) -> int:
        """Validate CSV format — must have a header row."""
        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration as e:
            raise DatasetValidationError("CSV file is empty") from e

        if len(header) < 2:
            raise DatasetValidationError(
                "CSV must have at least 2 columns"
            )

        count = sum(1 for _ in reader)
        if count == 0:
            raise DatasetValidationError("CSV has header but no data rows")

        return count

    def _read_rows(
        self, content: bytes, fmt: DatasetFormat, limit: int
    ) -> list[dict]:
        """Read first N rows from dataset content."""
        text = content.decode("utf-8")

        if fmt in (DatasetFormat.SHAREGPT, DatasetFormat.ALPACA):
            data = json.loads(text)
            return data[:limit]

        elif fmt in (DatasetFormat.JSONL, DatasetFormat.DPO):
            lines = [ln for ln in text.strip().split("\n") if ln.strip()]
            return [json.loads(ln) for ln in lines[:limit]]

        elif fmt == DatasetFormat.CSV:
            reader = csv.DictReader(io.StringIO(text))
            rows = []
            for i, row in enumerate(reader):
                if i >= limit:
                    break
                rows.append(dict(row))
            return rows

        return []

    @staticmethod
    def _format_extension(fmt: DatasetFormat) -> str:
        """Return file extension for a dataset format."""
        return {
            DatasetFormat.SHAREGPT: "json",
            DatasetFormat.ALPACA: "json",
            DatasetFormat.JSONL: "jsonl",
            DatasetFormat.CSV: "csv",
            DatasetFormat.DPO: "jsonl",
        }[fmt]

    @staticmethod
    def _row_to_info(row) -> DatasetInfo:
        """Convert a DB row to DatasetInfo."""
        meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
        return DatasetInfo(
            name=row["name"],
            path=row["path"],
            format=DatasetFormat(row["format"]),
            row_count=row["row_count"],
            size_bytes=row["size_bytes"],
            uploaded_by=row["uploaded_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            description=meta.get("description", ""),
        )
