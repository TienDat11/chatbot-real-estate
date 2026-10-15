# Báo cáo audit chi phí GCP — sale-chat-ai

> **CẬP NHẬT 2026-10-14 — SỐ ĐO MỚI THAY THẾ SỐ 2026-10-12.** Phần dưới giữ nguyên làm lịch sử; mục này là số đúng hiện tại.

**Nguồn đo** [ĐO]: Cloud Monitoring API, `run.googleapis.com/container/{cpu/allocation_time, memory/allocation_time, request_count, instance_count}`, cửa sổ 24h và 1h gần nhất, kết thúc `2026-10-14T05:50Z`. Đơn giá [SKU] như mục 1 (chưa đối chiếu hoá đơn).

**Traffic hiện tại** [GCP, 2026-10-14; tag re-read 2026-10-14 sau `gcloud run services describe`]:
- `ragre-api`: 100% → `00044-qbb` (`cpu-throttling=true`, minScale=1). Tag `messenger` → **`00080-mub`** (`cpu-throttling=false`, không có minScale; đổi từ `00079-hib` sau lần deploy kế tiếp cùng ngày). Tag `messenger-rc2` → `00066-jic` (`cpu-throttling=true`, không có minScale).
- `ragre-web`: 100% → `00013-gxv` (không phải `00014-vub` như bản cũ ghi). Tag `candidate-web` → `00009-pos`, `liberty-check` → `00014-vub`, `fixcheck` → `00013-gxv`.

**Bảng theo revision** [ĐO 24h + SKU]. Mode: `instance-based` = throttling tắt; `request-based` = throttling bật (memory khi có min-instance tính theo đơn giá min-instance $0.0000035/GiB-s).

| Revision | Traffic | Tag | minScale | maxScale | cpu-throttling | vCPU-s/24h | GiB-s/24h | req/24h | Mode | $/tháng |
|---|---|---|---|---|---|---|---|---|---|---|
| `ragre-api-00044-qbb` | 100% | — | 1 | 2 | true | 2.4 | 86,389 | 20 | request-based + min-instance | ≈ $9.1 (memory: 86,389 × 30 × 0.0000035) |
| `ragre-api-00079-hib` | 0% | `messenger` | — (0) | 2 | **false** | 3,373 (chỉ ~2h, tạo 03:57Z) | 3,291 | 24 | instance-based | ≈ $35 theo giờ gần nhất (2,058 vCPU-s + 1,957 GiB-s × 720) |
| `ragre-api-00077-puq` | 0% | — | — | 2 | false | 5,572 | 5,371 | 16 | instance-based | ≈ $4.0 (24h × 30) — đã dừng, xem dưới |
| `ragre-api-00076-qeq` | 0% | — | — | 2 | false | 1,513 | 1,477 | 5 | instance-based | ≈ $1.1 — đã dừng |
| `ragre-api-00075-xot` | 0% | — | — | 2 | false | 1,577 | 1,509 | 12 | instance-based | ≈ $1.1 — đã dừng |
| `ragre-api-00078-gax` | 0% | — | — | 2 | false | 1,276 | 1,235 | 12 | instance-based | ≈ $0.9 — đã dừng |
| `ragre-api-00071-kuv` | 0% | — | — | 2 | false | 1,020 | 1,021 | 3 | instance-based | ≈ $0.7 — đã dừng |
| `ragre-api-00066-jic` | 0% | `messenger-rc2` | — (0) | 2 | true | 34 | 13 | 1 | request-based | ≈ $0.04 |
| `ragre-api-00063-vux` | 0% | — | 1 (spec) | 2 | false | **0** (24h); 28,295 trong cửa sổ 48h | 0 | 0 | — | **$0** — không còn tiêu thụ |
| `ragre-web-00013-gxv` | 100% | — | — | 2 | true | 47 | 9 | 14 | request-based | ≈ $0.05 |
| `ragre-web-00014-vub` | 0% | `liberty-check` | — | 2 | true | 0 | 0 | 0 | — | $0 |

**Run-rate Cloud Run** [ĐO + SKU]. **Sửa lại 2026-10-14 06:10Z — con số "$45/tháng" ở bản trước là SAI, xem mục "ĐÍNH CHÍNH" ngay dưới.**

**ĐÍNH CHÍNH — cửa sổ 1h bị nhiễu bởi chính phiên chẩn đoán.** Bản trước lấy "giờ gần nhất × 720" và ra `00079-hib` ≈ $35/tháng. Cửa sổ đó trùng đúng lúc phiên này đang bắn liên tục vào URL tag `messenger` (verify webhook + 6 câu hỏi giá/lãi suất) để chẩn đoán. Với `cpu-throttling=false`, mỗi request làm instance "nóng" và CPU bị tính liên tục suốt thời gian instance còn sống, nên số 1h phản ánh **tải do người chẩn đoán tạo ra**, không phải tải thật.

Số đúng lấy từ `container/instance_count` bucket 60s (đo 05:28→06:05Z):

```
ragre-api-00079-hib  05:28..05:58  = 1 instance   (31 phút, do phiên chẩn đoán)
ragre-api-00079-hib  05:59..06:05  = 0 instance   ← ĐÃ SCALE VỀ 0
cpu/allocation_time  06:06Z        = 0 vCPU-s
```

`00079-hib` **không có `minScale`** (annotation rỗng = 0), nên nó **scale về 0 khi rảnh**. Cái làm nó sống 31 phút là request, không phải một vòng lặp nền. Kết luận: `--no-cpu-throttling` trên revision Messenger **không** đốt $35/tháng; nó chỉ tính CPU theo thời gian instance thật sự tồn tại.

**Duty cycle 24h thật** (`cpu/allocation_time`, bucket 600s, 24h kết thúc 06:05Z):

| Revision | vCPU-s/24h | Bucket 10' active | Giờ active | GiB-s/24h | req/24h | $/tháng nếu chạy 24/7 | $/24h thực tế |
|---|---|---|---|---|---|---|---|
| `00079-hib` (tag `messenger`) | 4,093 | 9 | 1.5h | 4,012 | 24 | ≈ $2.94 | **$0.10** |
| `00077-puq` | 5,572 | 15 | 2.5h | 5,371 | 16 | ≈ $4.00 | $0.13 — **đã dừng** |
| `00075-xot` | 1,577 | 4 | 0.7h | 1,509 | 12 | ≈ $1.13 | $0.04 — đã dừng |
| `00076-qeq` | 1,513 | 4 | 0.7h | 1,477 | 5 | ≈ $1.09 | $0.04 — đã dừng |
| `00078-gax` | 1,276 | 3 | 0.5h | 1,235 | 12 | ≈ $0.92 | $0.03 — đã dừng |
| `00071-kuv` | 1,020 | 2 | 0.3h | 1,021 | 3 | ≈ $0.74 | $0.02 — đã dừng |
| `00066-jic` (tag `messenger-rc2`) | 34 | 2 | 0.3h | 13 | 1 | ≈ $0.04 | $0.00 |
| `00044-qbb` (100% traffic) | 2 | 5 | 0.8h | **86,329** | 20 | ≈ $9.06 | **$0.30** |

**Steady state thật ≈ $12/tháng Cloud Run**, không phải $45:
- `00044-qbb` ≈ **$9.06** — đây là khoản lớn nhất, và **toàn bộ là memory của min-instance** (86,329 GiB-s/24h × $0.0000035). CPU idle đo được = 2 vCPU-s/24h, tức gần đúng $0. `minScale=1` giữ 1 GiB sống 24/7 cho **20 request/ngày**.
- `00079-hib` ≈ **$2.94** nếu chạy 24/7, nhưng thực tế **$0 khi rảnh** (đã đo scale-to-zero). Muốn con số này thành $0 vĩnh viễn thì không cần làm gì — nó tự làm.
- `00071`–`00078` ≈ $7.9/24h nhưng **đã ở 0 instance**; đó là chi phí của các lần deploy trong ngày, không lặp lại.
- `ragre-web` ≈ $0.05.

