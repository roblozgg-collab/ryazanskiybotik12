import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
REQUIRED_CHANNEL_ID = os.getenv("REQUIRED_CHANNEL_ID", "").strip()
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("В .env не указан BOT_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)

RYAZAN_LAT = 54.6296
RYAZAN_LON = 39.7425
UAV_SOURCE_URL = "https://t.me/s/Radar_Ryazan62"
UAV_STATE_FILE = Path("uav_state.json")
TRAFFIC_STATE_FILE = Path("traffic_state.json")
UAV_DANGER_PHRASE = "беспилотная опасность в рязанской области"
UAV_CLEAR_PHRASE = "отбой беспилотной опасности в рязанской области"

BASE_DIR = Path(__file__).resolve().parent
MENU_IMAGE = BASE_DIR / "menu.png"
WEATHER_IMAGE = BASE_DIR / "weather.png"
ROAD_IMAGE = BASE_DIR / "road.png"
UAV_RED_IMAGE = BASE_DIR / "red.png"
UAV_GREEN_IMAGE = BASE_DIR / "green.png"

MAIN_MENU_TEXT = (
    "🛰️ <b>Рязань-Инфо Бот включён.</b>\n\n"
    "<b>Регламент информирования:</b>\n"
    "— погодный блок: каждые 60 минут;\n"
    "— пробочная аналитика: каждые 10 минут;\n"
    "— статус БПЛА: моментально при появлении данных мониторинга."
)

DISTRICTS = {
    "sovetsky": "Советский",
    "moskovsky": "Московский",
    "oktyabrsky": "Октябрьский",
    "zheleznodorozhny": "Железнодорожный",
}

TRAFFIC_STATUSES = {
    "free": ("🟢", "Свободно"),
    "light": ("🟡", "Небольшая загрузка"),
    "medium": ("🟠", "Затруднено"),
    "heavy": ("🔴", "Сильные пробки"),
}

WEATHER_CODES = {
    0: "☀️ Ясно",
    1: "🌤 Преимущественно ясно",
    2: "⛅ Переменная облачность",
    3: "☁️ Пасмурно",
    45: "🌫 Туман",
    48: "🌫 Изморозь",
    51: "🌦 Слабая морось",
    53: "🌦 Морось",
    55: "🌧 Сильная морось",
    61: "🌧 Небольшой дождь",
    63: "🌧 Дождь",
    65: "🌧 Сильный дождь",
    71: "🌨 Небольшой снег",
    73: "🌨 Снег",
    75: "❄️ Сильный снег",
    80: "🌦 Небольшой ливень",
    81: "🌧 Ливень",
    82: "⛈ Сильный ливень",
    95: "⛈ Гроза",
    96: "⛈ Гроза с градом",
    99: "⛈ Сильная гроза с градом",
}

@dataclass
class UavState:
    state: str
    text: str
    published_at: Optional[datetime]
    source_url: str


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚨 Опасность БПЛА", callback_data="uav")],
        [InlineKeyboardButton(text="🌤 Погода в городе", callback_data="weather")],
        [InlineKeyboardButton(text="🚗 Пробки в 4 районах", callback_data="traffic")],
    ])


