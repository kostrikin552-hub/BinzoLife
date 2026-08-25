import csv
import io
import re
import asyncio
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload
from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, get_user, get_station_by_id,
    deactivate_station, activate_pro, get_station_by_name_address, get_all_reviews,
    get_avg_rating, set_city_slug, save_availability_report_with_consensus,
    get_user_stats, get_payment_stats, get_funnel_stats,
    get_review_stats, get_referral_stats
)
from database.models import (
    SourceType, AvailabilityStatus, FuelType, Station, City, CitySlug,
    FuelPrice, AvailabilityReport, UserAction, Notification
)
from services.city_importer import import_city_from_url

router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids

# ---------- Декоратор для админ-команд ----------
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

# ---------- Вспомогательные функции ----------
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

                station = await get_station_by_name_address(db, city.id, name, address)
                if not station:
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

# ---------- Импорт города по URL ----------
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

# ---------- Статистика ----------
@router.message(Command("stats"))
@admin_only
async def show_stats(message: types.Message):
    async with AsyncSessionLocal() as db:
        user_stats = await get_user_stats(db)
        payment_stats = await get_payment_stats(db)
        funnel_stats = await get_funnel_stats(db)
        review_stats = await get_review_stats(db)
        referral_stats = await get_referral_stats(db)

    text = "📊 <b>Статистика BinzoLife</b>\n\n"
    text += "👥 <b>Пользователи</b>\n"
    text += f"▪ Всего: {user_stats['total_users']}\n"
    text += f"▪ Активных за 7 дней: {user_stats['active_users_7d']}\n"
    text += f"▪ Сделали хотя бы один поиск: {user_stats['have_searches']}\n"
    text += f"▪ Активных PRO: {user_stats['active_pro']}\n"
    text += f"▪ Новых сегодня: {user_stats['new_today']}\n"
    text += f"▪ Новых за неделю: {user_stats['new_week']}\n"
    text += f"▪ Новых за месяц: {user_stats['new_month']}\n\n"

    text += "💳 <b>Платежи</b>\n"
    text += f"▪ Всего оплат: {payment_stats['total_payments']}\n"
    text += f"▪ Общая выручка: {payment_stats['total_revenue']:.2f} ₽\n"
    text += f"▪ Сегодня: {payment_stats['payments_today']} шт. ({payment_stats['revenue_today']:.2f} ₽)\n"
    text += f"▪ За неделю: {payment_stats['payments_week']} шт. ({payment_stats['revenue_week']:.2f} ₽)\n"
    text += f"▪ За месяц: {payment_stats['payments_month']} шт. ({payment_stats['revenue_month']:.2f} ₽)\n\n"

    text += "🔄 <b>Воронка</b>\n"
    total_funnel = sum(funnel_stats.values())
    stage_names = {
        0: "❌ Не начали поиск",
        1: "👋 1 день после первого поиска",
        2: "📊 3 дня",
        3: "⚠️ 7 дней",
        4: "🎁 14 дней",
        5: "💤 Завершено"
    }
    if total_funnel:
        for stage, count in funnel_stats.items():
            name = stage_names.get(stage, f"Стадия {stage}")
            percent = round(count / total_funnel * 100, 1)
            text += f"▪ {name}: {count} ({percent}%)\n"
    else:
        text += "▪ Нет данных по воронке (пользователи не совершали поиск)\n"
    text += "\n"

    text += "⭐ <b>Отзывы</b>\n"
    text += f"▪ Всего: {review_stats['total_reviews']}\n"
    text += f"▪ Средний рейтинг: {review_stats['avg_rating']}⭐\n\n"

    text += "👥 <b>Рефералы</b>\n"
    text += f"▪ Всего приглашённых: {referral_stats['total_referrals']}\n"
    text += f"▪ Получили бонус: {referral_stats['rewarded']}\n"

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

# ---------- Удаление города (с каскадным удалением всех зависимостей) ----------
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

        # 1. Получаем все станции города
        stations = await db.execute(select(Station.id).where(Station.city_id == city_id))
        station_ids = [row[0] for row in stations.all()]

        if station_ids:
            # 2. Удаляем цены
            await db.execute(delete(FuelPrice).where(FuelPrice.station_id.in_(station_ids)))
            # 3. Удаляем availability_reports
            await db.execute(delete(AvailabilityReport).where(AvailabilityReport.station_id.in_(station_ids)))
            # 4. Удаляем user_actions
            await db.execute(delete(UserAction).where(UserAction.station_id.in_(station_ids)))
            # 5. Удаляем notifications
            await db.execute(delete(Notification).where(Notification.station_id.in_(station_ids)))
            # 6. Удаляем станции
            await db.execute(delete(Station).where(Station.city_id == city_id))

        # 7. Удаляем слаг (если есть) до удаления города
        await db.execute(delete(CitySlug).where(CitySlug.city_id == city_id))
        # 8. Удаляем сам город
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

# ---------- КОМАНДЫ ДЛЯ РАБОТЫ С АДРЕСАМИ ----------
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
from services.selfcheck import run_self_check

@router.message(Command("selftest"))
@admin_only
async def self_test(message: types.Message):
    """Запуск расширенной самопроверки всех систем"""
    await message.answer("🔄 Запускаю полную самопроверку... Это может занять до 60 секунд.")
    try:
        result = await run_self_check()
        summary = result.summary()
        # Отправляем частями, если длинное
        if len(summary) > 4000:
            for i in range(0, len(summary), 4000):
                await message.answer(summary[i:i+4000], parse_mode="Markdown")
        else:
            await message.answer(summary, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Критическая ошибка при выполнении самопроверки: {e}")
