import csv
import io
import asyncio
import re
from aiogram import Router, types, F
from aiogram.filters import Command
from sqlalchemy import select, func
from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, get_user, get_station_by_id,
    deactivate_station, activate_pro, get_station_by_name_address, get_all_reviews,
    get_avg_rating, set_city_slug, save_availability_report_with_consensus,
    get_user_stats, get_payment_stats, get_funnel_stats,
    get_review_stats, get_referral_stats
)
from database.models import SourceType, AvailabilityStatus, FuelType, Station, City
from services.city_importer import import_city_from_url

router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids

# ---------- Стандартные админ-команды ----------
@router.message(Command("add_city"))
async def add_city_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /add_city Название [Регион]")
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
            new_city = City(name=name, region=region)
            db.add(new_city)
            await db.commit()
            await message.answer(f"Город {name} добавлен.")

@router.message(Command("set_slug"))
async def set_slug_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /set_slug Город slug")
        return
    city_name = parts[1]
    slug = parts[2]
    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name)
        if not city:
            await message.answer(f"Город {city_name} не найден.")
            return
        await set_city_slug(db, city.id, slug)
        await message.answer(f"Слаг для {city_name} установлен: {slug}")

@router.message(Command("add_station"))
async def add_station_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = [p.strip() for p in message.text.split("|")]
    if len(parts) < 6:
        await message.answer("Использование: /add_station Город | Название | Адрес | lat | lon [Бренд]")
        return
    try:
        city_name = parts[1]
        name = parts[2]
        address = parts[3]
        lat = float(parts[4])
        lon = float(parts[5])
        brand = parts[6] if len(parts) > 6 else None
    except ValueError:
        await message.answer("Неверные координаты.")
        return
    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name)
        if not city:
            await message.answer(f"Город '{city_name}' не найден. Создайте его через /add_city")
            return
        existing = await get_station_by_name_address(db, city.id, name, address)
        if existing:
            await message.answer(f"АЗС с таким названием и адресом уже существует (ID {existing.id}).")
            return
        station = await create_station(db, city.id, name, address, lat, lon, brand)
        await message.answer(f"АЗС {station.name} добавлена (ID {station.id})")

@router.message(Command("set_price"))
async def set_price_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /set_price station_id price")
        return
    try:
        station_id = int(parts[1])
        price = float(parts[2])
    except ValueError:
        await message.answer("Неверный формат.")
        return
    if price <= 0:
        await message.answer("❌ Цена должна быть больше 0.")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            await message.answer("АЗС не найдена.")
            return
        fuel = FuelType.AI_95
        await save_price(db, station_id, fuel, price, SourceType.ADMIN, confidence=0.9)
        await message.answer(f"Цена для АЗС {station.name} обновлена: {price} ₽")

@router.message(Command("set_availability"))
async def set_availability_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /set_availability station_id status (GREEN/YELLOW/RED/GRAY)")
        return
    try:
        station_id = int(parts[1])
        status_str = parts[2].upper()
        if status_str not in ["GREEN", "YELLOW", "RED", "GRAY"]:
            raise ValueError
        status = AvailabilityStatus[status_str]
    except (ValueError, KeyError):
        await message.answer("❌ Неверный статус. Допустимые: GREEN, YELLOW, RED, GRAY")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            await message.answer("АЗС не найдена.")
            return
        fuel = FuelType.AI_95
        await save_availability_report_with_consensus(
            db, station_id, fuel, status, SourceType.ADMIN, confidence=0.9
        )
        await message.answer(f"Статус наличия для {station.name} установлен: {status.value}")

@router.message(Command("deactivate_station"))
async def deactivate_station_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /deactivate_station station_id")
        return
    try:
        station_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный ID.")
        return
    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            await message.answer("АЗС не найдена.")
            return
        await deactivate_station(db, station_id)
        await message.answer(f"АЗС {station.name} деактивирована.")

@router.message(Command("import_csv"))
async def import_csv_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    await message.answer("Пришлите CSV-файл с колонками: city,name,brand,address,lat,lon,price,status")

