# handlers/admin.py — ПОЛНАЯ ФИНАЛЬНАЯ ВЕРСИЯ (с MultiGo, санитайзером и геокодингом)
import csv
import io
import re
import asyncio
import logging
import html
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from sqlalchemy import select, func, delete, or_, text
from sqlalchemy.orm import selectinload
from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, get_user, get_station_by_id,
    deactivate_station, activate_pro, get_station_by_name_address, get_all_reviews,
    get_avg_rating, set_city_slug, save_availability_report_with_consensus,
    get_user_stats, get_payment_stats, get_funnel_stats,
    get_review_stats, get_referral_stats, get_users_by_segment,
    get_marketing_stats, get_all_active_cities, get_deep_business_metrics,
    commit_or_rollback, update_city_prices_by_brand, seed_all_russian_cities,
    fill_missing_station_addresses
)
from database.models import (
    SourceType, AvailabilityStatus, FuelType, Station, City, CitySlug,
    FuelPrice, AvailabilityReport, UserAction, Notification,
    UserAchievement, Referral, UserEconomy, Review, Payment, User
)
from services.city_importer import import_city_from_url
from services.selfcheck import run_self_check
from services.multigo_parser import multigo_parser
from services.cities_seed import RUSSIA_CITIES
from utils.cleaners import normalize_name, clean_address
from services.data_sanitizer import clean_brand_name, validate_fuel_price, normalize_address

router = Router()
logger = logging.getLogger(__name__)


# ========== АДМИН-ПРОВЕРКА ==========
def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS

def admin_only(func):
    async def wrapper(*args, **kwargs):
        message = None
        for arg in args:
            if isinstance(arg, types.Message):
                message = arg
                break
        if not message and 'message' in kwargs:
            message = kwargs['message']
        if not message:
            return await func(*args, **kwargs)
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Нет прав.")
            return
        return await func(message)
    return wrapper

def parse_args(message: types.Message, min_count: int, usage: str):
    parts = message.text.split()
    if len(parts) < min_count:
        asyncio.create_task(message.answer(usage))
        return None
    return parts

async def get_city_or_reply(db, city_name: str, message: types.Message, include_inactive=False):
    city = await get_city_by_name(db, city_name, include_inactive=include_inactive)
    if not city:
        await message.answer(f"Город '{city_name}' не найден.")
        return None
    return city

async def get_station_or_reply(db, station_id: int, message: types.Message):
    station = await get_station_by_id(db, station_id)
    if not station:
        await message.answer("АЗС не найдена.")
        return None
    return station


# ---------- БАЗОВЫЕ АДМИН-КОМАНДЫ ----------
@router.message(Command("add_city"))
@admin_only
async def add_city_cmd(message: types.Message):
    parts = parse_args(message, 2, "Использование: /add_city Название [Регион]")
    if not parts:
        return
    name = parts[1]
    region = parts[2] if len(parts) > 2 else None
    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, name, include_inactive=True)
        if city:
            if not city.is_active:
                city.is_active = True
                await commit_or_rollback(db)
                await message.answer(f"Город {name} реактивирован.")
            else:
                await message.answer(f"Город {name} уже существует.")
        else:
            db.add(City(name=name, region=region))
            await commit_or_rollback(db)
            await message.answer(f"Город {name} добавлен.")


@router.message(Command("set_slug"))
@admin_only
async def set_slug_cmd(message: types.Message):
    parts = parse_args(message, 3, "Использование: /set_slug Город slug")
    if not parts:
        return
    city_name, slug = parts[1], parts[2]
    async with AsyncSessionLocal() as db:
        city = await get_city_or_reply(db, city_name, message)
        if not city:
            return
        await set_city_slug(db, city.id, slug)
        await message.answer(f"Слаг для {city_name} установлен: {slug}")


@router.message(Command("add_station"))
@admin_only
async def add_station_cmd(message: types.Message):
    parts = [p.strip() for p in message.text.split("|")]
    if len(parts) < 6:
        await message.answer("Использование: /add_station Город | Название | Адрес | lat | lon [Бренд]")
        return
    try:
        city_name, name, address = parts[1], parts[2], parts[3]
        lat, lon = float(parts[4]), float(parts[5])
        brand = parts[6] if len(parts) > 6 else None
    except ValueError:
        await message.answer("Неверные координаты.")
        return
    async with AsyncSessionLocal() as db:
        city = await get_city_or_reply(db, city_name, message)
        if not city:
            return
        existing = await get_station_by_name_address(db, city.id, name, address)
        if existing:
            await message.answer(f"АЗС с таким названием и адресом уже существует (ID {existing.id}).")
            return
        station = await create_station(db, city.id, name, address, lat, lon, brand)
        await message.answer(f"АЗС {station.name} добавлена (ID {station.id})")


