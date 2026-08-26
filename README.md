# Project 1: API Rate Limiter

Warm-up project — don't linger. Start with `INCR`/`EXPIRE` fixed window, then move to a sliding window with sorted sets.

## Why This Project

Smallest surface area, but a real bug to find: `INCR` then `EXPIRE` is two calls, same TOCTOU shape as the lock service's original problem. The bigger flaw is the fixed window's edge burst (2x the limit possible around a window boundary) — fix with a sliding-window log via sorted sets (score = timestamp, trim with `ZREMRANGEBYSCORE`).

## Build Steps

1. Fixed window: `INCR` a per-key, per-window counter; set `EXPIRE` on first increment.
2. Identify and articulate the TOCTOU gap between `INCR` and `EXPIRE`, and the edge-burst flaw at window boundaries.
3. Sliding-window log: use a sorted set per key, score = request timestamp. On each request, `ZREMRANGEBYSCORE` to trim entries older than the window, then check cardinality before adding.

## Resources

Runs on a single `redis:7-alpine` container.

## Cross-Cutting Ideas to Fold In Here

- **RESP protocol**: speak raw RESP over `telnet`/`nc` once, by hand — demystifies what the client library is doing.