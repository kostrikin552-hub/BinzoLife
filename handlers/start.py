from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.session import AsyncSessionLocal
from database.crud import get_user, create_user, get_city_by_name, apply_referral
from keyboards.inline import welcome_back_keyboard, city_choice_keyboard, popular_cities_keyboard, main_menu_keyboard

router = Router()

# ID канала (можно использовать как с минусом, так и без)
# Для проверки лучше использовать числовой ID, полученный через @userinfobot
CHANNEL_ID = -1004398885383  # или можно оставить как -1004398885383
CHANNEL_LINK = "https://t.me/BinzoLife_News"

class SubscribeStates(StatesGroup):
    waiting_subscribe = State()

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    # Проверяем подписку на канал
    user_id = message.from_user.id
    is_subscribed = False
    try:
        # Пробуем проверить через числовой ID
        chat_member = await message.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        is_subscribed = chat_member.status in ["member", "administrator", "creator"]
    except Exception as e:
        # Если не получилось (например, бот не админ или канал скрыт), пробуем через username
        try:
            chat_member = await message.bot.get_chat_member(chat_id="@BinzoLife_News", user_id=user_id)
            is_subscribed = chat_member.status in ["member", "administrator", "creator"]
        except Exception:
            # Если проверка совсем недоступна, пропускаем (можно выставить True, чтобы не блокировать)
            # Но лучше попросить подписаться в любом случае, чтобы не терять аудиторию
            is_subscribed = False

    if not is_subscribed:
        await state.set_state(SubscribeStates.waiting_subscribe)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_LINK)],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscribe")]
        ])
        await message.answer(
            "📢 Чтобы пользоваться ботом, подпишитесь на наш канал:\n"
            f"{CHANNEL_LINK}\n\n"
            "После подписки нажмите «Я подписался».",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        return

    # Если подписан – продолжаем
    await process_start(message, state)

@router.callback_query(F.data == "check_subscribe", SubscribeStates.waiting_subscribe)
async def check_subscribe(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    is_subscribed = False
    try:
        chat_member = await callback.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        is_subscribed = chat_member.status in ["member", "administrator", "creator"]
    except Exception:
        try:
            chat_member = await callback.bot.get_chat_member(chat_id="@BinzoLife_News", user_id=user_id)
            is_subscribed = chat_member.status in ["member", "administrator", "creator"]
        except Exception:
            is_subscribed = False

    if is_subscribed:
        await callback.message.delete()
        await callback.message.answer("✅ Спасибо! Теперь вы можете пользоваться ботом.")
        await state.clear()
        await process_start(callback.message, state)
    else:
        await callback.answer("❌ Вы ещё не подписались на канал. Пожалуйста, подпишитесь и нажмите кнопку снова.", show_alert=True)

async def process_start(message: types.Message, state: FSMContext):
    # Вся основная логика /start
    args = message.text.split()
    user_id = message.from_user.id
    username = message.from_user.username

    if len(args) > 1:
        if args[1].startswith("ref_"):
            ref_code = args[1][4:]
        elif args[1] == "pro":
            from handlers.payments import show_pro_info
            await show_pro_info(message)
            return
        else:
            ref_code = None
    else:
        ref_code = None

    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            user = await create_user(db, user_id, username)
            if ref_code:
                await apply_referral(db, user.id, ref_code)
            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await db.commit()

            # Приветствие для нового пользователя (короткое)
            await message.answer(
                "📖 **BinzoLife за 3 шага:**\n\n"
                "1. Нажми «Найти заправку» → выбери топливо → получи АЗС с ценой, наличием и маршрутом.\n"
                "2. В критической ситуации нажми «Бензин заканчивается!» — я найду топливо за 10 секунд.\n"
                "3. Подключи PRO, чтобы получать уведомления о снижении цен и не упускать выгоду.\n\n"
                "💰 Экономь до 500 ₽ за заправку. Начни сейчас → «Найти заправку».",
                reply_markup=welcome_back_keyboard(),
                parse_mode="HTML"
            )
            return

        if user.city_id:
            await message.answer(
                "⛽ С возвращением! Где ищем заправку сегодня?\n\n"
                "💰 Экономь до 500 ₽ за раз и не стой в очередях.",
                reply_markup=welcome_back_keyboard()
            )
            return

        await message.answer(
            "⛽ Привет! Я — BinzoLife.\n\n"
            "Я знаю, где прямо сейчас есть 95-й бензин, по какой цене и сколько до него ехать.\n\n"
            "Сэкономь до 500 ₽ на одной заправке и забудь про очереди.\n\n"
            "Что я умею:\n"
            "✅ Найти ближайшую АЗС с топливом (даже в час пик)\n"
            "✅ Показать самую дешёвую цену в твоём районе\n"
            "✅ Построить маршрут за 1 клик\n\n"
            "📍 Для начала выбери свой город:",
            reply_markup=city_choice_keyboard()
        )
