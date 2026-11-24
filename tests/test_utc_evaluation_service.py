"""Unit tests for EvaluationService (UTC-REV-NNN). All collaborators are injected fakes."""
import json
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from app.audio.downloader import AudioDownloadError
from app.feedback.gemini_feedback import FeedbackGenerationError
from app.schemas.messages import EvaluationTask
from app.services import evaluation_service as module
from app.services.artifact_storage_service import ArtifactUnavailableError
from app.services.audio_quality_service import AudioProcessingError, AudioQualityError
from app.services.evaluation_service import EvaluationService, PermanentEvaluationError

TEACHER_LENGTH = 200


def make_task(**changes):
    values = dict(messageId="m-1", evaluationId="ev-1",
                  templateUrl="https://example.com/teacher.wav", studentUrl="https://example.com/student.wav",
                  instrumentId="dan_bau",
                  instrument=dict(name="Dan bau", fminHz=65, fmaxHz=1200, toleranceCents=70))
    values.update(changes)
    return EvaluationTask(**values)


def recording(f0):
    return {"instrumentId": "dan_bau", "instrument": "Dan bau", "f0": np.asarray(f0, dtype=float),
            "tempoBpm": 100.0, "toleranceCents": 70.0}


class FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class FakeRepo:
    def __init__(self, status="queued"):
        self.records = {}
        self.status = status
        self.calls = []

    def create_if_missing(self, session, task):
        self.calls.append("create_if_missing")
        return self.records.setdefault(task.evaluationId, SimpleNamespace(
            status=self.status, analysis_artifact_key=None, analysis_artifact_sha256=None))

    def get(self, session, evaluation_id):
        return self.records.get(evaluation_id)

    def mark_processing(self, session, record):
        self.calls.append("mark_processing")

    def save_quality(self, session, record, report):
        self.calls.append("save_quality")
        self.quality = report

    def mark_completed(self, session, record, result, artifact):
        self.calls.append("mark_completed")
        self.completed = (result, artifact)

    def mark_failed(self, session, record, code, message):
        self.calls.append("mark_failed")
        self.failed = (code, message)


class FakeQuality:
    def __init__(self, verdict="PASS"):
        self.verdict = verdict

    async def inspect(self, path):
        report = {"verdict": self.verdict, "warnings": []}
        if self.verdict == "REJECT":
            report["rejections"] = [{"code": "AUDIO_SILENT", "message": "Audio qua im"}]
        return report

    def ensure_acceptable(self, report):
        if report["verdict"] == "REJECT":
            raise AudioQualityError(report)


class Harness:
    """Builds a service with every collaborator replaced by a fake and records the interactions."""

    def __init__(self, tmp_path, monkeypatch, *, status="queued", verdict="PASS", voiced=10, with_analysis=True):
        self.events, self.downloads, self.calls, self.origin = [], [], [], None
        self.sessions = []
        self.repo = FakeRepo(status)

        def factory():
            session = FakeSession()
            self.sessions.append(session)
            return session

        self.service = EvaluationService(self.repo, factory, str(tmp_path / "work"),
                                         audio_service=SimpleNamespace(download=self.download),
                                         rhythm_analysis_service=SimpleNamespace(analyze=self.analyze, compare=self.compare,
                                                                                 analyze_template=self.analyze_template),
                                         feedback_service=SimpleNamespace(generate=self.feedback),
                                         callback_client=SimpleNamespace(send=self.send))
        self.service.quality_service = FakeQuality(verdict)
        self.service.template_storage = SimpleNamespace(read=lambda key, sha: b"{}",
                                                        write_evaluation=self.write_evaluation)
        self.service.template_repository = SimpleNamespace(get=lambda session, analysis_id: None)
        self.voiced, self.with_analysis = voiced, with_analysis
        self.artifact = {"key": "evaluations/ev-1/attempt-1/analysis.json", "sha256": "abc"}
        self.written = []
        self.template_error = None
        monkeypatch.setattr(module, "diagnose", lambda *args: [])

    async def download(self, url, directory):
        self.downloads.append(url)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "marker").write_text("x")
        self.work_directory = directory
        return directory / "audio.wav"

    async def analyze(self, path, instrument_id, instrument):
        self.calls.append("analyze")
        return recording([220.0] * TEACHER_LENGTH)

    async def analyze_template(self, template, instrument_id, instrument):
        self.calls.append("analyze_template")
        if self.template_error:
            raise self.template_error
        return recording([220.0] * TEACHER_LENGTH)

    async def compare(self, teacher, student, origin_frame=0):
        self.calls.append("compare")
        self.origin = origin_frame
        result = {"overallScore": 88.0, "pitchAccuracyPercent": 90.0, "rhythmAccuracyPercent": 85.0,
                  "totalVoicedFrames": self.voiced, "teacherTempoBpm": 100.0, "studentTempoBpm": 90.0}
        if self.with_analysis:
            result["_analysis"] = {"teacherF0Hz": [220.0], "studentF0Hz": [221.0], "studentFrameByTeacherFrame": [0]}
        return result

    async def feedback(self, result):
        self.calls.append("feedback")
        return "Tiep tuc luyen tap."

    async def send(self, task, job, status, *, code=None, reason=None, result=None):
        self.events.append((job, status, code, reason, result))

    def write_evaluation(self, evaluation_id, attempt, document):
        self.written.append((evaluation_id, attempt, document))
        return self.artifact

    @property
    def stages(self):
        return [(job, status) for job, status, *_ in self.events]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


