import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.constants import audio_quality as consts
from app.services import audio_quality_service as module
from app.services.audio_quality_service import (
    AudioProcessingError,
    AudioQualityError,
    AudioQualityService,
)

SR = 8000


def svc():
    return AudioQualityService()


def tone(amplitude=0.2, seconds=1.0, sample_rate=SR):
    return amplitude * np.sin(2 * np.pi * 440 * np.arange(int(seconds * sample_rate)) / sample_rate)


def codes(report, field="warnings"):
    return [entry["code"] for entry in report[field]]


# ---------------- policy ----------------

def test_utc_raq_001_policy_returns_constants_snapshot():
    assert svc().policy() == {
        "policyVersion": consts.POLICY_VERSION,
        "lowLevelDbfs": -45.0,
        "silenceDbfs": -60.0,
        "silenceRatioWarn": 0.80,
        "clipAmplitude": 0.999,
        "clipRatioWarn": 0.01,
        "frameDurationMs": 50,
    }


def test_utc_raq_002_policy_returns_fresh_dict_each_call():
    first = svc().policy()
    first["lowLevelDbfs"] = 0
    assert svc().policy()["lowLevelDbfs"] == consts.LOW_LEVEL_DBFS


# ---------------- measure ----------------

def test_utc_raq_003_measure_clean_sine_passes():
    samples = tone()
    original = samples.copy()
    report = svc().measure(samples, SR)
    assert report["verdict"] == "PASS"
    assert report["warnings"] == [] and report["rejections"] == []
    assert report["audio"] == {"sampleRate": SR, "channels": 1, "sampleCount": SR, "durationSec": 1.0}
    assert report["metrics"]["rmsLinear"] == pytest.approx(0.2 / np.sqrt(2))
    assert report["metrics"]["peakAmplitude"] == pytest.approx(0.2)
    assert report["metrics"]["silenceRatio"] == 0
    assert report["metrics"]["clippingRatio"] == 0
    assert report["policyVersion"] == consts.POLICY_VERSION
    np.testing.assert_array_equal(samples, original)
    json.dumps(report, allow_nan=False)


def test_utc_raq_004_measure_empty_array_rejected():
    report = svc().measure(np.array([]), SR)
    assert report["verdict"] == "REJECT"
    assert codes(report, "rejections") == ["AUDIO_EMPTY"]
    assert report["rejections"][0]["message"] == "File không có mẫu âm thanh."
    assert report["audio"] is None and report["metrics"] is None


def test_utc_raq_005_measure_all_zero_rejected_silent_with_metrics():
    report = svc().measure(np.zeros((800, 2)), SR)
    assert report["verdict"] == "REJECT"
    assert codes(report, "rejections") == ["AUDIO_SILENT"]
    assert report["audio"] == {"sampleRate": SR, "channels": 2, "sampleCount": 800, "durationSec": 0.1}
    assert report["metrics"] == {"rmsLinear": 0.0, "rmsDbfs": None, "peakAmplitude": 0.0,
                                 "silenceRatio": 1.0, "clippingRatio": 0.0}
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_utc_raq_006_measure_non_finite_rejected(bad):
    report = svc().measure(np.array([0.1, bad, 0.2]), SR)
    assert report["verdict"] == "REJECT"
    assert codes(report, "rejections") == ["AUDIO_NON_FINITE"]
    assert report["rejections"][0]["message"] == "File chứa dữ liệu âm thanh không hợp lệ."


@pytest.mark.parametrize("rate", [0, -8000])
def test_utc_raq_007_measure_non_positive_sample_rate_rejected(rate):
    report = svc().measure(tone(), rate)
    assert codes(report, "rejections") == ["AUDIO_DECODE_FAILED"]
    assert report["rejections"][0]["message"] == "Thông tin audio không hợp lệ."


def test_utc_raq_008_measure_three_dimensional_input_rejected():
    report = svc().measure(np.ones((10, 2, 2)) * 0.1, SR)
    assert codes(report, "rejections") == ["AUDIO_DECODE_FAILED"]


def test_utc_raq_009_measure_mono_1d_and_stereo_channel_counts():
    mono = svc().measure(tone(), SR)
    stereo = svc().measure(np.column_stack([tone(), -tone()]), SR)
    assert mono["audio"]["channels"] == 1
    assert stereo["audio"]["channels"] == 2
    assert stereo["verdict"] == "PASS"
    assert stereo["audio"]["sampleCount"] == SR


