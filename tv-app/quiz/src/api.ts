import { API_VERSION, REQUEST_TIMEOUT_MS } from "./config";

export type Tier = "regular" | "challenge";
export interface Option {
  id: string;
  text: string;
}
export interface Part {
  prompt: string;
  options: Option[];
  status: "pending" | "current" | "right" | "wrong";
  given?: unknown;
  correct?: unknown;
  explanation?: string;
}
export interface VocabItem {
  word: string;
  meaning: string;
  sentence: string;
  check_prompt: string;
  options: Option[];
  done: boolean;
  need_review: boolean;
}
/** Server attempt view (all attempt endpoints). */
export interface AttemptView {
  id: string;
  tier: Tier;
  state: "issued" | "correct" | "exhausted" | "skipped" | "expired";
  rev: number;
  prompt: string;
  figure: string | null;
  answer_type: "integer" | "decimal" | "choice" | "passage";
  options: Option[];
  hints: string[];
  hints_total: number;
  tries_left: number | null;
  skips_left: number;
  outcome: string;
  credited_minutes: number;
  nominal_minutes: number;
  explanation: string | null;
  correct_answer: unknown;
  passage: string | null;
  stage: null | "parts" | "vocab" | "done";
  step: number | null;
  parts: Part[];
  vocab: VocabItem[];
  parts_right: number;
  parts_total: number;
}
export interface StatusView {
  remaining_s: number;
  quiz_today: number;
  earned_minutes: number;
  challenge_state: "none" | "issued" | "correct" | "exhausted" | "expired";
  available: boolean;
  reason: null | "locked" | "curfew" | "paused" | "disabled";
  /** Seconds left in a wrong-answer pause, else null. */
  retry_after_s: number | null;
}

export class ApiError extends Error {}
export class VersionError extends ApiError {
  constructor() {
    super("api version mismatch (426)");
  }
}
export class AuthError extends ApiError {
  constructor(public status: number) {
    super(`auth rejected (${status})`);
  }
}
export class UnavailableError extends ApiError {
  constructor(public reason: string, public retryAfter: number | null) {
    super(`unavailable: ${reason}`);
  }
}
export class RejectedError extends ApiError {
  constructor(public code: string, public retryAfter: number | null, public status = 0) {
    super(`rejected: ${code}`);
  }
}
export class RateLimitError extends ApiError {
  constructor(public retryAfter: number | null) {
    super("rate limited");
  }
}
/** Transport failure, timeout, or an unexpected HTTP status / body. */
export class NetworkError extends ApiError {
  constructor(message: string, public timedOut = false) {
    super(message);
  }
}
export class BusyError extends ApiError {
  constructor() {
    super("another write is in flight");
  }
}

export interface ApiOptions {
  baseUrl: string;
  token: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

interface ErrorBody {
  error?: string;
  code?: string;
  reason?: string;
  retry_after_s?: number;
}

export class ApiClient {
  private writing = false;
  private baseUrl: string;
  private timeoutMs: number;

  constructor(private opts: ApiOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? REQUEST_TIMEOUT_MS;
  }

  status(): Promise<StatusView> {
    return this.request("GET", "/api/status");
  }
  async openAttempts(): Promise<AttemptView[]> {
    const r = await this.request<{ attempts: AttemptView[] }>("GET", "/api/attempts/open");
    return r.attempts;
  }
  issue(tier: Tier): Promise<AttemptView> {
    return this.write("/api/attempt?tier=" + encodeURIComponent(tier));
  }
  get(id: string): Promise<AttemptView> {
    return this.request("GET", this.attemptPath(id));
  }
  submit(id: string, rev: number, answer: string, step?: number): Promise<AttemptView> {
    const body: Record<string, unknown> = { rev, answer };
    if (step !== undefined) body.step = step;
    return this.write(this.attemptPath(id) + "/submit", body);
  }
  hint(id: string, rev: number): Promise<AttemptView> {
    return this.write(this.attemptPath(id) + "/hint", { rev });
  }
  skip(id: string, rev: number): Promise<AttemptView> {
    return this.write(this.attemptPath(id) + "/skip", { rev });
  }
  review(id: string, rev: number): Promise<AttemptView> {
    return this.write(this.attemptPath(id) + "/review", { rev });
  }

  private attemptPath(id: string): string {
    return "/api/attempt/" + encodeURIComponent(id);
  }

  private async write<T>(path: string, body?: unknown): Promise<T> {
    if (this.writing) throw new BusyError();
    this.writing = true;
    try {
      return await this.request<T>("POST", path, body ?? {});
    } finally {
      this.writing = false;
    }
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const ctl = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      ctl.abort();
    }, this.timeoutMs);
    const fetchFn = this.opts.fetchImpl ?? fetch;
    try {
      const headers: Record<string, string> = {
        Authorization: "Bearer " + this.opts.token,
        "X-LTP-API": API_VERSION,
      };
      if (body !== undefined) headers["Content-Type"] = "application/json";
      let res: Response;
      try {
        res = await fetchFn(this.baseUrl + path, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: ctl.signal,
        });
      } catch {
        // Original error text is dropped on purpose so messages never carry URLs or headers.
        throw timedOut ? new NetworkError("request timed out", true) : new NetworkError("network error");
      }
      let data: unknown = null;
      try {
        data = await res.json();
      } catch {
        // An abort while reading the body is a timeout, not a bad body.
        if (timedOut) throw new NetworkError("request timed out", true);
        if (res.ok) throw new NetworkError("invalid response body");
      }
      if (res.ok) return data as T;
      throw this.toError(res.status, (data ?? {}) as ErrorBody);
    } finally {
      clearTimeout(timer);
    }
  }

  private toError(status: number, b: ErrorBody): ApiError {
    const retry = typeof b.retry_after_s === "number" ? b.retry_after_s : null;
    if (status === 426) return new VersionError();
    if (status === 401 || status === 403) return new AuthError(status);
    if (status === 503) return new UnavailableError(b.reason ?? b.error ?? "unknown", retry);
    if (status === 429) return new RateLimitError(retry);
    if (status === 409 || status === 400 || status === 404) {
      return new RejectedError(b.code ?? b.error ?? "rejected", retry, status);
    }
    return new NetworkError(`unexpected http status ${status}`);
  }
}