# ---------------------------------------------------------------- process_task

@pytest.mark.asyncio
async def test_utc_rev_001_happy_path_reports_stages_in_order(harness):
    await harness.service.process_task(make_task())
    assert harness.stages == [("quality_check", "started"), ("quality_check", "done"),
                              ("audio_analysis", "started"), ("audio_analysis", "done"),
                              ("comparison", "started"), ("comparison", "done"),
                              ("feedback", "started"), ("feedback", "done")]
    final = harness.events[-1][4]
    assert final["overallScore"] == 88.0 and final["aiFeedback"] == "Tiep tuc luyen tap."
    assert final["studentAudioQuality"]["verdict"] == "PASS"
    assert "_analysis" not in final


@pytest.mark.asyncio
async def test_utc_rev_002_happy_path_persists_record_and_cleans_work_directory(harness):
    await harness.service.process_task(make_task())
    assert harness.repo.calls == ["create_if_missing", "mark_processing", "save_quality", "mark_completed"]
    assert harness.downloads == ["https://example.com/student.wav", "https://example.com/teacher.wav"]
    assert not harness.work_directory.exists()
    assert all(session.closed for session in harness.sessions)


@pytest.mark.asyncio
async def test_utc_rev_003_already_completed_record_is_idempotent(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, status="completed")
    await harness.service.process_task(make_task())
    assert harness.events == [] and harness.downloads == [] and harness.calls == []
    assert "mark_processing" not in harness.repo.calls and "mark_completed" not in harness.repo.calls
    assert harness.sessions[0].closed


@pytest.mark.asyncio
async def test_utc_rev_004_quality_rejection_notifies_rejected_and_reraises(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, verdict="REJECT")
    with pytest.raises(AudioQualityError) as info:
        await harness.service.process_task(make_task())
    assert info.value.code == "AUDIO_SILENT"
    assert harness.stages == [("quality_check", "started"), ("quality_check", "rejected")]
    assert harness.events[-1][2] == "AUDIO_SILENT" and harness.events[-1][3] == "Audio qua im"
    assert harness.calls == [] and "mark_completed" not in harness.repo.calls
    assert "save_quality" in harness.repo.calls


@pytest.mark.asyncio
async def test_utc_rev_005_download_error_propagates_and_cleans_up(harness):
    async def failing(url, directory):
        directory.mkdir(parents=True, exist_ok=True)
        raise AudioDownloadError("DOWNLOAD_FAILED", "khong tai duoc")
    harness.service.audio_service = SimpleNamespace(download=failing)
    with pytest.raises(AudioDownloadError):
        await harness.service.process_task(make_task())
    assert harness.stages == [("quality_check", "started")]
    assert harness.calls == []


@pytest.mark.asyncio
async def test_utc_rev_006_feedback_failure_propagates_without_completing(harness):
    async def failing(result):
        raise FeedbackGenerationError("FEEDBACK_FAILED", "gemini loi")
    harness.service.feedback_service = SimpleNamespace(generate=failing)
    with pytest.raises(FeedbackGenerationError):
        await harness.service.process_task(make_task())
    assert harness.stages[-1] == ("feedback", "started")
    assert "mark_completed" not in harness.repo.calls and harness.written == []


