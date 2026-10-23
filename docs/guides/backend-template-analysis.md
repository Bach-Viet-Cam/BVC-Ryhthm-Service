# BE guide: gửi template MAIN sang Rhythm để phân tích trước

Tài liệu mô tả contract Rhythm đang hỗ trợ. Phần publisher và đồng bộ trạng thái MAIN trong BE cần được nối theo hướng dẫn này; chúng chưa được triển khai trong đợt thay đổi Rhythm.

Sau khi nhận job, Rhythm tự tải audio, kiểm tra quality, trích F0/tempo và lưu file JSON cùng metadata. BE cần lưu ID yêu cầu và chủ động đọc API để biết kết quả. Publisher confirm chỉ xác nhận broker nhận message, không xác nhận phân tích thành công. Hiện không có callback hoặc queue trả kết quả về BE.

## 1. Khi nào gửi job và lấy dữ liệu từ đâu

Gửi khi MAIN có audio mẫu mới, thay audio mẫu, đổi nhạc cụ hoặc cần phân tích lại. Audio mẫu là bản thu một nhạc cụ chơi giai điệu đơn, không nhạc đệm. Chỉ gửi sau khi file đã được upload/confirm thành công và quan hệ MAIN/audio đã được lưu trong DB BE.

| Trường job | Nguồn dữ liệu trong BE |
| --- | --- |
| sheetMusicId | `SheetMusic.id` của bản có `kind = MAIN` |
| mediaFileId | `MAIN.referenceAudioId`, tức ID MediaFile của audio mẫu |
| mediaUrl | URL tải của `MAIN.referenceAudio`, từ storage bằng `storageKey` |
| instrumentCode | Chuyển code của `MAIN.instrument` sang code Rhythm ở bảng dưới |
| analysisId | UUID mới do BE sinh cho lần phân tích |
| messageId | UUID do BE sinh cho message |
| requestedAt | Thời gian có múi giờ, ví dụ `new Date().toISOString()` |

`mediaFileId` không phải `sheetFileId` của PDF/ảnh. URL phải tuyệt đối, HTTP/HTTPS, tải trực tiếp từ môi trường worker; downloader không theo redirect và không gửi cookie/Bearer của người dùng. Mức giới hạn phía Rhythm mặc định 25 MiB. Nếu bật allowlist host thì storage host phải nằm trong allowlist. URL cần còn hiệu lực đủ lâu để chờ queue, tải và retry; giữ nội dung file ổn định cho cùng mediaFileId.

BE đang có `MusicLibraryDTO.toReadUrl()` và `StorageService.driver.createReadUrl(storageKey)` để tạo URL. Cần bảo đảm hostname/port phù hợp với nơi worker chạy: `localhost` trong container là chính container đó. Khi thay file, giữ file cũ đủ lâu nếu vẫn có job đang tải nó.

| Code catalog BE | instrumentCode gửi Rhythm |
| --- | --- |
| DAN_BAU | dan_bau |
| DAN_NHI, nếu có trong catalog | dan_nhi |
| DAN_NGUYET, nếu có trong catalog | dan_nguyet |
| DAN_TRANH | dan_tranh |
| TY_BA | dan_ty_ba |
| SAO_TRUC | sao_truc |

Cần mapping tường minh; chuyển chữ thường riêng lẻ sẽ sai trường hợp `TY_BA`. Không gửi UUID Instrument hoặc cấu hình fmin/fmax từ BE; Rhythm dùng danh mục nhạc cụ của chính nó.

Nếu đầu vào nghiệp vụ chỉ có pieceId, BE xác định nhạc cụ từ lớp/bài tập hoặc lựa chọn luyện tập, rồi tìm `findMain(pieceId, instrumentId)` để lấy MAIN/audio tương ứng. Một piece có thể có MAIN riêng cho từng nhạc cụ. Rhythm không tự truy vấn DB BE để suy ra các ID này.

## 2. Queue và payload