@router.message(F.document)
async def handle_csv_file(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    if not message.document.file_name.endswith('.csv'):
        await message.answer("Пожалуйста, отправьте файл в формате CSV.")
        return
    file_id = message.document.file_id
    file = await message.bot.get_file(file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    content = file_bytes.read().decode('utf-8-sig')
    sniffer = csv.Sniffer()
    dialect = sniffer.sniff(content[:1024])
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    
    async with AsyncSessionLocal() as db:
        async with db.begin():
            for row in reader:
                if not any(row.values()):
                    continue
                try:
                    city_name = row.get("city", "").strip()
                    name = row.get("name", "").strip()
                    brand = row.get("brand", "").strip() or None
                    address = row.get("address", "").strip()
                    if not city_name or not name or not address:
                        continue
                    lat = float(row.get("lat", 0)) if row.get("lat", "").strip() else 0.0
                    lon = float(row.get("lon", 0)) if row.get("lon", "").strip() else 0.0
                    price = float(row.get("price", 0)) if row.get("price", "").strip() else 0.0
                    status_str = row.get("status", "GRAY").strip().upper()
                    try:
                        status = AvailabilityStatus[status_str]
                    except KeyError:
                        status = AvailabilityStatus.GRAY
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
                    station = await create_station(db, city.id, name, address, lat, lon, brand)
                
                if price > 0:
                    await save_price(db, station.id, FuelType.AI_95, price, SourceType.ADMIN, confidence=0.8)
                await save_availability_report_with_consensus(
                    db, station.id, FuelType.AI_95, status, SourceType.ADMIN, confidence=0.8
                )
        await message.answer("CSV импортирован успешно.")

@router.message(Command("set_pro"))
async def set_pro_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /set_pro telegram_id days (0 - отключить)")
        return
    try:
        telegram_id = int(parts[1])
        days = int(parts[2])
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
async def show_reviews(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return
    
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
            text += f"\n"
            if len(text) > 3800:
                await message.answer(text)
                text = ""
        if text:
            await message.answer(text)

# ---------- Импорт города по URL ----------
@router.message(Command("import_city"))
async def import_city_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /import_city <url>\nПример: /import_city https://fuelprice.ru/moskva")
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

# ---------- Импорт всех городов ----------
@router.message(Command("import_all_cities"))
async def import_all_cities_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

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

    await message.answer(f"🔄 Начинаю импорт всех {len(city_urls)} городов. Это может занять несколько минут...", parse_mode=None)

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
    if len(report) > 4000:
        for i in range(0, len(report), 4000):
            await message.answer(report[i:i+4000], parse_mode=None)
    else:
        await message.answer(report, parse_mode=None)

# ---------- Установка координат ----------
@router.message(Command("set_city_coords"))
async def set_city_coords_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

    text = message.text
    cmd = "/set_city_coords"
    if text.startswith(cmd):
        text = text[len(cmd):].strip()

    tokens = text.split()
    if len(tokens) < 3:
        await message.answer(
            "❌ Неверный формат. Используйте:\n"
            "/set_city_coords <город> <lat> <lon>\n"
            "Пример: /set_city_coords Нижний Новгород 56.2965 43.9361"
        )
        return

    try:
        lat = float(tokens[-2])
        lon = float(tokens[-1])
    except ValueError:
        await message.answer("❌ Неверный формат координат. Используйте числа с точкой.")
        return

    city_name = " ".join(tokens[:-2])

    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name, include_inactive=True)
        if not city:
            await message.answer(f"❌ Город '{city_name}' не найден в базе.")
            return

        city.latitude = lat
        city.longitude = lon
        await db.commit()
        await message.answer(f"✅ Координаты для города '{city_name}' установлены: {lat}, {lon}")

# ---------- Статистика ----------
@router.message(Command("stats"))
async def show_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

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
    text += f"▪ Новых за месяц: {user_stats['new_month']}\n"
    text += "\n"

    text += "💳 <b>Платежи</b>\n"
    text += f"▪ Всего оплат: {payment_stats['total_payments']}\n"
    text += f"▪ Общая выручка: {payment_stats['total_revenue']:.2f} ₽\n"
    text += f"▪ Сегодня: {payment_stats['payments_today']} шт. ({payment_stats['revenue_today']:.2f} ₽)\n"
    text += f"▪ За неделю: {payment_stats['payments_week']} шт. ({payment_stats['revenue_week']:.2f} ₽)\n"
    text += f"▪ За месяц: {payment_stats['payments_month']} шт. ({payment_stats['revenue_month']:.2f} ₽)\n"
    text += "\n"

    text += "🔄 <b>Воронка</b>\n"
    total_funnel = sum(funnel_stats.values())
    if total_funnel > 0:
        stage_names = {
            0: "❌ Не начали поиск",
            1: "👋 1 день после первого поиска",
            2: "📊 3 дня",
            3: "⚠️ 7 дней",
            4: "🎁 14 дней",
            5: "💤 Завершено"
        }
        for stage, count in funnel_stats.items():
            name = stage_names.get(stage, f"Стадия {stage}")
            percent = round(count / total_funnel * 100, 1) if total_funnel > 0 else 0
            text += f"▪ {name}: {count} ({percent}%)\n"
    else:
        text += "▪ Нет данных по воронке (пользователи не совершали поиск)\n"
    text += "\n"

    text += "⭐ <b>Отзывы</b>\n"
    text += f"▪ Всего: {review_stats['total_reviews']}\n"
    text += f"▪ Средний рейтинг: {review_stats['avg_rating']}⭐\n"
    text += "\n"

    text += "👥 <b>Рефералы</b>\n"
    text += f"▪ Всего приглашённых: {referral_stats['total_referrals']}\n"
    text += f"▪ Получили бонус: {referral_stats['rewarded']}\n"

    await message.answer(text, parse_mode="HTML")

# ---------- Очистка адресов АЗС ----------
@router.message(Command("clean_addresses"))
async def clean_addresses_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

    await message.answer("🔄 Начинаю очистку адресов АЗС...")

    async with AsyncSessionLocal() as db:
        stations = await db.execute(
            select(Station).where(
                Station.address.contains("<strong>") | Station.address.contains("<br>")
            )
        )
        stations = stations.scalars().all()
        count = len(stations)

        if count == 0:
            await message.answer("✅ Испорченных адресов не найдено.")
            return

        for station in stations:
            station.address = ""
        await db.commit()

        await message.answer(
            f"✅ Очищено {count} адресов АЗС.\n\n"
            "Теперь запустите /import_all_cities, чтобы обновить адреса из парсера."
        )

# ---------- Статистика по городам ----------
@router.message(Command("cities_stats"))
async def cities_stats_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

    async with AsyncSessionLocal() as db:
        stmt = (
            select(
                City.id,
                City.name,
                City.latitude,
                City.longitude,
                func.count(Station.id).filter(Station.is_active == True).label('active_count'),
                func.count(Station.id).label('total_count')
            )
            .outerjoin(Station, Station.city_id == City.id)
            .group_by(City.id, City.name, City.latitude, City.longitude)
            .order_by(City.name)
        )
        result = await db.execute(stmt)
        rows = result.all()

        if not rows:
            await message.answer("❌ В базе нет городов.")
            return

        stats = []
        total_active = 0
        total_all = 0
        for row in rows:
            active = row.active_count or 0
            total = row.total_count or 0
            total_active += active
            total_all += total
            has_coords = row.latitude is not None and row.longitude is not None
            stats.append({
                "name": row.name,
                "active": active,
                "total": total,
                "has_coords": has_coords
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

# ---------- НОВЫЕ КОМАНДЫ ДЛЯ РАБОТЫ С АДРЕСАМИ ----------

@router.message(Command("stations_without_address"))
async def stations_without_address_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

    async with AsyncSessionLocal() as db:
        stations = await db.execute(
            select(Station).where(
                (Station.address.is_(None)) | (Station.address == "")
            )
        )
        stations = stations.scalars().all()
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
async def set_station_address_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

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
        station = await get_station_by_id(db, station_id)
        if not station:
            await message.answer(f"❌ Станция с ID {station_id} не найдена.")
            return
        station.address = address
        await db.commit()
        await message.answer(f"✅ Адрес для станции «{station.name}» (ID {station.id}) установлен:\n{address}")
