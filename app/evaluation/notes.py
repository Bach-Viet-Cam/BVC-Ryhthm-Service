"""Chuyển tần số sang nốt nhạc (MIDI, tên nốt kiểu Đô Rê Mi) để chẩn đoán lỗi theo nốt."""

import math

NOTE_NAMES = ("Do", "Do#", "Re", "Re#", "Mi", "Fa", "Fa#", "Sol", "Sol#", "La", "La#", "Si")


def hz_to_midi_float(hz: float) -> float:
    return 69.0 + 12.0 * math.log2(hz / 440.0)


def hz_to_midi(hz: float) -> int:
    return round(hz_to_midi_float(hz))


def midi_to_name(midi: int) -> str:
    """MIDI 60 là Do4 (C4)."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def pitch_class(midi: int) -> int:
    return midi % 12