BE dùng `RABBITMQ_URL`; Rhythm dùng `RHYTHM_SERVICE_RABBITMQ_URL`. Hai bên phải kết nối cùng broker/vhost.

- Queue: `rhythm.template.analyze.v1`.
- Dead-letter queue: `rhythm.template.analyze.dlq`.
- Dùng default exchange, routing key bằng tên queue; gửi JSON UTF-8.
- Cả hai queue durable. Nếu BE khai báo queue, phải dùng đúng các arguments bên dưới. Nếu đổi tên qua cấu hình Rhythm, BE phải dùng tên tương ứng.

```typescript
await channel.assertQueue('rhythm.template.analyze.dlq', { durable: true });
await channel.assertQueue('rhythm.template.analyze.v1', {
  durable: true,
  arguments: {
    'x-dead-letter-exchange': '',
    'x-dead-letter-routing-key': 'rhythm.template.analyze.dlq'
  }
});
```

Payload hợp lệ gồm đúng bảy trường sau:

```json
{
  "messageId": "0acaf37c-4ee8-42c5-9dfd-ed87a5b3d0ab",
  "analysisId": "5820ae1c-5102-4df9-91fb-dfa02bc734ee",
  "sheetMusicId": "d067917d-eb88-473c-8dab-771965f20575",
  "mediaFileId": "fbda6f08-83af-42ac-a2ec-587a99af0e40",
  "mediaUrl": "https://storage.example/template.wav",
  "instrumentCode": "dan_bau",
  "requestedAt": "2026-10-23T00:00:00Z"
}
```

Các ID phải là UUID, requestedAt phải có múi giờ. Schema từ chối trường dư. Không thêm pieceId, callbackUrl, sheetUrl hoặc instrument object vào payload này. Contract cũ trong comment TODO của `AnalysisService` chưa phải contract đang chạy.

Khi publish, dùng ConfirmChannel với `persistent: true`, `mandatory: true`, `contentType: application/json`; AMQP messageId bằng payload.messageId và correlationId bằng analysisId. Chờ publisher confirm, đồng thời xử lý message bị return, timeout và mất kết nối. `sendToQueue()` trả boolean cho backpressure; `await sendToQueue()` không phải cách chờ broker confirm hoặc kết quả AI. Tham khảo cơ chế callback confirm/return hiện có của `RhythmPublisherService`, nhưng chọn queue template thay vì queue so sánh `rhythm.evaluate.v1`.

## 3. BE cần làm gì sau khi gửi

1. Trước publish, lưu analysisId vào `SheetMusic.analysisJobId`, đặt `analysisStatus = PROCESSING`, xóa analysisError. Khi thay audio/nhạc cụ, vô hiệu hóa analysisFileUrl của kết quả cũ. Giữ snapshot MAIN ID, audio ID, nhạc cụ và payload để đối soát.
2. Sau confirm, có thể trả kết quả chấp nhận yêu cầu cho frontend, kèm analysisId. Tiếp tục theo dõi bất đồng bộ, không giữ request upload chờ phân tích.
3. Gọi API status bằng analysisId, khoảng 3–5 giây/lần là điểm bắt đầu phù hợp; điều chỉnh theo tải. Có thể để BE kiểm tra các MAIN đang PROCESSING định kỳ, hoặc FE hỏi qua BE. Nếu cần đồng bộ ngay cả khi người dùng đóng trang, BE phải có cơ chế kiểm tra nền và tiếp tục theo dõi sau restart.
4. Khi completed, đọc artifact qua API, xác nhận MAIN/audio/nhạc cụ/phiên bản khớp yêu cầu hiện hành rồi mới đặt READY.
5. Khi failed, lưu ERROR và thông báo lỗi. Không tự gửi lại vô hạn. Rhythm đã retry lỗi kỹ thuật trong giới hạn cấu hình, mặc định 3 lần sau lần đầu.

