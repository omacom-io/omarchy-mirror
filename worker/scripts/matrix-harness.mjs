// Serve the real Worker over an isolated local R2 emulation and expose a small
// control port so a pacman client matrix can activate and roll back rings.
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { readdir, readFile } from "node:fs/promises";
import { resolve, join } from "node:path";

const root = process.argv[2];
if (!root) throw new Error("Usage: node scripts/matrix-harness.mjs /path/to/store [port] [controlPort]");
const port = Number(process.argv[3] || 8787), controlPort = Number(process.argv[4] || 8788);

const mf = new Miniflare(convertV4MiniflareOptions({
  host: "0.0.0.0", port,
  workers: [{ name: "mirror-matrix", modules: true, scriptPath: resolve("dist/index.js"), compatibilityDate: "2026-09-06",
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

const read = async key => JSON.parse(await (await bucket.get("preview/" + key)).text());
const encode = value => Buffer.from(JSON.stringify(value) + "\n");

// Activating a ring rewrites the publication exactly as the publisher does:
// the databases themselves are content addressed and are never re-uploaded.
async function activate(ring, build, reason) {
  const pointer = await read("current.json");
  const previous = await read(`publications/${pointer.publication}.json`);
  const rings = { ...previous.rings };
  const key = `x86_64/${ring}`;
  if (!rings[key]) throw new Error(`Unknown ring ${key}`);
  rings[key] = { ...rings[key], build, reason, updated_at: new Date().toISOString() };
  const document = { schema_version: 1, rings, previous: pointer.publication, created_at: new Date().toISOString(),
    change: { ring: key, from: previous.rings[key].build, to: build, reason } };
  const body = encode(document);
  const id = createHash("sha256").update(body).digest("hex");
  await bucket.put(`preview/publications/${id}.json`, body, { httpMetadata: { contentType: "application/json" } });
  await bucket.put("preview/current.json", encode({ publication: id, schema_version: 1 }),
    { httpMetadata: { contentType: "application/json" } });
  return { publication: id, ring: key, build };
}

createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://control");
    if (url.pathname === "/activate") {
      const result = await activate(url.searchParams.get("ring"), url.searchParams.get("build"),
        url.searchParams.get("reason") || "matrix step");
      response.writeHead(200, { "Content-Type": "application/json" });
      return response.end(JSON.stringify(result));
    }
    if (url.pathname === "/builds") {
      const pointer = await read("current.json");
      const current = await read(`publications/${pointer.publication}.json`);
      response.writeHead(200, { "Content-Type": "application/json" });
      return response.end(JSON.stringify(current.rings));
    }
    response.writeHead(404).end("not found");
  } catch (error) {
    response.writeHead(500, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ error: String(error && error.message || error) }));
  }
}).listen(controlPort, "0.0.0.0");

console.log(`worker ${await mf.ready} control http://0.0.0.0:${controlPort}`);
async function stop() { await mf.dispose(); process.exit(0); }
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
