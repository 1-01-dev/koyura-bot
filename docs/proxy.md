# Proxy and WireGuard (outbound route)

**Admin → Proxy** decides how the engine leaves the machine. It exists because
some sites refuse a datacentre address outright: Reddit answers

```
ERROR: [Reddit] ... Your IP address is unable to access the Reddit API
ERROR: HTTP Error 403: Blocked
```

and a fresh cookie file does not change that — the site is refusing *the IP*,
not the session. Routing the engine through another address does.

## The connections

The engine no longer has one route: it has a **list of connections**, tried in
the order the page shows them. Each one is a row in the panel's
`proxy_connections` table:

| Column | Meaning |
| --- | --- |
| `id` | Primary key; also the name of the secret file. |
| `name` | What the list shows you. |
| `kind` | `socks5`, `socks5h`, `http`, `https` or `wireguard`. For a proxy the URL's scheme *is* the kind. |
| `url` | `scheme://host:port`, **never** with a login in it. |
| `username` | The non-secret half of the login. |
| `password_ref` | `secrets/<id>.txt` — a reference, never the password. |
| `config_path` | For `wireguard`, the tunnel's `wg0.conf`. |
| `assignment_mode` | `all`, `selected`, `except`. |
| `services` | JSON array of service names for the two list modes. |
| `enabled` | An off row is never dialled but keeps its place. |
| `position` | 0-based order, kept dense. |
| `status` | `ok`, `fail` or `unknown` — the last check's verdict. |
| `last_test_at`, `last_test_result` | When the check ran and what it said (the external IP, or a redacted error). |
| `created_at` | When the row was added. |

For a service, the engine walks the list and dials **the first** row that is
`enabled`, whose rule covers the service and whose `status` is not `fail`. If
only failed rows qualify, it dials the first of those — the last chance beats a
guaranteed refusal. No qualifying row means a direct connection, exactly as
before this page existed. The list is cached for 45 s, so a change reaches the
next job within a minute.

## Where the password lives

Not in the database. The password is written to
`/var/lib/koyura2/egress/secrets/<id>.txt` with mode `0600` (owner `koyura2`),
and the row keeps only the reference. The engine mounts that tree read-only and
pastes the credential into the URL when it dials, so the password never travels
over `GET /api/egress` and never reaches a page, a log line or a JSON response.
Deleting a connection deletes its file; clearing the username removes it.

The engine reads the file on every dial, so a rotated password applies to the
next job without a restart.

## The three shapes of a check

`POST /api/egress/test/<id>` runs `curl --proxy … https://api.ipify.org`
(10 s timeout) and stores the verdict:

* **ok** — the answer was a bare IP; `last_test_result` is that address;
* **fail** — curl failed, or the answer was not an IP; the error text is kept
  with the proxy's password taken out of it;
* **a tunnel that is not up** — `fail`, with the watcher's own message from
  `status.json`.

A `fail` row is still in the list (and still the last chance when nothing else
qualifies); it is simply skipped while a healthy one covers the same service.

## How the engine finds the panel

The rule lives in the panel, so the engine has to know where the panel is. It
reads two names, in this order:

| Variable | Notes |
| --- | --- |
| `KOYURA_DASHBOARD_URL` | The engine's own name. Wins when both are set. |
| `DASHBOARD_URL` | The name the bot and every native `/etc/koyura/koyura.env` already uses — what a native install normally relies on. |

Neither set means `http://dashboard:8080`, the compose service address. On a
native install that host does not exist, so a panel listening on
`127.0.0.1:8090` must be named in `DASHBOARD_URL`; otherwise the engine cannot
read the rule at all.

Startup names the address it uses, in one line:

```text
INFO koyura.engine.egress: Egress panel: http://127.0.0.1:8090 (from DASHBOARD_URL)
```

When the panel cannot be read, the engine keeps the last good rule (or `off`)
and — after three unreadable polls in a row — says so, once per three:

```text
WARNING koyura.engine.egress: Egress panel http://127.0.0.1:8090 could not be
read 3 times in a row (Connection refused); the engine keeps using off. Check
that the panel is running and that KOYURA_DASHBOARD_URL / DASHBOARD_URL names it.
```

