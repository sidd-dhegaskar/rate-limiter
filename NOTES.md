# Redis Concepts Log — Rate Limiter Project

Running notes from building the rate limiter, in the order we learned them.

## Technical Recap

**What was built**: a per-identity API rate limiter, first proven by hand
against raw Redis, then implemented as a FastAPI service
(`main.py` + `limiter.py`) backed by a single atomic Lua script.

**The core problem, in one line**: naive rate-limit logic splits one
logical decision ("should this request be allowed?") into multiple
separate Redis round-trips, and every gap between those round-trips is
either a crash-safety hole or an exploitable burst window.

**Two algorithms, two distinct flaws, one shared fix pattern**:

| | Fixed window (`INCR`+`EXPIRE`) | Sliding window log (`ZSET`) |
|---|---|---|
| Storage | one integer per window-bucket key | one sorted-set entry per request |
| Flaw found | TOCTOU gap: crash between `INCR` and `EXPIRE` leaves a key with a count but no TTL, stuck forever | edge-burst: fixed clock-aligned buckets let 2x the limit through around a boundary |
| Root cause | check-then-act split across 2+ network round-trips | no root cause in the atomic version — the flaw is *structural* to fixed windows, not a race |
| Fix | bundle `INCR`+`EXPIRE` into one Lua script | replace fixed buckets with a rolling log (`ZADD`/`ZREMRANGEBYSCORE`/`ZCARD`), also bundled into one Lua script |

**The one idea underlying every fix in this project**: Redis executes
commands single-threaded, so a Lua script sent via `EVAL`/`EVALSHA` runs
as one indivisible unit — no other client's command can interleave
mid-script, and it either completes fully or doesn't run at all. Every
race condition found in this project (the `INCR`/`EXPIRE` gap, and later
the `ZREMRANGEBYSCORE`/`ZCARD`/`ZADD` check-then-act sequence) was closed
the same way: move the whole decision server-side into one script instead
of coordinating it from application code across multiple round-trips.

**System shape, end to end**: `curl` → FastAPI route (`main.py`) reads the
caller's identity from an `X-User-Id` header → calls
`SlidingWindowLimiter.allow()` (`limiter.py`) → one `EVALSHA` round-trip
to Redis, which atomically trims expired entries, checks the count,
conditionally logs the new request, and computes remaining-count +
reset-time, all in a single Lua script → the allow/deny bit and metadata
come back as one RESP array → FastAPI turns that into a `200`+headers or
a `429`+`Retry-After`.

**Why this is genuinely production-shaped, not a simplified toy**: unique
UUID-suffixed ZSET members, `SCRIPT LOAD`/`EVALSHA` script caching instead
of resending Lua source per call, per-identity keying (not global state),
and `X-RateLimit-*`/`Retry-After` headers are all patterns used in real
systems (Stripe, Cloudflare, GitHub write publicly about variations of
this). The one place a real trillion-request-scale system (e.g. Cognito)
diverges is *algorithm choice under memory pressure* — swapping the exact
log for a cheaper two-integer approximation — and *topology* (sharding,
edge-local approximation) — not the atomicity technique itself, which is
the same Lua-script pattern at any scale.

**Verified live, not just read about**: every mechanism above was
triggered and observed firsthand — the stuck key from a simulated crash,
the 2x-limit burst across a fixed-window boundary, the ZSET member
collision undercount, the atomic script correctly rejecting a 4th request
over a 3-limit, per-user isolation between two independent identities,
live-counting `Retry-After` headers, and full window recovery through
real HTTP after waiting out the window.

## 1. RESP — the protocol Redis speaks

Redis doesn't speak HTTP or JSON. It speaks **RESP** (REdis Serialization
Protocol) over raw TCP — a tiny, typeable text protocol. Every client
library (redis-py, ioredis, etc.) is just:

1. Serializing your function call into RESP
2. Writing those bytes to a socket
3. Reading the RESP reply back and parsing it into a native type

Core RESP reply types:

