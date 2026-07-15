from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.core.auth import authenticate_request as get_current_user
from app.core.database_pool import db_pool

router = APIRouter()


@router.get("/properties")
async def list_properties(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    search: str = Query(""),
    current_user=Depends(get_current_user),
):
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=403, detail="Tenant context is required")

    if not db_pool.session_factory:
        await db_pool.initialize()

    if not db_pool.session_factory:
        raise HTTPException(status_code=503, detail="Database unavailable")

    offset = (page - 1) * page_size
    search_like = f"%{search}%"

    count_query = text(
        """
        SELECT COUNT(*)
        FROM properties
        WHERE tenant_id = :tenant_id
          AND (:search = '' OR name ILIKE :search_like OR id ILIKE :search_like)
        """
    )

    items_query = text(
        """
        SELECT id, tenant_id, name, timezone, created_at
        FROM properties
        WHERE tenant_id = :tenant_id
          AND (:search = '' OR name ILIKE :search_like OR id ILIKE :search_like)
        ORDER BY name ASC, id ASC
        LIMIT :limit OFFSET :offset
        """
    )

    async with db_pool.get_session() as session:
        total_result = await session.execute(
            count_query,
            {"tenant_id": tenant_id, "search": search, "search_like": search_like},
        )
        total = int(total_result.scalar() or 0)

        rows_result = await session.execute(
            items_query,
            {
                "tenant_id": tenant_id,
                "search": search,
                "search_like": search_like,
                "limit": page_size,
                "offset": offset,
            },
        )

        items = []
        for row in rows_result.mappings().all():
            items.append(
                {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "name": row["name"],
                    "timezone": row["timezone"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                }
            )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }