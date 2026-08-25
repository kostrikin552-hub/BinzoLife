import matplotlib
matplotlib.use('Agg')  # <-- ДОБАВЛЕНО

import matplotlib.pyplot as plt
import io
from datetime import datetime, timedelta, timezone
from typing import Optional
from database.session import AsyncSessionLocal
from database.crud import get_price_history
from database.models import FuelType
import logging

logger = logging.getLogger(__name__)

async def generate_price_graph(station_id: int, fuel_type: FuelType, days: int = 30) -> Optional[bytes]:
    logger.info(f"generate_price_graph: station_id={station_id}, fuel_type={fuel_type}, days={days}")
    async with AsyncSessionLocal() as db:
        history = await get_price_history(db, station_id, fuel_type, days)
        logger.info(f"Получено {len(history)} записей для station_id={station_id}")
        if not history:
            logger.warning(f"Нет данных для station_id={station_id} за {days} дней")
            return None

        history.sort(key=lambda x: x.recorded_at)
        dates = [h.recorded_at for h in history]
        prices = [h.price for h in history]

        filtered = [(d, p) for d, p in zip(dates, prices) if 0 < p < 200]
        if not filtered:
            logger.warning(f"Все цены аномальны для station_id={station_id}")
            return None
        dates, prices = zip(*filtered)

        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.plot(dates, prices, marker='o', linestyle='-', color='#1f77b4', linewidth=2.5, markersize=6)

        ax.set_title(f"Цена {fuel_type.value} за последние {days} дней", fontsize=14, fontweight='bold')
        ax.set_xlabel("Дата", fontsize=12)
        ax.set_ylabel("Цена, ₽", fontsize=12)
        ax.grid(True, linestyle='--', alpha=0.6)

        import matplotlib.dates as mdates
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
        plt.setp(ax.get_xticklabels(), rotation=30, ha='right', fontsize=10)

        fig.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close()
        logger.info(f"График сгенерирован, размер {len(buf.getvalue())} байт")
        return buf.getvalue()
