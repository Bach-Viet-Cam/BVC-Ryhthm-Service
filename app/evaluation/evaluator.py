from pathlib import Path

import librosa
import numpy as np

from app.evaluation.instruments import get_instrument, normalize_instrument_id

SAMPLE_RATE = 22050
HOP_LENGTH = 512
REFERENCE_HZ = 55.0


def _extract_f0(audio_path: Path, fmin: float, fmax: float) -> np.ndarray:
    audio, sample_rate = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
    f0, _, _ = librosa.pyin(audio, fmin=fmin, fmax=fmax, sr=sample_rate, hop_length=HOP_LENGTH)
    return f0


def _to_cents_sequence(f0: np.ndarray) -> np.ndarray:
    sequence = np.zeros_like(f0, dtype=float)
    voiced = ~np.isnan(f0)
    sequence[voiced] = 1200.0 * np.log2(f0[voiced] / REFERENCE_HZ)
    return sequence


def _score_pitch_and_rhythm(student_f0: np.ndarray, teacher_f0: np.ndarray, tolerance_cents: float) -> dict:
    _, warping_path = librosa.sequence.dtw(
        X=_to_cents_sequence(student_f0).reshape(1, -1),
        Y=_to_cents_sequence(teacher_f0).reshape(1, -1),
        metric="euclidean",
    )
    path = warping_path[::-1]
    passed_frames = 0
    total_voiced_frames = 0
    deviations: list[float] = []
    rhythm_segments: list[dict] = []
    error_segments: list[dict] = []
    current_error: dict | None = None

    chunk_frames = max(1, int(2.0 * SAMPLE_RATE / HOP_LENGTH))
    max_teacher_index = max((teacher_index for _, teacher_index in path), default=0)

    for student_index, teacher_index in path:
        teacher_pitch = teacher_f0[teacher_index]
        if np.isnan(teacher_pitch):
            if current_error is not None:
                error_segments.append(current_error)
                current_error = None
            continue

        total_voiced_frames += 1
        student_pitch = student_f0[student_index]
        if np.isnan(student_pitch):
            if current_error is not None:
                error_segments.append(current_error)
                current_error = None
            continue

        raw_difference = float(1200.0 * np.log2(student_pitch / teacher_pitch))
        absolute_difference = abs(raw_difference)
        deviations.append(absolute_difference)
        timestamp = teacher_index * HOP_LENGTH / SAMPLE_RATE

        if absolute_difference <= tolerance_cents:
            passed_frames += 1
            if current_error is not None:
                error_segments.append(current_error)
                current_error = None
        elif current_error is None:
            current_error = {"startTime": timestamp, "endTime": timestamp, "differences": [raw_difference]}
        else:
            current_error["endTime"] = timestamp
            current_error["differences"].append(raw_difference)

    if current_error is not None:
        error_segments.append(current_error)

    for chunk_start in range(0, max_teacher_index + 1, chunk_frames):
        chunk_end = chunk_start + chunk_frames
        chunk_path = [(student_index, teacher_index) for student_index, teacher_index in path if chunk_start <= teacher_index < chunk_end]
        if not chunk_path:
            continue
        voiced_count = sum(not np.isnan(teacher_f0[teacher_index]) for _, teacher_index in chunk_path)
        if voiced_count < chunk_frames * 0.2:
            continue

        student_indices = [student_index for student_index, _ in chunk_path]
        missing_count = sum(
            not np.isnan(teacher_f0[teacher_index]) and np.isnan(student_f0[student_index])
            for student_index, teacher_index in chunk_path
        )
        start_time = round(chunk_start * HOP_LENGTH / SAMPLE_RATE, 2)
        end_time = round(min(chunk_end, max_teacher_index) * HOP_LENGTH / SAMPLE_RATE, 2)
        if missing_count > voiced_count * 0.5:
            rhythm_segments.append({"startTime": start_time, "endTime": end_time, "status": "missing"})
            continue

        teacher_duration = len({teacher_index for _, teacher_index in chunk_path})
        student_duration = max(student_indices) - min(student_indices) + 1
        ratio = student_duration / teacher_duration if teacher_duration else 1.0
        if ratio > 1.3:
            rhythm_segments.append({"startTime": start_time, "endTime": end_time, "status": "slow"})
        elif ratio < 0.75:
            rhythm_segments.append({"startTime": start_time, "endTime": end_time, "status": "fast"})

    processed_errors = []
    for segment in error_segments:
        if segment["endTime"] - segment["startTime"] >= 0.3:
            processed_errors.append({
                "startTime": round(float(segment["startTime"]), 2),
                "endTime": round(float(segment["endTime"]), 2),
                "averageCentsDifference": round(float(np.mean(segment["differences"])), 2),
            })
    processed_errors.sort(key=lambda segment: abs(segment["averageCentsDifference"]), reverse=True)

    pitch_accuracy = passed_frames / total_voiced_frames * 100.0 if total_voiced_frames else 0.0
    rhythm_accuracy = max(0.0, 100.0 - len(rhythm_segments) * 10.0)
    return {
        "pitchAccuracyPercent": round(pitch_accuracy, 2),
        "rhythmAccuracyPercent": round(rhythm_accuracy, 2),
        "overallScore": round(pitch_accuracy * 0.6 + rhythm_accuracy * 0.4, 2),
        "meanDeviationCents": round(float(np.mean(deviations)), 2) if deviations else None,
        "totalVoicedFrames": total_voiced_frames,
        "passedFrames": passed_frames,
        "rhythmSegments": rhythm_segments,
        "pitchErrorSegments": processed_errors[:5],
    }


