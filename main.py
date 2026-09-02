import asyncio
import base64
import logging
import os
import re
import sqlite3
import time
from io import BytesIO
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram import BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

load_dotenv()
logging.basicConfig(level=logging.INFO)
router = Router()
ACCESS_CODE = os.getenv("ACCESS_CODE", "101010ajk")
AI_MODELS = {
    "minimax-m3": "MiniMax M3",
    "gemma-4-31b": "Gemma 4 31B",
    "ag/gemini-3.7-flash-high": "Gemini 3.7 Flash High",
}
VISION_MODEL = os.getenv("ANYMODEL_VISION_MODEL", "ag/gemini-3.7-flash-high")
TRANSCRIBE_MODELS = (
    os.getenv("ANYMODEL_TRANSCRIBE_MODEL", "dg/whisper-large"),
    "dg/nova-3",
    "dg/nova-2",
    "am/nemotron-3-nano-omni-30b-a3b-reasoning-stt",
)
AI_HISTORY: dict[int, list[dict[str, str]]] = {}


@dataclass(frozen=True)
class Book:
    key: str
    label: str
    subject: str
    max_task: int
    source_url: str
    site_id: str = ""
    task_ids: tuple[str, ...] = ()

    def url(self, task: str) -> str:
        if self.key == "makarichev":
            return f"https://reshak.ru/otvet/otvet24.php?otvet1={task}"
        return f"https://reshak.ru/otvet/reshebniki.php?otvet={task}&predmet={self.site_id}"


BOOKS = {
    "makarichev": Book("makarichev", "Алгебра 9 класс, Макарычев", "Алгебра", 1097, "https://reshak.ru/reshebniki/algebra/9/makarichev/index.php"),
    "barkhudarov": Book("barkhudarov", "Русский язык 9 класс, Бархударов", "Русский язык", 527, "https://reshak.ru/reshebniki/russkijazik/9/barh_new/index.html", "barh_new9"),
    "spotlight": Book("spotlight", "Английский 9 класс, Spotlight", "Английский", 500, "https://reshak.ru/spotlight9/index.html", "spotlight9"),
    "perishkin": Book("perishkin", "Физика 9 класс, Перышкин", "Физика", 500, "https://reshak.ru/reshebniki/fizika/9/perishkin/index.html", "perishkin9"),
    "pasechnik": Book("pasechnik", "Биология 9 класс, Пасечник", "Биология", 49, "https://reshak.ru/reshebniki/biologia/9/pasechnik_ucheb/index.html", "pasechnik_ucheb9"),
    "ostroumov": Book("ostroumov", "Химия 9 класс, Остроумов", "Химия", 500, "https://reshak.ru/reshebniki/ximiya/9/ostroumov/index.html", "ostroumov9"),
    "medinsky": Book("medinsky", "История Нового времени, Мединский и Чубарьян", "История Нового времени", 23, "https://reshak.ru/reshebniki/istoria/9/medinsky/index.html", "medinsky9", tuple(map(str, range(1, 24))) + ("itog1", "itog2", "itog3", "itog4", "exam")),
    "medinsky_torkunov": Book("medinsky_torkunov", "История России, Мединский и Торкунов", "История России", 37, "https://reshak.ru/reshebniki/istoria/9/medinsky_torkunov/index.html", "medinsky_torkunov9", ("1", "2", "3", "4", "5", "6-7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27-28", "29", "30", "31", "32", "33", "34", "35", "36", "37", "itog1", "itog2", "itog3", "itog4", "itog5", "itog6", "itog7", "itog8", "exam")),
}
SUBJECTS = {"algebra": ("Алгебра", ["makarichev"]), "russian": ("Русский язык", ["barkhudarov"]), "english": ("Английский", ["spotlight"]), "physics": ("Физика", ["perishkin"]), "biology": ("Биология", ["pasechnik"]), "chemistry": ("Химия", ["ostroumov"]), "history_modern": ("История Нового времени", ["medinsky"]), "history_russia": ("История России", ["medinsky_torkunov"])}


