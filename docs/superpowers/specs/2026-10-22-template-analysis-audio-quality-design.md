# Phân tích trước template và kiểm tra chất lượng audio trong Rhythm Service

Ngày: 2026-10-22 (Asia/Saigon).
Cập nhật: 2026-10-23, dùng một worker và bổ sung khóa liên kết MAIN/audio mẫu.
Trạng thái: Đã duyệt các phần thiết kế trong trao đổi; tài liệu tổng hợp chờ người dùng rà soát trước khi lập kế hoạch triển khai.

## 1. Mục tiêu và phạm vi

Rhythm Service nhận một job chứa URL audio template và nhạc cụ, kiểm tra chất lượng bản thu, trích dữ liệu phân tích và lưu thành file JSON. Database chỉ giữ tham chiếu file cùng metadata vận hành. Dữ liệu này chuẩn bị cho việc tái sử dụng template khi so sánh ở giai đoạn sau.

Template là bản thu một nhạc cụ chơi giai điệu đơn, không có nhạc đệm. Nghiệp vụ chỉ áp dụng cho bản MAIN. Phía gửi job sau này chịu trách nhiệm chọn MAIN; Rhythm không truy vấn database backend để xác minh loại bản nhạc.

Bổ sung kiểm tra chất lượng student vào pipeline so sánh hiện tại. Cả RabbitMQ và HTTP POST hiện có đều đi qua bước kiểm tra này vì chúng dùng chung EvaluationService.

Phạm vi triển khai nằm trong BVC-Ryhthm-Service. Không sửa BVC-BE hoặc frontend; không xây publisher template, callback backend hoặc tích hợp assignment/submission. Không thay thuật toán pYIN/DTW, công thức điểm, prompt Gemini hoặc cách phân tích template trong evaluate_pair. Pipeline so sánh vẫn tải và phân tích lại template; chưa sử dụng file JSON mới.

## 2. Hiện trạng và lựa chọn kiến trúc

Service dùng FastAPI, aio-pika, SQLAlchemy, PostgreSQL, Alembic, librosa và soundfile. Worker hiện nhận EvaluationTask trên rhythm.evaluate.v1; API nội bộ dùng Bearer key. AudioService tải file với giới hạn dung lượng, timeout và allowlist host. EvaluationService điều phối tải hai file, phân tích, Gemini và lưu kết quả.

Các phương án đã xem xét:

| Phương án | Đánh đổi |
| --- | --- |
| Queue và consumer template riêng trong Rhythm | Contract và retry riêng, ít tác động pipeline hiện tại; thêm queue và consumer |
| Dùng chung queue với jobType | Ít queue nhưng thay contract và điều hướng worker hiện tại |
| Tách dịch vụ template/quality | Triển khai độc lập nhưng tăng vận hành ngoài nhu cầu hiện tại |

Chọn queue template riêng và giữ queue so sánh hiện tại. Chỉ chạy một worker trong một process/container Rhythm. Worker nhận hai loại job và xử lý tuần tự: tại một thời điểm chỉ có một job template hoặc so sánh được thực thi.

Các consumer dùng prefetch_count = 1. Hai pipeline cùng dùng một asyncio.Lock trong process để không chạy đồng thời. API HTTP POST evaluation hiện có cũng đi qua cùng khóa này, vì BackgroundTasks có thể chạy trong lúc consumer đang xử lý. Lấy khóa một lần ở đầu pipeline, trước khi đọc/cập nhật trạng thái; hàm gọi bên ngoài không lấy khóa lồng nhau. GET trạng thái và đọc artifact vẫn phục vụ bình thường.

Tái sử dụng downloader và danh mục nhạc cụ; giữ contract EvaluationTask. Chạy tác vụ CPU và I/O file đồng bộ ngoài event loop để API không bị chặn. Không xây cơ chế chia việc, khóa phân tán hoặc tranh quyền xử lý giữa các process.

## 3. Contract job template

Queue durable: rhythm.template.analyze.v1.
Dead-letter queue durable: rhythm.template.analyze.dlq.

```json
{
  "messageId": "0acaf37c-4ee8-42c5-9dfd-ed87a5b3d0ab",
  "analysisId": "5820ae1c-5102-4df9-91fb-dfa02bc734ee",
  "sheetMusicId": "d067917d-eb88-473c-8dab-771965f20575",
  "mediaFileId": "fbda6f08-83af-42ac-a2ec-587a99af0e40",
  "mediaUrl": "https://storage.example/template.wav",
  "instrumentCode": "dan_bau",
  "requestedAt": "2026-10-22T08:00:00Z"
}
```

