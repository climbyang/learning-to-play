import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// The target TV runs Chrome 79: guard against newer APIs/CSS that esbuild cannot lower.
const root = join(__dirname, "..");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((n: string) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : p.endsWith(".ts") ? [p] : [];
  });
}

const JS_FORBIDDEN: [string, RegExp][] = [
  ["replaceChildren (Chrome 86)", /\.replaceChildren\(/],
  ["structuredClone (Chrome 98)", /\bstructuredClone\(/],
  [".at() (Chrome 92)", /\.at\(/],
  ["replaceAll (Chrome 85)", /\.replaceAll\(/],
  ["Promise.any (Chrome 85)", /Promise\.any\(/],
  ["Object.hasOwn (Chrome 93)", /Object\.hasOwn\(/],
  ["findLast (Chrome 97)", /\.findLast(Index)?\(/],
  ["AbortSignal.timeout (Chrome 103)", /AbortSignal\.timeout\(/],
];
const CSS_FORBIDDEN: [string, RegExp][] = [
  ["inset (Chrome 87)", /(^|[;{\s])inset\s*:/],
  ["gap (flex gap is Chrome 84; use margins)", /(^|[;{\s])(row-|column-)?gap\s*:/],
  ["aspect-ratio (Chrome 88)", /aspect-ratio\s*:/],
  [":focus-visible (Chrome 86)", /:focus-visible/],
  [":is( (Chrome 88)", /:is\(/],
  [":where( (Chrome 88)", /:where\(/],
  ["min()/max()/clamp() (Chrome 79 has min/max, not clamp)", /clamp\(/],
];

describe("Chrome 79 compatibility guard", () => {
  const files = walk(join(root, "src"));
  it("finds sources to scan", () => expect(files.length).toBeGreaterThan(5));
  for (const [name, re] of JS_FORBIDDEN) {
    it("no " + name, () => {
      const hits = files.filter((f) => re.test(readFileSync(f, "utf8")));
      expect(hits).toEqual([]);
    });
  }
  const css = readFileSync(join(root, "styles.css"), "utf8");
  for (const [name, re] of CSS_FORBIDDEN) {
    it("css: no " + name, () => expect(css).not.toMatch(re));
  }
});