class Cache:
    def __init__(self):
        self.path = os.getenv("CACHE_PATH", "cache.sqlite3")
        self.ttl = int(os.getenv("CACHE_TTL_HOURS", "6")) * 3600
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, title TEXT, task TEXT, images TEXT, url TEXT, created INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS authorized_users (user_id INTEGER PRIMARY KEY, authorized_at INTEGER NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(answers)")}
            old_columns = {"cache_key", "task_text", "image_urls", "source_url", "created_at"}
            if old_columns.issubset(columns):
                db.execute("""CREATE TABLE answers_migrated (
                    key TEXT PRIMARY KEY,
                    title TEXT,
                    task TEXT,
                    images TEXT,
                    url TEXT,
                    created INTEGER
                )""")
                db.execute("""INSERT INTO answers_migrated (key, title, task, images, url, created)
                    SELECT cache_key, title, task_text, image_urls, source_url, created_at
                    FROM answers""")
                db.execute("DROP TABLE answers")
                db.execute("ALTER TABLE answers_migrated RENAME TO answers")

    def get(self, key):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT title, task, images, url, created FROM answers WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[4] > self.ttl:
            return None
        return row[:2] + ((row[2].split("\n") if row[2] else []), row[3])

    def put(self, key, value):
        with sqlite3.connect(self.path) as db:
            title, task, images, url = value
            db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?, ?)", (key, title, task, "\n".join(images), url, int(time.time())))

    def is_authorized(self, user_id: int) -> bool:
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT 1 FROM authorized_users WHERE user_id=?", (user_id,)).fetchone() is not None

    def authorize(self, user_id: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO authorized_users VALUES (?, ?)", (user_id, int(time.time())))


async def fetch(book, task, session):
    url = book.url(task)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
        if response.status != 200:
            raise RuntimeError("сайт временно недоступен")
        soup = BeautifulSoup(await response.text(), "html.parser")
    title, task_node = soup.select_one("h1.titleh1"), soup.select_one(".text_zad")
    if not title or not task_node:
        raise RuntimeError("информация по заданию не найдена")
    images = []
    for image in soup.select("[class*=pic_otvet] img"):
        source = image.get("src") or image.get("data-src")
        if source and urljoin(url, source) not in images:
            images.append(urljoin(url, source))
    return (" ".join(title.get_text(" ", strip=True).split()), " ".join(task_node.get_text(" ", strip=True).split()), images, url)


def subjects_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=name, callback_data=f"subject:{key}")] for key, (name, _) in SUBJECTS.items()])


def ai_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"ai_model:{model}")]
        for model, name in AI_MODELS.items()
    ] + [[InlineKeyboardButton(text="Назад к предметам", callback_data="subjects")]])


def books_keyboard(subject):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BOOKS[key].label, callback_data=f"book:{key}")] for key in SUBJECTS[subject][1]] + [[InlineKeyboardButton(text="Назад", callback_data="subjects")]])


def numbers_keyboard(key, page=0):
    choices = BOOKS[key].task_ids or tuple(map(str, range(1, BOOKS[key].max_task + 1)))
    start, end = page * 50, min(page * 50 + 50, len(choices))
    rows = [[InlineKeyboardButton(text=choices[i], callback_data=f"task:{key}:{choices[i]}") for i in range(n, min(n + 5, end))] for n in range(start, end, 5)]
    nav = []
    if page: nav.append(InlineKeyboardButton(text="<<", callback_data=f"page:{key}:{page - 1}"))
    if end < len(choices): nav.append(InlineKeyboardButton(text=">>", callback_data=f"page:{key}:{page + 1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="Назад к предметам", callback_data="subjects")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SearchState(StatesGroup):
    choosing_book = State()
    entering_number = State()
    ai_chat = State()


cache = Cache()


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and cache.is_authorized(user.id):
            return await handler(event, data)

        if isinstance(event, Message):
            text = (event.text or "").strip()
            if text == ACCESS_CODE:
                cache.authorize(event.from_user.id)
                await event.answer("Код принят. Доступ открыт. Нажмите /start.")
                return
            if text.startswith("/start"):
                await event.answer("Для входа в бота отправьте код доступа сообщением.")
                return
            await event.answer("Доступ закрыт. Отправьте код доступа сообщением.")
            return

        if isinstance(event, CallbackQuery):
            await event.answer("Сначала введите код доступа.", show_alert=True)
            return


router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Выберите действие:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Найти решение", callback_data="subjects")],
        [InlineKeyboardButton(text="ИИ-чат", callback_data="ai_start")],
    ]))


@router.callback_query(F.data == "ai_start")
async def ai_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.ai_chat)
    await state.update_data(ai_model=os.getenv("ANYMODEL_MODEL", "minimax-m3"))
    await callback.message.edit_text("Выберите ИИ-модель:", reply_markup=ai_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("ai_model:"))
async def ai_model(callback: CallbackQuery, state: FSMContext):
    model = callback.data.split(":", 1)[1]
    if model not in AI_MODELS:
        await callback.answer("Кнопка устарела. Нажмите /start", show_alert=True)
        return
    await state.set_state(SearchState.ai_chat)
    await state.update_data(ai_model=model)
    await callback.message.edit_text(
        f"Выбрана модель: {AI_MODELS[model]}\n\nНапишите вопрос. Для выхода нажмите /start.",
        reply_markup=ai_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "subjects")
