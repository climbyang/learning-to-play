// Build the TV quiz app into dist/. Never prints the token.
import { build } from "esbuild";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, readFileSync, existsSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const root = dirname(fileURLToPath(import.meta.url));
const dist = join(root, "dist");

function parseEnvFile(path) {
  const out = {};
  if (!existsSync(path)) return out;
  for (const raw of readFileSync(path, "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    let val = line.slice(eq + 1).trim();
    if (/^(".*"|'.*')$/.test(val)) val = val.slice(1, -1);
    out[line.slice(0, eq).trim()] = val;
  }
  return out;
}

const env = { ...parseEnvFile(join(root, ".env.local")), ...process.env };
const missing = ["LTP_QUIZ_URL", "LTP_QUIZ_TOKEN"].filter((k) => !env[k]);
if (missing.length) {
  console.error(`Build failed: missing ${missing.join(", ")} (set in tv-app/quiz/.env.local, see .env.local.example)`);
  process.exit(1);
}

rmSync(dist, { recursive: true, force: true });
mkdirSync(dist, { recursive: true });

await build({
  entryPoints: [join(root, "src/main.ts")],
  outfile: join(dist, "app.js"),
  bundle: true,
  minify: true,
  target: "chrome79",
  format: "iife",
  sourcemap: false,
  define: {
    __LTP_QUIZ_URL__: JSON.stringify(env.LTP_QUIZ_URL),
    __LTP_QUIZ_TOKEN__: JSON.stringify(env.LTP_QUIZ_TOKEN),
  },
  logLevel: "warning",
});

for (const f of ["index.html", "styles.css", "appinfo.json", "icon.png", "largeIcon.png"]) {
  copyFileSync(join(root, f), join(dist, f));
}

const require = createRequire(import.meta.url);
const esCheckBin = join(dirname(require.resolve("es-check/package.json")), "index.js");
execFileSync(process.execPath, [esCheckBin, "es2019", "dist/app.js"], { stdio: "inherit", cwd: root });
console.log("Build OK: dist/");
