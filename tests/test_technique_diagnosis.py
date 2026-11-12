import pytest

from app.evaluation.notes import hz_to_midi, midi_to_name
from app.feedback import gemini_feedback
from app.feedback.technique import diagnose
from app.feedback.technique.events import NoteEvent, extract_note_events
from app.feedback.technique.diagnosis import classify_event
from app.feedback.technique.rules import dan_bau
from app.schemas.instrument import InstrumentConfig

FRAMES = 40  # ~0.93 s mỗi nốt


def hz(midi: int, cents: float = 0.0) -> float:
    return 440.0 * 2 ** ((midi - 69 + cents / 100.0) / 12)


def analysis(notes: list[tuple[int, float]]) -> dict:
    """Mỗi nốt: (MIDI của giáo viên, độ lệch cents của học viên); căn chỉnh 1-1."""
    teacher = [hz(midi) for midi, _ in notes for _ in range(FRAMES)]
    student = [hz(midi, cents) for midi, cents in notes for _ in range(FRAMES)]
    return {"teacherF0Hz": teacher, "studentF0Hz": student, "studentFrameByTeacherFrame": list(range(len(teacher)))}


def config(tunes: list[tuple[str, float]] = (), tolerance: float = 30) -> InstrumentConfig:
    return InstrumentConfig(name="x", fminHz=60, fmaxHz=2000, toleranceCents=tolerance,
                            tunes=[{"name": name, "pitchHz": pitch} for name, pitch in tunes])


def event(midi: int, cents: float) -> NoteEvent:
    return NoteEvent(startTime=1.0, endTime=2.0, teacherMidi=midi, centsDifference=cents)


def test_note_names_use_vietnamese_solfege():
    assert midi_to_name(60) == "Do4"
    assert midi_to_name(70) == "La#4"
    assert hz_to_midi(440.0) == 69


def test_events_split_on_teacher_note_change_and_measure_median_cents():
    events = extract_note_events(**{
        "teacher_f0": analysis([(69, 40), (71, -10)])["teacherF0Hz"],
        "student_f0": analysis([(69, 40), (71, -10)])["studentF0Hz"],
        "student_frame_by_teacher_frame": list(range(2 * FRAMES)),
    })
    assert [item.teacherMidi for item in events] == [69, 71]
    assert events[0].centsDifference == pytest.approx(40, abs=0.5)
    assert events[1].centsDifference == pytest.approx(-10, abs=0.5)


def test_events_respect_segment_origin_frame():
    data = analysis([(69, 40)])
    events = extract_note_events(data["teacherF0Hz"], data["studentF0Hz"], data["studentFrameByTeacherFrame"], origin_frame=430)
    assert events[0].startTime == pytest.approx(10.0, abs=0.05)


def test_within_tolerance_has_no_diagnosis():
    assert classify_event(event(69, 25), "dan_nhi", 30, {}) is None


def test_more_than_half_semitone_is_wrong_note():
    entry = classify_event(event(69, 95), "dan_nhi", 30, {})
    assert entry["kind"] == "WRONG_NOTE"
    assert entry["studentNote"] == "La#4"


def test_octave_jump_is_treated_as_detection_error():
    assert classify_event(event(69, 1195), "dan_nhi", 30, {}) is None


def test_open_string_deviation_asks_to_retune_that_string():
    entry = classify_event(event(67, -40), "dan_nhi", 30, {67: "Dây 2 (Sol 4)"})
    assert entry["kind"] == "STRING_TUNING"
    assert "Dây 2 (Sol 4)" in entry["advice"]


def test_flute_never_blames_strings():
    entry = classify_event(event(72, 40), "sao_truc", 30, {72: "Do 5"})
    assert entry["kind"] == "TECHNIQUE"
    assert "đặt môi" in entry["advice"]


@pytest.mark.parametrize(("midi", "sharp", "expected"), [
    (62, True, "kéo cần nhẹ tay hơn"),     # Re4 thuộc (C4, E4]
    (62, False, "kéo cần mạnh tay hơn"),
    (66, True, "nhấn cần mạnh tay hơn"),   # Fa#4 thuộc [F4, G4)
    (66, False, "nhấn cần nhẹ tay hơn"),
    (74, True, "kéo cần nhẹ tay hơn"),     # Re5 nằm ở cả hai khoảng: ưu tiên kéo
    (83, True, "nhấn cần mạnh tay hơn"),   # Si5 thuộc [B5, C6)
    (67, True, "gảy đúng điểm bồi âm"),    # Sol4 là điểm bồi âm
    (76, False, "gảy đúng điểm bồi âm"),   # Mi5
])
def test_dan_bau_pull_press_ranges(midi, sharp, expected):
    assert expected in dan_bau(midi, sharp)


def test_dan_tranh_matches_strings_by_pitch_class_and_presses_other_notes():
    tunes = [("Dây 3 (Do 4)", 261.63), ("Dây 4 (Re 4)", 293.66)]
    result = diagnose(analysis([(74, 40), (77, -40)]), "dan_tranh", config(tunes))
    kinds = {item["teacherNote"]: item for item in result}
    assert kinds["Re5"]["kind"] == "STRING_TUNING"
    assert kinds["Fa5"]["kind"] == "TECHNIQUE"
    assert "nhấn dây mạnh hơn" in kinds["Fa5"]["advice"]


def test_repeated_string_issue_is_merged():
    tunes = [("Dây buông (Do 4)", 261.63)]
    result = diagnose(analysis([(60, 40), (62, 0), (60, 38)]), "dan_bau", config(tunes))
    assert len(result) == 1
    assert result[0]["occurrences"] == 2


def test_diagnose_without_analysis_is_empty():
    assert diagnose(None, "dan_bau", config()) == []


def test_prompt_contains_diagnoses_and_usage_rules():
    item = {"kind": "TECHNIQUE", "startTime": 65.0, "endTime": 66.0, "teacherNote": "Re4", "studentNote": None,
            "centsDifference": 40.0, "direction": "cao", "advice": "kéo cần nhẹ tay hơn."}
    prompt = gemini_feedback._build_prompt({"instrument": "Đàn bầu", "techniqueDiagnoses": [item]})
    assert "kéo cần nhẹ tay hơn." in prompt
    assert '"01:05"' in prompt
    assert "WRONG_NOTE" in prompt


def test_prompt_falls_back_to_raw_segments_without_diagnoses():
    prompt = gemini_feedback._build_prompt({"pitchErrorSegments": [{"startTime": 1, "endTime": 2, "averageCentsDifference": 40}]})
    assert "pitchErrorSegments" in prompt
    assert "techniqueDiagnoses" not in prompt