messageId, analysisId, sheetMusicId và mediaFileId là UUID. sheetMusicId là ID của SheetMusic loại MAIN trong backend; mediaFileId là ID MediaFile của audio mẫu, tức referenceAudioId của MAIN, không phải sheetFileId của file PDF/ảnh bản nhạc. Hai ID này là tham chiếu ngoài do caller cung cấp, không tạo foreign key xuyên database. Rhythm chỉ validate định dạng; caller chịu trách nhiệm kiểm tra đúng MAIN và audio tương ứng trước khi gửi job. mediaUrl là URL HTTP/HTTPS, tuân thủ downloader và allowlist hiện có. requestedAt là thời gian có múi giờ. Payload mới không nhận các trường ngoài contract.

instrumentCode nhận đúng code trong app/evaluation/instruments.py: dan_bau, dan_nhi, dan_nguyet, dan_tranh, dan_ty_ba, sao_truc. Không nhận UUID Instrument của backend và không tự suy đoán nhạc cụ từ audio. Chuyển đổi code backend, gồm trường hợp TY_BA sang dan_ty_ba, nằm ngoài giai đoạn này.

Rhythm chụp cấu hình fmin, fmax và tolerance của danh mục hiện tại khi tạo bản ghi. Retry dùng lại cấu hình và phiên bản đã ghi nhận, không âm thầm chuyển sang cấu hình mới. Job mới được yêu cầu khi đổi audio, nhạc cụ hoặc phiên bản phân tích. Caller dùng URL nội dung ổn định và analysisId mới khi thay audio; cùng URL không chứng minh cùng nội dung.

## 4. Pipeline template

1. Validate message; tạo hoặc tìm bản ghi theo analysisId và đối chiếu đầu vào.
2. Trong lượt xử lý tuần tự, ghi processing và thời điểm bắt đầu.
3. Tải audio vào thư mục tạm riêng của lần xử lý; giữ giới hạn 25 MiB và quy tắc tải hiện có.
4. Tính SHA-256 trên bytes file nguồn; đo quality trước chuẩn hóa âm lượng, resample hoặc chuyển mono.
5. REJECT: lưu failed, mã lỗi và báo cáo quality; bỏ qua trích cao độ và Gemini.
6. PASS hoặc WARN: trích F0 và tempo bằng cùng phương pháp hiện tại; ghi cấu hình thực tế.
7. Ghi file JSON hoàn chỉnh vào storage bền vững.
8. Commit storage key, checksum file JSON, metadata và trạng thái completed vào database.
9. ACK sau commit; luôn dọn thư mục tạm và nhả khóa trong process khi pipeline kết thúc.

Không gọi Gemini cho template vì chưa có bài student để nhận xét. Không dùng kết quả quality để tự động sửa waveform.

## 5. Dữ liệu JSON và storage

Artifact có schemaVersion = 1, analysisVersion = template-pyin-v1 và policyVersion = audio-quality-v1. analysisVersion là phiên bản hành vi; JSON vẫn lưu tham số và phiên bản thư viện thực tế để truy vết.

Các nhóm dữ liệu bắt buộc:

| Nhóm | Nội dung |
| --- | --- |
| Định danh | analysisId, sheetMusicId, mediaFileId, schemaVersion, analysisVersion, thời điểm tạo |
| Nguồn | mediaUrl, sourceSha256, dung lượng file nguồn |
| Nhạc cụ | instrumentCode, tên và snapshot fminHz/fmaxHz/toleranceCents |
| Audio | Thời lượng, sample rate/kênh nguồn, sample rate phân tích |
| Trích cao độ | method = pyin, sampleRate = 22050, hopLength = 512, frameLength = 2048, center = true, f0Hz và voicedFlags cùng chiều dài |
| Nhịp | Tempo ước lượng; null nếu không có giá trị hữu hạn |
| Quality | Verdict, số liệu, cảnh báo, lý do từ chối và cấu hình đã dùng |
| Môi trường | Phiên bản librosa/numpy và tham số trích xuất thực tế |