@router.message(Command("set_price"))
@admin_only
async def set_price_cmd(message: types.Message):
    parts = parse_args(message, 3, "Использование: /set_price station_id price")
    if not parts:
        return
    try:
        station_id = int(parts[1])
        raw_price = parts[2].replace(",", ".").strip()
        price = float(raw_price)
    except ValueError:
        await message.answer("❌ Неверный формат цены. Используйте число с точкой (например: 54.90)")
        return
    if price <= 0:
        await message.answer("❌ Цена должна быть больше 0.")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_or_reply(db, station_id, message)
        if not station:
            return
        await save_price(db, station_id, FuelType.AI_95, price, SourceType.ADMIN, confidence=0.9)
        await message.answer(f"✅ Цена для АЗС {station.name} обновлена: {price:.2f} ₽")


@router.message(Command("set_availability"))
@admin_only
async def set_availability_cmd(message: types.Message):
    parts = parse_args(message, 3, "Использование: /set_availability station_id status (GREEN/YELLOW/RED/GRAY)")
    if not parts:
        return
    try:
        station_id = int(parts[1])
        status = AvailabilityStatus[parts[2].upper()]
    except (ValueError, KeyError):
        await message.answer("❌ Неверный статус. Допустимые: GREEN, YELLOW, RED, GRAY")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_or_reply(db, station_id, message)
        if not station:
            return
        await save_availability_report_with_consensus(
            db, station_id, FuelType.AI_95, status, SourceType.ADMIN, confidence=0.9
        )
        await message.answer(f"Статус наличия для {station.name} установлен: {status.value}")


@router.message(Command("deactivate_station"))
@admin_only
async def deactivate_station_cmd(message: types.Message):
    parts = parse_args(message, 2, "Использование: /deactivate_station station_id")
    if not parts:
        return
    try:
        station_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный ID.")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_or_reply(db, station_id, message)
        if not station:
            return
        await deactivate_station(db, station_id)
        await message.answer(f"АЗС {station.name} деактивирована.")


@router.message(Command("import_csv"))
@admin_only
async def import_csv_cmd(message: types.Message):
    await message.answer("Пришлите CSV-файл с колонками: city,name,brand,address,lat,lon,price,status")


@router.message(F.document)
async def handle_csv_file(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    if not message.document.file_name.endswith('.csv'):
        await message.answer("Пожалуйста, отправьте файл в формате CSV.")
        return
    file = await message.bot.get_file(message.document.file_id)
    content = (await message.bot.download_file(file.file_path)).read().decode('utf-8-sig')
    dialect = csv.Sniffer().sniff(content[:1024])
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)

    async with AsyncSessionLocal() as db:
        async with db.begin():
            for row in reader:
                if not any(row.values()):
                    continue
                try:
                    city_name = row.get("city", "").strip()
                    name = row.get("name", "").strip()
                    address = row.get("address", "").strip()
                    if not city_name or not name or not address:
                        continue
                    lat = float(row.get("lat", 0)) if row.get("lat", "").strip() else 0.0
                    lon = float(row.get("lon", 0)) if row.get("lon", "").strip() else 0.0
                    price = float(row.get("price", 0)) if row.get("price", "").strip() else 0.0
                    status_str = row.get("status", "GRAY").strip().upper()
                    status = AvailabilityStatus[status_str] if status_str in AvailabilityStatus.__members__ else AvailabilityStatus.GRAY
                except ValueError:
                    continue

                city = await get_city_by_name(db, city_name, include_inactive=True)
                if not city:
                    city = City(name=city_name)
                    db.add(city)
                    await db.flush()
                elif not city.is_active:
                    city.is_active = True

                existing_station = await db.execute(
                    select(Station).where(
                        Station.city_id == city.id,
                        Station.latitude == lat,
                        Station.longitude == lon
                    )
                ).scalar_one_or_none()
                if existing_station:
                    existing_station.name = name
                    existing_station.address = address
                    existing_station.brand = row.get("brand", "").strip() or None
                    station = existing_station
                else:
                    station = await create_station(db, city.id, name, address, lat, lon, brand=row.get("brand", "").strip() or None)

                if price > 0:
                    await save_price(db, station.id, FuelType.AI_95, price, SourceType.ADMIN, confidence=0.8)
                await save_availability_report_with_consensus(
                    db, station.id, FuelType.AI_95, status, SourceType.ADMIN, confidence=0.8
                )
        await message.answer("CSV импортирован успешно.")


@router.message(Command("set_pro"))
@admin_only
async def set_pro_cmd(message: types.Message):
    parts = parse_args(message, 3, "Использование: /set_pro telegram_id days (0 - отключить)")
    if not parts:
        return
    try:
        telegram_id, days = int(parts[1]), int(parts[2])
    except ValueError:
        await message.answer("Неверный формат.")
        return
    async with AsyncSessionLocal() as db:
        user = await get_user(db, telegram_id)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        if days <= 0:
            user.is_pro = False
            user.pro_until = None
            await commit_or_rollback(db)
            await message.answer(f"PRO отключён у пользователя {telegram_id}")
        else:
            await activate_pro(db, user, days)
            await message.answer(f"PRO активирован на {days} дней для пользователя {telegram_id}")


