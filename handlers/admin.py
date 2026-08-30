import csv
import io
import re
import asyncio
import logging
import os
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
    get_marketing_stats, get_all_active_cities
)
from database.models import (
    SourceType, AvailabilityStatus, FuelType, Station, City, CitySlug,
    FuelPrice, AvailabilityReport, UserAction, Notification,
    UserAchievement, Referral, UserEconomy, Review, Payment, User
)
from services.city_importer import import_city_from_url
from services.selfcheck import run_self_check
from services.fuelprice_parser import normalize_name, fetch_fuelprice_prices
from utils.cleaners import clean_address

# ========== ИМПОРТЫ ДЛЯ /test_prod ==========
from test_production import run_all_checks
# =============================================

router = Router()
logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids

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

# ---------- Стандартные админ-команды ----------
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
                await db.commit()
                await message.answer(f"Город {name} реактивирован.")
            else:
                await message.answer(f"Город {name} уже существует.")
        else:
            db.add(City(name=name, region=region))
            await db.commit()
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
        station_id, price = int(parts[1]), float(parts[2])
    except ValueError:
        await message.answer("Неверный формат.")
        return
    if price <= 0:
        await message.answer("❌ Цена должна быть больше 0.")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_or_reply(db, station_id, message)
        if not station:
            return
        await save_price(db, station_id, FuelType.AI_95, price, SourceType.ADMIN, confidence=0.9)
        await message.answer(f"Цена для АЗС {station.name} обновлена: {price} ₽")

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
            await db.commit()
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
            username = rev.user.username or f"User{rev.user.telegram_id}"
            text += f"{i}. {username}: {rev.rating}⭐ "
            if rev.comment:
                text += f"— {rev.comment[:50]}"
            text += "\n"
            if len(text) > 3800:
                await message.answer(text)
                text = ""
        if text:
            await message.answer(text)

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
    text = (
        f"✅ Импорт завершён!\n\n"
        f"🏙 Город: {result['city']}\n"
        f"🔗 Слаг: {result['slug']}\n"
        f"📊 Создано АЗС: {result['stations_created']}\n"
        f"🔄 Обновлено цен: {result['prices_updated']}\n"
        f"🔄 Обновлено адресов: {result.get('addresses_updated', 0)}"
    )
    await message.answer(text, parse_mode=None)

@router.message(Command("import_all_cities"))
@admin_only
async def import_all_cities_cmd(message: types.Message):
    city_urls = [
        "https://fuelprice.ru/moskva",
        "https://fuelprice.ru/novosibirsk",
        "https://fuelprice.ru/ekaterinburg",
        "https://fuelprice.ru/nizhniy-novgorod",
        "https://fuelprice.ru/kazan",
        "https://fuelprice.ru/chelyabinsk",
        "https://fuelprice.ru/omsk",
        "https://fuelprice.ru/samara",
        "https://fuelprice.ru/rostov-na-donu",
        "https://fuelprice.ru/ufa",
        "https://fuelprice.ru/perm",
        "https://fuelprice.ru/voronezh",
        "https://fuelprice.ru/volgograd",
        "https://fuelprice.ru/tula",
    ]
    await message.answer(f"🔄 Начинаю импорт всех {len(city_urls)} городов...", parse_mode=None)
    results = []
    for url in city_urls:
        try:
            res = await import_city_from_url(url)
            if "error" in res:
                results.append(f"❌ {url} — ошибка: {res['error']}")
            else:
                results.append(f"✅ {res['city']} — создано АЗС: {res['stations_created']}, цен: {res['prices_updated']}, адресов обновлено: {res.get('addresses_updated', 0)}")
        except Exception as e:
            results.append(f"❌ {url} — исключение: {e}")
    report = "📊 Итоги импорта всех городов:\n\n" + "\n".join(results)
    for i in range(0, len(report), 4000):
        await message.answer(report[i:i+4000], parse_mode=None)

@router.message(Command("set_city_coords"))
@admin_only
async def set_city_coords_cmd(message: types.Message):
    text = message.text.removeprefix("/set_city_coords").strip()
    tokens = text.split()
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
        await db.commit()
        await message.answer(f"✅ Координаты для города '{city_name}' установлены: {lat}, {lon}")

