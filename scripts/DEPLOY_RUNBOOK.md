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

# 7. Firebase service account private key — PHẢI tạo từ FILE để giữ nguyên ký tự \n trong khóa.
#    Lấy file JSON service account từ Firebase Console (Project settings → Service accounts) rồi:
gcloud secrets create FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY --data-file=path/to/sa.json
#    (hoặc qua stdin: gcloud secrets create FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY --data-file=- < path/to/sa.json)
#    KHÔNG dùng printf/echo cho giá trị này — sẽ làm hỏng dấu xuống dòng của private key.

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
  --cpu-boost \
  --no-cpu-throttling \
  --timeout=360 \
  --startup-probe=periodSeconds=10,timeoutSeconds=5,failureThreshold=8,httpGet.path=/ready,httpGet.port=8080 \
  --set-secrets=POSTGRES_PASSWORD=POSTGRES_PASSWORD:latest,ANON_IDENTITY_SECRET=ANON_IDENTITY_SECRET:latest,LEAD_MIRROR_HMAC_SECRET=LEAD_MIRROR_HMAC_SECRET:latest,LLM_API_KEY=LLM_API_KEY:latest,EMBEDDING_API_KEY=EMBEDDING_API_KEY:latest,RERANK_API_KEY=RERANK_API_KEY:latest,FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY=FIREBASE_SERVICE_ACCOUNT_PRIVATE_KEY:latest,R2_SECRET_ACCESS_KEY=R2_SECRET_ACCESS_KEY:latest \
  --set-env-vars=APP_ENV=prod,POSTGRES_HOST=aws-1-ap-southeast-1.pooler.supabase.com,POSTGRES_PORT=5432,POSTGRES_USER=<POSTGRES_USER>,POSTGRES_DATABASE=<POSTGRES_DATABASE>,POSTGRES_MAX_CONNECTIONS=10,EMBEDDING_DIM=1024,RAG_PREWARM_ENABLED=true,CORS_ORIGINS=https://sale-chat-bot-11e49.web.app,EMBEDDING_BINDING=gemini,RERANK_BINDING=aibox
```

Ghi chú xác thực flag (đối chiếu tài liệu `gcloud run deploy` chính thức, docs.cloud.google.com/sdk/gcloud/reference/run/deploy, 2026-09-06):
- ✅ `--startup-probe` **CÓ là flag chính thức** của `gcloud run deploy` (mục này từng bị ghi là không có). Cú pháp `KEY=VALUE` với key: `periodSeconds`, `timeoutSeconds`, `failureThreshold`, `httpGet.path`, `httpGet.port`.
- ⚠️ `--cpu-always-allocated` **KHÔNG còn** trên trang reference hiện hành — tên hiện tại của "CPU always allocated" là `--no-cpu-throttling`. Lệnh trên đã dùng `--no-cpu-throttling`; KHÔNG thêm `--cpu-always-allocated` kèm theo.
- ⚠️ `LLM_BINDING` **không tồn tại** trong `api/infrastructure/config/config.py` (grep toàn bộ `api/` không thấy field `llm_binding`) nên đã bị loại khỏi lệnh. TODO: nếu LightRAG runtime cần binding cho LLM leg, rà lại `ingest/lightrag_init.py` rồi bổ sung sau khi verify.
- `POSTGRES_USER` / `POSTGRES_DATABASE`: có trong config.py nhưng mặc định là `ragre`/`ragre` — với Supabase pooler thường là `postgres.<ref>` / `postgres`, bắt buộc truyền rõ.
- Biến bổ sung (tùy tính năng dùng) — verify thêm khi cần: `LLM_BASE_URL`, `R2_ACCOUNT_ID`, `R2_ENDPOINT`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_PUBLIC_URL`, `GEO_*`, `OPENROUTER_API_KEY`.

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

### 2.2. Tại sao probe + min-instances + always-CPU
- **Cloud Run giữ traffic cho đến khi startup probe pass.** Ngân sách probe ở trên = 8 lần × 10 giây = 80 giây, đủ cho LightRAG init lạnh ~30–40 giây (`get_lightrag` + `initialize_storages`, chạy trong background task lúc khởi động khi `RAG_PREWARM_ENABLED=true`).
- **`--min-instances=1`**: luôn giữ 1 instance không bị scale về 0 → LightRAG đã warm ở lúc deploy/startup, chat đầu tiên không trả chậm.
- **`--no-cpu-throttling` (CPU always allocated)**: nếu KHÔNG bật, Cloud Run thu hồi CPU giữa các request → thread prewarm/init bị đóng băng, khi request tới phải "tan băng" = chat đầu chậm lại đúng như vấn đề cần khắc phục. Bật luôn-CPU thì instance idle vẫn giữ ấm LightRAG + pool Postgres.
- Hai flag này chính là thứ tiêu diệt vấn đề "chat đầu tiên chậm 30–40 giây".

---

## 3. Deploy FE — Cloud Run `ragre-web` + Firebase Hosting

