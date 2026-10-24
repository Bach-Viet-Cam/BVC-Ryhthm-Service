import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from app.schemas.template_messages import TemplateAnalysisTask


class ArtifactUnavailableError(OSError):
    code = "ARTIFACT_UNAVAILABLE"


class ArtifactStorageService:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _resolve(self, key: str) -> Path:
        relative = PurePosixPath(key)
        if not key or relative.is_absolute() or "\\" in key or any(p in {"..", "."} for p in key.split("/")):
            raise ArtifactUnavailableError("Đường dẫn kết quả phân tích không hợp lệ.")
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise ArtifactUnavailableError("Đường dẫn kết quả nằm ngoài thư mục lưu trữ.")
        return target

    def write(self, task: TemplateAnalysisTask, document: dict[str, Any]) -> dict[str, Any]:
        return self._write(f"template-analyses/{task.sheetMusicId}/{task.mediaFileId}/{task.analysisId}/result.json", document)

    def write_evaluation(self, evaluation_id: str, attempt: int, document: dict[str, Any]) -> dict[str, Any]:
        # Evaluation ids come from the queue message, so they must not be able to shape the path
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", evaluation_id):
            raise ArtifactUnavailableError("Mã đánh giá không hợp lệ để lưu file phân tích.")
        return self._write(f"evaluations/{evaluation_id}/attempt-{attempt}/analysis.json", document)

    def _write(self, key: str, document: dict[str, Any]) -> dict[str, Any]:
        target = self._resolve(key)
        content = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".result-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"key": key, "sha256": hashlib.sha256(content).hexdigest(), "sizeBytes": len(content)}

    def read(self, key: str, checksum: str) -> bytes:
        try:
            content = self._resolve(key).read_bytes()
            if hashlib.sha256(content).hexdigest() != checksum:
                raise ArtifactUnavailableError("File kết quả phân tích không khớp checksum.")
            json.loads(content, parse_constant=self._invalid_constant)
            return content
        except (OSError, ValueError, UnicodeError) as error:
            raise ArtifactUnavailableError("Chưa thể đọc file kết quả phân tích. Vui lòng kiểm tra lưu trữ.") from error

    @staticmethod
    def _invalid_constant(value: str) -> None:
        raise ValueError("File JSON chứa giá trị số không hợp lệ.")