Không lưu từng timestamp lặp lại nếu có thể suy ra từ frame index: thời gian khung i là i * hopLength / sampleRate giây với cấu hình center nêu trên. f0Hz dùng null khi không xác định được cao độ; voicedFlags được suy ra từ F0 hữu hạn. Không coi đây là xác suất voicing. File không chứa NaN hoặc Infinity và dùng JSON UTF-8 chuẩn.

Dùng local artifact storage có persistent volume. Root mặc định trong container: /data/rhythm-artifacts; độc lập temp_directory. Database lưu key tương đối do service sinh, không lưu đường dẫn tuyệt đối hoặc hostname cố định.

Key có dạng template-analyses/{sheetMusicId}/{mediaFileId}/{analysisId}/result.json. Các ID được validate là UUID; không dùng tên file hoặc đường dẫn từ payload. Key chuẩn hóa phải nằm trong artifact root. Không cần phân thư mục theo lần xử lý vì worker chạy tuần tự.

Ghi file tạm cùng filesystem, flush và đóng file rồi đổi tên nguyên tử sang result.json. Chỉ công bố completed sau khi file tồn tại hoàn chỉnh. Job completed không ghi lại file. Nếu job chưa completed được retry, worker có thể ghi lại cùng key bằng cách thay file nguyên tử.

Nếu file đã ghi nhưng commit DB thất bại, job vẫn chưa completed. Khi broker giao lại message, worker xử lý lại và ghi file hoàn chỉnh trước khi commit. Luôn cố gắng dọn file tạm khi pipeline kết thúc. Giai đoạn này không xây tác vụ dọn artifact mồ côi riêng; file còn lại sau sự cố có thể được kiểm tra và dọn thủ công khi worker đã dừng.

Docker Compose mount một named volume vào artifact root của container Rhythm. API và worker cùng process sử dụng volume này. Chỉ triển khai một instance Rhythm; chưa thiết kế storage chia sẻ giữa nhiều host.

## 6. Database

Tạo model template_analyses riêng trong database Rhythm. Các trường chính:

- analysis_id unique, message_id, sheet_music_id, media_file_id, media_url, instrument_code, requested_at.
- Đối chiếu sheet_music_id, media_file_id, media_url và instrument_code đã lưu để phát hiện cùng analysisId nhưng khác đầu vào. messageId/requestedAt không làm thay đổi định danh nghiệp vụ của analysisId.
- analysis_version, instrument_config, quality_policy_version và snapshot cấu hình quality. Đây là metadata cấu hình nhỏ.
- status: queued, processing, completed hoặc failed.
- source_sha256, artifact_key nullable, artifact_sha256 nullable, artifact_size_bytes nullable.
- quality_report nullable: báo cáo nhỏ để đọc trạng thái hoặc giải thích thất bại; không chứa waveform, chuỗi F0 hay dữ liệu phân tích lớn.
- error_code, error_message, created_at, started_at, completed_at.

Không lưu toàn bộ artifact trong JSON database. source_sha256 và cấu hình cho phép xác minh đúng nội dung và phiên bản; chưa xây cache tự động giữa các analysisId khác nhau. Thêm index tra cứu theo sheet_music_id, media_file_id, instrument_code, analysis_version và created_at. Không đặt unique cho nhóm này vì cùng MAIN/audio có thể có nhiều lần phân tích với analysisId khác nhau.

Tích hợp model với Base, repository và cơ chế quản lý schema hiện có của Rhythm. Không sửa Prisma hoặc schema backend. Repository đọc/cập nhật trạng thái bình thường trong pipeline tuần tự; analysis_id vẫn có unique constraint để không tạo hai bản ghi cho cùng job.

### 6.1 Tra cứu template từ bài MAIN

sheetMusicId là khóa liên kết nghiệp vụ với MAIN; mediaFileId xác định phiên bản audio mẫu đang dùng. analysisId tiếp tục phân biệt từng lần phân tích, artifact_key chỉ xác định vị trí file JSON.

Luồng tích hợp backend ở giai đoạn sau:

