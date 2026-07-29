from __future__ import annotations

import json
import logging
import os
import struct
import time
from datetime import datetime
from typing import Any, Optional, Final, Literal, TypedDict, Mapping, Sequence, cast
from urllib import request, parse
import urllib.error as urlerror
from urllib.parse import urlsplit, parse_qsl, urlunsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody

try:
    # Prefer boto3-stubs for accurate typing if available (no runtime dependency)
    from mypy_boto3_s3 import S3Client  # type: ignore
except Exception:
    from typing import Any as _Any
    S3Client = _Any  # type: ignore


# ---------- Types ----------
class LambdaResponse(TypedDict):
    statusCode: int
    body: str

MediaType = Literal["IMAGE", "VIDEO"]


# ---------- Config ----------
BUCKET_NAME: Final[Optional[str]] = os.getenv("BUCKET_NAME")  # REQUIRED
UPLOADS_FOLDER: Final[str] = "uploads/"
PROCESSED_FOLDER: Final[str] = "processed/"
REJECTED_FOLDER: Final[str] = "rejected/"
HTTP_TIMEOUT: Final[int] = 10
SECRET_NAME: Final[str] = "instagram_api_token"  # only a reference, not the secret value
# Instagram Login tokens must use graph.instagram.com (not graph.facebook.com)
GRAPH_API_HOST: Final[str] = "https://graph.instagram.com"
GRAPH_API_VERSION: Final[str] = "v18.0"  # keep aligned with working Postman calls

# Supported media
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mp4", ".mov"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# Instagram feed image constraints
MIN_ASPECT_RATIO: Final[float] = 0.8   # 4:5 portrait
MAX_ASPECT_RATIO: Final[float] = 1.91  # landscape
MAX_IMAGE_BYTES: Final[int] = 8 * 1024 * 1024


# ---------- Logging ----------
logger = logging.getLogger("lambda_post_instagram")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
    logger.addHandler(_h)


# ---------- Utils (no secret leakage) ----------
def _now_iso() -> str:
    return datetime.now().isoformat()

def _ok(message: str) -> LambdaResponse:
    return {"statusCode": 200, "body": message}

def _err(message: str, code: int = 500) -> LambdaResponse:
    return {"statusCode": code, "body": message}

def _safe_len(value: Optional[str]) -> Optional[int]:
    return len(value) if isinstance(value, str) else None

def _field_lengths(data: Mapping[str, Any]) -> dict[str, Optional[int]]:
    """Only report per-field lengths; never log actual values."""
    lens: dict[str, Optional[int]] = {}
    for k, v in data.items():
        if isinstance(v, str):
            lens[k] = len(v)
        else:
            try:
                lens[k] = len(json.dumps(v))
            except Exception:
                lens[k] = None
    return lens

def _redacted_url_for_log(url: str, redact_keys: set[str] = {"access_token"}) -> tuple[str, dict[str, Optional[int]]]:
    """Return (base_url_no_query, query_field_lengths) without leaking sensitive query values."""
    parts = urlsplit(url)
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    # Replace sensitive values with their length only in log metadata
    lens: dict[str, Optional[int]] = {}
    for k, v in q.items():
        lens[k] = len(v) if isinstance(v, str) else None
    return base, lens


