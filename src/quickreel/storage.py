"""Storage abstraction.

The local filesystem backend is the default for development. An S3-compatible
backend (MinIO, R2, S3) is provided behind the same interface; select it with
``QUICKREEL_STORAGE_BACKEND=s3`` plus the usual AWS credentials.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import get_settings
from ..logging_setup import get_logger

logger = get_logger(__name__)


class Storage(ABC):
    @abstractmethod
    def put(self, local_path: str | Path, key: str) -> str:
        """Store a local file under ``key`` and return a URI."""

    @abstractmethod
    def url_for(self, key: str) -> str:
        """Return a retrievable location for ``key``."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class LocalStorage(Storage):
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or get_settings().data_dir / "objects")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Reject traversal: keys are always relative to the storage root.
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root.resolve())):
            raise ValueError(f"unsafe storage key: {key!r}")
        return candidate

    def put(self, local_path: str | Path, key: str) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, target)
        logger.info("storage.put", key=key, bytes=target.stat().st_size)
        return target.as_uri()

    def url_for(self, key: str) -> str:
        return self._path(key).as_uri()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def path_for(self, key: str) -> Path:
        return self._path(key)


class S3Storage(Storage):
    """S3-compatible storage. Requires the optional ``boto3`` dependency."""

    def __init__(self, bucket: str, endpoint_url: str | None = None) -> None:
        try:
            import boto3  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("boto3 is required for S3 storage") from exc
        self.bucket = bucket
        self._client = boto3.client("s3", endpoint_url=endpoint_url)

    def put(self, local_path: str | Path, key: str) -> str:
        self._client.upload_file(str(local_path), self.bucket, key)
        return f"s3://{self.bucket}/{key}"

    def url_for(self, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - boto3 raises botocore exceptions
            return False


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        settings = get_settings()
        backend = getattr(settings, "storage_backend", "local")
        if backend == "s3":  # pragma: no cover - requires external service
            _storage = S3Storage(
                bucket=getattr(settings, "s3_bucket", "quickreel"),
                endpoint_url=getattr(settings, "s3_endpoint_url", None) or None,
            )
        else:
            _storage = LocalStorage()
    return _storage