Nếu chưa xác định publish thành công hay chưa, giữ analysisId và payload đã lưu, tiếp tục đối soát. Đừng tự tạo analysisId mới mỗi lần timeout vì có thể sinh nhiều job. Nếu cần phát lại cùng một yêu cầu do trạng thái publish chưa rõ, giữ nguyên analysisId và đầu vào; completed duplicate được bỏ qua. Job failed cuối cùng muốn chạy lại cần analysisId mới.

BE không cần gửi ACK cho job sau khi publish. ACK message là trách nhiệm consumer Rhythm, sau khi lưu xong kết quả/lỗi cuối cùng hoặc broker đã nhận message retry.

## 4. API theo dõi và lấy JSON

Mọi request dưới đây do BE gọi server-to-server:

```http
Authorization: Bearer <RHYTHM_SERVICE_INTERNAL_API_KEY>
```

Base URL lấy từ `RHYTHM_SERVICE_BASE_URL` của BE. Shared key phải khớp cấu hình Rhythm; không đưa key ra frontend.

### Theo dõi đúng job vừa gửi

```http
GET {RHYTHM_SERVICE_BASE_URL}/api/v1/template-analyses/{analysisId}
```

API trả JSON trực tiếp, không có wrapper success/data của BVC-BE. HTTP 200 có thể là queued, processing, completed hoặc failed; phải đọc trường status.

| Kết quả | BE xử lý |
| --- | --- |
| 404 ngay sau publish | Worker có thể chưa nhận message nên chưa tạo record. Tiếp tục theo dõi; không kết luận gửi thất bại ngay |
| queued hoặc processing | Giữ PROCESSING. Trong lúc retry, Rhythm có thể vẫn báo processing |
| completed | Lấy artifactUrl, tải JSON và kiểm tra các khóa trước khi chuyển READY |
| failed | Lưu ERROR từ error.code/error.message; đọc quality để giải thích bản thu nếu có |
| 401 | Kiểm tra shared key; không tự publish job mới |
| Lỗi mạng/5xx | Thử đọc lại có giới hạn; chưa kết luận phân tích thất bại |

Một worker xử lý tuần tự nên job có thể chờ các job khác. BE cần giới hạn thời gian/số lần polling theo sản phẩm và có cách báo chờ lâu/đối soát, không để vòng polling vô hạn. Timeout polling là chưa biết kết quả, không phải trạng thái failed từ Rhythm. 404 kéo dài cũng có thể do sai broker/vhost/queue, payload vào DLQ hoặc worker chưa chạy.

### Đọc file JSON

Khi metadata có artifactUrl, ghép đường dẫn tương đối đó với base URL Rhythm:

```http
GET {RHYTHM_SERVICE_BASE_URL}/api/v1/template-analyses/{analysisId}/artifact
Authorization: Bearer <RHYTHM_SERVICE_INTERNAL_API_KEY>
```

API trả file JSON application/json. HTTP 409 nghĩa là job chưa completed; HTTP 503 với mã ARTIFACT_UNAVAILABLE nghĩa là file thiếu/hỏng hoặc checksum không khớp. Khi đó chưa đánh dấu READY như đã có dữ liệu sử dụng được.

File JSON chứa analysisId, sheetMusicId, mediaFileId, analysisVersion, nguồn audio, cấu hình nhạc cụ, quality và dữ liệu cao độ/tempo. BE không cần truy cập filesystem hoặc database Rhythm. Nếu dùng `analysisFileUrl` hiện có, có thể lưu URL API artifact nội bộ; mọi lần đọc vẫn phải kèm internal key ở server. Đây không phải URL công khai cho frontend; nếu cần FE tải thì BE phải cung cấp endpoint được kiểm soát riêng.

### Tra cứu từ MAIN/audio khi học sinh chọn piece, dành cho tích hợp sau

```http
GET {RHYTHM_SERVICE_BASE_URL}/api/v1/template-analyses/lookup?sheetMusicId={main.id}&mediaFileId={main.referenceAudioId}&instrumentCode=dan_bau&analysisVersion=template-pyin-v1
```