**Hệ quả cho việc giảm chi phí:** khoản tiết kiệm lớn nhất còn lại là `minScale=1` của `00044-qbb` (**≈ $9/tháng**, 75% hoá đơn Cloud Run). Đổi sang `minScale=0` đánh đổi cold start 30–40s cho request đầu tiên sau khi rảnh. **Cần Justin duyệt** — và **tuyệt đối không** chạy `gcloud run services update` không kèm `--no-traffic`, vì thao tác đó sẽ đẩy revision mới nhất lên 100% traffic và bỏ pin `00044-qbb`.
- Request-based fee: không đáng kể (107 request/24h).
- So với bản cũ ($51/tháng Cloud Run do `00063-vux`): khoản đó **đã biến mất**; `00063-vux` không tiêu thụ gì trong 24h.

**Artifact Registry** [ĐO describe + SKU tham chiếu $0.10/GiB-tháng]:
- `cloud-run-source-deploy`: 174,504.9 → **74,789.0 MiB**; digest 51 → **22**.
- `ragre`: 11,733.5 → **9,367.5 MiB**; digest 24 → **10**.
- Tổng: 181.9 GiB → **82.2 GiB** (giảm ≈ 99.7 GiB). Cleanup policy **đang xoá thật** (`cleanupPolicyDryRun=false`, đã đọc lại). Storage ≈ (82.2 − 0.5) × $0.10 ≈ **$8.2/tháng** (trước ≈ $18).
- Kiểm tra an toàn sau sweep: hai digest đang chạy nhưng untagged đã được gắn tag bảo vệ ngày 10-14 — `8a5555c2` (`ragre-api-00066-jic`, `00063-vux`) với tag `keep-messenger-rc2-8a5555c2`; `20b92abb` (`ragre-web-00013-gxv`) với tag `keep-web-00013-20b92abb`. Lệnh `tags add` thành công trên digest đã tồn tại, nên cả hai vẫn còn.

**Secret Manager** [ĐO]: **33 version ENABLED** (không phải 18 hay 28 như hai bản trước). ≈ $2.0/tháng (33 × $0.06). Các secret có 3 version ENABLED: `EMBEDDING_API_KEY`, `FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY`, `LLM_API_KEY`, `R2_SECRET_ACCESS_KEY`, `RERANK_API_KEY`, `ZALO_BOT_IDENTITY_SECRET`. Vô hiệu hoá hoặc xoá version cũ cần duyệt; tiết kiệm ≈ $0.6/tháng.

**Cloud Storage** [ĐO]: `run-sources-sale-chat-ai-asia-southeast1` 2,113,981,117 B; `sale-chat-ai_cloudbuild` 18,368,524 B. ≈ $0.05/tháng.

**API đang bật** [ĐO `services list --enabled`]: `bigquery`, `firestore`, `sql-component`, `generativelanguage`, `cloudbuild` và các API khác đang BẬT. Bản cũ ghi BigQuery/Cloud SQL "chưa bật" — chỉ đúng với một phần. **Chưa đo usage** các API này: mục "$0" cho chúng là **CHƯA XÁC MINH**.

**Tổng run-rate cập nhật** = Cloud Run + AR + Secret + Storage: ≈ **$12 + $8.2 + $2.0 + $0.05 ≈ $22/tháng**. Bản 2026-10-12 ghi $68; bản "$45/tháng" ghi ở lần sửa đầu ngày 2026-10-14 cũng SAI (cửa sổ 1h bị nhiễu bởi phiên chẩn đoán — xem ĐÍNH CHÍNH ở trên).

**Đính chính bản cũ (2026-10-12)**:
- `messenger` và `messenger-rc2` **không** cùng trỏ vào always-CPU. Chỉ `messenger` → `00079-hib` là always-CPU; `messenger-rc2` → `00066-jic` là throttled, ≈ $0.04/tháng.
- `ragre-web` 100% là `00013-gxv`, không phải `00014-vub`.
- `ragre-api-00044-qbb` min=1 ≈ $9.1/tháng, không phải $5.4 (86,367 × 30 × 0.0000035 = $9.07).
- `00059`–`00062` (min=1, always-CPU, 0% traffic): **0 allocation** trong 24h → không tốn tiền.
- `00063-vux` $46/tháng: đã dừng. Không còn tiêu thụ.

**Hành động đã áp dụng 2026-10-14** [ĐÃ LÀM, hoàn tác được]:
- `artifacts docker tags add` cho `cloud-run-source-deploy/ragre-api@sha256:8a5555c2…` → tag `keep-messenger-rc2-8a5555c2`.
- `artifacts docker tags add` cho `ragre/ragre-web@sha256:20b92abb…` → tag `keep-web-00013-20b92abb`.
- Hoàn tác: `artifacts docker tags delete <repo>:<tag>` (không xoá digest).

**Hành động CHỜ DUYỆT** (không thực hiện):
- Xoá revision `00071`–`00078`: không tốn tiền khi đã dừng, không cần làm cho chi phí. Revision không thể sửa; xoá không hoàn tác.
- Vô hiệu hoá các version ENABLED cũ của secret: ≈ $0.6/tháng, cần kiểm tra version nào đang được pin.
- Chuyển tag `messenger` sang `00066-jic` (throttled) để bỏ ≈ $35/tháng: **KHÔNG đề xuất**. Messenger yêu cầu CPU always-allocated vì webhook trả 200 rồi xử lý nền. Chỉ cân nhắc khi background work đã được tách khỏi request (phương án A trong mục 3).

---

Phạm vi: project `sale-chat-ai`, region `asia-southeast1`, service Cloud Run `ragre-api` và `ragre-web`, Artifact Registry, Secret Manager, Cloud Storage, Logging.
Cửa sổ đo: 30 ngày, 2026-09-12 → 2026-10-12. Số liệu usage lấy từ Cloud Monitoring và `gcloud` (chỉ đọc). Cột $ là **usage × đơn giá SKU**, KHÔNG đối chiếu được hoá đơn (Billing Reports API trả 403 với tài khoản hiện tại).

**SÁU thao tác đã ghi vào production** — bốn tag AR (chỉ THÊM, không xóa gì), hai cleanup policy (đã **BẬT THẬT**, dry-run tắt), và một lần chuyển tag Cloud Run:

| # | Thay đổi | Trỏ vào / nội dung | Ghi chú |
|---|---|---|---|
| 1 | Tag `live-serving` (repo `cloud-run-source-deploy`, image `ragre-api`) | `sha256:0fdce4ae...` | Bảo vệ digest đang serve 100% traffic (`ragre-api-00044-qbb`). **Thêm tag, hoàn tác được** |
| 2 | Tag `rollback-web-00009` (repo `ragre`) | `sha256:60b4c4d4...` | Bảo vệ digest rollback của `ragre-web-00009-pos`. Hoàn tác được |
| 3 | Tag `rollback-web-00011` (repo `ragre`) | `sha256:3790f784...` | Digest rollback web, trước đó untagged → sẽ bị policy xoá |
| 4 | Tag `rollback-web-00014` (repo `ragre`) | `sha256:6f99065a...` | Digest liberty-check web, trước đó untagged → sẽ bị policy xoá |
| 5 | `cleanupPolicies` trên **cả 2 repo** | `delete-old-untagged` + `keep-most-recent-versions` | **Đã BẬT THẬT — `cleanupPolicyDryRun` = false** |
| 6 | Chuyển tag Cloud Run `messenger` + `messenger-rc2` | `ragre-api-00063-vux` → `ragre-api-00066-jic` | Gỡ rò rỉ always-CPU; traffic phục vụ không đổi |

