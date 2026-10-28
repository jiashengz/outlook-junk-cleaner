// Cloudflare Worker: the same Junk cleaner, run the moment mail lands in Junk.
//
// Microsoft Graph calls this Worker's hidden URL whenever a message is created in
// the Junk Email folder; the Worker then does what cleaner.py does: messages in Junk
// that match the rules are marked as read and moved to Deleted Items (or deleted
// permanently for permanently_delete_domains). Nothing outside Junk is touched.
//
// Every 15 minutes a cron run renews the Graph subscription (it expires after a few
// days) and sweeps Junk once, in case a notification was missed.
//
// Stored in the KV namespace STATE (uploaded by deploy.sh):
//   rules          the rules from rules.toml, as JSON
//   refresh_token  the Worker's own sign-in to your mailbox
//   access_token   short-lived token, cached for an hour
//   client_state   random value Graph sends with each notification, so fakes are ignored

const GRAPH = "https://graph.microsoft.com/v1.0";
const TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token";
const SCOPE = "Mail.ReadWrite offline_access";
const RESOURCE = "me/mailFolders('junkemail')/messages";
const SUBSCRIPTION_HOURS = 72;  // Graph allows at most ~7 days for mail
const MAX_MOVES = 15;           // per run: Cloudflare's free plan allows 50 outgoing requests

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.origin + url.pathname !== env.HOOK_URL) return new Response("Not found", { status: 404 });

    // Graph checks the URL when a subscription is created: echo its token back.
    const validation = url.searchParams.get("validationToken");
    if (validation) return new Response(validation, { headers: { "content-type": "text/plain" } });

    // Manual kick after a deploy: subscribe (if needed) and sweep now.
    if (request.method === "GET" && url.searchParams.has("kick")) {
      ctx.waitUntil(scheduledRun(env));
      return new Response("ok\n");
    }
    if (request.method !== "POST") return new Response("Not found", { status: 404 });

    let body;
    try { body = await request.json(); } catch { return new Response(null, { status: 400 }); }
    const state = await env.STATE.get("client_state");
    const genuine = (body.value || []).some(n => state && n.clientState === state);
    // Answer at once (Graph wants a reply within 3 seconds); work continues afterwards.
    if (genuine) ctx.waitUntil(logErrors(sweep(env)));
    return new Response(null, { status: 202 });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(scheduledRun(env));
  },
};

async function scheduledRun(env) {
  await logErrors(keepSubscription(env));
  await logErrors(sweep(env));
}

async function logErrors(promise) {
  try { await promise; } catch (e) { console.error("Run failed:", e.message); }
}

// --------------------------------------------------------------------------
// Matching: the same rules, in the same order, as Rules in cleaner.py
// --------------------------------------------------------------------------

export function senderOf(msg) {
  for (const field of ["from", "sender"]) {
    const ea = (msg[field] || {}).emailAddress || {};
    const addr = (ea.address || "").trim().toLowerCase();
    const name = (ea.name || "").trim();
    if (addr || name) return [addr, name];
  }
  return ["", ""];
}

function inDomains(addr, domains) {
  if (!addr.includes("@")) return null;
  const domain = addr.slice(addr.lastIndexOf("@") + 1);
  return domains.find(d => domain === d || domain.endsWith("." + d)) ?? null;
}

