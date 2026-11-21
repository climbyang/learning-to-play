export type Direction = "up" | "down" | "left" | "right";

const SCROLL_STEP_PX = 120;

function isUsable(el: HTMLElement): boolean {
  if ((el as HTMLButtonElement).disabled || el.hasAttribute("disabled")) return false;
  for (let n: HTMLElement | null = el; n; n = n.parentElement) {
    if (n.hidden) return false;
    const s = getComputedStyle(n);
    if (s.display === "none" || s.visibility === "hidden") return false;
  }
  return true;
}

interface Cell {
  el: HTMLElement;
  row: number;
  col: number;
}

/** Spatial focus navigation over `[data-focus]` elements positioned by data-row / data-col. */
export class FocusGrid {
  private lastFocused: HTMLElement | null = null;

  constructor(private root: HTMLElement) {}

  private cells(): Cell[] {
    const out: Cell[] = [];
    this.root.querySelectorAll<HTMLElement>("[data-focus]").forEach((el) => {
      if (!isUsable(el)) return;
      out.push({ el, row: Number(el.dataset.row ?? 0), col: Number(el.dataset.col ?? 0) });
    });
    out.sort((a, b) => a.row - b.row || a.col - b.col);
    return out;
  }

  private current(cells: Cell[]): Cell | null {
    const active = document.activeElement;
    return cells.find((c) => c.el === active) ?? cells.find((c) => c.el === this.lastFocused) ?? null;
  }

  private focus(el: HTMLElement): void {
    if (el.tabIndex < 0 && !el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
    el.focus();
    this.lastFocused = el;
  }

  focusFirst(): boolean {
    const first = this.cells()[0];
    if (!first) return false;
    this.focus(first.el);
    return true;
  }

  /** Re-focus the element with `previousId` after a re-render; falls back to the first usable element. */
  restore(previousId: string | null | undefined): boolean {
    const cells = this.cells();
    const hit = previousId ? cells.find((c) => c.el.id === previousId) : undefined;
    const target = hit ?? cells[0];
    if (!target) return false;
    this.focus(target.el);
    return true;
  }

  /** Move focus; returns true if focus moved or a scroll region consumed the key. */
  move(dir: Direction): boolean {
    const cells = this.cells();
    if (cells.length === 0) return false;
    const cur = this.current(cells);
    if (!cur) return this.focusFirst();

    if (dir === "up" || dir === "down") {
      const region = cur.el.closest<HTMLElement>("[data-scroll]");
      if (region && this.scrollRegion(region, dir)) return true;
    }

    let best: Cell | null = null;
    if (dir === "left" || dir === "right") {
      const sign = dir === "right" ? 1 : -1;
      for (const c of cells) {
        if (c.row !== cur.row || (c.col - cur.col) * sign <= 0) continue;
        if (!best || Math.abs(c.col - cur.col) < Math.abs(best.col - cur.col)) best = c;
      }
    } else {
      const sign = dir === "down" ? 1 : -1;
      for (const c of cells) {
        const dr = (c.row - cur.row) * sign;
        if (dr <= 0) continue;
        if (!best) {
          best = c;
          continue;
        }
        const bdr = (best.row - cur.row) * sign;
        if (dr < bdr || (dr === bdr && Math.abs(c.col - cur.col) < Math.abs(best.col - cur.col))) best = c;
      }
    }
    if (!best) return false;
    this.focus(best.el);
    return true;
  }

  private scrollRegion(region: HTMLElement, dir: "up" | "down"): boolean {
    const max = region.scrollHeight - region.clientHeight;
    if (dir === "up") {
      if (region.scrollTop <= 0) return false;
      region.scrollTop = Math.max(0, region.scrollTop - SCROLL_STEP_PX);
      return true;
    }
    if (region.scrollTop >= max) return false;
    region.scrollTop = Math.min(max, region.scrollTop + SCROLL_STEP_PX);
    return true;
  }
}