# ---------- СТАТИСТИКА ----------
@router.message(Command("stats"))
@admin_only
async def show_stats(message: types.Message):
    async with AsyncSessionLocal() as db:
        stats = await get_marketing_stats(db)

    user = stats["user_stats"]
    payment = stats["payment_stats"]
    review = stats["review_stats"]
    referral = stats["referral_stats"]
    funnel = stats["funnel"]
    top_cities = stats["top_cities"]
    feature = stats["feature_usage"]
    pro = stats["pro_retention"]
    arpu = stats["arpu"]
    ltv = stats["ltv"]
    conversion = stats["conversion_search_to_pro"]

    text = "📊 <b>Статистика BinzoLife</b>\n\n"

    text += "👥 <b>Пользователи</b>\n"
    text += f"▪ Всего: {user['total_users']}\n"
    text += f"▪ Активных за 7 дней: {user['active_users_7d']} ({round(user['active_users_7d']/user['total_users']*100, 1) if user['total_users'] else 0}%)\n"
    text += f"▪ Сделали поиск: {user['have_searches']} ({round(user['have_searches']/user['total_users']*100, 1) if user['total_users'] else 0}%)\n"
    text += f"▪ Активных PRO: {user['active_pro']}\n"
    text += f"▪ Новых сегодня: {user['new_today']}\n"
    text += f"▪ За неделю: {user['new_week']}\n"
    text += f"▪ За месяц: {user['new_month']}\n\n"

    text += "📍 <b>Топ-5 городов по пользователям</b>\n"
    for i, city in enumerate(top_cities, 1):
        text += f"{i}. {city['city']} — {city['users']}\n"
    text += "\n"

    text += "💳 <b>Платежи</b>\n"
    text += f"▪ Всего оплат: {payment['total_payments']}\n"
    text += f"▪ Выручка: {payment['total_revenue']:.2f} ₽\n"
    text += f"▪ ARPU (PRO): {arpu:.2f} ₽\n"
    text += f"▪ Конверсия поиск → PRO: {conversion}%\n"
    text += f"▪ Средний LTV (оценка): {ltv:.2f} ₽\n"
    text += f"▪ Сегодня: {payment['payments_today']} шт. ({payment['revenue_today']:.2f} ₽)\n"
    text += f"▪ За неделю: {payment['payments_week']} шт. ({payment['revenue_week']:.2f} ₽)\n"
    text += f"▪ За месяц: {payment['payments_month']} шт. ({payment['revenue_month']:.2f} ₽)\n\n"

    text += "👑 <b>PRO-пользователи</b>\n"
    text += f"▪ Активных: {pro['active_pro']}\n"
    text += f"▪ Продлили подписку: {pro['renewed']} ({round(pro['renewed']/pro['active_pro']*100, 1) if pro['active_pro'] else 0}% от активных)\n"
    text += f"▪ Отписалось за месяц: {pro['churned']} (churn {pro['churn_rate']}%)\n"
    text += f"▪ PRO по рефералам (оценка): {referral['rewarded']} ({round(referral['rewarded']/pro['active_pro']*100, 1) if pro['active_pro'] else 0}%)\n\n"

    text += "🔄 <b>Воронка (от первого поиска)</b>\n"
    stage_names = {0: "До первого поиска", 1: "1 день", 2: "3 дня", 3: "7 дней", 4: "14 дней", 5: "Завершено"}
    counts = funnel["stages"]
    convs = funnel["conversions"]
    for i, stage in enumerate([0,1,2,3,4,5]):
        name = stage_names.get(stage, str(stage))
        count = counts[i] if i < len(counts) else 0
        if i == 0:
            text += f"▪ {name}: {count} (100%)\n"
        else:
            conv = convs[i-1] if i-1 < len(convs) else 0
            text += f"▪ {name}: {count} ({conv}% от предыдущего этапа)\n"
    if len(convs) > 1:
        drop = round(100 - convs[1], 1) if convs[1] is not None else 0
        text += f"⚠️ Самый сильный отвал — между 1 и 3 днём ({drop}%)\n\n"

    text += f"⭐ <b>Отзывы</b>: {review['total_reviews']}, ср. рейтинг {review['avg_rating']}⭐\n\n"

    text += f"👥 <b>Рефералы</b>: всего {referral['total_referrals']}, получили бонус {referral['rewarded']} ({round(referral['rewarded']/referral['total_referrals']*100, 1) if referral['total_referrals'] else 0}%)\n\n"

    text += "⚙️ <b>Популярные фичи (за 7 дней)</b>\n"
    total_actions = sum(feature.values())
    if total_actions:
        for action, count in feature.items():
            percent = round(count / total_actions * 100, 1)
            text += f"▪ {action.replace('_', ' ').title()}: {count} ({percent}%)\n"
    else:
        text += "▪ Нет данных\n"

    text += "\n⚠️ <b>Ошибки (за 7 дней)</b>: пока не собираются (добавьте логирование)\n"

    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await message.answer(text[i:i+4000], parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")

# ---------- Очистка адресов ----------
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
        await db.commit()
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
        await db.commit()
    await message.answer(f"✅ Очищено адресов: {count}")

# ---------- Статистика по городам ----------
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

# ---------- Удаление города ----------
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
        await db.commit()
        await callback.message.edit_text(f"✅ Город <b>'{city_name}'</b> и все его данные успешно удалены.", parse_mode="HTML")

@router.callback_query(F.data == "cancel_delete_city")
async def cancel_delete_city(callback: types.CallbackQuery):
    await callback.answer("Удаление отменено.")
    await callback.message.edit_text("❌ Удаление отменено.")

# ---------- Удаление пустых городов ----------
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
        await db.commit()
        await message.answer(f"✅ Удалены пустые города: {names}")

# ---------- Работа с адресами ----------
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
        await db.commit()
        await message.answer(f"✅ Адрес для станции «{station.name}» (ID {station.id}) установлен:\n{address}")

# ---------- Массовая рассылка ----------
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
    text = parts[2]
    async with AsyncSessionLocal() as db:
        users = await get_users_by_segment(db, segment)
        if not users:
            await message.answer(f"Нет пользователей в сегменте '{segment}'.")
            return
        sent = 0
        for user in users:
            try:
                await message.bot.send_message(user.telegram_id, text, parse_mode="HTML")
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
        await db.commit()
    await message.answer(f"✅ Очищено названий: {count}")

# ---------- ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ ДАННЫХ ДЛЯ ГОРОДА ----------
@router.message(Command("refresh_city"))
@admin_only
async def refresh_city_cmd(message: types.Message):
    parts = parse_args(message, 2, "Использование: /refresh_city <название_города>\nПример: /refresh_city Москва")
    if not parts:
        return
    city_name = parts[1]
    await message.answer(f"🔄 Начинаю обновление данных для города {city_name}...")
    try:
        await fetch_fuelprice_prices(city_name)
        await message.answer(f"✅ Обновление для {city_name} завершено.")
    except Exception as e:
        await message.answer(f"❌ Ошибка при обновлении {city_name}: {e}")

# ---------- ПРИНУДИТЕЛЬНОЕ ОБНОВЛЕНИЕ ДАННЫХ ДЛЯ ВСЕХ ГОРОДОВ ----------
@router.message(Command("refresh_all_cities"))
@admin_only
async def refresh_all_cities_cmd(message: types.Message):
    await message.answer("🔄 Начинаю обновление данных для всех городов... Это может занять несколько минут.")
    async with AsyncSessionLocal() as db:
        cities = await get_all_active_cities(db)
        if not cities:
            await message.answer("❌ Нет активных городов.")
            return
    results = []
    for city in cities:
        try:
            await fetch_fuelprice_prices(city.name)
            results.append(f"✅ {city.name} — обновлён")
        except Exception as e:
            results.append(f"❌ {city.name} — ошибка: {e}")
    report = "📊 Итоги обновления:\n\n" + "\n".join(results)
    for i in range(0, len(report), 4000):
        await message.answer(report[i:i+4000], parse_mode=None)

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

# ====================================================================
# РАСШИРЕННАЯ КОМАНДА: /clean_test_data – удаление ВСЕХ тестовых данных
# ====================================================================
@router.message(Command("clean_test_data"))
@admin_only
async def clean_test_data_cmd(message: types.Message):
    """Удаляет ВСЕХ тестовых пользователей (telegram_id<0 или username содержит test/expiring/expired/demo и т.д.), отзывы, тестовые платежи."""
    await message.answer("🔄 Начинаю расширенную очистку тестовых данных... Это может занять несколько секунд.")

    async with AsyncSessionLocal() as db:
        async with db.begin():
            # 1. Находим ВСЕХ тестовых пользователей
            # Критерии: telegram_id < 0 ИЛИ username содержит test/expiring/expired/demo/tester
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
                # Удаляем связанные данные
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
                # Удаляем самих пользователей
                await db.execute(
                    delete(User).where(User.id.in_(test_user_ids))
                )
                logger.info(f"Удалено {deleted_test_users_count} тестовых пользователей")

            # 2. Удаляем отзывы от администратора (если вы их писали)
            admin_user = await db.execute(
                select(User).where(User.telegram_id == int(settings.ADMIN_ID))
            )
            admin_user = admin_user.scalar_one_or_none()
            deleted_admin_reviews = 0
            if admin_user:
                result = await db.execute(
                    delete(Review).where(Review.user_id == admin_user.id)
                )
                deleted_admin_reviews = result.rowcount
                logger.info(f"Удалено {deleted_admin_reviews} отзывов администратора")

            # 3. Удаляем тестовые платежи (tariff='test')
            result = await db.execute(
                delete(Payment).where(Payment.tariff == 'test')
            )
            deleted_test_payments = result.rowcount
            deleted_payments += deleted_test_payments
            logger.info(f"Удалено {deleted_test_payments} тестовых платежей")

            # 4. Сбрасываем отрицательные total_saved
            await db.execute(
                text("UPDATE users SET total_saved = 0 WHERE total_saved < 0")
            )

            await db.commit()

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
