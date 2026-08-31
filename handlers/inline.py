from aiogram import Router, types, F
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton
from database.session import AsyncSessionLocal
from database.models import FuelType
from database.crud import get_user, get_city_by_name, get_stations_by_city, get_latest_fresh_price

router = Router()

@router.inline_query()
async def inline_search(query: types.InlineQuery):
    # Проверяем, есть ли у пользователя город
    async with AsyncSessionLocal() as db:
        user = await get_user(db, query.from_user.id)  # get_user уже подгружает city через joinedload
        if not user or not user.city_id:
            result = InlineQueryResultArticle(
                id="no_city",
                title="📍 Сначала выберите город",
                description="Нажмите, чтобы настроить город в боте",
                input_message_content=InputTextMessageContent(
                    "Чтобы искать дешёвый бензин, сначала настройте город в @BinzoLife_bot.\n"
                    "Откройте бота и выберите город в профиле."
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔧 Настроить город", url="https://t.me/BinzoLife_bot?start=set_city")]
                ])
            )
            await query.answer([result], cache_time=30)
            return

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

        city_name = user.city.name if user.city else "Москва"

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
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📍 Найти лучшие цены у себя", url="https://t.me/BinzoLife_bot?start=inline_search")]
            ])
        )
        await query.answer([result], cache_time=10)