const visible = text => [...text].map(ch => ch < "\x80" || /[\p{L}\p{N}\p{P}\p{S}\p{Z}]/u.test(ch) ? ch
  : "U+" + ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0")).join("");

// rules: the JSON written by prepare.py (lists already lower-cased like cleaner.py does).
export function compileRules(rules) {
  return {
    ...rules,
    from_regex: rules.from_regex.map(p => new RegExp(p, "i")),
    subject_regex: rules.subject_regex.map(p => new RegExp(p, "i")),
  };
}

export function permanent(rules, msg) {
  const d = inDomains(senderOf(msg)[0], rules.permanent_domains);
  return d ? `permanent domain ${d}` : null;
}

export function match(rules, msg) {
  const [addr, name] = senderOf(msg);
  const rawSubject = msg.subject || "";
  const subject = rawSubject.toLowerCase();

  if (addr && rules.senders.includes(addr)) return `sender ${addr}`;
  const d = inDomains(addr, rules.domains);
  if (d) return `domain ${d}`;
  for (const kw of rules.sender_name_contains)
    if (name.toLowerCase().includes(kw)) return `sender name contains '${visible(kw)}'`;
  for (const kw of rules.subject_contains)
    if (subject.includes(kw)) return `subject contains '${kw}'`;
  for (const rx of rules.from_regex)
    if (rx.test(addr)) return `address matches /${rx.source}/`;
  for (const rx of rules.subject_regex)
    if (rx.test(rawSubject)) return `subject matches /${rx.source}/`;
  return null;
}

export function describe(msg) {
  const [addr, name] = senderOf(msg);
  const when = (msg.receivedDateTime || "").slice(0, 16).replace("T", " ");
  let subj = (msg.subject || "(no subject)").replaceAll("\n", " ");
  if (subj.length > 70) subj = subj.slice(0, 67) + "...";
  return `${when} | ${name ? `${name} <${addr}>` : `<${addr}>`} | ${subj}`;
}

// --------------------------------------------------------------------------
// Graph
// --------------------------------------------------------------------------

async function accessToken(env) {
  const cached = await env.STATE.get("access_token", "json");
  if (cached && cached.expires > Date.now() + 120_000) return cached.token;

  const refresh = await env.STATE.get("refresh_token");
  if (!refresh) throw new Error("No sign-in stored. Run: worker/deploy.sh --login");
  const r = await fetch(TOKEN_URL, {
    method: "POST",
    body: new URLSearchParams({ client_id: env.CLIENT_ID, grant_type: "refresh_token",
                                refresh_token: refresh, scope: SCOPE }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(`Sign-in refresh failed (run ./move-junk-to-deleted.sh --login, then worker/deploy.sh --login): ${j.error_description || j.error}`);
  if (j.refresh_token && j.refresh_token !== refresh) await env.STATE.put("refresh_token", j.refresh_token);
  await env.STATE.put("access_token", JSON.stringify({ token: j.access_token, expires: Date.now() + j.expires_in * 1000 }),
                      { expirationTtl: Math.max(60, j.expires_in) });
  return j.access_token;
}

async function graph(token, method, path, body) {
  const r = await fetch(path.startsWith("http") ? path : GRAPH + path, {
    method,
    headers: { Authorization: `Bearer ${token}`, ...(body ? { "content-type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  if (!r.ok) throw new Error(`${method} ${path} -> ${r.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

// --------------------------------------------------------------------------
// Work
// --------------------------------------------------------------------------

async function sweep(env) {
  const stored = await env.STATE.get("rules", "json");
  if (!stored) throw new Error("No rules uploaded. Run: worker/deploy.sh");
  const rules = compileRules(stored);
  const token = await accessToken(env);
  const page = await graph(token, "GET", "/me/mailFolders/junkemail/messages" +
    "?$select=id,subject,from,sender,receivedDateTime&$top=100&$orderby=receivedDateTime desc");
  const msgs = page.value || [];

  let moved = 0, purged = 0, errors = 0;
  for (const m of msgs) {
    if (moved + purged + errors >= MAX_MOVES) break;  // the rest waits for the next run
    let reason = permanent(rules, m);
    if (reason) {
      try {
        await graph(token, "POST", `/me/messages/${m.id}/permanentDelete`);
        purged++;
        console.log(`Permanently deleted  (${reason}) ${describe(m)}`);
        continue;
      } catch (e) {
        console.warn(`Permanent delete failed, moving instead: ${e.message}`);
      }
    } else {
      reason = match(rules, m);
    }
    if (!reason) continue;
    try {
      // Mark read AFTER the move, on the moved copy (same as cleaner.py).
      const copy = await graph(token, "POST", `/me/messages/${m.id}/move`, { destinationId: "deleteditems" });
      await graph(token, "PATCH", `/me/messages/${copy.id}`, { isRead: true });
      moved++;
      console.log(`Moved to Deleted Items  (${reason}) ${describe(m)}`);
    } catch (e) {
      errors++;  // often just another run that moved it first
      console.error(`Failed on ${describe(m)}: ${e.message}`);
    }
  }
  if (moved || purged || errors)
    console.log(`Run done: ${msgs.length} in Junk, ${moved} moved to Deleted Items, ` +
                `${purged} permanently deleted, ${errors} errors`);
}

async function keepSubscription(env) {
  const token = await accessToken(env);
  const expirationDateTime = new Date(Date.now() + SUBSCRIPTION_HOURS * 3600_000).toISOString();
  const subs = (await graph(token, "GET", "/subscriptions")).value || [];
  const mine = subs.find(s => s.notificationUrl === env.HOOK_URL);
  if (mine) {
    await graph(token, "PATCH", `/subscriptions/${mine.id}`, { expirationDateTime });
    return;
  }
  let clientState = await env.STATE.get("client_state");
  if (!clientState) {
    clientState = crypto.randomUUID();
    await env.STATE.put("client_state", clientState);
  }
  const s = await graph(token, "POST", "/subscriptions", {
    changeType: "created", notificationUrl: env.HOOK_URL, resource: RESOURCE,
    expirationDateTime, clientState,
  });
  console.log(`Subscribed to new Junk mail (subscription ${s.id}, until ${s.expirationDateTime})`);
}