| Prefix | Type          | Example                              |
|--------|---------------|---------------------------------------|
| `+`    | Simple String | `+OK\r\n`                             |
| `-`    | Error         | `-ERR unknown command\r\n`            |
| `:`    | Integer       | `:1\r\n`                              |
| `$`    | Bulk String   | `$3\r\nfoo\r\n` (length-prefixed)     |
| `*`    | Array         | `*2\r\n$3\r\nfoo\r\n$3\r\nbar\r\n`    |

Every line ends in `\r\n` (CRLF), not just `\n`.

**Two ways to send commands, proven by hand:**

- **Inline mode** (legacy, human convenience): typing plain text like
  `PING` or `INCR mykey` directly into a raw socket (`nc localhost 6379`).
  Redis parses it loosely.
- **Multi-bulk array** (what real clients actually send): every command is
  sent as a RESP array of bulk strings. `INCR mykey` on the wire is:

  ```
  *2\r\n$4\r\nINCR\r\n$5\r\nmykey\r\n
  ```

  `*2` = array of 2 elements · `$4\r\nINCR\r\n` = 4-byte string "INCR" ·
  `$5\r\nmykey\r\n` = 5-byte string "mykey".

Verified this by hand:

```bash
printf '*2\r\n$4\r\nINCR\r\n$5\r\nmykey\r\n' | nc -w1 localhost 6379 | xxd | head -5
```

Reply came back as raw bytes `3a 33 0d 0a` = `:3\r\n` = RESP Integer, value
3. Confirmed a client library does nothing magic — same command, same
reply, whether typed by hand or sent by a library.

## 2. Keys are just opaque strings

`ratelimit:user:42` is **not** a Redis keyword or special syntax — Redis
has zero understanding of it. It's just a string used as a lookup key.
The colon-separated shape (`noun:id:field`) is a **human convention** for
faking namespaces, useful for tools like `SCAN --pattern 'ratelimit:*'`,
but Redis itself never parses it.

## 3. `TTL` and key expiry

- `TTL key` returns:
  - a positive integer = seconds remaining before auto-expiry
  - `-1` = key exists but has **no expiry** (lives forever)
  - `-2` = key does not exist
- `EXPIRE key seconds` attaches a TTL to an existing key. Returns `1` if
  set successfully, `0` if the key didn't exist.
- When a TTL hits zero, Redis deletes the key automatically — no manual
  `DEL` needed.

## 4. Auto-vivification (create-if-missing)

`INCR` on a key that doesn't exist treats the missing key as if its value
were `0`, then increments — creating the key and setting it to `1` in one
atomic step. This isn't unique to `INCR`; the same "spring into existence
on first write" pattern applies to `APPEND` (missing → empty string),
`LPUSH`/`RPUSH` (missing → empty list), `SADD` (missing → empty set), etc.

This is also why `INCR` alone is safe: it can never race with itself. The
danger only appears once application code bolts a **second**, unrelated
Redis call onto it.

**Point-wise recap — auto-vivification + TOCTOU, why they combine into the
bug in §5:**

- Redis keys have no TTL by default. A normally-created key's TTL is
  `-1` — it never expires on its own.
- `INCR` auto-vivifies: on a missing key it treats the value as `0`,
  increments to `1`, creates the key, and that key's TTL starts at `-1`.
- `INCR` itself is atomic and race-safe: two simultaneous `INCR`s on the
  same key can never both return `1` — Redis serializes them (`1` then
  `2`, in some order, never a duplicate).
- A rate limiter needs the counter to *expire* — e.g. 5 requests per 60s
  means `INCR key` followed by `EXPIRE key 60`.
- The naive fix only sets the TTL on the first request:

  ```
  count = INCR(key)
  if count == 1:
      EXPIRE(key, 60)
  if count > 5:
      reject
  ```

- The bug: `INCR` and `EXPIRE` are two separate Redis calls with
  application logic (`count == 1`) sitting in the gap between them. If
  the server crashes after `INCR` but before `EXPIRE`, the key is left
  at `count = 1`, `TTL = -1` — permanently unexpiring. Every future
  request just does `INCR → 2 → 3 → 4 → ...`; `count == 1` is never true
  again, so `EXPIRE` never runs, and the key is stuck forever.