def subscription_keyboard() -> InlineKeyboardMarkup:
    rows = []
    if REQUIRED_CHANNEL_URL:
        rows.append([InlineKeyboardButton(text="📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL)])
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main")]
    ])


def admin_district_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📍 {name}", callback_data=f"admin_traffic_district:{key}")]
        for key, name in DISTRICTS.items()
    ]
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_traffic_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_status_keyboard(district_key: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{emoji} {label}", callback_data=f"admin_traffic_set:{district_key}:{status_key}")]
        for status_key, (emoji, label) in TRAFFIC_STATUSES.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ К районам", callback_data="admin_traffic_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def is_subscribed(user_id: int) -> bool:
    if not REQUIRED_CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        logging.exception("Subscription check failed")
        return False


async def show_subscription_required(message: Message) -> None:
    await message.answer(
        "🔒 <b>Для использования бота необходимо подписаться на наш Telegram-канал.</b>\n\n"
        "После подписки нажмите «✅ Проверить подписку».",
        reply_markup=subscription_keyboard(),
        parse_mode="HTML",
    )


async def callback_has_access(callback: CallbackQuery) -> bool:
    if await is_subscribed(callback.from_user.id):
        return True
    await callback.answer("Сначала подпишитесь на канал", show_alert=True)
    await callback.message.answer(
        "🔒 <b>Доступ к функциям бота открыт только подписчикам канала.</b>",
        reply_markup=subscription_keyboard(),
        parse_mode="HTML",
    )
    return False


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def _photo(path: Path) -> FSInputFile:
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл изображения: {path}")
    return FSInputFile(path)


async def send_photo_menu(
    message: Message,
    image_path: Path,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    await message.answer_photo(
        photo=_photo(image_path),
        caption=caption,
        reply_markup=reply_markup,
        parse_mode="HTML",
    )


async def edit_photo_menu(
    callback: CallbackQuery,
    image_path: Path,
    caption: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    media = InputMediaPhoto(
        media=_photo(image_path),
        caption=caption,
        parse_mode="HTML",
    )

    # Пользовательские меню после /start являются фото-сообщениями,
    # поэтому переключаем и картинку, и подпись одним edit_media.
    if callback.message.photo:
        await callback.message.edit_media(media=media, reply_markup=reply_markup)
    else:
        # Например, после экрана проверки подписки исходное сообщение текстовое.
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=_photo(image_path),
            caption=caption,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )


async def get_weather_text() -> str:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": RYAZAN_LAT,
        "longitude": RYAZAN_LON,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "timezone": "Europe/Moscow",
    }
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            data = await response.json()
    current = data["current"]
    desc = WEATHER_CODES.get(current["weather_code"], "🌡 Погодные данные")
    return (
        "🌤 <b>Погода в Рязани</b>\n\n"
        f"{desc}\n\n"
        f"🌡 Температура: <b>{current['temperature_2m']} °C</b>\n"
        f"🤔 Ощущается как: <b>{current['apparent_temperature']} °C</b>\n"
        f"💧 Влажность: <b>{current['relative_humidity_2m']}%</b>\n"
        f"💨 Ветер: <b>{current['wind_speed_10m']} км/ч</b>\n\n"
        "🔄 Данные запрашиваются в момент нажатия."
    )


def _normalize_alert_text(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_tg_datetime(tag) -> Optional[datetime]:
    if not tag:
        return None
    raw = tag.get("datetime")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _load_saved_uav_state() -> Optional[UavState]:
    if not UAV_STATE_FILE.exists():
        return None
    try:
        data = json.loads(UAV_STATE_FILE.read_text(encoding="utf-8"))
        published_at = datetime.fromisoformat(data["published_at"]) if data.get("published_at") else None
        if published_at and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        return UavState(data["state"], data.get("text", ""), published_at, UAV_SOURCE_URL)
    except Exception:
        logging.exception("Failed to load saved UAV state")
        return None


def _save_uav_state(state: UavState) -> None:
    if state.state not in {"danger", "clear"}:
        return
    payload = {
        "state": state.state,
        "text": state.text,
        "published_at": state.published_at.isoformat() if state.published_at else None,
    }
    UAV_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _detect_uav_event(raw_text: str) -> Optional[str]:
    normalized = _normalize_alert_text(raw_text)
    if UAV_CLEAR_PHRASE in normalized:
        return "clear"
    if UAV_DANGER_PHRASE in normalized:
        return "danger"
    return None


async def fetch_uav_state() -> UavState:
    headers = {"User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/130 Safari/537.36"}
    saved = _load_saved_uav_state()
    latest_event = None
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=headers) as session:
        async with session.get(UAV_SOURCE_URL) as response:
            response.raise_for_status()
            html = await response.text()
    soup = BeautifulSoup(html, "html.parser")
    for item in soup.select(".tgme_widget_message_wrap"):
        text_tag = item.select_one(".tgme_widget_message_text")
        time_tag = item.select_one("time")
        if not text_tag:
            continue
        raw_text = " ".join(text_tag.stripped_strings)
        event_type = _detect_uav_event(raw_text)
        if not event_type:
            continue
        candidate = UavState(event_type, raw_text, _parse_tg_datetime(time_tag), UAV_SOURCE_URL)
        if latest_event is None:
            latest_event = candidate
        elif candidate.published_at and latest_event.published_at and candidate.published_at > latest_event.published_at:
            latest_event = candidate
        elif candidate.published_at and not latest_event.published_at:
            latest_event = candidate
        elif not candidate.published_at and not latest_event.published_at:
            latest_event = candidate
    if latest_event:
        if saved and saved.published_at and latest_event.published_at and saved.published_at > latest_event.published_at:
            return saved
        _save_uav_state(latest_event)
        return latest_event
    if saved:
        return saved
    return UavState("unknown", "Статус пока не найден.", None, UAV_SOURCE_URL)


def format_uav_text(state: UavState) -> str:
    if state.state == "danger":
        title = "🚨 <b>БЕСПИЛОТНАЯ ОПАСНОСТЬ</b>"
        note = "В Рязанской области действует беспилотная опасность."
    elif state.state == "clear":
        title = "✅ <b>ОТБОЙ БЕСПИЛОТНОЙ ОПАСНОСТИ</b>"
        note = "В Рязанской области опубликован отбой беспилотной опасности."
    else:
        title = "⚪ <b>Статус БПЛА пока неизвестен</b>"
        note = "Точный пост об опасности или отбое ещё не найден."

    published = ""

    if state.published_at:
        local_dt = state.published_at.astimezone(
            timezone(timedelta(hours=3))
        )
        published = f"\n🕒 Последнее изменение: <b>{local_dt:%d.%m.%Y %H:%M} МСК</b>"

    return f"{title}\n\n{note}{published}"


def default_traffic_state() -> dict:
    return {
        key: {"status": "free", "updated_at": None}
        for key in DISTRICTS
    }


def load_traffic_state() -> dict:
    if not TRAFFIC_STATE_FILE.exists():
        data = default_traffic_state()
        save_traffic_state(data)
        return data
    try:
        data = json.loads(TRAFFIC_STATE_FILE.read_text(encoding="utf-8"))
        defaults = default_traffic_state()
        for key in DISTRICTS:
            if key not in data or data[key].get("status") not in TRAFFIC_STATUSES:
                data[key] = defaults[key]
        return data
    except Exception:
        logging.exception("Failed to load traffic state")
        data = default_traffic_state()
        save_traffic_state(data)
        return data


def save_traffic_state(data: dict) -> None:
    TRAFFIC_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_traffic_status(district_key: str, status_key: str) -> None:
    data = load_traffic_state()
    data[district_key] = {
        "status": status_key,
        "updated_at": datetime.now(timezone(timedelta(hours=3))).isoformat(),
    }
    save_traffic_state(data)


def format_traffic_text() -> str:
    data = load_traffic_state()
    lines = ["🚗 <b>Состояние пробок в Рязани</b>", ""]
    latest = None
    for key, name in DISTRICTS.items():
        item = data[key]
        emoji, label = TRAFFIC_STATUSES[item["status"]]
        lines.append(f"{emoji} <b>{name}</b> — {label}")
        if item.get("updated_at"):
            try:
                dt = datetime.fromisoformat(item["updated_at"])
                if latest is None or dt > latest:
                    latest = dt
            except ValueError:
                pass
    lines.append("")
    if latest:
        lines.append(f"🕒 Последнее обновление: <b>{latest:%d.%m.%Y %H:%M} МСК</b>")
    else:
        lines.append("🕒 Статусы ещё не обновлялись администратором.")
    return "\n".join(lines)


@dp.message(CommandStart())
async def start_handler(message: Message):
    if not await is_subscribed(message.from_user.id):
        await show_subscription_required(message)
        return
    await send_photo_menu(message, MENU_IMAGE, MAIN_MENU_TEXT, main_keyboard())


@dp.message(F.text.casefold() == "/пробка")
async def admin_traffic_command(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🚗 <b>Управление пробками</b>\n\nВыберите район:",
        reply_markup=admin_district_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin_traffic_menu")
async def admin_traffic_menu_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        "🚗 <b>Управление пробками</b>\n\nВыберите район:",
        reply_markup=admin_district_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_traffic_district:"))
async def admin_traffic_district_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    district_key = callback.data.split(":", 1)[1]
    if district_key not in DISTRICTS:
        await callback.answer("Район не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"📍 <b>{DISTRICTS[district_key]} район</b>\n\nВыберите состояние пробок:",
        reply_markup=admin_status_keyboard(district_key),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_traffic_set:"))
async def admin_traffic_set_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, district_key, status_key = callback.data.split(":", 2)
    if district_key not in DISTRICTS or status_key not in TRAFFIC_STATUSES:
        await callback.answer("Некорректные данные", show_alert=True)
        return
    set_traffic_status(district_key, status_key)
    emoji, label = TRAFFIC_STATUSES[status_key]
    await callback.answer(f"{DISTRICTS[district_key]}: {emoji} {label}")
    await callback.message.edit_text(
        "🚗 <b>Управление пробками</b>\n\n"
        f"✅ {DISTRICTS[district_key]}: {emoji} <b>{label}</b>\n\n"
        "Выберите следующий район:",
        reply_markup=admin_district_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin_traffic_close")
async def admin_traffic_close_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.delete()
    await callback.answer("Меню закрыто")


@dp.callback_query(F.data == "check_subscription")
async def check_subscription_handler(callback: CallbackQuery):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("Подписка пока не найдена", show_alert=True)
        return
    await callback.answer("Подписка подтверждена ✅")
    await edit_photo_menu(callback, MENU_IMAGE, MAIN_MENU_TEXT, main_keyboard())


@dp.callback_query(F.data == "main")
async def main_handler(callback: CallbackQuery):
    if not await callback_has_access(callback):
        return
    await edit_photo_menu(callback, MENU_IMAGE, MAIN_MENU_TEXT, main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "weather")
async def weather_handler(callback: CallbackQuery):
    if not await callback_has_access(callback):
        return
    await callback.answer("Обновляю погоду…")
    try:
        text = await get_weather_text()
    except Exception:
        logging.exception("Weather error")
        text = "⚠️ Не удалось получить погоду. Попробуйте ещё раз."
    await edit_photo_menu(callback, WEATHER_IMAGE, text, back_keyboard())


@dp.callback_query(F.data == "uav")
async def uav_handler(callback: CallbackQuery):
    if not await callback_has_access(callback):
        return
    await callback.answer("Проверяю последние сообщения…")
    try:
        state = await fetch_uav_state()
        text = format_uav_text(state)
        image_path = UAV_RED_IMAGE if state.state == "danger" else UAV_GREEN_IMAGE
    except Exception:
        logging.exception("UAV source error")
        text = "⚠️ <b>Не удалось проверить статус БПЛА</b>\n\nИсточник временно недоступен."
        # Если источник недоступен, не меняем смысл текста; картинка используется
        # только как оформление меню, а фактический статус указан в подписи.
        image_path = UAV_GREEN_IMAGE
    await edit_photo_menu(callback, image_path, text, back_keyboard())


@dp.callback_query(F.data == "traffic")
async def traffic_handler(callback: CallbackQuery):
    if not await callback_has_access(callback):
        return
    await callback.answer()
    await edit_photo_menu(callback, ROAD_IMAGE, format_traffic_text(), back_keyboard())


async def main():
    logging.info("Starting Ryazan city bot")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
