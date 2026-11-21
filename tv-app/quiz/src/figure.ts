import { el } from "./dom";

const SVG_NS = "http://www.w3.org/2000/svg";

interface BarSpec {
  type: "bar";
  title?: string;
  data?: { label: unknown; value: unknown }[];
  labels?: unknown[];
  values?: unknown[];
}
interface TableSpec {
  type: "table";
  title?: string;
  headers?: unknown[];
  rows?: unknown[][];
}

function svg(tag: string, attrs: Record<string, string | number>, text?: string): SVGElement {
  const e = document.createElementNS(SVG_NS, tag);
  for (const k of Object.keys(attrs)) e.setAttribute(k, String(attrs[k]));
  if (text !== undefined) e.textContent = text;
  return e;
}

function barPoints(spec: BarSpec): { label: string; value: number }[] | null {
  let pts: { label: string; value: number }[] = [];
  if (Array.isArray(spec.data)) {
    pts = spec.data.map((d) => ({ label: String(d?.label), value: Number(d?.value) }));
  } else if (Array.isArray(spec.labels) && Array.isArray(spec.values)) {
    pts = spec.labels.map((l, i) => ({ label: String(l), value: Number(spec.values?.[i]) }));
  } else return null;
  if (pts.length === 0 || pts.some((p) => !isFinite(p.value) || p.value < 0)) return null;
  return pts;
}

function renderBar(spec: BarSpec): HTMLElement | null {
  const pts = barPoints(spec);
  if (!pts) return null;
  const W = 760;
  const H = 360;
  const top = 20;
  const bottom = 60;
  const max = Math.max(...pts.map((p) => p.value)) || 1;
  const slot = W / pts.length;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "figure-svg" });
  pts.forEach((p, i) => {
    const h = ((H - top - bottom) * p.value) / max;
    const x = i * slot + slot * 0.15;
    root.appendChild(svg("rect", { x, y: H - bottom - h, width: slot * 0.7, height: h, fill: "#4fc3f7" }));
    root.appendChild(
      svg("text", { x: x + slot * 0.35, y: H - bottom - h - 8, "text-anchor": "middle", fill: "#fff", "font-size": 32 }, String(p.value)),
    );
    root.appendChild(
      svg("text", { x: x + slot * 0.35, y: H - 20, "text-anchor": "middle", fill: "#fff", "font-size": 32 }, p.label),
    );
  });
  const wrap = el("div", "figure");
  if (spec.title) wrap.appendChild(el("div", "figure-title", String(spec.title)));
  wrap.appendChild(root);
  return wrap;
}

function renderTable(spec: TableSpec): HTMLElement | null {
  if (!Array.isArray(spec.rows) || spec.rows.length === 0 || !spec.rows.every(Array.isArray)) return null;
  const t = el("table", "figure-table");
  if (Array.isArray(spec.headers)) {
    const tr = el("tr");
    spec.headers.forEach((c) => tr.appendChild(el("th", undefined, String(c))));
    t.appendChild(tr);
  }
  spec.rows.forEach((r) => {
    const tr = el("tr");
    r.forEach((c) => tr.appendChild(el("td", undefined, String(c))));
    t.appendChild(tr);
  });
  const wrap = el("div", "figure");
  if (spec.title) wrap.appendChild(el("div", "figure-title", String(spec.title)));
  wrap.appendChild(t);
  return wrap;
}

/** Render a figure spec (JSON string) as SVG/HTML. Unknown or malformed figures return null (hidden). */
export function renderFigure(json: string | null): HTMLElement | null {
  if (!json) return null;
  let spec: { type?: unknown };
  try {
    spec = JSON.parse(json);
  } catch {
    return null;
  }
  if (!spec || typeof spec !== "object") return null;
  if (spec.type === "bar") return renderBar(spec as BarSpec);
  if (spec.type === "table") return renderTable(spec as TableSpec);
  return null;
}
