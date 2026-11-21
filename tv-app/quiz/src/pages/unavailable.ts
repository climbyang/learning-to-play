import { el, page, type Page } from "../dom";

export type UnavailableReason = "locked" | "curfew" | "paused" | "disabled" | "version" | "network" | "auth";

const TEXTS: Record<UnavailableReason, string> = {
  locked: "The quiz is locked right now.",
  curfew: "It's past bedtime. The quiz is closed until tomorrow.",
  paused: "The quiz is paused.",
  disabled: "The quiz is switched off.",
  version: "Please update the quiz app",
  network: "Can't reach the server, retrying…",
  auth: "The quiz app is not set up correctly. Please ask a parent.",
};

export function unavailableText(reason: string): string {
  return TEXTS[reason as UnavailableReason] ?? "The quiz is not available right now.";
}

export function formatCountdown(sec: number): string {
  const s = Math.max(0, Math.ceil(sec));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}

/** The countdown element carries id="countdown"; the app updates it once per second. */
export function renderUnavailable(reason: string, countdownSec: number | null): Page {
  const root = page("unavailable");
  root.appendChild(el("h1", "message", unavailableText(reason)));
  if (reason === "paused" && countdownSec !== null) {
    const c = el("div", "countdown", formatCountdown(countdownSec));
    c.id = "countdown";
    root.appendChild(c);
  }
  return { el: root };
}
