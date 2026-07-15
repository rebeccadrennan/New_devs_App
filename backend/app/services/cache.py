import json
import redis.asyncio as redis
from typing import Dict, Any
import os
from datetime import datetime, timezone

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def _revenue_cache_key(tenant_id: str, property_id: str, month: int, year: int) -> str:
    """Build the canonical tenant-scoped monthly revenue cache key."""
    return f"revenue:v2:{tenant_id}:{property_id}:{year:04d}-{month:02d}"


def _tenant_revenue_cache_pattern(tenant_id: str) -> str:
    """Build the canonical tenant-scoped revenue cache pattern."""
    return f"revenue:v2:{tenant_id}:*"


def _property_revenue_cache_pattern(tenant_id: str, property_id: str) -> str:
    """Build the canonical tenant+property monthly revenue cache pattern."""
    return f"revenue:v2:{tenant_id}:{property_id}:*"

async def get_revenue_summary(
    property_id: str,
    tenant_id: str,
    month: int | None = None,
    year: int | None = None,
) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    if month is None or year is None:
        now_utc = datetime.now(timezone.utc)
        month = month if month is not None else now_utc.month
        year = year if year is not None else now_utc.year

    cache_key = _revenue_cache_key(tenant_id, property_id, month, year)
    
    # Try to get from cache
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)
    
    # Revenue calculation is delegated to the reservation service.
    from app.services.reservations import calculate_monthly_revenue
    
    # Calculate revenue
    result = await calculate_monthly_revenue(property_id, tenant_id, month, year)
    
    # Cache the result for 5 minutes
    await redis_client.setex(cache_key, 300, json.dumps(result))
    
    return result


async def invalidate_revenue_summary_cache(
    property_id: str,
    tenant_id: str,
    month: int | None = None,
    year: int | None = None,
) -> bool:
    """Invalidate one or many tenant-scoped monthly revenue cache entries for a property."""
    if month is not None and year is not None:
        cache_key = _revenue_cache_key(tenant_id, property_id, month, year)
        deleted = await redis_client.delete(cache_key)
        return bool(deleted)

    keys = []
    pattern = _property_revenue_cache_pattern(tenant_id, property_id)
    async for key in redis_client.scan_iter(match=pattern):
        keys.append(key)

    if not keys:
        return False

    deleted = await redis_client.delete(*keys)
    return bool(deleted)


async def invalidate_tenant_revenue_cache(tenant_id: str) -> int:
        """Invalidate all revenue cache entries for a tenant."""
        pattern = _tenant_revenue_cache_pattern(tenant_id)
        keys = []

        async for key in redis_client.scan_iter(match=pattern):
                keys.append(key)

        if not keys:
                return 0

        deleted = await redis_client.delete(*keys)
        return int(deleted or 0)
