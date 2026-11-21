import {
  ApiError,
  AuthError,
  BusyError,
  NetworkError,
  RejectedError,
  UnavailableError,
  VersionError,
  type ApiClient,
  type AttemptView,
  type StatusView,
  type Tier,
} from "./api";
import { el, type Page } from "./dom";
import { FocusGrid } from "./focus";
import { KeyFilter } from "./keys";
import { renderHome } from "./pages/home";
import { answeredParts, renderPartFeedback, renderPassage } from "./pages/passage";
import { renderQuestion, type Entry } from "./pages/question";
import { renderResult } from "./pages/result";
import { formatCountdown, renderUnavailable } from "./pages/unavailable";
import { renderVocab } from "./pages/vocab";
import { RetryController } from "./retry";

type Screen =
  | { kind: "loading" }
  | { kind: "home" }
  | { kind: "attempt"; view: AttemptView }
  | { kind: "unavailable"; reason: string; until: number | null };

export type AppApi = Pick<ApiClient, "status" | "openAttempts" | "issue" | "get" | "submit" | "hint" | "skip" | "review">;

const REJECT_NOTICES: Record<string, string> = {
  skip_limit: "No skips left.",
  no_skip_challenge: "The daily challenge can't be skipped.",
};

export class App {
  private screen: Screen = { kind: "loading" };
  private status: StatusView | null = null;
  private open: AttemptView[] = [];
  private notice: string | null = null;
  private entry: Entry = { key: "", text: "" };
  /** Passage parts whose feedback the child has already dismissed (per attempt id). */
  private partsSeen: { id: string; count: number } | null = null;
  private page: Page | null = null;
  private busy = false;
  private grid: FocusGrid;
  private keys: KeyFilter;
  private retry: RetryController;
  private ticker: ReturnType<typeof setInterval> | null = null;

  constructor(
    private root: HTMLElement,
    private api: AppApi,
    opts: { keys?: KeyFilter; retry?: RetryController; now?: () => number } = {},
  ) {
    this.grid = new FocusGrid(root);
    this.keys = opts.keys ?? new KeyFilter();
    this.retry = opts.retry ?? new RetryController();
    this.now = opts.now ?? (() => Date.now());
    root.addEventListener("focusin", (e) => {
      root.querySelectorAll(".focused").forEach((n) => n.classList.remove("focused"));
      if (e.target instanceof HTMLElement) e.target.classList.add("focused");
    });
  }

  private now: () => number;

  /** Start the app: bind visibility handling and load home (never issues an attempt). */
  start(doc: Document = document): () => void {
    const unbind = this.retry.bindVisibility(doc);
    const onVisible = () => {
      if (!doc.hidden) this.refresh();
    };
    doc.addEventListener("visibilitychange", onVisible);
    this.refresh();
    return () => {
      unbind();
      doc.removeEventListener("visibilitychange", onVisible);
      this.retry.cancel();
      this.stopTicker();
    };
  }

  /** status() + openAttempts() only; retries through RetryController until it works. */
  refresh(): void {
    this.retry.start(() => this.loadHome());
  }

  private async loadHome(): Promise<void> {
    try {
      const st = await this.api.status();
      if (!st.available) {
        throw new UnavailableError(st.reason ?? "disabled", st.retry_after_s ?? null);
      }
      const open = await this.api.openAttempts();
      this.status = st;
      this.open = open;
      this.notice = null;
      this.setScreen({ kind: "home" });
    } catch (e) {
      if (this.showFatal(e)) return; // terminal (version/auth): stop retrying
      throw e;
    }
  }

  /** Show the unavailable page for an error. Returns true when retrying makes no sense. */
  private showFatal(e: unknown): boolean {
    if (e instanceof VersionError) {
      this.setScreen({ kind: "unavailable", reason: "version", until: null });
      return true;
    }
    if (e instanceof AuthError) {
      this.setScreen({ kind: "unavailable", reason: "auth", until: null });
      return true;
    }
    if (e instanceof UnavailableError) {
      const until = e.retryAfter !== null ? this.now() + e.retryAfter * 1000 : null;
      this.setScreen({ kind: "unavailable", reason: e.reason, until });
      return false;
    }
    if (e instanceof ApiError) {
      this.setScreen({ kind: "unavailable", reason: "network", until: null });
      return false;
    }
    throw e;
  }

  // ---- input ----

  /** Returns true when the key was consumed. */
  handleKey(e: Pick<KeyboardEvent, "keyCode" | "repeat">): boolean {
    const k = this.keys.handle(e);
    if (!k) return false;
    switch (k.key) {
      case "up":
      case "down":
      case "left":
      case "right":
        this.grid.move(k.key);
        return true;
      case "ok": {
        const a = document.activeElement;
        if (a instanceof HTMLElement && this.root.contains(a) && a.hasAttribute("data-focus")) a.click();
        return true;
      }
      case "back":
        this.back();
        return true;
      case "digit":
        this.page?.onDigit?.(k.digit ?? 0);
        return true;
      default:
        return true;
    }
  }

  back(): void {
    if (this.screen.kind === "attempt") this.refresh();
    // home / unavailable / loading: nothing to go back to.
  }

  // ---- actions ----

  private run(fn: () => Promise<void>): void {
    if (this.busy) return;
    this.busy = true;
    fn()
      .catch((e) => this.onActionError(e))
      .finally(() => {
        this.busy = false;
      });
  }

