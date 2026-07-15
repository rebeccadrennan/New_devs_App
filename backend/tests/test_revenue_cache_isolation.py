import asyncio
from pathlib import Path
import sys
import types


# Test-only shim: allow importing app.services.cache when redis is not installed.
if "redis.asyncio" not in sys.modules:
    redis_pkg = types.ModuleType("redis")
    redis_asyncio_mod = types.ModuleType("redis.asyncio")

    class _DummyRedisClient:
        async def get(self, *_args, **_kwargs):
            return None

        async def setex(self, *_args, **_kwargs):
            return None

        async def delete(self, *_args, **_kwargs):
            return 0

        async def scan_iter(self, *_args, **_kwargs):
            if False:
                yield None

    class _DummyRedisFactory:
        @classmethod
        def from_url(cls, *_args, **_kwargs):
            return _DummyRedisClient()

    redis_asyncio_mod.Redis = _DummyRedisFactory
    redis_pkg.asyncio = redis_asyncio_mod
    sys.modules["redis"] = redis_pkg
    sys.modules["redis.asyncio"] = redis_asyncio_mod

from app.services import cache as revenue_cache


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                deleted += 1
        return deleted

    async def scan_iter(self, match=None):
        if match is None or match == "*":
            for key in list(self.store.keys()):
                yield key
            return

        # Minimal wildcard support for suffix '*' patterns used by tests.
        if match.endswith("*"):
            prefix = match[:-1]
            for key in list(self.store.keys()):
                if str(key).startswith(prefix):
                    yield key
            return

        if match in self.store:
            yield match


def _run(coro):
    return asyncio.run(coro)


def _stub_calculate_total_revenue(property_id: str, tenant_id: str):
    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "total": f"{tenant_id}-total",
        "currency": "USD",
        "count": 1,
    }


def test_tenants_do_not_share_cache_for_same_property(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(revenue_cache, "redis_client", fake_redis)

    async def fake_calc(property_id, tenant_id):
        return _stub_calculate_total_revenue(property_id, tenant_id)

    from app.services import reservations

    monkeypatch.setattr(reservations, "calculate_total_revenue", fake_calc)

    tenant_a = _run(revenue_cache.get_revenue_summary("prop-002", "tenant-a"))
    tenant_b = _run(revenue_cache.get_revenue_summary("prop-002", "tenant-b"))

    assert tenant_a["tenant_id"] == "tenant-a"
    assert tenant_b["tenant_id"] == "tenant-b"
    assert tenant_a != tenant_b
    assert "revenue:tenant-a:prop-002" in fake_redis.store
    assert "revenue:tenant-b:prop-002" in fake_redis.store


def test_request_order_does_not_change_tenant_result(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(revenue_cache, "redis_client", fake_redis)

    async def fake_calc(property_id, tenant_id):
        return _stub_calculate_total_revenue(property_id, tenant_id)

    from app.services import reservations

    monkeypatch.setattr(reservations, "calculate_total_revenue", fake_calc)

    first_b = _run(revenue_cache.get_revenue_summary("prop-003", "tenant-b"))
    then_a = _run(revenue_cache.get_revenue_summary("prop-003", "tenant-a"))

    assert first_b["tenant_id"] == "tenant-b"
    assert then_a["tenant_id"] == "tenant-a"


def test_tenant_b_cache_hit_cannot_return_tenant_a_data(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(revenue_cache, "redis_client", fake_redis)

    async def fake_calc(property_id, tenant_id):
        return _stub_calculate_total_revenue(property_id, tenant_id)

    from app.services import reservations

    monkeypatch.setattr(reservations, "calculate_total_revenue", fake_calc)

    _run(revenue_cache.get_revenue_summary("prop-001", "tenant-a"))
    _run(revenue_cache.get_revenue_summary("prop-001", "tenant-b"))

    async def fail_if_called(property_id, tenant_id):
        raise AssertionError("calculate_total_revenue should not be called on cache hit")

    monkeypatch.setattr(reservations, "calculate_total_revenue", fail_if_called)

    tenant_b_hit = _run(revenue_cache.get_revenue_summary("prop-001", "tenant-b"))
    assert tenant_b_hit["tenant_id"] == "tenant-b"


def test_tenant_specific_invalidation_only_removes_that_tenant_key(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(revenue_cache, "redis_client", fake_redis)

    calls = {"count": 0}

    async def fake_calc(property_id, tenant_id):
        calls["count"] += 1
        return _stub_calculate_total_revenue(property_id, tenant_id)

    from app.services import reservations

    monkeypatch.setattr(reservations, "calculate_total_revenue", fake_calc)

    _run(revenue_cache.get_revenue_summary("prop-004", "tenant-a"))
    _run(revenue_cache.get_revenue_summary("prop-004", "tenant-b"))

    deleted = _run(revenue_cache.invalidate_revenue_summary_cache("prop-004", "tenant-a"))
    assert deleted is True
    assert "revenue:tenant-a:prop-004" not in fake_redis.store
    assert "revenue:tenant-b:prop-004" in fake_redis.store

    # tenant-a should recalculate, tenant-b should stay cached
    _run(revenue_cache.get_revenue_summary("prop-004", "tenant-a"))
    _run(revenue_cache.get_revenue_summary("prop-004", "tenant-b"))

    # 2 initial misses + 1 miss after tenant-a invalidation
    assert calls["count"] == 3


def test_dashboard_uses_authenticated_tenant_not_request_supplied_tenant():
    dashboard_file = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "dashboard.py"
    source = dashboard_file.read_text(encoding="utf-8")

    # Route binds authenticated user dependency, not a client-supplied tenant parameter.
    assert "current_user: dict = Depends(get_current_user)" in source
    assert "tenant_id: str" not in source
    assert 'tenant_id = getattr(current_user, "tenant_id", "default_tenant")' in source


def test_authenticated_tenant_cannot_fetch_other_tenant_cached_result(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(revenue_cache, "redis_client", fake_redis)

    async def fake_calc(property_id, tenant_id):
        return _stub_calculate_total_revenue(property_id, tenant_id)

    from app.services import reservations

    monkeypatch.setattr(reservations, "calculate_total_revenue", fake_calc)

    # First authenticated tenant populates cache for shared property_id.
    _run(revenue_cache.get_revenue_summary("prop-002", "tenant-a"))

    # Another authenticated tenant must resolve to its own tenant-scoped cache entry.
    tenant_b = _run(revenue_cache.get_revenue_summary("prop-002", "tenant-b"))
    assert tenant_b["tenant_id"] == "tenant-b"