@router.message(Command("reviews"))
@admin_only
async def show_reviews(message: types.Message):
    async with AsyncSessionLocal() as db:
        reviews = await get_all_reviews(db, limit=20)
        avg = await get_avg_rating(db)
        if not reviews:
            await message.answer("Пока нет отзывов.")
            return
        text = f"📊 Средний рейтинг: {avg}⭐\n\n"
        for i, rev in enumerate(reviews, 1):
            username = html.escape(rev.user.username or f"User{rev.user.telegram_id}")
            comment = html.escape(rev.comment or "")
            text += f"{i}. {username}: {rev.rating}⭐ "
            if comment:
                text += f"— {comment[:50]}"
            text += "\n"
            if len(text) > 3800:
                await message.answer(text)
                text = ""
        if text:
            await message.answer(text)


# ---------- ИМПОРТ ГОРОДОВ (ОБНОВЛЁННАЯ ВЕРСИЯ) ----------
@router.message(Command("import_city"))
@admin_only
async def import_city_cmd(message: types.Message):
    parts = parse_args(message, 2, "Использование: /import_city <url>\nПример: /import_city https://fuelprice.ru/moskva")
    if not parts:
        return
    url = parts[1]
    await message.answer(f"🔄 Начинаю импорт города из {url}...", parse_mode=None)
    result = await import_city_from_url(url)
    if "error" in result:
        await message.answer(f"❌ Ошибка: {result['error']}", parse_mode=None)
        return
    text_msg = (
        f"✅ Импорт завершён!\n\n"
        f"🏙 Город: {result['city']}\n"
        f"🔗 Слаг: {result['slug']}\n"
        f"📊 Создано АЗС: {result['stations_created']}\n"
        f"🔄 Обновлено цен: {result['prices_updated']}\n"
        f"🔄 Обновлено адресов: {result.get('addresses_updated', 0)}"
    )
    await message.answer(text_msg, parse_mode=None)


@router.message(Command("import_all_cities"))
@admin_only
async def import_all_cities_cmd(message: types.Message):
    """Импорт всех 65+ городов из списка RUSSIA_CITIES."""
    await message.answer(f"🔄 Начинаю импорт всех {len(RUSSIA_CITIES)} городов из списка... Это может занять до 20-30 минут.")

    results = []
    total_success = 0
    total_fail = 0

    for idx, city_data in enumerate(RUSSIA_CITIES, 1):
        city_name = city_data["name"]
        slug = city_data["slug"]
        url = f"https://fuelprice.ru/{slug}"

        try:
            if idx % 5 == 0 or idx == 1:
                await message.answer(f"⏳ Импорт {idx}/{len(RUSSIA_CITIES)}: {city_name}...")

            res = await import_city_from_url(url)

            if "error" in res:
                results.append(f"❌ {idx}. {city_name} — ошибка: {res['error']}")
                total_fail += 1
            else:
                results.append(
                    f"✅ {idx}. {res['city']} — "
                    f"АЗС: {res['stations_created']}, "
                    f"цен: {res['prices_updated']}, "
                    f"адресов обновлено: {res.get('addresses_updated', 0)}"
                )
                total_success += 1

        except Exception as e:
            logger.error(f"Исключение при импорте {city_name}: {e}")
            results.append(f"❌ {idx}. {city_name} — критическая ошибка: {e}")
            total_fail += 1

        await asyncio.sleep(3)

    report_lines = [
        f"📊 <b>Итоги импорта всех городов из списка:</b>",
        f"✅ Успешно: <b>{total_success}</b>",
        f"❌ Ошибок: <b>{total_fail}</b>",
        "",
        *results
    ]
    report = "\n".join(report_lines)

    for i in range(0, len(report), 4000):
        await message.answer(report[i:i+4000], parse_mode="HTML")


# ---------- ЭКСПОРТ ПОЛЬЗОВАТЕЛЕЙ ----------
@router.message(Command("export_users"))
@admin_only
async def export_users_cmd(message: types.Message):
    await message.answer("🔄 Формирую файл с пользователями... Это может занять время.")
    async with AsyncSessionLocal() as db:
        total = (await db.execute(text("SELECT COUNT(*) FROM users"))).scalar()
        if total == 0:
            await message.answer("❌ Нет пользователей.")
            return

        chunk_size = 500
        offset = 0
        while offset < total:
            rows = await db.execute(text("""
                SELECT id, telegram_id, username, city_id, is_pro, pro_until, created_at
                FROM users
                ORDER BY id
                LIMIT :limit OFFSET :offset
            """), {"limit": chunk_size, "offset": offset})
            rows = rows.mappings().all()
            if not rows:
                break
            csv_lines = []
            if offset == 0:
                csv_lines.append("id,telegram_id,username,city_id,is_pro,pro_until,created_at")
            for r in rows:
                csv_lines.append(f"{r['id']},{r['telegram_id']},{r['username'] or ''},{r['city_id'] or ''},{r['is_pro']},{r['pro_until']},{r['created_at']}")
            file_content = "\n".join(csv_lines)
            if file_content:
                await message.answer_document(
                    document=BufferedInputFile(
                        file_content.encode('utf-8'),
                        filename=f"users_part_{offset//chunk_size + 1}.csv"
                    )
                )
            offset += chunk_size
            await asyncio.sleep(0.5)

    await message.answer("✅ Экспорт завершён.")


