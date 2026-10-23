"""Measure recording quality without changing the source signal."""

import asyncio
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from app.constants import audio_quality


class AudioQualityError(ValueError):
    def __init__(self, report: dict[str, Any]) -> None:
        rejection = report["rejections"][0]
        super().__init__(rejection["message"])
        self.code = rejection["code"]
        self.report = report


class AudioProcessingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AudioQualityService:
    def policy(self) -> dict[str, Any]:
        return {
            "policyVersion": audio_quality.POLICY_VERSION,
            "lowLevelDbfs": audio_quality.LOW_LEVEL_DBFS,
            "silenceDbfs": audio_quality.SILENCE_DBFS,
            "silenceRatioWarn": audio_quality.SILENCE_RATIO_WARN,
            "clipAmplitude": audio_quality.CLIP_AMPLITUDE,
            "clipRatioWarn": audio_quality.CLIP_RATIO_WARN,
            "frameDurationMs": audio_quality.FRAME_DURATION_MS,
        }

    async def inspect(self, path: Path, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._inspect, path, policy or self.policy())

    def _inspect(self, path: Path, policy: dict[str, Any]) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError("Không tìm thấy file audio đã tải.")
        try:
            samples, sample_rate = self._decode(path)
        except (sf.LibsndfileError, ValueError, subprocess.CalledProcessError):
            return self._reject(policy, "AUDIO_DECODE_FAILED", "Không thể giải mã file audio. Vui lòng kiểm tra bản thu.")
        return self.measure(samples, sample_rate, policy)

    @staticmethod
    def _decode(path: Path) -> tuple[np.ndarray, int]:
        try:
            return sf.read(path, dtype="float64", always_2d=True)
        except sf.LibsndfileError:
            # M4A needs FFmpeg, already installed by the service Dockerfile.
            # Do not set sample rate, channels, normalization or audio filters.
            with tempfile.TemporaryDirectory(prefix="bvc-quality-") as directory:
                decoded = Path(directory) / "decoded.wav"
                try:
                    subprocess.run(
                        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                         "-vn", "-c:a", "pcm_f64le", str(decoded)],
                        check=True, capture_output=True, timeout=60,
                    )
                except FileNotFoundError as error:
                    raise AudioProcessingError("AUDIO_DECODER_UNAVAILABLE", "Bộ giải mã audio chưa sẵn sàng. Vui lòng kiểm tra cấu hình dịch vụ.") from error
                except subprocess.TimeoutExpired as error:
                    raise AudioProcessingError("AUDIO_DECODE_TIMEOUT", "Quá thời gian giải mã audio. Vui lòng thử lại sau.") from error
                return sf.read(decoded, dtype="float64", always_2d=True)

    def measure(self, samples: np.ndarray, sample_rate: int, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        policy = dict(policy or self.policy())
        signal = np.asarray(samples, dtype=np.float64)
        if signal.size == 0:
            return self._reject(policy, "AUDIO_EMPTY", "File không có mẫu âm thanh.")
        if signal.ndim == 1:
            signal = signal[:, None]
        if signal.ndim != 2 or sample_rate <= 0:
            return self._reject(policy, "AUDIO_DECODE_FAILED", "Thông tin audio không hợp lệ.")
        if not np.isfinite(signal).all():
            return self._reject(policy, "AUDIO_NON_FINITE", "File chứa dữ liệu âm thanh không hợp lệ.")
        peak = float(np.max(np.abs(signal)))
        if peak == 0:
            report = self._reject(policy, "AUDIO_SILENT", "Bản thu hoàn toàn im lặng. Vui lòng thu âm lại.")
            report["audio"] = {"sampleRate": int(sample_rate), "channels": int(signal.shape[1]),
                               "sampleCount": int(signal.shape[0]), "durationSec": signal.shape[0] / sample_rate}
            report["metrics"] = {"rmsLinear": 0.0, "rmsDbfs": None, "peakAmplitude": 0.0,
                                 "silenceRatio": 1.0, "clippingRatio": 0.0}
            return report

        rms = peak * float(np.sqrt(np.mean(np.square(signal / peak))))
        level = 20 * math.log10(rms)
        silence_ratio = self._silence_ratio(signal, sample_rate, policy)
        clipping_ratio = float(np.mean(np.abs(signal) >= policy["clipAmplitude"]))
        warnings = []
        checks = (
            (level < policy["lowLevelDbfs"], "AUDIO_LOW_LEVEL", "Âm lượng bản thu nhỏ, hãy kiểm tra khoảng cách tới micro."),
            (silence_ratio > policy["silenceRatioWarn"], "AUDIO_MOSTLY_SILENT", "Bản thu có nhiều khoảng lặng."),
            (clipping_ratio > policy["clipRatioWarn"], "AUDIO_POSSIBLE_CLIPPING", "Bản thu có dấu hiệu clipping, hãy giảm mức thu âm."),
        )
        for triggered, code, message in checks:
            if triggered:
                warnings.append({"code": code, "message": message})
        return {
            "policyVersion": policy["policyVersion"], "policy": policy,
            "verdict": "WARN" if warnings else "PASS", "warnings": warnings, "rejections": [],
            "audio": {"sampleRate": int(sample_rate), "channels": int(signal.shape[1]),
                      "sampleCount": int(signal.shape[0]), "durationSec": signal.shape[0] / sample_rate},
            "metrics": {"rmsLinear": rms, "rmsDbfs": level, "peakAmplitude": peak,
                        "silenceRatio": silence_ratio, "clippingRatio": clipping_ratio},
        }

    @staticmethod
    def _silence_ratio(signal: np.ndarray, sample_rate: int, policy: dict[str, Any]) -> float:
        frame_size = max(1, round(sample_rate * policy["frameDurationMs"] / 1000))
        silence_level = 10 ** (policy["silenceDbfs"] / 20)
        silent_samples = 0
        for start in range(0, len(signal), frame_size):
            frame = signal[start:start + frame_size]
            channel_rms = np.sqrt(np.mean(np.square(frame), axis=0))
            if np.all(channel_rms < silence_level):
                silent_samples += len(frame)
        return silent_samples / len(signal)

    @staticmethod
    def _reject(policy: dict[str, Any], code: str, message: str) -> dict[str, Any]:
        return {"policyVersion": policy["policyVersion"], "policy": policy,
                "verdict": "REJECT", "warnings": [], "rejections": [{"code": code, "message": message}],
                "audio": None, "metrics": None}

    @staticmethod
    def ensure_acceptable(report: dict[str, Any]) -> None:
        if report["verdict"] == "REJECT":
            raise AudioQualityError(report)
