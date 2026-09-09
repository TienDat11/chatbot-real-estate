export function sameOriginNotificationPath(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) return "/";
  try {
    const url = new URL(value, "https://local.invalid");
    return url.origin === "https://local.invalid" && url.pathname.startsWith("/") ? `${url.pathname}${url.search}${url.hash}` : "/";
  } catch {
    return "/";
  }
}