1. Học sinh chọn piece hoặc làm bài tập gắn với piece.
2. Backend xác định instrumentId từ lớp/bài tập đang làm; với luyện tự do, dùng nhạc cụ đã chọn và kiểm tra quyền/ngữ cảnh phù hợp. Không dùng tùy tiện lớp đầu tiên nếu học sinh thuộc nhiều lớp.
3. Backend gọi logic tương đương SheetMusicRepository.findMain(pieceId, instrumentId) để lấy MAIN hiện hành, kiểm tra quyền truy cập và lấy main.id cùng main.referenceAudioId. Không có MAIN hoặc audio mẫu thì báo chưa có template để so sánh.
4. Tra cứu Rhythm bằng sheetMusicId = main.id, mediaFileId = main.referenceAudioId, instrumentCode đã chuyển đổi và analysisVersion được hỗ trợ.
5. Chỉ dùng kết quả completed khớp đầy đủ các khóa trên; lấy analysisId và đọc artifact. Sau này job so sánh có thể tham chiếu analysisId này, nhưng chưa đổi payload/thuật toán so sánh trong giai đoạn hiện tại.

Không dùng pieceId làm khóa duy nhất vì một piece có một MAIN cho mỗi nhạc cụ. Không chỉ tra bằng sheetMusicId vì ID MAIN giữ nguyên khi thay audio. Thay audio phải tạo MediaFile mới và job mới; kết quả của mediaFileId cũ không tự trở thành template hiện hành. Caller vẫn chịu trách nhiệm cung cấp URL nội dung ổn định và dữ liệu liên kết chính xác.

Thiết kế này chuẩn bị khóa và API tra cứu trong Rhythm; chưa triển khai các bước giải quyết piece/lớp/MAIN hoặc publisher trong backend. Rhythm không tự suy ra sheetMusicId/mediaFileId chỉ từ pieceId và không truy vấn database backend.

## 7. Quality audio

Một AudioQualityService dùng chung cho template và student. Giải mã ở sample rate nguồn, giữ các kênh khi đo; không áp dụng normalization, denoise, trim hoặc biến đổi âm lượng. Khi tệp có nhiều kênh, đo trên các kênh nguồn để tránh mất tín hiệu do trộn kênh. Bước so sánh vẫn dùng cách load mono hiện tại.

Đầu ra gồm verdict PASS/WARN/REJECT, policyVersion, metadata nguồn, metrics, warnings, rejections và ngưỡng thực tế đã dùng. Mã lỗi/cảnh báo ổn định; message giải thích bằng tiếng Việt có dấu.

### 7.1 Điều kiện từ chối

| Mã | Điều kiện |
| --- | --- |
| AUDIO_DECODE_FAILED | Không giải mã được nội dung audio |
| AUDIO_EMPTY | Giải mã không có mẫu âm thanh |
| AUDIO_NON_FINITE | Có mẫu NaN hoặc Infinity |
| AUDIO_SILENT | Tất cả mẫu ở tất cả kênh bằng 0 |

Các lỗi tải file hoặc vượt giới hạn dung lượng tiếp tục dùng downloader hiện tại. Các điều kiện quality là lỗi vĩnh viễn đối với nội dung này, không retry. Toàn im lặng được kiểm tra sau empty/non-finite để mã lỗi nhất quán.

### 7.2 Cảnh báo ban đầu

| Mã | Quy tắc mặc định |
| --- | --- |
| AUDIO_LOW_LEVEL | RMS toàn bản thu dưới -45 dBFS |
| AUDIO_MOSTLY_SILENT | Tỷ lệ khung có RMS dưới -60 dBFS lớn hơn 0.80 |
| AUDIO_POSSIBLE_CLIPPING | Tỷ lệ mẫu có abs(sample) >= 0.999 lớn hơn 0.01 |

RMS toàn bản thu lấy căn trung bình bình phương trên tất cả mẫu/kênh. dBFS = 20 * log10(RMS), với tham chiếu biên độ 1. Không ghi -Infinity vào JSON; trường mức bằng null khi RMS bằng 0 và lưu thêm rmsLinear = 0.

Đo khoảng lặng bằng cửa sổ 50 ms không chồng lấn ở sample rate nguồn. Khung cuối dùng số mẫu thực tế, không đệm thêm số 0. Một khung là im lặng khi mọi kênh đều có RMS dưới ngưỡng. Tỷ lệ khung im lặng được tính có trọng số theo số mẫu thực tế của từng khung để phần cuối ngắn không làm lệch tỷ lệ. Clipping ratio tính trên tổng số mẫu của tất cả kênh. Có thể trả nhiều cảnh báo cùng lúc.

