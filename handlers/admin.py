import csv
import io
from aiogram import Router, types, F
from aiogram.filters import Command
from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, save_availability_report,
    get_user, get_station_by_id, deactivate_station, activate_pro,
    get_station_by_name_address, get_all_reviews, get_avg_rating
)
from database.models import SourceType, AvailabilityStatus, FuelType

router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids

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
            from database.models import City
            new_city = City(name=name, region=region)
            db.add(new_city)
            await db.commit()
            await message.answer(f"Город {name} добавлен.")

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
        await save_availability_report(db, station_id, fuel, status, SourceType.ADMIN, confidence=0.9)
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
                    from database.models import City
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
                await save_availability_report(db, station.id, FuelType.AI_95, status, SourceType.ADMIN, confidence=0.8)
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