# ---------- HTTP helpers ----------
def _http_post(url: str, data: Mapping[str, Any], timeout: int = HTTP_TIMEOUT) -> Optional[dict[str, Any]]:
    """POST form-encoded; returns parsed JSON or None on failure. Logs only field lengths."""
    try:
        payload = parse.urlencode({k: (v if isinstance(v, str) else json.dumps(v)) for k, v in data.items()})
        payload_bytes = payload.encode("utf-8")
        logger.info(
            "%s - HTTP POST to %s with fields=%s total_bytes=%d",
            _now_iso(), url, _field_lengths(data), len(payload_bytes)
        )

        req = request.Request(url, data=payload_bytes, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with request.urlopen(req, timeout=timeout) as resp:  # type: ignore
            status = getattr(resp, "status", getattr(resp, "code", None)) or resp.getcode()
            body = resp.read()
            logger.info("%s - HTTP POST response status=%s bytes=%d", _now_iso(), status, len(body) if body else 0)
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urlerror.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        body_len = len(body or b"")
        ig_summary: dict[str, Any] = {}
        try:
            j = json.loads(body.decode("utf-8"))
            if isinstance(j, dict) and isinstance(j.get("error"), dict):
                err = j["error"]
                ig_summary = {
                    "code": err.get("code"),
                    "subcode": err.get("error_subcode"),
                    "type": err.get("type"),
                    # Log only message length
                    "message_len": len(err.get("message") or ""),
                }
        except Exception:
            pass
        logger.error(
            "%s - HTTP POST %s failed: %s body_len=%d ig_error=%s fields=%s",
            _now_iso(), url, e, body_len, ig_summary, _field_lengths(data)
        )
        return None
    except Exception as exc:
        logger.error("%s - HTTP POST failed (%s): %s | fields=%s", _now_iso(), url, exc, _field_lengths(data))
        return None

def _http_get_json(url: str, timeout: int = HTTP_TIMEOUT) -> Optional[dict[str, Any]]:
    """GET JSON helper (used for video status polling). Logs redacted query details only."""
    try:
        base, q_lens = _redacted_url_for_log(url)
        logger.info("%s - HTTP GET %s with query_fields=%s", _now_iso(), base, q_lens)

        req = request.Request(url, method="GET")
        with request.urlopen(req, timeout=timeout) as resp:  # type: ignore
            status = getattr(resp, "status", getattr(resp, "code", None)) or resp.getcode()
            body = resp.read()
            logger.info("%s - HTTP GET %s status=%s bytes=%d", _now_iso(), base, status, len(body) if body else 0)
            if not body:
                return None
            return json.loads(body.decode("utf-8"))
    except urlerror.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        base, q_lens = _redacted_url_for_log(url)
        logger.error("%s - HTTP GET %s failed: %s body_len=%d query_fields=%s", _now_iso(), base, e, len(body or b""), q_lens)
        return None
    except Exception as exc:
        base, q_lens = _redacted_url_for_log(url)
        logger.error("%s - HTTP GET %s failed: %s query_fields=%s", _now_iso(), base, exc, q_lens)
        return None


# ---------- S3 helpers ----------
def list_media_files(s3_client: S3Client, bucket: str, prefix: str = UPLOADS_FOLDER) -> Sequence[str]:
    """List keys under the given prefix in S3. Returns list of keys (may be empty)."""
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)
        keys: list[str] = []
        page_count = 0
        for page in page_iterator:
            page_count += 1
            contents = page.get("Contents") or []
            logger.debug("%s - list_media_files page #%d objects=%d", _now_iso(), page_count, len(contents))
            for obj in contents:
                key = obj.get("Key")
                if isinstance(key, str) and not key.endswith("/"):
                    keys.append(key)
        logger.info("%s - list_media_files found %d keys under '%s'", _now_iso(), len(keys), prefix)
        if keys:
            logger.debug("%s - first key: %s", _now_iso(), keys[0])
        return keys
    except (BotoCoreError, ClientError) as exc:
        logger.error("%s - list_media_files error: %s", _now_iso(), exc)
        return []

def filter_media_keys(keys: Sequence[str]) -> list[str]:
    """Keep only supported media files (images/videos)."""
    return [k for k in keys if os.path.splitext(k)[1].lower() in MEDIA_EXTS]

def find_caption_file(keys: Sequence[str], media_key: str) -> Optional[str]:
    """Find caption .txt with same basename as media."""
    base, _ = os.path.splitext(media_key)
    candidate = f"{base}.txt"
    return candidate if candidate in keys else None