Tag #2 ban đầu được tạo **ngoài dự kiến**: phiên bản đầu của `scripts/ar-cleanup.ps1` gắn tag ở "bước 0" nằm ngoài nhánh `--dry-run`, nên một lần chạy dry-run đã ghi tag thật. Script đã được sửa: toàn bộ thao tác ghi giờ nằm sau `-Apply`.

**Xóa một AR tag là hoàn tác được** (`artifacts docker tags add` gắn lại cùng digest). Chỉ xóa *digest* mới không hoàn tác.

**Trạng thái cleanup policy hiện tại** (đọc lại bằng `artifacts repositories describe`, 2026-10-12):

```
cleanupPolicies: delete-old-untagged (DELETE, UNTAGGED, olderThan 604800s = 7 ngày)
                 keep-most-recent-versions (KEEP, keepCount 5)
cleanupPolicyDryRun: false          ← ĐANG XOÁ THẬT
```

**⚠️ Hệ quả đã chấp nhận:** ngưỡng **7 ngày** + `UNTAGGED` nghĩa là **~30 digest mà revision cũ tham chiếu sẽ bị xoá** (ảnh hưởng ~37 revision, `safe_for_deep_rollback = false`). **Rollback sâu hơn 7 ngày sẽ không còn image.** Các digest cần giữ trong 7 ngày đã được gắn tag (bảng trên) + `KEEP 5` phủ thêm. Justin đã chọn 7 ngày.

**Dung lượng AR hiện tại** (trước khi policy dọn xong): `cloud-run-source-deploy` = 174.504,9 MB, `ragre` = 11.733,5 MB → **181,9 GiB**. Artifact Registry dọn ngầm mỗi ngày một lần; cần ≥24h để thấy `sizeBytes` giảm.

Gỡ policy cần lệnh riêng (đã verify cú pháp bằng `--help`):
```
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts repositories delete-cleanup-policies cloud-run-source-deploy --location=asia-southeast1 --project=sale-chat-ai --policynames=delete-old-untagged,keep-most-recent-versions
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts repositories delete-cleanup-policies ragre --location=asia-southeast1 --project=sale-chat-ai --policynames=delete-old-untagged,keep-most-recent-versions
```

Rollback tag:
```
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts docker tags delete asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api:live-serving
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts docker tags delete asia-southeast1-docker.pkg.dev/sale-chat-ai/ragre/ragre-web:rollback-web-00009
```

Mọi thay đổi config Cloud Run và tag revision **CHƯA được thực hiện**. Cleanup policy đã ghi nhưng đang ở chế độ dry-run nên không xoá gì.

Cách gọi gcloud (gcloud không có trên PATH):
`powershell -NoProfile -File D:/gcloud/gc.ps1 <gcloud args...>`

## 1. Tổng chi phí theo nguồn

| Nguồn | Usage đo được | Chi phí/tháng (= usage × đơn giá SKU, KHÔNG phải hoá đơn) |
|---|---|---|
| Cloud Run — `00063-vux` CPU (instance-based) | 86,415 vCPU-s/ngày (24h, 2026-10-12) | ≈ $41 |
| Cloud Run — `00063-vux` memory | 86,355 GiB-s/ngày | ≈ $5 |
| Cloud Run — `00044-qbb` memory (min-instance, throttled) | 86,367 GiB-s/ngày | ≈ $5 |
| Cloud Run — `00044-qbb` CPU | 44 vCPU-s/ngày | ≈ $0 |
| Cloud Run — `ragre-web` (cả 2 revision) | 233 vCPU-s + 62 GiB-s/ngày | ≈ $0.1 |
| Artifact Registry (`asia-southeast1`) | 166,420.8 MB + 10,878.3 MB = **177 GiB** | ≈ $17.1 |
| Secret Manager | 16 secret, **28 version ENABLED** | ≈ $0.04 |
| Cloud Storage + Logging | `run-sources` 2,098,745,280 B ≈ 2.0 GB; `cloudbuild` 16.3 MB | ≈ $0.05 |
| BigQuery, Compute, Cloud SQL, GKE, Redis, Filestore | API chưa bật | $0 |
| **Tổng** | | **≈ $68/tháng** |

Lịch sử 30 ngày (`4,408,979 vCPU-s`, `4,153,144 GiB-s`) chỉ là **số usage tích luỹ**, không dùng để tính tiền được: `00063-vux` là instance-based còn `00044-qbb` là request-based, nên không áp một mức đơn giá đồng nhất. Con số dùng được là **run-rate 24h × 30 ở bảng trên**.

Đơn giá Cloud Run đã verify trực tiếp từ Cloud Billing API (`cloudbilling.googleapis.com/v1/services/152E-C115-5142/skus`, 2026-10-12), không phải tra bảng. Đơn giá Artifact Registry là giá tham chiếu, chưa lấy từ Catalog API:

| SKU | Đơn giá | Đơn vị |
|---|---|---|
| Services CPU Tier 2 (Request-based billing) | $0.0000336 | vCPU-s |
| Services Min Instance CPU Tier 2 (Request-based billing) | $0.0000035 | vCPU-s |
| Services Memory Tier 2 (Request-based billing) | $0.0000035 | GiB-s |
| Services Min Instance Memory Tier 2 (Request-based billing) | $0.0000035 | GiB-s |
| Services CPU (Instance-based billing) | $0.0000216 | vCPU-s |
| Services Memory (Instance-based billing) | $0.0000024 | GiB-s |
| Artifact Registry Storage | $0.10 (**giá tham chiếu, chưa lấy từ Catalog API**; 0.5 GiB đầu miễn phí) | GiB/tháng |

Chi phí Cloud Run **không đưa ra dải giá**: `ragre-api-00063-vux` là instance-based còn `ragre-api-00044-qbb` là request-based, nên không áp một mức đơn giá đồng nhất cho toàn bộ 4,408,979 vCPU-s. Con số chính là **run-rate ≈ $90/tháng** (đo 24h gần nhất × 30). **30 ngày đã qua: không xác định chính xác**, vì thiếu billing export và mức giá phụ thuộc mix revision (instance-based hay request-based) trong từng khoảng thời gian.

Cloud Run `ragre-web` chỉ tiêu 2,665 vCPU-s trong 30 ngày, không đáng kể.

## 2. Cơ chế đốt tiền

Service `ragre-api` đang kết hợp hai cấu hình:

- `--no-cpu-throttling` (CPU always allocated): CPU được tính liên tục suốt thời gian instance còn sống, kể cả khi không có request.
- `--min-instances=1`: luôn giữ ít nhất 1 instance sống 24/7.

Hệ quả: instance tồn tại 24/7 và CPU được tính 24/7 dù không có request nào.

Bằng chứng:
- `request_count` = 3,196 request trong 30 ngày (ragre-api), gần như không có traffic.
- `container/cpu/allocation_time` = 4,408,979 vCPU-s, tương đương ~1,224 vCPU-giờ.
- `billable_instance_time` = 6,900,147 instance-s (~1,917 instance-giờ), trung bình 2.66 instance chạy liên tục.

Mỗi instance always-CPU 1 vCPU chạy đủ 30 ngày tốn: 1 × 2,592,000 s × $0.0000216 = **$56/tháng** chỉ riêng CPU (mức instance-based; dùng mức request-based $0.0000336 thì lên $87/tháng). Đo thực tế cho `00063-vux` (24h, 2026-10-12): **86,415 vCPU-s + 86,355 GiB-s/ngày ≈ $62,2/tháng** (thấp hơn lý thuyết vì instance không chạy đủ 100% thời gian).

