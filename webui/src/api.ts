const KEY_STORAGE = "mf_proxy_key";

export function getKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

export function setKey(value: string): void {
  try {
    localStorage.setItem(KEY_STORAGE, value.trim());
  } catch {
    /* private windows may block storage; the key then lives per-session */
  }
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  return { "Content-Type": "application/json", "X-Mirofish-Proxy-Key": getKey(), ...extra };
}

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, headers: headers((options.headers as Record<string, string>) || {}) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data?.error?.message || `HTTP ${response.status}`;
    throw new ApiError(message, response.status);
  }
  return data as T;
}

export interface StreamHandlers {
  onDelta(text: string): void;
  onUsage?(usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number }): void;
}

/** POST /v1/chat/completions with stream:true and surface text deltas live. */
export async function streamChat(body: Record<string, unknown>, account: string,
                                 handlers: StreamHandlers, signal?: AbortSignal): Promise<void> {
  const extra: Record<string, string> = {};
  if (account) extra["X-Mirofish-Account"] = account;
  const response = await fetch("/v1/chat/completions", {
    method: "POST",
    headers: headers(extra),
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  });
  if (!response.ok || !response.body) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(data?.error?.message || `HTTP ${response.status}`, response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index: number;
    while ((index = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        const raw = line.slice(6).trim();
        if (!raw || raw === "[DONE]") continue;
        let chunk: any;
        try {
          chunk = JSON.parse(raw);
        } catch {
          continue;
        }
        if (chunk?.error) throw new ApiError(chunk.error.message || "stream error", chunk.error.code || 502);
        const delta = chunk?.choices?.[0]?.delta;
        if (delta?.content) handlers.onDelta(delta.content);
        if (chunk?.usage && handlers.onUsage) handlers.onUsage(chunk.usage);
      }
    }
  }
}
