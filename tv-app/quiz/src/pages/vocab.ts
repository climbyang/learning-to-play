import type { AttemptView, VocabItem } from "../api";
import { button, el, page, type Page } from "../dom";
import { skipButton } from "./passage";

export interface VocabActions {
  answer(optionId: string): void;
  review(): void;
  skip(): void;
}

export function currentVocab(v: AttemptView): VocabItem | null {
  return v.vocab.find((w) => !w.done) ?? null;
}

/** Sentence with each occurrence of the word wrapped in <mark>. */
export function highlightSentence(sentence: string, word: string): HTMLElement {
  const box = el("div", "sentence");
  const lower = sentence.toLowerCase();
  const w = word.toLowerCase();
  let pos = 0;
  while (w !== "" && pos < sentence.length) {
    const at = lower.indexOf(w, pos);
    if (at < 0) break;
    if (at > pos) box.appendChild(document.createTextNode(sentence.slice(pos, at)));
    box.appendChild(el("mark", undefined, sentence.slice(at, at + w.length)));
    pos = at + w.length;
  }
  if (pos < sentence.length) box.appendChild(document.createTextNode(sentence.slice(pos)));
  return box;
}

export function renderVocab(v: AttemptView, act: VocabActions): Page {
  const root = page("vocab");
  const w = currentVocab(v);
  if (!w) {
    root.appendChild(el("div", "prompt", "Almost done…"));
    return { el: root };
  }
  root.appendChild(el("div", "word", w.word));
  root.appendChild(el("div", "meaning", w.meaning));
  root.appendChild(highlightSentence(w.sentence, w.word));

  if (w.need_review) {
    root.classList.add("review");
    root.appendChild(el("div", "look-again", "Look again"));
    root.appendChild(button("vocab-read", "I've read it", 0, 0, act.review));
    appendSkip(root, v, 1, act);
    return { el: root };
  }
  root.appendChild(el("div", "prompt", w.check_prompt));
  const list = el("div", "options");
  w.options.forEach((o, i) => {
    list.appendChild(button("opt-" + o.id, o.text, i, 0, () => act.answer(o.id), "btn option"));
  });
  root.appendChild(list);
  appendSkip(root, v, w.options.length, act);
  return { el: root };
}

function appendSkip(root: HTMLElement, v: AttemptView, row: number, act: VocabActions): void {
  const skip = skipButton(v, row, 0, act.skip);
  if (!skip) return;
  const bar = el("div", "actions");
  bar.appendChild(skip);
  root.appendChild(bar);
}