def _estimate_tempo(audio_path: Path) -> float:
    audio, sample_rate = librosa.load(str(audio_path), sr=SAMPLE_RATE, mono=True)
    tempo, _ = librosa.beat.beat_track(y=audio, sr=sample_rate, hop_length=HOP_LENGTH)
    return round(float(np.atleast_1d(tempo)[0]), 1)


def analyze_recording(audio_path: Path, instrument_id: str) -> dict:
    """Extract features for a recording; scoring happens separately."""
    normalized_instrument_id = normalize_instrument_id(instrument_id)
    instrument = get_instrument(normalized_instrument_id)
    return {
        "instrumentId": normalized_instrument_id,
        "instrument": instrument["name"],
        "f0": _extract_f0(audio_path, float(instrument["fmin"]), float(instrument["fmax"])),
        "tempoBpm": _estimate_tempo(audio_path),
        "toleranceCents": float(instrument["toleranceCents"]),
    }


def compare_recordings(teacher: dict, student: dict) -> dict:
    """Score recordings whose audio features have already been extracted."""
    if teacher["instrumentId"] != student["instrumentId"]:
        raise ValueError("Hai bản thu phải dùng cùng nhạc cụ.")
    result = _score_pitch_and_rhythm(student["f0"], teacher["f0"], student["toleranceCents"])
    result["instrumentId"] = student["instrumentId"]
    result["instrument"] = student["instrument"]
    result["teacherTempoBpm"] = round(float(teacher["tempoBpm"]), 1)
    result["studentTempoBpm"] = round(float(student["tempoBpm"]), 1)
    return result


