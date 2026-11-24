import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.audio import downloader
from app.audio.downloader import AudioDownloadError, _validate_url, download_audio
from app.services.audio_service import AudioService


def run(coro):
    return asyncio.run(coro)


def make_settings(allowed=None, max_bytes=1024, timeout=7.0):
    return SimpleNamespace(
        allowed_host_values=set(allowed or []),
        max_file_bytes=max_bytes,
        download_timeout_seconds=timeout,
    )


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(b"abc",), raise_in_stream=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self._raise = raise_in_stream

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c
        if self._raise:
            raise self._raise


class FakeStream:
    def __init__(self, response, enter_error=None):
        self.response = response
        self.enter_error = enter_error

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        return self.response

    async def __aexit__(self, *a):
        return False


def install_client(monkeypatch, response=None, enter_error=None, captured=None):
    class FakeClient:
        def __init__(self, **kwargs):
            if captured is not None:
                captured["init"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url):
            if captured is not None:
                captured["call"] = (method, url)
            return FakeStream(response, enter_error)

    monkeypatch.setattr(downloader.httpx, "AsyncClient", FakeClient)


@pytest.fixture
def settings(monkeypatch):
    s = make_settings()
    monkeypatch.setattr(downloader, "get_settings", lambda: s)
    return s


# ---- _validate_url ----
def test_utc_rad_001_validate_https_ok(settings):
    assert _validate_url("https://cdn.example.com/a.mp3") is None


def test_utc_rad_002_validate_http_ok(settings):
    assert _validate_url("http://cdn.example.com/a.mp3") is None


@pytest.mark.parametrize("url", ["ftp://example.com/a.mp3", "file:///etc/passwd", "javascript:alert(1)"])
def test_utc_rad_003_validate_bad_scheme(settings, url):
    with pytest.raises(AudioDownloadError) as e:
        _validate_url(url)
    assert e.value.code == "INVALID_AUDIO_URL"
    assert e.value.retryable is False
    assert str(e.value) == "Audio URL phải dùng http hoặc https"


def test_utc_rad_004_validate_empty_url(settings):
    with pytest.raises(AudioDownloadError) as e:
        _validate_url("")
    assert e.value.code == "INVALID_AUDIO_URL"


def test_utc_rad_005_validate_missing_hostname(settings):
    with pytest.raises(AudioDownloadError) as e:
        _validate_url("https:///a.mp3")
    assert e.value.code == "INVALID_AUDIO_URL"


def test_utc_rad_006_validate_host_in_allowlist_case_insensitive(monkeypatch):
    monkeypatch.setattr(downloader, "get_settings", lambda: make_settings(allowed={"cdn.example.com"}))
    assert _validate_url("https://CDN.Example.com/a.mp3") is None


def test_utc_rad_007_validate_host_not_in_allowlist(monkeypatch):
    monkeypatch.setattr(downloader, "get_settings", lambda: make_settings(allowed={"cdn.example.com"}))
    with pytest.raises(AudioDownloadError) as e:
        _validate_url("https://evil.com/a.mp3")
    assert e.value.code == "AUDIO_HOST_NOT_ALLOWED"
    assert str(e.value) == "Host audio không nằm trong allowlist"


def test_utc_rad_008_validate_private_host_allowed_when_no_allowlist(settings):
    """Source has no private-IP blocking: empty allowlist accepts any host."""
    assert _validate_url("http://127.0.0.1:8080/a.mp3") is None


# ---- download_audio ----
def test_utc_rad_009_download_success_writes_file(settings, monkeypatch, tmp_path):
    cap = {}
    install_client(monkeypatch, FakeResponse(200, {"content-length": "6"}, [b"abc", b"def"]), captured=cap)
    path = run(download_audio("https://x.com/a.mp3", tmp_path / "sub"))
    assert path.read_bytes() == b"abcdef"
    assert path.parent == tmp_path / "sub"
    assert cap["call"] == ("GET", "https://x.com/a.mp3")


def test_utc_rad_010_temp_file_naming_uuid_audio_unique(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, FakeResponse(200))
    p1 = run(download_audio("https://x.com/a.mp3", tmp_path))
    p2 = run(download_audio("https://x.com/a.mp3", tmp_path))
    assert p1 != p2
    assert p1.suffix == ".audio" and len(p1.stem) == 32
    int(p1.stem, 16)


def test_utc_rad_011_client_config_no_redirect_and_timeout(settings, monkeypatch, tmp_path):
    cap = {}
    install_client(monkeypatch, FakeResponse(200), captured=cap)
    run(download_audio("https://x.com/a.mp3", tmp_path))
    assert cap["init"] == {"follow_redirects": False, "timeout": 7.0}


def test_utc_rad_012_invalid_url_no_directory_created(settings, tmp_path):
    target = tmp_path / "never"
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("ftp://x.com/a.mp3", target))
    assert e.value.code == "INVALID_AUDIO_URL"
    assert not target.exists()