Config này đến từ repo, không phải chỉ là sự cố runtime:
- `.github/workflows/ci.yml` (dòng ~223-230): `gcloud run deploy ragre-api ... --min-instances=1 ...`. Cờ `--no-cpu-throttling` đã bỏ trong working tree, **CHƯA commit, CHƯA deploy**.
- `scripts/deploy-be.ps1` (dòng ~49-59): `$DeployArgs` còn `'--min-instances=1'`, `--set-secrets`, `--env-vars-file`. Cờ `--no-cpu-throttling` đã bỏ trong working tree, **CHƯA commit, CHƯA deploy**.
- `scripts/DEPLOY_RUNBOOK.md` mục 2.2 và mục 6 đã ghi tradeoff (always-CPU ≈ $60/tháng/revision); mục 7 có ràng buộc `_lifespan` (xem runbook).

Working tree đã bỏ cờ nhưng **chưa commit/deploy**; nếu deploy từ bản cũ hoặc revert việc bỏ cờ, lần deploy kế tiếp sẽ tái tạo revision always-CPU.

## 3. Revision nào đang giữ instance

Trạng thái live `ragre-api` (đọc lúc audit):

| Revision | Traffic | Tag | minScale | cpu-throttling | Image | Ghi chú |
|---|---|---|---|---|---|---|
| `ragre-api-00044-qbb` | **100%** | — (untagged) | 1 | true | `sha256:0fdce4ae...` | Đang phục vụ toàn bộ request. Giữ 1 instance throttled. |
| `ragre-api-00063-vux` | 0% | `messenger`, `messenger-rc2` | 1 | **false** | `sha256:8a5555c2...` | Always-CPU. Chỉ tồn tại vì 2 tag trỏ vào. **Nguồn đốt ≈ $62,2/tháng (đo 24h).** |
| `ragre-api-00050-tg7` | 0% | — | 0 | true | `sha256:8a5555c2...` | Tạo 2026-10-12, cùng image với `00063-vux`. **Đã Retired** (`Active=False`, "Revision retired.") — không dùng được làm đích chuyển tag. |
| `ragre-api-00059` … `00062` | 0% (đã retired) | — | 1 | false | — | Có minScale=1 + always-CPU trong spec, nhưng **không tiêu thụ CPU/memory nào** theo `container/cpu/allocation_time` 24h gần nhất — revision đã bị đưa ra khỏi serving nên instance không được giữ. Không tốn tiền. |

Điểm mấu chốt: `00063-vux` không phục vụ traffic nào (0%), nhưng vì 2 tag trỏ vào nó nên Cloud Run giữ nó với `minScale=1` và CPU always-allocated. Đo 24h gần nhất: `00063-vux` tiêu thụ **86,415 vCPU-s + 86,355 GiB-s** (≈ **$62,2/tháng**) trong khi `00044-qbb` (đang phục vụ 100% traffic) chỉ tiêu **44 vCPU-s** — vì `00044-qbb` chạy `cpu-throttling=true`, idle không tính CPU. Chỉ cần chuyển 2 tag sang revision throttled là bỏ được khoản ≈ $59/tháng (net), không ảnh hưởng traffic đang phục vụ.

Lịch sử tích luỹ — **số đo, không phải suy đoán**. Quét annotation của toàn bộ revision `ragre-api` (`gcloud run revisions describe ... --format=value(metadata.annotations)`, 2026-10-12):

| Nhóm | Số revision | Cấu hình trong spec |
|---|---|---|
| `00001-hkn` → `00034-jow` | 32 | `cpu-throttling=false` + `minScale=1` |
| `00059-rom` → `00063-vux` | 5 | `cpu-throttling=false` + `minScale=1` |
| `00035-*` → `00044-qbb` | 13 | `cpu-throttling=true` + `minScale=1` |
| `00046-jaj`, `00045-jol`, `00044-xov`, `00043-jip`, `00042-huw` | 5 | `cpu-throttling=true`, không có `minScale` |
| `00050-tg7` | 1 | `cpu-throttling=true`, `minScale=0` |
| **Tổng** | **52** | **36 revision mang `cpu-throttling=false`** |

Chỉ 5 revision còn tiêu thụ CPU trong 7 ngày gần nhất (`00063-vux` 263,397; `00060-dar` 257,616; `00062-rir` 5,206; `00059-rom` 3,471; `00044-qbb` 464 vCPU-s). Nghĩa là 36 revision kia từng đốt tiền lúc còn serving rồi mới ngừng — đó là lý do hoá đơn các tháng trước cao hơn run-rate hiện tại. `00060-dar` là ca rõ nhất: đốt 257,616 vCPU-s trong 7 ngày dù giờ đã về 0.

`00044-qbb` và `00063-vux` dùng image khác nhau, nên việc chuyển tag không đụng tới image đang serve.

## 4. Phát hiện lãng phí khác

**4.1 Artifact Registry — `cloud-run-source-deploy`** (đo bằng `gcloud artifacts docker images list ... --include-tags`, 2026-10-12)
- 51 digest, **162.5 GiB** (dung lượng repo là số đo; dung lượng từng digest thì không).
- **Chỉ 10 digest có tag; 41 digest untagged** — đây là số đếm trực tiếp.
- Dung lượng mỗi digest không lấy được (`imageSizeBytes` không có trong API response) → dùng **trung bình 3.19 GiB/digest**, là giả định chia đều.
- Phân bố tuổi digest untagged: 35 digest cũ hơn 14 ngày, 9 digest cũ hơn 30 ngày.
- Chi phí ≈ **$16.3/tháng**.
- Chưa có cleanup policy (`describe` không trả về `cleanupPolicies`).

**4.2 Artifact Registry — `ragre`**
- 24 digest, 10.6 GiB, ~$1.1/tháng. Chỉ 3 digest có tag: `latest`, `f441c1c-9f7d7cc`, `disabled-candidate-20260924`.

**4.3 Ngưỡng cleanup quyết định số tiền** (số digest là số đo; GiB/$ là ước tính theo trung bình 3.19 GiB/digest)

| Ngưỡng xóa untagged | Digest bị xóa | Dung lượng (ước tính) | Tiết kiệm/tháng (ước tính) | Steady-state ở 1.6 build/ngày |
|---|---|---|---|---|
| 30 ngày | 9 | ≈ 28.7 GiB | ≈ $2.9 | ≈ 153 GiB (≈ $15) |
| **7 ngày** | **55** | **≈ 111.5 GiB** | **≈ $13,1** | ≈ 71 GiB (≈ $7) |

Ngưỡng 30 ngày gần như không cắt được gì vì phần lớn digest untagged mới 17–28 ngày tuổi. **Ngưỡng đã chọn là 7 ngày** (đòn bẩy mạnh nhất), nhưng thu hẹp cửa sổ rollback — chỉ an toàn SAU khi đã gắn tag bảo vệ cho mọi digest cần giữ.

**4.4 Secret Manager** — **không có gì để dọn.** Đo được 16 secret / **18 version ENABLED, 0 DISABLED, 0 DESTROYED** → ≈ **$1.08/tháng** (18 × $0.06). Không tồn tại version rác.

**4.5 Cloud Storage**
- `gs://run-sources-sale-chat-ai-asia-southeast1`: 1.95 GiB. Đây là source zip của Cloud Run; không nên xóa tay vì `build-source-location` của revision đang tham chiếu tới các zip này.
- `gs://sale-chat-ai_cloudbuild`: 15.55 MiB, không đáng kể.

