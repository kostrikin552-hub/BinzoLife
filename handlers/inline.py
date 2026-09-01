# handlers/inline.py
import logging
from aiogram import Router, types
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = Router(name="inline_router")


@router.inline_query()
async def inline_search(query: types.InlineQuery):
    results = []
    q_str = query.query.strip().lower()

    try:
        user_lat = query.location.latitude if query.location else None
        user_lon = query.location.longitude if query.location else None

        async with AsyncSessionLocal() as db:
            if user_lat and user_lon:
                # Поиск по радиусу (формула Haversine)
                stmt = text("""
                    SELECT s.id, s.name, s.brand, s.address,
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
                # Текстовый поиск по бренду/адресу
                pattern = f"%{q_str}%" if q_str else "%лукойл%"
                stmt = text("""
                    SELECT id, name, brand, address, 0 AS dist
                    FROM stations
                    WHERE is_active = true AND (LOWER(name) LIKE :q OR LOWER(brand) LIKE :q OR LOWER(address) LIKE :q)
                    LIMIT 6
                """)
                rows = (await db.execute(stmt, {"q": pattern})).mappings().all()

        for r in rows:
            dist_badge = f" • {r['dist']:.1f} км" if user_lat else ""
            msg = (
                f"⛽ <b>{r['brand']}</b> — {r['name']}\n"
                f"📍 Адрес: {r['address']}{dist_badge}\n\n"
                f"🔍 <i>Смотрите актуальные цены и статус пистолетов в @BinzoLife_bot</i>"
            )
            results.append(
                InlineQueryResultArticle(
                    id=f"st_{r['id']}",
                    title=f"{r['brand']} ({r['address'][:30]})",
                    description=f"Статус и цены на АЗС{dist_badge}",
                    input_message_content=InputTextMessageContent(
                        message_text=msg,
                        parse_mode="HTML"
                    )
                )
            )
    except Exception as e:
        logger.error(f"[Inline] Исключение: {e}")
    finally:
        # Гарантированный возврат ответа Telegram
        await query.answer(results=results, cache_time=15, is_personal=True)
