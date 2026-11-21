"""Object storage for source videos and rendered clips.

The local filesystem backend is the default for development. An S3-compatible
backend (S3, R2, MinIO) is selected with ``QUICKREEL_STORAGE_BACKEND=s3``.

Why this exists: on a host with an ephemeral filesystem, a container restart
keeps the database row but loses the MP4, so a clip reads as "rendered" while
there is nothing to play. Keeping the bytes in object storage decouples them
from the container's lifetime. ffmpeg still needs a real file to work on, so
callers fetch a local copy on demand rather than streaming from the bucket.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from .config import get_settings
from .logging_setup import get_logger

logger = get_logger(__name__)


class Storage(ABC):
    """Where source videos and rendered clips live."""

    # True when the bytes outlive this container. Local files do not, so a
    # local backend must not be treated as a durable copy.
    is_remote: bool = False

    @abstractmethod
    def put(self, local_path: str | Path, key: str) -> str:
        """Store a local file under ``key`` and return a URI."""

    @abstractmethod
    def get(self, key: str, local_path: str | Path) -> Path:
        """Copy ``key`` to ``local_path`` and return that path."""

    @abstractmethod
    def url_for(self, key: str) -> str:
        """Return a retrievable location for ``key``."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...


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
        if Path(local_path).resolve() != target.resolve():
            shutil.copy2(local_path, target)
        logger.info("storage.put", key=key, bytes=target.stat().st_size)
        return target.as_uri()

    def get(self, key: str, local_path: str | Path) -> Path:
        source = self._path(key)
        target = Path(local_path)
        if source.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return target

    def url_for(self, key: str) -> str:
        return self._path(key).as_uri()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def path_for(self, key: str) -> Path:
        return self._path(key)


class S3Storage(Storage):
    """S3-compatible storage."""

    is_remote = True

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        presign_seconds: int = 3600,
    ) -> None:
        try:
            import boto3  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("boto3 is required for S3 storage") from exc
        if not bucket:
            raise RuntimeError("QUICKREEL_S3_BUCKET is required for S3 storage")
        self.bucket = bucket
        self._presign_seconds = presign_seconds
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region_name or None,
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
        )

    def put(self, local_path: str | Path, key: str) -> str:
        self._client.upload_file(str(local_path), self.bucket, key)
        logger.info("storage.put", key=key, bucket=self.bucket)
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str, local_path: str | Path) -> Path:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, key, str(target))
        return target

    def url_for(self, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self._presign_seconds,
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - botocore raises its own error types
            return False

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        settings = get_settings()
        if settings.storage_backend.lower() == "s3":
            _storage = S3Storage(
                bucket=settings.s3_bucket,
                endpoint_url=settings.s3_endpoint_url,
                region_name=settings.s3_region,
                access_key_id=settings.s3_access_key_id,
                secret_access_key=settings.s3_secret_access_key,
                presign_seconds=settings.s3_presign_seconds,
            )
        else:
            _storage = LocalStorage()
    return _storage


def reset_storage() -> None:
    """Drop the cached backend. Tests swap configuration between cases."""
    global _storage
    _storage = None
