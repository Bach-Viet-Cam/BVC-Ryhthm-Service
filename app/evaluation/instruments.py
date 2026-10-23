INSTRUMENTS: dict[str, dict[str, float | str]] = {
    "dan_bau": {"name": "Đàn Bầu", "fmin": 65.0, "fmax": 1200.0, "toleranceCents": 70.0},
    "dan_nhi": {"name": "Đàn Nhị", "fmin": 200.0, "fmax": 1500.0, "toleranceCents": 50.0},
    "dan_nguyet": {"name": "Đàn Nguyệt", "fmin": 130.0, "fmax": 1000.0, "toleranceCents": 50.0},
    "dan_tranh": {"name": "Đàn Tranh", "fmin": 130.0, "fmax": 2100.0, "toleranceCents": 50.0},
    "dan_ty_ba": {"name": "Đàn Tỳ Bà", "fmin": 100.0, "fmax": 1200.0, "toleranceCents": 50.0},
    "sao_truc": {"name": "Sáo Trúc", "fmin": 400.0, "fmax": 2500.0, "toleranceCents": 50.0},
}

FALLBACK_INSTRUMENT_ID = "khong_ho_tro"
FALLBACK_INSTRUMENT_SOURCE_ID = "dan_bau"

# Unsupported instruments are still analyzed with Đàn Bầu's acoustic settings.
INSTRUMENTS[FALLBACK_INSTRUMENT_ID] = {
    **INSTRUMENTS[FALLBACK_INSTRUMENT_SOURCE_ID],
    "name": "Không hỗ trợ",
}


def normalize_instrument_id(instrument_id: str) -> str:
    return instrument_id if instrument_id in INSTRUMENTS else FALLBACK_INSTRUMENT_ID


def get_instrument(instrument_id: str) -> dict[str, float | str]:
    return INSTRUMENTS[normalize_instrument_id(instrument_id)]