def test_utc_raq_010_low_level_boundary_around_minus_45_dbfs():
    below = svc().measure(np.full(SR, 0.0056), SR)  # about -45.04 dBFS
    above = svc().measure(np.full(SR, 0.0057), SR)  # about -44.88 dBFS
    assert "AUDIO_LOW_LEVEL" in codes(below)
    assert below["verdict"] == "WARN"
    assert below["metrics"]["rmsDbfs"] < consts.LOW_LEVEL_DBFS
    assert "AUDIO_LOW_LEVEL" not in codes(above)
    assert above["metrics"]["rmsDbfs"] > consts.LOW_LEVEL_DBFS


def test_utc_raq_011_low_level_is_strict_less_than_with_custom_policy():
    samples = np.full(SR, 0.1)  # exactly -20 dBFS
    policy = svc().policy()
    level = svc().measure(samples, SR, policy)["metrics"]["rmsDbfs"]
    policy["lowLevelDbfs"] = level
    assert "AUDIO_LOW_LEVEL" not in codes(svc().measure(samples, SR, policy))
    policy["lowLevelDbfs"] = level + 0.01
    assert "AUDIO_LOW_LEVEL" in codes(svc().measure(samples, SR, policy))


def test_utc_raq_012_clipping_ratio_exactly_one_percent_does_not_warn():
    samples = np.full(1000, 0.1)
    samples[:10] = 1.0
    report = svc().measure(samples, SR)
    assert report["metrics"]["clippingRatio"] == pytest.approx(0.01)
    assert "AUDIO_POSSIBLE_CLIPPING" not in codes(report)
    assert report["verdict"] == "PASS"


def test_utc_raq_013_clipping_above_one_percent_warns():
    samples = np.full(1000, 0.1)
    samples[:11] = 1.0
    report = svc().measure(samples, SR)
    assert report["verdict"] == "WARN"
    assert codes(report) == ["AUDIO_POSSIBLE_CLIPPING"]
    assert report["warnings"][0]["message"] == "Bản thu có dấu hiệu clipping, hãy giảm mức thu âm."


def test_utc_raq_014_clip_amplitude_threshold_is_inclusive_at_0_999():
    at_threshold = np.full(1000, 0.1)
    at_threshold[:11] = 0.999
    below = np.full(1000, 0.1)
    below[:11] = 0.998
    assert "AUDIO_POSSIBLE_CLIPPING" in codes(svc().measure(at_threshold, SR))
    assert "AUDIO_POSSIBLE_CLIPPING" not in codes(svc().measure(below, SR))


def test_utc_raq_015_clipping_counts_negative_peaks():
    samples = np.full(1000, 0.1)
    samples[:11] = -1.0
    assert "AUDIO_POSSIBLE_CLIPPING" in codes(svc().measure(samples, SR))


def test_utc_raq_016_silence_ratio_exactly_eighty_percent_does_not_warn():
    samples = np.concatenate([np.zeros(6400), tone(seconds=0.2)])  # 16 of 20 frames silent
    report = svc().measure(samples, SR)
    assert report["metrics"]["silenceRatio"] == pytest.approx(0.8)
    assert "AUDIO_MOSTLY_SILENT" not in codes(report)


def test_utc_raq_017_silence_ratio_above_eighty_percent_warns():
    samples = np.concatenate([np.zeros(6800), tone(seconds=0.15)])  # 17 of 20 frames silent
    report = svc().measure(samples, SR)
    assert report["metrics"]["silenceRatio"] == pytest.approx(0.85)
    assert report["verdict"] == "WARN"
    assert codes(report) == ["AUDIO_MOSTLY_SILENT"]
    assert report["warnings"][0]["message"] == "Bản thu có nhiều khoảng lặng."


def test_utc_raq_018_multiple_warnings_listed_in_check_order():
    samples = np.concatenate([np.zeros(7200), np.ones(800)])  # 90% silent, 10% clipped
    report = svc().measure(samples, SR)
    assert report["verdict"] == "WARN"
    assert codes(report) == ["AUDIO_MOSTLY_SILENT", "AUDIO_POSSIBLE_CLIPPING"]
    assert report["rejections"] == []


def test_utc_raq_019_custom_policy_is_used_and_copied_into_report():
    policy = svc().policy()
    policy["lowLevelDbfs"] = -10.0
    policy["policyVersion"] = "custom-v9"
    report = svc().measure(tone(0.2), SR, policy)  # about -17 dBFS
    assert "AUDIO_LOW_LEVEL" in codes(report)
    assert report["policyVersion"] == "custom-v9"
    assert report["policy"] == policy
    assert report["policy"] is not policy