Bốn tham số đều bắt buộc. API chọn bản ghi mới nhất khớp đầy đủ các khóa; trả 404 nếu không có, 409 nếu bản mới nhất processing/failed. Không tự fallback sang completed cũ. Sau khi lookup completed, dùng analysisId/artifactUrl trả về để đọc JSON.

`template-pyin-v1` là phiên bản phân tích, không phải policyVersion của quality. Giá trị này được quản lý trong contract/source, không gửi thêm vào payload queue hiện tại.

## 5. Đồng bộ về MAIN và tránh ghi nhầm kết quả cũ

| Trạng thái Rhythm | analysisStatus trong BE đề xuất |
| --- | --- |
| Chưa có record nhưng publish đã confirm, queued, processing | PROCESSING |
| completed và artifact đọc/kiểm tra được | READY |
| failed cuối cùng | ERROR |
| Chắc chắn publish thất bại | ERROR với lỗi gửi queue |

Chỉ áp dụng kết quả nếu `SheetMusic.analysisJobId` vẫn bằng analysisId đang theo dõi, MAIN vẫn có referenceAudioId và nhạc cụ đúng snapshot. So khớp sheetMusicId/mediaFileId ở cả metadata và JSON; kiểm tra metadata.instrumentCode cùng JSON.instrument.code. Có thể dùng conditional update tương tự `updateIfJobMatches`, bổ sung điều kiện audio/nhạc cụ nếu cần. Nếu người dùng đã thay audio hoặc đổi nhạc cụ, bỏ qua kết quả cũ và tiếp tục theo dõi job mới.

WARN vẫn cho phép completed/READY; REJECT dẫn tới failed/ERROR, không có artifact hoàn tất. Các lỗi nội dung gồm AUDIO_DECODE_FAILED, AUDIO_EMPTY, AUDIO_NON_FINITE, AUDIO_SILENT; mã khác có thể liên quan tải file, nhạc cụ hoặc hạ tầng. Dùng error.code để xử lý và error.message để hiển thị, không phụ thuộc việc message giữ nguyên từng chữ.

Payload sai hoặc cùng analysisId nhưng khác MAIN/audio/URL/nhạc cụ bị chuyển vào `rhythm.template.analyze.dlq`. Message schema sai có thể không tạo record trong DB Rhythm. DLQ cần được kiểm tra khi đối soát; lỗi xử lý có record failed không nhất thiết vào DLQ.

Rhythm không callback tới `/internal/sheet-music/:id/analysis`, không tự cập nhật SheetMusic và không phát event completed về BE. Luồng so sánh student hiện tại vẫn nhận hai URL và phân tích lại template; việc thay sang dùng JSON đã lưu sẽ làm ở giai đoạn sau.

## 6. Các điểm nối BE cần triển khai

- Gửi từ các sự kiện tạo MAIN có audio, thay audio và đổi nhạc cụ; mở lại phần điều phối trong `AnalysisService.requestAnalysis()` theo contract mới.
- Publisher queue template và lưu trạng thái gửi; tái sử dụng connection/confirm/return của publisher hiện có.
- HTTP client đọc status/artifact, kiểm tra response, timeout và shared key.
- Cơ chế theo dõi PROCESSING, cập nhật có điều kiện và báo lỗi/thông báo cho giảng viên nếu sản phẩm cần.

Nguồn contract: [schema job](../../app/schemas/template_messages.py), [consumer](../../app/messaging/rabbitmq_worker.py), [API](../../app/routes/template_analysis_routes.py). Phía BE có [AnalysisService](../../../BVC-BE/src/modules/music-library/service/AnalysisService.ts), [publisher tham khảo](../../../BVC-BE/src/modules/audio/service/RhythmPublisherService.ts) và [findMain](../../../BVC-BE/src/modules/music-library/repository/SheetMusicRepository.ts).
