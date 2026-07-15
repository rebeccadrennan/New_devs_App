import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
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

from app.services.reservations import calculate_monthly_revenue
from app.services import cache as revenue_cache
from app.api.v1 import dashboard


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _RevenueSession:
    def __init__(self, reservations, timezone_name="UTC"):
        self._reservations = reservations
        self._timezone_name = timezone_name

    async def execute(self, query, params):
        query_text = str(query)

        if "FROM properties" in query_text:
            return _FakeResult(SimpleNamespace(timezone=self._timezone_name))

        if "FROM reservations" in query_text:
            total = Decimal("0")
            count = 0
            for reservation in self._reservations:
                if reservation["property_id"] != params["property_id"]:
                    continue
                if reservation["tenant_id"] != params["tenant_id"]:
                    continue
                if reservation["check_in_date"] < params["utc_start"]:
                    continue
                if reservation["check_in_date"] >= params["utc_end"]:
                    continue
                total += reservation["total_amount"]
                count += 1
            return _FakeResult(SimpleNamespace(total_revenue=total, reservation_count=count))

        raise AssertionError(f"Unexpected query: {query_text}")


class _FakeRedis:
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


def _march_date(day: int):
    return datetime(2024, 3, day, 12, 0, tzinfo=timezone.utc)


def test_decimal_triplet_sums_to_exact_internal_total():
    session = _RevenueSession(
        reservations=[
            {
                "property_id": "prop-001",
                "tenant_id": "tenant-a",
                "check_in_date": _march_date(15),
                "total_amount": Decimal("333.333"),
            },
            {
                "property_id": "prop-001",
                "tenant_id": "tenant-a",
                "check_in_date": _march_date(16),
                "total_amount": Decimal("333.333"),
            },
            {
                "property_id": "prop-001",
                "tenant_id": "tenant-a",
                "check_in_date": _march_date(17),
                "total_amount": Decimal("333.334"),
            },
        ]
    )

    result = _run(calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024, db_session=session))

    assert Decimal(result["total"]) == Decimal("1000.000")


def test_binary_float_case_is_exact_in_decimal_path():
    session = _RevenueSession(
        reservations=[
            {
                "property_id": "prop-001",
                "tenant_id": "tenant-a",
                "check_in_date": _march_date(10),
                "total_amount": Decimal("0.100"),
            },
            {
                "property_id": "prop-001",
                "tenant_id": "tenant-a",
                "check_in_date": _march_date(11),
                "total_amount": Decimal("0.200"),
            },
        ]
    )

    result = _run(calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024, db_session=session))

    assert Decimal(result["total"]) == Decimal("0.300")


def test_sum_before_rounding_with_half_up_reporting_boundary(monkeypatch):
    async def fake_summary(_property_id, _tenant_id, month=None, year=None):
        return {
            "property_id": "prop-001",
            "tenant_id": "tenant-a",
            "total": "1.005",
            "currency": "USD",
            "count": 3,
            "month": month,
            "year": year,
        }

    monkeypatch.setattr(dashboard, "get_revenue_summary", fake_summary)

    response = _run(
        dashboard.get_dashboard_summary(
            property_id="prop-001",
            month=3,
            year=2024,
            current_user=SimpleNamespace(tenant_id="tenant-a"),
        )
    )

    # If each line were rounded first, this class of case drifts by a cent.
    assert response["total_revenue"] == "1.005"
    assert response["total_revenue_display"] == "1.01"


def test_dashboard_response_preserves_exact_total_and_two_decimal_display(monkeypatch):
    async def fake_summary(_property_id, _tenant_id, month=None, year=None):
        return {
            "property_id": "prop-001",
            "tenant_id": "tenant-a",
            "total": "1000.000",
            "currency": "USD",
            "count": 3,
            "month": month,
            "year": year,
        }

    monkeypatch.setattr(dashboard, "get_revenue_summary", fake_summary)

    response = _run(
        dashboard.get_dashboard_summary(
            property_id="prop-001",
            month=3,
            year=2024,
            current_user=SimpleNamespace(tenant_id="tenant-a"),
        )
    )

    assert response["total_revenue"] == "1000.000"
    assert response["total_revenue_display"] == "1000.00"


def test_cached_and_uncached_paths_return_same_monetary_result(monkeypatch):
    fake_redis = _FakeRedis()
    calls = {"count": 0}

    async def fake_calc(property_id, tenant_id, month, year):
        calls["count"] += 1
        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": "1000.000",
            "currency": "USD",
            "count": 3,
            "month": month,
            "year": year,
        }

    monkeypatch.setattr(revenue_cache, "redis_client", fake_redis)
    from app.services import reservations

    monkeypatch.setattr(reservations, "calculate_monthly_revenue", fake_calc)

    uncached = _run(revenue_cache.get_revenue_summary("prop-001", "tenant-a", month=3, year=2024))
    cached = _run(revenue_cache.get_revenue_summary("prop-001", "tenant-a", month=3, year=2024))

    assert uncached == cached
    assert uncached["total"] == "1000.000"
    assert isinstance(uncached["total"], str)
    assert calls["count"] == 1
