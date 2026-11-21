import { beforeEach, describe, expect, it } from "vitest";
import { FocusGrid } from "../src/focus";

let root: HTMLElement;
function cell(id: string, row: number, col: number, parent: HTMLElement = root): HTMLButtonElement {
  const b = document.createElement("button");
  b.id = id;
  b.dataset.focus = "";
  b.dataset.row = String(row);
  b.dataset.col = String(col);
  parent.appendChild(b);
  return b;
}
const active = () => document.activeElement?.id;

beforeEach(() => {
  document.body.innerHTML = "";
  root = document.createElement("div");
  document.body.appendChild(root);
});

describe("FocusGrid", () => {
  it("focusFirst and move in all directions", () => {
    cell("a", 0, 0); cell("b", 0, 1); cell("c", 1, 0); cell("d", 1, 1);
    const g = new FocusGrid(root);
    expect(g.focusFirst()).toBe(true);
    expect(active()).toBe("a");
    expect(g.move("right")).toBe(true);
    expect(active()).toBe("b");
    expect(g.move("down")).toBe(true);
    expect(active()).toBe("d");
    expect(g.move("left")).toBe(true);
    expect(active()).toBe("c");
    expect(g.move("up")).toBe(true);
    expect(active()).toBe("a");
    expect(g.move("up")).toBe(false);
    expect(g.move("left")).toBe(false);
    expect(active()).toBe("a");
  });
  it("picks the nearest column when moving vertically", () => {
    cell("a", 0, 0); cell("b", 0, 2); cell("c", 1, 0); cell("d", 1, 1);
    const g = new FocusGrid(root);
    g.restore("b");
    g.move("down");
    expect(active()).toBe("d");
  });
  it("skips hidden and disabled elements", () => {
    cell("a", 0, 0);
    const b = cell("b", 0, 1); b.disabled = true;
    const c = cell("c", 0, 2); c.hidden = true;
    cell("d", 0, 3);
    const g = new FocusGrid(root);
    g.focusFirst();
    g.move("right");
    expect(active()).toBe("d");
  });
  it("restore focuses previous id, else falls back to first visible", () => {
    cell("a", 0, 0); cell("b", 0, 1);
    const g = new FocusGrid(root);
    expect(g.restore("b")).toBe(true);
    expect(active()).toBe("b");
    root.innerHTML = "";
    cell("x", 0, 0); cell("y", 0, 1);
    expect(g.restore("b")).toBe(true);
    expect(active()).toBe("x");
    expect(g.restore(null)).toBe(true);
  });
  it("move after the focused node was removed recovers to first element", () => {
    const a = cell("a", 0, 0); cell("b", 0, 1);
    const g = new FocusGrid(root);
    g.focusFirst();
    a.remove();
    expect(g.move("right")).toBe(true);
    expect(active()).toBe("b");
  });
  it("handles an empty grid", () => {
    const g = new FocusGrid(root);
    expect(g.focusFirst()).toBe(false);
    expect(g.restore("a")).toBe(false);
    expect(g.move("down")).toBe(false);
  });
  it("scroll region scrolls on up/down until boundary, then focus leaves", () => {
    cell("top", 0, 0);
    const region = document.createElement("div");
    region.dataset.scroll = "";
    root.appendChild(region);
    let st = 0;
    Object.defineProperty(region, "scrollTop", { get: () => st, set: (v: number) => { st = v; } });
    Object.defineProperty(region, "scrollHeight", { value: 300 });
    Object.defineProperty(region, "clientHeight", { value: 200 });
    cell("in", 1, 0, region);
    cell("below", 2, 0);
    const g = new FocusGrid(root);
    g.restore("in");
    expect(g.move("down")).toBe(true);
    expect(st).toBe(100);
    expect(active()).toBe("in");
    expect(g.move("down")).toBe(true);
    expect(active()).toBe("below");
    g.restore("in");
    expect(g.move("up")).toBe(true);
    expect(st).toBe(0);
    expect(active()).toBe("in");
    expect(g.move("up")).toBe(true);
    expect(active()).toBe("top");
  });
});
