import matplotlib.pyplot as plt
import io
from datetime import datetime
from database.session import AsyncSessionLocal
from database.crud import get_price_history
from database.models import FuelType

async def generate_price_graph(station_id: int, fuel_type: FuelType, days: int = 30) -> Optional[bytes]:
    async with AsyncSessionLocal() as db:
        history = await get_price_history(db, station_id, fuel_type, days)
        if not history:
            return None
        dates = [h.recorded_at for h in history]
        prices = [h.price for h in history]
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(dates, prices, marker='o', linestyle='-', color='blue')
        ax.set_title(f"Цена {fuel_type.value} за {days} дней")
        ax.set_xlabel("Дата")
        ax.set_ylabel("Цена, ₽")
        ax.grid(True)
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        return buf.getvalue()