- Two concurrent requests make the race concrete: Request A gets `1`
  (the one that *would* call `EXPIRE`), Request B gets `2` (which skips
  `EXPIRE` entirely). If A crashes before its `EXPIRE`, B cannot rescue
  it — B never saw `count == 1`.
- This is the general TOCTOU/timing-gap shape: one logical operation
  ("increment, and if this was the first write, set a TTL") gets split
  into multiple round-trips with app logic between them, and any
  crash/network gap in that space leaves the system in an inconsistent
  state.
- The fix (detailed in §6): push the whole `INCR` → `if count == 1` →
  `EXPIRE` sequence server-side into one atomic Lua script, so there is
  no crash/network gap between the increment and the conditional expiry.

## 5. The TOCTOU bug in fixed-window rate limiting

Naive fixed-window logic:

```
count = INCR(key)
if count == 1:
    EXPIRE(key, window_seconds)
if count > limit:
    reject
```

This is **two separate round-trips** to Redis, with app logic
(`if count == 1`) sitting between them. If the process crashes, times out,
or gets killed after `INCR` but before `EXPIRE`, the key is left with a
count but `TTL == -1` — it never expires. That user is now stuck rate
limited (or never limited) forever, and nobody has to write a bug for
this to happen — it's a timing gap inherent to splitting one logical
operation into two network calls.

**Reproduced live:**

```bash
redis-cli DEL ratelimit:user:99
redis-cli INCR ratelimit:user:99   # simulates "Request A", then crash
redis-cli TTL ratelimit:user:99    # -> -1, stuck forever
```

```
TWO REQUESTS HIT SERVER AT SAME TIME
                    |
        ┌───────────┴───────────┐
        │                       │
   Request A                Request B
        │                       │
   INCR key                 INCR key
        │                       │
   ← returns 1             ← returns 2
        │                       │
   count == 1?             count == 1?
      YES                      NO
        │                       │
   [about to EXPIRE]       [skips EXPIRE]
        │
      💥 CRASH
   (before EXPIRE runs)
        │
        ▼
   key = count:1
   TTL  = -1  ← stuck forever!
        │
        ▼
every future request just keeps incrementing
        │
        ▼
   count: 2 → 3 → 4 → 5 ...
   TTL still = -1
        │
        ▼
   user rate-limited forever 🔴
```

## 6. The fix: atomicity via Lua scripting (`EVAL`)

Redis executes commands single-threaded, and a Lua script run via `EVAL`
executes as **one indivisible unit** on the server — no other client's
command can interleave partway through it, and it either fully completes
or doesn't run at all (no partial-execution state ever becomes visible).

```lua
local count = redis.call("INCR", KEYS[1])
if count == 1 then
    redis.call("EXPIRE", KEYS[1], ARGV[1])
end
return count
```

Run via:

```bash
redis-cli EVAL "local count = redis.call('INCR', KEYS[1]) if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end return count" 1 ratelimit:user:99 10
```

`EVAL` syntax: `EVAL "<script>" numkeys key1 key2 ... arg1 arg2 ...`

- `numkeys` tells Redis how many of the following args are **keys** vs.
  plain **values** — this lets Redis Cluster route the script to the
  right shard without parsing the Lua body.
- Inside the script, keys become `KEYS[1]`, `KEYS[2]`, ... and plain args
  become `ARGV[1]`, `ARGV[2]`, ...

Verified live: `TTL` went `3 → 1 → -2` with no `-1` ever observed — the
increment and the expiry happened as one atomic step, closing the gap a
crash could land in.

```
─────────────────────────────────────────
THE FIX: Lua script (atomic, one trip)
─────────────────────────────────────────

   Request hits server
        │
        ▼
   [INCR + EXPIRE as one atomic block]
        │
      Redis
        │
   ← count returned
        │
   count > limit?
   ┌────┴────┐
  YES        NO
   │          │
 reject     allow ✅
```

