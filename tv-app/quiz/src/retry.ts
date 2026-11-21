export const BASE_DELAY_MS = 2000;
export const MAX_DELAY_MS = 30_000;
export const JITTER = 0.2;

export type RetryTask = () => Promise<void>;

/** Extract a server-provided retry-after (seconds) from a thrown error, if any. */
function retryAfterMs(err: unknown): number | null {
  const v = (err as { retryAfter?: unknown } | null)?.retryAfter;
  return typeof v === "number" && v >= 0 ? v * 1000 : null;
}

/**
 * Single retry loop: runs the task until it resolves. Failures wait with exponential backoff
 * (2s -> 30s, +/-20% jitter), never less than the error's retryAfter. Starting a new loop
 * cancels the previous one. While paused (page hidden) no attempt is started.
 */
export class RetryController {
  private gen = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private task: RetryTask | null = null;
  private failures = 0;
  private paused = false;
  private inFlight = false;
  private attemptDue = false;
  private dueAt = 0;
  /** Visibility of the bound document (bindVisibility); false when unbound. */
  private docHidden = false;

  constructor(private random: () => number = Math.random) {}

  get active(): boolean {
    return this.task !== null;
  }

  start(task: RetryTask): void {
    const keepPaused =
      this.docHidden || (this.paused && typeof document !== "undefined" && document.hidden);
    this.cancel();
    this.task = task;
    this.failures = 0;
    if (keepPaused) {
      // Hidden page: stay paused and run the first attempt on resume.
      this.paused = true;
      this.attemptDue = true;
      this.dueAt = 0;
      return;
    }
    void this.attempt(this.gen);
  }

  pause(): void {
    if (!this.task || this.paused) return;
    this.paused = true;
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
      this.attemptDue = true;
    }
  }

  resume(): void {
    if (!this.task || !this.paused) return;
    this.paused = false;
    if (this.attemptDue) {
      this.attemptDue = false;
      const wait = this.dueAt - Date.now();
      if (wait > 0) this.schedule(this.gen, wait);
      else void this.attempt(this.gen);
    }
  }

  cancel(): void {
    this.gen++;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.task = null;
    this.paused = false;
    this.inFlight = false;
    this.attemptDue = false;
  }

  /** Pause on hidden, resume on visible. Returns an unsubscribe function. */
  bindVisibility(doc: Document): () => void {
    const h = () => {
      this.docHidden = doc.hidden;
      if (doc.hidden) this.pause();
      else this.resume();
    };
    // Apply the current state: an app launched hidden gets no visibilitychange event.
    h();
    doc.addEventListener("visibilitychange", h);
    return () => {
      doc.removeEventListener("visibilitychange", h);
      this.docHidden = false;
    };
  }

  private backoff(): number {
    const base = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** this.failures);
    return base * (1 + JITTER * (2 * this.random() - 1));
  }

  private schedule(gen: number, delay: number): void {
    this.dueAt = Date.now() + delay;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.attempt(gen);
    }, delay);
  }

  private async attempt(gen: number): Promise<void> {
    const task = this.task;
    if (!task || gen !== this.gen || this.inFlight) return;
    this.inFlight = true;
    try {
      await task();
    } catch (err) {
      if (gen !== this.gen) return;
      this.inFlight = false;
      const delay = Math.max(this.backoff(), retryAfterMs(err) ?? 0);
      this.failures++;
      if (this.paused) {
        this.attemptDue = true;
        this.dueAt = Date.now() + delay;
        return;
      }
      this.schedule(gen, delay);
      return;
    }
    if (gen === this.gen) this.cancel();
  }
}
