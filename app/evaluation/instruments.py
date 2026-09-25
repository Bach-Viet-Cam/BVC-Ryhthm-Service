INSTRUMENTS: dict[str, dict[str, float | str]] = {
    "dan_bau": {"name": "Đàn Bầu", "fmin": 65.0, "fmax": 1200.0, "toleranceCents": 70.0},
    "dan_nhi": {"name": "Đàn Nhị", "fmin": 200.0, "fmax": 1500.0, "toleranceCents": 50.0},
    "dan_nguyet": {"name": "Đàn Nguyệt", "fmin": 130.0, "fmax": 1000.0, "toleranceCents": 50.0},
    "dan_tranh": {"name": "Đàn Tranh", "fmin": 130.0, "fmax": 2100.0, "toleranceCents": 50.0},
    "dan_ty_ba": {"name": "Đàn Tỳ Bà", "fmin": 100.0, "fmax": 1200.0, "toleranceCents": 50.0},
    "sao_truc": {"name": "Sáo Trúc", "fmin": 400.0, "fmax": 2500.0, "toleranceCents": 50.0},
}


def get_instrument(instrument_id: str) -> dict[str, float | str]:
    try:
        return INSTRUMENTS[instrument_id]
    except KeyError as error:
        raise ValueError(f"Nhạc cụ không được hỗ trợ: {instrument_id}") from error
