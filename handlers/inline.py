import logging
from aiogram import Router, types
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = Router(name="inline")

@router.inline_query()
async def inline_query_handler(inline_query: types.InlineQuery):
    results = []
    query_text = inline_query.query.strip().lower()
    
    try:
        # Определяем координаты пользователя (если переданы)
        user_lat = inline_query.location.latitude if inline_query.location else None
        user_lon = inline_query.location.longitude if inline_query.location else None

        async with AsyncSessionLocal() as db:
            if user_lat and user_lon:
                # Поиск ближайших АЗС
                sql = text("""
                    SELECT s.id, s.name, s.address, s.brand,
                           (6371 * acos(cos(radians(:lat)) * cos(radians(s.latitude)) * 
                            cos(radians(s.longitude) - radians(:lon)) + 
                            sin(radians(:lat)) * sin(radians(s.latitude)))) AS distance
                    FROM stations s
                    WHERE s.is_active = True
                    ORDER BY distance ASC
                    LIMIT 5
                """)
                res = await db.execute(sql, {"lat": user_lat, "lon": user_lon})
            else:
                # Поиск по названию / адресу / бренду
                sql = text("""
                    SELECT id, name, address, brand, 0 as distance
                    FROM stations
                    WHERE is_active = True AND (LOWER(name) LIKE :q OR LOWER(address) LIKE :q OR LOWER(brand) LIKE :q)
                    LIMIT 5
                """)
                search_pattern = f"%{query_text}%" if query_text else "%лукойл%"
                res = await db.execute(sql, {"q": search_pattern})
                
            stations = res.mappings().all()

        for st in stations:
            dist_str = f" • {st['distance']:.1f} км" if user_lat else ""
            card_text = (
                f"⛽ <b>{st['brand']} — {st['name']}</b>\n"
                f"📍 Адрес: {st['address']}{dist_str}\n\n"
                f"🔍 Откройте @BinzoLife_bot для просмотра цен и наличия топлива!"
            )
            results.append(
                InlineQueryResultArticle(
                    id=f"station_{st['id']}",
                    title=f"{st['brand']} ({st['address'][:25]}...)",
                    description=f"Наличие и цены на АЗС{dist_str}",
                    input_message_content=InputTextMessageContent(
                        message_text=card_text,
                        parse_mode="HTML"
                    )
                )
            )
    except Exception as e:
        logger.error(f"[Inline] Ошибка обработки инлайн запроса: {e}")
    finally:
        # КРИТИЧНО: Всегда отправляем ответ Telegram, иначе клиент зависает
        await inline_query.answer(
            results=results,
            cache_time=10,
            is_personal=True
        )