REJECT ưu tiên cao hơn WARN; có ít nhất một cảnh báo và không có rejection thì WARN; còn lại PASS. PASS và WARN đều tiếp tục xử lý. Chỉ clipping ratio là dấu hiệu nghi clipping, không khẳng định nguyên nhân méo âm. Các ngưỡng này là mặc định ban đầu, chưa được hiệu chỉnh trên dữ liệu thực tế.

Không chặn dựa trên sai nốt, sai nhịp, ít F0 hợp lệ, không nhận diện được nhạc cụ hoặc SNR ước lượng. Không cam kết phát hiện mọi bản thu không dùng được; bản thu gần im lặng nhưng không bằng 0 hiện chỉ bị cảnh báo.

## 8. Tích hợp quality với so sánh hiện tại

EvaluationService tiếp tục tải template và student như hiện tại, sau đó kiểm tra student trước RhythmAnalysisService.evaluate. Không thêm bước quality cho template cũ trong luồng này; template mới được kiểm tra bởi pipeline riêng.

Bổ sung cột student_quality_report nullable vào model RhythmEvaluation để lưu báo cáo quality nhỏ cho cả PASS, WARN và REJECT. Không thay schema database backend. PASS/WARN: gọi evaluate_pair với hai đường dẫn và instrumentId như cũ, rồi Gemini và lưu kết quả; kèm bản sao báo cáo tại result_json.studentAudioQuality để đọc nội bộ, còn các trường điểm giữ nguyên. REJECT: không gọi evaluate_pair/Gemini; lưu failed, error_code/error_message tương ứng và result_json = null. Không đặt báo cáo đơn lẻ vào result_json khi failed vì schema backend yêu cầu đủ các trường điểm mỗi khi result khác null.

API Rhythm tiếp tục trả cấu trúc evaluationId/status/result/error. BVC-BE hiện lọc những trường bổ sung trong result nên chưa hiển thị studentAudioQuality; mã lỗi và thông báo REJECT vẫn theo schema error hiện có. Không thay public backend schema trong giai đoạn này.

Giữ cách xử lý repeated evaluationId, retry và ACK hiện tại của worker so sánh; bổ sung phân loại lỗi quality thành lỗi vĩnh viễn. Hai pipeline dùng khóa trong process đã nêu ở mục 2 để bảo đảm mỗi lần chỉ thực thi một job.

## 9. Job template trùng, restart và retry cơ bản

analysisId là định danh lần phân tích. Worker tìm bản ghi bằng analysisId và đối chiếu sheetMusicId/mediaFileId/mediaUrl/instrumentCode trong lượt xử lý tuần tự:

- Cùng ID nhưng khác đầu vào: chuyển message xung đột sang DLQ, giữ bản ghi và file gốc.
- Cùng ID, cùng đầu vào và completed: ACK, không phân tích lại hoặc ghi đè file.
- Cùng ID, cùng đầu vào và failed cuối cùng: ACK; muốn chạy lại thì gửi analysisId mới.
- queued hoặc processing: xử lý tiếp bằng cách chạy lại pipeline từ đầu, giữ snapshot cấu hình đã lưu.

Không cần xử lý tranh chấp vì chỉ có một worker và một job được thực thi tại mỗi thời điểm. processing còn lại sau khi process chết được xử lý lại khi RabbitMQ giao lại message chưa ACK. Chỉ có dòng processing trong DB mà không còn message thì worker không tự quét hoặc tạo job mới; cần kiểm tra và gửi lại job có cùng đầu vào.

Dùng retry cùng kiểu worker hiện tại, không thêm retry queue hoặc cơ chế TTL: tăng x-retry-count rồi publish lại vào queue template chính. Retry tối đa theo max_retries hiện có, mặc định 3 lần sau lần đầu. Chỉ ACK message gốc sau khi broker xác nhận nhận message retry. Nếu publish retry hoặc lưu kết quả/lỗi cuối cùng thất bại, không ACK như đã hoàn tất.

Lỗi mạng, HTTP 429/5xx và lỗi hạ tầng tạm thời được retry có giới hạn. Payload sai, nhạc cụ không hỗ trợ và lỗi nội dung/quality không retry. Hết lượt phải lưu failed với mã lỗi cụ thể. Giới hạn này áp dụng retry do ứng dụng gửi; khi process chết trước ACK, RabbitMQ có thể giao lại message và job có thể phải chạy lại.

