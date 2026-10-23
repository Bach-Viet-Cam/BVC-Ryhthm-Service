import json

import numpy as np
import pytest
import soundfile as sf

from app.config import get_settings


def service():
    from app.services.audio_quality_service import AudioQualityService
    return AudioQualityService()


def tone(amplitude=0.2, seconds=1.0, sample_rate=8000):
    return amplitude * np.sin(2 * np.pi * 440 * np.arange(int(seconds * sample_rate)) / sample_rate)


def codes(report, field="warnings"):
    return {entry["code"] for entry in report[field]}


def test_clean_tone_passes_and_preserves_samples():
    samples = tone()
    original = samples.copy()
    report = service().measure(samples, 8000)
    assert report["verdict"] == "PASS"
    assert report["audio"] == {"sampleRate": 8000, "channels": 1, "sampleCount": 8000, "durationSec": 1.0}
    assert report["metrics"]["rmsLinear"] == pytest.approx(0.2 / np.sqrt(2))
    np.testing.assert_array_equal(samples, original)
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("samples,code", [
    (np.zeros(800), "AUDIO_SILENT"),
    (np.array([]), "AUDIO_EMPTY"),
    (np.array([0.1, np.nan]), "AUDIO_NON_FINITE"),
    (np.array([np.inf]), "AUDIO_NON_FINITE"),
])
def test_unusable_content_is_rejected_without_nonstandard_json(samples, code):
    report = service().measure(samples, 8000)
    assert report["verdict"] == "REJECT"
    assert code in codes(report, "rejections")
    json.dumps(report, allow_nan=False)


def test_low_level_is_warning_instead_of_rejection():
    report = service().measure(tone(0.001), 8000)
    assert report["verdict"] == "WARN"
    assert "AUDIO_LOW_LEVEL" in codes(report)
    assert report["rejections"] == []


def test_mostly_silent_is_warning():
    samples = np.concatenate([np.zeros(7200), tone(seconds=0.1)])
    report = service().measure(samples, 8000)
    assert "AUDIO_MOSTLY_SILENT" in codes(report)
    assert report["metrics"]["silenceRatio"] == pytest.approx(0.9)


def test_clipping_is_warning_and_threshold_uses_strict_ratio():
    samples = np.full(1000, 0.1)
    samples[:10] = 1
    assert "AUDIO_POSSIBLE_CLIPPING" not in codes(service().measure(samples, 8000))
    samples[10] = 1
    report = service().measure(samples, 8000)
    assert report["verdict"] == "WARN"
    assert "AUDIO_POSSIBLE_CLIPPING" in codes(report)


def test_silence_ratio_boundary_does_not_warn_at_exactly_eighty_percent():
    report = service().measure(np.concatenate([np.zeros(6400), tone(seconds=0.2)]), 8000)
    assert report["metrics"]["silenceRatio"] == pytest.approx(0.8)
    assert "AUDIO_MOSTLY_SILENT" not in codes(report)


def test_partial_last_frame_has_sample_weight_not_equal_frame_weight():
    report = service().measure(np.concatenate([np.zeros(400), np.ones(1) * 0.1]), 8000)
    assert report["metrics"]["silenceRatio"] == pytest.approx(400 / 401)


def test_antiphase_stereo_is_not_misclassified_as_silence():
    samples = tone()
    report = service().measure(np.column_stack([samples, -samples]), 8000)
    assert report["verdict"] == "PASS"
    assert report["audio"]["channels"] == 2


@pytest.mark.asyncio
async def test_inspect_real_audio_and_reject_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "source.wav"
    sf.write(path, tone(), 8000, subtype="FLOAT")
    report = await service().inspect(path)
    assert report["verdict"] == "PASS"
    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"not audio")
    import subprocess
    from app.services import audio_quality_service as module
    def invalid_content(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg")
    monkeypatch.setattr(module.subprocess, "run", invalid_content)
    report = await service().inspect(bad)
    assert "AUDIO_DECODE_FAILED" in codes(report, "rejections")


def test_policy_snapshot_overrides_current_source_constants(monkeypatch):
    from app.constants import audio_quality
    monkeypatch.setattr(audio_quality, "LOW_LEVEL_DBFS", -20)
    saved = service().policy()
    monkeypatch.setattr(audio_quality, "LOW_LEVEL_DBFS", -80)
    report = service().measure(tone(0.01), 8000, saved)
    assert "AUDIO_LOW_LEVEL" in codes(report)
    assert report["policy"]["lowLevelDbfs"] == -20


def test_legacy_environment_thresholds_do_not_change_recording_verdict(monkeypatch):
    for name, value in {
        "LOW_LEVEL_DBFS": "-1", "SILENCE_DBFS": "-1", "SILENCE_RATIO_WARN": "0",
        "CLIP_AMPLITUDE": "0.1", "CLIP_RATIO_WARN": "0",
    }.items():
        monkeypatch.setenv("RHYTHM_SERVICE_AUDIO_QUALITY_" + name, value)
    get_settings.cache_clear()
    try:
        report = service().measure(tone(), 8000)
        assert report["verdict"] == "PASS"
        assert report["policy"]["lowLevelDbfs"] == -45
    finally:
        get_settings.cache_clear()


def test_silence_report_keeps_duration_and_json_safe_zero_energy_metrics():
    report = service().measure(np.zeros((800, 2)), 8000)
    assert report["audio"]["durationSec"] == 0.1
    assert report["metrics"]["rmsLinear"] == 0
    assert report["metrics"]["rmsDbfs"] is None
    assert report["metrics"]["silenceRatio"] == 1
    json.dumps(report, allow_nan=False)


def test_vietnamese_quality_message_keeps_accents():
    report = service().measure(np.zeros(800), 8000)
    assert report["rejections"][0]["message"] == "Bản thu hoàn toàn im lặng. Vui lòng thu âm lại."


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code", [("timeout", "AUDIO_DECODE_TIMEOUT"), ("missing_decoder", "AUDIO_DECODER_UNAVAILABLE")])
async def test_decoder_infrastructure_errors_are_not_content_rejection(tmp_path, monkeypatch, failure, code):
    import subprocess
    from app.services import audio_quality_service as module
    path = tmp_path / "source.m4a"
    path.write_bytes(b"force fallback decoder")
    def unavailable(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ffmpeg", 60)
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(module.subprocess, "run", unavailable)
    with pytest.raises(RuntimeError) as raised:
        await service().inspect(path)
    assert raised.value.code == code
