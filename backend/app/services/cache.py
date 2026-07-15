import json
import redis.asyncio as redis
from typing import Dict, Any
import os

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def _revenue_cache_key(tenant_id: str, property_id: str) -> str:
    """Build the canonical tenant-scoped revenue cache key."""
    return f"revenue:{tenant_id}:{property_id}"


def _tenant_revenue_cache_pattern(tenant_id: str) -> str:
    """Build the canonical tenant-scoped revenue cache pattern."""
    return f"revenue:{tenant_id}:*"

async def get_revenue_summary(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    cache_key = _revenue_cache_key(tenant_id, property_id)
    
    # Try to get from cache
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)
    
    # Revenue calculation is delegated to the reservation service.
    from app.services.reservations import calculate_total_revenue
    
    # Calculate revenue
    result = await calculate_total_revenue(property_id, tenant_id)
    
    # Cache the result for 5 minutes
    await redis_client.setex(cache_key, 300, json.dumps(result))
    
    return result


async def invalidate_revenue_summary_cache(property_id: str, tenant_id: str) -> bool:
        """Invalidate one tenant-scoped revenue cache entry."""
        cache_key = _revenue_cache_key(tenant_id, property_id)
        deleted = await redis_client.delete(cache_key)
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
