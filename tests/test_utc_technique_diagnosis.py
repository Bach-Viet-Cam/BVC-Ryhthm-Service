import math

import pytest

from app.feedback.technique.diagnosis import (
    MAX_DIAGNOSES, _entry, _is_octave_error, _merge_string_issues, _tune_lookup, classify_event, diagnose,
)
from app.feedback.technique.events import NoteEvent, _is_voiced, extract_note_events
from app.feedback.technique.rules import dan_bau, dan_nhi, dan_tranh, fretted, generic, sao_truc
from app.schemas.instrument import InstrumentConfig

MIN_FRAMES = 6  # round(0.15 * 22050 / 512)


def hz(midi, cents=0.0):
    return 440.0 * 2 ** ((midi - 69 + cents / 100.0) / 12)


def analysis(notes, frames=40):
    teacher = [hz(m) for m, _ in notes for _ in range(frames)]
    student = [hz(m, c) for m, c in notes for _ in range(frames)]
    return {"teacherF0Hz": teacher, "studentF0Hz": student, "studentFrameByTeacherFrame": list(range(len(teacher)))}


def config(tunes=(), tolerance=30):
    return InstrumentConfig(name="x", fminHz=60, fmaxHz=2000, toleranceCents=tolerance,
                            tunes=[{"name": n, "pitchHz": p} for n, p in tunes])


def ev(midi, cents, start=1.0):
    return NoteEvent(startTime=start, endTime=start + 1, teacherMidi=midi, centsDifference=cents)


def run_events(n_teacher, n_student_voiced=None, midi=69, cents=20.0):
    teacher = [hz(midi)] * n_teacher
    voiced = n_teacher if n_student_voiced is None else n_student_voiced
    student = [hz(midi, cents)] * voiced + [None] * (n_teacher - voiced)
    return extract_note_events(teacher, student, list(range(n_teacher)))


# ---- _is_octave_error
def test_utc_rtd_001_octave_exact_1200():
    assert _is_octave_error(1200.0) is True


def test_utc_rtd_002_octave_margin_upper_boundary_inclusive():
    assert _is_octave_error(1260.0) is True and _is_octave_error(1140.0) is True


def test_utc_rtd_003_octave_just_outside_margin():
    assert _is_octave_error(1260.1) is False and _is_octave_error(1139.9) is False


def test_utc_rtd_004_octave_negative_direction():
    assert _is_octave_error(-1195.0) is True


def test_utc_rtd_005_octave_zero_cents_not_octave():
    assert _is_octave_error(0.0) is False


# ---- _tune_lookup
def test_utc_rtd_006_tune_lookup_keys_by_midi():
    lookup = _tune_lookup("dan_nhi", config([("Dây 1", 440.0), ("Dây 2", 293.66)]))
    assert lookup == {69: "Dây 1", 62: "Dây 2"}


def test_utc_rtd_007_tune_lookup_dan_tranh_uses_pitch_class():
    lookup = _tune_lookup("dan_tranh", config([("Do 4", 261.63)]))
    assert lookup == {0: "Do 4"}


def test_utc_rtd_008_tune_lookup_duplicate_key_keeps_first():
    lookup = _tune_lookup("dan_tranh", config([("Do 4", 261.63), ("Do 5", 523.25)]))
    assert lookup == {0: "Do 4"}


def test_utc_rtd_009_tune_lookup_empty_tunes():
    assert _tune_lookup("dan_bau", config()) == {}


# ---- _entry
def test_utc_rtd_010_entry_direction_and_student_note():
    sharp = _entry(ev(69, 40.0), "TECHNIQUE", "x")
    flat = _entry(ev(69, -40.0), "WRONG_NOTE", "y", 68)
    assert sharp["direction"] == "cao" and sharp["studentNote"] is None and sharp["teacherNote"] == "La4"
    assert flat["direction"] == "thấp" and flat["studentNote"] == "Sol#4"


# ---- classify_event
def test_utc_rtd_011_classify_exactly_at_tolerance_is_none():
    assert classify_event(ev(69, 30.0), "dan_nhi", 30, {}) is None
    assert classify_event(ev(69, -30.0), "dan_nhi", 30, {}) is None


def test_utc_rtd_012_classify_just_above_tolerance_flagged():
    entry = classify_event(ev(69, 30.1), "dan_nhi", 30, {})
    assert entry["kind"] == "TECHNIQUE"


def test_utc_rtd_013_classify_exactly_50_cents_not_wrong_note():
    assert classify_event(ev(69, 50.0), "dan_nhi", 30, {})["kind"] == "TECHNIQUE"