def template_analysis_to_recording(template: dict, instrument_id: str) -> dict:
    """Validate and convert a cached template artifact for comparison."""
    normalized_instrument_id = normalize_instrument_id(instrument_id)
    instrument = get_instrument(normalized_instrument_id)
    template_instrument = template.get("instrument")
    pitch = template.get("pitch")
    rhythm = template.get("rhythm")
    if not isinstance(template_instrument, dict) or template_instrument.get("code") != normalized_instrument_id:
        raise ValueError("Template analysis does not match the instrument.")
    if not isinstance(pitch, dict) or pitch.get("sampleRate") != SAMPLE_RATE or pitch.get("hopLength") != HOP_LENGTH:
        raise ValueError("Template analysis version is incompatible.")
    values = pitch.get("f0Hz")
    if not isinstance(values, list) or not values:
        raise ValueError("Template analysis has no pitch data.")
    try:
        f0 = np.asarray([np.nan if value is None else float(value) for value in values], dtype=float)
        tempo = float(rhythm["tempoBpm"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Template analysis is invalid.") from error
    if not np.all(np.isfinite(f0) | np.isnan(f0)) or not np.isfinite(tempo) or tempo <= 0:
        raise ValueError("Template analysis has invalid values.")
    return {"instrumentId": normalized_instrument_id, "instrument": instrument["name"], "f0": f0,
            "tempoBpm": tempo, "toleranceCents": float(instrument["toleranceCents"])}


def evaluate_pair(teacher_path: Path, student_path: Path, instrument_id: str) -> dict:
    normalized_instrument_id = normalize_instrument_id(instrument_id)
    instrument = get_instrument(normalized_instrument_id)
    fmin = float(instrument["fmin"])
    fmax = float(instrument["fmax"])
    teacher_f0 = _extract_f0(teacher_path, fmin, fmax)
    student_f0 = _extract_f0(student_path, fmin, fmax)
    result = _score_pitch_and_rhythm(student_f0, teacher_f0, float(instrument["toleranceCents"]))
    result["instrumentId"] = normalized_instrument_id
    result["instrument"] = instrument["name"]
    result["teacherTempoBpm"] = _estimate_tempo(teacher_path)
    result["studentTempoBpm"] = _estimate_tempo(student_path)
    return result


def evaluate_against_template(template: dict, student_path: Path, instrument_id: str) -> dict:
    """Evaluate a student recording using the persisted features of a template."""
    normalized_instrument_id = normalize_instrument_id(instrument_id)
    instrument = get_instrument(normalized_instrument_id)
    template_instrument = template.get("instrument")
    pitch = template.get("pitch")
    rhythm = template.get("rhythm")
    if not isinstance(template_instrument, dict) or template_instrument.get("code") != normalized_instrument_id:
        raise ValueError("Template analysis khÃ´ng khá»›p nháº¡c cá»¥.")
    if not isinstance(pitch, dict) or pitch.get("sampleRate") != SAMPLE_RATE or pitch.get("hopLength") != HOP_LENGTH:
        raise ValueError("Template analysis khÃ´ng tÆ°Æ¡ng thÃ­ch phiÃªn báº£n chÃ¢m Ä‘iá»ƒm.")
    values = pitch.get("f0Hz")
    if not isinstance(values, list) or not values:
        raise ValueError("Template analysis khÃ´ng cÃ³ dá»¯ liá»‡u cao Ä‘á»™.")
    try:
        teacher_f0 = np.asarray([np.nan if value is None else float(value) for value in values], dtype=float)
        teacher_tempo = float(rhythm["tempoBpm"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Template analysis khÃ´ng hợp lá»‡.") from error
    if not np.all(np.isfinite(teacher_f0) | np.isnan(teacher_f0)) or not np.isfinite(teacher_tempo) or teacher_tempo <= 0:
        raise ValueError("Template analysis chứa giÃ¡ trá»‹ khÃ´ng hợp lá»‡.")
    student_f0 = _extract_f0(student_path, float(instrument["fmin"]), float(instrument["fmax"]))
    result = _score_pitch_and_rhythm(student_f0, teacher_f0, float(instrument["toleranceCents"]))
    result["instrumentId"] = normalized_instrument_id
    result["instrument"] = instrument["name"]
    result["teacherTempoBpm"] = round(teacher_tempo, 1)
    result["studentTempoBpm"] = _estimate_tempo(student_path)
    return result