# ---------- УСТАНОВКА КООРДИНАТ ГОРОДА ----------
@router.message(Command("set_city_coords"))
@admin_only
async def set_city_coords_cmd(message: types.Message):
    text_msg = message.text.removeprefix("/set_city_coords").strip()
    tokens = text_msg.split()
    if len(tokens) < 3:
        await message.answer(
            "❌ Неверный формат. Используйте:\n"
            "/set_city_coords <город> <lat> <lon>\n"
            "Пример: /set_city_coords Нижний Новгород 56.2965 43.9361"
        )
        return
    try:
        lat, lon = float(tokens[-2]), float(tokens[-1])
    except ValueError:
        await message.answer("❌ Неверный формат координат. Используйте числа с точкой.")
        return
    city_name = " ".join(tokens[:-2])
    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name, include_inactive=True)
        if not city:
            await message.answer(f"❌ Город '{city_name}' не найден в базе.")
            return
        city.latitude, city.longitude = lat, lon
        await commit_or_rollback(db)
        await message.answer(f"✅ Координаты для города '{city_name}' установлены: {lat}, {lon}")


# ---------- РАСШИРЕННАЯ СТАТИСТИКА ----------
@router.message(Command("stats"))
@admin_only
async def show_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    async with AsyncSessionLocal() as db:
        stats = await get_deep_business_metrics(db)

    total_users = stats["total_users"] or 1
    searched_users = stats["first_search_count"] or 0
    pro_users = stats["pro_users_count"] or 0
    total_revenue = stats["total_revenue_rub"] or 0
    referred_users = stats["referred_users_count"] or 0
    total_saved = stats["total_saved_community"] or 0
    total_reports = stats["total_price_reports"] or 0

    conv_to_search = (searched_users / total_users) * 100
    conv_to_pro = (pro_users / searched_users * 100) if searched_users > 0 else 0
    k_factor = referred_users / total_users
    arpu = total_revenue / total_users

    dashboard_text = (
        f"📊 <b>Бизнес-аналитика BinzoLife Dashboard</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего регистраций: <b>{total_users:,}</b>\n"
        f"• Сделали поиск (Active): <b>{searched_users:,}</b> ({conv_to_search:.1f}%)\n"
        f"• Реферальные приглашения: <b>{referred_users:,}</b> (K-factor: {k_factor:.2f})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>Монетизация & Воронка:</b>\n"
        f"• Активных PRO-подписчиков: <b>{pro_users:,}</b>\n"
        f"• Конверсия Search ➔ PRO: <b>{conv_to_pro:.1f}%</b>\n"
        f"• Общая выручка: <b>{total_revenue:,.0f} ₽</b>\n"
        f"• ARPU: <b>{arpu:.1f} ₽ / польз.</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⛽ <b>Экономика комьюнити:</b>\n"
        f"• Сохранено водителями: <b>~{total_saved:,.0f} ₽</b>\n"
        f"• Народных репортов цен: <b>{total_reports:,}</b>\n"
    )
    await message.answer(dashboard_text, parse_mode="HTML")


# ---------- ОЧИСТКА АДРЕСОВ ----------
@router.message(Command("clean_addresses"))
@admin_only
async def clean_addresses_cmd(message: types.Message):
    await message.answer("🔄 Начинаю очистку адресов АЗС...")
    async with AsyncSessionLocal() as db:
        stations = (await db.execute(
            select(Station).where(
                Station.address.contains("<strong>") | Station.address.contains("<br>")
            )
        )).scalars().all()
        if not stations:
            await message.answer("✅ Испорченных адресов не найдено.")
            return
        for station in stations:
            station.address = ""
        await commit_or_rollback(db)
        await message.answer(
            f"✅ Очищено {len(stations)} адресов АЗС.\n\n"
            "Теперь запустите /import_all_cities, чтобы обновить адреса из парсера."
        )


@router.message(Command("clean_all_addresses"))
@admin_only
async def clean_all_addresses_cmd(message: types.Message):
    await message.answer("🔄 Начинаю массовую очистку адресов всех АЗС...")
    async with AsyncSessionLocal() as db:
        stations = await db.execute(select(Station))
        stations = stations.scalars().all()
        count = 0
        for station in stations:
            if station.address:
                cleaned = clean_address(station.address, max_length=255)
                if cleaned and station.address != cleaned:
                    station.address = cleaned
                    count += 1
        await commit_or_rollback(db)
    await message.answer(f"✅ Очищено адресов: {count}")