def read_caption_file(s3_client: S3Client, bucket: str, key: str) -> Optional[str]:
    """Read a small text caption file from S3 and return its contents as str."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        body = resp.get("Body")
        if not isinstance(body, StreamingBody):
            body = cast(StreamingBody, body)
        raw = body.read()
        text = raw.decode("utf-8")
        logger.info("%s - read_caption_file %s length=%d bytes=%d", _now_iso(), key, len(text), len(raw) if isinstance(raw, (bytes, bytearray)) else 0)
        return text
    except (BotoCoreError, ClientError) as exc:
        logger.error("%s - read_caption_file error for %s: %s", _now_iso(), key, exc)
        return None

def move_from_uploads(s3_client: S3Client, bucket: str, key: str, dest_prefix: str) -> bool:
    """Move object from uploads/ to dest_prefix by copying and deleting the original."""
    try:
        if not key.startswith(UPLOADS_FOLDER):
            new_key = f"{dest_prefix}{os.path.basename(key)}"
        else:
            new_key = key.replace(UPLOADS_FOLDER, dest_prefix, 1)
        copy_source = {"Bucket": bucket, "Key": key}
        s3_client.copy_object(Bucket=bucket, CopySource=copy_source, Key=new_key)
        s3_client.delete_object(Bucket=bucket, Key=key)
        logger.info("%s - moved %s to %s", _now_iso(), key, new_key)
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.error("%s - move_from_uploads failed for %s -> %s: %s", _now_iso(), key, dest_prefix, exc)
        return False


def _png_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(data) - 8:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        ):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7) or marker == 0x01:
            i += 2
            continue
        if i + 4 > len(data):
            break
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + length
    return None


def image_dimensions_from_bytes(data: bytes, key: str) -> Optional[tuple[int, int]]:
    ext = os.path.splitext(key)[1].lower()
    if ext == ".png":
        return _png_dimensions(data)
    if ext in {".jpg", ".jpeg"}:
        return _jpeg_dimensions(data)
    return None


def validate_feed_image(s3_client: S3Client, bucket: str, key: str) -> tuple[bool, str, Optional[int], Optional[int]]:
    """Validate Instagram feed image constraints. Returns (ok, reason, width, height)."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        body = resp.get("Body")
        if not isinstance(body, StreamingBody):
            body = cast(StreamingBody, body)
        data = body.read()
    except (BotoCoreError, ClientError) as exc:
        logger.error("%s - validate_feed_image download failed for %s: %s", _now_iso(), key, exc)
        return False, "download failed", None, None

    size = len(data) if isinstance(data, (bytes, bytearray)) else 0
    if size <= 0:
        return False, "empty file", None, None
    if size > MAX_IMAGE_BYTES:
        return False, f"file too large ({size} bytes)", None, None

    dims = image_dimensions_from_bytes(bytes(data), key)
    if not dims:
        return False, "unable to read image dimensions", None, None

    width, height = dims
    if width <= 0 or height <= 0:
        return False, "invalid image dimensions", width, height

    ratio = width / height
    if ratio < MIN_ASPECT_RATIO or ratio > MAX_ASPECT_RATIO:
        return (
            False,
            f"aspect ratio {ratio:.4f} outside {MIN_ASPECT_RATIO}-{MAX_ASPECT_RATIO}",
            width,
            height,
        )
    return True, "ok", width, height


def reject_media_and_caption(
    s3_client: S3Client,
    bucket: str,
    media_key: str,
    caption_key: Optional[str],
    reason: str,
) -> LambdaResponse:
    """Move rejected media (and caption if present) out of uploads/."""
    logger.warning("%s - rejecting media_key=%s reason=%s", _now_iso(), media_key, reason)
    moved_media = move_from_uploads(s3_client, bucket, media_key, REJECTED_FOLDER)
    logger.info("%s - rejected_media=%s", _now_iso(), moved_media)
    if caption_key:
        moved_caption = move_from_uploads(s3_client, bucket, caption_key, REJECTED_FOLDER)
        logger.info("%s - rejected_caption=%s", _now_iso(), moved_caption)
    return _ok(f"Rejected media: {reason}")


# ---------- Secrets ----------
def get_instagram_secrets(secret_name: str = SECRET_NAME) -> tuple[Optional[str], Optional[str]]:
    """Read Instagram secrets from AWS Secrets Manager."""
    try:
        client = boto3.client("secretsmanager")
        region = client.meta.region_name
        logger.info("%s - fetching secret name=%s region=%s", _now_iso(), secret_name, region)
        result = client.get_secret_value(SecretId=secret_name)

        if "ARN" in result:
            logger.debug("%s - secret ARN suffix=%s", _now_iso(), str(result["ARN"])[-12:])

        secret_string = result.get("SecretString")
        if not isinstance(secret_string, str):
            logger.error("%s - secret %s missing SecretString", _now_iso(), secret_name)
            return None, None

        logger.info("%s - secret %s SecretString length=%d", _now_iso(), secret_name, len(secret_string))

        payload = json.loads(secret_string)
        access_token = payload.get("instagram_access_token")
        user_id = payload.get("instagram_user_id")

        # NEVER print token or ID values; only lengths
        logger.info(
            "%s - parsed instagram_access_token length=%s instagram_user_id length=%s",
            _now_iso(), _safe_len(access_token), _safe_len(user_id)
        )

        return (
            access_token if isinstance(access_token, str) else None,
            user_id if isinstance(user_id, str) else None,
        )
    except (BotoCoreError, ClientError, json.JSONDecodeError) as exc:
        logger.error("%s - failed reading secret %s: %s", _now_iso(), secret_name, exc)
        return None, None