@pytest.mark.parametrize("status,retryable", [(500, True), (503, True), (429, True), (404, False), (403, False), (400, False)])
def test_utc_rad_013_http_error_retryable_flag(settings, monkeypatch, tmp_path, status, retryable):
    install_client(monkeypatch, FakeResponse(status))
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("https://x.com/a.mp3", tmp_path))
    assert e.value.code == "AUDIO_DOWNLOAD_FAILED"
    assert e.value.retryable is retryable
    assert str(e.value) == f"Storage trả về HTTP {status}"
    assert list(tmp_path.iterdir()) == []


def test_utc_rad_014_redirect_status_rejected_not_retryable(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, FakeResponse(302))
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("https://x.com/a.mp3", tmp_path))
    assert e.value.code == "AUDIO_DOWNLOAD_FAILED"
    assert e.value.retryable is False


def test_utc_rad_015_boundary_status_199_and_300_rejected(settings, monkeypatch, tmp_path):
    for status in (199, 300):
        install_client(monkeypatch, FakeResponse(status))
        with pytest.raises(AudioDownloadError):
            run(download_audio("https://x.com/a.mp3", tmp_path))


def test_utc_rad_016_content_length_exceeds_limit(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, FakeResponse(200, {"content-length": "1025"}))
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("https://x.com/a.mp3", tmp_path))
    assert e.value.code == "AUDIO_TOO_LARGE"
    assert e.value.retryable is False
    assert list(tmp_path.iterdir()) == []


def test_utc_rad_017_content_length_equal_limit_ok(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, FakeResponse(200, {"content-length": "1024"}, [b"x" * 1024]))
    path = run(download_audio("https://x.com/a.mp3", tmp_path))
    assert path.stat().st_size == 1024


def test_utc_rad_018_streamed_bytes_exceed_limit_without_header(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, FakeResponse(200, {}, [b"x" * 600, b"x" * 600]))
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("https://x.com/a.mp3", tmp_path))
    assert e.value.code == "AUDIO_TOO_LARGE"
    assert list(tmp_path.iterdir()) == []


def test_utc_rad_019_streamed_bytes_exactly_limit_ok(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, FakeResponse(200, {}, [b"x" * 512, b"x" * 512]))
    assert run(download_audio("https://x.com/a.mp3", tmp_path)).stat().st_size == 1024


def test_utc_rad_020_content_type_not_checked(settings, monkeypatch, tmp_path):
    """Source performs no content-type validation: text/html body is accepted."""
    install_client(monkeypatch, FakeResponse(200, {"content-type": "text/html"}, [b"<html>"]))
    assert run(download_audio("https://x.com/a.mp3", tmp_path)).read_bytes() == b"<html>"


def test_utc_rad_021_timeout_is_retryable(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, enter_error=httpx.ReadTimeout("slow"))
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("https://x.com/a.mp3", tmp_path))
    assert e.value.code == "AUDIO_DOWNLOAD_FAILED"
    assert e.value.retryable is True
    assert str(e.value) == "Không thể tải file audio"
    assert isinstance(e.value.__cause__, httpx.ReadTimeout)


def test_utc_rad_022_connect_error_retryable(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, enter_error=httpx.ConnectError("down"))
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("https://x.com/a.mp3", tmp_path))
    assert e.value.retryable is True


def test_utc_rad_023_mid_stream_error_cleans_partial_file(settings, monkeypatch, tmp_path):
    install_client(monkeypatch, FakeResponse(200, {}, [b"abc"], raise_in_stream=httpx.ReadError("cut")))
    with pytest.raises(AudioDownloadError) as e:
        run(download_audio("https://x.com/a.mp3", tmp_path))
    assert e.value.retryable is True
    assert list(tmp_path.iterdir()) == []


def test_utc_rad_024_audio_download_error_defaults():
    err = AudioDownloadError("CODE", "msg")
    assert (err.code, err.retryable, str(err)) == ("CODE", False, "msg")


# ---- AudioService.download ----
def test_utc_rad_025_service_delegates_to_download_audio(monkeypatch, tmp_path):
    seen = {}

    async def fake(url, target):
        seen["args"] = (url, target)
        return tmp_path / "f.audio"

    monkeypatch.setattr("app.services.audio_service.download_audio", fake)
    result = run(AudioService().download("https://x.com/a.mp3", tmp_path))
    assert result == tmp_path / "f.audio"
    assert seen["args"] == ("https://x.com/a.mp3", tmp_path)


def test_utc_rad_026_service_propagates_error(settings, tmp_path):
    with pytest.raises(AudioDownloadError) as e:
        run(AudioService().download("", tmp_path))
    assert e.value.code == "INVALID_AUDIO_URL"