async def subjects(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.choosing_book)
    await callback.message.edit_text("Выберите предмет:", reply_markup=subjects_keyboard())
    await callback.answer()


@router.callback_query(F.data == "choose_book")
async def legacy_choose_book(callback: CallbackQuery, state: FSMContext):
    # Keep old menus working after the bot was updated.
    await state.set_state(SearchState.choosing_book)
    await callback.message.edit_text("Выберите предмет:", reply_markup=subjects_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("subject:"))
async def subject(callback: CallbackQuery):
    subject_key = callback.data.split(":", 1)[1]
    if subject_key not in SUBJECTS:
        await callback.answer("Кнопка устарела. Нажмите /start", show_alert=True)
        return
    await callback.message.edit_text("Выберите учебник:", reply_markup=books_keyboard(subject_key))
    await callback.answer()


@router.callback_query(F.data.startswith("book:"))
async def book(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in BOOKS:
        await callback.answer("Кнопка устарела. Нажмите /start", show_alert=True)
        return
    await state.update_data(book_key=key)
    await state.set_state(SearchState.entering_number)
    await callback.message.edit_text(f"{BOOKS[key].label}\nВыберите номер задания:", reply_markup=numbers_keyboard(key))
    await callback.answer()


@router.callback_query(F.data.startswith("page:"))
async def page(callback: CallbackQuery):
    _, key, value = callback.data.split(":")
    if key not in BOOKS or not value.isdigit():
        await callback.answer("Кнопка устарела. Нажмите /start", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=numbers_keyboard(key, int(value)))
    await callback.answer()


@router.callback_query(F.data.startswith("task:"))
async def task_button(callback: CallbackQuery, state: FSMContext):
    _, key, task = callback.data.split(":")
    if key not in BOOKS:
        await callback.answer("Кнопка устарела. Нажмите /start", show_alert=True)
        return
    await state.update_data(book_key=key)
    await state.set_state(SearchState.entering_number)
    await callback.answer()
    await process(callback.message, state, task)


@router.callback_query()
async def unknown_callback(callback: CallbackQuery):
    logging.warning("Unhandled callback data: %r", callback.data)
    await callback.answer("Кнопка устарела. Нажмите /start", show_alert=True)


def render_latex(formula: str) -> BytesIO:
    image = BytesIO()
    figure = plt.figure(figsize=(0.01, 0.01), dpi=220)
    figure.text(0, 0, f"${formula.strip()}$", fontsize=16)
    figure.savefig(image, format="png", transparent=True, bbox_inches="tight", pad_inches=0.12)
    plt.close(figure)
    image.seek(0)
    return image


def split_latex(text: str) -> tuple[str, list[str]]:
    formulas = []
    pattern = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\$(?!\$)(.+?)(?<!\$)\$", re.DOTALL)

    def replace(match):
        formula = next(part for part in match.groups() if part is not None)
        formulas.append(formula)
        return "[формула отправлена изображением]"

    return pattern.sub(replace, text), formulas


async def ask_ai(user_id: int, prompt: str, model: str) -> str:
    api_key = os.getenv("ANYMODEL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ИИ-провайдер не настроен")
    history = AI_HISTORY.setdefault(user_id, [])
    messages = [{"role": "system", "content": "Ты полезный школьный помощник. Объясняй понятно и пошагово. Математические формулы пиши в LaTeX: блочные формулы между $$ и $$, короткие формулы между $. Не используй HTML."}]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": prompt})
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://anymodel.org/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 1800},
                timeout=aiohttp.ClientTimeout(total=90),
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    raise RuntimeError("ИИ-провайдер вернул ошибку")
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise RuntimeError("ИИ-провайдер временно недоступен") from error
    answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    if not answer:
        raise RuntimeError("ИИ не вернул ответ")
    history.extend([{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}])
    return answer


async def anymodel_request(payload, model: str, endpoint: str = "chat/completions"):
    api_key = os.getenv("ANYMODEL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ИИ-провайдер не настроен")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://anymodel.org/v1/{endpoint}",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    logging.warning("AnyModel error %s: %s", response.status, data)
                    raise RuntimeError("ИИ-провайдер вернул ошибку")
                return data
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        raise RuntimeError("ИИ-провайдер временно недоступен") from error


async def transcribe_voice(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    api_key = os.getenv("ANYMODEL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ИИ-провайдер не настроен")
    last_error = None
    for model in dict.fromkeys(TRANSCRIBE_MODELS):
        for attempt in range(2):
            form = aiohttp.FormData()
            form.add_field("file", file_bytes, filename=filename, content_type="audio/ogg")
            form.add_field("model", model)
            form.add_field("language", "ru")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://anymodel.org/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {api_key}"},
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=90),
                    ) as response:
                        data = await response.json(content_type=None)
                        if response.status == 200:
                            text = data.get("text", "").strip()
                            if text:
                                return text
                        last_error = f"{response.status}: {data}"
                        logging.warning("Transcription error with %s, attempt %s: %s", model, attempt + 1, last_error)
                        if response.status not in (429, 500, 502, 503, 504):
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = str(error)
                logging.warning("Transcription connection error with %s: %s", model, error)
            if attempt == 0:
                await asyncio.sleep(2)
    raise RuntimeError("сервис транскрибации временно недоступен") from Exception(last_error)


