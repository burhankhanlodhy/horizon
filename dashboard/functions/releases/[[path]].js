// Desktop installers and latest.json at app.contextshrink.com/releases/*,
// streamed from the private R2 bucket bound as RELEASES (dashboard/wrangler.toml).
// The bucket is never public: this function is the only way in. It serves single
// flat file names, plus the three Code-tab tools under claude-code-tools/<version>/
// (npx contextshrink-claude), the engine zip and its manifest under
// engine/<version>/ (npx contextshrink-claude and contextshrink-codex), and nothing
// else. Uploads come from desktop/publish-installer.ps1.

const NAME = /^[A-Za-z0-9][A-Za-z0-9._+-]*$/;
const TOOLS =
  /^claude-code-tools\/\d+\.\d+\.\d+\/(claude-code-launcher\.exe|contextshrink-codetab\.exe|manifest\.json)$/;
const ENGINE =
  /^engine\/\d+\.\d+\.\d+\/(contextshrink-engine-windows-x86_64\.zip|engine-manifest\.json)$/;

const TYPES = {
  json: "application/json",
  zip: "application/zip",
  exe: "application/vnd.microsoft.portable-executable",
  dmg: "application/x-apple-diskimage",
  deb: "application/vnd.debian.binary-package",
  rpm: "application/x-rpm",
};

function contentType(key) {
  const ext = key.slice(key.lastIndexOf(".") + 1).toLowerCase();
  return TYPES[ext] || "application/octet-stream";
}

function text(status, body, extra = {}) {
  return new Response(body, { status, headers: { "Content-Type": "text/plain", ...extra } });
}

export async function onRequest({ request, env, params }) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return text(405, "Method Not Allowed", { Allow: "GET, HEAD" });
  }
  const parts = Array.isArray(params.path) ? params.path : [params.path];
  const joined = parts.join("/");
  let key = "";
  if (parts.length === 1 && NAME.test(joined)) key = joined;
  else if (TOOLS.test(joined)) key = joined;
  else if (ENGINE.test(joined)) key = joined;
  if (!key) return text(404, "Not Found");

  let object;
  try {
    object =
      request.method === "HEAD"
        ? await env.RELEASES.head(key)
        : await env.RELEASES.get(key, { range: request.headers });
  } catch {
    // R2 rejects a Range outside the file.
    return text(416, "Range Not Satisfiable");
  }
  if (!object) return text(404, "Not Found");

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", contentType(key));
  headers.set("ETag", object.httpEtag);
  headers.set("Accept-Ranges", "bytes");
  if (key === "latest.json" || key === "updater.json") {
    headers.set("Cache-Control", "no-cache");
  } else {
    // Installer names carry the version, so a name never changes content.
    headers.set("Cache-Control", "public, max-age=31536000, immutable");
    headers.set("Content-Disposition", "attachment");
  }

  if (request.method === "HEAD") {
    headers.set("Content-Length", String(object.size));
    return new Response(null, { headers });
  }

  // Resumed downloads: answer a Range request with just that part.
  const range = object.range;
  if (range && request.headers.has("Range")) {
    let offset;
    let length;
    if ("suffix" in range) {
      length = Math.min(range.suffix, object.size);
      offset = object.size - length;
    } else {
      offset = range.offset ?? 0;
      length = range.length ?? object.size - offset;
    }
    headers.set("Content-Range", `bytes ${offset}-${offset + length - 1}/${object.size}`);
    headers.set("Content-Length", String(length));
    return new Response(object.body, { status: 206, headers });
  }

  headers.set("Content-Length", String(object.size));
  return new Response(object.body, { headers });
}