def test_utc_raq_020_very_short_signal_single_partial_frame():
    report = svc().measure(np.array([0.5, -0.5, 0.5]), SR)
    assert report["audio"]["sampleCount"] == 3
    assert report["audio"]["durationSec"] == pytest.approx(3 / SR)
    assert report["metrics"]["silenceRatio"] == 0
    assert report["verdict"] == "PASS"
    assert report["metrics"]["rmsLinear"] == pytest.approx(0.5)


# ---------------- _silence_ratio ----------------

def test_utc_raq_021_silence_ratio_frame_rms_threshold_minus_60_dbfs():
    quiet = np.full((400, 1), 0.0009)   # below 0.001
    louder = np.full((400, 1), 0.0011)  # above 0.001
    assert AudioQualityService._silence_ratio(quiet, SR, svc().policy()) == 1.0
    assert AudioQualityService._silence_ratio(louder, SR, svc().policy()) == 0.0


def test_utc_raq_022_silence_ratio_weights_partial_last_frame_by_samples():
    signal = np.concatenate([np.zeros(400), [0.1]])[:, None]
    assert AudioQualityService._silence_ratio(signal, SR, svc().policy()) == pytest.approx(400 / 401)


def test_utc_raq_023_silence_ratio_requires_all_channels_silent():
    signal = np.column_stack([np.zeros(800), tone(0.2, 0.1)])
    assert AudioQualityService._silence_ratio(signal, SR, svc().policy()) == 0.0


def test_utc_raq_024_silence_ratio_tiny_sample_rate_uses_frame_size_of_one():
    signal = np.array([0.0, 0.0, 0.5, 0.0])[:, None]
    assert AudioQualityService._silence_ratio(signal, 10, svc().policy()) == pytest.approx(0.75)


# ---------------- _reject ----------------

def test_utc_raq_025_reject_builds_report_structure():
    policy = svc().policy()
    report = AudioQualityService._reject(policy, "X_CODE", "msg")
    assert report == {
        "policyVersion": consts.POLICY_VERSION, "policy": policy, "verdict": "REJECT",
        "warnings": [], "rejections": [{"code": "X_CODE", "message": "msg"}],
        "audio": None, "metrics": None,
    }


# ---------------- ensure_acceptable / errors ----------------

def test_utc_raq_026_ensure_acceptable_raises_quality_error_on_reject():
    report = svc().measure(np.zeros(800), SR)
    with pytest.raises(AudioQualityError) as raised:
        AudioQualityService.ensure_acceptable(report)
    assert raised.value.code == "AUDIO_SILENT"
    assert raised.value.report is report
    assert str(raised.value) == "Bản thu hoàn toàn im lặng. Vui lòng thu âm lại."
    assert isinstance(raised.value, ValueError)


@pytest.mark.parametrize("samples", [tone(), tone(0.001)])
def test_utc_raq_027_ensure_acceptable_allows_pass_and_warn(samples):
    report = svc().measure(samples, SR)
    assert report["verdict"] in ("PASS", "WARN")
    assert AudioQualityService.ensure_acceptable(report) is None


def test_utc_raq_028_audio_processing_error_carries_code_and_message():
    error = AudioProcessingError("AUDIO_DECODE_TIMEOUT", "timeout msg")
    assert error.code == "AUDIO_DECODE_TIMEOUT"
    assert str(error) == "timeout msg"
    assert isinstance(error, RuntimeError)


# ---------------- _decode ----------------

def test_utc_raq_029_decode_stereo_wav_returns_2d_float64_and_rate(tmp_path):
    path = tmp_path / "s.wav"
    sf.write(path, np.column_stack([tone(0.3), tone(0.1)]), SR, subtype="FLOAT")
    samples, rate = AudioQualityService._decode(path)
    assert rate == SR
    assert samples.dtype == np.float64
    assert samples.shape == (SR, 2)


def test_utc_raq_030_decode_falls_back_to_ffmpeg_without_resampling(tmp_path, monkeypatch):
    path = tmp_path / "bad.m4a"
    path.write_bytes(b"not decodable by libsndfile")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        sf.write(cmd[-1], tone(0.2, 0.1), SR, subtype="FLOAT")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    samples, rate = AudioQualityService._decode(path)
    assert rate == SR and samples.shape == (800, 1)
    cmd, kwargs = calls[0]
    assert cmd[0] == "ffmpeg" and "-vn" in cmd and str(path) in cmd
    assert "-ar" not in cmd and "-ac" not in cmd
    assert kwargs["check"] is True and kwargs["timeout"] == 60


