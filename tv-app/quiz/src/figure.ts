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

interface ChessSpec {
  type: "chess";
  title?: string;
  fen?: unknown;
}

// Solid glyphs for both sides; colour comes from fill/stroke so white pieces stay readable.
const PIECE_GLYPH: Record<string, string> = {
  k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟",
};

/** Board rows from a FEN (rank 8 first), each 8 entries of a piece letter or "". */
export function parseFenBoard(fen: unknown): { rows: string[][]; whiteToMove: boolean } | null {
  if (typeof fen !== "string") return null;
  const fields = fen.trim().split(/\s+/);
  const ranks = fields[0].split("/");
  if (ranks.length !== 8) return null;
  const rows: string[][] = [];
  for (const rank of ranks) {
    const row: string[] = [];
    for (const ch of rank) {
      if (ch >= "1" && ch <= "8") {
        for (let i = 0; i < Number(ch); i++) row.push("");
      } else if (/^[kqrbnpKQRBNP]$/.test(ch)) {
        row.push(ch);
      } else return null;
    }
    if (row.length !== 8) return null;
    rows.push(row);
  }
  return { rows, whiteToMove: fields[1] !== "b" };
}

function renderChess(spec: ChessSpec): HTMLElement | null {
  const parsed = parseFenBoard(spec.fen);
  if (!parsed) return null;
  const sq = 56;
  const margin = 32;
  const size = sq * 8 + margin * 2;
  const files = "abcdefgh";
  const root = svg("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, class: "figure-svg figure-chess" });
  // Drawn from the side to move's point of view.
  const flip = !parsed.whiteToMove;
  for (let r = 0; r < 8; r++) {
    for (let c = 0; c < 8; c++) {
      const row = flip ? 7 - r : r;
      const col = flip ? 7 - c : c;
      const x = margin + c * sq;
      const y = margin + r * sq;
      const light = (row + col) % 2 === 0;
      root.appendChild(svg("rect", { x, y, width: sq, height: sq, fill: light ? "#eed8b0" : "#b07a4f" }));
      const piece = parsed.rows[row][col];
      if (piece) {
        const white = piece === piece.toUpperCase();
        root.appendChild(svg("text", {
          x: x + sq / 2, y: y + sq * 0.8, "text-anchor": "middle", "font-size": sq * 0.85,
          fill: white ? "#ffffff" : "#111111", stroke: white ? "#111111" : "#ffffff", "stroke-width": 1.5,
          class: "chess-piece",
        }, PIECE_GLYPH[piece.toLowerCase()]));
      }
    }
  }
  for (let i = 0; i < 8; i++) {
    const file = files[flip ? 7 - i : i];
    const rank = String(flip ? i + 1 : 8 - i);
    root.appendChild(svg("text", { x: margin + i * sq + sq / 2, y: size - 8, "text-anchor": "middle", fill: "#fff", "font-size": 22 }, file));
    root.appendChild(svg("text", { x: margin / 2, y: margin + i * sq + sq * 0.62, "text-anchor": "middle", fill: "#fff", "font-size": 22 }, rank));
  }
  const wrap = el("div", "figure");
  if (spec.title) wrap.appendChild(el("div", "figure-title", String(spec.title)));
  wrap.appendChild(root);
  return wrap;
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
  if (spec.type === "chess") return renderChess(spec as ChessSpec);
  return null;
}