# ---------- Instagram Graph ----------
def get_media_type_from_ext(key: str) -> MediaType:
    ext = os.path.splitext(key)[1].lower()
    if ext in VIDEO_EXTS:
        return "VIDEO"
    return "IMAGE"

def create_media_container(access_token: str, instagram_account_id: str, media_url: str, caption: str, media_type: MediaType) -> Optional[dict[str, Any]]:
    """Create a media container on the Instagram Graph API."""
    url = f"{GRAPH_API_HOST}/{GRAPH_API_VERSION}/{instagram_account_id}/media"
    payload: dict[str, Any] = {"access_token": access_token, "caption": caption}
    if media_type == "IMAGE":
        payload["image_url"] = media_url
    else:
        payload["media_type"] = "VIDEO"
        payload["video_url"] = media_url
        payload["video_codec"] = "h264" 
        payload["video_format"] = "mp4"
    logger.info(
        "%s - create_media_container type=%s account_id length=%d caption length=%d media_url length=%d token length=%d",
        _now_iso(), media_type, len(instagram_account_id), len(caption), len(media_url), len(access_token),
    )
    return _http_post(url, payload, timeout=HTTP_TIMEOUT)

def publish_media(access_token: str, instagram_account_id: str, creation_id: str) -> Optional[dict[str, Any]]:
    """Publish a media container to the Instagram account."""
    url = f"{GRAPH_API_HOST}/{GRAPH_API_VERSION}/{instagram_account_id}/media_publish"
    payload = {"access_token": access_token, "creation_id": creation_id}
    logger.info(
        "%s - publish_media account_id length=%d creation_id length=%d token length=%d",
        _now_iso(), len(instagram_account_id), len(creation_id), len(access_token),
    )
    return _http_post(url, payload, timeout=HTTP_TIMEOUT)

def poll_container_status(access_token: str, creation_id: str, max_wait_seconds: int = 180) -> bool:
    """Poll media container until FINISHED or ERROR. Returns True when FINISHED."""
    # Build URL with token in query; logs will redact query values
    base = f"{GRAPH_API_HOST}/{GRAPH_API_VERSION}/{creation_id}"
    query = parse.urlencode({"fields": "status_code", "access_token": access_token})
    url = f"{base}?{query}"

    started = time.time()
    delay = 2.0
    while True:
        data = _http_get_json(url, timeout=HTTP_TIMEOUT)
        status = None
        if isinstance(data, dict):
            status = data.get("status_code")
        logger.info("%s - poll_container_status status=%s elapsed=%.1fs", _now_iso(), status, time.time() - started)

        if status == "FINISHED":
            return True
        if status in ("ERROR", "FAILED", "EXPIRED"):
            return False

        if time.time() - started > max_wait_seconds:
            logger.error("%s - poll_container_status timed out after %ds", _now_iso(), max_wait_seconds)
            return False

        time.sleep(delay)
        # Cap delay to avoid long sleeps; simple linear backoff
        delay = min(delay + 1.0, 10.0)


