"""Tách bài thành các nốt của giáo viên và đo độ lệch trung vị của học viên trên từng nốt."""

import math
from dataclasses import dataclass
from statistics import median

from app.evaluation.evaluator import HOP_LENGTH, SAMPLE_RATE
from app.evaluation.notes import hz_to_midi, hz_to_midi_float

# Một nốt ngắn hơn ngưỡng này thường là chỗ chuyển nốt hoặc nhiễu căn chỉnh, không đáng nhận xét
MIN_NOTE_SEC = 0.15
# Cần đủ khung có tiếng của học viên trong nốt thì độ lệch mới đáng tin
MIN_STUDENT_COVERAGE = 0.5


@dataclass(frozen=True)
class NoteEvent:
    startTime: float
    endTime: float
    teacherMidi: int
    # Độ lệch trung vị (cents, có dấu) của học viên so với tần số giáo viên thực tế
    centsDifference: float


def _is_voiced(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _frame_to_seconds(frame: int) -> float:
    return frame * HOP_LENGTH / SAMPLE_RATE


def extract_note_events(
    teacher_f0: list[float | None],
    student_f0: list[float | None],
    student_frame_by_teacher_frame: list[int],
    origin_frame: int = 0,
) -> list[NoteEvent]:
    events: list[NoteEvent] = []
    min_frames = max(1, round(MIN_NOTE_SEC * SAMPLE_RATE / HOP_LENGTH))
    run_start: int | None = None
    run_midi: int | None = None
    run_diffs: list[float] = []

    def close(end_frame: int) -> None:
        if run_start is None or run_midi is None:
            return
        length = end_frame - run_start
        if length >= min_frames and len(run_diffs) >= length * MIN_STUDENT_COVERAGE:
            events.append(NoteEvent(
                startTime=round(_frame_to_seconds(run_start + origin_frame), 2),
                endTime=round(_frame_to_seconds(end_frame - 1 + origin_frame), 2),
                teacherMidi=run_midi,
                centsDifference=round(float(median(run_diffs)), 1),
            ))

    for teacher_index, teacher_hz in enumerate(teacher_f0):
        midi = hz_to_midi(teacher_hz) if _is_voiced(teacher_hz) else None
        if midi != run_midi:
            close(teacher_index)
            run_start, run_midi, run_diffs = (teacher_index, midi, []) if midi is not None else (None, None, [])
        if midi is None or teacher_index >= len(student_frame_by_teacher_frame):
            continue
        student_index = min(int(student_frame_by_teacher_frame[teacher_index]), len(student_f0) - 1)
        student_hz = student_f0[student_index] if student_index >= 0 else None
        if _is_voiced(student_hz):
            run_diffs.append(100.0 * (hz_to_midi_float(student_hz) - hz_to_midi_float(teacher_hz)))
    close(len(teacher_f0))
    return events
