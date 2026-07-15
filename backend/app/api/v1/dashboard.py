from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from decimal import Decimal, ROUND_HALF_UP
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()


def _as_decimal(value: Any) -> Decimal:
    """Normalize cached/service totals into Decimal without passing through float."""
    return Decimal(str(value))

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    month: int | None = None,
    year: int | None = None,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    
    tenant_id = getattr(current_user, "tenant_id", "default_tenant") or "default_tenant"
    
    revenue_data = await get_revenue_summary(property_id, tenant_id, month=month, year=year)
    total_revenue = _as_decimal(revenue_data['total'])
    total_revenue_display = total_revenue.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    
    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": format(total_revenue, "f"),
        "total_revenue_display": format(total_revenue_display, "f"),
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count'],
        "month": revenue_data.get('month'),
        "year": revenue_data.get('year')
    }
