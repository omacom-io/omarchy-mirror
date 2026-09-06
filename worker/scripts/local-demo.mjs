// Run the real Worker against an isolated local R2 emulation, seeded from a CLI store.
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { readdir, readFile } from "node:fs/promises";
import { resolve, join } from "node:path";

const root = process.argv[2];
if (!root) throw new Error("Usage: node scripts/local-demo.mjs /path/to/local/store [port]");
const mf = new Miniflare(convertV4MiniflareOptions({
  host: "127.0.0.1", port: Number(process.argv[3] || 8787),
  workers: [{ name: "mirror-demo", modules: true, scriptPath: resolve("dist/index.js"), compatibilityDate: "2026-09-06",
    compatibilityFlags: ["nodejs_compat"], r2Buckets: ["POOL"],
    bindings: { STORE_PREFIX: "preview", DEFAULT_RING: "edge", HOST_RINGS: "{}" } }],
}));
const bucket = await mf.getR2Bucket("POOL");
async function seed(directory, prefix = "") {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name.startsWith(".")) continue;
    const relative = prefix + entry.name;
    if (entry.isDirectory()) await seed(join(directory, entry.name), relative + "/");
    else if (entry.isFile()) await bucket.put("preview/" + relative, await readFile(join(directory, entry.name)), {
      httpMetadata: { contentType: relative.endsWith(".json") ? "application/json" : "application/octet-stream" },
    });
  }
}
await seed(root);
console.log(`Seeded local Worker ready at ${await mf.ready}`);
async function stop() { await mf.dispose(); process.exit(0); }
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
