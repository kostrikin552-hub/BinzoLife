@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    args = message.text.split()
    ref_code = None
    if len(args) > 1 and args[1].startswith("ref_"):
        ref_code = args[1][4:]

    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username)
            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await db.commit()
            # применяем рефералку, если есть
            if ref_code:
                await apply_referral(db, user.id, ref_code)
        elif ref_code:
            # если пользователь уже есть, но пришёл по ссылке — всё равно пробуем применить (если не применял)
            await apply_referral(db, user.id, ref_code)

    await message.answer(
        "⛽ <b>Добро пожаловать в BinzoLife!</b>\n\n"
        "В 2026 году цены на бензин непредсказуемы, очереди – обычное дело. "
        "Но вы можете быть на шаг впереди.\n\n"
        "<b>Что я умею:</b>\n"
        "• Найду АЗС с самой низкой ценой АИ‑95 рядом с вами.\n"
        "• Покажу наличие топлива в реальном времени (по данным пользователей).\n"
        "• Предупрежу о резком росте цен (PRO‑функция).\n"
        "• Сэкономлю вам до 500 ₽ на каждой заправке.\n\n"
        "Если вы не в Красноярске, сначала установите город в Профиле.\n\n"
        "Нажмите «Найти заправку».",
        reply_markup=main_menu_keyboard()
    )