@pytest.mark.asyncio
async def test_utc_rev_007_analysis_artifact_is_written_and_linked(harness):
    await harness.service.process_task(make_task(attempt=2))
    result, artifact = harness.repo.completed
    assert artifact == harness.artifact
    assert result["analysisArtifactUrl"] == "/api/v1/evaluations/ev-1/analysis"
    evaluation_id, attempt, document = harness.written[0]
    assert (evaluation_id, attempt) == ("ev-1", 2) and document["templateAnalysisId"] is None


@pytest.mark.asyncio
async def test_utc_rev_008_artifact_write_failure_still_completes(harness):
    def broken(*args):
        raise ArtifactUnavailableError("disk full")
    harness.service.template_storage.write_evaluation = broken
    await harness.service.process_task(make_task())
    result, artifact = harness.repo.completed
    assert artifact is None and "analysisArtifactUrl" not in result
    assert harness.stages[-1] == ("feedback", "done")


@pytest.mark.asyncio
async def test_utc_rev_009_no_analysis_means_no_artifact(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, with_analysis=False)
    await harness.service.process_task(make_task())
    result, artifact = harness.repo.completed
    assert artifact is None and harness.written == [] and "analysisArtifactUrl" not in result


@pytest.mark.asyncio
async def test_utc_rev_010_technique_diagnoses_attached_only_when_present(harness, monkeypatch):
    monkeypatch.setattr(module, "diagnose", lambda *args: [{"note": "C4", "issue": "flat"}])
    await harness.service.process_task(make_task())
    assert harness.repo.completed[0]["techniqueDiagnoses"] == [{"note": "C4", "issue": "flat"}]
    monkeypatch.setattr(module, "diagnose", lambda *args: [])
    await harness.service.process_task(make_task(evaluationId="ev-2"))
    assert "techniqueDiagnoses" not in harness.repo.completed[0]


@pytest.mark.asyncio
async def test_utc_rev_011_progress_callback_failure_does_not_block_scoring(harness):
    async def broken(*args, **kwargs):
        raise RuntimeError("backend down")
    harness.service.callback_client = SimpleNamespace(send=broken)
    await harness.service.process_task(make_task())
    assert "mark_completed" in harness.repo.calls


# ---------------------------------------------------------------- template cache

def cache_record(**changes):
    values = dict(status="completed", instrument_code="dan_bau", artifact_key="k", artifact_sha256="s")
    values.update(changes)
    return SimpleNamespace(**values)


def cache_artifact(analysis_id, **changes):
    values = {"analysisId": str(analysis_id), "instrument": {"fminHz": 65, "fmaxHz": 1200}}
    values.update(changes)
    return values


def with_cache(harness, record, artifact):
    harness.service.template_repository = SimpleNamespace(get=lambda session, id_: record)
    harness.service.template_storage.read = lambda key, sha: json.dumps(artifact).encode()


@pytest.mark.asyncio
async def test_utc_rev_012_template_cache_hit_skips_template_download(harness):
    analysis_id = uuid4()
    with_cache(harness, cache_record(), cache_artifact(analysis_id))
    await harness.service.process_task(make_task(templateAnalysisId=analysis_id))
    assert harness.downloads == ["https://example.com/student.wav"]
    assert "analyze_template" in harness.calls
    assert harness.written[0][2]["templateAnalysisId"] == str(analysis_id)


@pytest.mark.asyncio
async def test_utc_rev_013_template_cache_miss_downloads_template(harness):
    await harness.service.process_task(make_task(templateAnalysisId=uuid4()))
    assert harness.downloads == ["https://example.com/student.wav", "https://example.com/teacher.wav"]
    assert "analyze_template" not in harness.calls
    assert harness.written[0][2]["templateAnalysisId"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [KeyError("f0Hz"), TypeError("bad"), ValueError("stale")])
async def test_utc_rev_014_corrupt_cached_template_falls_back_to_download(harness, error):
    analysis_id = uuid4()
    with_cache(harness, cache_record(), cache_artifact(analysis_id))
    harness.template_error = error
    await harness.service.process_task(make_task(templateAnalysisId=analysis_id))
    assert harness.downloads[-1] == "https://example.com/teacher.wav"
    assert harness.written[0][2]["templateAnalysisId"] is None
    assert harness.stages[-1] == ("feedback", "done")