Payload không hợp lệ được reject(requeue=false) sang DLQ. Nhạc cụ không hỗ trợ trong payload hợp lệ được lưu failed với mã UNSUPPORTED_INSTRUMENT rồi ACK. Không tự đăng ký consumer no_ack để đọc/xóa message DLQ mới. Giữ hành vi DLQ cũ trong phạm vi này.

## 10. API đọc kết quả nội bộ

Dùng cùng Bearer internal key hiện có:

- GET /api/v1/template-analyses/lookup?sheetMusicId={id}&mediaFileId={id}&instrumentCode={code}&analysisVersion={version}: tra cứu một kết quả theo MAIN/audio/nhạc cụ/phiên bản. Bốn tham số đều bắt buộc. Chọn bản ghi mới nhất theo created_at giảm dần, rồi analysis_id giảm dần để thứ tự xác định. Không có bản ghi khớp trả 404; bản ghi mới nhất chưa completed trả 409 với status/error, không âm thầm lấy bản completed cũ hơn. Khi completed, trả analysisId, các khóa liên kết và đường dẫn API tương đối đọc artifact. Đăng ký route lookup trước route động {analysisId}.
- GET /api/v1/template-analyses/{analysisId}: metadata, status, error, quality, sheetMusicId và mediaFileId; khi completed có đường dẫn API tương đối để đọc artifact.
- GET /api/v1/template-analyses/{analysisId}/artifact: trả file JSON application/json theo storage key trong DB. Không cho caller truyền đường dẫn file tùy ý.

Không có analysisId: 404. Artifact chưa completed: 409. DB báo completed nhưng file thiếu hoặc checksum sai: 503 với mã ARTIFACT_UNAVAILABLE, ghi log để khắc phục; không tự phân tích lại và không trả completed giả với artifact rỗng. Response lỗi mới có code và message tiếng Việt. Không đổi contract API evaluation cũ.

Không thêm POST template qua HTTP, publisher backend hoặc callback. Khi backend được tích hợp ở giai đoạn sau, nó giải quyết MAIN/audio hiện hành và dùng các endpoint trên để tra cứu, đọc status và artifact.

## 11. Cấu hình và vòng đời

Mọi biến mới theo tiền tố RHYTHM_SERVICE_, khai báo qua Settings và .env.example; không sửa .env thực tế:

| Hậu tố biến | Mặc định |
| --- | --- |
| TEMPLATE_TASK_QUEUE | rhythm.template.analyze.v1 |
| TEMPLATE_DEAD_LETTER_QUEUE | rhythm.template.analyze.dlq |
| ARTIFACT_ROOT | /data/rhythm-artifacts |

Năm ngưỡng quality đặt trong `app/constants/audio_quality.py`, không dùng env:

| Constant | Giá trị |
| --- | --- |
| LOW_LEVEL_DBFS | -45 |
| SILENCE_DBFS | -60 |
| SILENCE_RATIO_WARN | 0.80 |
| CLIP_AMPLITUDE | 0.999 |
| CLIP_RATIO_WARN | 0.01 |

`POLICY_VERSION` và `FRAME_DURATION_MS` cũng nằm trong file này. Tăng phiên bản khi thay hành vi. Các biến quality cũ trong môi trường không còn ảnh hưởng service.

Validate cấu hình hạ tầng lúc khởi động. Số lượt retry dùng max_retries hiện có. Job template giữ snapshot cấu hình quality qua các lần retry. Luồng evaluation hiện tại dùng cấu hình tại lần kiểm tra và ghi cấu hình đó trong student_quality_report; không thay quy tắc retry/repeated ID của evaluation. Chính sách thay đổi hành vi phải tăng policyVersion.

main.run_service khởi động một worker nhận cả hai queue cùng API, dùng chung một khóa xử lý trong process. Chạy một instance/container, không bật nhiều process phục vụ HTTP. Theo dõi task nền để lỗi worker không bị bỏ qua trong khi API vẫn báo hoạt động bình thường. Khi shutdown, dừng nhận message mới, đóng kết nối và để message chưa ACK được broker giao lại. Không giữ database session trong lúc tải file, đo quality hoặc chạy pYIN.

## 12. Kiểm thử và tiêu chí nghiệm thu