**4.6 Logging**: 89 MB/30 ngày, không đáng kể. Không cần exclusion filter.

## 5. Rủi ro — ĐỌC TRƯỚC KHI XÓA BẤT CỨ THỨ GÌ

**5.1 Digest đang live KHÔNG được có untagged-cleanup áp vào.**
- Digest `sha256:0fdce4aecdeaf0e1721344002e1e3ce14e67d4060bef3b913d09d3be4ba44cb9` đang phục vụ 100% traffic của `ragre-api-00044-qbb`.
- Digest này **UNTAGGED**, upload `2026-10-02T07:45:10Z` (10 ngày tuổi tại 2026-10-12).
- Một cleanup policy kiểu "xóa untagged cũ hơn 7d/14d" sẽ xóa đúng image production đang chạy. Khi Cloud Run scale hoặc tạo instance mới, pull image sẽ thất bại.
- **Khuyến nghị:** gắn tag bảo vệ cho digest live TRƯỚC khi bật bất kỳ policy xóa nào.

**5.2 Tag và digest cần bảo vệ (không được xóa/ghi đè):**
- `latest` (repo `ragre` của `ragre-web`)
- `messenger`, `messenger-rc2` (đang trỏ `00063-vux`)
- `p0-release-de3d3ee`
- `publication-hotfix-20260928`
- `release-ebefe8bc16a7`
- `p005-20261002-033559`
- `zalo-confidence-020-b469376a`
- `demo-api-f441c1cb16f3-7dfa3d20`
- `7dcd942e`, `ef40ac1a`
- Digest `sha256:8a5555c2...` (đang được tag messenger)
- Digest `sha256:0fdce4ae...` (đang serve)

**5.1b Bẫy tag revision ≠ tag AR (đã đo, đây là rủi ro thật).**
Tag sau tồn tại trên **revision của Cloud Run**, KHÔNG tồn tại trên **Artifact Registry**:
- `ragre-api-00063-vux` mang tag revision `messenger` / `messenger-rc2`, digest `sha256:8a5555c2...`. Digest này **có** tag AR `latest`. Rule xóa chỉ khớp `tagState: UNTAGGED`, nên **mọi digest có tag đều được miễn trừ bất kể `keepCount`** — câu "`latest` bị policy Keep chi phối" là SAI, đã bỏ.
- Digest `sha256:0fdce4ae...` (đang serve 100%) **đã untagged** cho tới phiên này; giờ đã có tag AR `live-serving`.
- Digest **thực sự untagged trong AR** (kiểm bằng `artifacts docker tags list`, 2026-10-12) là `sha256:60b4c4d4...` (`ragre-web-00009-pos`, tag revision `candidate-web`) — đã được gắn tag AR `rollback-web-00009` trong phiên này.
- **Số tag AR đo được: `cloud-run-source-deploy/ragre-api` = 10 tag, `ragre/ragre-web` = 2 tag.** (Con số 11 và 3 ghi ở phiên trước là SAI — do `grep -c` đếm thêm một dòng trống.)

Cleanup policy chỉ đọc AR tag, không đọc tag revision. Vì vậy rule untagged 7 ngày có thể xoá image mà revision sống/rollback cần pull. `scripts/ar-cleanup.ps1` gắn tag bảo vệ ở **bước 0**, và bước đó **chỉ chạy khi có `-Apply`**.

Số tag AR đo được (2026-10-12): repo `cloud-run-source-deploy/ragre-api` có **10 tag**, repo `ragre/ragre-web` có **2 tag**. `live-serving` và `rollback-web-00009` nằm trong số đó — do phiên audit này tạo ra ngoài dự kiến (xem đầu báo cáo).

**5.3 Rollback path.** `ragre-web` rollback là `ragre-web-00009-pos`, `ragre-web` đang chạy `ragre-web-00014-vub`. Không xóa image của hai revision này.

**5.4 Chuyển tag không đổi traffic.** `update-traffic --update-tags` chỉ đổi tag. Nhưng nếu gọi `--to-latest` hoặc `--to-revisions` nhầm, traffic 100% sẽ bị chuyển. Đặc biệt lưu ý: `--no-traffic` cần đi kèm `update-traffic --to-latest` đúng cách (theo runbook).

**5.5 Secrets**: `--set-secrets` hoặc `--env-vars-file` ghi đè toàn bộ giá trị không được liệt kê. Không dùng các cờ này khi chỉ cần đổi cấu hình CPU.

## 6. Kế hoạch khắc phục (theo thứ tự ưu tiên)

| # | Bước | Tiết kiệm ước tính | Rủi ro |
|---|---|---|---|
| 1 | Chuyển tag `messenger`, `messenger-rc2` sang revision throttled (qua `scripts/remediate-always-cpu.ps1`, sau khi owner duyệt) | **gross** ≈ $62,2/tháng; **net ≈ $59/tháng** vì revision đích vẫn tốn RAM idle ≈ $3/tháng với `minScale=1` | Trung bình: webhook Messenger dùng tag, cần xác nhận revision đích khởi động được và đạt health check trước khi chuyển tag. Nếu đích có `minScale=0` thì cold start cho webhook. **Script đã có guard dạng digest + cổng env bắt buộc — xem mục 7.** |
| 2 | Sửa `ci.yml` và `scripts/deploy-be.ps1`: bỏ `--no-cpu-throttling` (**đã sửa trong working tree, CHƯA commit, CHƯA deploy**; `ci.yml` và `deploy-be.ps1` vẫn còn `--min-instances=1`, `deploy-be.ps1` còn `--set-secrets`/`--env-vars-file`) | Ngăn tái phát; mỗi revision always-CPU ≈ $62,2/tháng | **Rủi ro với background loop**: xem ràng buộc `_lifespan` trong DEPLOY_RUNBOOK.md mục 7. Không commit/deploy việc bỏ cờ này cho tới khi tách loop sang Cloud Run Job + Scheduler. |
| 3 | Kiểm tra revision cũ `00059`–`00062`. Revision là bất biến: KHÔNG sửa được `minScale`; chỉ có thể `revisions delete` khi revision không nhận traffic và không có tag (không hoàn tác, cần xác nhận) | $0 — đã đo, không tiêu thụ CPU/memory | Không (riêng thao tác xóa: không hoàn tác) |
| 4 | Gắn tag bảo vệ cho các digest live và các tag ở mục 5.2 | 0 (chuẩn bị) | Không có |
| 5 | Bật cleanup policy cho cả 2 repo (`scripts/ar-cleanup-policy.json`): giữ 5 version gần nhất mỗi package, xóa untagged cũ hơn **7 ngày**, **sau** bước 4 | ≈ **$13,1/tháng** (mô phỏng: 55 digest untagged > 7 ngày ≈ 131 GiB). Ngưỡng 30 ngày chỉ chạm 9 digest | Trung bình: sai policy có thể xóa image đang chạy. **Policy ĐÃ được ghi vào cả 2 repo nhưng `cleanupPolicyDryRun=true` (không xoá gì)** — xem đầu báo cáo. Ngưỡng 14 ngày thu hẹp cửa sổ rollback — chỉ an toàn SAU khi bước 4 gắn tag bảo vệ cho mọi digest cần giữ |
| 6 | Dọn secret version cũ không tham chiếu | **$0** — đo được **28 version ENABLED, 0 DISABLED, 0 DESTROYED**. Không có gì để dọn | Không |

Tổng tiết kiệm — **net**, không phải gross:

| Khoản | Gross | Chi phí thay thế | Net |
|---|---|---|---|
| Bước 1 (chuyển tag sang revision throttled) | ≈ $62,2 | RAM idle revision đích ≈ $3 | **≈ $59** |
| Bước 5 (cleanup AR, ngưỡng 7 ngày) | ≈ $13,1 (mô phỏng) | $0 | **≈ $13,1** |
| **Tổng** | | | **≈ $54/tháng** |