def test_utc_raq_031_decode_ffmpeg_missing_raises_decoder_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "bad.m4a"
    path.write_bytes(b"junk")

    def missing(*a, **k):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(module.subprocess, "run", missing)
    with pytest.raises(AudioProcessingError) as raised:
        AudioQualityService._decode(path)
    assert raised.value.code == "AUDIO_DECODER_UNAVAILABLE"
    assert str(raised.value) == "Bộ giải mã audio chưa sẵn sàng. Vui lòng kiểm tra cấu hình dịch vụ."


def test_utc_raq_032_decode_ffmpeg_timeout_raises_decode_timeout(tmp_path, monkeypatch):
    path = tmp_path / "bad.m4a"
    path.write_bytes(b"junk")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("ffmpeg", 60)

    monkeypatch.setattr(module.subprocess, "run", slow)
    with pytest.raises(AudioProcessingError) as raised:
        AudioQualityService._decode(path)
    assert raised.value.code == "AUDIO_DECODE_TIMEOUT"
    assert str(raised.value) == "Quá thời gian giải mã audio. Vui lòng thử lại sau."


# ---------------- _inspect / inspect ----------------

def test_utc_raq_033_inspect_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="Không tìm thấy file audio đã tải."):
        svc()._inspect(tmp_path / "nope.wav", svc().policy())


def test_utc_raq_034_inspect_directory_path_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        svc()._inspect(tmp_path, svc().policy())


@pytest.mark.asyncio
async def test_utc_raq_035_inspect_real_wav_passes(tmp_path):
    path = tmp_path / "ok.wav"
    sf.write(path, tone(), SR, subtype="FLOAT")
    report = await svc().inspect(path)
    assert report["verdict"] == "PASS"
    assert report["audio"]["sampleRate"] == SR
    assert report["audio"]["sampleCount"] == SR
    assert report["policy"] == svc().policy()


@pytest.mark.asyncio
async def test_utc_raq_036_inspect_uses_supplied_policy(tmp_path):
    path = tmp_path / "ok.wav"
    sf.write(path, tone(0.2), SR, subtype="FLOAT")
    policy = svc().policy()
    policy["lowLevelDbfs"] = -10.0
    report = await svc().inspect(path, policy)
    assert "AUDIO_LOW_LEVEL" in codes(report)
    assert report["policy"]["lowLevelDbfs"] == -10.0


@pytest.mark.asyncio
async def test_utc_raq_037_inspect_silent_wav_rejected(tmp_path):
    path = tmp_path / "silent.wav"
    sf.write(path, np.zeros(SR), SR, subtype="FLOAT")
    report = await svc().inspect(path)
    assert report["verdict"] == "REJECT"
    assert codes(report, "rejections") == ["AUDIO_SILENT"]


@pytest.mark.asyncio
async def test_utc_raq_038_inspect_zero_frame_wav_rejected_empty(tmp_path):
    path = tmp_path / "empty.wav"
    sf.write(path, np.zeros(0), SR, subtype="FLOAT")
    report = await svc().inspect(path)
    assert codes(report, "rejections") == ["AUDIO_EMPTY"]


@pytest.mark.asyncio
async def test_utc_raq_039_inspect_corrupt_file_rejected_decode_failed(tmp_path, monkeypatch):
    path = tmp_path / "broken.wav"
    path.write_bytes(b"not audio")

    def invalid(*a, **k):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(module.subprocess, "run", invalid)
    report = await svc().inspect(path)
    assert report["verdict"] == "REJECT"
    assert codes(report, "rejections") == ["AUDIO_DECODE_FAILED"]
    assert report["rejections"][0]["message"] == "Không thể giải mã file audio. Vui lòng kiểm tra bản thu."
    assert report["audio"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [("timeout", "AUDIO_DECODE_TIMEOUT"), ("missing", "AUDIO_DECODER_UNAVAILABLE")])
async def test_utc_raq_040_inspect_decoder_infrastructure_errors_propagate(tmp_path, monkeypatch, failure, code):
    path = tmp_path / "x.m4a"
    path.write_bytes(b"junk")

    def boom(*a, **k):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ffmpeg", 60)
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(module.subprocess, "run", boom)
    with pytest.raises(AudioProcessingError) as raised:
        await svc().inspect(path)
    assert raised.value.code == code
