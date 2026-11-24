import asyncio
import math
from pathlib import Path

import numpy as np
import pytest

from app.evaluation import evaluator as ev
from app.evaluation import notes
from app.schemas.instrument import InstrumentConfig
from app.services import rhythm_analysis_service as svc_module
from app.services.rhythm_analysis_service import RhythmAnalysisService

NAN = float("nan")


def instrument(tolerance=50.0):
    return InstrumentConfig(name="Dan Bau", fminHz=80.0, fmaxHz=1000.0, toleranceCents=tolerance)


def recording(f0, instrument_id="dan_bau", tempo=120.0, tolerance=50.0):
    return {"instrumentId": instrument_id, "instrument": "Dan Bau", "f0": np.asarray(f0, dtype=float),
            "tempoBpm": tempo, "toleranceCents": tolerance}


def template(f0=None, code="dan_bau", sample_rate=22050, hop=512, tempo=100.0):
    return {"instrument": {"code": code},
            "pitch": {"sampleRate": sample_rate, "hopLength": hop, "f0Hz": [220.0, None, 220.0] if f0 is None else f0},
            "rhythm": {"tempoBpm": tempo}}


# ---------------- seconds_to_frame ----------------
def test_utc_rra_001_seconds_to_frame_normal():
    assert ev.seconds_to_frame(1.0, 1000) == 43
    assert ev.seconds_to_frame(0.5, 100) == 22


def test_utc_rra_002_seconds_to_frame_zero_seconds():
    assert ev.seconds_to_frame(0, 100) == 0


def test_utc_rra_003_seconds_to_frame_negative_clamped_to_zero():
    assert ev.seconds_to_frame(-5.0, 100) == 0


def test_utc_rra_004_seconds_to_frame_beyond_length_clamped():
    assert ev.seconds_to_frame(100.0, 10) == 10


def test_utc_rra_005_seconds_to_frame_zero_length():
    assert ev.seconds_to_frame(3.0, 0) == 0


# ---------------- slice_recording ----------------
def test_utc_rra_006_slice_recording_normal_copy():
    original = recording(np.arange(100, dtype=float))
    sliced, start = ev.slice_recording(original, 0.5, 1.0)
    assert start == 22
    assert list(sliced["f0"]) == list(range(22, 43))
    assert len(original["f0"]) == 100 and sliced["instrumentId"] == "dan_bau"


def test_utc_rra_007_slice_recording_end_beyond_length():
    sliced, start = ev.slice_recording(recording(np.arange(30, dtype=float)), 0.0, 999.0)
    assert start == 0 and len(sliced["f0"]) == 30


def test_utc_rra_008_slice_recording_negative_start():
    sliced, start = ev.slice_recording(recording(np.arange(100, dtype=float)), -3.0, 0.5)
    assert start == 0 and len(sliced["f0"]) == 22


def test_utc_rra_009_slice_recording_empty_f0():
    sliced, start = ev.slice_recording(recording([]), 0.0, 1.0)
    assert start == 0 and len(sliced["f0"]) == 0


def test_utc_rra_010_slice_recording_start_after_end_gives_empty():
    sliced, start = ev.slice_recording(recording(np.arange(100, dtype=float)), 1.0, 0.5)
    assert start == 43 and len(sliced["f0"]) == 0


# ---------------- score_baseline ----------------
def artifact(teacher, student, mapping):
    return {"pitch": {"teacherF0Hz": teacher, "studentF0Hz": student},
            "alignment": {"studentFrameByTeacherFrame": mapping}}


def test_utc_rra_011_score_baseline_normal():
    art = artifact([220.0] * 10, [220.0] * 10, list(range(10)))
    result = ev.score_baseline(art, 0.0, 10.0, 50.0)
    assert set(result) == {"overallScore", "pitchAccuracyPercent", "rhythmAccuracyPercent", "totalVoicedFrames"}
    assert result["pitchAccuracyPercent"] == 100.0 and result["totalVoicedFrames"] == 10


@pytest.mark.parametrize("bad", [{}, None, {"pitch": {}}, {"pitch": {"teacherF0Hz": [1.0], "studentF0Hz": [1.0]}}])
def test_utc_rra_012_score_baseline_missing_keys_returns_none(bad):
    assert ev.score_baseline(bad, 0.0, 1.0, 50.0) is None


