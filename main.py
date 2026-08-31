import math
import time
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

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
    result = await request.app.state.limiter.allow(x_user_id)

    headers = {
        "X-RateLimit-Limit": str(LIMIT),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(math.ceil(result.reset_at)),
    }

    if not result.allowed:
        retry_after = max(0, math.ceil(result.reset_at - time.time()))
        headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded"},
            headers=headers,
        )

    return JSONResponse(content={"message": "pong"}, headers=headers)
