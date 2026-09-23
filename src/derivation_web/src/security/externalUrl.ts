const EXTERNAL_HOSTS = ["openai.com", "chatgpt.com"] as const;

export function allowedOpenAiHttpsUrl(value: string): URL | null {
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLowerCase();
    const allowed = EXTERNAL_HOSTS.some((candidate) => hostname === candidate || hostname.endsWith(`.${candidate}`));
    if (url.protocol !== "https:" || !allowed || url.username || url.password || url.port || url.hash) return null;
    return url;
  } catch {
    return null;
  }
}
