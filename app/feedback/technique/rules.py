"""Lời khuyên chỉnh cao độ theo cơ chế phát âm của từng nhạc cụ.

Mỗi hàm nhận nốt MIDI của giáo viên và chiều lệch (True = học viên cao hơn), trả về lời khuyên kỹ thuật.
Các nốt dây buông (trong bảng tunes) và lỗi sai nốt được xử lý chung ở `diagnosis.py` trước khi tới đây.
"""

from collections.abc import Callable

from app.evaluation.notes import midi_to_name

C3, C4, E4, F4, G4, BB4, B4, C5, D5, E5, F5, FS5, G5, BB5, B5, C6 = 48, 60, 64, 65, 67, 70, 71, 72, 74, 76, 77, 78, 79, 82, 83, 84


def _between(low: int, high: int, *, low_open: bool, high_open: bool) -> Callable[[int], bool]:
    return lambda midi: (midi > low if low_open else midi >= low) and (midi < high if high_open else midi <= high)


# Đàn bầu: khoảng cần kéo căng cần đàn và khoảng cần nhấn cần đàn. Nốt nằm ở cả hai (Re5) ưu tiên kéo.
DAN_BAU_PULL = (
    _between(C4, E4, low_open=True, high_open=False),
    _between(G4, BB4, low_open=True, high_open=False),
    _between(C5, D5, low_open=True, high_open=False),
    _between(E5, F5, low_open=True, high_open=False),
    _between(G5, BB5, low_open=True, high_open=False),
)
DAN_BAU_PRESS = (
    _between(C3, C4, low_open=True, high_open=True),
    _between(F4, G4, low_open=False, high_open=True),
    _between(B4, C5, low_open=False, high_open=True),
    _between(D5, E5, low_open=False, high_open=True),
    _between(FS5, G5, low_open=False, high_open=True),
    _between(B5, C6, low_open=False, high_open=True),
)


def dan_bau(midi: int, sharp: bool) -> str:
    if any(contains(midi) for contains in DAN_BAU_PULL):
        return "Nốt này cần kéo căng cần đàn: " + ("kéo cần nhẹ tay hơn." if sharp else "kéo cần mạnh tay hơn.")
    if any(contains(midi) for contains in DAN_BAU_PRESS):
        return "Nốt này cần nhấn cần đàn: " + ("nhấn cần mạnh tay hơn." if sharp else "nhấn cần nhẹ tay hơn.")
    return "Nốt này không cần kéo hay nhấn cần đàn, chỉ cần gảy đúng điểm bồi âm và giữ yên cần đàn."


def dan_nhi(midi: int, sharp: bool) -> str:
    return "Dịch ngón tay lên hoặc bấm nhẹ ngón tay hơn." if sharp else "Dịch ngón tay xuống."


def fretted(midi: int, sharp: bool) -> str:
    """Đàn nguyệt và tỳ bà: có dây buông và phím đàn."""
    return "Bấm phím nhẹ tay hơn, không kéo dây." if sharp else "Bấm phím mạnh tay hơn."


def dan_tranh(midi: int, sharp: bool) -> str:
    """Các nốt ngoài dây ngũ cung (Fa, Si, nốt thăng/giáng) tạo ra bằng cách nhấn dây."""
    return "Nốt này tạo bằng cách nhấn dây: " + ("nhấn dây nhẹ hơn." if sharp else "nhấn dây mạnh hơn.")


def sao_truc(midi: int, sharp: bool) -> str:
    direction = "cao" if sharp else "thấp"
    return f"Nốt {midi_to_name(midi)} đang {direction}: xem lại cách đặt môi, lực hơi và bấm kín các lỗ cho nốt này."


def generic(midi: int, sharp: bool) -> str:
    return "Nốt đang cao hơn bản mẫu, hạ cao độ xuống." if sharp else "Nốt đang thấp hơn bản mẫu, nâng cao độ lên."


TECHNIQUE_RULES: dict[str, Callable[[int, bool], str]] = {
    "dan_bau": dan_bau,
    "dan_nhi": dan_nhi,
    "dan_nguyet": fretted,
    "dan_ty_ba": fretted,
    "dan_tranh": dan_tranh,
    "sao_truc": sao_truc,
}

# Sáo không có dây nên không có lỗi lên dây; đàn tranh so theo tên nốt vì mỗi bậc ngũ cung có dây riêng ở mọi quãng tám
NO_STRINGS = frozenset({"sao_truc"})
MATCH_TUNES_BY_PITCH_CLASS = frozenset({"dan_tranh"})
