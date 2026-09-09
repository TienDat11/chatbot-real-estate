# DEPLOY RUNBOOK — ragre (BE Cloud Run + FE Cloud Run + Firebase Hosting)

> Tài liệu hướng dẫn triển khai production cho chủ dự án. Gõ từng lệnh theo thứ tự.
> Tất cả giá trị nhạy cảm là PLACEHOLDER (`<...>`) — thay bằng giá trị thật khi gõ, KHÔNG đưa vào git.
>
> Kiến trúc:
> - **BE** `ragre-api`: Cloud Run `asia-southeast1` (Dockerfile ở repo root đã có sẵn), source = repo root.
> - **FE** `ragre-web`: Cloud Run `asia-southeast1` (Dockerfile tại `apps/web/Dockerfile`, build context = **repo root**).
> - **Firebase Hosting** site `sale-chat-bot-11e49`: rewrite mọi request → Cloud Run `ragre-web` (không dùng Firebase App Hosting — Next.js 16.3 chưa nằm trong dải version App Hosting hỗ trợ chính thức, xem mục 0.3).

---

## 0. Điều kiện tiên quyết

### 0.1. CLI + xác thực
```bash
gcloud auth login
gcloud config set project <FIREBASE_PROJECT_ID>
npm install -g firebase-tools
firebase login
```

### 0.2. Billing Blaze + bật API
Billing phải là gói **Blaze** (Cloud Run + Cloud Build + Secret Manager yêu cầu).
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  firebase.googleapis.com firestore.googleapis.com
```

### 0.3. Quyết định đường deploy FE (đã chốt)
**Firebase App Hosting KHÔNG được dùng** cho FE này. Bằng chứng (firebase.google.com/docs/app-hosting, mục frameworks & tooling, truy vấn 2026-09-06): App Hosting chỉ hỗ trợ chính thức Next.js 13.5.x / 14.2.x / 15.0.x–15.2.x; "App Hosting does not automatically provide active support for newly released framework versions" và "versions outside of these ranges may work but are not officially supported" → Next.js **16.3.0** chưa được hỗ trợ chính thức.
**Đường đi chốt: Firebase Hosting (classic) + rewrite → Cloud Run `ragre-web`.** Hoạt động độc lập với version Next.js.

### 0.4. Gắn Firebase CLI vào project (tạo .firebaserc thật)
File `apps/web/.firebaserc` đang chứa placeholder. Chạy lệnh sau ở `apps/web/` rồi chọn project + alias `default` — CLI sẽ tự ghi project id thật vào `.firebaserc`:
```bash
cd apps/web
firebase use --add
cd ..
```
Site hosting đã/đang dùng là `sale-chat-bot-11e49`. Nếu đó **không phải** site mặc định của project, khai báo target bằng cách thêm vào `.firebaserc` (bên cạnh `projects`) và tag `hosting` trong `firebase.json` với `"target": "ragre-web"`:
```json
"hosting": { "ragre-web": { "site": "sale-chat-bot-11e49" } }
```

### 0.5. Artifact Registry (chứa image FE)
```bash
gcloud artifacts repositories create ragre --repository-format=docker --location=asia-southeast1
```
(Khuyến nghị) Tạo `.gcloudignore` ở repo root để nguồn upload cho BE không chứa file nhạy cảm:
```
.git
.gitignore
.env
.env.*
**/node_modules
data/
*.log
```

---

## 1. Secret Manager — 8 secrets của BE

Tạo từng secret (thay placeholder). Secret KHÔNG bao giờ nằm trong env var phẳng hay trong git.

```bash
# 1. Mật khẩu Postgres (Supabase)
printf '%s' '<POSTGRES_PASSWORD>' | gcloud secrets create POSTGRES_PASSWORD --data-file=-

# 2. Secret ký danh tính ẩn danh — tối thiểu 32 ký tự (config.py bắt buộc ở prod)
openssl rand -base64 48   # chạy thử để lấy giá trị, dán vào lệnh dưới
printf '%s' '<ANON_IDENTITY_SECRET>' | gcloud secrets create ANON_IDENTITY_SECRET --data-file=-

# 3. Secret HMAC mirror lead — tối thiểu 32 ký tự
printf '%s' '<LEAD_MIRROR_HMAC_SECRET>' | gcloud secrets create LEAD_MIRROR_HMAC_SECRET --data-file=-

# 4. API key LLM
printf '%s' '<LLM_API_KEY>' | gcloud secrets create LLM_API_KEY --data-file=-