async def answer_ai_content(user_id: int, content, model: str) -> str:
    if isinstance(content, str):
        return await ask_ai(user_id, content, model)
    api_key = os.getenv("ANYMODEL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ИИ-провайдер не настроен")
    response = await anymodel_request({
        "model": model,
        "messages": [{"role": "system", "content": "Ты полезный школьный помощник. Опиши и реши задание с изображения. Формулы пиши в LaTeX между $$ и $$. Не используй HTML."}, {"role": "user", "content": content}],
        "temperature": 0.2,
        "max_tokens": 1800,
    })
    answer = response.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    if not answer:
        raise RuntimeError("ИИ не вернул ответ")
    return answer


async def reply_with_ai(message: Message, state: FSMContext, content):
    model = (await state.get_data()).get("ai_model", os.getenv("ANYMODEL_MODEL", "minimax-m3"))
    await message.answer("ИИ готовит ответ...")
    try:
        answer = await answer_ai_content(message.from_user.id, content, model)
        text, formulas = split_latex(answer)
        for part in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
            await message.answer(part)
        for formula in formulas:
            try:
                await message.answer_photo(BufferedInputFile(render_latex(formula).read(), filename="formula.png"))
            except Exception:
                await message.answer(f"LaTeX-формула: {formula}")
    except RuntimeError:
        await message.answer("Невозможно получить ответ от ИИ. Попробуйте позже.")


@router.message(SearchState.ai_chat, F.text)
async def ai_message(message: Message, state: FSMContext):
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("Напишите вопрос текстом.")
        return
    model = (await state.get_data()).get("ai_model", "minimax-m3")
    await reply_with_ai(message, state, prompt)


@router.message(SearchState.ai_chat, F.voice)
async def ai_voice(message: Message, state: FSMContext, bot: Bot):
    try:
        telegram_file = await bot.get_file(message.voice.file_id)
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, buffer)
        prompt = await transcribe_voice(buffer.getvalue())
        await message.answer(f"Распознано: {prompt}")
        await reply_with_ai(message, state, prompt)
    except RuntimeError:
        await message.answer("Невозможно распознать голосовое сообщение. Попробуйте еще раз.")


@router.message(SearchState.ai_chat, F.photo)
async def ai_photo(message: Message, state: FSMContext, bot: Bot):
    try:
        telegram_file = await bot.get_file(message.photo[-1].file_id)
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, buffer)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        question = message.caption or "Реши задание на изображении и объясни решение пошагово."
        content = [{"type": "text", "text": question}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}]
        await reply_with_ai(message, state, content)
    except RuntimeError:
        await message.answer("Невозможно получить информацию с фотографии. Попробуйте другое изображение.")


@router.message(SearchState.entering_number)
async def task_message(message: Message, state: FSMContext):
    task = (message.text or "").strip()
    data = await state.get_data()
    book = BOOKS.get(data.get("book_key"))
    if not book or not re.fullmatch(r"\d+(?:-\d+)?", task):
        await message.answer("Выберите номер кнопкой или введите номер задания.")
        return
    await process(message, state, task)


async def process(message, state, task):
    book = BOOKS[(await state.get_data()).get("book_key")]
    await message.answer("Ищу решение...")
    try:
        answer = cache.get(f"{book.key}:{task}")
        if not answer:
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 ReshakTelegramBot/1.0"}) as session:
                answer = await fetch(book, task, session)
            cache.put(f"{book.key}:{task}", answer)
        title, task_text, images, url = answer
        await message.answer(f"<b>{title}</b>\n\n{task_text}\n\n<a href=\"{url}\">Источник на Reshak.ru</a>", disable_web_page_preview=True)
        for image in images:
            try: await message.answer_photo(image)
            except Exception: logging.warning("image send failed: %s", image)
    except Exception as error:
        await message.answer(
            "Невозможно получить информацию по этому заданию. "
            "Попробуйте еще раз позже или откройте решение на Reshak.ru.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть на Reshak.ru", url=book.url(task))
            ]]),
        )
    await state.clear()


async def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token: raise RuntimeError("Добавьте BOT_TOKEN в .env")
    bot, dispatcher = Bot(token), Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