def test_utc_rra_013_score_baseline_empty_lists_returns_none():
    assert ev.score_baseline(artifact([], [220.0], [0]), 0.0, 1.0, 50.0) is None
    assert ev.score_baseline(artifact([220.0], [], [0]), 0.0, 1.0, 50.0) is None
    assert ev.score_baseline(artifact([220.0], [220.0], []), 0.0, 1.0, 50.0) is None


def test_utc_rra_014_score_baseline_range_beyond_length_returns_none():
    art = artifact([220.0] * 5, [220.0] * 5, list(range(5)))
    assert ev.score_baseline(art, 50.0, 60.0, 50.0) is None


def test_utc_rra_015_score_baseline_none_values_are_unvoiced():
    art = artifact([220.0, None, 220.0, None], [220.0, 220.0, None, None], [0, 1, 2, 3])
    result = ev.score_baseline(art, 0.0, 10.0, 50.0)
    assert result["totalVoicedFrames"] == 2
    assert result["pitchAccuracyPercent"] == 50.0


def test_utc_rra_016_score_baseline_mapping_index_clamped_to_student_length():
    art = artifact([220.0] * 3, [220.0] * 2, [0, 1, 99])
    result = ev.score_baseline(art, 0.0, 10.0, 50.0)
    assert result["totalVoicedFrames"] == 3 and result["pitchAccuracyPercent"] == 100.0


# ---------------- compare_recordings ----------------
def test_utc_rra_017_compare_recordings_instrument_mismatch():
    with pytest.raises(ValueError, match="Hai bản thu phải dùng cùng nhạc cụ."):
        ev.compare_recordings(recording([220.0] * 5, "a"), recording([220.0] * 5, "b"))


def test_utc_rra_018_compare_recordings_identical_f0_full_score():
    result = ev.compare_recordings(recording([220.0] * 20, tempo=100.04), recording([220.0] * 20, tempo=90.0))
    assert result["pitchAccuracyPercent"] == 100.0
    assert result["totalVoicedFrames"] == 20 and result["meanDeviationCents"] == 0.0
    assert result["teacherTempoBpm"] == 100.0 and result["studentTempoBpm"] == 90.0
    assert result["instrumentId"] == "dan_bau" and "_studentFrames" not in result
    assert set(result["_analysis"]) == {"teacherF0Hz", "studentF0Hz", "studentFrameByTeacherFrame"}
    assert len(result["_analysis"]["teacherF0Hz"]) == 20


def test_utc_rra_019_compare_recordings_nan_serialised_as_none():
    result = ev.compare_recordings(recording([220.0, NAN, 220.0, 220.0]), recording([220.0, 220.0, NAN, 220.0]))
    assert result["_analysis"]["teacherF0Hz"] == [220.0, None, 220.0, 220.0]
    assert result["_analysis"]["studentF0Hz"] == [220.0, 220.0, None, 220.0]


# ---------------- template_analysis_to_recording ----------------
def test_utc_rra_020_template_to_recording_normal():
    rec = ev.template_analysis_to_recording(template(), "dan_bau", instrument(35.0))
    assert rec["instrumentId"] == "dan_bau" and rec["instrument"] == "Dan Bau"
    assert rec["tempoBpm"] == 100.0 and rec["toleranceCents"] == 35.0
    assert rec["f0"][0] == 220.0 and np.isnan(rec["f0"][1])


def test_utc_rra_021_template_to_recording_instrument_mismatch():
    with pytest.raises(ValueError, match="does not match the instrument"):
        ev.template_analysis_to_recording(template(code="other"), "dan_bau", instrument())


def test_utc_rra_022_template_to_recording_incompatible_version():
    with pytest.raises(ValueError, match="version is incompatible"):
        ev.template_analysis_to_recording(template(sample_rate=16000), "dan_bau", instrument())
    with pytest.raises(ValueError, match="version is incompatible"):
        ev.template_analysis_to_recording(template(hop=256), "dan_bau", instrument())


def test_utc_rra_023_template_to_recording_empty_f0():
    with pytest.raises(ValueError, match="no pitch data"):
        ev.template_analysis_to_recording(template(f0=[]), "dan_bau", instrument())


def test_utc_rra_024_template_to_recording_missing_tempo_or_bad_value():
    bad = template()
    bad["rhythm"] = None
    with pytest.raises(ValueError, match="Template analysis is invalid."):
        ev.template_analysis_to_recording(bad, "dan_bau", instrument())
    with pytest.raises(ValueError, match="Template analysis is invalid."):
        ev.template_analysis_to_recording(template(f0=["abc"]), "dan_bau", instrument())