# 5. API key Embedding
printf '%s' '<EMBEDDING_API_KEY>' | gcloud secrets create EMBEDDING_API_KEY --data-file=-

# 6. API key Rerank
printf '%s' '<RERANK_API_KEY>' | gcloud secrets create RERANK_API_KEY --data-file=-

# 7. Firebase service-account private key.
#    The application needs only the PEM private-key value, not the whole JSON file.
#    Extract it locally without committing the JSON key file:
#      PowerShell: (Get-Content path/to/sa.json -Raw | ConvertFrom-Json).private_key
#      jq:         jq -r .private_key path/to/sa.json
#    Store the resulting PEM as the secret value. The app converts literal \n
#    escapes back to newlines before signing JWT assertions.
gcloud secrets create FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY --data-file=private-key.pem
#    Never upload the complete service-account JSON to this secret.

# 8. Cloudflare R2 secret key
printf '%s' '<R2_SECRET_ACCESS_KEY>' | gcloud secrets create R2_SECRET_ACCESS_KEY --data-file=-
```

Kiểm tra: `gcloud secrets list` phải thấy đủ 8 secret.

---

## 2. Deploy BE — Cloud Run `ragre-api`

Chạy **từ repo root** (Dockerfile BE nằm ở root).

```bash
gcloud run deploy ragre-api \
  --source . \
  --region asia-southeast1 \
  --min-instances=1 \
  --max-instances=2 \
  --cpu-boost \
  --memory=1Gi --cpu=1 \
  --timeout=360 \
  --startup-probe=periodSeconds=10,timeoutSeconds=5,failureThreshold=8,httpGet.path=/ready,httpGet.port=8080 \
  --set-secrets=POSTGRES_PASSWORD=POSTGRES_PASSWORD:latest,ANON_IDENTITY_SECRET=ANON_IDENTITY_SECRET:latest,LEAD_MIRROR_HMAC_SECRET=LEAD_MIRROR_HMAC_SECRET:latest,LLM_API_KEY=LLM_API_KEY:latest,EMBEDDING_API_KEY=EMBEDDING_API_KEY:latest,RERANK_API_KEY=RERANK_API_KEY:latest,FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY=FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY:latest,R2_SECRET_ACCESS_KEY=R2_SECRET_ACCESS_KEY:latest \
  --env-vars-file=env-prod.yaml