# ---------------------------------------------------------------- segment

def segment_task(**changes):
    values = dict(templateStartSec=2.0, templateEndSec=4.0)
    values.update(changes)
    return make_task(**values)


def store_baseline(harness, with_artifact=True, status="completed"):
    document = {"pitch": {"teacherF0Hz": [220.0] * TEACHER_LENGTH, "studentF0Hz": [221.0] * TEACHER_LENGTH},
                "alignment": {"studentFrameByTeacherFrame": list(range(TEACHER_LENGTH))}}
    harness.repo.records["ev-original"] = SimpleNamespace(
        status=status, analysis_artifact_key="k" if with_artifact else None,
        analysis_artifact_sha256="s" if with_artifact else None)
    harness.service.template_storage.read = lambda key, sha: json.dumps(document).encode()


@pytest.mark.asyncio
async def test_utc_rev_015_segment_task_slices_template_and_scores_baseline(harness):
    store_baseline(harness)
    await harness.service.process_task(segment_task(baselineEvaluationId="ev-original"))
    result = harness.events[-1][4]
    assert result["segment"] == {"startSec": 2.0, "endSec": 4.0}
    assert set(result["baseline"]) == {"overallScore", "pitchAccuracyPercent", "rhythmAccuracyPercent"}
    assert result["baseline"]["pitchAccuracyPercent"] == 100.0
    assert harness.origin > 0
    assert result["pitch"]["teacherF0Hz"] == [220.0] and result["pitch"]["sampleRate"] == 22050
    assert harness.written == [] and harness.repo.completed[1] is None


@pytest.mark.asyncio
async def test_utc_rev_016_segment_task_without_baseline_id_has_null_baseline(harness):
    await harness.service.process_task(segment_task())
    assert harness.events[-1][4]["baseline"] is None


@pytest.mark.asyncio
async def test_utc_rev_017_segment_task_with_unknown_baseline_still_completes(harness):
    await harness.service.process_task(segment_task(baselineEvaluationId="missing"))
    assert harness.events[-1][4]["baseline"] is None
    assert harness.stages[-1] == ("feedback", "done")


@pytest.mark.asyncio
async def test_utc_rev_018_silent_segment_reports_null_scores(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, voiced=0)
    store_baseline(harness)
    await harness.service.process_task(segment_task(baselineEvaluationId="ev-original"))
    result = harness.events[-1][4]
    assert result["overallScore"] is None and result["pitchAccuracyPercent"] is None
    assert result["rhythmAccuracyPercent"] is None


@pytest.mark.asyncio
async def test_utc_rev_019_full_piece_task_has_no_segment_keys(harness):
    await harness.service.process_task(make_task())
    result = harness.events[-1][4]
    assert "segment" not in result and "baseline" not in result and "pitch" not in result
    assert harness.origin == 0


def test_utc_rev_020_attach_segment_nulls_baseline_when_baseline_has_no_voiced_frames(harness):
    harness.service._read_baseline = lambda task, tol: {"overallScore": 50.0, "pitchAccuracyPercent": 60.0,
                                                        "rhythmAccuracyPercent": 40.0, "totalVoicedFrames": 0}
    result = {"totalVoicedFrames": 5, "overallScore": 80.0}
    harness.service._attach_segment(segment_task(baselineEvaluationId="b"), result, 70.0)
    assert result["baseline"] == {"overallScore": None, "pitchAccuracyPercent": None, "rhythmAccuracyPercent": None}
    assert result["overallScore"] == 80.0


def test_utc_rev_021_attach_segment_keeps_baseline_scores_and_drops_voiced_count(harness):
    harness.service._read_baseline = lambda task, tol: {"overallScore": 50.0, "pitchAccuracyPercent": 60.0,
                                                        "rhythmAccuracyPercent": 40.0, "totalVoicedFrames": 12}
    result = {"totalVoicedFrames": 5}
    harness.service._attach_segment(segment_task(baselineEvaluationId="b"), result, 70.0)
    assert result["baseline"] == {"overallScore": 50.0, "pitchAccuracyPercent": 60.0, "rhythmAccuracyPercent": 40.0}


# ---------------------------------------------------------------- _read_baseline

