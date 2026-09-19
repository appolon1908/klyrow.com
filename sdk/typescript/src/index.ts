import type { components } from "./schema";

export type TemplateVersion = components["schemas"]["TemplateVersionView"];
export type TemplateVersionPage = components["schemas"]["TemplateVersionPage"];

export type UsageHistoryPage = components["schemas"]["UsageHistoryPage"];
export type UsageHistoryOptions = { from?: string; to?: string; unit?: string; limit?: number; cursor?: string };

export type Transport = (url: string, init: RequestInit) => Promise<Response>;

export class KlyrowError extends Error {
  constructor(public status: number, public errorCode: string, message: string, public requestId?: string, public details?: unknown) {
    super(message);
  }
}

export class Klyrow {
  constructor(private token: string, private tenantId?: string, private baseUrl = "https://api.klyrow.com", private transport: Transport = fetch) {
    if (!token) throw new Error("token is required");
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  async request<T>(method: string, path: string, body?: unknown, idempotencyKey?: string): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.token}`, Accept: "application/json" };
    if (this.tenantId) headers["X-Klyrow-Tenant-Id"] = this.tenantId;
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await this.transport(this.baseUrl + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const payload = await response.json().catch(() => ({})) as any;
    if (!response.ok) {
      const detail = payload.detail ?? payload;
      const code = typeof detail === "string" ? detail : detail.error_code ?? "request_failed";
      throw new KlyrowError(response.status, code, typeof detail === "string" ? detail : detail.message ?? code, response.headers.get("X-Request-Id") ?? undefined, detail.details);
    }
    return payload as T;
  }

  send(message: Record<string, unknown>, idempotencyKey: string) { return this.request("POST", "/v1/messages", message, idempotencyKey); }
  message(messageId: string) { return this.request("GET", `/v1/messages/${encodeURIComponent(messageId)}`); }
  messages(limit = 50, cursor?: string) { return this.request("GET", `/v1/messages?limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`); }
  createWebhook(url: string, events: string[]) { return this.request("POST", "/v1/webhook-subscriptions", { url, events }); }
  templateVersions(templateId: string, options: { limit?: number; cursor?: string } = {}) {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set("limit", String(options.limit));
    if (options.cursor !== undefined) query.set("cursor", options.cursor);
    const suffix = query.size ? `?${query.toString()}` : "";
    return this.request<TemplateVersionPage>("GET", `/v1/templates/${encodeURIComponent(templateId)}/versions${suffix}`);
  }
  usageDaily(options: UsageHistoryOptions = {}) { return this.usageHistory("daily", options); }
  usageMonthly(options: UsageHistoryOptions = {}) { return this.usageHistory("monthly", options); }
  private usageHistory(granularity: "daily" | "monthly", options: UsageHistoryOptions) {
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(options)) {
      if (value !== undefined) query.set(name, String(value));
    }
    const suffix = query.size ? `?${query.toString()}` : "";
    return this.request<UsageHistoryPage>("GET", `/v1/usage/${granularity}${suffix}`);
  }
  templateVersion(templateId: string, versionId: string) {
    return this.request<TemplateVersion>("GET", `/v1/templates/${encodeURIComponent(templateId)}/versions/${encodeURIComponent(versionId)}`);
  }
}

export async function verifyWebhook(secret: string, timestamp: string, eventId: string, body: Uint8Array, signature: string, now = Math.floor(Date.now() / 1000), toleranceSeconds = 300): Promise<boolean> {
  const issued = Number(timestamp);
  if (!Number.isInteger(issued) || Math.abs(now - issued) > toleranceSeconds) return false;
  const prefix = new TextEncoder().encode(`${timestamp}.${eventId}.`);
  const signed = new Uint8Array(prefix.length + body.length); signed.set(prefix); signed.set(body, prefix.length);
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const digest = new Uint8Array(await crypto.subtle.sign("HMAC", key, signed));
  const expected = Array.from(digest).map(byte => byte.toString(16).padStart(2, "0")).join("");
  const supplied = signature.replace(/^sha256=/, "");
  if (expected.length !== supplied.length) return false;
  let difference = 0; for (let i = 0; i < expected.length; i++) difference |= expected.charCodeAt(i) ^ supplied.charCodeAt(i);
  return difference === 0;
}
