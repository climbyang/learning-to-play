import { describe, expect, it } from "vitest";
import { acceptRepeat, KeyFilter, mapKey } from "../src/keys";

describe("mapKey", () => {
  const table: Array<[number, string]> = [
    [38, "up"], [40, "down"], [37, "left"], [39, "right"],
    [13, "ok"], [461, "back"], [405, "yellow"], [406, "blue"],
  ];
  it.each(table)("maps %i to %s", (code, key) => {
    expect(mapKey({ keyCode: code })).toEqual({ key });
  });
  it("maps digits 48-57", () => {
    expect(mapKey({ keyCode: 48 })).toEqual({ key: "digit", digit: 0 });
    expect(mapKey({ keyCode: 57 })).toEqual({ key: "digit", digit: 9 });
  });
  it("returns null for 1536/1537 and unknown codes", () => {
    expect(mapKey({ keyCode: 1536 })).toBeNull();
    expect(mapKey({ keyCode: 1537 })).toBeNull();
    expect(mapKey({ keyCode: 65 })).toBeNull();
  });
});

describe("acceptRepeat", () => {
  it("is true only for directions", () => {
    for (const k of ["up", "down", "left", "right"] as const) expect(acceptRepeat(k)).toBe(true);
    for (const k of ["ok", "back", "digit", "yellow", "blue"] as const) expect(acceptRepeat(k)).toBe(false);
  });
});

describe("KeyFilter", () => {
  it("ignores OK repeat but accepts Up repeat", () => {
    const f = new KeyFilter(() => 0);
    expect(f.handle({ keyCode: 13, repeat: true })).toBeNull();
    expect(f.handle({ keyCode: 13, repeat: false })).toEqual({ key: "ok" });
    expect(f.handle({ keyCode: 38, repeat: true })).toEqual({ key: "up" });
  });
  it("debounces back for 300 ms", () => {
    let t = 1000;
    const f = new KeyFilter(() => t);
    expect(f.handle({ keyCode: 461, repeat: false })).toEqual({ key: "back" });
    t = 1299;
    expect(f.handle({ keyCode: 461, repeat: false })).toBeNull();
    t = 1300;
    expect(f.handle({ keyCode: 461, repeat: false })).toEqual({ key: "back" });
  });
  it("debounces yellow and blue independently", () => {
    const f = new KeyFilter(() => 5);
    expect(f.handle({ keyCode: 405, repeat: false })).not.toBeNull();
    expect(f.handle({ keyCode: 406, repeat: false })).not.toBeNull();
    expect(f.handle({ keyCode: 405, repeat: false })).toBeNull();
  });
  it("does not debounce ok or directions", () => {
    const f = new KeyFilter(() => 5);
    expect(f.handle({ keyCode: 13, repeat: false })).not.toBeNull();
    expect(f.handle({ keyCode: 13, repeat: false })).not.toBeNull();
  });
});