A list that "does nothing" while `GET /api/egress` on the engine still answers
`{"connections": [], "test_interval": 0}` is almost always that line: the
engine is asking the wrong host. Fix the variable, not the proxy.

The rule is applied per job and per metadata lookup — the preview
(`POST /api/info`) leaves through the same address the download would, so a page
that only renders from the right IP is not refused on its card.

## Manual proxy

The URL must start with one of:

```
socks5://    socks5h://    http://    https://
```

`user:pass@` is accepted before the host
(`socks5h://user:secret@10.0.0.5:1080`). A path, query or fragment is refused —
it is usually how a *target* URL gets pasted into the proxy field, which would
never work.

`socks5h://` is the one to prefer for this problem: it lets the **proxy** resolve
the site's name, so a blocked DNS answer on this server stops mattering.

**Check** sends one request to `https://api.ipify.org` through the URL
(10 s timeout, server-side) and prints the external IP, or the error text. Use
it before saving a proxy: a typo and a dead proxy both look like "the download
failed" until you do.

The password is written to `EGRESS_PATH/secrets/<id>.txt` (mode `0600`) and is
**never rendered back**: the form shows `socks5h://***:***@host:port`, and
leaving the field blank on a later save keeps the stored secret. Pasting a URL
that still carries `user:pass@` is accepted — the login is split out before the
row is written, so it never lands in the `url` column.

## WireGuard

Upload a `.conf` (≤ 64 KB) and press **Apply**. The file must carry:

* an `[Interface]` section with `PrivateKey` and `Address`;
* a `[Peer]` section with `Endpoint`.

