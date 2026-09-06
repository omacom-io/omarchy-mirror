import { page } from "./page";

type Ring = { release: string; build: string; updated_at: string; reason: string };
type Publication = { schema_version: number; rings: Record<string, Ring> };
type Build = { release: string; repositories: Record<string, Record<string, string>>; catalogue: string };
type Route = { kind: "ring" | "build"; ring?: string; build?: string; arch: string; repo: string };

class HttpError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

function safe(value: string): string {
  if (!value || value.startsWith("/") || value.split("/").some(v => !v || v === "." || v === "..") || /[\\\x00-\x20\x7f]/.test(value)) {
    throw new HttpError(400, "Invalid path");
  }
  return value;
}

function key(env: Env, path: string): string {
  const prefix = env.STORE_PREFIX.replace(/^\/+|\/+$/g, "");
  return prefix ? `${safe(prefix)}/${safe(path)}` : safe(path);
}

async function json<T>(env: Env, path: string): Promise<T> {
  const object = await env.POOL.get(key(env, path));
  if (!object) throw new HttpError(404, "Not found");
  if (object.size > 16 * 1024 * 1024) throw new HttpError(503, "Registry document too large");
  return object.json<T>();
}

async function publication(env: Env): Promise<{ id: string; data: Publication }> {
  const pointer = await json<{ publication: string }>(env, "current.json");
  if (!/^[a-f0-9]{64}$/.test(pointer.publication)) throw new HttpError(503, "Invalid publication");
  return { id: pointer.publication, data: await json<Publication>(env, `publications/${pointer.publication}.json`) };
}

async function buildForRing(env: Env, arch: string, ring: string): Promise<Build> {
  const current = await publication(env);
  const selected = current.data.rings[`${arch}/${ring}`];
  if (!selected) throw new HttpError(404, "Ring not found");
  return json<Build>(env, `builds/${selected.build}.json`);
}

function matches(header: string | null, etag: string, weak = true): boolean {
  return header !== null && header.split(",").map(v => v.trim()).some(v => v === "*" || (weak ? v.replace(/^W\//, "") : v) === etag);
}

function byteRange(header: string, size: number): { offset: number; length: number } {
  const match = /^bytes=(\d*)-(\d*)$/.exec(header);
  if (!match || (!match[1] && !match[2]) || size === 0) throw new HttpError(416, "Unsupported range");
  let start: number, end: number;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) throw new HttpError(416, "Invalid range");
    start = Math.max(0, size - suffix); end = size - 1;
  } else {
    start = Number(match[1]); end = match[2] ? Number(match[2]) : size - 1;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start > end || start >= size) throw new HttpError(416, "Invalid range");
    end = Math.min(end, size - 1);
  }
  return { offset: start, length: end - start + 1 };
}

async function serve(request: Request, env: Env, objectKey: string, immutable: boolean): Promise<Response> {
  const storedKey = key(env, objectKey);
  const head = await env.POOL.head(storedKey);
  if (!head) throw new HttpError(404, "Not found");
  const headers = new Headers({
    "Cache-Control": immutable ? "public, max-age=31536000, immutable" : "no-store",
    "ETag": head.httpEtag, "Last-Modified": head.uploaded.toUTCString(),
    "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff",
    "Content-Type": head.httpMetadata?.contentType || "application/octet-stream",
    "Access-Control-Allow-Origin": "*",
  });
  if (request.headers.has("If-Match") && !matches(request.headers.get("If-Match"), head.httpEtag, false)) {
    return new Response(null, { status: 412, headers });
  }
  const modifiedSince = request.headers.get("If-Modified-Since");
  if (matches(request.headers.get("If-None-Match"), head.httpEtag) ||
      (!request.headers.has("If-None-Match") && modifiedSince && Math.floor(head.uploaded.getTime() / 1000) <= Math.floor(Date.parse(modifiedSince) / 1000))) {
    return new Response(null, { status: 304, headers });
  }
  let range: { offset: number; length: number } | undefined;
  const rangeHeader = request.headers.get("Range");
  const ifRange = request.headers.get("If-Range");
  const rangeAllowed = !ifRange || ifRange === head.httpEtag || (!ifRange.startsWith('"') && Date.parse(ifRange) >= Math.floor(head.uploaded.getTime() / 1000) * 1000);
  if (request.method === "GET" && rangeHeader && rangeAllowed) {
    try { range = byteRange(rangeHeader, head.size); }
    catch (error) {
      if (!(error instanceof HttpError)) throw error;
      headers.set("Content-Range", `bytes */${head.size}`);
      return new Response(null, { status: 416, headers });
    }
  }
  headers.set("Content-Length", String(range?.length ?? head.size));
  if (range) headers.set("Content-Range", `bytes ${range.offset}-${range.offset + range.length - 1}/${head.size}`);
  if (request.method === "HEAD") return new Response(null, { headers });
  const object = await env.POOL.get(storedKey, { onlyIf: { etagMatches: head.etag }, range });
  if (!object) throw new HttpError(404, "Not found");
  if (!("body" in object)) {
    headers.delete("Content-Length");
    return new Response(null, { status: 412, headers });
  }
  return new Response(object.body, { status: range ? 206 : 200, headers });
}