(Related but distinct tool for atomicity: Redis also supports
`MULTI`/`EXEC` transactions and `SET key val EX seconds NX` — the latter
atomically creates+expires in one call but only helps on the *first*
write to a key, since plain `INCR` has no `EX` flag. Lua is the general
fix.)

## 7. Fixed-window bucketing and the edge-burst flaw

A fixed window maps a timestamp to a window number by integer division:

```
window_number = floor(current_epoch_seconds / window_size)
```

Each window number gets its own independent key, e.g.
`ratelimit:user:42:<window_number>`. The flaw: a client can spend its
whole budget in the last instant of window N, then immediately spend its
whole budget again in the first instant of window N+1 — because the two
windows are backed by two completely unrelated keys with no shared
memory. In the worst case, 2x the limit gets through in under a
millisecond of real wall-clock time.

**Reproduced live** with a 3-per-5s limit: filled window `357561759` to
its cap of 3 (`INCR` → 1, 2, 3), then immediately filled window
`357561760` to its cap of 3 as well — 6 requests allowed within seconds,
double the intended limit. This is a **structural** property of fixed
windows, not a race condition — closing the earlier TOCTOU gap with Lua
does nothing to prevent it, because there's no bug here, just a design
that resets completely at arbitrary clock-aligned boundaries.

## 8. The fix: sliding-window log via sorted sets (`ZSET`)

Instead of one counter per fixed bucket, keep a **log of every request's
exact timestamp** in a single Redis key, using a sorted set:

- **score** = the request's timestamp (numeric, used for range queries)
- **member** = a unique string per request (needs uniqueness, not the
  score — see the collision gotcha below)

Three commands:

| Command | Purpose |
|---|---|
| `ZADD key score member` | log a request at a given timestamp |
| `ZREMRANGEBYSCORE key min max` | delete members with score in `[min, max]` — ages out anything older than `now - window` |
| `ZCARD key` | count remaining members — this is the current request count |

Correct per-request order: **trim → check → conditionally add**, not
add-then-check (the latter records over-limit requests you then have to
undo).

**Collision gotcha, found by hand:** using the raw timestamp as *both*
score and member is dangerous. `ZADD` with a member that already exists
just updates its score (no-op if unchanged) rather than adding a new
entry — so two requests landing on the same timestamp silently
undercount. Confirmed: re-`ZADD`ing the same `t` as both score and member
left `ZCARD` unchanged; switching to unique members (`"<timestamp>:<random
suffix>"`, score stays the pure timestamp) correctly counted multiple
requests sharing one timestamp. This is also the real industry pattern —
production implementations typically use millisecond/microsecond
timestamps plus a UUID or request-ID suffix as the member.

**Proved the "sliding" property**: after enough real wall-clock time
passed (5s window, ~1800s elapsed), `ZREMRANGEBYSCORE key 0 (now-5)`
silently trimmed all old entries and `ZCARD` dropped to 0 — with no
window-boundary "reset event." The window is always exactly
`window_size` seconds of history relative to *whenever the check runs*,
so there's no fixed alignment to burst across.

## 9. Atomic sliding-window check as one Lua script

Trim + check + conditionally-add is again a check-then-act sequence done
as 3 separate round-trips from application code — same TOCTOU shape as
the very first bug, just relocated. Fix: bundle all three into one Lua
script.

```lua
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    return 1
else
    return 0
end
```

Notes on the Lua specifics:
- `ARGV` entries arrive as strings in Lua; `tonumber(...)` converts them
  for arithmetic/comparison.
- Returns `1` (allowed) or `0` (rejected) — one round-trip, one atomic
  decision, no separate follow-up call needed to learn the outcome.
- Because the whole block runs as one indivisible unit, two concurrent
  requests can't both observe `count < limit` as true for the same slot;
  the second request's `ZCARD` strictly sees the first's committed `ZADD`.

**Verified live**: 4 calls with the same timestamp, limit=3 → returned
`1, 1, 1, 0`. First three allowed, fourth correctly rejected — full
atomic sliding-window limiter, hand-verified end to end.

