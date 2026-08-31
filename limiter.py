import time
import uuid
from dataclasses import dataclass

import redis.asyncio as redis

SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

local allowed = 0
if count < limit then
    redis.call('ZADD', key, now, member)
    allowed = 1
    count = count + 1
end

local remaining = limit - count
if remaining < 0 then
    remaining = 0
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local reset_at = now + window
if #oldest > 0 then
    reset_at = tonumber(oldest[2]) + window
end

return {allowed, remaining, reset_at}
"""


@dataclass
class LimitResult:
    allowed: bool
    remaining: int
    reset_at: float


class SlidingWindowLimiter:
    def __init__(self, redis_client: redis.Redis, window_seconds: int, limit: int):
        self.redis = redis_client
        self.window_seconds = window_seconds
        self.limit = limit
        self._script = self.redis.register_script(SLIDING_WINDOW_SCRIPT)

    async def allow(self, identifier: str) -> LimitResult:
        key = f"ratelimit:{identifier}"
        now = time.time()
        member = f"{now}:{uuid.uuid4()}"

        allowed, remaining, reset_at = await self._script(
            keys=[key],
            args=[now, self.window_seconds, self.limit, member],
        )
        return LimitResult(allowed=allowed == 1, remaining=remaining, reset_at=reset_at)