# ---------- СТАТИСТИКА ПО ГОРОДАМ ----------
@router.message(Command("cities_stats"))
@admin_only
async def cities_stats_cmd(message: types.Message):
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                City.id, City.name, City.latitude, City.longitude,
                func.count(Station.id).filter(Station.is_active == True).label('active_count'),
                func.count(Station.id).label('total_count')
            )
            .outerjoin(Station, Station.city_id == City.id)
            .group_by(City.id, City.name, City.latitude, City.longitude)
            .order_by(City.name)
        )).all()
        if not rows:
            await message.answer("❌ В базе нет городов.")
            return

        stats = []
        total_active = total_all = 0
        for row in rows:
            active, total = row.active_count or 0, row.total_count or 0
            total_active += active
            total_all += total
            stats.append({
                "name": row.name,
                "active": active,
                "total": total,
                "has_coords": row.latitude is not None and row.longitude is not None
            })
        stats.sort(key=lambda x: x["active"], reverse=True)

        text = "🏙 <b>Статистика АЗС по городам</b>\n\n"
        for city in stats:
            coords_icon = "✅" if city["has_coords"] else "❌"
            text += (
                f"📍 <b>{city['name']}</b>\n"
                f"   Активных АЗС: {city['active']}\n"
                f"   Всего АЗС: {city['total']}\n"
                f"   Координаты: {coords_icon}\n\n"
            )
        text += f"📊 <b>Итого:</b> активных АЗС: {total_active}, всего: {total_all}"
        await message.answer(text, parse_mode="HTML")


# ---------- УДАЛЕНИЕ ГОРОДА ----------
@router.message(Command("delete_city"))
@admin_only
async def delete_city_cmd(message: types.Message):
    parts = parse_args(message, 2, "Использование: /delete_city <название>")
    if not parts:
        return
    city_name = parts[1]
    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name, include_inactive=True)
        if not city:
            await message.answer(f"❌ Город '{city_name}' не найден.")
            return
        stations_count = await db.execute(select(func.count(Station.id)).where(Station.city_id == city.id))
        stations_count = stations_count.scalar()
        text = (
            f"⚠️ Вы уверены, что хотите удалить город <b>'{city_name}'</b>?\n"
            f"Будет удалено: {stations_count} АЗС, все цены, отчёты и связанные данные.\n"
            f"Это действие необратимо!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_city_{city.id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete_city")
            ]
        ])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("confirm_delete_city_"))
async def confirm_delete_city(callback: types.CallbackQuery):
    await callback.answer()
    city_id = int(callback.data.split("_")[3])
    async with AsyncSessionLocal() as db:
        city = await db.get(City, city_id)
        if not city:
            await callback.message.edit_text("❌ Город уже удалён или не найден.")
            return
        city_name = city.name
        stations = await db.execute(select(Station.id).where(Station.city_id == city_id))
        station_ids = [row[0] for row in stations.all()]
        if station_ids:
            await db.execute(delete(FuelPrice).where(FuelPrice.station_id.in_(station_ids)))
            await db.execute(delete(AvailabilityReport).where(AvailabilityReport.station_id.in_(station_ids)))
            await db.execute(delete(UserAction).where(UserAction.station_id.in_(station_ids)))
            await db.execute(delete(Notification).where(Notification.station_id.in_(station_ids)))
            await db.execute(delete(Station).where(Station.city_id == city_id))
        await db.execute(delete(CitySlug).where(CitySlug.city_id == city_id))
        await db.delete(city)
        await commit_or_rollback(db)
        await callback.message.edit_text(f"✅ Город <b>'{city_name}'</b> и все его данные успешно удалены.", parse_mode="HTML")


@router.callback_query(F.data == "cancel_delete_city")
async def cancel_delete_city(callback: types.CallbackQuery):
    await callback.answer("Удаление отменено.")
    await callback.message.edit_text("❌ Удаление отменено.")


# ---------- УДАЛЕНИЕ ПУСТЫХ ГОРОДОВ ----------
@router.message(Command("delete_empty_cities"))
@admin_only
async def delete_empty_cities_cmd(message: types.Message):
    await message.answer("🔄 Проверяю города без АЗС...")
    async with AsyncSessionLocal() as db:
        cities = await db.execute(
            select(City)
            .outerjoin(Station, Station.city_id == City.id)
            .group_by(City.id)
            .having(func.count(Station.id) == 0)
        )
        cities = cities.scalars().all()
        if not cities:
            await message.answer("✅ Нет пустых городов.")
            return
        names = ", ".join([c.name for c in cities])
        for city in cities:
            await db.delete(city)
            await db.execute(delete(CitySlug).where(CitySlug.city_id == city.id))
        await commit_or_rollback(db)
        await message.answer(f"✅ Удалены пустые города: {names}")


# ---------- РАБОТА С АДРЕСАМИ ----------
@router.message(Command("stations_without_address"))
@admin_only
async def stations_without_address_cmd(message: types.Message):
    async with AsyncSessionLocal() as db:
        stations = (await db.execute(
            select(Station)
            .options(selectinload(Station.city))
            .where((Station.address.is_(None)) | (Station.address == ""))
        )).scalars().all()
        count = len(stations)
        if count == 0:
            await message.answer("✅ Все станции имеют адрес.")
            return
        text = f"🛢 Найдено {count} станций без адреса:\n\n"
        for station in stations[:20]:
            city_name = station.city.name if station.city else "неизвестен"
            text += f"ID: {station.id} — {station.name} (город {city_name})\n"
        if count > 20:
            text += f"... и ещё {count - 20} станций."
        await message.answer(text, parse_mode="HTML")


