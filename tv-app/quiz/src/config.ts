// Values are injected at build time by esbuild `define` (see build.mjs).
declare const __LTP_QUIZ_URL__: string;
declare const __LTP_QUIZ_TOKEN__: string;

export const QUIZ_URL: string = __LTP_QUIZ_URL__.replace(/\/+$/, "");
export const QUIZ_TOKEN: string = __LTP_QUIZ_TOKEN__;
export const API_VERSION = "2";
export const REQUEST_TIMEOUT_MS = 10_000;