async function handle(request: Request, env: Env): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method not allowed", { status: 405, headers: { "Allow": "GET, HEAD", "Cache-Control": "no-store" } });
  }
  const url = new URL(request.url);
  let path: string;
  try { path = decodeURIComponent(url.pathname).replace(/^\//, ""); }
  catch { throw new HttpError(400, "Invalid path encoding"); }
  if (path === "") return new Response(request.method === "HEAD" ? null : page, { headers: {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
  } });
  safe(path);
  if (path === "api/v1/rings.json") {
    const current = await publication(env);
    return new Response(request.method === "HEAD" ? null : JSON.stringify({ schema_version: 1, publication: current.id, rings: current.data.rings }), {
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*" },
    });
  }
  const api = /^api\/v1\/rings\/([a-z0-9_.-]+)\/([a-z0-9_.-]+)\/packages\.json$/.exec(path);
  if (api) return serve(request, env, (await buildForRing(env, api[1], api[2])).catalogue, false);
  const catalog = /^api\/v1\/builds\/([a-f0-9]{64})\/packages\.json$/.exec(path);
  if (catalog) return serve(request, env, (await json<Build>(env, `builds/${catalog[1]}.json`)).catalogue, true);
  if (/^pool\/[a-f0-9]{64}\/[A-Za-z0-9_+.~:@-]+\.pkg\.tar\.[A-Za-z0-9]+$/.test(path) || /^signatures\/[a-f0-9]{64}\.sig$/.test(path)) {
    return serve(request, env, path, true);
  }
  // libalpm requests the detached signature beside the effective package URL
  // after following a redirect, even when the database embeds that signature.
  const adjacentSignature = /^pool\/([a-f0-9]{64})\/([A-Za-z0-9_+.~:@-]+\.pkg\.tar\.[A-Za-z0-9]+)\.sig$/.exec(path);
  if (adjacentSignature) {
    const artifact = await json<{ signature_key: string }>(env, `archive-signatures/${adjacentSignature[1]}/${adjacentSignature[2]}.json`);
    return serve(request, env, artifact.signature_key, true);
  }
  const conventional = /^([a-z0-9_.-]+)\/os\/([a-z0-9_.-]+)\/([^/]+)$/.exec(path);
  const arm = /^([a-z0-9_.-]+)\/([a-z0-9_.-]+)\/([^/]+)$/.exec(path);
  const repo = conventional?.[1] ?? arm?.[2];
  const arch = conventional?.[2] ?? arm?.[1];
  const file = conventional?.[3] ?? arm?.[3];
  if (!repo || !arch || !file) throw new HttpError(404, "Not found");
  const database = /^([a-z0-9_.-]+)\.(db|files)(?:\.tar\.gz)?(\.sig)?$/.exec(file);
  if (database) {
    let route: Route;
    if (database[1] === repo) {
      const hosts = JSON.parse(env.HOST_RINGS) as Record<string, string>;
      route = { kind: "ring", ring: hosts[url.hostname] ?? env.DEFAULT_RING, repo, arch };
    } else route = await json<Route>(env, `routes/${arch}/${repo}/${database[1]}.json`);
    if (route.repo !== repo || route.arch !== arch) throw new HttpError(404, "Alias mismatch");
    const build = route.kind === "ring" ? await buildForRing(env, arch, route.ring!) : await json<Build>(env, `builds/${route.build}.json`);
    const target = build.repositories[repo]?.[database[2] + (database[3] ?? "")];
    if (!target) throw new HttpError(404, "Repository not found");
    return serve(request, env, target, route.kind === "build");
  }
  const packageFile = file.endsWith(".sig") ? file.slice(0, -4) : file;
  if (!/^[A-Za-z0-9_+.~:@-]+\.pkg\.tar\.[A-Za-z0-9]+$/.test(packageFile)) throw new HttpError(404, "Not found");
  const artifact = await json<{ archive_key: string; signature_key: string }>(env, `filenames/${arch}/${repo}/${packageFile}.json`);
  const target = file.endsWith(".sig") ? artifact.signature_key : artifact.archive_key;
  // Redirects converge every ring on one immutable URL; Range is retained by clients.
  return new Response(null, { status: 307, headers: {
    "Location": new URL("/" + safe(target).split("/").map(encodeURIComponent).join("/"), url).toString(),
    "Cache-Control": "public, max-age=31536000, immutable",
  } });
}

export default {
  async fetch(request, env): Promise<Response> {
    try { return await handle(request, env); }
    catch (error) {
      if (error instanceof HttpError) return new Response(request.method === "HEAD" ? null : error.message, {
        status: error.status, headers: { "Cache-Control": "no-store" },
      });
      console.error(JSON.stringify({ event: "mirror_request_failed", error: error instanceof Error ? error.message : "Unknown error" }));
      return new Response("Mirror temporarily unavailable", { status: 503, headers: { "Cache-Control": "no-store" } });
    }
  },
} satisfies ExportedHandler<Env>;
