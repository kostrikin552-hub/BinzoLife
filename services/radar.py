import logging
from datetime import datetime, timezone
from sqlalchemy import select, func
from database.session import AsyncSessionLocal
from database.models import User, Station, FuelPrice, FuelType
from config import settings
from aiogram import Bot

logger = logging.getLogger(__name__)
bot = Bot(token=settings.BOT_TOKEN)

async def send_friday_fuel_radar():
    """Отправляет пятничный радар цен всем пользователям."""
    # Проверяем, что сегодня пятница
    if datetime.now(timezone.utc).weekday() != 4:  # 4 = пятница
        return

    async with AsyncSessionLocal() as db:
        users = await db.execute(
            select(User).where(
                User.telegram_id.is_not(None),
                User.telegram_id > 0
            )
        )
        users = users.scalars().all()

        for user in users:
            try:
                # Находим топ-3 дешёвые АЗС в городе пользователя
                if not user.city_id:
                    continue

                cheapest = await db.execute(
                    select(Station, FuelPrice.price)
                    .join(FuelPrice, FuelPrice.station_id == Station.id)
                    .where(
                        Station.city_id == user.city_id,
                        FuelPrice.fuel_type == FuelType.AI_95,
                        FuelPrice.is_fresh == True,
                        FuelPrice.price > 0
                    )
                    .order_by(FuelPrice.price.asc())
                    .limit(3)
                )
                stations = cheapest.all()

                if not stations:
                    continue

                text = "🚗 <b>Пятничный радар цен перед выходными!</b>\n\n"
                text += "⚡ Цены на бензин в вашем городе обновились.\n"
                text += "💡 <i>Заправьте полный бак сегодня до вечерних пробок и сэкономьте до 350 ₽.</i>\n\n"
                text += "📍 <b>Топ-3 дешёвых АЗС:</b>\n"

                for i, (station, price) in enumerate(stations, 1):
                    text += f"{i}. {station.name} — {price:.2f} ₽/л\n"

                text += "\n👇 Нажмите кнопку ниже, чтобы увидеть все дешёвые АЗС рядом:"

                await bot.send_message(
                    user.telegram_id,
                    text,
                    parse_mode="HTML",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "🔍 Найти дешёвые АЗС рядом", "callback_data": "quick_search_cheapest"}]
                        ]
                    }
                )
            except Exception as e:
                logger.error(f"Ошибка отправки радара пользователю {user.telegram_id}: {e}")
                continue
