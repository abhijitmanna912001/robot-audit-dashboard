// The post-sign-in redirect target. Only a same-origin path is accepted: it must start with one "/"
// and not "//" or "/\", and may not contain a scheme, a backslash or control characters. Anything
// else becomes "/". Prevents an open redirect through ?next=.
export function safeNextPath(raw: string | null | undefined): string {
  if (typeof raw !== "string" || raw === "") return "/";
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/";
  if (/[\\\u0000-\u001f\u007f]/.test(raw)) return "/";
  try {
    const u = new URL(raw, "http://x.invalid");
    if (u.origin !== "http://x.invalid") return "/";
  } catch {
    return "/";
  }
  return raw;
}
