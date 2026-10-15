# Hướng dẫn giảm chi phí Cloud Run — nhánh Messenger (`ragre-api`)

> **CẬP NHẬT 2026-10-14 — ĐO LẠI, VÀ ĐÍNH CHÍNH LẦN ĐO ĐẦU TRONG NGÀY.** Các con số `00063-vux` ở dưới là LỊCH SỬ, không còn đúng.
>
> **ĐÍNH CHÍNH:** bản sửa đầu tiên của mục này (cùng ngày, 05:50Z) ghi `00079-hib` ≈ **$35/tháng** và suy ra "instance không scale về 0". **SAI.** Cửa sổ "1 giờ gần nhất" đó trùng đúng lúc phiên chẩn đoán đang bắn liên tục vào URL tag `messenger` (verify webhook + 6 câu hỏi giá/lãi suất). Với `cpu-throttling=false`, mỗi request làm instance nóng và CPU bị tính suốt thời gian instance tồn tại — nên số đo phản ánh tải của người chẩn đoán. Số đúng:
>
> - `container/instance_count` bucket 60s: `00079-hib` = **1 instance từ 05:28 đến 05:58Z, rồi 0 từ 05:59Z**; `cpu/allocation_time` lúc 06:06Z = **0 vCPU-s**. Revision **không có `minScale`** ⇒ **tự scale về 0 khi rảnh**. Không có vòng lặp nền nào giữ nó sống.
> - Duty cycle 24h thật: `00079-hib` = **4,093 vCPU-s + 4,012 GiB-s trên 1.5h active** ⇒ ≈ **$2.94/tháng nếu chạy 24/7**, và **≈ $0.10/ngày** theo tải thật. Không phải $35.
> - `00044-qbb` (min=1, throttled): 86,329 GiB-s/24h × $0.0000035 ⇒ **≈ $9.06/tháng** — khoản lớn nhất, **toàn bộ là memory min-instance** cho 20 request/ngày. CPU idle đo được = 2 vCPU-s/24h ≈ $0.
> - `00066-jic` (tag `messenger-rc2`, throttled): 34 vCPU-s/24h ⇒ ≈ $0.04/tháng. Gỡ tag không tiết kiệm đáng kể.
> - `00071`–`00078`: ≈ $8/24h **một lần** trong lúc deploy, hiện **0 instance**. Không lặp lại.
> - **Steady state Cloud Run ≈ $12/tháng**, không phải $45. Tổng (Cloud Run + AR $8.2 + Secret $2.0 + Storage $0.05) ≈ **$22/tháng**.
>
> **Ràng buộc always-CPU vẫn đúng**: webhook Messenger trả 200 rồi xử lý nền, nên không đề xuất bật throttling cho `00079`. Nhưng nó **không** tốn $35/tháng — chỉ tốn khi có request.
>
> **Khoản tiết kiệm lớn nhất còn lại là `minScale=1` của `00044-qbb` (≈ $9/tháng).** Đổi sang `minScale=0` đánh đổi cold start 30–40s cho request đầu sau khi rảnh. **Cần duyệt**, và **không** chạy `gcloud run services update` thiếu `--no-traffic` (sẽ đẩy revision mới nhất lên 100% traffic).

---

> Đây là **chỉ dẫn**, chưa áp dụng vào bất kỳ đâu. Ngày kiểm: 2026-10-12. Project `sale-chat-ai`, region `asia-southeast1`.

## Nhãn nguồn dùng trong file

- **[ĐO]**: Cloud Monitoring, 24h gần nhất, đã đo.
- **[SKU]**: usage × đơn giá SKU đã verify từ Cloud Billing API (service `152E-C115-5142`). **Chưa đối chiếu hoá đơn thật**: Billing Reports API trả 403 với tài khoản hiện tại.
- **[CODE]**: đã đọc file trong worktree, số dòng đã xác nhận lại.
- **[GCP]**: `gcloud ... describe` chỉ đọc, đã chạy.
- **[RUNBOOK]**: chỉ là lời ghi trong runbook, chưa đo.
- **[SUY LUẬN]**: chưa xác minh, phải kiểm tra trước khi tin.

---

## 0. Tóm tắt