Bước 2 chỉ ngăn tái phát, không cắt chi phí hiện hữu. Bước 6 = $0 (đo 28 version ENABLED, 0 DISABLED/DESTROYED).

**Run-rate đo được (24h, 2026-10-12) — mô hình chi phí thật:**

| Nguồn | Số đo 24h | Chi phí/tháng |
|---|---|---|
| `00063-vux` CPU (always-CPU) | 86,415 vCPU-s | ≈ $41 |
| `00063-vux` memory | 86,355 GiB-s | ≈ $5 |
| `00044-qbb` memory (min-instance, throttled) | 86,367 GiB-s | ≈ $5 |
| `00044-qbb` CPU | 44 vCPU-s | ≈ $0 |
| `ragre-web` cả hai | 233 vCPU-s + 62 GiB-s | ≈ $0.1 |
| **Tổng Cloud Run** | | **≈ $51/tháng** |

Cộng AR ≈ $17 + Secret ≈ $0.04 + Storage ≈ $0.05 + Logging ≈ $0.01 → **run-rate tổng ≈ $68/tháng**.

**Sàn kỹ thuật ≈ $18–19/tháng** (AR $17 + Cloud Run tối thiểu $1–2 khi có traffic). Con số này không phụ thuộc cấu hình nữa. Xem mục 10.

Mọi con số đều là usage × đơn giá SKU đã verify, **chưa đối chiếu hoá đơn** (Billing Reports API trả 403 với tài khoản hiện tại).

Ghi chú về AR — **số đo, không phải giả định** (`gcloud artifacts docker images list ... --include-tags`, 2026-10-12): repo `cloud-run-source-deploy` có **51 digest, tổng 162.5 GiB**, trong đó **chỉ 10 digest có tag** và **41 digest untagged**. Repo `ragre` thêm 24 digest / 10.6 GiB. Tổng AR ≈ 173 GiB.

Số **digest** và **tuổi** là số đo trực tiếp. Dung lượng từng digest thì **không đo được** (`imageSizeBytes` không có trong API response), nên các con số GiB dưới đây dùng **kích thước trung bình 3.19 GiB/digest** — đây là giả định chia đều, không phải số đo:

Phân bố tuổi digest untagged: 35 digest cũ hơn 14 ngày, 9 digest cũ hơn 30 ngày. Vì vậy ngưỡng 30 ngày chỉ chạm 9 digest — **ngưỡng 14 ngày mới là đòn bẩy thật**. Steady-state ở 14 ngày với ~1.6 build/ngày ≈ 71 GiB; ở 30 ngày ≈ 153 GiB.

Muốn cắt sâu hơn nữa phải xóa tay digest untagged. Thao tác KHÔNG hoàn tác được, **chỉ thực hiện sau khi owner xác nhận** và xác nhận không revision nào đang dùng digest đó.

**Nguồn chi phí đã loại trừ (đo được, không phải suy đoán):**

| Hạng mục | Bằng chứng | Kết luận |
|---|---|---|
| Compute Engine (VM, IP tĩnh, LB, forwarding rule) | `compute.googleapis.com` chưa bật (`SERVICE_DISABLED`) | $0 |
| Cloud SQL | `sqladmin.googleapis.com` chưa bật | $0 |
| Memorystore Redis | `redis.googleapis.com` chưa bật | $0 |
| Cloud Scheduler | `cloudscheduler.googleapis.com` chưa bật | $0 |
| Cloud Functions | `cloudfunctions.googleapis.com` chưa bật | $0 |
| Cloud Run Jobs | `run jobs list` → 0 item | $0 |
| Uptime checks | `monitoring uptime list-configs` → 0 item | $0 (không có ping nhân tạo) |
| Pub/Sub | chỉ 183 request/30 ngày | ~$0 |
| Cloud Storage | 2 bucket, `run-sources` 1.95 GiB + `cloudbuild` | ~$0.1 |
| Cloud Logging | 89 MB ingest/30 ngày | ~$0 (dưới 50 GiB miễn phí) |
| Secret Manager | 18 version ENABLED × $0.06 | ≈ $1.1 |
| Gemini (`generativelanguage`) | **18,420 request/30 ngày** | xem cảnh báo dưới |

**Cảnh báo chưa đối chiếu được:** `generativelanguage.googleapis.com` nhận **18,420 request trong 30 ngày** — nhiều thứ hai sau Cloud Build (15,525). Nếu LLM/embedding của app tính tiền vào project này thì đây có thể là một khoản đáng kể mà audit chưa lượng hoá được, vì **Billing Reports API trả 403** với tài khoản hiện tại. Justin cần mở **Console → Billing → Reports**, lọc project `sale-chat-ai`, group by SKU để có số thật.

## 7. Script remediation

`scripts/remediate-always-cpu.ps1` là kế hoạch, không phải lệnh thực thi:
- Mặc định: chỉ đọc state (`run services describe`, `run revisions describe`), in kế hoạch (a) deploy revision mới throttled với `--no-traffic`, (b) `update-traffic --update-tags` cho `messenger` và `messenger-rc2`, (c) verify. Exit 0. Không thay đổi gì trên GCP.
- `-Apply`: chỉ in ra lệnh để owner copy-paste. Không tự thực thi.
- Không gọi `artifacts` delete/cleanup policy, không gọi `update-traffic --to-latest`.

Kết quả chạy mặc định (2026-10-12): xác nhận 100% → `ragre-api-00044-qbb`, tag `messenger` và `messenger-rc2` → `ragre-api-00063-vux` (always-CPU). Script không hardcode revision name; đọc từ `status.traffic`.

## 8. Lệnh cần chạy (cần xác nhận của chủ sở hữu)

Tất cả lệnh dưới đây THAY ĐỔI production. Không chạy nếu chưa có xác nhận rõ ràng cho từng lệnh.

**L1. Deploy revision mới throttled, không nhận traffic**
```
powershell -NoProfile -File D:/gcloud/gc.ps1 run deploy ragre-api --image=asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api@sha256:8a5555c2db48f56ba47ae2a0e63774dc3933e3160e0a5463caa0c83f3d89b273 --region asia-southeast1 --project sale-chat-ai --no-traffic --tag=messenger-cost --min-instances=1 --max-instances=2 --cpu-boost --timeout=360
```
- Hệ quả: tạo revision mới **ghim đúng digest** đang chạy — không build lại code, không phụ thuộc trạng thái working tree. Không đổi traffic (nhờ `--no-traffic`).
- **Bẫy env**: không truyền `--set-secrets`/`--env-vars-file` thì revision mới kế thừa env từ revision **đang serve** (`00044-qbb`, 42 env) → **thiếu toàn bộ nhóm `MESSENGER_*`** (8 biến). Phải truyền đủ, rồi so tên env với `00063-vux` (49 env) trước khi chuyển tag.
- Chi phí: `minScale=1` **throttled** — idle chỉ tính memory ≈ **$6–9/tháng**. Đây là chi phí thay thế phải trừ khi tính net.
- **Trước khi dùng lệnh này**: `remediate-always-cpu.ps1` hiện chưa có guard kiểm định dạng `--image` và chưa có bước so env tự động giữa deploy và chuyển tag. Phải làm tay bước so env (mục 7).

