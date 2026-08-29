from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta, timezone
from database.session import AsyncSessionLocal
from database.crud import get_user, create_review, get_avg_rating
from database.models import Review
from keyboards.reply import main_menu_keyboard
from sqlalchemy import select

router = Router()

class ReviewStates(StatesGroup):
    waiting_rating = State()
    waiting_comment = State()

@router.message(F.text == "⭐ Оставить отзыв")
async def start_review(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала выполните /start")
            return

        # Проверка: не оставлял ли пользователь отзыв за последние 24 часа
        last_review = await db.execute(
            select(Review).where(
                Review.user_id == user.id,
                Review.created_at >= datetime.now(timezone.utc) - timedelta(days=1)
            )
        ).scalar_one_or_none()
        if last_review:
            await message.answer("⏳ Вы уже оставляли отзыв за последние 24 часа. Попробуйте завтра.")
            return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 1", callback_data="rate_1"),
         InlineKeyboardButton(text="⭐ 2", callback_data="rate_2"),
         InlineKeyboardButton(text="⭐ 3", callback_data="rate_3")],
        [InlineKeyboardButton(text="⭐ 4", callback_data="rate_4"),
         InlineKeyboardButton(text="⭐ 5", callback_data="rate_5")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_review")]
    ])
    await state.set_state(ReviewStates.waiting_rating)
    await message.answer(
        "⭐ Оцените работу бота от 1 до 5 звёзд:",
        reply_markup=kb
    )

@router.callback_query(lambda c: c.data.startswith("rate_"))
async def process_rating(callback: types.CallbackQuery, state: FSMContext):
    rating = int(callback.data.split("_")[1])
    await state.update_data(rating=rating)
    await state.set_state(ReviewStates.waiting_comment)
    await callback.message.edit_text(
        f"Вы поставили {rating}⭐.\nТеперь напишите комментарий (или отправьте «Пропустить», чтобы оставить без комментария):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏩ Пропустить", callback_data="skip_comment")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "skip_comment")
async def skip_comment(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(comment=None)
    await save_review(callback.message, state, callback.from_user.id)
    await callback.answer()

@router.message(ReviewStates.waiting_comment, F.text)
async def process_comment(message: types.Message, state: FSMContext):
    comment = message.text.strip()
    if comment and len(comment) > 500:
        await message.answer("Комментарий не должен превышать 500 символов. Попробуйте снова.")
        return
    await state.update_data(comment=comment)
    await save_review(message, state, message.from_user.id)

async def save_review(message: types.Message, state: FSMContext, telegram_id: int):
    data = await state.get_data()
    rating = data.get("rating")
    comment = data.get("comment")
    if not rating:
        await message.answer("Что-то пошло не так. Попробуйте снова.")
        await state.clear()
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, telegram_id)
        if not user:
            await message.answer("Сначала выполните /start")
            await state.clear()
            return

        await create_review(db, user.id, rating, comment)
        user.reputation += 2
        await db.commit()

        avg = await get_avg_rating(db)
        await message.answer(
            f"✅ Спасибо за ваш отзыв!\n"
            f"Ваша оценка: {rating}⭐\n"
            f"Средний рейтинг бота: {avg}⭐\n"
            f"Ваша репутация +2 (всего {user.reputation})\n\n"
            "Ваше мнение помогает нам становиться лучше!",
            reply_markup=main_menu_keyboard()
        )
    await state.clear()

@router.callback_query(F.data == "cancel_review")
async def cancel_review(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Отзыв отменён.")
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())