def test_utc_rra_025_template_to_recording_infinite_f0_or_zero_tempo():
    with pytest.raises(ValueError, match="invalid values"):
        ev.template_analysis_to_recording(template(f0=[220.0, float("inf")]), "dan_bau", instrument())
    with pytest.raises(ValueError, match="invalid values"):
        ev.template_analysis_to_recording(template(tempo=0), "dan_bau", instrument())


# ---------------- _to_cents_sequence ----------------
def test_utc_rra_026_to_cents_sequence_values_and_unvoiced():
    seq = ev._to_cents_sequence(np.array([55.0, 110.0, NAN, 220.0]))
    assert seq[0] == pytest.approx(0.0) and seq[1] == pytest.approx(1200.0)
    assert seq[2] == 0.0 and seq[3] == pytest.approx(2400.0)


def test_utc_rra_027_to_cents_sequence_empty():
    assert len(ev._to_cents_sequence(np.array([]))) == 0


# ---------------- _student_frame_by_teacher_frame ----------------
def test_utc_rra_028_student_frame_median_per_teacher_frame():
    path = [(0, 0), (1, 0), (2, 0), (3, 1)]
    assert ev._student_frame_by_teacher_frame(path, 2) == [1, 3]


def test_utc_rra_029_student_frame_monotonic_and_gap_carries_last():
    path = [(5, 0), (2, 2)]
    assert ev._student_frame_by_teacher_frame(path, 3) == [5, 5, 5]


# ---------------- _score_frames ----------------
def test_utc_rra_030_score_frames_tolerance_edge_inclusive():
    teacher = np.array([220.0])
    student = np.array([233.0])
    diff = abs(float(1200.0 * np.log2(233.0 / 220.0)))
    at_edge = ev._score_frames(teacher, student, [(0, 0)], diff)
    below = ev._score_frames(teacher, student, [(0, 0)], diff - 0.01)
    assert at_edge["passedFrames"] == 1 and at_edge["pitchAccuracyPercent"] == 100.0
    assert below["passedFrames"] == 0 and below["pitchAccuracyPercent"] == 0.0


def test_utc_rra_031_score_frames_unvoiced_frames_handled():
    teacher = np.array([220.0, NAN, 220.0, 220.0])
    student = np.array([220.0, 220.0, NAN, 220.0])
    result = ev._score_frames(teacher, student, [(i, i) for i in range(4)], 50.0)
    assert result["totalVoicedFrames"] == 3 and result["passedFrames"] == 2
    assert result["pitchAccuracyPercent"] == 66.67 and result["meanDeviationCents"] == 0.0


def test_utc_rra_032_score_frames_empty_f0_no_voiced():
    result = ev._score_frames(np.array([]), np.array([]), [], 50.0)
    assert result["totalVoicedFrames"] == 0 and result["pitchAccuracyPercent"] == 0.0
    assert result["meanDeviationCents"] is None and result["rhythmAccuracyPercent"] == 100.0
    assert result["overallScore"] == 40.0 and result["_studentFrames"] == []


def test_utc_rra_033_score_frames_error_segment_with_origin_offset():
    teacher = np.full(20, 220.0)
    student = np.full(20, 440.0)
    result = ev._score_frames(teacher, student, [(i, i) for i in range(20)], 50.0, origin_frame=43)
    assert result["passedFrames"] == 0
    segment = result["pitchErrorSegments"][0]
    assert segment["averageCentsDifference"] == pytest.approx(1200.0)
    assert segment["startTime"] == 1.0 and segment["endTime"] == pytest.approx(1.44, abs=0.01)


def test_utc_rra_034_score_frames_short_error_segment_dropped():
    teacher = np.full(5, 220.0)
    student = np.full(5, 440.0)
    result = ev._score_frames(teacher, student, [(i, i) for i in range(5)], 50.0)
    assert result["pitchErrorSegments"] == []


# ---------------- notes ----------------
def test_utc_rra_035_hz_to_midi_float_and_int():
    assert notes.hz_to_midi_float(440.0) == pytest.approx(69.0)
    assert notes.hz_to_midi(261.63) == 60
    assert notes.hz_to_midi(880.0) == 81


@pytest.mark.parametrize("bad", [0.0, -100.0])
def test_utc_rra_036_hz_to_midi_non_positive_hz_raises(bad):
    with pytest.raises((ValueError, ZeroDivisionError)):
        notes.hz_to_midi(bad)


def test_utc_rra_037_midi_to_name_normal_and_low_boundary():
    assert notes.midi_to_name(60) == "Do4"
    assert notes.midi_to_name(69) == "La4"
    assert notes.midi_to_name(61) == "Do#4"
    assert notes.midi_to_name(0) == "Do-1"


