import { env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";
import worker from "../src/index";

const bindings = env as Env;
const id = "a".repeat(64), buildId = "b".repeat(64), archiveId = "c".repeat(64);
const request = (path: string, init?: RequestInit) => worker.fetch(new Request("https://mirror.example" + path, init), bindings, {} as ExecutionContext);
async function put(key: string, value: unknown) {
  await bindings.POOL.put("preview/" + key, JSON.stringify(value), { httpMetadata: { contentType: "application/json" } });
}

beforeEach(async () => {
  const listed = await bindings.POOL.list({ prefix: "preview/" });
  if (listed.objects.length) await bindings.POOL.delete(listed.objects.map(o => o.key));
  await put("current.json", { publication: id });
  await put(`publications/${id}.json`, { schema_version: 1, rings: {
    "x86_64/stable": { release: "release-stable", build: buildId, updated_at: "2026-09-06", reason: "test" },
    "x86_64/edge": { release: "release-stable", build: buildId, updated_at: "2026-09-06", reason: "test" },
  } });
  await put(`builds/${buildId}.json`, { release: "release-stable", repositories: { extra: {
    db: "databases/test.db", "db.sig": "signatures/" + id + ".sig", files: "databases/test.files", "files.sig": "signatures/" + id + ".sig",
  } }, catalogue: "catalogues/test.json" });
  await put("routes/x86_64/extra/extra-stable.json", { kind: "ring", ring: "stable", repo: "extra", arch: "x86_64" });
  await put("routes/x86_64/extra/extra-hyprland-oob-c123.json", { kind: "build", build: buildId, repo: "extra", arch: "x86_64" });
  await bindings.POOL.put("preview/databases/test.db", "database");
  await bindings.POOL.put("preview/databases/test.files", "file-list");
  await bindings.POOL.put(`preview/signatures/${id}.sig`, "signature");
  await put("catalogues/test.json", { packages: [{ name: "example", version: "1-1" }] });
  await bindings.POOL.put(`preview/pool/${archiveId}/example-1-1-x86_64.pkg.tar.zst`, "0123456789");
  await put(`archive-signatures/${archiveId}/example-1-1-x86_64.pkg.tar.zst.json`, { signature_key: `signatures/${id}.sig` });
  await put("filenames/x86_64/extra/example-1-1-x86_64.pkg.tar.zst.json", {
    archive_key: `pool/${archiveId}/example-1-1-x86_64.pkg.tar.zst`, signature_key: `signatures/${id}.sig`,
  });
});

describe("pacman routing", () => {
  it("serves ring aliases and conventional aliases with mutable cache policy", async () => {
    for (const alias of ["extra-stable.db", "extra-stable.db.tar.gz", "extra.db"]) {
      const response = await request(`/extra/os/x86_64/${alias}`);
      expect(response.status).toBe(200);
      expect(await response.text()).toBe("database");
      expect(response.headers.get("Cache-Control")).toBe("no-store");
    }
  });
  it("serves immutable candidate databases and matching .files/signatures", async () => {
    const candidate = await request("/extra/os/x86_64/extra-hyprland-oob-c123.db");
    expect(candidate.status).toBe(200);
    expect(candidate.headers.get("Cache-Control")).toContain("immutable");
    expect(await (await request("/extra/os/x86_64/extra-hyprland-oob-c123.files")).text()).toBe("file-list");
    expect(await (await request("/extra/os/x86_64/extra-hyprland-oob-c123.db.sig")).text()).toBe("signature");
  });
  it("keeps a candidate on its build after stable moves", async () => {
    const replacement = "d".repeat(64);
    await put(`builds/${replacement}.json`, { repositories: { extra: { db: "databases/new.db" } } });
    await bindings.POOL.put("preview/databases/new.db", "new database");
    await put(`publications/${id}.json`, { rings: { "x86_64/stable": { build: replacement } } });
    expect(await (await request("/extra/os/x86_64/extra-stable.db")).text()).toBe("new database");
    expect(await (await request("/extra/os/x86_64/extra-hyprland-oob-c123.db")).text()).toBe("database");
  });
  it("redirects old package filenames and detached signatures to the pool", async () => {
    const response = await request("/extra/os/x86_64/example-1-1-x86_64.pkg.tar.zst");
    expect(response.status).toBe(307);
    expect(response.headers.get("Location")).toContain(`/pool/${archiveId}/`);
    const signature = await request("/extra/os/x86_64/example-1-1-x86_64.pkg.tar.zst.sig");
    expect(signature.headers.get("Location")).toContain(`/signatures/${id}.sig`);
    const adjacent = await request(`/pool/${archiveId}/example-1-1-x86_64.pkg.tar.zst.sig`);
    expect(adjacent.status).toBe(200);
    expect(await adjacent.text()).toBe("signature");
    expect(adjacent.headers.get("Cache-Control")).toContain("immutable");
  });
  it("streams full, partial, suffix and resumed responses", async () => {
    const path = `/pool/${archiveId}/example-1-1-x86_64.pkg.tar.zst`;
    const full = await request(path);
    expect(await full.text()).toBe("0123456789");
    const range = await request(path, { headers: { Range: "bytes=3-6" } });
    expect(range.status).toBe(206);
    expect(range.headers.get("Content-Range")).toBe("bytes 3-6/10");
    expect(range.headers.get("Content-Length")).toBe("4");
    expect(await range.text()).toBe("3456");
    expect(await (await request(path, { headers: { Range: "bytes=-3" } })).text()).toBe("789");
    expect(await (await request(path, { headers: { Range: "bytes=7-" } })).text()).toBe("789");
    const invalid = await request(path, { headers: { Range: "bytes=99-" } });
    expect(invalid.status).toBe(416);
    expect(invalid.headers.get("Content-Range")).toBe("bytes */10");
  });
  it("handles HEAD, conditional requests, and If-Range", async () => {
    const path = `/pool/${archiveId}/example-1-1-x86_64.pkg.tar.zst`;
    const head = await request(path, { method: "HEAD" });
    expect(await head.text()).toBe("");
    expect(head.headers.get("Content-Length")).toBe("10");
    const etag = head.headers.get("ETag")!;
    expect((await request(path, { headers: { "If-None-Match": etag } })).status).toBe(304);
    expect((await request(path, { headers: { "If-Match": '"stale"' } })).status).toBe(412);
    expect((await request(path, { headers: { "If-Match": `W/${etag}` } })).status).toBe(412);
    expect((await request(path, { headers: { "If-None-Match": `W/${etag}` } })).status).toBe(304);
    const changed = await request(path, { headers: { Range: "bytes=3-", "If-Range": '"stale"' } });
    expect(changed.status).toBe(200);
    expect(await changed.text()).toBe("0123456789");
  });
  it("never caches missing records or exposes internal registry paths", async () => {
    for (const path of ["/extra/os/x86_64/absent.db", "/current.json", "/metadata/test.json", "/verification/secret"]) {
      const response = await request(path);
      expect(response.status).toBe(404);
      expect(response.headers.get("Cache-Control")).toBe("no-store");
    }
    expect((await request("/extra/os/x86_64/extra-stable.db", { method: "POST" })).status).toBe(405);
  });
  it("publishes catalogue API and website with a pinned publication", async () => {
    expect((await (await request("/api/v1/rings.json")).json() as { publication: string }).publication).toBe(id);
    expect(await (await request(`/api/v1/builds/${buildId}/packages.json`)).json()).toEqual({ packages: [{ name: "example", version: "1-1" }] });
    expect(await (await request("/")).text()).toContain("Omarchy packages");
  });
});
