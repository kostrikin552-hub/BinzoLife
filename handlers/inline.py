from aiogram import Router, types, F
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from database.session import AsyncSessionLocal
from database.models import FuelType
from database.crud import get_city_by_name, get_stations_by_city, get_latest_fresh_price

router = Router()

@router.inline_query()
async def inline_search(query: types.InlineQuery):
    # Разбираем запрос: "95", "92", "98", "дт"
    fuel_type_map = {
        "95": FuelType.AI_95,
        "92": FuelType.AI_92,
        "98": FuelType.AI_98,
        "дт": FuelType.DT,
        "dt": FuelType.DT
    }
    fuel_input = query.query.strip().lower()
    fuel_type = fuel_type_map.get(fuel_input, FuelType.AI_95)  # по умолчанию 95

    # Определяем город пользователя (пока берём Москву, если нет города)
    # Для полноценной работы нужно получать город из состояния или профиля, но для инлайн-режима проще взять Москву
    city_name = "Москва"

    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name)
        if not city:
            await query.answer([], cache_time=60)
            return

        stations = await get_stations_by_city(db, city.id)
        if not stations:
            await query.answer([], cache_time=60)
            return

        # Берём топ-3 станции с самой низкой ценой
        prices = []
        for station in stations:
            price = await get_latest_fresh_price(db, station.id, fuel_type)
            if price:
                prices.append((station, price.price))
        prices.sort(key=lambda x: x[1])  # сортируем по цене
        top3 = prices[:3]

        if not top3:
            await query.answer([], cache_time=60)
            return

        # Формируем текст
        text = f"⛽ <b>Топ АЗС с лучшей ценой на {fuel_type.value}:</b>\n\n"
        for i, (station, price) in enumerate(top3, 1):
            text += f"{i}️⃣ <b>{station.name}</b> — {price:.2f} ₽/л\n"
        text += f"\n🔍 <i>Ищи дешёвое топливо рядом с собой в @BinzoLife_bot</i>"

        # Создаём результат
        result = InlineQueryResultArticle(
            id="cheapest_fuel",
            title=f"⚡ Топ дешёвых АЗС ({fuel_type.value})",
            description=f"Показать 3 самые выгодные заправки",
            input_message_content=InputTextMessageContent(
                message_text=text,
                parse_mode="HTML"
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "📍 Найти лучшие цены у себя", "url": "https://t.me/BinzoLife_bot?start=inline_search"}]
                ]
            }
        )
        await query.answer([result], cache_time=10)