1. Mỗi revision chạy **always-CPU** (`--no-cpu-throttling`, `cpu-throttling=false`) với 1 instance liên tục tốn khoảng **$56/tháng** theo lý thuyết [SKU]. Revision `00063-vux` đo được **≈ $46/tháng** (24h, 2026-10-12: 86.415 vCPU-s + 86.355 GiB-s/ngày).
2. Revision throttled với `min-instances=1` chỉ tốn khoảng **$5.4/tháng** cho RAM [ĐO + SKU]. `00044-qbb` đang serve 100% đúng cấu hình này. CPU idle của nó đo được ≈ 0 (44 vCPU-s/ngày).
3. **Không được bỏ `--no-cpu-throttling` ngay.** Các loop nền trong process sẽ chạy sai nhịp (mục 2). Phải tách loop ra trước (phương án A).
4. Hai nguồn đang đẩy always-CPU vào production: `ci.yml` (mỗi push `main`) và `deploy-be.ps1`. Đây là lý do cờ cần gỡ.
5. `ragre-api-00050-tg7` **đã bị Cloud Run thu hồi (Retired)**, không dùng được làm đích chuyển tag.

---

## 1. Mục tiêu và số tiền

| Cấu hình | Công thức | Số tiền/tháng | Nguồn |
|---|---|---|---|
| Always-CPU, 1 vCPU, 1 instance liên tục (`00063-vux`) | 86.415 vCPU-s/ngày × 30 × $0.0000216 + 86.355 GiB-s/ngày × 30 × $0.0000024 | **≈ $46** | [ĐO 24h 2026-10-12] + [SKU] |
| Lý thuyết always-CPU 1 vCPU + 1 GiB, đủ 30 ngày | 2.592.000 × $0.0000216 + 2.592.000 × $0.0000024 | ≈ $62 | [SKU] |
| Throttled, `min=1`, RAM 1 GiB (`00044-qbb`) | 86.367 GiB-s/ngày × 30 × $0.0000035 | **≈ $5.4** | [ĐO 24h] + [SKU] |
| Throttled, `min=1`, CPU idle (lý thuyết) | 2.592.000 vCPU-s × $0.0000035 | ≈ $9.1 nếu bị tính đủ | [SKU] |
| Throttled, `min=1`, CPU idle (đo thực tế `00044-qbb`) | 44 vCPU-s/ngày | ≈ $0.003 | [ĐO] |
| `ragre-web` cả 2 revision | 233 vCPU-s + 62 GiB-s/ngày | ≈ $0.1 | [ĐO] |
| Worst case always-CPU nếu `max-instances=20` (`ci.yml`) | 20 × ~$62 | ≈ $1.240 | [SKU], trần lý thuyết |

Ghi chú:
- **Số đo đã làm mới (24h, 2026-10-12).** Các con số 82.810 vCPU-s/82.822 GiB-s và "$59,6"/"$8,7" ghi ở phiên trước là SAI — đã thay bằng số đo lại. `00063-vux` = 86.415 vCPU-s + 86.355 GiB-s/ngày ≈ **$46/tháng**.
- CPU idle của min-instance throttled đo được ≈ 0 (44 vCPU-s/ngày cho `00044-qbb`) — **khớp** với thực tế. Lý thuyết ~$9/tháng/vCPU là trần trên, không xảy ra. Không còn mâu thuẫn cần hoá đơn để giải quyết.
- Run-rate tổng đo được ≈ **$68/tháng** (Cloud Run ≈ $51 + AR ≈ $17 + Secret/Storage/Logging ≈ $0.1). Sàn kỹ thuật ≈ $18–19/tháng. Muốn về $10 phải đổi registry hoặc giảm dung lượng image — xem `GCLOUD_COST_AUDIT.md` mục 10.
- Artifact Registry `cloud-run-source-deploy` 166.420 MB + `ragre` 10.878 MB ≈ 177 GiB ≈ $17/tháng [ĐO + SKU]. Khoản tiết kiệm phụ, ngoài phạm vi cờ CPU.

---

## 2. Vì sao cờ `--no-cpu-throttling` tồn tại

Trong `api/interfaces/api/main.py` [CODE], `_lifespan` khởi động các task nền **trong cùng process** với web:

