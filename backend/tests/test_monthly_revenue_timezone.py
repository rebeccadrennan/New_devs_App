from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.reservations import calculate_monthly_revenue


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def fetchone(self):
		return self._row


class _FakeSession:
	def __init__(self):
		self.properties = {
			("prop-001", "tenant-a"): "Europe/Paris",
			("prop-010", "tenant-b"): "America/New_York",
		}
		self.reservations = [
			# Paris boundary: local 2024-03-01 00:30, should be in March (Paris)
			{
				"property_id": "prop-001",
				"tenant_id": "tenant-a",
				"check_in_date": datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc),
				"total_amount": Decimal("1250.000"),
			},
			# Existing normal March reservation for Paris property
			{
				"property_id": "prop-001",
				"tenant_id": "tenant-a",
				"check_in_date": datetime(2024, 3, 15, 10, 0, tzinfo=timezone.utc),
				"total_amount": Decimal("333.333"),
			},
			# Tenant-scoping guard: same property_id but different tenant
			{
				"property_id": "prop-001",
				"tenant_id": "tenant-b",
				"check_in_date": datetime(2024, 3, 8, 10, 0, tzinfo=timezone.utc),
				"total_amount": Decimal("999.000"),
			},
			# Paris local 2024-04-01 00:00 (DST +2), must be excluded from March
			{
				"property_id": "prop-001",
				"tenant_id": "tenant-a",
				"check_in_date": datetime(2024, 3, 31, 22, 0, tzinfo=timezone.utc),
				"total_amount": Decimal("200.000"),
			},
			# New York boundary: local 2024-02-29 23:30, should be in February (NY)
			{
				"property_id": "prop-010",
				"tenant_id": "tenant-b",
				"check_in_date": datetime(2024, 3, 1, 4, 30, tzinfo=timezone.utc),
				"total_amount": Decimal("500.000"),
			},
			# Normal March reservation in New York
			{
				"property_id": "prop-010",
				"tenant_id": "tenant-b",
				"check_in_date": datetime(2024, 3, 20, 12, 0, tzinfo=timezone.utc),
				"total_amount": Decimal("700.000"),
			},
		]

	async def execute(self, query, params):
		query_text = str(query)
		if "FROM properties" in query_text:
			tz_name = self.properties.get((params["property_id"], params["tenant_id"]))
			row = SimpleNamespace(timezone=tz_name) if tz_name else None
			return _FakeResult(row)

		if "FROM reservations" in query_text:
			total = Decimal("0")
			count = 0
			for reservation in self.reservations:
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


def _run(coro):
	import asyncio

	return asyncio.run(coro)


def test_paris_boundary_reservation_counts_in_march_not_february():
	session = _FakeSession()

	march = _run(calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024, db_session=session))
	feb = _run(calculate_monthly_revenue("prop-001", "tenant-a", 2, 2024, db_session=session))

	assert Decimal(march["total"]) == Decimal("1583.333")
	assert march["count"] == 2
	assert Decimal(feb["total"]) == Decimal("0")
	assert feb["count"] == 0


def test_new_york_boundary_case_uses_property_local_time():
	session = _FakeSession()

	feb = _run(calculate_monthly_revenue("prop-010", "tenant-b", 2, 2024, db_session=session))
	march = _run(calculate_monthly_revenue("prop-010", "tenant-b", 3, 2024, db_session=session))

	assert Decimal(feb["total"]) == Decimal("500.000")
	assert feb["count"] == 1
	assert Decimal(march["total"]) == Decimal("700.000")
	assert march["count"] == 1


def test_end_boundary_is_exclusive_at_local_next_month_midnight():
	session = _FakeSession()

	march = _run(calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024, db_session=session))
	assert Decimal(march["total"]) == Decimal("1583.333")
	assert march["count"] == 2


def test_tenant_scoping_is_preserved_for_monthly_revenue():
	session = _FakeSession()

	march = _run(calculate_monthly_revenue("prop-001", "tenant-a", 3, 2024, db_session=session))
	assert Decimal(march["total"]) == Decimal("1583.333")
	assert Decimal(march["total"]) != Decimal("2582.333")
