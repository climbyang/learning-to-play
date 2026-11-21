import type { AttemptView, StatusView, Tier } from "../api";
import { button, el, page, type Page } from "../dom";

export interface HomeActions {
  start(tier: Tier): void;
}

export function renderHome(status: StatusView, open: AttemptView[], act: HomeActions): Page {
  const root = page("home");
  root.appendChild(el("h1", undefined, "Quiz time"));
  const stats = el("div", "stats");
  stats.appendChild(el("div", "stat", `Time left: ${Math.floor(status.remaining_s / 60)} min`));
  stats.appendChild(el("div", "stat", `Correct today: ${status.quiz_today}`));
  stats.appendChild(el("div", "stat", `Earned today: ${status.earned_minutes} min`));
  root.appendChild(stats);

  const hasRegular = open.some((a) => a.tier === "regular");
  const hasChallenge = open.some((a) => a.tier === "challenge");
  const buttons = el("div", "buttons");
  buttons.appendChild(
    button("home-start", hasRegular ? "Continue quiz" : "Start quiz", 0, 0, () => act.start("regular")),
  );
  if (hasChallenge || status.challenge_state === "none" || status.challenge_state === "issued") {
    buttons.appendChild(
      button("home-challenge", hasChallenge ? "Continue challenge" : "Daily challenge", 1, 0, () =>
        act.start("challenge"),
      ),
    );
  } else {
    const done = button("home-challenge", "Challenge done", 1, 0, () => undefined);
    done.disabled = true;
    buttons.appendChild(done);
  }
  root.appendChild(buttons);
  return { el: root };
}