The panel only writes the file — `/var/lib/koyura2/egress/wg0.conf`, mode
`0600`. A root-owned watcher on the host (`/usr/local/bin/koyura-egress`, started
by `koyura-egress.path` — outside this repository's containers) applies the
tunnel as soon as the file changes, and writes the result to
`/var/lib/koyura2/egress/status.json`:

```json
{
  "state": "up",
  "egress_ip": "1.2.3.4",
  "error": null,
  "socks": "10.8.0.2:11080",
  "updated_at": "2026-01-31T12:00:00Z"
}
```

`state` is `up`, `down` or `error`. The page shows the state, the external IP,
the SOCKS address, the timestamp and, for `error`, the watcher's message.
**Refresh status** re-reads the file; the page never caches it.

A config with fake keys reaches the watcher and comes back as `error` — that is
the expected result of testing with dummy values, and it is a report, not a
crash. The `wireguard` connection has no URL of its own: the engine reads the
tunnel's SOCKS address from `status.json`, so a dead tunnel means **direct**
(or the next connection in the list), never a half-configured proxy.

**One tunnel, one row.** The watcher owns a single interface and the config is a
single file, so a second `kind=wireguard` connection is refused with
`OnlyOneWireGuard`.

**Disconnect** deletes the config; the watcher tears the tunnel down and the
engine returns to a direct connection.

## Apply it to: all, some, or all but some

| Option | Meaning |
| --- | --- |
| **All services** | Every download goes through the route. |
| **Selected services** | Only the ticked ones do; everything else is direct. |
| **All except selected** | Everything except the ticked ones goes through it. |

The list is the same catalogue **Admin → Cookies** offers — yt-dlp's own
extractor names — so a name here is a name the engine can compute for a URL.
Which service a link belongs to is decided by the same extractor table the
cookie lookup uses: `https://www.reddit.com/r/x` is `reddit` for both features.

For a datacentre block the usual setting is **Selected services** with the sites
that refuse the server ticked (`reddit`, and whatever else reports `blocked`).
YouTube keeps the direct connection, which is faster and avoids burning proxy
bandwidth on files that never needed it.

## Why a download says "blocked"

The engine classifies the failure before it retries anything:

* markers such as `unable to access the reddit api`, `http error 403: blocked`,
  `blocked`, `429`, `too many requests` or `captcha` mean the **address** was
  refused. The job ends with `error_code: "service_blocked"` and the bot says
  *"Reddit blocks the server's IP address — a proxy is needed. Cookies will not
  help."* — it does not try the next cookie jar, because another jar answers the
  same 403.
* the genuine sign-in markers (`sign in to confirm`, `login required`,
  `account authentication is required` — Reddit's preview line — `this video is
  private`, `age-restricted`, and a plain `http error 403` with none of the
  blocked words above) stay `cookies_required`: *"add a cookie file"*.

**Admin → Cookies** shows the same distinction. A check that ends in
`blocked · needs another IP` is a note about the server, not about the file —
re-uploading the jar will not clear it. Fix it on this page, then re-run the
check.

The preview (`POST /api/info`) answers with the same two codes, because the
first request a user makes is the metadata lookup: a blocked address returns
`code: "service_blocked"` there too, and a service whose jars were all refused
(or that has none) returns `code: "cookies_required"` with the sentence the bot
shows instead of the raw yt-dlp line. The preview never burns a second jar on a
blocked address.

## Checking the external IP without the panel

```bash
curl --silent --proxy 'socks5h://10.8.0.2:11080' https://api.ipify.org; echo
curl --silent https://api.ipify.org; echo          # direct, for comparison
```

If the two differ, the route is live. If the proxied one prints your own
address, the proxy is not being used; if it times out, the tunnel is down (see
`status.json`).

## Limits worth knowing

* **One outbound IP for every service in the rule.** Two services in the same
  rule share one address; a site that rate-limits per IP is rate-limited across
  all of them.
* **The route applies to yt-dlp, not to the panel.** Uploads, the dashboard's
  own HTTP and Telegram traffic are unaffected.
* **A proxy costs speed.** A slow tunnel slows the download; the estimate in the
  progress card cannot know that.
* **Nothing is retried through the direct connection.** If a job is in scope for
  the rule and the proxy fails, the job fails — it does not silently fall back to
  your own IP, because that is the address the site blocked.
* **The WireGuard private key never leaves the file.** The panel writes the
  bytes it was given and never reads them back; no page, log line or JSON
  response contains the key.
* **One tunnel at a time.** The watcher owns a single interface (`wg0`); a
  second config replaces the first.

## The endpoint the engine reads

`GET /api/egress` (internal, `X-Internal-Token`) answers:

```json
{
  "connections": [
    {
      "id": 1,
      "name": "Primary",
      "kind": "socks5h",
      "url": "socks5h://10.0.0.5:1080",
      "assignment": {"mode": "selected", "services": ["reddit"]},
      "enabled": true,
      "status": "unknown"
    }
  ],
  "test_interval": 0
}
```

No username, no password, no `password_ref`, no `config_path` — the engine gets
the URL and reads the credential from the shared secrets tree itself.
`test_interval` is the re-test period in minutes (`0` = never); it is stored for
the page to schedule against, not enforced by the engine.

## Upgrading from the single-rule panel

Before the list, the page stored one rule as `egress.mode`, `egress.proxy_url`,
`egress.assignment` and `egress.services`. On the first start the panel migrates
it once: `mode=manual` becomes a connection named `Default` (kind from the URL's
scheme, login split into `username` + the secret file), `mode=wireguard` becomes
a `wireguard` row when `wg0.conf` exists, and every other key is ignored. A
marker in `settings` (`egress.migrated`) makes the next start a no-op, so a row
you delete does not come back. Until the page itself is replaced, the old form
keeps editing that one `Default` connection.

## Environment

| Variable | Default | What it does |
| --- | --- | --- |
| `KOYURA_DASHBOARD_URL` | *(empty)* | Panel address the engine asks for the list. Wins over `DASHBOARD_URL`. |
| `DASHBOARD_URL` | `http://dashboard:8080` | Panel address, shared with the bot. Native runs must set it (`http://127.0.0.1:8090`). |
| `EGRESS_PATH` | `/var/lib/koyura2/egress` | Where `wg0.conf` and `secrets/<id>.txt` are written and `status.json` is read. Shared by the panel and the engine, and mounted by the host watcher. |

The connections live in the panel's database (`proxy_connections`), not in the
environment: they are an operator decision, changed from the page. The engine
reads them from `GET /api/egress` with the internal token.