def test_utc_rtd_014_classify_above_50_cents_is_wrong_note_with_student_note():
    entry = classify_event(ev(69, 50.1), "dan_nhi", 30, {})
    assert entry["kind"] == "WRONG_NOTE" and entry["studentNote"] == "La#4"
    assert "Đánh sai nốt: bản mẫu là La4, bạn đánh thành La#4." == entry["advice"]


def test_utc_rtd_015_classify_flat_wrong_note_goes_down():
    entry = classify_event(ev(69, -160.0), "dan_nhi", 30, {})
    assert entry["kind"] == "WRONG_NOTE" and entry["direction"] == "thấp"
    assert entry["studentNote"] == "Sol4"


def test_utc_rtd_016_classify_octave_error_ignored_even_beyond_tolerance():
    assert classify_event(ev(69, 1200.0), "dan_nhi", 30, {}) is None
    assert classify_event(ev(69, -1150.0), "dan_nhi", 30, {}) is None


def test_utc_rtd_017_classify_string_tuning_sharp_vs_flat_text():
    tunes = {67: "Dây 2"}
    sharp = classify_event(ev(67, 40.0), "dan_nhi", 30, tunes)
    flat = classify_event(ev(67, -40.0), "dan_nhi", 30, tunes)
    assert sharp["kind"] == flat["kind"] == "STRING_TUNING"
    assert "đang cao" in sharp["advice"] and "đang thấp" in flat["advice"]
    assert '"Dây 2"' in sharp["advice"]


def test_utc_rtd_018_classify_unknown_instrument_uses_generic():
    sharp = classify_event(ev(69, 40.0), "dan_xyz", 30, {})
    flat = classify_event(ev(69, -40.0), "dan_xyz", 30, {})
    assert sharp["advice"] == "Nốt đang cao hơn bản mẫu, hạ cao độ xuống."
    assert flat["advice"] == "Nốt đang thấp hơn bản mẫu, nâng cao độ lên."


def test_utc_rtd_019_classify_sao_truc_skips_tunes_and_names_note():
    entry = classify_event(ev(72, -40.0), "sao_truc", 30, {72: "Do 5"})
    assert entry["kind"] == "TECHNIQUE" and "Nốt Do5 đang thấp" in entry["advice"]


def test_utc_rtd_020_classify_dan_tranh_matches_other_octave_by_pitch_class():
    entry = classify_event(ev(74, 40.0), "dan_tranh", 30, {2: "Dây Re"})
    assert entry["kind"] == "STRING_TUNING"


def test_utc_rtd_021_classify_no_matching_string_falls_to_technique():
    entry = classify_event(ev(70, 40.0), "dan_nhi", 30, {67: "Dây 2"})
    assert entry["kind"] == "TECHNIQUE" and "Dịch ngón tay" in entry["advice"]


# ---- rules
@pytest.mark.parametrize(("midi", "sharp", "text"), [
    (60, True, "không cần kéo hay nhấn"),    # C4 excluded from both
    (61, False, "kéo cần mạnh tay hơn"),     # (C4,E4]
    (64, True, "kéo cần nhẹ tay hơn"),       # E4 included in pull (closed high end)
    (65, True, "nhấn cần mạnh tay hơn"),     # F4 included in press
    (79, False, "không cần kéo hay nhấn"),   # G5 excluded from press, pull starts above
    (49, False, "nhấn cần nhẹ tay hơn"),     # (C3, C4)
    (48, True, "không cần kéo hay nhấn"),    # C3 excluded
])
def test_utc_rtd_022_dan_bau_range_boundaries(midi, sharp, text):
    assert text in dan_bau(midi, sharp)


def test_utc_rtd_023_simple_rules_sharp_vs_flat():
    assert dan_nhi(60, True) == "Dịch ngón tay lên hoặc bấm nhẹ ngón tay hơn."
    assert dan_nhi(60, False) == "Dịch ngón tay xuống."
    assert fretted(60, True) == "Bấm phím nhẹ tay hơn, không kéo dây."
    assert fretted(60, False) == "Bấm phím mạnh tay hơn."
    assert dan_tranh(60, True).endswith("nhấn dây nhẹ hơn.")
    assert dan_tranh(60, False).endswith("nhấn dây mạnh hơn.")
    assert generic(60, True) != generic(60, False)
    assert "đang cao" in sao_truc(60, True) and "Do4" in sao_truc(60, True)


