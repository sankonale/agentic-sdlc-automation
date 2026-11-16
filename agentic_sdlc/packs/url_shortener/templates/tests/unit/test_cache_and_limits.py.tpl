# covers: NFR-1, NFR-3
from app.cache import TTLCache
from app.rate_limit import TokenBucketLimiter


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_cache_ttl_expiry():
    c = Clock()
    cache = TTLCache(10, ttl_s=5, clock=c)
    cache.set("a", 1)
    assert cache.get("a") == 1
    c.t = 6
    assert cache.get("a") is None


def test_cache_lru_eviction():
    cache = TTLCache(2, ttl_s=100)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")
    cache.set("c", 3)
    assert cache.get("b") is None and cache.get("a") == 1 and cache.get("c") == 3


def test_token_bucket_limits_and_refills():
    c = Clock()
    lim = TokenBucketLimiter(rate_per_min=60, burst=2, clock=c)
    assert lim.allow("k") and lim.allow("k")
    assert not lim.allow("k")
    c.t = 1.0   # one token per second
    assert lim.allow("k")
    assert lim.allow("other")   # buckets are per client
