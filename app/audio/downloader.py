import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from app.config import get_settings


class AudioDownloadError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _is_private_host(hostname: str) -> bool:
    try:
        addresses = {result[4][0] for result in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as error:
        raise AudioDownloadError("AUDIO_HOST_NOT_FOUND", "Không phân giải được host audio") from error

    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved:
            return True
    return False


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AudioDownloadError("INVALID_AUDIO_URL", "Audio URL phải dùng http hoặc https")

    settings = get_settings()
    hostname = parsed.hostname.lower()
    if settings.allowed_host_values and hostname not in settings.allowed_host_values:
        raise AudioDownloadError("AUDIO_HOST_NOT_ALLOWED", "Host audio không nằm trong allowlist")
    if _is_private_host(hostname):
        raise AudioDownloadError("PRIVATE_AUDIO_URL", "Không cho phép truy cập địa chỉ mạng nội bộ")


async def download_audio(url: str, target_directory: Path) -> Path:
    _validate_url(url)
    settings = get_settings()
    target_directory.mkdir(parents=True, exist_ok=True)
    target_path = target_directory / f"{uuid4().hex}.audio"

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=settings.download_timeout_seconds) as client:
            async with client.stream("GET", url) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    is_retryable = response.status_code >= 500 or response.status_code == 429
                    raise AudioDownloadError(
                        "AUDIO_DOWNLOAD_FAILED",
                        f"Storage trả về HTTP {response.status_code}",
                        retryable=is_retryable,
                    )
                content_length = int(response.headers.get("content-length", "0"))
                if content_length > settings.max_file_bytes:
                    raise AudioDownloadError("AUDIO_TOO_LARGE", "File audio vượt quá giới hạn kích thước")

                total_bytes = 0
                with target_path.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > settings.max_file_bytes:
                            raise AudioDownloadError("AUDIO_TOO_LARGE", "File audio vượt quá giới hạn kích thước")
                        output.write(chunk)
    except AudioDownloadError:
        target_path.unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, OSError) as error:
        target_path.unlink(missing_ok=True)
        raise AudioDownloadError("AUDIO_DOWNLOAD_FAILED", "Không thể tải file audio", retryable=True) from error

    return target_path


async def resolve_host_safely(hostname: str) -> None:
    await asyncio.to_thread(_is_private_host, hostname)