# ---- _merge_string_issues
def _se(note, direction, cents):
    return {"kind": "STRING_TUNING", "teacherNote": note, "direction": direction, "centsDifference": cents, "startTime": 0}


def test_utc_rtd_024_merge_same_string_averages_cents_and_counts():
    merged = _merge_string_issues([_se("Do4", "cao", 40.0), _se("Do4", "cao", 35.0)])
    assert len(merged) == 1 and merged[0]["occurrences"] == 2 and merged[0]["centsDifference"] == 37.5


def test_utc_rtd_025_merge_keeps_different_direction_and_other_kinds():
    other = {"kind": "TECHNIQUE", "teacherNote": "Do4", "direction": "cao", "centsDifference": 40.0, "startTime": 1}
    merged = _merge_string_issues([_se("Do4", "cao", 40.0), _se("Do4", "thấp", -40.0), other])
    assert len(merged) == 3 and merged[-1] is other
    assert all(m["occurrences"] == 1 for m in merged if m["kind"] == "STRING_TUNING")


def test_utc_rtd_026_merge_empty_list():
    assert _merge_string_issues([]) == []


# ---- diagnose
def test_utc_rtd_027_diagnose_empty_or_none_analysis():
    assert diagnose(None, "dan_bau", config()) == []
    assert diagnose({}, "dan_bau", config()) == []
    assert diagnose({"teacherF0Hz": [], "studentF0Hz": [], "studentFrameByTeacherFrame": []}, "dan_bau", config()) == []


def test_utc_rtd_028_diagnose_unknown_instrument_generic_advice():
    result = diagnose(analysis([(69, 40)]), "unknown_inst", config())
    assert len(result) == 1 and result[0]["advice"].startswith("Nốt đang cao")


def test_utc_rtd_029_diagnose_caps_to_max_and_keeps_wrong_notes_sorted_by_time():
    notes = [(60 + 2 * i, 40) for i in range(MAX_DIAGNOSES + 2)]
    notes[-1] = (90, 120)  # WRONG_NOTE at the last position must survive truncation
    result = diagnose(analysis(notes), "dan_nhi", config())
    assert len(result) == MAX_DIAGNOSES
    assert any(r["kind"] == "WRONG_NOTE" for r in result)
    starts = [r["startTime"] for r in result]
    assert starts == sorted(starts)


def test_utc_rtd_030_diagnose_origin_frame_shifts_time():
    base = diagnose(analysis([(69, 40)]), "dan_nhi", config())[0]["startTime"]
    shifted = diagnose(analysis([(69, 40)]), "dan_nhi", config(), origin_frame=430)[0]["startTime"]
    assert shifted - base == pytest.approx(430 * 512 / 22050, abs=0.02)


# ---- events
def test_utc_rtd_031_min_note_length_boundary():
    assert run_events(MIN_FRAMES - 1) == []
    events = run_events(MIN_FRAMES)
    assert len(events) == 1 and events[0].teacherMidi == 69
    assert events[0].centsDifference == pytest.approx(20, abs=0.5)


def test_utc_rtd_032_student_coverage_boundary():
    n = 10
    assert len(run_events(n, n_student_voiced=5)) == 1   # exactly 50%
    assert run_events(n, n_student_voiced=4) == []       # below 50%


def test_utc_rtd_033_unvoiced_teacher_frames_split_notes():
    f0 = [hz(69)] * 8 + [None] * 3 + [0.0] * 2 + [float("nan")] + [hz(71)] * 8
    events = extract_note_events(f0, [hz(69)] * len(f0), list(range(len(f0))))
    assert [e.teacherMidi for e in events] == [69, 71]


def test_utc_rtd_034_mapping_shorter_than_teacher_and_clamped_student_index():
    teacher = [hz(69)] * 10
    # mapping covers only 8 frames; student index 99 is clamped to last student frame
    events = extract_note_events(teacher, [hz(69, 10)] * 3, [99] * 8)
    assert len(events) == 1 and events[0].centsDifference == pytest.approx(10, abs=0.5)


def test_utc_rtd_035_empty_inputs_and_empty_student():
    assert extract_note_events([], [], []) == []
    assert extract_note_events([hz(69)] * 10, [], list(range(10))) == []


@pytest.mark.parametrize(("value", "expected"), [
    (None, False), (0.0, False), (-5.0, False), (float("nan"), False), (math.inf, False), (440.0, True),
])
def test_utc_rtd_036_is_voiced(value, expected):
    assert _is_voiced(value) is expected
