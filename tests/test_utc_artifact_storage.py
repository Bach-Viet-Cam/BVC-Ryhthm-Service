import hashlib
import json
from types import SimpleNamespace

import pytest

from app.services.artifact_storage_service import ArtifactStorageService, ArtifactUnavailableError


def _task():
    return SimpleNamespace(sheetMusicId="sheet-1", mediaFileId="media-1", analysisId="analysis-1")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def svc(tmp_path):
    return ArtifactStorageService(tmp_path)


def test_utc_ras_001_init_resolves_root(tmp_path):
    s = ArtifactStorageService(str(tmp_path / "a" / ".." / "b"))
    assert s.root == (tmp_path / "b").resolve()


def test_utc_ras_002_write_key_layout(svc, tmp_path):
    meta = svc.write(_task(), {"a": 1})
    assert meta["key"] == "template-analyses/sheet-1/media-1/analysis-1/result.json"
    assert (tmp_path / "template-analyses/sheet-1/media-1/analysis-1/result.json").is_file()


def test_utc_ras_003_write_json_and_checksum(svc, tmp_path):
    doc = {"name": "Việt", "n": [1, 2]}
    meta = svc.write(_task(), doc)
    raw = (tmp_path / meta["key"]).read_bytes()
    assert raw == json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert meta["sha256"] == _sha(raw)
    assert meta["sizeBytes"] == len(raw)


def test_utc_ras_004_write_overwrite_existing(svc, tmp_path):
    svc.write(_task(), {"v": 1})
    meta = svc.write(_task(), {"v": 2})
    assert json.loads((tmp_path / meta["key"]).read_bytes()) == {"v": 2}


def test_utc_ras_005_write_leaves_no_temp_files(svc, tmp_path):
    meta = svc.write(_task(), {"v": 1})
    files = [p.name for p in (tmp_path / meta["key"]).parent.iterdir()]
    assert files == ["result.json"]


def test_utc_ras_006_write_evaluation_key_layout(svc, tmp_path):
    meta = svc.write_evaluation("eval_1-A", 1, {"ok": True})
    assert meta["key"] == "evaluations/eval_1-A/attempt-1/analysis.json"
    assert (tmp_path / meta["key"]).is_file()


def test_utc_ras_007_write_evaluation_attempt_numbering(svc, tmp_path):
    m1 = svc.write_evaluation("e1", 1, {"a": 1})
    m2 = svc.write_evaluation("e1", 2, {"a": 2})
    assert m1["key"] != m2["key"]
    assert m2["key"].endswith("attempt-2/analysis.json")
    assert json.loads((tmp_path / m1["key"]).read_bytes()) == {"a": 1}
    assert json.loads((tmp_path / m2["key"]).read_bytes()) == {"a": 2}


def test_utc_ras_008_write_evaluation_overwrite_same_attempt(svc, tmp_path):
    svc.write_evaluation("e1", 1, {"a": 1})
    meta = svc.write_evaluation("e1", 1, {"a": 9})
    assert json.loads((tmp_path / meta["key"]).read_bytes()) == {"a": 9}


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a b", "a" + chr(92) + "b", "x" * 129, "é"])
def test_utc_ras_009_write_evaluation_invalid_id(svc, bad):
    with pytest.raises(ArtifactUnavailableError, match="Mã đánh giá không hợp lệ"):
        svc.write_evaluation(bad, 1, {})


def test_utc_ras_010_write_evaluation_id_max_length(svc, monkeypatch):
    # _write is stubbed: a 128-char path exceeds the Windows path limit under the pytest tmp dir
    monkeypatch.setattr(svc, "_write", lambda key, doc: {"key": key})
    assert svc.write_evaluation("x" * 128, 1, {})["key"] == "evaluations/" + "x" * 128 + "/attempt-1/analysis.json"


def test_utc_ras_011_write_rejects_nan(svc, tmp_path):
    with pytest.raises(ValueError):
        svc.write_evaluation("e1", 1, {"v": float("nan")})
    assert not (tmp_path / "evaluations/e1/attempt-1/analysis.json").exists()


def test_utc_ras_012_write_rejects_infinity(svc):
    with pytest.raises(ValueError):
        svc.write(_task(), {"v": float("inf")})


def test_utc_ras_013_read_correct_checksum(svc):
    meta = svc.write(_task(), {"a": 1})
    assert svc.read(meta["key"], meta["sha256"]) == b'{"a":1}'


def test_utc_ras_014_read_wrong_checksum(svc):
    meta = svc.write(_task(), {"a": 1})
    with pytest.raises(ArtifactUnavailableError, match="Chưa thể đọc file kết quả phân tích"):
        svc.read(meta["key"], "0" * 64)


def test_utc_ras_015_read_missing_file(svc):
    with pytest.raises(ArtifactUnavailableError) as exc:
        svc.read("evaluations/none/attempt-1/analysis.json", "0" * 64)
    assert exc.value.code == "ARTIFACT_UNAVAILABLE"
    assert isinstance(exc.value, OSError)


@pytest.mark.parametrize("key", ["../outside.json", "a/../../b.json", "/etc/passwd", "a/./b.json", "a" + chr(92) + "b.json", ""])
def test_utc_ras_016_resolve_rejects_unsafe_keys(svc, key):
    with pytest.raises(ArtifactUnavailableError, match="không hợp lệ"):
        svc._resolve(key)


def test_utc_ras_017_read_traversal_key_is_unavailable(svc, tmp_path):
    (tmp_path.parent / "secret.json").write_text("{}")
    with pytest.raises(ArtifactUnavailableError, match="Chưa thể đọc"):
        svc.read("../secret.json", _sha(b"{}"))


def test_utc_ras_018_resolve_outside_via_symlink(svc, tmp_path):
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink not permitted")
    with pytest.raises(ArtifactUnavailableError, match="nằm ngoài thư mục lưu trữ"):
        svc._resolve("link/file.json")


def test_utc_ras_019_read_nan_constant_rejected(svc, tmp_path):
    (tmp_path / "bad.json").write_bytes(b'{"v":NaN}')
    with pytest.raises(ArtifactUnavailableError, match="Chưa thể đọc"):
        svc.read("bad.json", _sha(b'{"v":NaN}'))


def test_utc_ras_020_read_invalid_json_and_bad_utf8(svc, tmp_path):
    (tmp_path / "bad.json").write_bytes(b"{not json")
    with pytest.raises(ArtifactUnavailableError):
        svc.read("bad.json", _sha(b"{not json"))
    (tmp_path / "u.json").write_bytes(b"\xff\xfe")
    with pytest.raises(ArtifactUnavailableError):
        svc.read("u.json", _sha(b"\xff\xfe"))


def test_utc_ras_021_invalid_constant_raises():
    with pytest.raises(ValueError, match="giá trị số không hợp lệ"):
        ArtifactStorageService._invalid_constant("Infinity")