  private async onActionError(e: unknown): Promise<void> {
    if (e instanceof BusyError) return;
    const cur = this.screen.kind === "attempt" ? this.screen.view : null;
    if (e instanceof RejectedError) {
      if (e.code === "cooldown") {
        this.showNotice(`Please wait ${formatCountdown(e.retryAfter ?? 0)} before trying again.`);
        return;
      }
      if (REJECT_NOTICES[e.code]) {
        this.showNotice(REJECT_NOTICES[e.code]);
        return;
      }
      // review_required, stale revision, etc.: reload the attempt from the server.
      if (cur) {
        try {
          this.showAttempt(await this.api.get(cur.id));
          return;
        } catch (e2) {
          this.failToRetry(e2);
          return;
        }
      }
    }
    if (cur && e instanceof NetworkError) {
      // The write may or may not have landed: reconcile with the server, never re-send.
      // A 503 UnavailableError is different: the guard refused it, so nothing landed and
      // it falls through to the unavailable page (with its countdown) below.
      try {
        this.showAttempt(await this.api.get(cur.id));
        return;
      } catch (e2) {
        this.failToRetry(e2);
        return;
      }
    }
    this.failToRetry(e);
  }

  private failToRetry(e: unknown): void {
    if (this.showFatal(e)) return;
    this.refresh();
  }

  private showNotice(text: string): void {
    this.notice = text;
    this.render();
  }

  private startTier(tier: Tier): void {
    this.run(async () => {
      const existing = this.open.find((a) => a.tier === tier);
      this.showAttempt(existing ?? (await this.api.issue(tier)));
    });
  }

  private act(call: (v: AttemptView) => Promise<AttemptView>): void {
    const v = this.screen.kind === "attempt" ? this.screen.view : null;
    if (!v) return;
    this.run(async () => this.showAttempt(await call(v)));
  }

  private showAttempt(view: AttemptView): void {
    if (view.state !== "issued") {
      this.open = this.open.filter((a) => a.id !== view.id);
    } else {
      this.open = this.open.filter((a) => a.id !== view.id).concat(view);
    }
    if (view.answer_type === "passage" && (this.partsSeen === null || this.partsSeen.id !== view.id)) {
      // First sight of this set in this session: only parts answered from now on get feedback.
      this.partsSeen = { id: view.id, count: answeredParts(view) };
    }
    this.notice = null;
    this.setScreen({ kind: "attempt", view });
  }

  // ---- rendering ----

  private setScreen(s: Screen): void {
    this.screen = s;
    this.render();
  }

  private buildPage(): Page {
    const s = this.screen;
    switch (s.kind) {
      case "home":
        return renderHome(this.status as StatusView, this.open, { start: (t) => this.startTier(t) });
      case "unavailable":
        return renderUnavailable(s.reason, s.until === null ? null : Math.max(0, (s.until - this.now()) / 1000));
      case "loading":
        return { el: el("div", "page loading", "Loading…") };
      case "attempt":
        return this.buildAttempt(s.view);
    }
  }

  private buildAttempt(v: AttemptView): Page {
    if (v.state !== "issued" || v.stage === "done") {
      return renderResult(v, () => this.refresh());
    }
    if (v.answer_type === "passage") {
      const answered = answeredParts(v);
      const seen = this.partsSeen;
      if (seen !== null && seen.id === v.id && answered > seen.count) {
        return renderPartFeedback(v, answered - 1, () => {
          seen.count = answered;
          this.render();
        });
      }
      const answer = (optionId: string) =>
        this.act((cur) => this.api.submit(cur.id, cur.rev, optionId, cur.step ?? undefined));
      const skip = () => this.act((cur) => this.api.skip(cur.id, cur.rev));
      if (v.stage === "vocab") {
        return renderVocab(v, { answer, skip, review: () => this.act((cur) => this.api.review(cur.id, cur.rev)) });
      }
      return renderPassage(v, { answer, skip, hint: () => this.act((cur) => this.api.hint(cur.id, cur.rev)) });
    }
    const key = `${v.id}:${v.step ?? 0}`;
    if (this.entry.key !== key) this.entry = { key, text: "" };
    return renderQuestion(v, this.entry, {
      submit: (answer) => this.act((cur) => this.api.submit(cur.id, cur.rev, answer)),
      hint: () => this.act((cur) => this.api.hint(cur.id, cur.rev)),
      skip: () => this.act((cur) => this.api.skip(cur.id, cur.rev)),
    });
  }

  private render(): void {
    const prevId = document.activeElement instanceof HTMLElement ? document.activeElement.id : null;
    const p = this.buildPage();
    this.page = p;
    while (this.root.firstChild) this.root.removeChild(this.root.firstChild);
    if (this.notice) this.root.appendChild(el("div", "notice", this.notice));
    this.root.appendChild(p.el);
    const ids = Array.prototype.map.call(p.el.querySelectorAll("[id]"), (n: Element) => n.id) as string[];
    const sameScreenFocus = prevId && ids.indexOf(prevId) >= 0 ? prevId : null;
    this.grid.restore(sameScreenFocus ?? p.initialFocus ?? null);
    this.syncTicker();
  }

  private syncTicker(): void {
    const s = this.screen;
    if (s.kind === "unavailable" && s.until !== null && s.reason === "paused") {
      if (this.ticker === null) this.ticker = setInterval(() => this.tick(), 1000);
    } else this.stopTicker();
  }

  private stopTicker(): void {
    if (this.ticker !== null) clearInterval(this.ticker);
    this.ticker = null;
  }

  private tick(): void {
    const s = this.screen;
    const c = this.root.querySelector("#countdown");
    if (s.kind !== "unavailable" || s.until === null) return;
    const left = (s.until - this.now()) / 1000;
    if (left <= 0) {
      // The pause is over: ask the server again (it decides; home if available).
      this.stopTicker();
      this.refresh();
      return;
    }
    if (c) c.textContent = formatCountdown(left);
  }
}