| Task | Vị trí | Chu kỳ | Ghi chú |
|---|---|---|---|
| RAG prewarm | `main.py:963`, `_maybe_start_rag_prewarm` (`:876-890`) | Một lần lúc startup | Không phải loop. Không ảnh hưởng cờ CPU sau khi chạy xong. |
| Lead mirror reconciliation | `main.py:964`, `_maybe_start_lead_mirror_reconciliation` (`:811-827`) | Vòng lặp, `asyncio.sleep(lead_mirror_sweep_interval_seconds=300)` (`lead_mirror_reconciliation.py:85`) | Chỉ chạy khi `FIREBASE_BINDING == "firestore"` (`main.py:819`). **Cả `00044-qbb` và `00063-vux` đều có `FIREBASE_BINDING=firestore`** [GCP]. Quét mirror cũ hơn `lead_mirror_stale_after_minutes=300` (`config.py:616`). |
| Chat history retention | `main.py:965-966`, `_chat_history_retention_loop` (`:769-777`) | `asyncio.sleep(86400)` | Mặc định bật (`config.py:647`). Env `CHAT_HISTORY_RETENTION_ENABLED` không có trên cả hai revision [GCP] → dùng mặc định `true`. |
| Messenger reconciliation | `messenger_bot_runtime.py:78`, `messenger_bot_reconciliation.py:27-30` | Vòng `run()` có poll interval giới hạn | Chỉ chạy khi `MESSENGER_BOT_ENABLED=true`. **`00063-vux` có `MESSENGER_BOT_ENABLED=true`** [GCP]. Không được brief liệt kê, nhưng cùng loại vấn đề. Chu kỳ cụ thể chưa đọc được [SUY LUẬN]. |

Cơ chế:
- Khi throttling **bật**, CPU chỉ được cấp khi đang xử lý request. Giữa các request, event loop bị đóng băng; các `asyncio.sleep` không chạy đúng chu kỳ.
- Khi throttling **tắt** (`--no-cpu-throttling`), CPU được cấp liên tục, loop chạy đúng nhịp.

Phân biệt đường chính và lưới an toàn:
- `sync_lead_mirror_after_commit` là **đường chính**, gọi inline khi ghi lead (`api/interfaces/api/lead.py:239`, `crm_customer_service.py:279, 341`, `lead_service.py:313`) [CODE]. Lead vẫn đồng bộ khi có request.
- Loop reconciliation là **lưới an toàn** để retry các lead mirror `pending/failed` mà đường chính bỏ sót.

> **Phải nói thẳng:** bỏ `--no-cpu-throttling` mà không xử lý loop = giảm tiền, nhưng sweep mirror và retention chạy sai nhịp. Lead bị kẹt ở trạng thái pending/failed sẽ chỉ được retry khi có request tiếp theo. Messenger reconciliation cũng trễ tương tự.

---

## 3. Ba phương án (xếp hạng)

### (A) Đúng và rẻ nhất — tách loop sang Cloud Run Job + Cloud Scheduler, rồi bật throttling. **Khuyến nghị chính.**

- **Ý tưởng:** service serving chạy throttled, `min=1`, ~$9/tháng. Các sweep định kỳ chạy trong Cloud Run Job với always-CPU trong lúc chạy, được Scheduler gọi.
- **Chi phí ước tính:** service ~$9 + Job ~$3 [SUY LUẬN: Job chạy 10s, 1 vCPU, 0.5 GiB, mỗi 5 phút, tính theo SKU request-based; SKU Jobs chưa có trong danh sách đã verify].
- **Việc phải viết (chưa tồn tại trong repo):** một entrypoint, ví dụ `api/interfaces/jobs/maintenance.py`, nhận tham số `sweep` hoặc `retention`:
  - `sweep`: lấy `get_lead_repository()` và `get_realtime_lead_mirror()`, gọi `sweep_stale_lead_mirrors_once(repo=..., mirror=..., stale_before=...)` (`lead_mirror_reconciliation.py:44`). Logic `stale_before` lấy từ `lead_mirror_stale_after_minutes`, như `run_lead_mirror_reconciliation_loop` (`:89-93`).
  - `retention`: gọi `repository.cleanup(retention_days=...)` (`postgres_chat_history.py:369`).
  - Entrypoint **không được** khởi động RAG prewarm hay messenger/zalo runtime.
- **Lệnh tạo Job (cú pháp chưa chạy thử [SUY LUẬN]):**
```bash
gcloud run jobs deploy ragre-maintenance-sweep \
  --source . \
  --region asia-southeast1 \
  --command=python \
  --args=-m,api.interfaces.jobs.maintenance,sweep \
  --cpu=1 --memory=512Mi \
  --max-retries=0 \
  --task-timeout=300 \
  --set-secrets=<cùng 13 binding như deploy-be.ps1 $Secrets> \
  --env-vars-file=env-prod.yaml
```
Tạo thêm `ragre-maintenance-retention` với `--args=-m,api.interfaces.jobs.maintenance,retention`.

