from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from database.crud import get_user, create_user, get_city_by_name
from database.session import AsyncSessionLocal
from keyboards.reply import main_menu_keyboard

router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username)
            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await db.commit()

    await message.answer(
        "⛽ Добро пожаловать в <b>BinzoLife</b>!\n\n"
        "Я помогу найти лучшую АЗС для заправки прямо сейчас.\n"
        "Выберите действие:",
        reply_markup=main_menu_keyboard()
    )