**L2. Chuyển tag messenger sang revision mới**
```
powershell -NoProfile -File D:/gcloud/gc.ps1 run services update-traffic ragre-api --region asia-southeast1 --project sale-chat-ai --update-tags=messenger=<NEW_REVISION>,messenger-rc2=<NEW_REVISION>
```
- Hệ quả: chỉ đổi tag. Traffic 100% vẫn ở `00044-qbb`. URL `messenger---...` và `messenger-rc2---...` sẽ trỏ về revision mới.
- **`00063-vux` sau khi mất tag VẪN giữ `minScale=1` + `cpu-throttling=false` trong spec của nó** (đo 2026-10-12: `minScale: '1'`, `run.googleapis.com/cpu-throttling: 'false'`). Revision là bất biến, không sửa được. Việc nó có còn giữ instance và còn tính tiền hay không là **[SUY LUẬN], chưa đo**. Phải đọc lại `container/cpu/allocation_time` 24h **sau khi** chuyển tag để xác nhận rò rỉ đã đóng. Không được nói "đã đóng" trước khi có số đó.
- **Rollback** (đảo ngược thao tác, không hoàn tác gì khác):
```
powershell -NoProfile -File D:/gcloud/gc.ps1 run services update-traffic ragre-api --region asia-southeast1 --project sale-chat-ai --update-tags=messenger=ragre-api-00063-vux,messenger-rc2=ragre-api-00063-vux
```

**L3. Xóa revision cũ `00059`–`00062` (nếu owner duyệt)**
```
powershell -NoProfile -File D:/gcloud/gc.ps1 run revisions delete <REVISION> --region asia-southeast1 --project sale-chat-ai
```
- Hệ quả: xóa vĩnh viễn revision, KHÔNG hoàn tác. Không thể rollback về revision đó. Revision là bất biến nên không sửa được `minScale` của `00059`–`00062`; chỉ có thể xóa. Không chạy với revision nào đang có tag hoặc traffic. Phải có xác nhận của owner trước khi chạy.

**L4. Sửa cấu hình scaling của revision (nếu cần, thay cho L1–L2)**
```
powershell -NoProfile -File D:/gcloud/gc.ps1 run services update ragre-api --region asia-southeast1 --project sale-chat-ai --cpu-throttling --min-instances=0
```
- Flag đã verify tồn tại: `gcloud run services update --help` liệt kê `--[no-]cpu-throttling` và `--min-instances` (kiểm 2026-10-12).
- Hệ quả: thay đổi template của revision mới nhất, tạo revision mới. Hạ `minScale` về 0 nghĩa là cold start cho mọi request sau khi idle. Không dùng `--set-secrets` hoặc `--env-vars-file` để tránh xóa env/secret không liệt kê.

**L5. Gắn tag bảo vệ cho digest live + rollback (ĐÃ THỰC HIỆN trong phiên này)**
```
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts docker tags add asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api@sha256:0fdce4aecdeaf0e1721344002e1e3ce14e67d4060bef3b913d09d3be4ba44cb9 asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api:live-serving
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts docker tags add asia-southeast1-docker.pkg.dev/sale-chat-ai/ragre/ragre-web@sha256:60b4c4d4cefe7674c8329c9372608c55fedac5fd1969afd826a6855de2fed407 asia-southeast1-docker.pkg.dev/sale-chat-ai/ragre/ragre-web:rollback-web-00009
```
- Trạng thái: cả hai tag **đã được gắn** (đọc lại bằng `artifacts docker tags list` — không phải `describe` từng digest), khớp đúng digest đã cho. Thao tác thêm tag, không xóa gì.
- **Tag `rollback-web-00009` là thao tác NGOÀI KẾ HOẠCH**: nó được gắn bởi lần chạy `ar-cleanup.ps1` đầu tiên khi bước gắn tag còn nằm ngoài nhánh `--dry-run`. Không phải bước L5 dự kiến. Script đã sửa để mọi thao tác ghi nằm sau `-Apply`.
- Hệ quả: hai digest này giờ có tag nên rule untagged không xóa được.
- **Xóa một AR tag là HOÀN TÁC ĐƯỢC**: `artifacts docker tags add` với cùng digest sẽ gắn lại. Chỉ xóa *digest* mới không hoàn tác.
- Rollback hai tag này:
```
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts docker tags delete asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api:live-serving
powershell -NoProfile -File D:/gcloud/gc.ps1 artifacts docker tags delete asia-southeast1-docker.pkg.dev/sale-chat-ai/ragre/ragre-web:rollback-web-00009
```

**L6. Bật cleanup policy (chỉ sau L5 và sau khi đọc dry-run)**
```
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/ar-cleanup.ps1
```
- Chạy không tham số = **dry-run**: chỉ in kế hoạch, KHÔNG gắn tag. Nhưng **`set-cleanup-policies --dry-run` vẫn GHI policy** vào repo (chỉ bật cờ `cleanupPolicyDryRun=true`, Artifact Registry không xoá gì). Đây không phải thao tác trắng.
- Trạng thái hiện tại (đã đọc lại): cả 2 repo đã có `cleanupPolicies` + `cleanupPolicyDryRun: true`. Trước phiên audit hai repo này **không có** `cleanupPolicies`.
- Thêm `-Apply` mới (a) gắn tag bảo vệ, và (b) tắt dry-run để policy thực sự xoá.
- Policy nằm ở `scripts/ar-cleanup-policy.json`: giữ 5 version gần nhất mỗi package + xóa untagged cũ hơn **7 ngày**.
- **Bước 0 (chỉ khi `-Apply`) gắn tag bảo vệ** cho 2 digest:
  - `live-serving` → `sha256:0fdce4ae...` (ragre-api-00044-qbb, đang serve 100%)
  - `rollback-web-00009` → `sha256:60b4c4d4...` (ragre-web-00009-pos, rollback path)
- **Vì sao phải gắn tag AR**: tag `candidate-web` / `messenger` là tag **revision của Cloud Run**, KHÔNG phải tag của Artifact Registry. Cleanup policy chỉ nhìn thấy AR tag, nên nếu không gắn tag AR thì rule untagged 7 ngày sẽ xóa mất cả digest đang serve lẫn digest rollback.
- **Ước tính sẽ xóa** (không phải số đo trực tiếp): 55 digest untagged cũ hơn 7 ngày ≈ 131 GiB → ≈ $13,1/tháng (mô phỏng). `imageSizeBytes` không có trong API response nên không đo được dung lượng chính xác từng digest.
- Lưu ý: `gcloud --dry-run` chỉ validate schema rồi echo policy (`olderThan: 1209600s`) — nó KHÔNG liệt kê version sẽ xóa.

**L7. Xóa image digest cụ thể (không khuyến nghị)**
- Chỉ xóa digest KHÔNG có tag, KHÔNG phải digest live, KHÔNG nằm trong danh sách mục 5.2. Lệnh xóa không hoàn tác được.

## 10. Có xuống được $10/tháng không?

**Không. Sàn kỹ thuật ≈ $18–19/tháng.** $10 là bất khả thi nếu giữ Cloud Run trong project này.

Sàn được cấu thành bởi hai khoản không thể ép xuống bằng cấu hình:

| Khoản | Sàn | Vì sao không xuống được |
|---|---|---|
| Artifact Registry | **≈ $18,2/tháng** | 181,9 GiB đo được (174.504,9 MB `cloud-run-source-deploy` + 11.733,5 MB `ragre`). Cleanup 7 ngày đưa về ≈ 71 GiB ≈ $7,1. Nhưng `ragre-api` image ≈ 3.19 GiB × số version cần giữ, và **dưới 14 ngày thì mất khả năng rollback** |
| Cloud Run | **≈ $1–2/tháng** | Cold start sau mỗi lần idle. Một request ≈ 1–3 vCPU-s + 1 GiB-s. Nếu có traffic thật thì khoản này tăng theo usage — đây là chi phí biến đổi, không phải rò rỉ |
| Secret/Storage/Logging | **≈ $0.1/tháng** | Đo được: 28 version ENABLED, storage 2.1 GB, logging bucket 30 ngày |