@pytest.mark.parametrize("case", [
    pytest.param("missing", id="UTC-REV-022-missing-record"),
    pytest.param("not-completed", id="UTC-REV-023-not-completed"),
    pytest.param("no-key", id="UTC-REV-024-no-artifact-key"),
])
def test_utc_rev_baseline_unusable_record_returns_none(harness, case):
    if case == "not-completed":
        store_baseline(harness, status="processing")
    elif case == "no-key":
        store_baseline(harness, with_artifact=False)
    assert harness.service._read_baseline(segment_task(baselineEvaluationId="ev-original"), 70.0) is None


@pytest.mark.parametrize("reader_error", [ArtifactUnavailableError("gone"), OSError("io"), UnicodeError("enc")])
def test_utc_rev_025_baseline_read_errors_return_none(harness, reader_error):
    store_baseline(harness)

    def broken(key, sha):
        raise reader_error
    harness.service.template_storage.read = broken
    assert harness.service._read_baseline(segment_task(baselineEvaluationId="ev-original"), 70.0) is None


def test_utc_rev_026_baseline_with_corrupt_json_returns_none(harness):
    store_baseline(harness)
    harness.service.template_storage.read = lambda key, sha: b"not json"
    assert harness.service._read_baseline(segment_task(baselineEvaluationId="ev-original"), 70.0) is None


# ---------------------------------------------------------------- _read_template_cache

def read_cache(harness, record, artifact, analysis_id):
    with_cache(harness, record, artifact)
    return harness.service._read_template_cache(make_task(templateAnalysisId=analysis_id))


def test_utc_rev_027_cache_without_analysis_id_returns_none(harness):
    assert harness.service._read_template_cache(make_task()) is None


def test_utc_rev_028_cache_valid_artifact_is_returned(harness):
    analysis_id = uuid4()
    artifact = read_cache(harness, cache_record(), cache_artifact(analysis_id), analysis_id)
    assert artifact["analysisId"] == str(analysis_id)


@pytest.mark.parametrize("record_changes", [
    pytest.param(None, id="UTC-REV-029-missing-record"),
    pytest.param(dict(status="failed"), id="UTC-REV-030-not-completed"),
    pytest.param(dict(instrument_code="dan_nhi"), id="UTC-REV-031-other-instrument"),
    pytest.param(dict(artifact_sha256=None), id="UTC-REV-032-no-sha"),
])
def test_utc_rev_cache_unusable_record_returns_none(harness, record_changes):
    analysis_id = uuid4()
    record = None if record_changes is None else cache_record(**record_changes)
    assert read_cache(harness, record, cache_artifact(analysis_id), analysis_id) is None


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda a: {**a, "analysisId": "other"}, id="UTC-REV-033-analysis-id-mismatch"),
    pytest.param(lambda a: {**a, "instrument": {"fminHz": 65, "fmaxHz": 1047}}, id="UTC-REV-034-range-changed"),
    pytest.param(lambda a: {**a, "instrument": "dan_bau"}, id="UTC-REV-035-instrument-not-dict"),
])
def test_utc_rev_cache_stale_artifact_returns_none(harness, mutate):
    analysis_id = uuid4()
    artifact = mutate(cache_artifact(analysis_id))
    assert read_cache(harness, cache_record(), artifact, analysis_id) is None


def test_utc_rev_036_cache_storage_error_returns_none(harness):
    analysis_id = uuid4()
    with_cache(harness, cache_record(), cache_artifact(analysis_id))

    def broken(key, sha):
        raise ArtifactUnavailableError("sha mismatch")
    harness.service.template_storage.read = broken
    assert harness.service._read_template_cache(make_task(templateAnalysisId=analysis_id)) is None


# ---------------------------------------------------------------- _diagnose / _write_analysis

def test_utc_rev_037_diagnose_returns_diagnose_result(monkeypatch):
    seen = []
    monkeypatch.setattr(module, "diagnose", lambda *args: seen.append(args) or [{"note": "A4"}])
    assert EvaluationService._diagnose(make_task(), {"x": 1}, 7) == [{"note": "A4"}]
    assert seen[0][0] == {"x": 1} and seen[0][1] == "dan_bau" and seen[0][3] == 7