@router.message(Command("set_station_address"))
@admin_only
async def set_station_address_cmd(message: types.Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование: /set_station_address <id> <адрес>\n"
            "Пример: /set_station_address 123 ул. Ленина, 15"
        )
        return
    try:
        station_id = int(parts[1])
    except ValueError:
        await message.answer("❌ ID должен быть числом.")
        return
    address = parts[2].strip()
    if not address:
        await message.answer("❌ Адрес не может быть пустым.")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_or_reply(db, station_id, message)
        if not station:
            return
        station.address = address
        await commit_or_rollback(db)
        await message.answer(f"✅ Адрес для станции «{station.name}» (ID {station.id}) установлен:\n{address}")


# ---------- МАССОВАЯ РАССЫЛКА ----------
@router.message(Command("broadcast"))
@admin_only
async def broadcast_cmd(message: types.Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование: /broadcast <сегмент> <текст>\n"
            "Сегменты: all, inactive, no_pro, reporters\n"
            "Пример: /broadcast no_pro Специальное предложение для вас!"
        )
        return
    segment = parts[1]
    text_msg = parts[2]
    async with AsyncSessionLocal() as db:
        users = await get_users_by_segment(db, segment)
        if not users:
            await message.answer(f"Нет пользователей в сегменте '{segment}'.")
            return
        sent = 0
        for user in users:
            try:
                await message.bot.send_message(user.telegram_id, text_msg, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Ошибка отправки broadcast пользователю {user.telegram_id}: {e}")
        await message.answer(f"✅ Рассылка отправлена {sent} пользователям из сегмента '{segment}'.")


# ---------- SELF TEST ----------
@router.message(Command("selftest"))
@admin_only
async def self_test(message: types.Message):
    await message.answer("🔄 Запускаю полную самопроверку... Это может занять до 60 секунд.")
    try:
        result = await run_self_check()
        summary = result.summary()
        if len(summary) > 4000:
            for i in range(0, len(summary), 4000):
                await message.answer(summary[i:i+4000], parse_mode=None)
        else:
            await message.answer(summary, parse_mode=None)
    except Exception as e:
        await message.answer(f"❌ Критическая ошибка: {e}", parse_mode=None)


# ---------- ОЧИСТКА НАЗВАНИЙ АЗС ----------
@router.message(Command("clean_station_names"))
@admin_only
async def clean_station_names_cmd(message: types.Message):
    await message.answer("🔄 Начинаю очистку названий АЗС...")
    async with AsyncSessionLocal() as db:
        stations = await db.execute(select(Station))
        stations = stations.scalars().all()
        count = 0
        for station in stations:
            clean = normalize_name(station.name)
            if clean and station.name != clean:
                station.name = clean
                count += 1
        await commit_or_rollback(db)
    await message.answer(f"✅ Очищено названий: {count}")


# ---------- ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ ДАННЫХ (устаревшие, оставлены для совместимости) ----------
@router.message(Command("refresh_city"))
@admin_only
async def refresh_city_cmd(message: types.Message):
    await message.answer("⚠️ Команда устарела. Используйте /test_multigo для проверки нового парсера.")


@router.message(Command("refresh_all_cities"))
@admin_only
async def refresh_all_cities_cmd(message: types.Message):
    await message.answer("⚠️ Команда устарела. Используйте /test_multigo для проверки нового парсера.")


# ---------- ТЕСТОВЫЙ ПРОИЗВОДСТВЕННЫЙ ТЕСТ ----------
@router.message(Command("test_prod"))
@admin_only
async def test_production_cmd(message: types.Message):
    await message.answer("🔄 Запускаю полную проверку... Это может занять до 30 секунд.")
    try:
        passed, total, results, warnings = await run_all_checks()

        lines = []
        lines.append("<b>🔍 Результат производственного теста BinzoLife</b>")
        lines.append("")
        lines.append(f"<b>📊 Результат: {passed}/{total} проверок пройдено</b>")
        lines.append("")
        for name, ok, msg in results:
            icon = "✅" if ok else "❌"
            if len(msg) > 100:
                msg = msg[:97] + "..."
            lines.append(f"{icon} <b>{name}</b>: {msg}")
        if warnings:
            lines.append("")
            lines.append("<b>⚠️ Предупреждения:</b>")
            for w in warnings:
                lines.append(f"   • {w}")
        lines.append("")
        lines.append("<b>🔍 Рекомендации:</b>")
        lines.append("• Проверьте, что тестовый инвойс пришёл админу")
        lines.append("• Оплатите 1 рубль или 1 Star, чтобы убедиться, что PRO активируется")
        lines.append("• Проверьте работу cron-заданий (цены, уведомления)")
        lines.append("• Проверьте, что /health пингуется каждые 5-10 минут")
        lines.append("• Протестируйте сценарий «Бензин заканчивается!»")
        if passed == total:
            lines.append("")
            lines.append("<b>🎉 Все проверки успешны! Бот готов к продакшену.</b>")
        else:
            lines.append("")
            lines.append("<b>⚠️ Некоторые проверки не пройдены. Исправьте ошибки перед запуском.</b>")

        report = "\n".join(lines)
        for i in range(0, len(report), 4000):
            await message.answer(report[i:i+4000], parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка при выполнении теста: {str(e)[:200]}")


# ---------- УДАЛЕНИЕ ТЕСТОВЫХ ДАННЫХ ----------
@router.message(Command("clean_test_data"))
@admin_only
async def clean_test_data_cmd(message: types.Message):
    await message.answer("🔄 Начинаю расширенную очистку тестовых данных... Это может занять несколько секунд.")

    async with AsyncSessionLocal() as db:
        async with db.begin():
            test_patterns = ["test", "expiring", "expired", "expiry", "demo", "tester"]
            conditions = [User.telegram_id < 0]
            for pattern in test_patterns:
                conditions.append(User.username.ilike(f"%{pattern}%"))

            test_users = await db.execute(
                select(User).where(or_(*conditions))
            )
            test_users = test_users.scalars().all()
            test_user_ids = [u.id for u in test_users]

            deleted_actions = deleted_notifications = deleted_achievements = 0
            deleted_referrals = deleted_economies = deleted_reviews = 0
            deleted_payments = 0
            deleted_test_users_count = len(test_users)

            if test_user_ids:
                result = await db.execute(
                    delete(Notification).where(Notification.user_id.in_(test_user_ids))
                )
                deleted_notifications = result.rowcount
                result = await db.execute(
                    delete(UserAction).where(UserAction.user_id.in_(test_user_ids))
                )
                deleted_actions = result.rowcount
                result = await db.execute(
                    delete(UserAchievement).where(UserAchievement.user_id.in_(test_user_ids))
                )
                deleted_achievements = result.rowcount
                result = await db.execute(
                    delete(Referral).where(
                        or_(
                            Referral.referrer_id.in_(test_user_ids),
                            Referral.referred_user_id.in_(test_user_ids)
                        )
                    )
                )
                deleted_referrals = result.rowcount
                result = await db.execute(
                    delete(UserEconomy).where(UserEconomy.user_id.in_(test_user_ids))
                )
                deleted_economies = result.rowcount
                result = await db.execute(
                    delete(Review).where(Review.user_id.in_(test_user_ids))
                )
                deleted_reviews = result.rowcount
                result = await db.execute(
                    delete(Payment).where(Payment.user_id.in_(test_user_ids))
                )
                deleted_payments += result.rowcount
                await db.execute(
                    delete(User).where(User.id.in_(test_user_ids))
                )
                logger.info(f"Удалено {deleted_test_users_count} тестовых пользователей")

            admin_user = await db.execute(
                select(User).where(User.telegram_id == int(settings.ADMIN_IDS[0] if settings.ADMIN_IDS else 0))
            )
            admin_user = admin_user.scalar_one_or_none()
            deleted_admin_reviews = 0
            if admin_user:
                result = await db.execute(
                    delete(Review).where(Review.user_id == admin_user.id)
                )
                deleted_admin_reviews = result.rowcount
                logger.info(f"Удалено {deleted_admin_reviews} отзывов администратора")

            result = await db.execute(
                delete(Payment).where(Payment.tariff == 'test')
            )
            deleted_test_payments = result.rowcount
            deleted_payments += deleted_test_payments
            logger.info(f"Удалено {deleted_test_payments} тестовых платежей")

            await db.execute(
                text("UPDATE users SET total_saved = 0 WHERE total_saved < 0")
            )

            await commit_or_rollback(db)

    await message.answer(
        "✅ Расширенная очистка тестовых данных завершена!\n\n"
        f"🧹 Удалено тестовых пользователей: {deleted_test_users_count}\n"
        f"   • Уведомлений: {deleted_notifications}\n"
        f"   • Действий: {deleted_actions}\n"
        f"   • Достижений: {deleted_achievements}\n"
        f"   • Рефералов: {deleted_referrals}\n"
        f"   • Записей экономии: {deleted_economies}\n"
        f"   • Отзывов: {deleted_reviews}\n"
        f"   • Платежей: {deleted_payments}\n"
        f"🗑 Удалено отзывов администратора: {deleted_admin_reviews}\n"
        f"🗑 Удалено тестовых платежей (tariff='test'): {deleted_test_payments}\n\n"
        "Теперь база данных полностью чиста. Можно запускать рекламу! 🚀"
    )


# ========== АДМИН-КОМАНДА ДЛЯ ПРИНУДИТЕЛЬНОГО СИДИНГА ==========
@router.message(Command("seed_cities"))
@admin_only
async def admin_seed_cities_cmd(message: types.Message):
    await message.answer("⏳ Запускаю импорт всех 65+ городов России...")
    try:
        async with AsyncSessionLocal() as session:
            from database.crud import seed_all_russian_cities
            added = await seed_all_russian_cities(session)
            total = await session.execute(text("SELECT COUNT(*) FROM cities WHERE is_active = true"))
            with_slugs = await session.execute(text("SELECT COUNT(*) FROM cities WHERE slug IS NOT NULL AND slug != ''"))
            await message.answer(
                f"✅ <b>Синхронизация городов завершена!</b>\n\n"
                f"• Добавлено новых городов: <b>{added}</b>\n"
                f"• Всего городов в БД: <b>{total.scalar()}</b>\n"
                f"• Городов со слагами: <b>{with_slugs.scalar()}</b>",
                parse_mode="HTML"
            )
    except Exception as e:
        await message.answer(f"❌ Ошибка сидинга: {e}")


# ========== ГЕОКОДИРОВАНИЕ АДРЕСОВ ==========
@router.message(Command("fix_addresses"))
@admin_only
async def admin_fix_addresses(message: types.Message):
    """
    Команда для запуска геокодирования АЗС без адресов.
    Использование: /fix_addresses [лимит, по умолчанию 50]
    """
    parts = message.text.split()
    limit = 50
    if len(parts) > 1 and parts[1].isdigit():
        limit = min(int(parts[1]), 250)

    status_msg = await message.answer(
        f"⏳ <b>Геокодирование запущено...</b>\n"
        f"Ищем до {limit} АЗС без адресов и определяем их координаты.\n"
        f"<i>Пожалуйста, подождите (1-2 сек на станцию)...</i>",
        parse_mode="HTML"
    )

    try:
        async with AsyncSessionLocal() as session:
            updated, found = await fill_missing_station_addresses(session, limit=limit)
        await status_msg.edit_text(
            f"✅ <b>Геокодирование завершено!</b>\n\n"
            f"• Найдено АЗС без адреса: <b>{found}</b>\n"
            f"• Успешно определено и сохранено: <b>{updated}</b>\n\n"
            f"<i>Если станций без адреса больше {limit}, запустите команду ещё раз.</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка во время геокодирования: {e}")


# ========== ТЕСТ MULTIGO ==========
@router.message(Command("test_multigo"))
@admin_only
async def test_multigo_cmd(message: types.Message):
    """Тестирует MultiGo API на указанном городе. Использование: /test_multigo [город]"""
    args = message.text.split(maxsplit=1)
    city_name = args[1] if len(args) > 1 else "Москва"

    async with AsyncSessionLocal() as session:
        city = await get_city_by_name(session, city_name)
        if not city:
            await message.answer(f"❌ Город '{city_name}' не найден.")
            return
        if city.latitude is None or city.longitude is None:
            await message.answer(f"❌ У города '{city_name}' нет координат.")
            return

    await message.answer(f"⏳ Тестируем MultiGo для города {city.name}...")

    try:
        stations = await multigo_parser.fetch_stations_in_bbox(city.latitude, city.longitude)
        if not stations:
            await message.answer(f"⚠️ Не найдено станций для {city.name} через MultiGo.")
            return

        async with AsyncSessionLocal() as session:
            updated_stations, updated_prices = await multigo_parser.sync_stations_batch(
                session, city.id, stations
            )

        await message.answer(
            f"✅ <b>MultiGo тест завершён!</b>\n\n"
            f"🏙 Город: {city.name}\n"
            f"📍 Найдено станций: {len(stations)}\n"
            f"💾 Сохранено новых станций: {updated_stations}\n"
            f"💾 Сохранено цен: {updated_prices}\n\n"
            f"<i>Проверьте базу данных.</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")


# ========== ТЕСТ САНИТАЙЗЕРА ==========
@router.message(Command("test_sanitizer"))
@admin_only
async def test_sanitizer_cmd(message: types.Message):
    """Тестирует санитайзер на примере данных."""
    from services.data_sanitizer import clean_brand_name, validate_fuel_price, normalize_address

    test_data = [
        ("ООО Газпромнефть-Центр АЗС №44", "Газпромнефть"),
        ("Татнефть-АЗС-Запад №12", "Татнефть"),
        ("ЛУКОЙЛ-Уралнефтепродукт", "Лукойл"),
        ("Неизвестный бренд", "Азс"),
        ("Сургутнефтегаз", "Сургутнефтегаз"),
    ]

    result_lines = ["🧹 <b>Тест санитайзера</b>\n"]
    for raw, expected in test_data:
        cleaned = clean_brand_name(raw)
        status = "✅" if cleaned == expected else "❌"
        result_lines.append(f"{status} «{raw}» → «{cleaned}» (ожидалось «{expected}»)")

    price_tests = [
        ("АИ-95", 54.50, 54.50),
        ("АИ-95", 5.40, None),
        ("АИ-95", 999.0, None),
        ("ГАЗ", 30.0, 30.0),
        ("ГАЗ", 50.0, None),
    ]

    result_lines.append("\n🔢 <b>Тест валидации цен</b>")
    for fuel, price, expected in price_tests:
        validated = validate_fuel_price(fuel, price)
        status = "✅" if validated == expected else "❌"
        result_lines.append(f"{status} {fuel} {price} → {validated} (ожидалось {expected})")

    addr_tests = [
        ("  г.   Казань,   ул. Чистопольская,   43  ", "г. Казань, ул. Чистопольская, 43"),
    ]
    result_lines.append("\n📍 <b>Тест нормализации адресов</b>")
    for raw, expected in addr_tests:
        cleaned = normalize_address(raw)
        status = "✅" if cleaned == expected else "❌"
        result_lines.append(f"{status} «{raw}» → «{cleaned}» (ожидалось «{expected}»)")

    await message.answer("\n".join(result_lines), parse_mode="HTML")