## 10. Wiring it into FastAPI (real code, not `redis-cli`)

Built `limiter.py` (`SlidingWindowLimiter`) + `main.py` (FastAPI app),
using `uv` for env/deps (`uv init --no-readme --python 3.13`,
`uv add fastapi "uvicorn[standard]" redis`). `uv` created an isolated
`.venv` and a `pyproject.toml`/`uv.lock` pair; `uv run uvicorn main:app
--reload` runs the app inside that env with auto-restart on file changes.

`limiter.py`:

```python
import time
import uuid
import redis.asyncio as redis

SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    return 1
else
    return 0
end
"""

class SlidingWindowLimiter:
    def __init__(self, redis_client, window_seconds, limit):
        self.redis = redis_client
        self.window_seconds = window_seconds
        self.limit = limit
        self._script = self.redis.register_script(SLIDING_WINDOW_SCRIPT)

    async def allow(self, identifier: str) -> bool:
        key = f"ratelimit:{identifier}"
        now = time.time()
        member = f"{now}:{uuid.uuid4()}"
        result = await self._script(
            keys=[key], args=[now, self.window_seconds, self.limit, member]
        )
        return result == 1
```

`main.py`:

```python
from contextlib import asynccontextmanager
import redis.asyncio as redis
from fastapi import FastAPI, Header, HTTPException, Request
from limiter import SlidingWindowLimiter

REDIS_URL = "redis://localhost:6379"
WINDOW_SECONDS = 10
LIMIT = 5

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.from_url(REDIS_URL)
    app.state.limiter = SlidingWindowLimiter(
        app.state.redis, window_seconds=WINDOW_SECONDS, limit=LIMIT
    )
    yield
    await app.state.redis.aclose()

app = FastAPI(lifespan=lifespan)

@app.get("/ping")
async def ping(request: Request, x_user_id: str = Header(...)):
    allowed = await request.app.state.limiter.allow(x_user_id)
    if not allowed:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    return {"message": "pong"}
```

Concepts:

- **`redis.asyncio`**, not the sync `redis` client — FastAPI is
  async/await end to end; a sync Redis call would block the whole event
  loop on every network round-trip, killing concurrency.
- **`register_script(...)`** — `redis-py`'s Lua helper; see §11, it
  replaces resending the full Lua source on every call.
- **`lifespan`** — FastAPI/Starlette setup/teardown hook. The Redis
  client (really a connection *pool*) and the limiter instance are
  created **once** at startup and stashed on `app.state`, not recreated
  per request. Connecting is comparatively expensive; the pool is meant
  to be shared/reused across requests.
- **`HTTPException(status_code=429, ...)`** — `429 Too Many Requests` is
  the correct HTTP status for this; FastAPI turns the raised exception
  into a proper JSON error response automatically.
- **`Header(...)`** — FastAPI dependency-declaration helper. Writing
  `x_user_id: str = Header(...)` extracts the value from the incoming
  HTTP header `X-User-Id` (FastAPI auto-maps the snake_case param name to
  the kebab-case header name) and the `...` (Ellipsis) means "no default,
  required" — FastAPI auto-rejects the request with `422` if the header
  is missing.
- **Per-user identity**: rate limiting is per Redis *key*
  (`ratelimit:<identifier>`), fully independent across identifiers.
  Initially used `request.client.host` (caller IP) — but every local
  `curl` test shares one IP, so it looked single-user until swapped for a
  header-based identity (`x_user_id`). Real systems key rate limits by
  something the caller can't easily change/share — API key, authenticated
  user/token subject — not raw IP (shared behind NAT/proxies, spoofable).
  The header here is a stand-in for that.

**Verified live end-to-end over real HTTP**:
- 7 rapid `curl` calls to `/ping` → first 5 returned `200`, next 2
  returned `429`.
- A second batch run immediately after was *entirely* rejected (all 7 →
  `429`) — correct, since batch 1's 5 timestamps hadn't aged out of the
  10s window yet, so the log was still full before batch 2 even started.