- **Lịch Scheduler (cú pháp chưa chạy thử [SUY LUẬN]):**
```bash
gcloud scheduler jobs create http ragre-maintenance-sweep-tick \
  --location=asia-southeast1 \
  --schedule="*/5 * * * *" \
  --uri="https://run.googleapis.com/v2/projects/sale-chat-ai/locations/asia-southeast1/jobs/ragre-maintenance-sweep:run" \
  --http-method=POST \
  --oauth-service-account-email=<SA_SCHEDULER>@sale-chat-ai.iam.gserviceaccount.com

gcloud scheduler jobs create http ragre-maintenance-retention-tick \
  --location=asia-southeast1 \
  --schedule="0 3 * * *" \
  --uri="https://run.googleapis.com/v2/projects/sale-chat-ai/locations/asia-southeast1/jobs/ragre-maintenance-retention:run" \
  --http-method=POST \
  --oauth-service-account-email=<SA_SCHEDULER>@sale-chat-ai.iam.gserviceaccount.com
```
Service account của Scheduler cần `roles/run.invoker` trên Job.

- **Đánh đổi:**
  - Phải viết và kiểm thử entrypoint + Job trước khi gỡ cờ.
  - Messenger reconciliation (`messenger_bot_runtime.py:78`) vẫn nằm trong service. Khi throttled, nó trễ đến khi có request. Cần quyết định riêng: chấp nhận trễ, hoặc đưa sweep của nó vào Job (chưa đọc hàm sweep 1 lần của messenger).
  - Thêm một hệ thống (Job + Scheduler) để vận hành.

### (B) Rẻ nhất nhưng mất nhịp loop

- `--min-instances=0`, bỏ `--no-cpu-throttling`. Idle ≈ $0 [SUY LUẬN từ SKU request-based].
- **Đánh đổi:**
  - Cold start 30–40s [RUNBOOK, dòng 166 và 288, chưa đo]. Meta webhook có thể timeout và retry; mức timeout cụ thể của Meta chưa verify [SUY LUẬN].
  - Loop chỉ chạy khi có request. Cron ping `/ready` mỗi ~9 phút (runbook dòng 288) không đảm bảo chu kỳ 300s: tick chỉ xảy ra khi có request, không phải mỗi 5 phút.
  - Cloud Run vẫn có thể thu hồi instance bất kỳ lúc nào [RUNBOOK].

### (C) Giữ nguyên always-CPU

- Đúng chức năng, nhưng **≈ $46/tháng cho mỗi revision always-CPU** đang serve hoặc giữ `min=1` [ĐO 24h].
- Chỉ chọn nếu chưa tách được loop. Nếu chọn C, phải đặt `--max-instances=2` để trần chi phí (mục 5.2).

**Thứ tự bắt buộc:** A → (sau khi Job chạy ổn định) gỡ cờ khỏi đường deploy chính. Không được gỡ cờ khỏi `ci.yml` / `deploy-be.ps1` trước khi có A, vì sẽ làm loop đóng băng ngay trên production.

---

## 4. Revision có sẵn và cách đưa Messenger lên

### 4.1. Trạng thái hiện tại [GCP]

| Revision | Digest | minScale | cpu-throttling | Env (plain + secretRef) | Trạng thái |
|---|---|---|---|---|---|
| `ragre-api-00044-qbb` | `sha256:0fdce4ae…` | 1 | true | 42 (9 secretRef) | **Đang serve 100%**, không có Messenger |
| `ragre-api-00063-vux` | `sha256:8a5555c2…` | 1 | **false** | 49 (36 plain + 13 secretRef) | Ready, tag `messenger` + `messenger-rc2`, 0% traffic. `MESSENGER_BOT_ENABLED=true` |
| `ragre-api-00050-tg7` | `sha256:8a5555c2…` (cùng digest) | 0 | true | 49 | **Retired**: `Ready=True` với `reason: Retired`, `Active=False`, `ResourcesAvailable=Unknown`, message `Revision retired.` [SUY LUẬN] thu hồi ~1 giây sau khi tạo — suy từ hai `lastTransitionTime` (10:06:37.495299Z → 10:06:37.707511Z), không quan sát trực tiếp. **Không dùng được.** |

Service `ragre-api` [GCP]:
- `latestReadyRevisionName` = `00063-vux`
- `latestCreatedRevisionName` = `00050-tg7` (đã Retired)

Hệ quả:
- `00063-vux` **vẫn là revision Ready duy nhất có Messenger.** Nó giữ `min=1` + always-CPU nên vẫn đốt **≈ $46/tháng** (đo 24h 2026-10-12: 86.415 vCPU-s + 86.355 GiB-s/ngày) dù không nhận traffic.
- Template mới nhất (`00050-tg7`) là throttled, `min=0`, `max=2`. Nhưng vì `00050-tg7` đã Retired, không được coi là đích an toàn.