### 3.1. Ràng buộc monorepo (quan trọng — đọc trước khi chạy)
`gcloud run deploy --source <dir>` dùng **chính `<dir>` làm build context** và chỉ tìm Dockerfile ngay trong `<dir>` (tài liệu: "If a Dockerfile is present in the source code directory, it will be built using that Dockerfile"). Context `apps/web` sẽ KHÔNG thấy `package.json`/`package-lock.json` ở root và `packages/*` — mà npm workspaces bắt buộc cần root manifest để `npm ci`. Vì vậy:
- ❌ KHÔNG dùng `gcloud run deploy ragre-web --source apps/web` cho build này.
- ✅ Build với **context = repo root** và `-f apps/web/Dockerfile`, theo một trong hai cách dưới đây.
- Ghi chú: `--set-build-env-vars` (đã verify là flag thật của `gcloud run deploy`) chỉ có tác dụng với build source/buildpack — ở đây build bằng Dockerfile nên giá trị build-time truyền qua `--build-arg` / substitutions.

### 3.2. Cách A — Cloud Build (khuyến nghị, không cần Docker cục bộ)
Tạo file `deploy/cloudbuild-web.yaml` với nội dung dưới đây (điền `PROJECT_ID`):
```yaml
steps:
  - name: gcr.io/cloud-builders/docker
    args:
      - build
      - -f
      - apps/web/Dockerfile
      - --build-arg
      - NEXT_PUBLIC_FIREBASE_API_KEY=$_FE_API_KEY
      - --build-arg
      - NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$_FE_AUTH_DOMAIN
      - --build-arg
      - NEXT_PUBLIC_FIREBASE_PROJECT_ID=$_FE_PROJECT_ID
      - --build-arg
      - NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET=$_FE_STORAGE_BUCKET
      - --build-arg
      - NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID=$_FE_SENDER_ID
      - --build-arg
      - NEXT_PUBLIC_FIREBASE_APP_ID=$_FE_APP_ID
      - --build-arg
      - NEXT_PUBLIC_API_PROXY_TARGET=$_FE_PROXY_TARGET
      - -t
      - asia-southeast1-docker.pkg.dev/PROJECT_ID/ragre/ragre-web:latest
      - .
images:
  - asia-southeast1-docker.pkg.dev/PROJECT_ID/ragre/ragre-web:latest
options:
  logging: CLOUD_LOGGING_ONLY
```
Build + deploy (chạy từ repo root; 7 giá trị `NEXT_PUBLIC_*` lấy từ Firebase console — chúng là giá trị public nhưng vẫn nên điền cẩn thận):
```bash
gcloud builds submit . \
  --config deploy/cloudbuild-web.yaml \
  --substitutions _FE_API_KEY=<NEXT_PUBLIC_FIREBASE_API_KEY>,_FE_AUTH_DOMAIN=<NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN>,_FE_PROJECT_ID=<NEXT_PUBLIC_FIREBASE_PROJECT_ID>,_FE_STORAGE_BUCKET=<NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET>,_FE_SENDER_ID=<NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID>,_FE_APP_ID=<NEXT_PUBLIC_FIREBASE_APP_ID>,_FE_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app

gcloud run deploy ragre-web \
  --image asia-southeast1-docker.pkg.dev/<FIREBASE_PROJECT_ID>/ragre/ragre-web:latest \
  --region asia-southeast1 \
  --min-instances=0 \
  --set-env-vars=NEXT_PUBLIC_API_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app
```
`NEXT_PUBLIC_API_PROXY_TARGET` phải đặt ở **runtime** nữa vì `next.config.ts` đọc nó lúc server start để tính rewrites `/api/*` → BE. 6 biến `NEXT_PUBLIC_FIREBASE_*` chỉ cần ở build (đã inline vào bundle client).

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
  --build-arg NEXT_PUBLIC_API_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app \
  -t asia-southeast1-docker.pkg.dev/<FIREBASE_PROJECT_ID>/ragre/ragre-web:latest \
  --push .

gcloud run deploy ragre-web \
  --image asia-southeast1-docker.pkg.dev/<FIREBASE_PROJECT_ID>/ragre/ragre-web:latest \
  --region asia-southeast1 \
  --min-instances=0 \
  --set-env-vars=NEXT_PUBLIC_API_PROXY_TARGET=https://ragre-api-<HASH>-asia-southeast1.a.run.app
```

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

- **Chi phí "luôn ấm":** `--min-instances=1` + `--no-cpu-throttling` = trả tiền ~24/7 cho một instance nhỏ dù không có traffic. Muốn giảm: đặt `--min-instances=0` (tiết kiệm, đổi lại chấp nhận cold start ~30–40 giây chat đầu tiên sau khi instance ngủ).
- **Vùng `asia-southeast1` (Singapore):** trùng vùng với Supabase pooler `aws-1-ap-southeast-1` → độ trễ BE↔Postgres thấp nhất, quan trọng vì LightRAG query fan-out nhiều round-trip vào PG. Giữ BE và pooler cùng vùng.

## 7. Việc còn mở (TODO verify)

- `LLM_BINDING`: không có trong `api/infrastructure/config/config.py` — đã loại khỏi lệnh deploy BE (mục 2). Rà lại nếu LightRAG cần.
- `--cpu-always-allocated`: tên flag cũ, không còn trên reference hiện hành — dùng `--no-cpu-throttling` thay thế.
- File `deploy/cloudbuild-web.yaml` (mục 3.2) do chủ dự án tự tạo theo nội dung trong tài liệu này; nhớ điền `PROJECT_ID`.
- Chưa verify tên biến cho LLM gateway prod (`LLM_BASE_URL`, model roles) — bổ sung vào `--set-env-vars` của BE khi có key/provider chốt.