- Two different `X-User-Id` values (`alice`, `bob`), 6 requests each →
  each independently got 5×`200` then 1×`429`. Hitting alice's limit had
  **zero** effect on bob's budget, proving separate keys/ZSETs per
  identifier, i.e. the limiter is inherently multi-user by design, not
  just by accident.

## 11. `SCRIPT LOAD` / `EVALSHA` — why `register_script` is cheaper than `EVAL`

Calling `EVAL "<full lua source>" ...` resends the entire script text on
every single call — wasteful at any real request volume. Redis instead
lets you upload a script once and reference it by hash after that:

- `SCRIPT LOAD "<lua source>"` → returns a **SHA1 hash** (40 hex chars)
  and caches the compiled script server-side, keyed by that hash.
- `EVALSHA <sha1> numkeys key1... arg1...` → runs the cached script;
  network payload is just the hash, never the source, after the first
  load.

Verified by hand:

```bash
SHA=$(redis-cli SCRIPT LOAD "return 'hello from cached script'")
redis-cli EVALSHA "$SHA" 0
# -> "hello from cached script", source text never resent
```

If `EVALSHA` is called with a hash Redis doesn't have cached (e.g. after
a server restart wipes the in-memory script cache), Redis replies
`NOSCRIPT No matching script`. `redis-py`'s `register_script()` handles
this transparently: first call tries `EVALSHA`; on `NOSCRIPT` it
automatically falls back to full `EVAL` (which also (re-)caches the
script), so application code never has to think about the fallback —
`await self._script(keys=..., args=...)` just works, cheaply, forever
after the first call.

## 12. What "per-request UUID" actually costs at scale (Cognito-style systems)

Question raised: if every logged request gets a unique member (timestamp
+ UUID), how does a system handling trillions of requests track all of
that?

Answer: it doesn't accumulate anywhere near that much, and nothing is
"tracked" by UUID lookup:

- Each ZSET entry lives inside **one user's key only**
  (`ratelimit:<identifier>`), and is auto-deleted by
  `ZREMRANGEBYSCORE` the moment it ages past the window. At any instant a
  single user's key holds **at most `limit` entries** (5, in our
  example) — not a growing history. Total memory is roughly
  `active_users × limit`, not `total_requests_ever`.
- The UUID is never looked up by value — nothing does
  `ZSCORE key "<that-member>"`. Its only job is avoiding a same-timestamp
  collision inside one user's small log (see the collision gotcha in
  §8) — it is not a request-tracking ID.
- Real large-scale systems differ from our toy version in a few concrete
  ways:
  1. **Sharding** (Redis Cluster / app-level sharding) — a user's key
     hashes to one of many nodes; no single instance holds all users.
  2. **Cheaper approximate algorithms at extreme scale** — the "sliding
     window log" (what we built) is the most *accurate* approach but
     costs one ZSET entry per request. Many production limiters instead
     use a **sliding window counter**: just two integers per user
     (previous window's count + current window's count), blended with a
     weighted estimate. O(1) memory per user instead of O(limit), at the
     cost of the count being approximate. Stripe and Cloudflare have both
     written publicly about this tradeoff.
  3. **Local/edge approximation** — extreme-scale systems (e.g.
     Cloudflare-scale) often avoid a network round-trip to a central
     Redis on every single request entirely, letting edge nodes keep
     locally-approximate counters synced/reconciled periodically instead.
  4. **Identity is already a stable ID the caller owns** — production
     systems key by API key / client ID / authenticated user ID, never
     by a UUID they mint per request. The UUID in our code is purely a
     ZSET-collision disambiguator for one already-identified user, with
     no relation to *who* is being tracked.

## 13. Rate-limit response headers

Standard (de facto, not an official RFC, but widely followed by GitHub,
Stripe, etc.) headers for rate-limited APIs:

- **`X-RateLimit-Limit`** — the cap itself.
- **`X-RateLimit-Remaining`** — how many more requests this identity can
  make right now.
