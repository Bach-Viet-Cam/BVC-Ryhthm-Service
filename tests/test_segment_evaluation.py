import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from app.evaluation.evaluator import compare_recordings, score_baseline, seconds_to_frame, slice_recording
from app.schemas.messages import EvaluationTask
from test_evaluation_artifact import recording
from test_evaluation_quality import task as base_task

FIXTURES = Path(__file__).parent / "fixtures"


def load_golden(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_full_piece_scoring_is_unchanged_after_split():
    teacher = recording([220.0, 220.0, np.nan, 330.0, 330.0, 440.0] * 40)
    student = recording([220.0, np.nan, 330.0, 440.0] * 50)
    before = load_golden("full_piece_golden.json")
    after = compare_recordings(teacher, student)
    after.pop("_analysis")
    assert after == before


def test_segment_error_times_fall_inside_the_range():
    teacher = recording(([220.0] * 200) + ([330.0] * 200))
    student = recording([350.0] * 120)
    sliced, origin = slice_recording(teacher, 5.0, 8.0)
    result = compare_recordings(sliced, student, origin)
    assert result["pitchErrorSegments"]
    for segment in result["pitchErrorSegments"] + result["rhythmSegments"]:
        assert 5.0 - 0.05 <= segment["startTime"] <= segment["endTime"] <= 8.0 + 0.05


def test_slice_clamps_end_past_the_template():
    teacher = recording([220.0] * 100)
    sliced, origin = slice_recording(teacher, 1.0, 999.0)
    assert origin == seconds_to_frame(1.0, 100)
    assert len(sliced["f0"]) == 100 - origin


def test_baseline_over_whole_range_matches_original_pitch_score():
    teacher = recording([220.0, 220.0, 330.0, 330.0] * 50)
    student = recording([221.0, 300.0, 330.0, 331.0] * 50)
    full = compare_recordings(teacher, student)
    artifact = {"pitch": {"teacherF0Hz": full["_analysis"]["teacherF0Hz"], "studentF0Hz": full["_analysis"]["studentF0Hz"]},
                "alignment": {"studentFrameByTeacherFrame": full["_analysis"]["studentFrameByTeacherFrame"]}}
    baseline = score_baseline(artifact, 0.0, 999.0, 70.0)
    assert baseline["pitchAccuracyPercent"] == pytest.approx(full["pitchAccuracyPercent"], abs=6.0)


def test_baseline_without_alignment_is_none():
    assert score_baseline({"pitch": {"teacherF0Hz": [220.0]}}, 0.0, 1.0, 70.0) is None


def test_silent_segment_has_no_voiced_frames():
    teacher = recording([np.nan] * 100)
    sliced, origin = slice_recording(teacher, 0.5, 1.5)
    assert compare_recordings(sliced, recording([220.0] * 40), origin)["totalVoicedFrames"] == 0


@pytest.mark.parametrize("extra", [
    {"templateStartSec": 1.0},
    {"templateStartSec": 3.0, "templateEndSec": 3.0},
    {"baselineEvaluationId": "ev-1"},
    {"templateStartSec": -1.0, "templateEndSec": 2.0},
])
def test_invalid_segment_fields_are_rejected(extra):
    with pytest.raises(ValidationError):
        base_task(**extra)


def test_task_without_segment_is_unchanged():
    task = base_task()
    assert not task.is_segment and task.templateStartSec is None


def test_segment_task_is_accepted():
    task = base_task(templateStartSec=1.5, templateEndSec=4.0, baselineEvaluationId="ev-1")
    assert task.is_segment


# --- EvaluationService: segment branch ---------------------------------------------------------

from types import SimpleNamespace

from app.feedback.gemini_feedback import _build_prompt
from app.services.artifact_storage_service import ArtifactStorageService
from test_evaluation_quality import setup

TEACHER_LENGTH = 200


def segment_service(tmp_path, voiced_frames=10):
    service, sessions, _ = setup(tmp_path, 0.2)
    service.template_storage = ArtifactStorageService(tmp_path / "artifacts")
    spy = SimpleNamespace(origin_frame=None, teacher_length=None)
    callbacks = []

    async def analyze(path, instrument):
        return recording([220.0] * TEACHER_LENGTH)

    async def compare(teacher, student, origin_frame=0):
        spy.origin_frame = origin_frame
        spy.teacher_length = len(teacher["f0"])
        return {"overallScore": 81.0, "pitchAccuracyPercent": 85.0, "rhythmAccuracyPercent": 75.0,
                "totalVoicedFrames": voiced_frames, "teacherTempoBpm": 100.0, "studentTempoBpm": 90.0,
                "_analysis": {"teacherF0Hz": [220.0], "studentF0Hz": [220.0], "studentFrameByTeacherFrame": [0]}}

    async def send(task, job, status, *, code=None, reason=None, result=None):
        callbacks.append((job, status, result))
        return True

    service.rhythm_analysis_service = SimpleNamespace(analyze=analyze, compare=compare)
    service.callback_client = SimpleNamespace(send=send)
    return service, sessions, spy, callbacks


def store_baseline(service, sessions, evaluation_id, with_artifact=True):
    baseline_task = base_task(evaluationId=evaluation_id)
    artifact = None
    if with_artifact:
        document = {"pitch": {"teacherF0Hz": [220.0] * TEACHER_LENGTH, "studentF0Hz": [221.0] * TEACHER_LENGTH},
                    "alignment": {"studentFrameByTeacherFrame": list(range(TEACHER_LENGTH))}}
        artifact = service.template_storage.write_evaluation(evaluation_id, 1, document)
    with sessions() as session:
        record = service.repository.create_if_missing(session, baseline_task)
        service.repository.mark_completed(session, record, {"overallScore": 62}, artifact)


def final_result(callbacks):
    done = [entry for entry in callbacks if (entry[0], entry[1]) == ("feedback", "done")]
    assert len(done) == 1
    return done[0][2]


@pytest.mark.asyncio
async def test_segment_task_slices_template_and_scores_baseline(tmp_path):
    service, sessions, spy, callbacks = segment_service(tmp_path)
    store_baseline(service, sessions, "ev-original")

    await service.process_task(base_task(templateStartSec=2.0, templateEndSec=4.0, baselineEvaluationId="ev-original"))

    result = final_result(callbacks)
    assert result["segment"] == {"startSec": 2.0, "endSec": 4.0}
    assert set(result["baseline"]) == {"overallScore", "pitchAccuracyPercent", "rhythmAccuracyPercent"}
    assert result["baseline"]["pitchAccuracyPercent"] == 100.0
    assert spy.origin_frame == seconds_to_frame(2.0, TEACHER_LENGTH)
    assert spy.teacher_length == seconds_to_frame(4.0, TEACHER_LENGTH) - spy.origin_frame


@pytest.mark.asyncio
async def test_missing_baseline_artifact_still_completes(tmp_path):
    service, sessions, _, callbacks = segment_service(tmp_path)
    store_baseline(service, sessions, "ev-old", with_artifact=False)

    await service.process_task(base_task(templateStartSec=2.0, templateEndSec=4.0, baselineEvaluationId="ev-old"))

    assert final_result(callbacks)["baseline"] is None


@pytest.mark.asyncio
async def test_unknown_baseline_evaluation_still_completes(tmp_path):
    service, _, _, callbacks = segment_service(tmp_path)

    await service.process_task(base_task(templateStartSec=2.0, templateEndSec=4.0, baselineEvaluationId="missing"))

    assert final_result(callbacks)["baseline"] is None


@pytest.mark.asyncio
async def test_silent_segment_reports_null_scores(tmp_path):
    service, sessions, _, callbacks = segment_service(tmp_path, voiced_frames=0)
    store_baseline(service, sessions, "ev-original")

    await service.process_task(base_task(templateStartSec=2.0, templateEndSec=4.0, baselineEvaluationId="ev-original"))

    result = final_result(callbacks)
    assert result["overallScore"] is None and result["pitchAccuracyPercent"] is None
    assert result["rhythmAccuracyPercent"] is None


@pytest.mark.asyncio
async def test_full_piece_task_has_no_segment_keys(tmp_path):
    service, _, spy, callbacks = segment_service(tmp_path)

    await service.process_task(base_task())

    result = final_result(callbacks)
    assert "segment" not in result and "baseline" not in result
    assert spy.origin_frame == 0 and spy.teacher_length == TEACHER_LENGTH


def test_feedback_prompt_restricts_comment_to_the_segment():
    prompt = _build_prompt({"segment": {"startSec": 65.0, "endSec": 80.0}, "overallScore": None})
    assert "01:05–01:20" in prompt and "chỉ nhận xét đoạn này" in prompt
    assert "chỉ nhận xét đoạn này" not in _build_prompt({"overallScore": 80})
