"""Chẩn đoán lỗi cao độ theo từng nốt, tất định, để AI chỉ diễn đạt lại chứ không tự suy luận nhạc lý."""

from collections import defaultdict
from typing import Any, Literal

from app.evaluation.notes import hz_to_midi, midi_to_name, pitch_class
from app.feedback.technique.events import NoteEvent, extract_note_events
from app.feedback.technique.rules import MATCH_TUNES_BY_PITCH_CLASS, NO_STRINGS, TECHNIQUE_RULES, generic
from app.schemas.instrument import InstrumentConfig

DiagnosisKind = Literal["WRONG_NOTE", "STRING_TUNING", "TECHNIQUE"]

# Lệch quá nửa cung là học viên đã đánh sang nốt khác
WRONG_NOTE_CENTS = 50.0
# Lệch gần đúng một quãng tám thường là pyin bắt nhầm bồi âm, không phải lỗi của học viên
OCTAVE_CENTS = 1200.0
OCTAVE_MARGIN_CENTS = 60.0
MAX_DIAGNOSES = 8
KIND_ORDER: dict[str, int] = {"WRONG_NOTE": 0, "STRING_TUNING": 1, "TECHNIQUE": 2}


def _is_octave_error(cents: float) -> bool:
    return abs(abs(cents) - OCTAVE_CENTS) <= OCTAVE_MARGIN_CENTS


def _tune_lookup(instrument_id: str, instrument: InstrumentConfig) -> dict[int, str]:
    """Khóa là MIDI (hoặc pitch class với đàn tranh), giá trị là tên dây trong bảng tunes."""
    lookup: dict[int, str] = {}
    for tune in instrument.tunes:
        midi = hz_to_midi(tune.pitchHz)
        key = pitch_class(midi) if instrument_id in MATCH_TUNES_BY_PITCH_CLASS else midi
        lookup.setdefault(key, tune.name)
    return lookup


def _entry(event: NoteEvent, kind: DiagnosisKind, advice: str, student_midi: int | None = None) -> dict[str, Any]:
    return {
        "kind": kind, "startTime": event.startTime, "endTime": event.endTime,
        "teacherNote": midi_to_name(event.teacherMidi),
        "studentNote": midi_to_name(student_midi) if student_midi is not None else None,
        "centsDifference": event.centsDifference, "direction": "cao" if event.centsDifference > 0 else "thấp",
        "advice": advice,
    }


def classify_event(event: NoteEvent, instrument_id: str, tolerance_cents: float,
                   tunes: dict[int, str]) -> dict[str, Any] | None:
    cents = event.centsDifference
    if abs(cents) <= tolerance_cents or _is_octave_error(cents):
        return None
    sharp = cents > 0
    if abs(cents) > WRONG_NOTE_CENTS:
        student_midi = event.teacherMidi + round(cents / 100.0)
        return _entry(event, "WRONG_NOTE", f"Đánh sai nốt: bản mẫu là {midi_to_name(event.teacherMidi)}, "
                                           f"bạn đánh thành {midi_to_name(student_midi)}.", student_midi)
    if instrument_id not in NO_STRINGS:
        key = pitch_class(event.teacherMidi) if instrument_id in MATCH_TUNES_BY_PITCH_CLASS else event.teacherMidi
        string_name = tunes.get(key)
        if string_name is not None:
            return _entry(event, "STRING_TUNING", f"Nốt dây buông đang {'cao' if sharp else 'thấp'}: "
                                                  f"chỉnh lại \"{string_name}\" theo bảng lên dây.")
    rule = TECHNIQUE_RULES.get(instrument_id, generic)
    return _entry(event, "TECHNIQUE", rule(event.teacherMidi, sharp))


def _merge_string_issues(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Một dây lên sai làm mọi lần gảy dây đó lệch; gộp thành một mục để AI không nhắc lặp lại."""
    by_string: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    others: list[dict[str, Any]] = []
    for entry in entries:
        if entry["kind"] == "STRING_TUNING":
            by_string[(entry["teacherNote"], entry["direction"])].append(entry)
        else:
            others.append(entry)
    merged = []
    for group in by_string.values():
        first = dict(group[0])
        first["occurrences"] = len(group)
        first["centsDifference"] = round(sum(item["centsDifference"] for item in group) / len(group), 1)
        merged.append(first)
    return merged + others


def diagnose(analysis: dict[str, Any] | None, instrument_id: str, instrument: InstrumentConfig,
             origin_frame: int = 0) -> list[dict[str, Any]]:
    """`analysis` là khối `_analysis` của compare_recordings (teacherF0Hz, studentF0Hz, studentFrameByTeacherFrame)."""
    if not analysis:
        return []
    events = extract_note_events(
        analysis.get("teacherF0Hz") or [], analysis.get("studentF0Hz") or [],
        analysis.get("studentFrameByTeacherFrame") or [], origin_frame,
    )
    tunes = _tune_lookup(instrument_id, instrument)
    entries = [entry for event in events
               if (entry := classify_event(event, instrument_id, float(instrument.toleranceCents), tunes)) is not None]
    entries = _merge_string_issues(entries)
    entries.sort(key=lambda entry: (KIND_ORDER[entry["kind"]], -abs(entry["centsDifference"])))
    return sorted(entries[:MAX_DIAGNOSES], key=lambda entry: entry["startTime"])
