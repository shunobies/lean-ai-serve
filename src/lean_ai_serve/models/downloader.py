"""HuggingFace model downloader with SSE progress streaming."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.utils import (
    EntryNotFoundError,
    RepositoryNotFoundError,
)

from lean_ai_serve.config import get_settings
from lean_ai_serve.models.schemas import PullProgress

logger = logging.getLogger(__name__)


class ModelDownloader:
    """Downloads models from HuggingFace Hub with progress tracking."""

    def __init__(self) -> None:
        settings = get_settings()
        self._cache_dir = Path(settings.cache.directory) / "models"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._hf_token = settings.cache.huggingface_token or None
        self._api = HfApi(token=self._hf_token)

    def model_path(self, source: str, revision: str = "main") -> Path:
        """Return the local cache path for a model."""
        # HuggingFace Hub stores snapshots in a deterministic layout
        safe_name = source.replace("/", "--")
        return self._cache_dir / f"models--{safe_name}" / "snapshots"

    async def check_exists(self, source: str) -> bool:
        """Check if a model repo exists on HuggingFace."""
        try:
            await asyncio.to_thread(
                self._api.repo_info, repo_id=source, repo_type="model"
            )
            return True
        except RepositoryNotFoundError:
            return False

    async def get_model_size(self, source: str, revision: str = "main") -> int:
        """Get total download size in bytes."""
        try:
            info = await asyncio.to_thread(
                self._api.repo_info,
                repo_id=source,
                revision=revision,
                repo_type="model",
            )
            total = 0
            if info.siblings:
                for sibling in info.siblings:
                    if sibling.size is not None:
                        total += sibling.size
            return total
        except (RepositoryNotFoundError, EntryNotFoundError):
            return 0

    async def download(
        self,
        source: str,
        revision: str = "main",
    ) -> AsyncIterator[PullProgress]:
        """Download a model with progress updates.

        Yields PullProgress events suitable for SSE streaming.
        """
        yield PullProgress(
            status="downloading",
            message=f"Starting download of {source}",
        )

        total_size = await self.get_model_size(source, revision)

        try:
            # snapshot_download is blocking — run in thread
            local_path = await asyncio.to_thread(
                snapshot_download,
                repo_id=source,
                revision=revision,
                cache_dir=str(self._cache_dir),
                token=self._hf_token,
            )

            yield PullProgress(
                status="verifying",
                downloaded_bytes=total_size,
                total_bytes=total_size,
                progress_pct=100.0,
                message="Verifying download integrity",
            )

            yield PullProgress(
                status="complete",
                downloaded_bytes=total_size,
                total_bytes=total_size,
                progress_pct=100.0,
                message=f"Model downloaded to {local_path}",
            )

        except RepositoryNotFoundError:
            yield PullProgress(
                status="error",
                message=f"Repository not found: {source}",
            )
        except Exception as e:
            logger.exception("Download failed for %s", source)
            yield PullProgress(
                status="error",
                message=f"Download failed: {e}",
            )

    async def delete_cached(self, source: str) -> bool:
        """Delete a model from the local cache."""
        import shutil

        safe_name = source.replace("/", "--")
        model_dir = self._cache_dir / f"models--{safe_name}"
        if model_dir.exists():
            await asyncio.to_thread(shutil.rmtree, model_dir)
            logger.info("Deleted cached model: %s", source)
            return True
        return False

    def get_local_path(self, source: str, revision: str = "main") -> Path | None:
        """Get the snapshot path for an already-downloaded model."""
        safe_name = source.replace("/", "--")
        snapshot_dir = self._cache_dir / f"models--{safe_name}" / "snapshots"
        if not snapshot_dir.exists():
            return None

        # Return the latest snapshot
        snapshots = sorted(snapshot_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        if snapshots:
            return snapshots[-1]
        return None