### 4.2. Lưu ý trước khi deploy candidate mới

- **Tag `messenger` đang gắn `00063-vux`.** Đừng tái dùng tag này cho revision mới: hành vi gcloud khi gán tag đã tồn tại chưa được kiểm chứng [SUY LUẬN]. Dùng tag mới, ví dụ `messenger-cost`.
- **Env nền — CHƯA ĐO.** `gcloud run deploy --image` không truyền env/secret: nguồn kế thừa **chưa kiểm chứng**. Hai giả thuyết: (a) lấy từ **service template** hiện tại, hoặc (b) từ **revision đang serve** (`00044-qbb`, 42 env, không có `MESSENGER_*`). Nếu (b) đúng thì revision mới thiếu 8 biến: `MESSENGER_BOT_ENABLED`, `MESSENGER_PAGE_ID`, `MESSENGER_GRAPH_API_VERSION`, `MESSENGER_OPEN_DEMO_ACCESS` (plain) và `MESSENGER_VERIFY_TOKEN`, `MESSENGER_APP_SECRET`, `MESSENGER_PAGE_ACCESS_TOKEN`, `MESSENGER_IDENTITY_SECRET` (secretKeyRef). **Đây là giả thuyết, không phải sự thật đã đo.** Vì vậy truyền **đủ** `--env-vars-file` và `--set-secrets`, không dựa vào kế thừa. `ci.yml:208-214` ghi rằng `gcloud run deploy --source` kế thừa env + secretKeyRef từ revision đang serve, và một flag env/secret sẽ **thay thế toàn bộ** tập đó [CODE]. Sau deploy phải so tên env với `00063-vux` bằng `envcmp.ps1` (mục 6).
- **Revision không tag, không traffic sẽ bị Retired.** [SUY LUẬN] `00050-tg7` được tạo với `--no-traffic` và không có tag nào, rồi bị Retired. Vì vậy candidate **bắt buộc** phải có `--tag=messenger-cost` — nếu không nó sẽ chết y như `00050-tg7`. Chưa kiểm chứng nguyên nhân trực tiếp.
- **Không dùng `--source .`** cho candidate này: build từ working tree đang dirty (nhiều file `M`/`??` ở worktree). Dùng `--image=<digest>` để đúng bản code đã chạy.
- **`MESSENGER_OPEN_DEMO_ACCESS`** có trong env candidate. Trước khi bật, kiểm tra điều kiện theo ticket `D-ACCESSMODE` (theo memory dự án, chưa xác minh lại trong repo).

### 4.3. Lệnh deploy candidate (đã sửa theo kiểm tra)

```bash
gcloud run deploy ragre-api \
  --image=asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api@sha256:8a5555c2db48f56ba47ae2a0e63774dc3933e3160e0a5463caa0c83f3d89b273 \
  --region=asia-southeast1 \
  --project=sale-chat-ai \
  --allow-unauthenticated \
  --no-traffic --tag=messenger-cost \
  --min-instances=1 --max-instances=2 \
  --cpu-boost --cpu=1 --memory=1Gi \
  --timeout=360 \
  --startup-probe=periodSeconds=10,timeoutSeconds=5,failureThreshold=8,httpGet.path=/ready,httpGet.port=8080 \
  --set-secrets=<13 binding như deploy-be.ps1 $Secrets> \
  --env-vars-file=env-prod.yaml
```

Chỗ khác với brief gốc, và lý do:
- Thêm `--allow-unauthenticated`: webhook Meta gọi không xác thực IAM. Nếu IAM service đã là `allUsers`, flag này vẫn an toàn [SUY LUẬN]. Verify ở mục 6.
- Thêm `--cpu=1 --memory=1Gi`: khớp `00063-vux` (`limits: cpu 1, memory 1Gi` [GCP]) và runbook dòng 121.
- Thêm `--set-secrets` + `--env-vars-file`: tránh bẫy kế thừa env (mục 4.2).
- **KHÔNG** có `--no-cpu-throttling`: đây là chủ đích.

---

## 5. Bốn chỗ phải sửa trong nhánh trước khi deploy

Tất cả số dòng đã đọc lại trong worktree. **Không tự apply vào worktree này.** Các diff dưới đây là chỉ dẫn.

### 5.1. Gỡ `--no-cpu-throttling` khỏi đường deploy chính (chỉ sau khi có phương án A)