```

Ghi chú xác thực flag (đối chiếu tài liệu `gcloud run deploy` chính thức, docs.cloud.google.com/sdk/gcloud/reference/run/deploy, 2026-09-06):
- ✅ `--startup-probe` **CÓ là flag chính thức** của `gcloud run deploy` (mục này từng bị ghi là không có). Cú pháp `KEY=VALUE` với key: `periodSeconds`, `timeoutSeconds`, `failureThreshold`, `httpGet.path`, `httpGet.port`.
- ⚠️ `--cpu-always-allocated` **KHÔNG còn** trên trang reference hiện hành — tên hiện tại của "CPU always allocated" là `--no-cpu-throttling`. **Phiên bản tiết kiệm chi phí trong lệnh trên đã BỎ `--no-cpu-throttling`** (dùng request-based billing — CPU chỉ tính khi xử lý request; idle min-instance rẻ hơn ~4 lần, xem mục 2.2). Nếu bạn vẫn muốn always-CPU (ví dụ cho lead-mirror sweep chạy đúng chu kỳ 24/7) thì thêm lại `--no-cpu-throttling` vào lệnh, hiểu rõ hóa đơn tăng (~$52 vs ~$13/tháng, proxy tier-1).
- ⚠️ `LLM_BINDING` **không tồn tại** trong `api/infrastructure/config/config.py` (grep toàn bộ `api/` không thấy field `llm_binding`) nên đã bị loại khỏi lệnh. TODO: nếu LightRAG runtime cần binding cho LLM leg, rà lại `ingest/lightrag_init.py` rồi bổ sung sau khi verify.
- `POSTGRES_USER` / `POSTGRES_DATABASE`: có trong config.py nhưng mặc định là `ragre`/`ragre` — với Supabase pooler thường là `postgres.<ref>` / `postgres`, bắt buộc truyền rõ.
- Nhóm biến LLM/embedding/rerank trong lệnh trên khớp `.env` local đang chạy tốt (giá model Gemini + Jina, `RERANK_BASE_URL` = `https://api.jina.ai` KHÔNG có đuôi `/v1` — binding `aibox` tự ghép `/v1/rerank`). `LLM_API_KEY`/`EMBEDDING_API_KEY`/`RERANK_API_KEY` lấy từ Secret Manager ở mục 1.
- Biến bổ sung (tùy tính năng dùng) — verify thêm khi cần: `R2_ACCOUNT_ID`, `R2_ENDPOINT`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_PUBLIC_URL`, `IMAGE_CDN_PROJECT_MAP`, `NEXT_PUBLIC_MEDIA_ORIGINS_JSON`, `GEO_*`, `OPENROUTER_API_KEY`. (`IMAGE_CDN_PROJECT_MAP` + `R2_*` cần set nếu muốn ảnh Camellia/Soleil hiển thị đúng qua R2 từ môi trường prod — copy nguyên giá trị JSON từ `.env` local.)

### 2.1. Phương án YAML cho startup probe (nếu không dùng flag)
```bash
gcloud run services describe ragre-api --region asia-southeast1 --format yaml > service.yaml
```
Sửa `spec.template.spec.containers[0]`, thêm:
```yaml
startupProbe:
  httpGet:
    path: /ready
    port: 8080
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 8
```
```bash
gcloud run services replace service.yaml --region asia-southeast1
```

### 2.2. Tại sao probe + min-instances (+ throttling mặc định)
- **Cloud Run giữ traffic cho đến khi startup probe pass.** Ngân sách probe ở trên = 8 lần × 10 giây = 80 giây, đủ cho LightRAG init lạnh ~30–40 giây (`get_lightrag` + `initialize_storages`, chạy trong background task lúc khởi động khi `RAG_PREWARM_ENABLED=true`).
- **`--min-instances=1`**: luôn giữ 1 instance không bị scale về 0 → LightRAG đã warm ở lúc deploy/startup, chat đầu tiên không trả chậm.
- **Bỏ `--no-cpu-throttling` (request-based billing, khuyến nghị):** prewarm LightRAG chạy TRONG startup và `/ready` chỉ trả `ok:true` khi `RAG_PREWARM_FINISHED=true` — tức prewarm xong TRƯỚC khi nhận traffic, và Cloud Run cấp CPU đầy đủ trong giai đoạn startup ở cả hai chế độ. Sau đó instance idle chỉ giữ LightRAG ấm trong bộ nhớ (trạng thái RAM), CPU tạm ngưng không làm mất sự ấm đó → chat đầu tiên vẫn nhanh, hóa đơn idle giảm ~4 lần (idle min-instance $0.0000025/vCPU-s so với always-CPU $0.000018/vCPU-s, proxy tier-1). Đổi lại: lead-mirror reconciliation sweep (chu kỳ 5 phút) chỉ chạy khi có request đi qua — chấp nhận được vì tolerance sweep là 300 phút.
- **`--max-instances=2 --memory=1Gi --cpu=1`**: chặn chi phí khi bị spam; 1Gi chặn OOM của LightRAG/LlamaIndex (mặc định 512MiB chưa verify đủ); 1 vCPU đủ cho 1 worker uvicorn.
- Hai flag `--min-instances=1` + startup probe `/ready` là thứ tiêu diệt vấn đề "chat đầu tiên chậm 30–40 giây"; CPU throttling mặc định là thứ hạ hóa đơn xuống ~$13/tháng thay vì ~$52.

---

## 3. Deploy FE — Cloud Run `ragre-web` + Firebase Hosting

### 3.1. Ràng buộc monorepo (quan trọng — đọc trước khi chạy)
`gcloud run deploy --source <dir>` dùng **chính `<dir>` làm build context** và chỉ tìm Dockerfile ngay trong `<dir>` (tài liệu: "If a Dockerfile is present in the source code directory, it will be built using that Dockerfile"). Context `apps/web` sẽ KHÔNG thấy `package.json`/`package-lock.json` ở root và `packages/*` — mà npm workspaces bắt buộc cần root manifest để `npm ci`. Ngoài ra reference `gcloud run deploy` **KHÔNG có flag `--dockerfile`** để chỉ định Dockerfile ở path khác (verify 2026-09-07), nên:
- ❌ KHÔNG dùng `gcloud run deploy ragre-web --source apps/web` cho build này.
- ✅ Build image bằng **Cloud Build với context = repo root** và `-f apps/web/Dockerfile` (Cách A — file `deploy/cloudbuild-web.yaml` đã có sẵn trong repo), rồi deploy image đã build (Cách B nếu máy có Docker).

### 3.2. Cách A — Cloud Build (khuyến nghị, không cần Docker cục bộ)
File `deploy/cloudbuild-web.yaml` đã tồn tại — KHÔNG cần tự tạo. Nó nhận 9 substitutions `NEXT_PUBLIC_*` (giá trị public lấy từ Firebase console / `.env.local` — chúng là giá trị public nhưng vẫn nên điền cẩn thận) rồi push image vào Artifact Registry repo `ragre` (đã tạo ở bước 0.5). `NEXT_PUBLIC_MAP_TILE_URL` là optional (có fallback CARTO Voyager sẵn trong code) nên không đưa vào Cloud Build config.

```bash
# (chạy từ repo root)
gcloud builds submit . \
  --config deploy/cloudbuild-web.yaml \
  --substitutions _FE_API_KEY=<NEXT_PUBLIC_FIREBASE_API_KEY>,_FE_AUTH_DOMAIN=<NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN>,_FE_PROJECT_ID=<NEXT_PUBLIC_FIREBASE_PROJECT_ID>,_FE_STORAGE_BUCKET=<NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET>,_FE_SENDER_ID=<NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID>,_FE_APP_ID=<NEXT_PUBLIC_FIREBASE_APP_ID>,_FE_VAPID_KEY=<NEXT_PUBLIC_FIREBASE_VAPID_KEY>,_FE_MEDIA_ORIGINS_JSON=<NEXT_PUBLIC_MEDIA_ORIGINS_JSON>,_FE_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app

gcloud run deploy ragre-web \
  --image asia-southeast1-docker.pkg.dev/<PROJECT_ID>/ragre/ragre-web:latest \
  --region asia-southeast1 \
  --min-instances=0 \
  --max-instances=2 \
  --cpu-boost \
  --memory=512Mi --cpu=1 \
  --set-env-vars=NEXT_PUBLIC_API_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app
```
Ghi chú:
- `<PROJECT_ID>` = GCP project id đã set ở bước 0.1 (xem bằng `gcloud config get-value project`).
- ⚠️ `NEXT_PUBLIC_API_PROXY_TARGET` là **build-arg BẮT BUỘC** (đã verify 2026-09-07 trên prod: rewrites `/api/*` được bake vào `routes-manifest.json` lúc build — runtime env KHÔNG recomputed, thiếu build-arg này → mọi call `/api/*` 404 dù `--set-env-vars` có set). Nó cũng được set lại ở runtime qua `--set-env-vars` cho chắc (next.config đọc `??` fallback — chuỗi rỗng không rơi vào fallback). 8 biến còn lại (`NEXT_PUBLIC_FIREBASE_*` ×6, `NEXT_PUBLIC_FIREBASE_VAPID_KEY`, `NEXT_PUBLIC_MEDIA_ORIGINS_JSON`) chỉ cần ở build (đã inline vào bundle client — build thiếu VAPID key làm FCM getToken hỏng, thiếu MEDIA_ORIGINS_JSON làm allowlist ảnh R2 rỗng). Dockerfile giờ có guard fail-fast: build lỗi ngay nếu thiếu API_PROXY_TARGET.
- `--max-instances=2` chặn chi phí khi bị spam; `--cpu-boost` rút ngắn khởi động SSR; FE giữ `min-instances=0` (scale-to-zero, cold start SSR 1–3s chấp nhận được, request-based billing nên idle không mất tiền).
- ⚠️ `--substitutions` của gcloud tách giá trị theo dấu phẩy nên `_FE_MEDIA_ORIGINS_JSON` (JSON chứa dấu phẩy) KHÔNG truyền được qua flag này. Với biến này: dùng Cách B (build local — đã verify chạy thành công), hoặc đưa giá trị vào khối `substitutions:` mặc định trong `deploy/cloudbuild-web.yaml` (kiểm chứng trước khi tin; nhớ KHÔNG commit giá trị vào git).

### 3.3. Cách B — Docker cục bộ (nếu máy có Docker)
```bash
docker buildx build --platform linux/amd64 \
  -f apps/web/Dockerfile \
  --build-arg NEXT_PUBLIC_FIREBASE_API_KEY=<...> \
  --build-arg NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=<...> \
  --build-arg NEXT_PUBLIC_FIREBASE_PROJECT_ID=<...> \
  --build-arg NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=<...> \
  --build-arg NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=<...> \
  --build-arg NEXT_PUBLIC_FIREBASE_APP_ID=<...> \
  --build-arg NEXT_PUBLIC_FIREBASE_VAPID_KEY=<NEXT_PUBLIC_FIREBASE_VAPID_KEY> \
  --build-arg NEXT_PUBLIC_MEDIA_ORIGINS_JSON="<NEXT_PUBLIC_MEDIA_ORIGINS_JSON — BẮT BUỘC, copy NGUYÊN giá trị JSON từ .env.local, giữ nguyên dấu ngoặc kép; rỗng = guard trong Dockerfile fail build>" \
  --build-arg NEXT_PUBLIC_API_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app \
  --build-arg NEXT_PUBLIC_MAP_TILE_URL="<NEXT_PUBLIC_MAP_TILE_URL nếu có, từ .env.local — rỗng OK>" \
  -t asia-southeast1-docker.pkg.dev/<PROJECT_ID>/ragre/ragre-web:latest \
  -t ragre-web:local \
  .
```

```bash
gcloud auth configure-docker asia-southeast1-docker.pkg.dev
docker push asia-southeast1-docker.pkg.dev/<PROJECT_ID>/ragre/ragre-web:latest
```

(--push của buildx phụ thuộc engine driver; tách build rồi push riêng là đường đã verify chạy được trên máy này.)

```bash
gcloud run deploy ragre-web \
  --image asia-southeast1-docker.pkg.dev/<PROJECT_ID>/ragre/ragre-web:latest \
  --region asia-southeast1 \
  --min-instances=0 \
  --max-instances=2 \
  --cpu-boost \
  --memory=512Mi --cpu=1 \
  --set-env-vars=NEXT_PUBLIC_API_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app
```
Ghi chú Cách B:
- `NEXT_PUBLIC_MEDIA_ORIGINS_JSON` chứa JSON có khoảng trắng → bắt buộc bọc trong `"..."` như trên; build đã verify baked đúng vào client bundle.
- `<PROJECT_ID>` = `sale-chat-ai` (GCP), KHÔNG phải Firebase project id.
- Các build-arg lấy từ `apps/web/.env.local` (cùng giá trị đã build thử local thành công).
- `NEXT_PUBLIC_MEDIA_ORIGINS_JSON` giờ là **build-arg bắt buộc** (guard fail-fast trong Dockerfile cạnh guard PROXY_TARGET, verify 2026-09-07). Lưu ý: client bundle inline giá trị lúc BUILD — runtime ENV chỉ phục vụ `next start` (images.remotePatterns). Cả hai nguồn đã verify bake đúng trên revision `ragre-web-00002-6ql`.
- ⚠️ Ảnh greeting/chat báo hỏng trên prod (2026-09-07) KHÔNG phải do FE: FE + image cho phép mọi URL R2 allowlist (curl `_next/image` → 200). Nguyên nhân: service **ragre-api** thiếu env `IMAGE_CDN_PROJECT_MAP` (+ `R2_ACCOUNT_ID`/`R2_PUBLIC_URL`) — BE fail-closed ở prod (`_project_media_policy` trả origins rỗng) nên `/llms-hello` trả `images: []` và `videos[].url_cdn: null`, BE log: "media entries resolved to zero valid urls: project_key=camellia entry_count=3 invalid_url_count=3". Fix: `gcloud run services update ragre-api --region asia-southeast1 --set-env-vars=IMAGE_CDN_PROJECT_MAP='<JSON từ .env>'` (thuộc BE session — xem mục 281).
- `NEXT_PUBLIC_MEDIA_ORIGINS_JSON` + `NEXT_PUBLIC_MAP_TILE_URL` đã được bake vào cả runtime ENV của image (Dockerfile runtime stage) → KHÔNG cần `--set-env-vars` cho chúng lúc deploy. `NEXT_PUBLIC_API_PROXY_TARGET` giờ là **build-arg bắt buộc** (guard fail-fast trong Dockerfile), đồng thời vẫn set ở runtime qua `--set-env-vars`.

### 3.4. Mở quyền cho Firebase Hosting gọi được Cloud Run
```bash
gcloud run services add-iam-policy-binding ragre-web \
  --region asia-southeast1 \
  --member=allUsers --role=roles/run.invoker
```
(Nếu muốn riêng tư hơn: thay `allUsers` bằng service agent hosting `service-<PROJECT_NUMBER>@gcp-sa-firebasehosting.iam.gserviceaccount.com`.)

### 3.5. Deploy Firebase Hosting (rewrite → ragre-web)
```bash
cd apps/web
firebase deploy --only hosting
cd ..
```
Sau bước này web chạy tại `https://sale-chat-bot-11e49.web.app` (và `https://sale-chat-bot-11e49.firebaseapp.com`). Mọi request (kể cả `/api/*` do Next proxy sang BE) đi qua Hosting → Cloud Run `ragre-web`.

---

## 4. Kiểm tra sau deploy

```bash
# 4.1. /ready của BE — kỳ vọng ok:true + rag_prewarm_finished:true
curl https://ragre-api-<HASH>-asia-southeast1.a.run.app/ready

# 4.2. FE trả trang
curl -I https://sale-chat-bot-11e49.web.app
```
- **4.3. Hai chat warm (Camellia + Soleil):** mở web, chat 1 câu trong dự án Camellia và 1 câu trong Soleil; kiểm tra SSE chảy mượt (sources → facts → token → done), citation + confidence hiển thị đúng.
- **4.4. Sales login + FCM:** đăng nhập sales, xác nhận nhận thông báo đẩy (firebase-messaging-sw.js đăng ký thành công, không lỗi permission trong console trình duyệt).
- **4.5. CORS:** gọi một endpoint từ origin FE (qua proxy `/api/*`) — không được có lỗi CORS; `CORS_ORIGINS` ở BE đã trỏ đúng `https://sale-chat-bot-11e49.web.app`.

## 5. Rollback

```bash
gcloud run revisions list --service ragre-api --region asia-southeast1
gcloud run services update-traffic ragre-api --region asia-southeast1 --to-revisions <PREV_REVISION>=100
# Tương tự cho FE nếu cần:
gcloud run services update-traffic ragre-web --region asia-southeast1 --to-revisions <PREV_REVISION>=100
```

## 6. Chi phí + lý do vùng miền

- **Cấu hình tiết kiệm hiện tại của runbook** (request-based billing, BE `min=1`): idle min-instance chỉ tính ~$0.0000025/vCPU-s + $0.0000025/GiB-s → BE ~$13/tháng (proxy tier-1; tier-2 Singapore cao hơn, xem pricing calculator), FE `min=0` ≈ $0 trong free tier. Nếu thêm `--no-cpu-throttling` (always-CPU) thì BE tăng lên ~$52/tháng — chỉ dùng khi cần background sweep 24/7.
- **Muốn $0 trên Cloud Run:** đặt `--min-instances=0` và cron ping `/ready` mỗi ~9 phút (UptimeRobot/GitHub Actions scheduled) — instance không phải min-instance thì idle KHÔNG bị tính tiền; đánh đổi là thi thoảng vẫn dính cold start ~30–40s vì Cloud Run có quyền thu hồi instance bất cứ lúc nào.
- **Vùng `asia-southeast1` (Singapore):** trùng vùng với Supabase pooler `aws-1-ap-southeast-1` → độ trễ BE↔Postgres thấp nhất, quan trọng vì LightRAG query fan-out nhiều round-trip vào PG. Giữ BE và pooler cùng vùng. Nếu sau này chuyển sang Neon: chọn project Neon `aws-ap-southeast-1` để giữ cùng lý do.

## 7. Việc còn mở (TODO verify)

- `LLM_BINDING`: không có trong `api/infrastructure/config/config.py` — đã loại khỏi lệnh deploy BE (mục 2). Rà lại nếu LightRAG cần.
- `--cpu-always-allocated`: tên flag cũ, không còn trên reference hiện hành — dùng `--no-cpu-throttling` thay thế (runbook mặc định ĐÃ BỎ flag này để tiết kiệm; thêm lại nếu muốn always-CPU).
- R2 prod: `IMAGE_CDN_PROJECT_MAP`, `R2_ACCOUNT_ID`, `R2_ENDPOINT`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_PUBLIC_URL` chưa có trong lệnh deploy BE — ảnh trả về sẽ thiếu nếu không set. Copy giá trị JSON từ `.env` local vào `--set-env-vars` (không phải secret trừ `R2_ACCESS_KEY_ID` — có thể thêm vào Secret Manager nếu muốn).
- Giá tier-2 chính xác cho `asia-southeast1` chưa verify (trang pricing chỉ render bảng Iowa) — xem Google Cloud pricing calculator trước khi chốt ngân sách.
