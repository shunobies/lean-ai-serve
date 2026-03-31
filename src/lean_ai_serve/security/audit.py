"""Append-only audit logger with hash chain integrity for tamper detection."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from lean_ai_serve.config import get_settings
from lean_ai_serve.db import Database

logger = logging.getLogger(__name__)


class AuditLogger:
    """Structured audit logger with hash chain for tamper detection.

    Each row includes a SHA-256 hash of the previous row, forming a chain
    that can be verified to detect unauthorized modifications.

    Optionally encrypts prompt/response content at rest when an
    EncryptionService is provided.
    """

    def __init__(self, db: Database, encryption=None):
        self._db = db
        self._encryption = encryption  # Optional EncryptionService
        self._last_hash: str | None = None

    async def initialize(self) -> None:
        """Load the last chain hash from the database."""
        row = await self._db.fetchone(
            "SELECT chain_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        )
        self._last_hash = row["chain_hash"] if row else "genesis"
        logger.info("Audit logger initialized (chain position: %s)", self._last_hash[:16])

    def _compute_chain_hash(self, row_data: str) -> str:
        """Compute hash for the chain: SHA-256(previous_hash + row_data)."""
        previous = self._last_hash or "genesis"
        combined = f"{previous}:{row_data}"
        return hashlib.sha256(combined.encode()).hexdigest()

    @staticmethod
    def _hash_content(content: str | None) -> str | None:
        """SHA-256 hash of content (for hash-only mode or integrity)."""
        if content is None:
            return None
        return hashlib.sha256(content.encode()).hexdigest()

    async def log(
        self,
        *,
        user_id: str,
        user_role: str = "",
        source_ip: str = "",
        action: str,
        model: str | None = None,
        prompt: str | None = None,
        response: str | None = None,
        token_count: int = 0,
        latency_ms: int = 0,
        status: str = "success",
        error_detail: str | None = None,
    ) -> None:
        """Record an audit entry."""
        settings = get_settings()

        if not settings.audit.enabled:
            return

        request_id = str(uuid4())
        timestamp = datetime.now(UTC).isoformat()
        prompt_hash = self._hash_content(prompt)
        response_hash = self._hash_content(response)

        # Determine what content to store
        store_prompt = None
        store_response = None
        if settings.audit.log_prompts and not settings.audit.log_prompts_hash_only:
            store_prompt = prompt
            store_response = response

            # Encrypt at rest if configured
            if self._encryption and store_prompt:
                store_prompt = self._encryption.encrypt(store_prompt)
            if self._encryption and store_response:
                store_response = self._encryption.encrypt(store_response)

        # Build row data string for chain hash
        row_data = json.dumps(
            {
                "timestamp": timestamp,
                "request_id": request_id,
                "user_id": user_id,
                "action": action,
                "model": model,
                "prompt_hash": prompt_hash,
                "response_hash": response_hash,
                "status": status,
            },
            sort_keys=True,
        )
        chain_hash = self._compute_chain_hash(row_data)

        await self._db.execute(
            """
            INSERT INTO audit_log (
                timestamp, request_id, user_id, user_role, source_ip,
                action, model, prompt_content, prompt_hash,
                response_content, response_hash, token_count, latency_ms,
                status, error_detail, chain_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                request_id,
                user_id,
                user_role,
                source_ip,
                action,
                model,
                store_prompt,
                prompt_hash,
                store_response,
                response_hash,
                token_count,
                latency_ms,
                status,
                error_detail,
                chain_hash,
            ),
        )
        await self._db.commit()
        self._last_hash = chain_hash

    async def query(
        self,
        *,
        user_id: str | None = None,
        action: str | None = None,
        model: str | None = None,
        status: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Query audit log entries with optional filters.

        Returns (entries, total_count).
        """
        conditions = []
        params: list = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if model:
            conditions.append("model = ?")
            params.append(model)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if from_time:
            conditions.append("timestamp >= ?")
            params.append(from_time.isoformat())
        if to_time:
            conditions.append("timestamp <= ?")
            params.append(to_time.isoformat())

        where = " AND ".join(conditions) if conditions else "1=1"

        # Count total
        count_row = await self._db.fetchone(
            f"SELECT COUNT(*) as cnt FROM audit_log WHERE {where}", tuple(params)
        )
        total = count_row["cnt"] if count_row else 0

        # Fetch page
        rows = await self._db.fetchall(
            f"""
            SELECT id, timestamp, request_id, user_id, user_role, source_ip,
                   action, model, prompt_hash, response_hash,
                   token_count, latency_ms, status, error_detail, chain_hash
            FROM audit_log
            WHERE {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )

        entries = [dict(row) for row in rows]
        return entries, total

    async def verify_chain(self, limit: int = 1000) -> tuple[bool, str]:
        """Verify the hash chain integrity of the last N entries.

        Returns (is_valid, message).
        """
        rows = await self._db.fetchall(
            "SELECT * FROM audit_log ORDER BY id ASC LIMIT ?", (limit,)
        )
        if not rows:
            return True, "No audit entries to verify"

        previous_hash = "genesis"
        for row in rows:
            row_data = json.dumps(
                {
                    "timestamp": row["timestamp"],
                    "request_id": row["request_id"],
                    "user_id": row["user_id"],
                    "action": row["action"],
                    "model": row["model"],
                    "prompt_hash": row["prompt_hash"],
                    "response_hash": row["response_hash"],
                    "status": row["status"],
                },
                sort_keys=True,
            )
            expected = hashlib.sha256(f"{previous_hash}:{row_data}".encode()).hexdigest()
            if row["chain_hash"] != expected:
                return False, (
                    f"Chain broken at entry {row['id']}: "
                    f"expected {expected[:16]}..., got {row['chain_hash'][:16]}..."
                )
            previous_hash = row["chain_hash"]

        return True, f"Chain verified: {len(rows)} entries OK"