**a. `.github/workflows/ci.yml:226`** [CODE] — dòng hiện tại `          --no-cpu-throttling`:

```diff
           --min-instances=1
           --max-instances=20
           --cpu-boost
-          --no-cpu-throttling
           --timeout=360
```

**b. `scripts/deploy-be.ps1:56`** [CODE] — dòng hiện tại `    '--no-cpu-throttling',`:

```diff
     '--min-instances=1',
+    '--max-instances=2',
     '--cpu-boost',
-    '--no-cpu-throttling',
     '--timeout=360',
```

Ghi chú: `deploy-be.ps1` **không có** `--max-instances` nào [CODE, đã grep]. Thêm vào để không phụ thuộc giá trị kế thừa [SUY LUẬN về hành vi kế thừa].

### 5.2. `ci.yml:224` — `--max-instances=20` không khớp runbook

[CODE] `ci.yml:224` ghi `--max-instances=20`. [GCP] template hiện tại `maxScale=2` (`00063-vux`, `00050-tg7`, `00044-qbb` đều `maxScale=2`). Runbook `DEPLOY_RUNBOOK.md:119` ghi `--max-instances=2`.

Push `main` **sẽ nâng trần từ 2 lên 20**. Với always-CPU, trần lý thuyết ≈ $1.240/tháng (mục 1). Chốt `2`:

```diff
           --min-instances=1
-          --max-instances=20
+          --max-instances=2
           --cpu-boost
```

### 5.3. `DEPLOY_RUNBOOK.md:443-463` — candidate Messenger là nguồn rò rỉ

Hiện tại [CODE]:
- Dòng 443: `--min-instances=1 --cpu-boost --no-cpu-throttling --timeout=360`
- Dòng 455-457: `--min-instances=1 \`, `--cpu-boost \`, `--no-cpu-throttling \`
- Dòng 462-463: `--no-traffic \`, `--tag messenger`
- Dòng 439 ghi "copy arg từ `scripts/deploy-be.ps1` `$DeployArgs`, dòng 45–57" — **sai số dòng**. Block `$DeployArgs` thực tế là **dòng 49–61** [CODE]. Sửa thành "dòng 49–61" (dòng 439 của runbook).

Diff cho khối lệnh (dòng 455-457):

```diff
   --min-instances=1 \
+  --max-instances=2 \
   --cpu-boost \
-  --no-cpu-throttling \
   --timeout=360 \
```

Diff cho dòng 443:

```diff
-`--min-instances=1 --cpu-boost --no-cpu-throttling --timeout=360
+`--min-instances=1 --max-instances=2 --cpu-boost --timeout=360
```

Diff cho dòng 441:

```diff
-Cú pháp gốc trong `deploy-be.ps1` (khớp từng arg — đã đối chiếu bằng mắt 2026-10-05):
+Cú pháp gốc trong `deploy-be.ps1` `$DeployArgs` (dòng 49–61, không có `--no-cpu-throttling` sau khi mục 5.1 được áp dụng):
```

Đổi tag (dòng 463) để không đụng `messenger` đang gắn `00063-vux`:

```diff
-  --tag messenger
+  --tag messenger-cost
```

Lệnh candidate đầy đủ: xem mục 4.3.

### 5.4. `DEPLOY_RUNBOOK.md:111` — câu "bắt buộc thêm `--no-cpu-throttling`"

[CODE] dòng 111: `Nếu được duyệt bật Zalo, bắt buộc thêm \`--no-cpu-throttling\`, giữ \`--min-instances=1\``.

Câu này chỉ đúng **khi loop còn nằm trong process**. Sau khi tách loop (phương án A), không còn bắt buộc. Thay bằng điều kiện rõ:

```diff
-Nếu được duyệt bật Zalo, bắt buộc thêm `--no-cpu-throttling`, giữ `--min-instances=1`
-hoặc cao hơn, và hoàn tất mục 8. Cron ping `/ready` không thay thế CPU nền.
+Nếu được duyệt bật Zalo: loop `zalo_bot_reconciliation` (`zalo_bot_runtime.py:70`) và
+loop reconciliation/retention trong process cần CPU nền. Nếu các loop này vẫn nằm trong
+process (chưa tách sang Cloud Run Job), thêm `--no-cpu-throttling` và giữ `--min-instances=1`.
+Nếu đã tách sang Job (phương án A), không cần cờ này. Hoàn tất mục 8 trong cả hai trường hợp.
+Cron ping `/ready` không thay thế CPU nền.
```

### 5.5. Các dòng runbook mâu thuẫn số liệu (phụ, nên sửa)