def test_utc_rra_038_pitch_class():
    assert notes.pitch_class(60) == 0 and notes.pitch_class(71) == 11 and notes.pitch_class(72) == 0


# ---------------- RhythmAnalysisService ----------------
def test_utc_rra_039_service_analyze_delegates(monkeypatch):
    calls = []
    monkeypatch.setattr(svc_module, "analyze_recording", lambda *a: calls.append(a) or {"ok": 1})
    inst = instrument()
    assert asyncio.run(RhythmAnalysisService().analyze(Path("a.wav"), "dan_bau", inst)) == {"ok": 1}
    assert calls == [(Path("a.wav"), "dan_bau", inst)]


def test_utc_rra_040_service_analyze_propagates_error(monkeypatch):
    def boom(*_):
        raise RuntimeError("cannot decode")
    monkeypatch.setattr(svc_module, "analyze_recording", boom)
    with pytest.raises(RuntimeError, match="cannot decode"):
        asyncio.run(RhythmAnalysisService().analyze(Path("a.wav"), "dan_bau", instrument()))


def test_utc_rra_041_service_analyze_template_real_conversion():
    rec = asyncio.run(RhythmAnalysisService().analyze_template(template(), "dan_bau", instrument()))
    assert rec["tempoBpm"] == 100.0 and len(rec["f0"]) == 3


def test_utc_rra_042_service_analyze_template_invalid_raises():
    with pytest.raises(ValueError, match="does not match the instrument"):
        asyncio.run(RhythmAnalysisService().analyze_template(template(code="x"), "dan_bau", instrument()))


def test_utc_rra_043_service_compare_passes_origin_frame(monkeypatch):
    calls = []
    monkeypatch.setattr(svc_module, "compare_recordings", lambda t, s, o: calls.append(o) or {"r": 1})
    assert asyncio.run(RhythmAnalysisService().compare({}, {}, 7)) == {"r": 1}
    assert asyncio.run(RhythmAnalysisService().compare({}, {})) == {"r": 1}
    assert calls == [7, 0]


def test_utc_rra_044_service_compare_real_instrument_mismatch():
    with pytest.raises(ValueError, match="Hai bản thu"):
        asyncio.run(RhythmAnalysisService().compare(recording([220.0] * 5, "a"), recording([220.0] * 5, "b")))


def test_utc_rra_045_service_evaluate_with_synthetic_f0(monkeypatch):
    monkeypatch.setattr(ev, "_extract_f0", lambda path, fmin, fmax: np.full(20, 220.0))
    monkeypatch.setattr(ev, "_estimate_tempo", lambda path: 111.1)
    result = asyncio.run(RhythmAnalysisService().evaluate(Path("t.wav"), Path("s.wav"), "dan_bau", instrument()))
    assert result["pitchAccuracyPercent"] == 100.0 and "_studentFrames" not in result
    assert result["teacherTempoBpm"] == 111.1 and result["instrument"] == "Dan Bau"


def test_utc_rra_046_service_evaluate_against_template_with_synthetic_f0(monkeypatch):
    monkeypatch.setattr(ev, "_extract_f0", lambda path, fmin, fmax: np.array([220.0, NAN, 220.0]))
    monkeypatch.setattr(ev, "_estimate_tempo", lambda path: 90.0)
    result = asyncio.run(RhythmAnalysisService().evaluate_against_template(template(), Path("s.wav"), "dan_bau", instrument()))
    assert result["totalVoicedFrames"] == 2 and result["pitchAccuracyPercent"] == 100.0
    assert result["teacherTempoBpm"] == 100.0 and result["studentTempoBpm"] == 90.0


def test_utc_rra_047_service_evaluate_against_template_instrument_mismatch(monkeypatch):
    monkeypatch.setattr(ev, "_extract_f0", lambda *a: np.array([220.0]))
    with pytest.raises(ValueError):
        asyncio.run(RhythmAnalysisService().evaluate_against_template(template(code="x"), Path("s.wav"), "dan_bau", instrument()))


def test_utc_rra_048_service_evaluate_against_template_empty_f0_raises(monkeypatch):
    monkeypatch.setattr(ev, "_extract_f0", lambda *a: np.array([220.0]))
    with pytest.raises(ValueError):
        asyncio.run(RhythmAnalysisService().evaluate_against_template(template(f0=[]), Path("s.wav"), "dan_bau", instrument()))
