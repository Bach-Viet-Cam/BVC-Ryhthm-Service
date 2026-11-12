from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.instrument import InstrumentConfig
from app.schemas.messages import EvaluationTask
from app.schemas.template_messages import TemplateAnalysisTask

BAU = dict(name="Đàn bầu", fminHz=65, fmaxHz=1047, toleranceCents=35)


def evaluation_values(**changes):
    values = dict(messageId=str(uuid4()), evaluationId=str(uuid4()), templateUrl="https://example.com/t.wav",
                  studentUrl="https://example.com/s.wav", instrumentId="DBA103", instrument=BAU)
    values.update(changes)
    return values


def template_values(**changes):
    values = dict(messageId=str(uuid4()), analysisId=str(uuid4()), sheetMusicId=str(uuid4()), mediaFileId=str(uuid4()),
                  mediaUrl="https://example.com/t.wav", instrumentCode="DBA103", requestedAt="2026-10-23T00:00:00Z",
                  instrument=BAU)
    values.update(changes)
    return values


def test_instrument_config_accepts_be_values():
    assert InstrumentConfig(**BAU).to_stored() == {"name": "Đàn bầu", "fmin": 65.0, "fmax": 1047.0, "toleranceCents": 35.0}


def test_instrument_config_rejects_inverted_range():
    with pytest.raises(ValidationError):
        InstrumentConfig(**{**BAU, "fminHz": 1047, "fmaxHz": 65})


def test_instrument_config_rejects_non_positive_tolerance():
    with pytest.raises(ValidationError):
        InstrumentConfig(**{**BAU, "toleranceCents": 0})


def test_evaluation_task_requires_instrument_block():
    values = evaluation_values()
    del values["instrument"]
    with pytest.raises(ValidationError):
        EvaluationTask(**values)


def test_task_keeps_instrument_code_unchanged():
    assert EvaluationTask(**evaluation_values()).instrumentId == "DBA103"
    assert TemplateAnalysisTask(**template_values()).instrumentCode == "DBA103"


def test_template_task_requires_instrument_block():
    values = template_values()
    del values["instrument"]
    with pytest.raises(ValidationError):
        TemplateAnalysisTask(**values)
