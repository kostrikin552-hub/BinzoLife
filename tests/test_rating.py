import pytest
from datetime import datetime
from services.rating import calculate_rating
from database.models import Station, FuelPrice, AvailabilityReport, AvailabilityStatus, FuelType

@pytest.mark.asyncio
async def test_rating_calculation():
    station = Station(id=1, latitude=55.0, longitude=82.0)
    price = FuelPrice(price=65.0, recorded_at=datetime.utcnow())
    avail = AvailabilityReport(status=AvailabilityStatus.GREEN, recorded_at=datetime.utcnow(), confidence=0.9)
    result = calculate_rating(station, 55.0, 82.0, price, avail, 67.0, 64.0, 70.0)
    assert result["rating"] > 50
    assert "цена ниже средней" in result["explanation"]