- **`X-RateLimit-Reset`** — Unix timestamp of when a slot next frees up.
  For a *sliding* window this specifically means "when the oldest logged
  entry ages out" — not "when the count resets to zero," which is the
  fixed-window meaning. Confirmed live: across a run of rejected requests
  where nothing new got logged, `x-ratelimit-reset` stayed **pinned** to
  the same value the whole time, since the oldest entry never changed.
- **`Retry-After`** — a real HTTP-spec header (not rate-limit-specific),
  seconds to wait before retrying. Only set on `429` responses.

To supply `remaining`/`reset_at`, the Lua script itself was extended to
compute and return them, since a Lua script can only `return` one value
— but that value can be a **table**, which arrives in Python as a list:

```lua
local allowed = 0
if count < limit then
    redis.call('ZADD', key, now, member)
    allowed = 1
    count = count + 1
end

local remaining = limit - count
if remaining < 0 then remaining = 0 end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local reset_at = now + window
if #oldest > 0 then
    reset_at = tonumber(oldest[2]) + window
end

return {allowed, remaining, reset_at}
```

New Redis/Lua pieces:
- **`ZRANGE key 0 0 WITHSCORES`** — fetch the member at rank 0 (lowest
  score = oldest entry, since a ZSET is always sorted by score) plus its
  score. `WITHSCORES` returns `[member, score]` instead of just
  `[member]`.
- **`#oldest`** — Lua's length operator on the returned table; `0` when
  the log is empty, so `reset_at` falls back to `now + window`.
  **Lua tables are 1-indexed** — `oldest[1]` is the member, `oldest[2]`
  is the score (the timestamp), not `oldest[0]`/`oldest[1]` as in
  0-indexed languages.
- **`return {allowed, remaining, reset_at}`** — a Lua table literal
  becomes a RESP array reply; `redis-py` decodes it as a Python list,
  unpacked in `limiter.py` as `allowed, remaining, reset_at = await
  self._script(...)`.

App-side: `limiter.py` gained a `LimitResult` `@dataclass` (auto-generates
`__init__`/`__repr__` for a plain field-bundle — `LimitResult(allowed=...,
remaining=..., reset_at=...)` self-documents better than a raw tuple).
`main.py` switched from an implicit `return {...}` dict response to an
explicit `JSONResponse(..., headers=...)`, since custom headers need to
be attached on both the success and the `429` path (plain `HTTPException`
has no clean way to attach extra headers).

**Verified live**: fresh user → `200`, `remaining: 4`. 5 more requests →
`remaining` ticked `4, 3, 2, 1, 0`. Next 5 requests → `429` each time,
with `retry-after` counting down live per request (`4, 3, 3, 3, 3`,
recomputed fresh every call as `reset_at - time.time()`, not cached) while
`x-ratelimit-reset` stayed constant the whole time (nothing new got
logged, so the oldest entry — and thus reset time — never moved).

## 14. Sliding recovery, proven through real HTTP

Closed the loop on the "sliding, not resetting" property (originally
proven by hand with `redis-cli` in §8) through the actual FastAPI server:

```bash
sleep 11 && curl -i -H "X-User-Id: headers-test" http://127.0.0.1:8000/ping
```

Waited past the 10s window, then made one more request as the same user
who'd just exhausted their limit. Result: `200 OK`,
`x-ratelimit-remaining: 4` — full recovery. All 5 entries from the
previous burst were now >10s old, `ZREMRANGEBYSCORE` trimmed all of them,
`ZCARD` read `0`, and this request became entry #1 of a fresh log — no
reset event, no clock-aligned boundary, purely the passage of real time
relative to *this* request's own `now`.

## Still open / not yet covered

- Dockerizing with `redis:7-alpine` per the README's "Resources" section,
  instead of relying on local homebrew Redis.
- Basic automated tests (pytest) instead of manual `curl` loops.
- Sliding-window log's cost tradeoff vs. fixed window: O(window size ×
  request rate) memory per key (one ZSET entry per request) instead of
  one integer.
- `MULTI`/`EXEC` transactions as an alternative atomicity mechanism to
  Lua (different semantics — no conditional branching mid-transaction,
  unlike Lua).
