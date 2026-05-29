/**
 * Cloudflare Worker: latest report collector + original response format
 *
 * Bindings:
 *  - env.AUTH_TOKEN   (secret / env var)
 *  - env.BAND_USAGE_KV      (KV namespace binding)
 *
 * Routes:
 *  - POST /report    store latest uploaded usage
 *  - POST /reset     optional: force zero usage on worker side for a node
 *  - GET  /          return plain text: upload=...;download=...;total=...;expire=...;reset_day=...
 *
 * Query params:
 *  - ?node=NODE_ID   choose node (default: "default")
 */

const LATEST_PREFIX = "netquota:latest:";
const DEFAULT_NODE = "default";
const WRITE_TTL_SECONDS = 180 * 24 * 3600;

export default {
  async fetch(request, env, ctx) {
    try {
      if (!env.AUTH_TOKEN) return text("Missing env var: AUTH_TOKEN", 500);
      if (!env.BAND_USAGE_KV) return text("Missing KV binding: BAND_USAGE_KV", 500);

      const url = new URL(request.url);
      const pathname = url.pathname.replace(/\/+$/, "") || "/";

      if (request.method === "POST" && pathname === "/report") {
        return handleReport(request, env, ctx);
      }

      if (request.method === "POST" && pathname === "/reset") {
        return handleReset(request, env, ctx);
      }

      if (request.method === "GET" && pathname === "/") {
        return handleRead(url, env);
      }

      return text("Not found", 404);
    } catch (e) {
      return text(`Worker error: ${e?.message || String(e)}`, 500);
    }
  },
};

async function handleReport(request, env, ctx) {
  assertAuthorized(request, env);
  const body = await readJson(request);

  const nodeId = normalizeNode(body.node_id || DEFAULT_NODE);
  const payload = normalizePayload(body, nodeId);
  const key = kvKey(nodeId);

  ctx.waitUntil(env.BAND_USAGE_KV.put(key, JSON.stringify(payload), { expirationTtl: WRITE_TTL_SECONDS }));

  return json({ ok: true, node_id: nodeId, stored_at: new Date().toISOString() }, 200);
}

async function handleReset(request, env, ctx) {
  assertAuthorized(request, env);
  const body = await readJson(request).catch(() => ({}));
  const nodeId = normalizeNode(body.node_id || DEFAULT_NODE);
  const resetDay = normalizeInt(body.reset_day, 1);
  const expire = normalizeInt(body.expire, 0);
  const payload = {
    node_id: nodeId,
    upload: 0,
    download: 0,
    total: normalizeInt(body.total, 0),
    expire,
    reset_day: resetDay,
    ts: new Date().toISOString(),
    hostname: typeof body.hostname === "string" ? body.hostname : undefined,
    interfaces: Array.isArray(body.interfaces) ? body.interfaces.filter((x) => typeof x === "string") : undefined,
  };

  ctx.waitUntil(env.BAND_USAGE_KV.put(kvKey(nodeId), JSON.stringify(payload), { expirationTtl: WRITE_TTL_SECONDS }));
  return json({ ok: true, node_id: nodeId, reset: true }, 200);
}

async function handleRead(url, env) {
  const nodeId = normalizeNode(url.searchParams.get("node") || DEFAULT_NODE);
  const stored = await env.BAND_USAGE_KV.get(kvKey(nodeId));
  if (!stored) {
    return new Response("upload=0;download=0;total=0;expire=0;reset_day=0", {
      status: 200,
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "cache-control": "no-store",
        "x-cache": "MISS",
      },
    });
  }

  const payload = JSON.parse(stored);
  const body = formatOriginal(payload);
  return new Response(body, {
    status: 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      "x-cache": "HIT",
      "x-node-id": nodeId,
      "x-report-ts": payload.ts || "",
    },
  });
}

function kvKey(nodeId) {
  return `${LATEST_PREFIX}${nodeId}`;
}

function normalizePayload(body, nodeId) {
  return {
    node_id: nodeId,
    upload: normalizeInt(body.upload, 0),
    download: normalizeInt(body.download, 0),
    total: normalizeInt(body.total, 0),
    expire: normalizeInt(body.expire, 0),
    reset_day: normalizeInt(body.reset_day, 0),
    ts: typeof body.ts === "string" && body.ts ? body.ts : new Date().toISOString(),
    hostname: typeof body.hostname === "string" ? body.hostname : undefined,
    interfaces: Array.isArray(body.interfaces) ? body.interfaces.filter((x) => typeof x === "string") : undefined,
  };
}

function formatOriginal(payload) {
  const upload = normalizeInt(payload.upload, 0);
  const download = normalizeInt(payload.download, 0);
  const total = normalizeInt(payload.total, 0);
  const expire = normalizeInt(payload.expire, 0);
  const reset_day = normalizeInt(payload.reset_day, 0);
  return `upload=${upload};download=${download};total=${total};expire=${expire};reset_day=${reset_day}`;
}

function assertAuthorized(request, env) {
  const auth = request.headers.get("authorization") || "";
  if (auth !== `Bearer ${env.AUTH_TOKEN}`) {
    throw new HttpError("Unauthorized", 401);
  }
}

function normalizeNode(value) {
  if (typeof value !== "string") return DEFAULT_NODE;
  const cleaned = value.trim();
  if (!cleaned) return DEFAULT_NODE;
  return cleaned.replace(/[^a-zA-Z0-9._:-]/g, "_").slice(0, 128);
}

function normalizeInt(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

async function readJson(request) {
  const textBody = await request.text();
  if (!textBody) return {};
  try {
    return JSON.parse(textBody);
  } catch {
    throw new HttpError("Invalid JSON body", 400);
  }
}

class HttpError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function text(msg, status = 200) {
  return new Response(msg, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
