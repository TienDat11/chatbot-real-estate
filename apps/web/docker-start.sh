#!/bin/sh
# next.config.ts derives images.remotePatterns from NEXT_PUBLIC_MEDIA_ORIGINS_JSON
# when the server starts, but the value ships base64-encoded (see Dockerfile),
# so decode it back to plain JSON before handing off to `next start`.
set -e
if [ -n "${NEXT_PUBLIC_MEDIA_ORIGINS_JSON_B64:-}" ]; then
  NEXT_PUBLIC_MEDIA_ORIGINS_JSON="$(node -e 'process.stdout.write(Buffer.from(process.env.NEXT_PUBLIC_MEDIA_ORIGINS_JSON_B64, "base64").toString("utf8"))')"
  export NEXT_PUBLIC_MEDIA_ORIGINS_JSON
fi
exec npm run start -- -p "${PORT:-8080}"