1. Job template hợp lệ lưu đúng sheetMusicId/mediaFileId trong DB và artifact JSON, không gọi Gemini; F0 khớp cách trích hiện tại trên cùng audio/cấu hình.
2. JSON không chứa NaN/Infinity; f0Hz/voicedFlags cùng chiều dài; checksum nguồn và artifact được kiểm chứng.
3. Audio tổng hợp bao phủ zero, empty, non-finite, âm nhỏ, khoảng lặng, clipping, đa kênh và các giá trị sát ngưỡng. Đo quality không làm thay đổi tín hiệu nguồn.
4. Quality PASS/WARN cho phép chấm; REJECT không gọi evaluate/Gemini, lưu failed có mã lỗi và result_json = null; báo cáo được lưu ở student_quality_report. HTTP và RabbitMQ evaluation có cùng hành vi quality.
5. Trên audio đạt quality, điểm và luồng gọi evaluate_pair giữ hành vi cũ; template vẫn được tải/trích lại, chưa dùng artifact mới.
6. Test duplicate template completed, xung đột cùng ID và xử lý lại processing sau restart. Kiểm tra job template, job so sánh và HTTP POST dùng chung khóa: số job đang thực thi luôn tối đa một; không bị khóa lồng nhau.
7. Test retry lỗi tải tạm thời, hết lượt, payload sai, nhạc cụ không hỗ trợ và ACK sau persistence/publisher confirm. Test DB lỗi không bị ACK như thành công.
8. Test file ghi dở, commit DB thất bại, ghi lại file khi retry, artifact thiếu/hỏng và dọn file tạm sau khi kết thúc pipeline.
9. API lookup/status/artifact yêu cầu internal key, mã HTTP đúng và không cho đọc file ngoài artifact root. Lookup phân biệt MAIN/audio/nhạc cụ/phiên bản, trả bản mới nhất theo thứ tự xác định; không lấy kết quả audio cũ hoặc fallback về completed cũ khi job mới chưa sẵn sàng. Thiếu tham số lookup trả lỗi validation; route lookup không bị route {analysisId} bắt nhầm.
10. Kiểm tra tích hợp thực tế với PostgreSQL/RabbitMQ riêng cho test, persistent volume và restart container. Không dùng database hoặc queue đang phục vụ người dùng cho test phá dữ liệu.
11. Chạy lại bộ test evaluation hiện có; bổ sung dependency quality giả trong các test chỉ kiểm tra orchestration, dùng audio thật cho test quality và hồi quy thuật toán.
12. BVC-BE và frontend không thay đổi; queue mới có thể kiểm chứng bằng publisher test, không cần xây publisher sản phẩm.

## 13. Giới hạn giai đoạn này

Các ngưỡng quality cần hiệu chỉnh bằng bản thu thực tế của từng nhạc cụ. Không tự động chọn ngưỡng riêng khi chưa có dữ liệu kiểm chứng. Không kiểm tra chất lượng biểu diễn và không tự sửa audio.

Đã có sheetMusicId/mediaFileId trong contract Rhythm để liên kết artifact với MAIN và audio mẫu. Chưa triển khai tích hợp bài nộp/MAIN trong backend hay thay thuật toán so sánh. Thay hoặc xóa template ở backend chưa tự hủy/xóa artifact. Chưa xây tác vụ dọn artifact mồ côi tự động.

Thiết kế chỉ hỗ trợ một worker trong một process/container, mỗi lần một job. Chưa tái sử dụng kết quả giữa các analysisId và chưa tối ưu bộ nhớ cho các bản thu dài. Giữ giới hạn dung lượng tải hiện có; file nén nhỏ không bảo đảm chi phí giải mã hoặc pYIN nhỏ.

## 14. Rà soát tài liệu

Đã đối chiếu contract job, trạng thái, phạm vi MAIN, cấu hình nhạc cụ, dữ liệu JSON, quality, API và giới hạn tương thích. Đã phân biệt lỗi chất lượng bản thu với lỗi biểu diễn, dữ liệu bền vững với thư mục tạm, và thay đổi pipeline với thay đổi thuật toán chấm.

Đã đơn giản hóa tài liệu theo yêu cầu ngày 2026-10-23: một worker xử lý tuần tự, không có cơ chế tranh quyền xử lý phân tán. Chỉ tài liệu được tạo/cập nhật trong bước brainstorming. Chưa viết code triển khai, chưa chạy migration, chưa sửa dịch vụ đang chạy và chưa commit. Người dùng rà soát tài liệu này trước khi chuyển sang lập kế hoạch triển khai theo skill brainstorming.