Đường đi thực tế (cập nhật theo trạng thái **đã áp dụng**):

| Mức | Đạt được bằng | Đánh đổi |
|---|---|---|
| **≈ $115/30 ngày** (trước) | Không làm gì | `00063-vux` đốt $62,2/tháng vô ích + AR 181,9 GiB |
| **≈ $25/tháng** ← **ĐANG Ở ĐÂY** | Chuyển tag khỏi `00063-vux` (**đã làm**) + cleanup AR 7 ngày (**đã bật**) | Cold start khi idle; mất image cũ hơn 7 ngày |
| **≈ $20/tháng** | Thêm: cắt docling/torch khỏi image BE | Việc riêng; cần import-smoke trước |
| **$10/tháng** | **Không có đường** bằng tối ưu cấu hình | AR một mình đã ≈ $15 sau khi dọn |

Muốn thật sự xuống $10 thì phải thay đổi kiến trúc, không phải tối ưu cấu hình:

1. **Chuyển image sang GitHub Container Registry** (miễn phí cho repo public, $0.25/GB cho private). `ragre-api` ≈ 3.19 GiB × 5 version ≈ 16 GiB ≈ **$4/tháng** thay vì $17. Cloud Run pull được image từ registry ngoài. **Đây là đòn duy nhất đủ mạnh để về $10.** Rủi ro: phụ thuộc registry ngoài, cần cấu hình auth cho Cloud Run.
2. **Giảm dung lượng image** (`ragre-api` 3.19 GiB/digest là lớn — thường do model/cache nhúng trong image). Multi-stage build + `.dockerignore` có thể cắt còn 1 GiB → $5/tháng ở 5 version. **Chưa đo được thành phần image**, cần kiểm `docker history`.
3. **Xóa hẳn `ragre-web` khỏi Cloud Run** nếu không dùng (hiện chỉ $0.1/tháng — không đáng).

Khuyến nghị: chốt mục tiêu **≈ $22/tháng** trước (hai bước đầu, rủi ro thấp, đã có script). Nếu cần $10 thì phải làm (1) hoặc (2) — cả hai đều là việc riêng, cần đo dung lượng image trước khi hứa con số.

## 11. Việc chưa xác minh

- ~~Số instance còn sống của `00059`–`00062`~~ → **đã đo**: `container/cpu/allocation_time` 24h gần nhất bằng 0 cho `00059`–`00062`. Không tốn tiền.
- ~~Memory cost của `00063-vux`~~ → **đã đo lại (24h, 2026-10-12)**: 86,415 vCPU-s + 86,355 GiB-s/ngày ≈ **$62,2/tháng**. (Con số 82,810 GiB-s và "$60/tháng" ở phiên trước là SAI — đã sửa.)
- **Thành phần dung lượng image `ragre-api` (3.19 GiB/digest) chưa đo** — cần `docker history` hoặc `crane manifest` mới biết cắt được bao nhiêu.
- **Hành vi Cloud Run với revision `minScale=1` khi mất hết tag: chưa đo.** `00063-vux` vẫn giữ `minScale='1'` + `cpu-throttling='false'` trong spec sau khi chuyển tag; có còn tính tiền hay không phải đọc lại `cpu/allocation_time` 24h sau đó.
- **AR tag `latest` trên repo `ragre` trỏ digest nào: chưa kiểm bằng `describe`.**
- **`00050-tg7` KHÔNG dùng được làm đích chuyển tag — nó đã bị Retired.** `gcloud run revisions describe ragre-api-00050-tg7` trả `Ready=True` với `reason: Retired`, `Active=False` ("Revision retired."), `ResourcesAvailable=Unknown`. [SUY LUẬN] Cloud Run thu hồi tài nguyên ~1 giây sau khi tạo — suy ra từ hai timestamp `lastTransitionTime` (10:06:37.495299Z cho ContainerReady và 10:06:37.707511Z cho Retired), **không phải quan sát trực tiếp**. Nó cùng digest với `00063-vux` và cùng cấu hình throttled (`minScale='0'`, `cpu-throttling='true'`, `client-name=cloud-console`), nhưng không phục vụ được. Phương án đúng là **deploy revision MỚI ghim `--image=<digest 8a5555c2...>`** (không dùng `--source .` để tránh build từ working tree dirty).
- **Bẫy env khi deploy bằng `--image` — cổng env đã chứng minh bắt được, hành vi kế thừa vẫn chưa đo.** Hành vi của `gcloud run deploy --image` khi không truyền `--set-secrets`/`--env-vars-file` (lấy env từ service template hay từ revision đang serve) **chưa kiểm chứng**. Đã **chạy thật** `envcmp.ps1 -A ragre-api-00063-vux -B ragre-api-00044-qbb` (read-only): so 50 khoá, in ra **đúng 8 khoá `MESSENGER_*` là `<missing>` ở `00044-qbb`** — `MESSENGER_APP_SECRET`, `MESSENGER_BOT_ENABLED`, `MESSENGER_GRAPH_API_VERSION`, `MESSENGER_IDENTITY_SECRET`, `MESSENGER_OPEN_DEMO_ACCESS`, `MESSENGER_PAGE_ACCESS_TOKEN`, `MESSENGER_PAGE_ID`, `MESSENGER_VERIFY_TOKEN` (8 DIFF tổng, khoá thứ 9 là `OUTBOUND_ALLOWED_HOSTS`). Số env: `00063-vux`=49, `00050-tg7`=49, `00044-qbb`=42 (số 62/51 ghi ở phiên trước là SAI). **Cổng env là bắt buộc trước khi chuyển tag** — và nó đã được chứng minh là bắt đúng tập biến cần bắt.
- **`OUTBOUND_ALLOWED_HOSTS`**: chỉ có ở `00044-qbb`; `00063-vux`/`00050-tg7` không có. Không gây hỏng vì `config.py` có `default_factory` gồm `openrouter.ai`, `generativelanguage.googleapis.com`, `api.jina.ai`. **Số dòng khác nhau theo nhánh** (đã kiểm bằng `git grep`): `api/infrastructure/config/config.py:261` trên `main` (working tree hiện tại), `:346` trên `de3d3ee`, `:407` trên `feat/connect-messenger-bot`. Cả hai citation ở phiên trước đều đúng — chỉ khác ref.
- **`ci.yml` deploy `--max-instances=20`** trong khi service template hiện tại là `maxScale=2` và runbook ghi 2. Push `main` sẽ **nâng trần lên 20** → nếu bị spam có thể nhân 20 instance. Cần chốt một giá trị trước khi push.
- **Bằng chứng `00063-vux` chạy với minScale=1 thật**: `status.conditions` của nó có `MinInstancesProvisioned=True` với message "Min instances provisioned successfully", còn `00050-tg7` bị Retired. `instance_count` group by revision (7 ngày, ALIGN_MAX): `00063-vux`=1, `00044-qbb`=3.
- `00060-dar` (10-06) đốt **257,616 vCPU-s trong 7 ngày** — revision always-CPU thứ hai bị bỏ quên. Đã ngừng đốt (24h gần nhất = 0).
- Chưa xác định được tỷ lệ chính xác giữa CPU request-based và min-instance trong `cpu/allocation_time` — Cloud Run không tách metric này. Vì vậy chi phí CPU nêu dạng dải, không phải một số duy nhất.
- Chưa có quyền đọc Billing Reports API (403 với tài khoản hiện tại), nên không đối chiếu được với hoá đơn thực tế. Mọi con số đều suy ra từ số đo Monitoring × đơn giá SKU đã verify.
