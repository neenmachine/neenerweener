"""Image uploads: decode untrusted bytes, strip metadata, and store fixed WebP variants."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import uuid
from dataclasses import dataclass, fields
from typing import Protocol

from PIL import Image, ImageCms, ImageOps

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 50_000_000
MAX_SIDE = 12_000
LG_EDGE, LG_QUALITY = 2048, 82
SM_EDGE, SM_QUALITY = 800, 80
SWEEP_LIMIT = 20
FORMATS = ("JPEG", "PNG", "WEBP")
VARIANTS = ("lg", "sm")
CONTENT_TYPE = "image/webp"

Image.MAX_IMAGE_PIXELS = MAX_PIXELS
_decode = threading.BoundedSemaphore(1)  # one decode per process, so two large uploads cannot double peak RAM
logger = logging.getLogger(__name__)


class UnsupportedImage(ValueError):
    pass


class MediaStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class MediaAssets(Protocol):
    """The asset rows; `TimelineStore` implements it."""

    def create_media_asset(self, asset_id: uuid.UUID, width: int, height: int, byte_size: int, sha256: bytes) -> None: ...
    def expired_media_assets(self, limit: int) -> list[uuid.UUID]: ...
    def delete_media_asset(self, asset_id: uuid.UUID) -> None: ...


@dataclass(frozen=True)
class MediaConfig:
    MEDIA_BUCKET: str | None = None
    MEDIA_ENDPOINT: str | None = None
    MEDIA_REGION: str | None = None
    MEDIA_ACCESS_KEY_ID: str | None = None
    MEDIA_SECRET_ACCESS_KEY: str | None = None

    @classmethod
    def from_env(cls) -> MediaConfig:
        return cls(**{field.name: os.environ.get(field.name) or None for field in fields(cls)})

    @property
    def missing(self) -> list[str]:
        return [field.name for field in fields(self) if getattr(self, field.name) is None]

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def partial(self) -> bool:
        return 0 < len(self.missing) < len(fields(self))


class S3MediaStore:
    def __init__(self, config: MediaConfig):
        import boto3
        from botocore.config import Config

        self.bucket = config.MEDIA_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=config.MEDIA_ENDPOINT,
            region_name=config.MEDIA_REGION,
            aws_access_key_id=config.MEDIA_ACCESS_KEY_ID,
            aws_secret_access_key=config.MEDIA_SECRET_ACCESS_KEY,
            config=Config(
                s3={"addressing_style": "virtual"},  # Railway buckets use virtual-hosted URLs
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


class MemoryMediaStore:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    def get(self, key: str) -> bytes:
        return self.objects[key][0]

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@dataclass(frozen=True)
class ProcessedImage:
    lg: bytes
    sm: bytes
    width: int
    height: int
    sha256: bytes


def media_key(asset_id: uuid.UUID, variant: str) -> str:
    return f"media/{asset_id}/{variant}.webp"


def fit(width: int, height: int, edge: int) -> tuple[int, int]:
    """Scale so the long edge is at most `edge`; never upscale."""
    scale = edge / max(width, height)
    if scale >= 1:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))


def process_image(data: bytes) -> ProcessedImage:
    """Decode JPEG/PNG/WebP bytes and re-encode them as metadata-free WebP variants."""
    with _decode:
        try:
            with Image.open(io.BytesIO(data), formats=FORMATS) as source:
                if source.format not in FORMATS or getattr(source, "n_frames", 1) > 1:
                    raise UnsupportedImage
                width, height = source.size
                if width * height > MAX_PIXELS or max(width, height) > MAX_SIDE:
                    raise UnsupportedImage
                if source.format == "JPEG":
                    source.draft("RGB", (LG_EDGE, LG_EDGE))  # decode at 1/2..1/8 scale to bound memory
                source.load()
                image = _srgb(ImageOps.exif_transpose(source))
                lg = image.resize(fit(*image.size, LG_EDGE), Image.Resampling.LANCZOS)
                sm = lg.resize(fit(*lg.size, SM_EDGE), Image.Resampling.LANCZOS)
                lg_bytes, sm_bytes = _webp(lg, LG_QUALITY), _webp(sm, SM_QUALITY)
        except UnsupportedImage:
            raise
        except Exception as exc:  # any decode failure is the client's bad input
            raise UnsupportedImage from exc
    return ProcessedImage(lg_bytes, sm_bytes, lg.width, lg.height, hashlib.sha256(lg_bytes).digest())


def _srgb(image: Image.Image) -> Image.Image:
    mode = "RGBA" if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info else "RGB"
    icc = image.info.get("icc_profile")
    if icc:
        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if image.mode not in ("RGB", "RGBA", "CMYK", "L"):
            image = image.convert(mode)
        return ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"), outputMode=mode)
    return image.convert(mode)


def _webp(image: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    # No exif=, icc_profile= or xmp= arguments: the saved file carries pixels only.
    image.save(out, "WEBP", quality=quality, method=4)
    return out.getvalue()


def store_image(assets: MediaAssets, store: MediaStore, image: ProcessedImage) -> uuid.UUID:
    """Write the asset row first, so every stored object has a row the sweep can find."""
    asset_id = uuid.uuid4()
    assets.create_media_asset(asset_id, image.width, image.height, len(image.lg) + len(image.sm), image.sha256)
    store.put(media_key(asset_id, "lg"), image.lg, CONTENT_TYPE)
    store.put(media_key(asset_id, "sm"), image.sm, CONTENT_TYPE)
    return asset_id


def sweep(assets: MediaAssets, store: MediaStore) -> None:
    """Remove a bounded batch of expired assets: objects first, then the row."""
    for asset_id in assets.expired_media_assets(SWEEP_LIMIT):
        try:
            for variant in VARIANTS:
                store.delete(media_key(asset_id, variant))
        except Exception as exc:
            logger.warning("media.sweep.object_delete_failed: %s", type(exc).__name__)
            continue  # keep the row so the next sweep retries
        assets.delete_media_asset(asset_id)