@pytest.mark.parametrize("error", [KeyError("a"), TypeError("b"), ValueError("c"), IndexError("d")])
def test_utc_rev_038_diagnose_swallows_expected_errors(monkeypatch, error):
    def broken(*args):
        raise error
    monkeypatch.setattr(module, "diagnose", broken)
    assert EvaluationService._diagnose(make_task(), None, 0) == []


def test_utc_rev_039_diagnose_does_not_swallow_unexpected_errors(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")
    monkeypatch.setattr(module, "diagnose", broken)
    with pytest.raises(RuntimeError):
        EvaluationService._diagnose(make_task(), None, 0)


@pytest.mark.asyncio
async def test_utc_rev_040_write_analysis_without_analysis_returns_none(harness):
    assert await harness.service._write_analysis(make_task(), None, {}, False) is None
    assert harness.written == []


@pytest.mark.asyncio
async def test_utc_rev_041_write_analysis_builds_document_and_returns_artifact(harness):
    analysis_id = uuid4()
    analysis = {"teacherF0Hz": [1.0], "studentF0Hz": [2.0], "studentFrameByTeacherFrame": [0]}
    result = {"teacherTempoBpm": 100.0, "studentTempoBpm": 90.0}
    artifact = await harness.service._write_analysis(make_task(attempt=3, templateAnalysisId=analysis_id), analysis, result, True)
    assert artifact == harness.artifact
    _, attempt, document = harness.written[0]
    assert attempt == 3 and document["schemaVersion"] == 1 and document["templateAnalysisId"] == str(analysis_id)
    assert document["rhythm"] == {"teacherTempoBpm": 100.0, "studentTempoBpm": 90.0}
    assert document["alignment"] == {"studentFrameByTeacherFrame": [0]}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ArtifactUnavailableError("x"), OSError("y"), ValueError("z"), TypeError("w")])
async def test_utc_rev_042_write_analysis_swallows_storage_errors(harness, error):
    def broken(*args):
        raise error
    harness.service.template_storage.write_evaluation = broken
    analysis = {"teacherF0Hz": [1.0], "studentF0Hz": [2.0], "studentFrameByTeacherFrame": [0]}
    assert await harness.service._write_analysis(make_task(), analysis, {}, False) is None


# ---------------------------------------------------------------- report_final_failure / _notify / record_failure

@pytest.mark.asyncio
async def test_utc_rev_043_report_final_failure_defaults_to_quality_check_stage(harness):
    await harness.service.report_final_failure(make_task(), "X_CODE", "ly do")
    assert harness.events == [("quality_check", "failed", "X_CODE", "ly do", None)]


@pytest.mark.asyncio
async def test_utc_rev_044_report_final_failure_uses_last_active_stage(harness):
    async def failing(result):
        raise FeedbackGenerationError("FEEDBACK_FAILED", "loi")
    harness.service.feedback_service = SimpleNamespace(generate=failing)
    with pytest.raises(FeedbackGenerationError):
        await harness.service.process_task(make_task())
    await harness.service.report_final_failure(make_task(), "FEEDBACK_FAILED", "loi")
    assert harness.events[-1][:3] == ("feedback", "failed", "FEEDBACK_FAILED")


@pytest.mark.asyncio
async def test_utc_rev_045_notify_swallows_callback_errors(harness):
    async def broken(*args, **kwargs):
        raise ConnectionError("down")
    harness.service.callback_client = SimpleNamespace(send=broken)
    assert await harness.service._notify(make_task(), "feedback", "done", result={}) is None


@pytest.mark.asyncio
async def test_utc_rev_046_notify_forwards_code_reason_and_result(harness):
    await harness.service._notify(make_task(), "comparison", "rejected", code="C", reason="R", result={"a": 1})
    assert harness.events == [("comparison", "rejected", "C", "R", {"a": 1})]


def test_utc_rev_047_record_failure_marks_record_failed_and_closes_session(harness):
    harness.service.record_failure(make_task(), "SOME_CODE", "thong bao")
    assert harness.repo.calls == ["create_if_missing", "mark_failed"]
    assert harness.repo.failed == ("SOME_CODE", "thong bao")
    assert harness.sessions[-1].closed


def test_utc_rev_048_record_failure_closes_session_when_repository_raises(harness):
    def broken(session, record, code, message):
        raise RuntimeError("db down")
    harness.repo.mark_failed = broken
    with pytest.raises(RuntimeError):
        harness.service.record_failure(make_task(), "C", "m")
    assert harness.sessions[-1].closed