- `DEPLOY_RUNBOOK.md:166`: "CPU throttling mặc định là thứ hạ hóa đơn xuống ~$13/tháng thay vì ~$52". Số ~$13 và ~$52 là ước tính cũ [RUNBOOK], không khớp đo ($5.4 RAM và $46 always-CPU).
- `DEPLOY_RUNBOOK.md:287`: "BE ~$13/tháng … always-CPU ~$52". Cập nhật theo mục 1.
- `DEPLOY_RUNBOOK.md:294`: ghi `--cpu-always-allocated` là "tên cũ". Chưa kiểm chứng lại trong nhánh này.
- `DEPLOY_RUNBOOK.md:130`: đã ghi đúng mức "runbook mặc định đã bỏ cờ". Giữ nguyên, nhưng khớp với 5.1 sau khi áp dụng.

---

## 6. Checklist lúc deploy (theo thứ tự)

- [ ] **0. Chốt phương án.** Chọn A. Nếu chọn C, đặt `--max-instances=2` trước khi làm bất kỳ bước nào khác.
- [ ] **0b. Tạm dừng push `main`** cho đến khi mục 5.1 và 5.2 hoàn tất. Mỗi push `main` hiện tại tạo revision always-CPU và đưa **100% traffic** (`ci.yml:218-228`, không có `--no-traffic`).
- [ ] **1. Bảo vệ digest live.** Gắn tag Artifact Registry cho digest `8a5555c2…` để cleanup không xoá image khi revision bị gỡ [SUY LUẬN cú pháp, chưa chạy thử]:
  ```bash
  gcloud artifacts docker tags add \
    asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api@sha256:8a5555c2db48f56ba47ae2a0e63774dc3933e3160e0a5463caa0c83f3d89b273 \
    asia-southeast1-docker.pkg.dev/sale-chat-ai/cloud-run-source-deploy/ragre-api:keep-messenger-8a5555c2
  ```
- [ ] **2. Snapshot trạng thái (chỉ đọc).** Lưu tên env, không lưu giá trị:
  ```bash
  gcloud run services describe ragre-api --region asia-southeast1 --format="yaml(status.traffic,status.latestReadyRevisionName)"
  gcloud run revisions describe ragre-api-00063-vux --region asia-southeast1 --format="json(spec.containers[0].env)"
  ```
- [ ] **3. Phương án A hoàn tất trước:** entrypoint `api/interfaces/jobs/maintenance.py` đã viết và test (theo dõi `sweep` và `retention`); Job và Scheduler đã tạo; đã chạy thủ công một lần và thấy log thành công.
- [ ] **4. Deploy candidate, không traffic:** lệnh mục 4.3, tag `messenger-cost`.
- [ ] **5. So env với `00063-vux` (tên, không giá trị):**
  - Kỳ vọng: **49 env**, **36 plain**, **13 secretKeyRef**, tập tên trùng khớp hoàn toàn với `00063-vux`.
  - `OUTBOUND_ALLOWED_HOSTS` có trên `00044-qbb` nhưng **không có** trên `00063-vux`. Chấp nhận được: `api/infrastructure/config/config.py:407` (nhánh `feat/connect-messenger-bot`) có `default_factory` với `openrouter.ai`, `generativelanguage.googleapis.com`, `api.jina.ai` [CODE]. Trên `main` cùng khai báo nằm ở `:261` — số dòng khác nhau theo nhánh.
  - Nếu tập tên lệch → **dừng**, không promote.
- [ ] **6. Kiểm tra Ready và IAM của revision mới:** `status.conditions` phải có `Ready=True` **và** `Active=True` (không `reason: Retired`). Kiểm tra `roles/run.invoker` cho `allUsers` trên service.
- [ ] **7. Smoke trên tag URL** (`https://messenger-cost---ragre-api-aj6vbt6m3q-as.a.run.app`): `/ready` trả 200. Không gửi tin nhắn thật.
- [ ] **8. Promote — CẦN JUSTIN DUYỆT TỪNG LỆNH, không phải bước thường quy.** Lệnh này đẩy **100% traffic production** sang candidate. Chỉ chạy sau khi bước 5–7 đạt và Justin đồng ý rõ ràng:
  ```bash
  gcloud run services update-traffic ragre-api --region asia-southeast1 --to-tags=messenger-cost=100
  ```
- [ ] **9. Rollback sẵn sàng:** đích rollback là `00044-qbb` (pre-Messenger, digest `0fdce4ae…`, đang là revision serving trước khi promote):
  ```bash
  gcloud run services update-traffic ragre-api --region asia-southeast1 --to-revisions=ragre-api-00044-qbb=100
  ```
