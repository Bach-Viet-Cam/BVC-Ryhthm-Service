# Tiến độ — 2026-10-23-template-analysis-audio-quality.md

Baseline: 9 tests passed. Task 1: RED 18 → GREEN 18. Task 2: RED 16 → GREEN, gồm F0 khớp helper cũ. Task 3: RED thiếu quality/khóa → GREEN 14 evaluation tests. Task 4: RED 9 → GREEN 23 worker/evaluation tests. Task 5: RED 7 → GREEN 16 API/evaluation tests. Task 6: RED schema/lifecycle/config → GREEN 9 tests. Toàn bộ trước rà soát: 73 passed.

Ruling: triển khai trực tiếp trong checkout theo yêu cầu người dùng, không commit/push; giữ spec đã stage và pyc thay đổi có sẵn. Không tạo worktree mới để kết quả nằm trong project người dùng đang làm.
Ruling: kế hoạch đã tự rà soát và triển khai liên tục theo yêu cầu đã duyệt, không hỏi lại cách thực thi.
Ruling: thêm migration trong dự án Rhythm theo Alembic hiện có; quy tắc không tạo migration trong BVC-BE không áp dụng sang dự án Python này. Không chạy migration lên DB đang dùng.

Rà soát độc lập phát hiện decoder timeout/thiếu FFmpeg bị coi như lỗi nội dung vĩnh viễn. Test tái hiện: RED 3 trước khi sửa phân loại lỗi kỹ thuật/retry.
Tự rà soát và reviewer phát hiện mất dấu do PowerShell OutputEncoding, báo cáo im lặng thiếu metadata, tên fminHz/fmaxHz chưa khớp JSON. Đã có test RED cho cả ba; sửa nguồn UTF-8, metadata và contract.
Đã thêm kiểm thử phục hồi sau lỗi completion commit và lỗi atomic replace; các hành vi này đã chạy qua.

Giới hạn: Docker daemon chưa chạy. Compose hợp lệ, chưa có kiểm chứng PostgreSQL/RabbitMQ thật hoặc restart container/volume. Chưa khởi động hay thay đổi dịch vụ sản phẩm.
Final review: complete. Đã sửa tất cả lỗi quan trọng được phát hiện; không còn lỗi nhỏ bị hoãn từ lượt review này.

Final fixed: thông báo tiếng Việt — test_vietnamese_quality_message_keeps_accents RED → GREEN.
Final fixed: metadata/metrics cho audio im lặng — test_silence_report_keeps_duration_and_json_safe_zero_energy_metrics RED → GREEN.
Final fixed: tên fminHz/fmaxHz trong artifact — test_template_persists_main_audio_keys_and_real_artifact RED → GREEN.
Final fixed: decoder timeout/thiếu FFmpeg là lỗi kỹ thuật — test_decoder_infrastructure_errors_are_not_content_rejection và test_decoder_timeout_sends_retry_instead_of_permanent_quality_failure RED → GREEN.
Final added: phục hồi sau lỗi completion commit, atomic replace thất bại, HTTP thật chạy cùng hai handler queue, schema ORM khớp migration và sinh SQL PostgreSQL offline.

Kiểm chứng cuối: 84 passed, 31 cảnh báo deprecation từ FastAPI/Alembic/audioread; 46 file Python compile hợp lệ bằng compile() không ghi pyc; git diff --check qua; Docker Compose config với .env.example qua. Không đổi evaluate_pair/instruments hoặc contract EvaluationTask. Không chạy migration hay kết nối broker/DB sản phẩm. Không commit/push.

Các quyết định giữ nguyên: thực hiện trực tiếp tại checkout hiện tại, không tạo worktree/commit; có migration riêng trong dự án Rhythm; backend publisher và dùng artifact khi chấm vẫn là giai đoạn sau. Giới hạn kiểm chứng PostgreSQL/RabbitMQ/container thật được ghi rõ ở trên và README.

Điều chỉnh sau triển khai: chuyển năm ngưỡng quality sang app/constants/audio_quality.py; bỏ khỏi Settings, .env.example và Docker Compose. Giữ snapshot của job template đang retry. Đã thay năm test validation env không còn áp dụng bằng test env cũ không thay đổi verdict; bộ test hiện tại 80 passed. Compose config hợp lệ. Chạy kiểm thử từ source với pycache prefix tạm, không sửa/xóa các pyc có sẵn.
