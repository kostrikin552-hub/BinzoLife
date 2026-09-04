# handlers/inline.py — ПОЛНАЯ ВЕРСИЯ
import logging
import html
from aiogram import Router, types
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = Router(name="inline_router")


@router.inline_query()
async def inline_search(query: types.InlineQuery):
    query_text = query.query.strip()
    results = []

    # Если строка пустая — выводим подсказку
    if not query_text:
        help_item = InlineQueryResultArticle(
            id="help_hint",
            title="🔍 Введите город и тип топлива",
            description="Пример: @BinzoLife_bot Казань АИ-95",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "⛽ Для быстрого поиска цен укажите город и топливо, например:\n"
                    "<code>@BinzoLife_bot Москва АИ-95</code>"
                ),
                parse_mode="HTML"
            )
        )
        await query.answer([help_item], cache_time=10)
        return

    # Безопасный разбор аргументов
    parts = query_text.split(maxsplit=1)
    city_name = parts[0]
    fuel_type = parts[1].upper() if len(parts) > 1 else "АИ-95"

    try:
        user_lat = query.location.latitude if query.location else None
        user_lon = query.location.longitude if query.location else None

        async with AsyncSessionLocal() as db:
            if user_lat and user_lon:
                # Поиск по геолокации
                stmt = text("""
                    SELECT s.id, s.name, s.brand, s.address, s.latitude, s.longitude,
                           (6371 * acos(cos(radians(:lat)) * cos(radians(s.latitude)) *
                            cos(radians(s.longitude) - radians(:lon)) +
                            sin(radians(:lat)) * sin(radians(s.latitude)))) AS dist
                    FROM stations s
                    WHERE s.is_active = true
                    ORDER BY dist ASC
                    LIMIT 6
                """)
                rows = (await db.execute(stmt, {"lat": user_lat, "lon": user_lon})).mappings().all()
            else:
                # Поиск по городу и бренду/адресу
                stmt = text("""
                    SELECT s.id, s.name, s.brand, s.address, s.latitude, s.longitude, 0 AS dist
                    FROM stations s
                    JOIN cities c ON s.city_id = c.id
                    WHERE s.is_active = true
                      AND (LOWER(c.name) LIKE :city OR LOWER(s.name) LIKE :city
                           OR LOWER(s.brand) LIKE :city OR LOWER(s.address) LIKE :city)
                    LIMIT 6
                """)
                rows = (await db.execute(stmt, {"city": f"%{city_name}%"})).mappings().all()

        for r in rows:
            dist_badge = f" • {r['dist']:.1f} км" if user_lat else ""
            nav_url = f"https://yandex.ru/navi/?whatshere[point]={r['longitude']}%2C{r['latitude']}&whatshere[zoom]=16"
            msg = (
                f"⛽ <b>{html.escape(r['brand'] or 'АЗС')}</b> — {html.escape(r['name'])}\n"
                f"📍 {html.escape(r['address'])}{dist_badge}\n\n"
                f"🔍 <i>Смотрите актуальные цены и статус пистолетов в @BinzoLife_bot</i>"
            )
            results.append(
                InlineQueryResultArticle(
                    id=f"st_{r['id']}",
                    title=f"{html.escape(r['brand'] or 'АЗС')} ({html.escape(r['address'][:30])})",
                    description=f"Статус и цены на АЗС{dist_badge}",
                    input_message_content=InputTextMessageContent(
                        message_text=msg,
                        parse_mode="HTML"
                    ),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🗺 Маршрут в Навигаторе", url=nav_url)]
                    ])
                )
            )
    except Exception as e:
        logger.error(f"[Inline] Исключение: {e}")
    finally:
        await query.answer(results=results, cache_time=15, is_personal=True)
