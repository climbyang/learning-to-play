/** Tiny DOM helpers; all text goes through textContent (never innerHTML). */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  cls?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

export function button(
  id: string,
  label: string,
  row: number,
  col: number,
  onClick: () => void,
  cls = "btn",
): HTMLButtonElement {
  const b = el("button", cls, label);
  b.type = "button";
  b.id = id;
  b.setAttribute("data-focus", "");
  b.dataset.row = String(row);
  b.dataset.col = String(col);
  b.addEventListener("click", onClick);
  return b;
}

export interface Page {
  el: HTMLElement;
  /** Remote digit key (numeric question pages only). */
  onDigit?: (d: number) => void;
  /** Element id to focus after render (defaults to the first focusable element). */
  initialFocus?: string;
}

export function page(cls: string): HTMLElement {
  return el("div", "page " + cls);
}