- [ ] **10. Gỡ revision always-CPU cũ** (chỉ sau khi xác nhận Justin, và sau khi candidate đã ổn định):
  ```bash
  gcloud run services update-traffic ragre-api --region asia-southeast1 --remove-tags=messenger,messenger-rc2
  gcloud run revisions delete ragre-api-00063-vux --region asia-southeast1
  ```
  Lệnh này **xoá** revision. Digest đã được bảo vệ ở bước 1.
- [ ] **11. Đo lại sau 24h:** `container/cpu/allocation_time` và `container/memory/allocation_time` của `00063-vux` phải về 0 (hoặc revision đã bị xoá). Ước tính sau thay đổi: ~$9/tháng RAM của `min=1` throttled.
- [ ] **12. Cập nhật tag rollback** trong runbook sau khi xác nhận: serving = candidate, rollback = `00044-qbb`.

---

## 7. Cảnh báo: `env-prod-candidate.yaml` (untracked)

Kết quả kiểm tra [CODE/GCP], không in giá trị:

- File có **36 khoá**, toàn bộ là khoá không nhạy cảm theo tên (ví dụ `CORS_ORIGINS`, `EMBEDDING_BINDING`, `POSTGRES_HOST`, `R2_ENDPOINT`, `MESSENGER_PAGE_ID`).
- Không có khoá nào có tên chứa `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `PRIVATE`, `CREDENTIAL`.
- Không có dòng nào khớp mẫu bí mật phổ biến (`AIza…`, `sk-…`, PEM private key, `ya29.`, URL Postgres có mật khẩu, JWT). Đây là kiểm tra theo mẫu, **không phải kiểm tra đầy đủ**.
- **Vấn đề thật (đã kiểm bằng `git check-ignore -v`):** `.gitignore` chặn `/env-prod.yaml` (dòng 152) và `/env-staging.yaml`. **`env-prod-candidate.yaml` KHÔNG khớp rule nào** — `git check-ignore -v env-prod-candidate.yaml` không trả gì. File đang untracked; một lệnh `git add .` sẽ đưa nó vào commit.
- **Lưu ý về phạm vi kiểm tra:** kết quả "không có literal" ở trên **chỉ kiểm TÊN khoá** theo mẫu nhạy cảm, **chưa kiểm GIÁ TRỊ**. Một secret literal nằm dưới khoá trung tính (ví dụ `POSTGRES_HOST=...`) sẽ lọt qua kiểm tra này.

Khuyến nghị:
- **Không `git add` / không stage `env-prod-candidate.yaml`** cho tới khi Justin quyết.
- Thêm vào `.gitignore` (nếu Justin đồng ý): `/env-prod-candidate.yaml`.
- Nếu sau này phát hiện giá trị secret dạng literal trong file, **không commit và báo Justin**.

---

## 8. Việc còn mở (chưa verify)

- [ ] Hành vi `gcloud run deploy --image` khi không truyền env: lấy từ template service hay từ revision đang serve. **Chưa kiểm chứng** — `envcmp.ps1` sẽ trả lời.
- [ ] Nguyên nhân `00050-tg7` bị Retired (giả thuyết: không tag + không traffic). Chưa kiểm chứng.
- [ ] Cú pháp `gcloud run jobs deploy --command/--args`, `gcloud scheduler jobs create http` (mục 3, 6). Chưa chạy thử. (`artifacts docker tags add` và `delete-cleanup-policies` **đã verify** bằng `--help` và bằng chạy thật.)
- [ ] SKU Cloud Run Jobs chưa có trong danh sách đã verify.
- [ ] Thành phần dung lượng image `ragre-api` (3.19 GiB/digest) chưa đo — quyết định khả năng xuống dưới $18/tháng.
- [ ] Hành vi Cloud Run với revision `minScale=1` sau khi mất hết tag: chưa đo. Phải đọc lại `cpu/allocation_time` 24h sau khi chuyển tag.
- [ ] Chu kỳ `messenger_bot_reconciliation` (`run()` poll interval) chưa đọc.
- [ ] Timeout webhook của Meta khi cold start: chưa verify.
- [ ] `ZALO_BOT_ENABLED` trên revision: chưa kiểm tra giá trị.
- [ ] Giá tier-2 `asia-southeast1` đã dùng trong SKU bảng mục 1 [SKU]; chưa đối chiếu hoá đơn (Billing Reports API trả 403 với tài khoản hiện tại).
