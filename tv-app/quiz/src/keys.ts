export type Key = "up" | "down" | "left" | "right" | "ok" | "back" | "digit" | "yellow" | "blue";
export interface MappedKey {
  key: Key;
  digit?: number;
}

export const DEBOUNCE_MS = 300;
const DEBOUNCED: ReadonlySet<Key> = new Set<Key>(["back", "yellow", "blue"]);
const DIRECTIONS: ReadonlySet<Key> = new Set<Key>(["up", "down", "left", "right"]);

const CODE_MAP: Record<number, Key> = {
  38: "up",
  40: "down",
  37: "left",
  39: "right",
  13: "ok",
  461: "back",
  405: "yellow",
  406: "blue",
};

/** Map a keydown to an app key; null for 1536/1537 (webOS system noise) and unknown codes. */
export function mapKey(e: Pick<KeyboardEvent, "keyCode">): MappedKey | null {
  const code = e.keyCode;
  if (code === 1536 || code === 1537) return null;
  if (code >= 48 && code <= 57) return { key: "digit", digit: code - 48 };
  const key = CODE_MAP[code];
  return key ? { key } : null;
}

/** Only direction keys may auto-repeat. */
export function acceptRepeat(key: Key): boolean {
  return DIRECTIONS.has(key);
}

/** Stateful filter: drops disallowed auto-repeat and debounces back/yellow/blue. */
export class KeyFilter {
  private last = new Map<Key, number>();

  constructor(private now: () => number = () => Date.now()) {}

  handle(e: Pick<KeyboardEvent, "keyCode" | "repeat">): MappedKey | null {
    const mapped = mapKey(e);
    if (!mapped) return null;
    if (e.repeat && !acceptRepeat(mapped.key)) return null;
    if (DEBOUNCED.has(mapped.key)) {
      const t = this.now();
      const prev = this.last.get(mapped.key);
      if (prev !== undefined && t - prev < DEBOUNCE_MS) return null;
      this.last.set(mapped.key, t);
    }
    return mapped;
  }
}