# ---------------------------------------------------------------- process_direct_task

@pytest.mark.asyncio
async def test_utc_rev_049_direct_task_success_records_no_failure(harness):
    await harness.service.process_direct_task(make_task())
    assert "mark_failed" not in harness.repo.calls and "mark_completed" in harness.repo.calls


@pytest.mark.asyncio
async def test_utc_rev_050_direct_quality_rejection_records_failure_without_failed_callback(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch, verdict="REJECT")
    await harness.service.process_direct_task(make_task())
    assert harness.repo.failed == ("AUDIO_SILENT", "Audio qua im")
    assert [status for _, status, *_ in harness.events] == ["started", "rejected"]


@pytest.mark.asyncio
async def test_utc_rev_051_direct_feedback_error_reports_and_records(harness):
    async def failing(result):
        raise FeedbackGenerationError("FEEDBACK_FAILED", "gemini loi")
    harness.service.feedback_service = SimpleNamespace(generate=failing)
    await harness.service.process_direct_task(make_task())
    assert harness.events[-1][:4] == ("feedback", "failed", "FEEDBACK_FAILED", "gemini loi")
    assert harness.repo.failed == ("FEEDBACK_FAILED", "gemini loi")


@pytest.mark.asyncio
async def test_utc_rev_052_direct_audio_processing_error_reports_and_records(harness):
    async def failing(path, instrument_id, instrument):
        raise AudioProcessingError("AUDIO_DECODE_FAILED", "khong giai ma")
    harness.service.rhythm_analysis_service.analyze = failing
    await harness.service.process_direct_task(make_task())
    assert harness.events[-1][:3] == ("audio_analysis", "failed", "AUDIO_DECODE_FAILED")
    assert harness.repo.failed == ("AUDIO_DECODE_FAILED", "khong giai ma")


@pytest.mark.asyncio
async def test_utc_rev_053_direct_download_error_reports_and_records(harness):
    async def failing(url, directory):
        raise AudioDownloadError("DOWNLOAD_FAILED", "khong tai duoc", retryable=True)
    harness.service.audio_service = SimpleNamespace(download=failing)
    await harness.service.process_direct_task(make_task())
    assert harness.events[-1][:4] == ("quality_check", "failed", "DOWNLOAD_FAILED", "khong tai duoc")
    assert harness.repo.failed == ("DOWNLOAD_FAILED", "khong tai duoc")


@pytest.mark.asyncio
async def test_utc_rev_054_direct_value_error_maps_to_evaluation_failed(harness):
    async def failing(teacher, student, origin_frame=0):
        raise ValueError("Hai ban thu phai dung cung nhac cu.")
    harness.service.rhythm_analysis_service.compare = failing
    await harness.service.process_direct_task(make_task())
    assert harness.events[-1][:3] == ("comparison", "failed", "EVALUATION_FAILED")
    assert harness.repo.failed == ("EVALUATION_FAILED", "Hai ban thu phai dung cung nhac cu.")


@pytest.mark.asyncio
async def test_utc_rev_055_direct_permanent_error_keeps_its_code(harness):
    async def failing(path, instrument_id, instrument):
        raise PermanentEvaluationError("TEMPLATE_INVALID", "mau khong hop le")
    harness.service.rhythm_analysis_service.analyze = failing
    await harness.service.process_direct_task(make_task())
    assert harness.repo.failed == ("TEMPLATE_INVALID", "mau khong hop le")
    assert harness.events[-1][2] == "TEMPLATE_INVALID"


@pytest.mark.asyncio
async def test_utc_rev_056_direct_unexpected_error_maps_to_internal_error(harness):
    async def failing(path, instrument_id, instrument):
        raise RuntimeError("secret internal detail")
    harness.service.rhythm_analysis_service.analyze = failing
    await harness.service.process_direct_task(make_task())
    assert harness.events[-1][2:4] == ("INTERNAL_ERROR", "Internal evaluation error")
    assert harness.repo.failed == ("INTERNAL_ERROR", "Lỗi nội bộ khi phân tích audio")


def test_utc_rev_057_permanent_evaluation_error_exposes_code_and_message():
    error = PermanentEvaluationError("CODE_1", "thong bao")
    assert error.code == "CODE_1" and str(error) == "thong bao" and isinstance(error, Exception)