# ---------- Lambda handler ----------
def lambda_handler(event: Mapping[str, Any], context: Any) -> LambdaResponse:
    """AWS Lambda entrypoint."""
    try:
        logger.info(
            "%s - invocation start BUCKET_NAME=%s AWS_REGION=%s event_keys=%s",
            _now_iso(), BUCKET_NAME, os.getenv("AWS_REGION"),
            list(event.keys()) if isinstance(event, Mapping) else type(event).__name__,
        )

        bucket = BUCKET_NAME
        if not bucket:
            logger.error("%s - BUCKET_NAME not configured", _now_iso())
            return _err("Server misconfiguration: BUCKET_NAME not set", 500)

        access_token, instagram_account_id = get_instagram_secrets()
        if not instagram_account_id or not access_token:
            logger.error("%s - Instagram config missing", _now_iso())
            return _err("Server misconfiguration: Instagram credentials missing", 500)

        s3_client: S3Client = boto3.client("s3")  # type: ignore[assignment]

        keys = list_media_files(s3_client, bucket, UPLOADS_FOLDER)
        media_candidates = filter_media_keys(keys)
        logger.info("%s - media candidates=%d of total keys=%d", _now_iso(), len(media_candidates), len(keys))
        if not media_candidates:
            logger.info("%s - no media files with extensions=%s", _now_iso(), sorted(MEDIA_EXTS))
            return _ok("No media files to process")

        # Pick first media file (preserve simple ordering behavior)
        media_key = media_candidates[0]
        logger.info("%s - selected media_key=%s", _now_iso(), media_key)

        caption_key = find_caption_file(keys, media_key)
        caption = ""
        if caption_key:
            logger.info("%s - found caption_key=%s", _now_iso(), caption_key)
            caption = read_caption_file(s3_client, bucket, caption_key) or ""
        else:
            logger.info("%s - no caption file for media_key=%s", _now_iso(), media_key)
        logger.info("%s - caption length=%d", _now_iso(), len(caption))

        media_type = get_media_type_from_ext(media_key)
        logger.info("%s - media_type=%s", _now_iso(), media_type)

        # Reject images that Instagram feed cannot publish (aspect ratio / size)
        if media_type == "IMAGE":
            ok, reason, width, height = validate_feed_image(s3_client, bucket, media_key)
            logger.info(
                "%s - image validation ok=%s reason=%s size=%sx%s",
                _now_iso(), ok, reason, width, height,
            )
            if not ok:
                return reject_media_and_caption(s3_client, bucket, media_key, caption_key, reason)

        # Generate presigned URL that Instagram Graph API reliably accepts
        s3_config = boto3.session.Session().get_component("s3")
        media_url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": media_key,
            },
            ExpiresIn=3600,
            # Instagram prefers this style
            HttpMethod="GET",
        )
        logger.info(
            "%s - generated presigned_url length=%d (first 80 chars: %s...)",
            _now_iso(), len(media_url), media_url[:80]
        )

        create_resp = create_media_container(access_token, instagram_account_id, media_url, caption, media_type)
        if not create_resp:
            logger.error("%s - create_media_container returned no response", _now_iso())
            return _err("Failed to create media container", 502)

        creation_id = create_resp.get("id")
        logger.info(
            "%s - create_media_container response keys=%s creation_id length=%s",
            _now_iso(), list(create_resp.keys()), _safe_len(creation_id if isinstance(creation_id, str) else None),
        )
        if not isinstance(creation_id, str):
            logger.error("%s - invalid creation id in response: %s", _now_iso(), create_resp)
            return _err("Invalid creation response", 502)

        # Wait until IG finishes processing the container (images can also return 9007 if published too soon)
        max_wait = 180 if media_type == "VIDEO" else 60
        if not poll_container_status(access_token, creation_id, max_wait_seconds=max_wait):
            logger.error("%s - media container did not reach FINISHED", _now_iso())
            return _err("Media container not ready for publish", 502)

        publish_resp = publish_media(access_token, instagram_account_id, creation_id)
        publish_ok = bool(publish_resp)

        if not publish_ok:
            return _err("Failed to publish media", 502)

        logger.info("%s - publish succeeded", _now_iso())

        # Move original media (and caption if present) into processed folder to avoid reprocessing
        moved_media = move_from_uploads(s3_client, bucket, media_key, PROCESSED_FOLDER)
        logger.info("%s - moved_media=%s", _now_iso(), moved_media)
        if caption_key:
            moved_caption = move_from_uploads(s3_client, bucket, caption_key, PROCESSED_FOLDER)
            logger.info("%s - moved_caption=%s", _now_iso(), moved_caption)

        logger.info("%s - invocation complete", _now_iso())
        return _ok("Media posted and moved to processed")
    except (BotoCoreError, ClientError) as exc:
        logger.error("%s - AWS error in handler: %s", _now_iso(), exc)
        return _err("Server AWS error", 500)
    except Exception:
        # Full traceback to aid debugging; does not include secret values
        logger.exception("%s - unexpected error", _now_iso())
        return _err("Unexpected server error", 500)