// Minimal typings for the compat guard test (no @types/node in this project).
declare module "node:fs" {
  export function readdirSync(p: string): string[];
  export function readFileSync(p: string, enc: "utf8"): string;
  export function statSync(p: string): { isDirectory(): boolean };
}
declare module "node:path" {
  export function join(...p: string[]): string;
}
declare const __dirname: string;
