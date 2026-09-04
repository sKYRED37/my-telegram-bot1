"""
Standoff 2 Esports Betting Bot
Stack: Python, aiogram 3.x, aiosqlite
"""

import asyncio
import logging
import os
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ─────────────────────────────────────────────
#  КОНФИГУРАЦИЯ
# ─────────────────────────────────────────────
# Токен берётся из переменной окружения BOT_TOKEN, либо из значения по умолчанию ниже.
API_TOKEN = os.getenv("BOT_TOKEN", "8679358583:AAFd4Heg2JrmruiOx9bqqptFBZHJB_j7O2Y")
# ─── Твой Telegram ID (можно узнать у @userinfobot) ───
ADMIN_ID  = int(os.getenv("5728174980", "1766395031"))
DB_PATH   = os.getenv("DB_PATH", "standoff_bot.db")
START_BALANCE = 20_000
# Ставка на MVP всегда выплачивается с фиксированным множителем x2,
# независимо от того, что сохранено в поле mvp_odds конкретного матча.
MVP_MULTIPLIER = 2.0

MAPS = ["Rust", "Province", "Hanami", "Prison", "Sandstone", "Dune", "Breeze"]

# Сколько карт нужно выбрать для каждого формата матча
FORMAT_MAP_COUNT = {"BO1": 1, "BO2": 2, "BO3": 3, "BO5": 5}

MAP_SEP = "|"  # разделитель карт при сохранении в БД

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ─────────────────────────────────────────────
#  FSM СОСТОЯНИЯ
# ─────────────────────────────────────────────
class CreateMatch(StatesGroup):
    tournament_id = State()  # выбор турнира (или «без турнира»)
    team1        = State()
    team2        = State()
    fmt          = State()
    odds1        = State()
    odds2        = State()
    ask_draw     = State()   # включить ли ставки на ничью (инлайн-кнопки)
    odds_draw    = State()
    ask_mvp      = State()   # включить ли ставки на MVP (инлайн-кнопки)
    mvp_players_t1 = State()  # состав первой команды
    mvp_players_t2 = State()  # состав второй команды


class QuickCreateMatch(StatesGroup):
    """FSM для быстрого создания матча одним сообщением по шаблону (кэфы + составы сразу)."""
    raw_text = State()


class SetMapFlow(StatesGroup):
    picking = State()   # пошаговый выбор N карт


class EditTeams(StatesGroup):
    choose_match = State()
    new_team1    = State()
    new_team2    = State()


class BetWinner(StatesGroup):
    amount = State()


class BetMVP(StatesGroup):
    amount = State()


class AdminBonus(StatesGroup):
    amount = State()


class AdminGiveTokens(StatesGroup):
    """FSM для выдачи (или списания) токенов одному конкретному пользователю."""
    target = State()
    amount = State()


class CancelMatch(StatesGroup):
    """FSM для отмены матча с возвратом всех ставок участникам (откат токенов)."""
    choose_match = State()


class SubBonus(StatesGroup):
    """FSM для создания акции «Бонус за подписку» (3 шага)."""
    coins      = State()   # Шаг 1: сумма коинов
    channel_id = State()   # Шаг 2: технический ID канала
    channel_url = State()  # Шаг 3: публичная ссылка / юзернейм


class CreateTournament(StatesGroup):
    """FSM для создания турнира (1 шаг)."""
    name = State()


class BroadcastMessage(StatesGroup):
    """FSM для рассылки сообщения от лица бота всем игрокам."""
    text = State()
    confirm = State()


class AddModer(StatesGroup):
    """FSM для добавления модератора через панель."""
    choose_user = State()


class RemoveModer(StatesGroup):
    """FSM для удаления модератора через панель."""
    choose_moder = State()


class ActivatePromo(StatesGroup):
    """FSM для активации промокода пользователем (1 шаг — ввод кода)."""
    code = State()


class CreatePromo(StatesGroup):
    """FSM для создания промокода админом (3 шага)."""
    name        = State()  # Шаг 1: название промокода
    activations = State()  # Шаг 2: количество активаций
    amount      = State()  # Шаг 3: сумма начисления


# ─────────────────────────────────────────────
#  БД — ИНИЦИАЛИЗАЦИЯ
# ─────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                first_name TEXT,
                balance    INTEGER DEFAULT {START_BALANCE}
            )
        """)
        # Таблица турниров — создаётся через /admin
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                status     TEXT    DEFAULT 'active',
                created_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER DEFAULT NULL,
                team1         TEXT NOT NULL,
                team2         TEXT NOT NULL,
                format        TEXT NOT NULL,
                odds1         REAL NOT NULL,
                odds2         REAL NOT NULL,
                odds_draw     REAL    DEFAULT NULL,
                has_mvp       INTEGER NOT NULL DEFAULT 1,
                mvp_players   TEXT    DEFAULT NULL,
                mvp_odds      REAL    DEFAULT NULL,
                map           TEXT    DEFAULT NULL,
                status        TEXT    DEFAULT 'pending',
                winner        TEXT    DEFAULT NULL,
                mvp           TEXT    DEFAULT NULL,
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                bet_type TEXT    NOT NULL,
                target   TEXT    NOT NULL,
                amount   INTEGER NOT NULL,
                settled  INTEGER DEFAULT 0,
                won      INTEGER DEFAULT 0
            )
        """)
        # Таблица для хранения акций «бонус за подписку»
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sub_campaigns (
                id          TEXT    PRIMARY KEY,
                coins       INTEGER NOT NULL,
                channel_id  TEXT    NOT NULL,
                channel_url TEXT    NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now'))
            )
        """)
        # Таблица учёта: кто уже получил бонус за конкретную акцию
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sub_bonuses (
                user_id     INTEGER NOT NULL,
                campaign_id TEXT    NOT NULL,
                PRIMARY KEY (user_id, campaign_id)
            )
        """)
        # Таблица модераторов — добавляются через панель админа
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moderators (
                user_id    INTEGER PRIMARY KEY,
                added_at   TEXT    DEFAULT (datetime('now'))
            )
        """)
        # Таблица промокодов — создаются админом через панель
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code              TEXT    PRIMARY KEY,
                amount            REAL    NOT NULL,
                activations_left  INTEGER NOT NULL,
                created_at        TEXT    DEFAULT (datetime('now'))
            )
        """)
        # История активаций промокодов: кто и какой код уже использовал.
        # PRIMARY KEY (user_id, code) физически не даёт активировать
        # один и тот же код дважды одним пользователем.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_activations (
                user_id      INTEGER NOT NULL,
                code         TEXT    NOT NULL,
                activated_at TEXT    DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, code)
            )
        """)
        await db.commit()

        # ── Миграция: добавить tournament_id в matches если колонки нет ──
        # Нужно при обновлении со старой версии бота (без турниров)
        async with db.execute("PRAGMA table_info(matches)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        if "tournament_id" not in columns:
            await db.execute(
                "ALTER TABLE matches ADD COLUMN tournament_id INTEGER DEFAULT NULL"
            )
            await db.commit()
            log.info("БД: добавлена колонка tournament_id в таблицу matches")

        # ── Миграция: добавить odds_draw в matches если колонки нет ──
        # Нужно при обновлении со старой версии бота (без ставок на ничью).
        # У старых матчей odds_draw останется NULL — ставка на ничью для
        # них просто не предлагается (см. has_draw_odds()).
        async with db.execute("PRAGMA table_info(matches)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        if "odds_draw" not in columns:
            await db.execute(
                "ALTER TABLE matches ADD COLUMN odds_draw REAL DEFAULT NULL"
            )
            await db.commit()
            log.info("БД: добавлена колонка odds_draw в таблицу matches")


# ─────────────────────────────────────────────
#  ХЕЛПЕРЫ БД
# ─────────────────────────────────────────────
async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def register_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # Стартовый баланс строго START_BALANCE (20 000) только при первой регистрации.
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, balance) "
            "VALUES (?,?,?,?)",
            (user_id, username, first_name, START_BALANCE),
        )
        # Обновляем никнейм/имя при каждом /start (баланс не трогаем).
        await db.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (username, first_name, user_id),
        )
        await db.commit()


async def update_balance(user_id: int, delta: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (delta, user_id),
        )
        await db.commit()


async def get_all_users() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def create_sub_campaign(campaign_id: str, coins: int, channel_id: str, channel_url: str):
    """Создать запись акции «бонус за подписку» в БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sub_campaigns (id, coins, channel_id, channel_url) "
            "VALUES (?,?,?,?)",
            (campaign_id, coins, channel_id, channel_url),
        )
        await db.commit()


async def get_sub_campaign(campaign_id: str) -> Optional[dict]:
    """Получить данные акции по campaign_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sub_campaigns WHERE id=?", (campaign_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def has_claimed_bonus(user_id: int, campaign_id: str) -> bool:
    """Проверить, получал ли пользователь бонус за данную акцию."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM sub_bonuses WHERE user_id=? AND campaign_id=?",
            (user_id, campaign_id),
        ) as cur:
            return (await cur.fetchone()) is not None


async def mark_bonus_claimed(user_id: int, campaign_id: str):
    """Записать факт выдачи бонуса (защита от повторного получения)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO sub_bonuses (user_id, campaign_id) VALUES (?,?)",
            (user_id, campaign_id),
        )
        await db.commit()


async def get_user_bet_on_match(user_id: int, match_id: int) -> Optional[dict]:
    """Получить ставку пользователя на конкретный матч (любого типа)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bets WHERE user_id=? AND match_id=? AND settled=0 LIMIT 1",
            (user_id, match_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_users_count() -> int:
    """Получить количество зарегистрированных пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ── Хелперы промокодов ──────────────────────────────────────────────
async def create_promo_code_db(code: str, amount: float, activations: int) -> bool:
    """
    Создать новый промокод.
    Возвращает False, если промокод с таким названием уже существует
    (названия промокодов уникальны — регистр приводится к верхнему).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO promo_codes (code, amount, activations_left) "
            "VALUES (?,?,?)",
            (code, amount, activations),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_promo_code_db(code: str) -> Optional[dict]:
    """Получить данные промокода по названию."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM promo_codes WHERE code=?", (code,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def activate_promo_code_db(user_id: int, code: str) -> tuple[str, Optional[float]]:
    """
    Атомарно активирует промокод для пользователя.

    Возвращает (статус, сумма):
      статус "not_found"      — такого промокода нет
      статус "already_used"   — этот пользователь уже активировал этот код
      статус "no_activations" — активации закончились
      статус "ok"             — успех, сумма начислена на баланс

    Порядок проверок защищён от гонок (двойной тап / параллельные запросы):
    1) Промокод должен существовать.
    2) Запись в promo_activations вставляется через INSERT OR IGNORE —
       благодаря PRIMARY KEY (user_id, code) повторная вставка того же
       пользователя для того же кода физически невозможна, поэтому
       rowcount==0 однозначно значит «уже активировал».
    3) Только после успешной вставки списывается активация промокода
       атомарным UPDATE ... WHERE activations_left > 0. Если активаций
       не осталось — откатываем вставку из шага 2, чтобы не заблокировать
       пользователю возможность попробовать другой код.
    """
    code = code.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        async with db.execute(
            "SELECT amount FROM promo_codes WHERE code=?", (code,)
        ) as cur:
            promo = await cur.fetchone()
        if promo is None:
            return "not_found", None

        ins_cur = await db.execute(
            "INSERT OR IGNORE INTO promo_activations (user_id, code) VALUES (?,?)",
            (user_id, code),
        )
        if ins_cur.rowcount == 0:
            await db.commit()
            return "already_used", None

        upd_cur = await db.execute(
            "UPDATE promo_codes SET activations_left = activations_left - 1 "
            "WHERE code=? AND activations_left > 0",
            (code,),
        )
        if upd_cur.rowcount == 0:
            # Активаций не осталось — откатываем вставку из истории активаций
            await db.execute(
                "DELETE FROM promo_activations WHERE user_id=? AND code=?",
                (user_id, code),
            )
            await db.commit()
            return "no_activations", None

        amount = promo["amount"]
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (amount, user_id),
        )
        await db.commit()
        return "ok", amount


# ── Хелперы модераторов ─────────────────────────────────────────────
async def get_moderators() -> list:
    """Все модераторы (с именами из users, если зарегистрированы)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT m.user_id, u.username, u.first_name
            FROM moderators m
            LEFT JOIN users u ON u.user_id = m.user_id
        """) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def is_moderator_db(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM moderators WHERE user_id=?", (user_id,)
        ) as cur:
            return (await cur.fetchone()) is not None


async def add_moderator_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO moderators (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()


async def remove_moderator_db(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM moderators WHERE user_id=?", (user_id,))
        await db.commit()


async def get_top_users(limit: int = 100) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users ORDER BY balance DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_user_rank(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*)+1 FROM users "
            "WHERE balance > (SELECT balance FROM users WHERE user_id=?)",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 1


async def get_user_stats(user_id: int) -> dict:
    """Считает успешные/неудачные прогнозы и текущую серию побед
    по уже рассчитанным (settled=1) ставкам пользователя.

    Ставки на ОТМЕНЁННЫЕ матчи (cancel_match_db помечает их won=0,settled=1
    при возврате денег) намеренно исключены — это не проигрыш, а откат,
    и не должен портить статистику/серию побед.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.won FROM bets b
               JOIN matches m ON b.match_id = m.id
               WHERE b.user_id=? AND b.settled=1 AND m.status != 'cancelled'
               ORDER BY b.id DESC""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()

    wins   = sum(1 for r in rows if r["won"])
    losses = len(rows) - wins

    # Серия побед — считаем подряд идущие победы от самой свежей ставки
    streak = 0
    for r in rows:
        if r["won"]:
            streak += 1
        else:
            break

    return {"wins": wins, "losses": losses, "streak": streak}


# Пороги рангов: (макс. побед для этого ранга, название)
# Список отсортирован по возрастанию — первый подходящий порог и определяет ранг.
RANK_TIERS = [
    (2,   "🌱 Новичок"),
    (7,   "🔰 Стажёр"),
    (15,  "📈 Любитель"),
    (30,  "📊 Аналитик"),
    (50,  "🎯 Снайпер"),
    (90,  "🏆 Профи"),
    (150, "💎 Мастер"),
    (250, "🥇 Легенда"),
    (400, "👑 ОЛД"),
]


def get_rank_title(wins: int) -> str:
    """Возвращает звание игрока по количеству успешных прогнозов."""
    for threshold, title in RANK_TIERS:
        if wins <= threshold:
            return title
    return "🚀 Икона ставок"


async def create_match_db(
    t1, t2, fmt, o1, o2, has_mvp: bool, players_str: Optional[str], mvp_odds: Optional[float],
    tournament_id: Optional[int] = None, odds_draw: Optional[float] = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO matches "
            "(tournament_id,team1,team2,format,odds1,odds2,odds_draw,has_mvp,mvp_players,mvp_odds) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tournament_id, t1, t2, fmt, o1, o2, odds_draw, 1 if has_mvp else 0, players_str, mvp_odds),
        )
        await db.commit()
        return cur.lastrowid


async def get_match(match_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE id=?", (match_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_active_matches() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE status IN ('pending','live')"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_pending_matches() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE status='pending'"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Хелперы турниров ────────────────────────────────────

async def create_tournament_db(name: str) -> int:
    """Создать турнир; вернуть его ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tournaments (name) VALUES (?)", (name,)
        )
        await db.commit()
        return cur.lastrowid


async def get_active_tournaments() -> list:
    """Все активные турниры (status='active')."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournaments WHERE status='active' ORDER BY id"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_tournament(tournament_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tournaments WHERE id=?", (tournament_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_active_matches_by_tournament(tournament_id: int) -> list:
    """Активные матчи конкретного турнира."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM matches WHERE status IN ('pending','live') "
            "AND tournament_id=?",
            (tournament_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_matches_by_tournament(tournament_id: int) -> int:
    """Сколько всего матчей (любого статуса) привязано к турниру."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM matches WHERE tournament_id=?",
            (tournament_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def delete_tournament_db(tournament_id: int):
    """Удалить турнир. Привязанные матчи НЕ удаляются — у них просто
    обнуляется tournament_id (становятся матчами «без турнира»), чтобы
    не потерять историю ставок и расчётов.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET tournament_id=NULL WHERE tournament_id=?",
            (tournament_id,),
        )
        await db.execute(
            "DELETE FROM tournaments WHERE id=?", (tournament_id,)
        )
        await db.commit()


async def set_match_maps(match_id: int, maps: list[str]):
    """Сохраняет список карт (через MAP_SEP) и закрывает приём ставок."""
    maps_str = MAP_SEP.join(maps)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET map=?, status='live' WHERE id=?",
            (maps_str, match_id),
        )
        await db.commit()


async def update_match_teams(match_id: int, team1: str, team2: str):
    """Меняет названия команд в матче. Ставки пользователей (target='team1'/'team2') не трогаем —
    они остаются корректными, так как привязаны к стороне, а не к имени."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET team1=?, team2=? WHERE id=?",
            (team1, team2, match_id),
        )
        await db.commit()


async def finish_match_db(match_id: int, winner: str, mvp: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE matches SET status='finished', winner=?, mvp=? WHERE id=?",
            (winner, mvp, match_id),
        )
        await db.commit()


async def cancel_match_db(match_id: int) -> Optional[list]:
    """Отменить матч: вернуть всем игрокам сумму их несыгранных ставок на
    этот матч и пометить ставки как рассчитанные (won=0, settled=1), чтобы
    они больше не попадали в выплаты при повторном расчёте.
    Статус матча переводится в 'cancelled'.

    Атомарно проверяет, что матч ещё не finished/cancelled — если кто-то
    (второй админ, двойной тап) уже отменил/завершил его, возвращает None,
    чтобы не выполнить возврат денег повторно.

    Возвращает список {user_id, amount} по каждой возвращённой ставке,
    либо None если матч уже был в терминальном статусе.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Атомарная проверка+обновление статуса — защита от двойной отмены
        cur = await db.execute(
            "UPDATE matches SET status='cancelled' "
            "WHERE id=? AND status IN ('pending','live')",
            (match_id,),
        )
        if cur.rowcount == 0:
            await db.commit()
            return None

        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bets WHERE match_id=? AND settled=0", (match_id,)
        ) as bets_cur:
            bets = [dict(r) for r in await bets_cur.fetchall()]

        refunds = []
        for bet in bets:
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id=?",
                (bet["amount"], bet["user_id"]),
            )
            await db.execute(
                "UPDATE bets SET settled=1, won=0 WHERE id=?",
                (bet["id"],),
            )
            refunds.append({"user_id": bet["user_id"], "amount": bet["amount"]})

        await db.commit()

    return refunds


async def create_bet(user_id, match_id, bet_type, target, amount):
    """Атомарно списывает баланс и создаёт ставку в одной транзакции."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем баланс внутри транзакции (защита от race condition)
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row or row[0] < amount:
                raise ValueError("Недостаточно средств")
        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id=?",
            (amount, user_id),
        )
        await db.execute(
            "INSERT INTO bets (user_id,match_id,bet_type,target,amount) "
            "VALUES (?,?,?,?,?)",
            (user_id, match_id, bet_type, target, amount),
        )
        await db.commit()


async def get_unsettled_bets(match_id: int) -> list:
    """Только нерассчитанные ставки — защита от двойного начисления."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bets WHERE match_id=? AND settled=0", (match_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_user_active_bets(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.*, m.team1, m.team2 FROM bets b
               JOIN matches m ON b.match_id = m.id
               WHERE b.user_id=? AND b.settled=0""",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_active_bettors() -> list:
    """Все несыгранные (settled=0) ставки со всеми данными для группировки
    по пользователю: кто, сколько ставок, на что и сколько коинов.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT b.user_id, b.bet_type, b.target, b.amount,
                      m.id AS match_id, m.team1, m.team2,
                      u.username, u.first_name
               FROM bets b
               JOIN matches m ON b.match_id = m.id
               LEFT JOIN users u ON u.user_id = b.user_id
               WHERE b.settled = 0
               ORDER BY b.user_id, b.id"""
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def settle_bets(match_id: int) -> list:
    """Рассчитать все ставки матча. Возвращает список результатов."""
    match = await get_match(match_id)
    bets  = await get_unsettled_bets(match_id)
    results = []

    # Открываем ОДНО соединение для всех UPDATE
    async with aiosqlite.connect(DB_PATH) as db:
        for bet in bets:
            won        = False
            multiplier = 0.0

            if bet["bet_type"] == "winner":
                if bet["target"] == "team1" and match["winner"] == "team1":
                    won, multiplier = True, match["odds1"]
                elif bet["target"] == "team2" and match["winner"] == "team2":
                    won, multiplier = True, match["odds2"]
                elif bet["target"] == "draw" and match["winner"] == "draw":
                    won, multiplier = True, match["odds_draw"] or 0.0
            elif bet["bet_type"] == "mvp":
                if bet["target"].strip().lower() == (match["mvp"] or "").strip().lower():
                    won, multiplier = True, MVP_MULTIPLIER

            # Выплата = ставка × коэф (целое число)
            payout = round(bet["amount"] * multiplier) if won else 0

            await db.execute(
                "UPDATE bets SET settled=1, won=? WHERE id=?",
                (1 if won else 0, bet["id"]),
            )

            if won and payout > 0:
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id=?",
                    (payout, bet["user_id"]),
                )

            results.append({
                "user_id": bet["user_id"],
                "won":     won,
                "payout":  payout,
                "amount":  bet["amount"],
            })

        await db.commit()   # один коммит для всего пакета

    return results


# ─────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ─────────────────────────────────────────────
ROSTER_SEP = "||"  # разделитель составов двух команд при сохранении в БД (mvp_players)


def parse_players(raw: Optional[str]) -> list[str]:
    """Разбить строку игроков (общий плоский список) по запятым и очистить пробелы.
    Понимает оба формата хранения: старый плоский 'A, B, C' и новый по-командный
    'A, B||C, D' — во втором случае просто отдаёт всех игроков одним списком."""
    if not raw:
        return []
    raw = raw.replace(ROSTER_SEP, ",")
    return [p.strip() for p in raw.split(",") if p.strip()]


def parse_players_by_team(raw: Optional[str]) -> tuple[list[str], list[str]]:
    """Разбивает сохранённую строку состава на два списка — по командам.
    Новый формат хранения: 'p1, p2, p3||p4, p5, p6'.
    Старый формат (без разделителя, из старых матчей) — весь список считается
    общим и возвращается первым элементом кортежа, второй список пустой."""
    if not raw:
        return [], []
    if ROSTER_SEP in raw:
        left, right = raw.split(ROSTER_SEP, 1)
        return parse_players(left), parse_players(right)
    return parse_players(raw), []


def parse_maps(raw: Optional[str]) -> list[str]:
    """Разбить сохранённую строку карт по разделителю."""
    if not raw:
        return []
    return [m.strip() for m in raw.split(MAP_SEP) if m.strip()]


def parse_quick_match_text(text: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Разбирает текст быстрого создания матча одним сообщением.

    Формат:
        Team1 - Team2            (или «Team1 vs Team2»)
        Формат: BO3              (необязательно, по умолчанию BO3)
        Кэф1: 1.85
        Кэф2: 1.95
        Ничья: 4.20               (необязательно — убрать строку, если ставок на ничью нет)
        Состав1: A, B, C, ...     (состав Team1 — только вместе со «Состав2»; выплата на MVP всегда x2)
        Состав2: D, E, F, ...     (состав Team2 — только вместе со «Состав1»)
        Турнир: Название          (необязательно, ищется среди активных турниров)

    Порядок строк после первой не важен, лишние/незнакомые строки игнорируются.
    Возвращает (data, error) — если error задан, data всегда None.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return None, "❌ Пустое сообщение. Пришли данные матча по шаблону выше."

    first = lines[0]
    low = first.lower()
    sep_used = None
    for sep in (" vs ", " — ", " – ", " - "):
        if sep in low:
            sep_used = sep
            break
    if not sep_used:
        return None, (
            "❌ Первая строка должна быть в формате «Команда1 - Команда2» "
            "(или «Команда1 vs Команда2»)."
        )
    idx = low.index(sep_used)
    team1 = first[:idx].strip()
    team2 = first[idx + len(sep_used):].strip()
    if not team1 or not team2:
        return None, "❌ Не удалось распознать названия команд в первой строке."

    fields: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        fields[label.strip().lower()] = value.strip()

    def get(*aliases: str) -> Optional[str]:
        for a in aliases:
            if a in fields:
                return fields[a]
        return None

    fmt = (get("формат", "format") or "BO3").upper()
    if fmt not in FORMAT_MAP_COUNT:
        return None, (
            f"❌ Неизвестный формат «{fmt}». "
            f"Допустимые значения: {', '.join(FORMAT_MAP_COUNT)}."
        )

    def to_odds(raw: Optional[str], label: str) -> tuple[Optional[float], Optional[str]]:
        if raw is None:
            return None, f"❌ Не хватает поля «{label}»."
        cleaned = raw.replace(",", ".")
        try:
            value = float(cleaned)
            if value <= 1.0:
                raise ValueError
        except ValueError:
            return None, f"❌ Некорректный коэффициент в поле «{label}»: {raw}"
        return value, None

    odds1, err = to_odds(get("кэф1", "к1", "коэф1"), "Кэф1")
    if err:
        return None, err
    odds2, err = to_odds(get("кэф2", "к2", "коэф2"), "Кэф2")
    if err:
        return None, err

    odds_draw = None
    draw_raw = get("ничья", "кэф ничья", "кничья")
    if draw_raw and draw_raw.strip("-").strip():
        odds_draw, err = to_odds(draw_raw, "Ничья")
        if err:
            return None, err

    players1_raw   = get("состав1", "состав 1", "состав команды 1")
    players2_raw   = get("состав2", "состав 2", "состав команды 2")
    legacy_raw     = get("составы", "игроки")  # обратная совместимость со старым шаблоном
    # Строка «MVP: ...» больше не обязательна и игнорируется — выплата на MVP всегда x2.
    # Оставлена для совместимости со старыми сообщениями, которые её ещё содержат.

    has_mvp = False
    players_str = None
    if players1_raw or players2_raw or legacy_raw:
        if players1_raw or players2_raw:
            if not (players1_raw and players2_raw):
                return None, "❌ «Состав1» и «Состав2» указываются вместе — впиши составы обеих команд."
            p1 = parse_players(players1_raw)
            p2 = parse_players(players2_raw)
            if not p1 or not p2:
                return None, "❌ В каждом составе должен быть минимум 1 игрок."
            players_str = f"{', '.join(p1)}{ROSTER_SEP}{', '.join(p2)}"
        else:
            players = parse_players(legacy_raw)
            if len(players) < 2:
                return None, "❌ В поле «Составы» нужно минимум 2 игрока через запятую."
            players_str = ", ".join(players)
        has_mvp = True

    mvp_odds = MVP_MULTIPLIER if has_mvp else None
    tournament_name = get("турнир")

    return {
        "team1": team1,
        "team2": team2,
        "fmt": fmt,
        "odds1": odds1,
        "odds2": odds2,
        "odds_draw": odds_draw,
        "has_mvp": has_mvp,
        "mvp_odds": mvp_odds,
        "players_str": players_str,
        "tournament_name": tournament_name,
    }, None


def user_display(u: dict) -> str:
    return f"@{u['username']}" if u.get("username") else u.get("first_name", "Игрок")


def has_draw_odds(m: dict) -> bool:
    """Доступна ли ставка на ничью для этого матча (старые матчи без odds_draw — нет)."""
    return m.get("odds_draw") is not None


def winner_target_label(target: str, team1: str, team2: str) -> str:
    """Человекочитаемое название исхода ставки/победителя матча: команда или «Ничья»."""
    if target == "draw":
        return "🤝 Ничья"
    if target == "team1":
        return team1
    if target == "team2":
        return team2
    return target  # неожиданное значение — показываем как есть


def fmt_coins(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def validate_bet_amount(raw: str, balance: int) -> tuple[Optional[int], Optional[str]]:
    """
    Строгая проверка суммы ставки.
    Возвращает (amount, error_message). Если amount is None — ввод некорректен.
    Правила:
      - только положительное целое число (без знаков, пробелов внутри, дробей и т.п.)
      - amount > 0
      - amount <= текущий баланс пользователя
    """
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None, "❌ Введи целое положительное число (например: 500). Попробуй ещё раз:"
    amount = int(raw)
    if amount <= 0:
        return None, "❌ Сумма ставки должна быть больше 0. Попробуй ещё раз:"
    if amount > balance:
        return None, (
            f"❌ Недостаточно коинов!\n"
            f"🪙 Твой баланс: <b>{fmt_coins(balance)}</b>\n"
            f"Введи сумму не больше {fmt_coins(balance)}:"
        )
    return amount, None


def normalize_promo_code(raw: str) -> str:
    """Приводит промокод к единому виду: обрезка пробелов + верхний регистр."""
    return (raw or "").strip().upper()


def validate_promo_activations(raw: str) -> tuple[Optional[int], Optional[str]]:
    """Целое положительное число активаций промокода."""
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None, "❌ Введи целое положительное число (например: 10). Попробуй ещё раз:"
    value = int(raw)
    if value <= 0:
        return None, "❌ Количество активаций должно быть больше 0. Попробуй ещё раз:"
    return value, None


def validate_promo_amount(raw: str) -> tuple[Optional[float], Optional[str]]:
    """Сумма начисления по промокоду: положительное число, допускаются дробные (150 или 500.50)."""
    raw = (raw or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None, "❌ Введи число (например: 150 или 500.50). Попробуй ещё раз:"
    if value <= 0:
        return None, "❌ Сумма должна быть больше 0. Попробуй ещё раз:"
    return value, None


# ─────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def main_menu_kb(is_staff: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="⚔️ Матчи / Ставки"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="🏆 Топ игроков"),     KeyboardButton(text="ℹ️ О боте")],
        [KeyboardButton(text="🎁 Активировать промокод")],
    ]
    if is_staff:
        rows.append([KeyboardButton(text="🔐 Панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Создать турнир",                callback_data="admin:create_tournament")],
        [InlineKeyboardButton(text="🗑 Удалить турнир",                 callback_data="admin:manage_tournaments")],
        [InlineKeyboardButton(text="➕ Создать матч",                  callback_data="admin:create_match")],
        [InlineKeyboardButton(text="⚡ Быстро создать матч (одним сообщением)", callback_data="admin:quick_match")],
        [InlineKeyboardButton(text="🗺 Указать карту и закрыть ставки", callback_data="admin:set_map")],
        [InlineKeyboardButton(text="✏️ Изменить названия команд",       callback_data="admin:edit_teams")],
        [InlineKeyboardButton(text="🏆 Завершить матч (Расчет)",        callback_data="admin:finish_match")],
        [InlineKeyboardButton(text="↩️ Отменить матч (вернуть ставки)", callback_data="admin:cancel_match")],
        [InlineKeyboardButton(text="🎲 Кто сейчас делает ставки",       callback_data="admin:active_bettors")],
        [InlineKeyboardButton(text="💰 Выдать бонус всем",              callback_data="admin:bonus")],
        [InlineKeyboardButton(text="🎯 Выдать токены игроку",           callback_data="admin:give_tokens")],
        [InlineKeyboardButton(text="📢 Создать бонус за подписку",      callback_data="admin:sub_bonus")],
        [InlineKeyboardButton(text="👥 Список игроков",                 callback_data="admin:players_list")],
        [InlineKeyboardButton(text="📣 Написать всем игрокам",          callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🛡 Управление модераторами",        callback_data="admin:moders")],
        [InlineKeyboardButton(text="🎁 Создать промокод",               callback_data="admin:create_promo")],
    ])


def moder_menu_kb() -> InlineKeyboardMarkup:
    """Панель модератора: создать матч, бан-пики, завершить матч."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать матч",                  callback_data="admin:create_match")],
        [InlineKeyboardButton(text="⚡ Быстро создать матч (одним сообщением)", callback_data="admin:quick_match")],
        [InlineKeyboardButton(text="🗺 Бан-пики / закрыть ставки",     callback_data="admin:set_map")],
        [InlineKeyboardButton(text="🏆 Завершить матч (Расчет)",        callback_data="admin:finish_match")],
        [InlineKeyboardButton(text="↩️ Отменить матч (вернуть ставки)", callback_data="admin:cancel_match")],
    ])


def match_format_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="BO1", callback_data="fmt:BO1"),
        InlineKeyboardButton(text="BO2", callback_data="fmt:BO2"),
        InlineKeyboardButton(text="BO3", callback_data="fmt:BO3"),
        InlineKeyboardButton(text="BO5", callback_data="fmt:BO5"),
    ]])


def mvp_toggle_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да",  callback_data="mvp_toggle:yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="mvp_toggle:no"),
    ]])


def draw_toggle_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да",  callback_data="draw_toggle:yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="draw_toggle:no"),
    ]])


def maps_pick_kb(remaining_maps: list[str]) -> InlineKeyboardMarkup:
    """Клавиатура выбора одной карты из списка ещё доступных карт. По 2 в ряд."""
    rows = []
    for i in range(0, len(remaining_maps), 2):
        row = [
            InlineKeyboardButton(text=f"🗺 {m}", callback_data=f"pickmap:{m}")
            for m in remaining_maps[i:i + 2]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def matches_kb(matches: list, cb_prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"⚔️ {m['team1']} vs {m['team2']} [{m['format']}]",
            callback_data=f"{cb_prefix}:{m['id']}",
        )]
        for m in matches
    ])


def cancel_match_confirm_kb(match_id: int) -> InlineKeyboardMarkup:
    """Подтверждение отмены матча с возвратом ставок."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отменить и вернуть ставки", callback_data=f"cancel_match_yes:{match_id}")],
        [InlineKeyboardButton(text="❌ Не отменять",                    callback_data="cancel_match_no")],
    ])


def tournaments_kb(tournaments: list) -> InlineKeyboardMarkup:
    """Клавиатура выбора турнира — каждая кнопка несёт tourn:<id>."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🏆 {t['name']}",
            callback_data=f"tourn:{t['id']}",
        )]
        for t in tournaments
    ])


def tournaments_manage_kb(tournaments: list) -> InlineKeyboardMarkup:
    """Список турниров с кнопкой удаления у каждого + назад в админ-панель."""
    rows = [
        [InlineKeyboardButton(
            text=f"🗑 Удалить «{t['name']}»",
            callback_data=f"tourn_del:{t['id']}",
        )]
        for t in tournaments
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tourn_del:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tournament_delete_confirm_kb(tournament_id: int) -> InlineKeyboardMarkup:
    """Подтверждение удаления конкретного турнира."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"tourn_del_yes:{tournament_id}")],
        [InlineKeyboardButton(text="❌ Отмена",        callback_data="admin:manage_tournaments")],
    ])


def matches_with_back_kb(matches: list, cb_prefix: str, show_back: bool) -> InlineKeyboardMarkup:
    """Список матчей + опциональная кнопка «🔙 К списку турниров»."""
    rows = [
        [InlineKeyboardButton(
            text=f"⚔️ {m['team1']} vs {m['team2']} [{m['format']}]",
            callback_data=f"{cb_prefix}:{m['id']}",
        )]
        for m in matches
    ]
    if show_back:
        rows.append([InlineKeyboardButton(
            text="🔙 К списку турниров",
            callback_data="back_to_tournaments",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def winner_choice_kb(match_id, team1, team2, odds1, odds2, odds_draw=None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🛡 {team1}  (x{odds1})",
                              callback_data=f"bet_winner:{match_id}:team1")],
        [InlineKeyboardButton(text=f"⚔️ {team2}  (x{odds2})",
                              callback_data=f"bet_winner:{match_id}:team2")],
    ]
    if odds_draw is not None:
        rows.append([InlineKeyboardButton(text=f"🤝 Ничья  (x{odds_draw})",
                                          callback_data=f"bet_winner:{match_id}:draw")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mvp_choice_kb(match_id: int, players: list) -> InlineKeyboardMarkup:
    """Игроки — по 2 в ряд, аккуратной сеткой. Коэффициент на MVP всегда фиксированный x2."""
    rows = []
    for i in range(0, len(players), 2):
        row = []
        for p in players[i:i + 2]:
            row.append(InlineKeyboardButton(
                text=f"⭐️ {p}  (x{MVP_MULTIPLIER:g})",
                callback_data=f"bet_mvp:{match_id}:{p}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_mvp_kb(match_id: int, players: list) -> InlineKeyboardMarkup:
    """Игроки — по 2 в ряд."""
    rows = []
    for i in range(0, len(players), 2):
        row = []
        for p in players[i:i + 2]:
            row.append(InlineKeyboardButton(
                text=f"⭐️ {p}",
                callback_data=f"admin_mvp:{match_id}:{p}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def match_bet_options_kb(
    match_id: int, has_mvp: bool, back_cb: str = "back_to_matches"
) -> InlineKeyboardMarkup:
    """Кнопки ставок. back_cb — callback для кнопки «Назад»:
       'back_to_matches' (1 турнир) или 'tourn:<id>' (несколько турниров).
    """
    rows = [
        [InlineKeyboardButton(text="🔥 Поставить на Победителя",
                              callback_data=f"bet_opt_winner:{match_id}")],
    ]
    if has_mvp:
        rows.append([InlineKeyboardButton(text="⭐️ Поставить на MVP",
                                          callback_data=f"bet_opt_mvp:{match_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад к матчам",
                                      callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_winner_kb(match_id, team1, team2, has_draw: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🛡 {team1}",
                              callback_data=f"admin_winner:{match_id}:team1")],
        [InlineKeyboardButton(text=f"⚔️ {team2}",
                              callback_data=f"admin_winner:{match_id}:team2")],
    ]
    if has_draw:
        rows.append([InlineKeyboardButton(text="🤝 Ничья",
                                          callback_data=f"admin_winner:{match_id}:draw")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_mvp_kb(match_id: int) -> InlineKeyboardMarkup:
    """Если у матча нет MVP — сразу кнопка завершить расчёт."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Завершить расчёт (без MVP)",
                              callback_data=f"admin_mvp_skip:{match_id}")],
    ])


# ─────────────────────────────────────────────
#  КАРТОЧКА МАТЧА
# ─────────────────────────────────────────────
def maps_display_str(maps: list[str]) -> str:
    """Например: '1. Sandstone, 2. Rust, 3. Province'"""
    return ", ".join(f"{i}. {m}" for i, m in enumerate(maps, 1))


def match_card_text(m: dict) -> str:
    status_label = {
        "pending":  "⏳ Ожидание бан-пиков",
        "live":     "🔴 Идёт игра — LIVE",
        "finished": "✅ Завершён",
    }.get(m["status"], m["status"])

    maps = parse_maps(m.get("map"))
    if maps:
        map_line = f"🗺 Карты матча: <b>{maps_display_str(maps)}</b>"
    else:
        map_line = "🗺 Карты определятся после бан-пиков"

    has_mvp = bool(m.get("has_mvp", 1))
    mvp_block = ""
    if has_mvp:
        p1, p2 = parse_players_by_team(m.get("mvp_players"))
        total = len(p1) + len(p2)
        if p2:
            roster_lines = (
                f"  🛡 <b>{m['team1']}:</b> {', '.join(p1)}\n"
                f"  ⚔️ <b>{m['team2']}:</b> {', '.join(p2)}\n"
            )
        else:
            # старый матч без разбивки по командам — плоский список как раньше
            roster_lines = f"  {'  |  '.join(p1)}\n"
        mvp_block = (
            f"  ⭐️ MVP: <b>x{MVP_MULTIPLIER:g}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👾 <b>Составы ({total} игроков):</b>\n"
            f"{roster_lines}"
        )
    else:
        mvp_block = "  ⭐️ Ставки на MVP: <i>не проводятся для этого матча</i>\n"

    draw_line = f"  🤝 Ничья: <b>x{m['odds_draw']}</b>\n" if has_draw_odds(m) else ""

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️  <b>{m['team1']}</b>  vs  <b>{m['team2']}</b>\n"
        f"📋 Формат: <b>{m['format']}</b>   {status_label}\n"
        f"{map_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💹 <b>Коэффициенты:</b>\n"
        f"  🛡 {m['team1']}: <b>x{m['odds1']}</b>\n"
        f"  ⚔️ {m['team2']}: <b>x{m['odds2']}</b>\n"
        f"{draw_line}"
        f"{mvp_block}"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message):
    uid   = message.from_user.id
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or "Игрок"
    await register_user(uid, uname, fname)
    staff = is_admin(uid) or await is_moder(uid)
    await message.answer(
        f"⚡ <b>SKYRED & MHERO | PREDICTOR BOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔌 Система инициализирована, <b>{fname}</b>. Канал связи с "
        f"киберстатистикой Standoff 2 — открыт.\n\n"
        f"🪙 На твой счёт зачислен стартовый капитал: "
        f"<b>{fmt_coins(START_BALANCE)} коинов</b>\n\n"
        f"🎯 Анализируй матчи, делай точные прогнозы и поднимайся в "
        f"<b>топ лидеров</b>.\n"
        f"Удача любит холодный расчёт. Поехали? 🚀",
        reply_markup=main_menu_kb(is_staff=staff),
    )


# ─────────────────────────────────────────────
#  ПРОФИЛЬ
# ─────────────────────────────────────────────
@router.message(F.text == "👤 Профиль")
async def profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи /start")
        return
    bets  = await get_user_active_bets(message.from_user.id)
    place = await get_user_rank(message.from_user.id)
    stats = await get_user_stats(message.from_user.id)
    title = get_rank_title(stats["wins"])
    name  = user_display(user)

    if bets:
        bets_lines = []
        for b in bets:
            t     = "Победитель" if b["bet_type"] == "winner" else "MVP"
            emoji = "⚔️" if b["bet_type"] == "winner" else "⭐️"
            tgt = (
                b["target"] if b["bet_type"] == "mvp"
                else winner_target_label(b["target"], b["team1"], b["team2"])
            )
            bets_lines.append(
                f"• {emoji} <i>{b['team1']} vs {b['team2']}</i> | {t}: "
                f"<b>{tgt}</b> | 🪙 {fmt_coins(b['amount'])}"
            )
        bets_text = "\n".join(bets_lines)
    else:
        bets_text = "<i>Активных ставок нет</i>"

    await message.answer(
        f"👤 <b>ПРОФИЛЬ ИГРОКА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 <b>Игрок:</b> {name}\n"
        f"🆔 <b>ID:</b> <code>{user['user_id']}</code>\n"
        f"🎖 <b>Ранг:</b> {title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Баланс:</b> <code>{fmt_coins(user['balance'])}</code> коинов\n"
        f"🏆 <b>Место в топе:</b> <code>#{place}</code>\n\n"
        f"📊 <b>СТАТИСТИКА:</b>\n"
        f"✅ <b>Победы:</b> <code>{stats['wins']}</code>\n"
        f"❌ <b>Поражения:</b> <code>{stats['losses']}</code>\n"
        f"🔥 <b>Текущая серия побед:</b> <code>{stats['streak']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>АКТИВНЫЕ СТАВКИ:</b>\n"
        f"{bets_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ─────────────────────────────────────────────
#  ТОП ИГРОКОВ
# ─────────────────────────────────────────────
@router.message(F.text == "🏆 Топ игроков")
async def top_players(message: Message):
    top    = await get_top_users(100)
    medals = ["🥇", "🥈", "🥉"]
    lines  = []
    for i, u in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"  {i}."
        lines.append(f"{medal} <b>{user_display(u)}</b> — 🪙 {fmt_coins(u['balance'])}")

    rank = await get_user_rank(message.from_user.id)
    user = await get_user(message.from_user.id)
    my_bal = fmt_coins(user["balance"]) if user else "—"
    rank_display = f"#{rank}" if user else "нет (введи /start)"

    # Telegram ограничивает сообщения до 4096 символов — разбиваем на части
    header = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🏆 <b>ТОП-100 ИГРОКОВ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Твоё место: <b>{rank_display}</b>  |  🪙 {my_bal}"
    )

    # Разбиваем список по 25 строк на сообщение
    chunk_size = 25
    if not lines:
        # Подстраховка: если топ пуст, всё равно показываем заголовок/своё место,
        # иначе цикл ниже просто ничего не отправит и пользователь не получит ответа.
        await message.answer(header + "<i>Пока никто не играл.</i>\n" + footer)
        return
    for chunk_idx in range(0, len(lines), chunk_size):
        chunk = lines[chunk_idx:chunk_idx + chunk_size]
        is_first = chunk_idx == 0
        is_last  = chunk_idx + chunk_size >= len(lines)
        text = (header if is_first else "") + "\n".join(chunk) + (footer if is_last else "")
        await message.answer(text)


# ─────────────────────────────────────────────
#  О БОТЕ
# ─────────────────────────────────────────────
@router.message(F.text == "ℹ️ О боте")
async def about(message: Message):
    await message.answer(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎮 <b>SKYRED & MHERO | PREDICTOR BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Добро пожаловать туда, где решают не удача, а <b>знание игры</b>. "
        "Standoff 2 — твоя арена, прогнозы — твоё оружие. 🚀\n\n"
        "<b>🔥 Твой путь к вершине:</b>\n"
        f"1️⃣ Получи стартовый бонус <b>{fmt_coins(START_BALANCE)} 🪙</b> прямо при входе — "
        "и сразу в игру.\n"
        "2️⃣ Делай ставку <b>до начала бан-пиков</b> — как только матч стартует, "
        "приём прогнозов закрывается.\n"
        "3️⃣ Жди результат и забирай выплату по <b>коэффициенту</b> — чем точнее "
        "прогноз, тем жирнее куш.\n\n"
        "<b>⚔️ Типы прогнозов:</b>\n"
        "▪️ <b>Победитель</b> — угадай, какая команда возьмёт матч.\n"
        "▪️ ⭐️ <b>MVP</b> — вычисли игрока, который сделает игру.\n\n"
        "<b>📈 Рейтинг:</b>\n"
        "Каждая верная ставка — это шаг к <b>ТОП-10 лучших</b>. Здесь не просто "
        "играют — здесь <b>доказывают</b>, кто реально шарит в Standoff 2. "
        "Поднимайся в топе и забирай статус легенды. 🏆\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🍀 Все ставки виртуальные — играй смело, прокачивай аналитику и лови "
        "кураж. Удачи в прогнозах!\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# ─────────────────────────────────────────────
#  МАТЧИ / СТАВКИ  (с поддержкой турниров)
# ─────────────────────────────────────────────

async def _show_matches_for_tournament(
    target,           # Message или CallbackQuery
    tournament_id: int,
    show_back: bool,  # показывать кнопку «К списку турниров»?
    edit: bool = False,
):
    """Общий хелпер: показать матчи одного турнира.
    edit=True — редактировать существующее сообщение (для callback).
    """
    tourn   = await get_tournament(tournament_id)
    matches = await get_active_matches_by_tournament(tournament_id)
    name    = tourn["name"] if tourn else f"Турнир #{tournament_id}"

    if not matches:
        text = f"🏆 <b>{name}</b>\n\n😔 Матчей пока нет. Следи за обновлениями!"
        kb   = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 К списку турниров",
                                 callback_data="back_to_tournaments")
        ]]) if show_back else None
    else:
        text = f"🏆 <b>{name}</b>\n\n⚔️ Выбери матч для ставки:"
        kb   = matches_with_back_kb(matches, "view_match", show_back)

    msg = target if isinstance(target, Message) else target.message
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


async def _show_tournament_picker(target, edit: bool = False):
    """Показать список турниров для выбора."""
    tournaments = await get_active_tournaments()
    if not tournaments:
        text = "😔 Сейчас нет активных турниров и матчей. Следи за обновлениями!"
        msg = target if isinstance(target, Message) else target.message
        if edit:
            await msg.edit_text(text)
        else:
            await msg.answer(text)
        return

    text = "🏆 <b>Выберите турнир из списка:</b>"
    kb   = tournaments_kb(tournaments)
    msg  = target if isinstance(target, Message) else target.message
    if edit:
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


@router.message(F.text == "⚔️ Матчи / Ставки")
async def matches_list(message: Message, state: FSMContext):
    await state.clear()
    tournaments = await get_active_tournaments()

    if not tournaments:
        # Нет турниров вообще — показываем все матчи без фильтра (обратная совместимость)
        matches = await get_active_matches()
        if not matches:
            await message.answer("😔 Сейчас нет активных матчей. Следи за обновлениями!")
            return
        await message.answer(
            "⚔️ <b>Активные матчи</b>\nВыбери матч для ставки:",
            reply_markup=matches_kb(matches, "view_match"),
        )
        return

    if len(tournaments) == 1:
        # Единственный турнир — сразу показываем его матчи
        await _show_matches_for_tournament(message, tournaments[0]["id"], show_back=False)
    else:
        # Несколько турниров — показываем выбор
        await _show_tournament_picker(message)


@router.callback_query(F.data.startswith("tourn:"))
async def tournament_selected(call: CallbackQuery, state: FSMContext):
    """Пользователь выбрал турнир — показываем его матчи."""
    await state.clear()
    tournament_id = int(call.data.split(":")[1])
    tournaments   = await get_active_tournaments()
    show_back = len(tournaments) > 1
    await _show_matches_for_tournament(call, tournament_id, show_back=show_back, edit=True)
    await call.answer()


@router.callback_query(F.data == "back_to_tournaments")
async def back_to_tournaments(call: CallbackQuery, state: FSMContext):
    """Кнопка «🔙 К списку турниров»."""
    await state.clear()
    await _show_tournament_picker(call, edit=True)
    await call.answer()


@router.callback_query(F.data == "back_to_matches")
async def back_to_matches(call: CallbackQuery, state: FSMContext):
    """Возврат к списку матчей (используется когда турнир один или турниров нет)."""
    await state.clear()
    tournaments = await get_active_tournaments()

    if not tournaments:
        matches = await get_active_matches()
        if not matches:
            await call.message.edit_text("😔 Сейчас нет активных матчей.")
            await call.answer()
            return
        await call.message.edit_text(
            "⚔️ <b>Активные матчи</b>\nВыбери матч для ставки:",
            reply_markup=matches_kb(matches, "view_match"),
        )
        await call.answer()
        return

    if len(tournaments) == 1:
        await _show_matches_for_tournament(call, tournaments[0]["id"], show_back=False, edit=True)
    else:
        await _show_tournament_picker(call, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("view_match:"))
async def view_match(call: CallbackQuery):
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    if not m or m["status"] == "finished":
        await call.answer("Матч уже завершён.", show_alert=True)
        return

    # Определяем корректный callback для кнопки «Назад» в зависимости от числа турниров
    tournaments = await get_active_tournaments()
    if m.get("tournament_id") and len(tournaments) > 1:
        back_cb = f"tourn:{m['tournament_id']}"
    else:
        back_cb = "back_to_matches"

    if m["status"] == "live":
        await call.message.edit_text(
            match_card_text(m) + "\n\n🔒 <b>Приём ставок закрыт — матч идёт!</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад к матчам", callback_data=back_cb)
            ]]),
        )
        await call.answer()
        return
    await call.message.edit_text(
        match_card_text(m),
        reply_markup=match_bet_options_kb(match_id, bool(m.get("has_mvp", 1)), back_cb=back_cb),
    )
    await call.answer()


# ─────────────────────────────────────────────
#  СТАВКА НА ПОБЕДИТЕЛЯ
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("bet_opt_winner:"))
async def bet_opt_winner(call: CallbackQuery):
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    if not m or m["status"] != "pending":
        await call.answer("Ставки закрыты!", show_alert=True)
        return
    await call.message.edit_text(
        f"🔥 <b>Ставка на Победителя</b>\n\n{match_card_text(m)}\n\nВыбери команду:",
        reply_markup=winner_choice_kb(match_id, m["team1"], m["team2"],
                                      m["odds1"], m["odds2"], m.get("odds_draw")),
    )
    await call.answer()


@router.callback_query(F.data.startswith("bet_winner:"))
async def bet_winner_choose(call: CallbackQuery, state: FSMContext):
    parts    = call.data.split(":")
    match_id = int(parts[1])
    side     = parts[2]
    m = await get_match(match_id)
    if not m or m["status"] != "pending":
        await call.answer("Ставки закрыты!", show_alert=True)
        return

    if side == "draw" and not has_draw_odds(m):
        # Кэф на ничью для этого матча не задан — кнопка устарела/невалидна
        await call.answer("Ставка на ничью для этого матча недоступна.", show_alert=True)
        return

    # Проверка: уже есть ставка на этот матч?
    existing = await get_user_bet_on_match(call.from_user.id, match_id)
    if existing:
        bet_type = "Победитель" if existing["bet_type"] == "winner" else "MVP"
        await call.answer(
            f"❌ Ты уже поставил на этот матч ({bet_type})!\nНа один матч можно сделать только одну ставку.",
            show_alert=True,
        )
        return

    team_name = winner_target_label(side, m["team1"], m["team2"])
    if side == "draw":
        odds = m["odds_draw"]
    elif side == "team1":
        odds = m["odds1"]
    else:
        odds = m["odds2"]
    await state.update_data(match_id=match_id, side=side, odds=odds, target=side)
    await state.set_state(BetWinner.amount)
    user = await get_user(call.from_user.id)
    await call.message.edit_text(
        f"🔥 <b>Ставка на {team_name}</b>  (x{odds})\n\n"
        f"🪙 Твой баланс: <b>{fmt_coins(user['balance'])} коинов</b>\n\n"
        f"💬 Введи сумму ставки целым числом:"
    )


@router.message(StateFilter(BetWinner.amount))
async def bet_winner_amount(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи /start")
        await state.clear()
        return

    amount, error = validate_bet_amount(message.text, user["balance"])
    if error:
        await message.answer(error)
        return

    data = await state.get_data()
    m = await get_match(data["match_id"])
    if not m or m["status"] != "pending":
        await message.answer("❌ Ставки на этот матч уже закрыты.")
        await state.clear()
        return

    # Повторная проверка: вдруг другая вкладка/устройство уже поставило пока вводили сумму
    existing = await get_user_bet_on_match(message.from_user.id, data["match_id"])
    if existing:
        await message.answer("❌ Ты уже поставил на этот матч. На один матч — только одна ставка.")
        await state.clear()
        return

    try:
        await create_bet(message.from_user.id, data["match_id"], "winner", data["target"], amount)
    except ValueError:
        await message.answer("❌ Недостаточно коинов для ставки.")
        await state.clear()
        return
    await state.clear()

    team_name = winner_target_label(data["side"], m["team1"], m["team2"])
    payout    = round(amount * data["odds"])
    new_bal   = user["balance"] - amount

    staff = is_admin(message.from_user.id) or await is_moder(message.from_user.id)
    await message.answer(
        f"✅ <b>Ставка принята!</b>\n\n"
        f"⚔️ Матч: <b>{m['team1']} vs {m['team2']}</b>\n"
        f"🛡 Твой выбор: <b>{team_name}</b>  (x{data['odds']})\n"
        f"🪙 Ставка: <b>{fmt_coins(amount)}</b> коинов\n"
        f"💹 Возможный выигрыш: <b>{fmt_coins(payout)}</b> коинов\n\n"
        f"💰 Остаток баланса: <b>{fmt_coins(new_bal)}</b> коинов\n"
        f"Удачи! 🍀",
        reply_markup=main_menu_kb(is_staff=staff),
    )


# ─────────────────────────────────────────────
#  СТАВКА НА MVP
# ─────────────────────────────────────────────
@router.callback_query(F.data.startswith("bet_opt_mvp:"))
async def bet_opt_mvp(call: CallbackQuery):
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    if not m or m["status"] != "pending":
        await call.answer("Ставки закрыты!", show_alert=True)
        return
    if not m.get("has_mvp", 1):
        await call.answer("Для этого матча ставки на MVP не проводятся.", show_alert=True)
        return
    players = parse_players(m["mvp_players"])
    await call.message.edit_text(
        f"⭐️ <b>Ставка на MVP</b>\n\n{match_card_text(m)}\n\nВыбери игрока:",
        reply_markup=mvp_choice_kb(match_id, players),
    )
    await call.answer()


@router.callback_query(F.data.startswith("bet_mvp:"))
async def bet_mvp_choose(call: CallbackQuery, state: FSMContext):
    parts    = call.data.split(":", 2)
    match_id = int(parts[1])
    player   = parts[2]
    m = await get_match(match_id)
    if not m or m["status"] != "pending":
        await call.answer("Ставки закрыты!", show_alert=True)
        return
    if not m.get("has_mvp", 1):
        await call.answer("Для этого матча ставки на MVP не проводятся.", show_alert=True)
        return

    # Проверка: уже есть ставка на этот матч?
    existing = await get_user_bet_on_match(call.from_user.id, match_id)
    if existing:
        bet_type = "Победитель" if existing["bet_type"] == "winner" else "MVP"
        await call.answer(
            f"❌ Ты уже поставил на этот матч ({bet_type})!\nНа один матч можно сделать только одну ставку.",
            show_alert=True,
        )
        return

    await state.update_data(match_id=match_id, player=player, odds=MVP_MULTIPLIER)
    await state.set_state(BetMVP.amount)
    user = await get_user(call.from_user.id)
    await call.message.edit_text(
        f"⭐️ <b>Ставка на MVP: {player}</b>  (x{MVP_MULTIPLIER:g})\n\n"
        f"🪙 Твой баланс: <b>{fmt_coins(user['balance'])} коинов</b>\n\n"
        f"💬 Введи сумму ставки целым числом:"
    )


@router.message(StateFilter(BetMVP.amount))
async def bet_mvp_amount(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи /start")
        await state.clear()
        return

    amount, error = validate_bet_amount(message.text, user["balance"])
    if error:
        await message.answer(error)
        return

    data = await state.get_data()
    m = await get_match(data["match_id"])
    if not m or m["status"] != "pending":
        await message.answer("❌ Ставки на этот матч уже закрыты.")
        await state.clear()
        return

    # Повторная проверка: вдруг другая вкладка/устройство уже поставило пока вводили сумму
    existing = await get_user_bet_on_match(message.from_user.id, data["match_id"])
    if existing:
        await message.answer("❌ Ты уже поставил на этот матч. На один матч — только одна ставка.")
        await state.clear()
        return

    try:
        await create_bet(message.from_user.id, data["match_id"], "mvp", data["player"], amount)
    except ValueError:
        await message.answer("❌ Недостаточно коинов для ставки.")
        await state.clear()
        return
    await state.clear()

    payout  = round(amount * data["odds"])
    new_bal = user["balance"] - amount

    staff = is_admin(message.from_user.id) or await is_moder(message.from_user.id)
    await message.answer(
        f"✅ <b>Ставка принята!</b>\n\n"
        f"⚔️ Матч: <b>{m['team1']} vs {m['team2']}</b>\n"
        f"⭐️ Твой выбор MVP: <b>{data['player']}</b>  (x{data['odds']})\n"
        f"🪙 Ставка: <b>{fmt_coins(amount)}</b> коинов\n"
        f"💹 Возможный выигрыш: <b>{fmt_coins(payout)}</b> коинов\n\n"
        f"💰 Остаток баланса: <b>{fmt_coins(new_bal)}</b> коинов\n"
        f"Удачи! 🍀",
        reply_markup=main_menu_kb(is_staff=staff),
    )


# ─────────────────────────────────────────────
#  ПРОМОКОДЫ (ПОЛЬЗОВАТЕЛЬ)
# ─────────────────────────────────────────────
@router.message(F.text == "🎁 Активировать промокод")
async def promo_activate_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи /start")
        return
    await state.set_state(ActivatePromo.code)
    await message.answer(
        "🎁 <b>Активация промокода</b>\n\n"
        "Введи промокод текстом:"
    )


@router.message(StateFilter(ActivatePromo.code))
async def promo_activate_code(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала введи /start")
        await state.clear()
        return

    code = normalize_promo_code(message.text)
    if not code:
        await message.answer("❌ Промокод не может быть пустым. Введи промокод:")
        return

    status, amount = await activate_promo_code_db(message.from_user.id, code)
    await state.clear()

    if status == "not_found":
        await message.answer(
            "❌ Такого промокода не существует. Проверь правильность ввода.",
            reply_markup=main_menu_kb(is_staff=await is_moder(message.from_user.id)),
        )
        return
    if status == "already_used":
        await message.answer(
            "⚠️ Вы уже активировали этот промокод.",
            reply_markup=main_menu_kb(is_staff=await is_moder(message.from_user.id)),
        )
        return
    if status == "no_activations":
        await message.answer(
            "❌ Этот промокод больше недействителен (закончились активации).",
            reply_markup=main_menu_kb(is_staff=await is_moder(message.from_user.id)),
        )
        return

    # status == "ok"
    await message.answer(
        f"✅ <b>Успешно!</b> Ваш баланс пополнен на "
        f"<b>{fmt_coins(int(amount)) if amount == int(amount) else amount} коинов</b>.",
        reply_markup=main_menu_kb(is_staff=await is_moder(message.from_user.id)),
    )


# ─────────────────────────────────────────────
#  ПРОВЕРКА ПРАВ
# ─────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def is_moder(user_id: int) -> bool:
    """Модератор или выше."""
    return user_id == ADMIN_ID or await is_moderator_db(user_id)



# ─────────────────────────────────────────────
#  АДМИН-ПАНЕЛЬ
# ─────────────────────────────────────────────
@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ Нет доступа.")
        return
    await message.answer(
        "🔐 <b>Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_menu_kb(),
    )


@router.message(F.text == "🔐 Панель")
async def staff_panel_button(message: Message, state: FSMContext):
    """Кнопка «🔐 Панель» — открывает соответствующую панель для admin/moder."""
    uid = message.from_user.id
    await state.clear()
    if is_admin(uid):
        await message.answer(
            "🔐 <b>Админ-панель</b>\nВыбери действие:",
            reply_markup=admin_menu_kb(),
        )
    elif await is_moder(uid):
        await message.answer(
            "🛡 <b>Панель модератора</b>\nВыбери действие:",
            reply_markup=moder_menu_kb(),
        )
    else:
        await message.answer("⛔️ Нет доступа.")


# ── Создание турнира ────────────────────────
@router.callback_query(F.data == "admin:create_tournament")
async def admin_create_tournament(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(CreateTournament.name)
    await call.message.edit_text(
        "🏆 <b>Создание турнира</b>\n\n"
        "📝 Введи <b>название турнира</b>:\n"
        "<i>Например: Standoff 2 Pro League — Сезон 4</i>"
    )
    await call.answer()


@router.message(StateFilter(CreateTournament.name))
async def ct_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Введи название турнира:")
        return
    tournament_id = await create_tournament_db(name)
    await state.clear()
    await message.answer(
        f"✅ <b>Турнир создан!</b>\n\n"
        f"🏆 <b>{name}</b>\n"
        f"🆔 ID: <code>{tournament_id}</code>\n\n"
        f"Теперь ты можешь создавать матчи и прикреплять их к этому турниру.",
        reply_markup=admin_menu_kb(),
    )


# ── Удаление турнира ─────────────────────────
@router.callback_query(F.data == "admin:manage_tournaments")
async def admin_manage_tournaments(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    tournaments = await get_active_tournaments()
    if not tournaments:
        await call.message.edit_text(
            "🗑 <b>Удаление турнира</b>\n\nАктивных турниров пока нет.",
            reply_markup=admin_menu_kb(),
        )
    else:
        await call.message.edit_text(
            "🗑 <b>Удаление турнира</b>\n\n"
            "Выбери турнир, который нужно удалить:",
            reply_markup=tournaments_manage_kb(tournaments),
        )
    await call.answer()


@router.callback_query(F.data == "tourn_del:back")
async def tourn_del_back(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "🔐 <b>Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("tourn_del:"))
async def tourn_del_ask(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    tournament_id = int(call.data.split(":")[1])
    tourn = await get_tournament(tournament_id)
    if not tourn:
        await call.answer("Турнир не найден (уже удалён?).", show_alert=True)
        tournaments = await get_active_tournaments()
        await call.message.edit_text(
            "🗑 <b>Удаление турнира</b>\n\nВыбери турнир, который нужно удалить:",
            reply_markup=tournaments_manage_kb(tournaments),
        )
        return
    matches_count = await count_matches_by_tournament(tournament_id)
    extra = (
        f"\n\n⚠️ К турниру привязано <b>{matches_count}</b> матч(а/ей) — "
        f"они не удалятся, а станут матчами «без турнира»."
        if matches_count else ""
    )
    await call.message.edit_text(
        f"❗️ Удалить турнир <b>«{tourn['name']}»</b>?{extra}\n\n"
        f"Это действие нельзя отменить.",
        reply_markup=tournament_delete_confirm_kb(tournament_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("tourn_del_yes:"))
async def tourn_del_confirm(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    tournament_id = int(call.data.split(":")[1])
    tourn = await get_tournament(tournament_id)
    name = tourn["name"] if tourn else f"ID {tournament_id}"
    await delete_tournament_db(tournament_id)

    tournaments = await get_active_tournaments()
    if tournaments:
        await call.message.edit_text(
            f"✅ Турнир <b>«{name}»</b> удалён.\n\n"
            f"Выбери ещё один турнир для удаления или вернись назад:",
            reply_markup=tournaments_manage_kb(tournaments),
        )
    else:
        await call.message.edit_text(
            f"✅ Турнир <b>«{name}»</b> удалён.\n\nАктивных турниров больше нет.",
            reply_markup=admin_menu_kb(),
        )
    await call.answer("Турнир удалён")


# ── Создание матча ──────────────────────────
def tournament_pick_kb(tournaments: list) -> InlineKeyboardMarkup:
    """Инлайн-кнопки выбора турнира при создании матча (admin FSM)."""
    rows = [
        [InlineKeyboardButton(
            text=f"🏆 {t['name']}",
            callback_data=f"pick_tourn:{t['id']}",
        )]
        for t in tournaments
    ]
    rows.append([InlineKeyboardButton(
        text="🚫 Без турнира",
        callback_data="pick_tourn:none",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin:create_match")
async def admin_create_match(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id):
        return
    tournaments = await get_active_tournaments()
    if tournaments:
        # Предлагаем выбрать турнир (или «без турнира»)
        await state.set_state(CreateMatch.tournament_id)
        await call.message.edit_text(
            "➕ <b>Создание матча</b>\n\n"
            "📝 Шаг 1: Выбери <b>турнир</b> для этого матча:",
            reply_markup=tournament_pick_kb(tournaments),
        )
    else:
        # Турниров нет — сразу к вводу команд
        await state.update_data(tournament_id=None)
        await state.set_state(CreateMatch.team1)
        await call.message.edit_text(
            "➕ <b>Создание матча</b>\n\n"
            "📝 Шаг 1: Введи название <b>Team 1</b>:"
        )
    await call.answer()


@router.callback_query(F.data.startswith("pick_tourn:"), StateFilter(CreateMatch.tournament_id))
async def cm_tournament(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    raw = call.data.split(":")[1]
    tid = None if raw == "none" else int(raw)
    await state.update_data(tournament_id=tid)
    await state.set_state(CreateMatch.team1)
    await call.message.edit_text(
        "➕ <b>Создание матча</b>\n\n"
        "📝 Введи название <b>Team 1</b>:"
    )
    await call.answer()


@router.message(StateFilter(CreateMatch.team1))
async def cm_team1(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id): return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Введи название Team 1:")
        return
    await state.update_data(team1=name)
    await state.set_state(CreateMatch.team2)
    await message.answer("📝 Шаг 2: Введи название <b>Team 2</b>:")


@router.message(StateFilter(CreateMatch.team2))
async def cm_team2(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id): return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Введи название Team 2:")
        return
    await state.update_data(team2=name)
    await state.set_state(CreateMatch.fmt)
    await message.answer("📝 Шаг 3: Выбери формат матча:", reply_markup=match_format_kb())


@router.callback_query(F.data.startswith("fmt:"))
async def cm_format(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    fmt  = call.data.split(":")[1]
    data = await state.get_data()
    await state.update_data(fmt=fmt)
    await state.set_state(CreateMatch.odds1)
    await call.message.edit_text(
        f"📝 Шаг 4: Введи коэффициент на <b>{data['team1']}</b> (например: 1.85):"
    )
    await call.answer()


@router.message(StateFilter(CreateMatch.odds1))
async def cm_odds1(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id): return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        odds1 = float(raw)
        assert odds1 > 1.0
    except Exception:
        await message.answer("❌ Введи число больше 1.0 (например: 1.85):")
        return
    await state.update_data(odds1=odds1)
    await state.set_state(CreateMatch.odds2)
    data = await state.get_data()
    await message.answer(f"📝 Шаг 5: Введи коэффициент на <b>{data['team2']}</b>:")


@router.message(StateFilter(CreateMatch.odds2))
async def cm_odds2(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id): return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        odds2 = float(raw)
        assert odds2 > 1.0
    except Exception:
        await message.answer("❌ Введи число больше 1.0:")
        return
    await state.update_data(odds2=odds2)
    await state.set_state(CreateMatch.ask_draw)
    await message.answer(
        "📝 Шаг 6: В этом матче возможна <b>ничья</b> (например, по картам)?\n"
        "Включить ставки на ничью?",
        reply_markup=draw_toggle_kb(),
    )


@router.callback_query(F.data.startswith("draw_toggle:"), StateFilter(CreateMatch.ask_draw))
async def cm_draw_toggle(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    choice = call.data.split(":")[1]

    if choice == "no":
        await state.update_data(odds_draw=None)
        await state.set_state(CreateMatch.ask_mvp)
        await call.message.edit_text(
            "📝 Шаг 7: Включить ставки на MVP для этого матча?",
            reply_markup=mvp_toggle_kb(),
        )
        await call.answer()
        return

    # choice == "yes"
    await state.set_state(CreateMatch.odds_draw)
    await call.message.edit_text(
        "📝 Шаг 6.1: Введи коэффициент на <b>ничью</b> (например: 5.50):"
    )
    await call.answer()


@router.message(StateFilter(CreateMatch.odds_draw))
async def cm_odds_draw(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id): return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        odds_draw = float(raw)
        assert odds_draw > 1.0
    except Exception:
        await message.answer("❌ Введи число больше 1.0 (например: 5.50):")
        return
    await state.update_data(odds_draw=odds_draw)
    await state.set_state(CreateMatch.ask_mvp)
    await message.answer(
        "📝 Шаг 7: Включить ставки на MVP для этого матча?",
        reply_markup=mvp_toggle_kb(),
    )


@router.callback_query(F.data.startswith("mvp_toggle:"), StateFilter(CreateMatch.ask_mvp))
async def cm_mvp_toggle(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    choice = call.data.split(":")[1]

    if choice == "no":
        # Без MVP — сразу создаём матч
        data = await state.get_data()
        match_id = await create_match_db(
            data["team1"], data["team2"], data["fmt"],
            data["odds1"], data["odds2"],
            has_mvp=False, players_str=None, mvp_odds=None,
            tournament_id=data.get("tournament_id"),
            odds_draw=data.get("odds_draw"),
        )
        tourn_line = ""
        if data.get("tournament_id"):
            t = await get_tournament(data["tournament_id"])
            tourn_line = f"🏆 Турнир: <b>{t['name']}</b>\n" if t else ""
        draw_line = f"🤝 Кэф на ничью: x{data['odds_draw']}\n" if data.get("odds_draw") else ""
        await state.clear()
        cm_back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()
        await call.message.edit_text(
            f"✅ <b>Матч #{match_id} создан!</b>\n\n"
            f"{tourn_line}"
            f"⚔️ <b>{data['team1']}</b> vs <b>{data['team2']}</b>\n"
            f"📋 Формат: {data['fmt']}\n"
            f"💹 Кэфы: x{data['odds1']} / x{data['odds2']}\n"
            f"{draw_line}"
            f"⭐️ MVP: <i>отключён для этого матча</i>\n"
            f"📌 Статус: <b>Ожидание бан-пиков</b>",
            reply_markup=cm_back_kb,
        )
        return

    # choice == "yes" — состав вводится отдельно по каждой команде
    data = await state.get_data()
    await state.set_state(CreateMatch.mvp_players_t1)
    await call.message.edit_text(
        f"📝 Шаг 8: Введи состав <b>{data['team1']}</b> через запятую:\n\n"
        "<i>Пример: Snej, Horizon, Bullet, Morse, Gentle</i>"
    )
    await call.answer()


@router.message(StateFilter(CreateMatch.mvp_players_t1))
async def cm_mvp_players_t1(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id): return
    players = parse_players(message.text or "")
    if len(players) < 1:
        await message.answer("❌ Введи хотя бы 1 игрока через запятую:")
        return
    data = await state.get_data()
    await state.update_data(mvp_players_t1=", ".join(players))
    await state.set_state(CreateMatch.mvp_players_t2)
    await message.answer(
        f"👾 Состав {data['team1']} сохранён ({len(players)} чел.)\n\n"
        f"📝 Шаг 9: Введи состав <b>{data['team2']}</b> через запятую:\n\n"
        "<i>Пример: Reborn, Lukian, Kronos, Sky, Dexter</i>"
    )


@router.message(StateFilter(CreateMatch.mvp_players_t2))
async def cm_mvp_players_t2(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id): return
    players = parse_players(message.text or "")
    if len(players) < 1:
        await message.answer("❌ Введи хотя бы 1 игрока через запятую:")
        return
    data = await state.get_data()
    players_str = f"{data['mvp_players_t1']}{ROSTER_SEP}{', '.join(players)}"

    # Коэффициент на MVP больше не спрашиваем — выплата всегда фиксированная x2.
    match_id = await create_match_db(
        data["team1"], data["team2"], data["fmt"],
        data["odds1"], data["odds2"],
        has_mvp=True, players_str=players_str, mvp_odds=MVP_MULTIPLIER,
        tournament_id=data.get("tournament_id"),
        odds_draw=data.get("odds_draw"),
    )
    tourn_line = ""
    if data.get("tournament_id"):
        t = await get_tournament(data["tournament_id"])
        tourn_line = f"🏆 Турнир: <b>{t['name']}</b>\n" if t else ""
    draw_line = f"🤝 Кэф на ничью: x{data['odds_draw']}\n" if data.get("odds_draw") else ""
    await state.clear()

    p1, p2 = parse_players_by_team(players_str)
    total = len(p1) + len(p2)
    cm_mvp_back_kb = admin_menu_kb() if is_admin(message.from_user.id) else moder_menu_kb()
    await message.answer(
        f"✅ <b>Матч #{match_id} создан!</b>\n\n"
        f"{tourn_line}"
        f"⚔️ <b>{data['team1']}</b> vs <b>{data['team2']}</b>\n"
        f"📋 Формат: {data['fmt']}\n"
        f"💹 Кэфы: x{data['odds1']} / x{data['odds2']}\n"
        f"{draw_line}"
        f"⭐️ MVP: x{MVP_MULTIPLIER:g}  ({total} игроков)\n"
        f"👾 Составы:\n"
        f"   🛡 {data['team1']}: {', '.join(p1)}\n"
        f"   ⚔️ {data['team2']}: {', '.join(p2)}\n"
        f"📌 Статус: <b>Ожидание бан-пиков</b>",
        reply_markup=cm_mvp_back_kb,
    )


# ── Быстрое создание матча одним сообщением ─
QUICK_MATCH_TEMPLATE = (
    "⚡ <b>Быстрое создание матча</b>\n\n"
    "Скопируй шаблон ниже, поменяй значения и пришли <b>одним сообщением</b>:\n\n"
    "<code>Navi - Virtus.pro\n"
    "Формат: BO3\n"
    "Кэф1: 1.85\n"
    "Кэф2: 1.95\n"
    "Ничья: 4.20\n"
    "Состав1: Snej, Electronic, s1mple, b1t, Perfecto\n"
    "Состав2: ANGE1, FL1T, Jady, fame, Fn\n"
    "Турнир: PGL Major</code>\n\n"
    "ℹ️ <b>Необязательные строки можно удалить целиком:</b>\n"
    "• «Формат» — если не указать, по умолчанию BO3\n"
    "• «Ничья» — если ставок на ничью не будет\n"
    "• «Состав1» + «Состав2» — указываются <b>только вместе</b>; убери обе строки, "
    "если ставок на MVP не будет\n"
    "• «Турнир» — если матч без турнира (ищется точное совпадение названия среди активных турниров)\n\n"
    "Порядок строк после первой не важен. Составы пишутся отдельно по каждой команде и "
    "будут показаны под их названиями. Выплата за угаданный MVP всегда фиксированная — <b>x2</b>."
)


@router.callback_query(F.data == "admin:quick_match")
async def admin_quick_match_start(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id):
        return
    await state.set_state(QuickCreateMatch.raw_text)
    await call.message.edit_text(QUICK_MATCH_TEMPLATE)
    await call.answer()


@router.message(StateFilter(QuickCreateMatch.raw_text))
async def quick_match_process(message: Message, state: FSMContext):
    if not await is_moder(message.from_user.id):
        return

    data, error = parse_quick_match_text(message.text or "")
    if error:
        await message.answer(f"{error}\n\nИсправь и пришли снова одним сообщением (или /admin, чтобы отменить):")
        return

    tournament_id = None
    tourn_warning = ""
    if data["tournament_name"]:
        tournaments = await get_active_tournaments()
        match = next(
            (t for t in tournaments if t["name"].strip().lower() == data["tournament_name"].strip().lower()),
            None,
        )
        if match:
            tournament_id = match["id"]
        else:
            tourn_warning = (
                f"\n⚠️ Турнир «{data['tournament_name']}» не найден среди активных — "
                f"матч создан без привязки к турниру."
            )

    match_id = await create_match_db(
        data["team1"], data["team2"], data["fmt"],
        data["odds1"], data["odds2"],
        has_mvp=data["has_mvp"], players_str=data["players_str"], mvp_odds=data["mvp_odds"],
        tournament_id=tournament_id, odds_draw=data["odds_draw"],
    )
    await state.clear()

    tourn_line = ""
    if tournament_id:
        t = await get_tournament(tournament_id)
        tourn_line = f"🏆 Турнир: <b>{t['name']}</b>\n" if t else ""
    draw_line = f"🤝 Кэф на ничью: x{data['odds_draw']}\n" if data["odds_draw"] else ""
    if data["has_mvp"]:
        p1, p2 = parse_players_by_team(data["players_str"])
        total = len(p1) + len(p2)
        if p2:
            roster_lines = (
                f"👾 Составы:\n"
                f"   🛡 {data['team1']}: {', '.join(p1)}\n"
                f"   ⚔️ {data['team2']}: {', '.join(p2)}\n"
            )
        else:
            roster_lines = f"👾 Составы: {', '.join(p1)}\n"
        mvp_line = f"⭐️ MVP: x{MVP_MULTIPLIER:g}  ({total} игроков)\n{roster_lines}"
    else:
        mvp_line = "⭐️ MVP: <i>отключён для этого матча</i>\n"

    back_kb = admin_menu_kb() if is_admin(message.from_user.id) else moder_menu_kb()
    await message.answer(
        f"✅ <b>Матч #{match_id} создан!</b>\n\n"
        f"{tourn_line}"
        f"⚔️ <b>{data['team1']}</b> vs <b>{data['team2']}</b>\n"
        f"📋 Формат: {data['fmt']}\n"
        f"💹 Кэфы: x{data['odds1']} / x{data['odds2']}\n"
        f"{draw_line}"
        f"{mvp_line}"
        f"📌 Статус: <b>Ожидание бан-пиков</b>"
        f"{tourn_warning}",
        reply_markup=back_kb,
    )


# ── Указать карту(ы) и закрыть ставки ───────
@router.callback_query(F.data == "admin:set_map")
async def admin_set_map(call: CallbackQuery):
    if not await is_moder(call.from_user.id): return
    matches = await get_pending_matches()
    map_back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()
    if not matches:
        await call.message.edit_text(
            "😔 Нет матчей в статусе «Ожидание бан-пиков».",
            reply_markup=map_back_kb,
        )
        return
    await call.message.edit_text(
        "🗺 <b>Выбери матч для указания карты:</b>",
        reply_markup=matches_kb(matches, "admin_setmap_match"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_setmap_match:"))
async def admin_setmap_match(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    if not m or m["status"] != "pending":
        await call.answer("Матч недоступен для указания карты.", show_alert=True)
        return

    need = FORMAT_MAP_COUNT.get(m["format"], 1)
    await state.set_state(SetMapFlow.picking)
    await state.update_data(
        setmap_match_id=match_id,
        setmap_need=need,
        setmap_chosen=[],
        setmap_remaining=list(MAPS),
    )

    step_label = f"1/{need}" if need > 1 else "1/1"
    await call.message.edit_text(
        f"🗺 <b>{m['team1']} vs {m['team2']}</b>  [{m['format']}]\n"
        f"Выбери карту {step_label}:",
        reply_markup=maps_pick_kb(MAPS),
    )


@router.callback_query(F.data.startswith("pickmap:"), StateFilter(SetMapFlow.picking))
async def pickmap_select(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    chosen_map = call.data.split(":", 1)[1]

    data       = await state.get_data()
    match_id   = data["setmap_match_id"]
    need       = data["setmap_need"]
    chosen     = data["setmap_chosen"]
    remaining  = data["setmap_remaining"]

    if chosen_map not in remaining:
        await call.answer("Эта карта уже выбрана, выбери другую.", show_alert=True)
        return

    chosen = chosen + [chosen_map]
    remaining = [m for m in remaining if m != chosen_map]
    await state.update_data(setmap_chosen=chosen, setmap_remaining=remaining)

    m = await get_match(match_id)

    if len(chosen) >= need:
        # Все карты выбраны — сохраняем и закрываем ставки
        await set_match_maps(match_id, chosen)
        await state.clear()
        pickmap_back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()
        await call.message.edit_text(
            f"✅ <b>Карты установлены!</b>\n\n"
            f"⚔️ {m['team1']} vs {m['team2']}\n"
            f"🗺 Карты матча: <b>{maps_display_str(chosen)}</b>\n"
            f"🔒 Приём ставок закрыт — матч идёт!",
            reply_markup=pickmap_back_kb,
        )
        return

    step_label = f"{len(chosen) + 1}/{need}"
    chosen_str = maps_display_str(chosen)
    await call.message.edit_text(
        f"🗺 <b>{m['team1']} vs {m['team2']}</b>  [{m['format']}]\n"
        f"✅ Уже выбрано: {chosen_str}\n\n"
        f"Выбери карту {step_label}:",
        reply_markup=maps_pick_kb(remaining),
    )


# ── Изменить названия команд (LIVE-редактирование) ──
@router.callback_query(F.data == "admin:edit_teams")
async def admin_edit_teams(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    matches = await get_active_matches()  # pending + live
    if not matches:
        await call.message.edit_text("😔 Нет активных матчей.", reply_markup=admin_menu_kb())
        return
    await state.set_state(EditTeams.choose_match)
    await call.message.edit_text(
        "✏️ <b>Изменение названий команд</b>\nВыбери матч:",
        reply_markup=matches_kb(matches, "admin_editteams_match"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_editteams_match:"), StateFilter(EditTeams.choose_match))
async def admin_editteams_match(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return
    await state.update_data(edit_match_id=match_id)
    await state.set_state(EditTeams.new_team1)
    await call.message.edit_text(
        f"✏️ Текущее название Team 1: <b>{m['team1']}</b>\n\n"
        f"Введи новое название для <b>Team 1</b>:"
    )


@router.message(StateFilter(EditTeams.new_team1))
async def editteams_team1(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Введи новое название Team 1:")
        return
    data = await state.get_data()
    m = await get_match(data["edit_match_id"])
    await state.update_data(new_team1=name)
    await state.set_state(EditTeams.new_team2)
    await message.answer(
        f"✏️ Текущее название Team 2: <b>{m['team2']}</b>\n\n"
        f"Введи новое название для <b>Team 2</b>:"
    )


@router.message(StateFilter(EditTeams.new_team2))
async def editteams_team2(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Введи новое название Team 2:")
        return

    data     = await state.get_data()
    match_id = data["edit_match_id"]
    new_t1   = data["new_team1"]
    new_t2   = name

    await update_match_teams(match_id, new_t1, new_t2)
    await state.clear()

    m = await get_match(match_id)
    await message.answer(
        f"✅ <b>Названия команд обновлены!</b>\n\n"
        f"⚔️ Матч #{match_id}: <b>{m['team1']}</b> vs <b>{m['team2']}</b>\n"
        f"📌 Все ранее сделанные ставки пользователей сохранены без изменений.",
        reply_markup=admin_menu_kb(),
    )


# ── Завершить матч ──────────────────────────
@router.callback_query(F.data == "admin:finish_match")
async def admin_finish(call: CallbackQuery):
    if not await is_moder(call.from_user.id): return
    matches = await get_active_matches()
    back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()
    if not matches:
        await call.message.edit_text("😔 Нет активных матчей.", reply_markup=back_kb)
        return
    await call.message.edit_text(
        "🏆 <b>Выбери матч для расчёта:</b>",
        reply_markup=matches_kb(matches, "admin_finish_match"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("admin_finish_match:"))
async def admin_finish_match_select(call: CallbackQuery):
    if not await is_moder(call.from_user.id): return
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return
    if m["status"] == "finished":
        await call.answer("Этот матч уже завершён!", show_alert=True)
        return
    await call.message.edit_text(
        f"🏆 <b>Расчёт матча</b>\n"
        f"⚔️ {m['team1']} vs {m['team2']}\n\n"
        f"Шаг 1: Выбери <b>победителя</b>:",
        reply_markup=admin_winner_kb(match_id, m["team1"], m["team2"], has_draw_odds(m)),
    )


@router.callback_query(F.data.startswith("admin_winner:"))
async def admin_winner_select(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    parts    = call.data.split(":")
    match_id = int(parts[1])
    winner   = parts[2]
    await state.update_data(finish_match_id=match_id, finish_winner=winner)
    m = await get_match(match_id)
    winner_name = winner_target_label(winner, m["team1"], m["team2"])

    if not m.get("has_mvp", 1):
        # MVP не предусмотрен — сразу к завершению расчёта
        await call.message.edit_text(
            f"✅ Результат: <b>{winner_name}</b>\n\n"
            f"Для этого матча ставки на MVP не проводились.",
            reply_markup=skip_mvp_kb(match_id),
        )
        await call.answer()
        return

    players = parse_players(m["mvp_players"])
    await call.message.edit_text(
        f"✅ Результат: <b>{winner_name}</b>\n\n"
        f"Шаг 2: Выбери <b>MVP</b> матча ({len(players)} игроков):",
        reply_markup=admin_mvp_kb(match_id, players),
    )
    await call.answer()


async def _do_finish_and_settle(call: CallbackQuery, state: FSMContext, match_id: int, winner: str, mvp: Optional[str]):
    """Общая логика завершения матча + расчёт ставок + уведомления."""
    await finish_match_db(match_id, winner, mvp)
    results = await settle_bets(match_id)
    await state.clear()

    m = await get_match(match_id)
    winner_name = winner_target_label(winner, m["team1"], m["team2"])

    notified = 0
    for r in results:
        if r["won"] and r["payout"] > 0:
            try:
                await bot.send_message(
                    r["user_id"],
                    f"🎉 <b>Твоя ставка сыграла!</b>\n\n"
                    f"⚔️ Матч: <b>{m['team1']} vs {m['team2']}</b>\n"
                    f"🪙 Ты выиграл <b>{fmt_coins(r['payout'])} коинов!</b>\n\n"
                    f"Проверь баланс в 👤 Профиле!",
                )
                notified += 1
            except Exception:
                pass

    total_bets = len(results)
    won_bets   = sum(1 for r in results if r["won"])
    mvp_line   = f"⭐️ MVP: <b>{mvp}</b>\n" if mvp else ""

    # Возвращаем меню в зависимости от роли вызвавшего
    caller_id = call.from_user.id
    back_kb = admin_menu_kb() if is_admin(caller_id) else moder_menu_kb()

    await call.message.edit_text(
        f"✅ <b>Матч завершён и рассчитан!</b>\n\n"
        f"⚔️ {m['team1']} vs {m['team2']}\n"
        f"🏆 Результат: <b>{winner_name}</b>\n"
        f"{mvp_line}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Ставок рассчитано: <b>{total_bets}</b>\n"
        f"🎯 Выигрышных: <b>{won_bets}</b>\n"
        f"📩 Уведомлений отправлено: <b>{notified}</b>",
        reply_markup=back_kb,
    )


@router.callback_query(F.data.startswith("admin_mvp:"))
async def admin_mvp_select(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id): return
    parts    = call.data.split(":", 2)
    match_id = int(parts[1])
    mvp      = parts[2]

    data   = await state.get_data()
    winner = data.get("finish_winner")
    if not winner:
        await call.answer("Ошибка: сначала выбери победителя.", show_alert=True)
        return

    await _do_finish_and_settle(call, state, match_id, winner, mvp)


@router.callback_query(F.data.startswith("admin_mvp_skip:"))
async def admin_mvp_skip(call: CallbackQuery, state: FSMContext):
    """Завершение матча без MVP (для матчей, где ставки на MVP были отключены)."""
    if not await is_moder(call.from_user.id): return
    match_id = int(call.data.split(":")[1])

    data   = await state.get_data()
    winner = data.get("finish_winner")
    if not winner:
        await call.answer("Ошибка: сначала выбери победителя.", show_alert=True)
        return

    await _do_finish_and_settle(call, state, match_id, winner, None)


# ─────────────────────────────────────────────
#  ОТКАТ ТОКЕНОВ: ОТМЕНА МАТЧА С ВОЗВРАТОМ СТАВОК
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin:cancel_match")
async def admin_cancel_match_start(call: CallbackQuery, state: FSMContext):
    """Шаг 1: показать список активных матчей для отмены."""
    if not await is_moder(call.from_user.id):
        return
    await state.clear()
    matches = await get_active_matches()
    back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()
    if not matches:
        await call.message.edit_text("😔 Нет активных матчей для отмены.", reply_markup=back_kb)
        return
    await state.set_state(CancelMatch.choose_match)
    await call.message.edit_text(
        "↩️ <b>Отмена матча (откат токенов)</b>\n\n"
        "Все игроки, сделавшие ставки на выбранный матч, получат свои "
        "коины обратно.\n\n"
        "Выбери матч:",
        reply_markup=matches_kb(matches, "cancel_match_pick"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("cancel_match_pick:"), StateFilter(CancelMatch.choose_match))
async def admin_cancel_match_pick(call: CallbackQuery, state: FSMContext):
    """Шаг 2: подтверждение отмены конкретного матча."""
    if not await is_moder(call.from_user.id):
        return
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return
    if m["status"] not in ("pending", "live"):
        await call.answer("Этот матч уже завершён или отменён.", show_alert=True)
        return

    bets = await get_unsettled_bets(match_id)
    total_amount = sum(b["amount"] for b in bets)
    await state.update_data(cancel_match_id=match_id)

    await call.message.edit_text(
        f"⚠️ <b>Подтверди отмену матча</b>\n\n"
        f"⚔️ {m['team1']} vs {m['team2']} [{m['format']}]\n\n"
        f"📊 Ставок на матч: <b>{len(bets)}</b>\n"
        f"🪙 Сумма к возврату: <b>{fmt_coins(total_amount)}</b> коинов\n\n"
        f"Все ставки будут аннулированы, коины вернутся игрокам. "
        f"Действие необратимо. Продолжить?",
        reply_markup=cancel_match_confirm_kb(match_id),
    )
    await call.answer()


@router.callback_query(F.data == "cancel_match_no")
async def admin_cancel_match_no(call: CallbackQuery, state: FSMContext):
    if not await is_moder(call.from_user.id):
        return
    await state.clear()
    back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()
    await call.message.edit_text(
        "❌ Отмена матча не выполнена.",
        reply_markup=back_kb,
    )
    await call.answer()


@router.callback_query(F.data.startswith("cancel_match_yes:"))
async def admin_cancel_match_confirm(call: CallbackQuery, state: FSMContext):
    """Шаг 3: выполнить отмену — вернуть ставки и уведомить игроков."""
    if not await is_moder(call.from_user.id):
        return
    match_id = int(call.data.split(":")[1])
    m = await get_match(match_id)
    await state.clear()
    if not m:
        await call.answer("Матч не найден.", show_alert=True)
        return

    refunds = await cancel_match_db(match_id)
    back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()

    if refunds is None:
        # Матч уже был отменён/завершён (например, второй админ успел раньше)
        await call.message.edit_text(
            "⚠️ Этот матч уже был завершён или отменён — повторная отмена не выполнена.",
            reply_markup=back_kb,
        )
        await call.answer()
        return

    notified = 0
    for r in refunds:
        try:
            await bot.send_message(
                r["user_id"],
                f"↩️ <b>Матч отменён администрацией.</b>\n\n"
                f"⚔️ {m['team1']} vs {m['team2']}\n"
                f"🪙 Твоя ставка <b>{fmt_coins(r['amount'])} коинов</b> возвращена на баланс.",
            )
            notified += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)

    await call.message.edit_text(
        f"✅ <b>Матч отменён, ставки возвращены!</b>\n\n"
        f"⚔️ {m['team1']} vs {m['team2']}\n"
        f"📊 Возвращено ставок: <b>{len(refunds)}</b>\n"
        f"📩 Уведомлений отправлено: <b>{notified}</b>",
        reply_markup=back_kb,
    )
    await call.answer()


# ─────────────────────────────────────────────
#  СПИСОК ИГРОКОВ С АКТИВНЫМИ СТАВКАМИ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin:active_bettors")
async def admin_active_bettors(call: CallbackQuery):
    """Показать всех игроков, у кого есть несыгранные ставки — с суммой и тем, на что поставлено."""
    if not await is_moder(call.from_user.id):
        return
    rows = await get_active_bettors()
    back_kb = admin_menu_kb() if is_admin(call.from_user.id) else moder_menu_kb()

    if not rows:
        await call.message.edit_text(
            "🎲 Сейчас ни у кого нет активных ставок.",
            reply_markup=back_kb,
        )
        await call.answer()
        return

    # Группируем по пользователю, сохраняя порядок появления
    by_user: dict[int, dict] = {}
    for r in rows:
        uid = r["user_id"]
        if uid not in by_user:
            by_user[uid] = {
                "username": r["username"],
                "first_name": r["first_name"],
                "bets": [],
                "total": 0,
            }
        if r["bet_type"] == "mvp":
            target_label = f"MVP: {r['target']}"
        else:
            target_label = winner_target_label(r["target"], r["team1"], r["team2"])
        by_user[uid]["bets"].append({
            "match": f"{r['team1']} vs {r['team2']}",
            "target": target_label,
            "amount": r["amount"],
        })
        by_user[uid]["total"] += r["amount"]

    users_list = list(by_user.items())
    total_users = len(users_list)
    grand_total = sum(u["total"] for _, u in users_list)

    await call.message.edit_text(
        f"🎲 <b>Игроки с активными ставками</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Всего игроков со ставками: <b>{total_users}</b>\n"
        f"🪙 Сумма всех активных ставок: <b>{fmt_coins(grand_total)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Список будет отправлен следующими сообщениями...",
        reply_markup=back_kb,
    )

    # Разбиваем на сообщения, чтобы не упереться в лимит длины Telegram
    chunk_lines: list[str] = []
    chunk_len = 0
    MAX_LEN = 3500

    async def flush():
        nonlocal chunk_lines, chunk_len
        if chunk_lines:
            await call.message.answer("\n\n".join(chunk_lines))
            await asyncio.sleep(0.1)
            chunk_lines, chunk_len = [], 0

    for idx, (uid, info) in enumerate(users_list, start=1):
        name = f"@{info['username']}" if info.get("username") else (info.get("first_name") or f"ID {uid}")
        bet_lines = "\n".join(
            f"   • {b['match']} → <b>{b['target']}</b>: 🪙 {fmt_coins(b['amount'])}"
            for b in info["bets"]
        )
        block = (
            f"{idx}. {name} | ID: <code>{uid}</code>\n"
            f"   Всего в ставках: 🪙 {fmt_coins(info['total'])}\n"
            f"{bet_lines}"
        )
        if chunk_len + len(block) > MAX_LEN:
            await flush()
        if len(block) > MAX_LEN:
            # У одного игрока очень много ставок — блок сам больше лимита.
            # Отправляем его отдельным сообщением, не накапливая с другими.
            await call.message.answer(block)
            await asyncio.sleep(0.1)
            continue
        chunk_lines.append(block)
        chunk_len += len(block)

    await flush()


# ── Бонус всем ──────────────────────────────
@router.callback_query(F.data == "admin:bonus")
async def admin_bonus_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await state.set_state(AdminBonus.amount)
    await call.message.edit_text(
        "💰 <b>Выдача бонуса всем пользователям</b>\n\n"
        "Введи сумму коинов для начисления (целое число больше 0):"
    )
    await call.answer()


@router.message(StateFilter(AdminBonus.amount))
async def admin_bonus_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    raw = message.text.strip() if message.text else ""
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("❌ Введи целое положительное число больше 0:")
        return
    amount = int(raw)
    await state.clear()

    users = await get_all_users()
    # Пакетное начисление в одном соединении
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ?", (amount,))
        await db.commit()

    sent = 0
    for u in users:
        try:
            await bot.send_message(
                u["user_id"],
                f"🎁 <b>Бонус от администрации!</b>\n\n"
                f"Тебе начислено <b>🪙 {fmt_coins(amount)} коинов</b>!\n"
                f"Удачи в ставках! 🍀",
            )
            sent += 1
        except Exception:
            pass

    await message.answer(
        f"✅ <b>Бонус выдан!</b>\n\n"
        f"🪙 Сумма: <b>{fmt_coins(amount)}</b> коинов\n"
        f"👥 Пользователей: <b>{len(users)}</b>\n"
        f"📩 Уведомлений: <b>{sent}</b>",
        reply_markup=admin_menu_kb(),
    )


# ─────────────────────────────────────────────
#  ВЫДАЧА ТОКЕНОВ ОДНОМУ ПОЛЬЗОВАТЕЛЮ
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin:give_tokens")
async def admin_give_tokens_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    users = await get_all_users()
    if not users:
        await call.answer("Нет зарегистрированных игроков.", show_alert=True)
        return
    await state.set_state(AdminGiveTokens.target)
    await call.message.edit_text(
        "🎯 <b>Выдача токенов игроку</b>\n\n"
        "Выбери игрока из списка:",
        reply_markup=users_page_kb(users, 0, "give_tok:pick", "admin_back"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("give_tok:pick_page:"), StateFilter(AdminGiveTokens.target))
async def admin_give_tokens_page(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    page = int(call.data.split(":")[2])
    users = await get_all_users()
    await call.message.edit_reply_markup(
        reply_markup=users_page_kb(users, page, "give_tok:pick", "admin_back")
    )
    await call.answer()


@router.callback_query(F.data.startswith("give_tok:pick:"), StateFilter(AdminGiveTokens.target))
async def admin_give_tokens_pick(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    target_id = int(call.data.split(":")[2])
    user = await get_user(target_id)
    if not user:
        await call.answer("Игрок не найден.", show_alert=True)
        return

    await state.update_data(target_id=target_id)
    name = user_display(user)
    await state.set_state(AdminGiveTokens.amount)
    await call.message.edit_text(
        f"👤 Игрок выбран: <b>{name}</b>  (ID: <code>{target_id}</code>)\n"
        f"🪙 Текущий баланс: <b>{fmt_coins(user['balance'])}</b> коинов\n\n"
        f"💬 Введи сумму коинов для начисления.\n"
        f"Чтобы <b>списать</b>, введи число со знаком минус (например: -500):"
    )
    await call.answer()


@router.message(StateFilter(AdminGiveTokens.amount))
async def admin_give_tokens_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().replace(" ", "")
    try:
        amount = int(raw)
        assert amount != 0
    except Exception:
        await message.answer("❌ Введи целое число, отличное от 0 (например: 1000 или -500):")
        return

    data      = await state.get_data()
    target_id = data["target_id"]

    user = await get_user(target_id)
    if not user:
        # Пользователь мог быть удалён из БД между шагами — маловероятно, но проверяем
        await state.clear()
        await message.answer(
            "❌ Игрок больше не найден в базе. Операция отменена.",
            reply_markup=admin_menu_kb(),
        )
        return

    if amount < 0 and user["balance"] + amount < 0:
        await message.answer(
            f"❌ Нельзя списать больше, чем есть на балансе.\n"
            f"🪙 Текущий баланс игрока: <b>{fmt_coins(user['balance'])}</b> коинов.\n"
            f"Введи другую сумму:"
        )
        return

    await update_balance(target_id, amount)
    await state.clear()

    new_user = await get_user(target_id)
    name = user_display(new_user)

    action_word = "начислено" if amount > 0 else "списано"
    display_amount = f"+{fmt_coins(amount)}" if amount > 0 else f"-{fmt_coins(abs(amount))}"

    try:
        if amount > 0:
            await bot.send_message(
                target_id,
                f"🎁 <b>Администрация начислила тебе токены!</b>\n\n"
                f"🪙 Начислено: <b>+{fmt_coins(amount)}</b> коинов\n"
                f"💰 Новый баланс: <b>{fmt_coins(new_user['balance'])}</b> коинов",
            )
        else:
            await bot.send_message(
                target_id,
                f"⚠️ <b>Администрация списала с тебя токены.</b>\n\n"
                f"🪙 Списано: <b>{fmt_coins(abs(amount))}</b> коинов\n"
                f"💰 Новый баланс: <b>{fmt_coins(new_user['balance'])}</b> коинов",
            )
        notified = True
    except Exception:
        notified = False

    await message.answer(
        f"✅ <b>Готово!</b>\n\n"
        f"👤 Игрок: <b>{name}</b>  (ID: <code>{target_id}</code>)\n"
        f"🪙 {action_word.capitalize()}: <b>{display_amount}</b> коинов\n"
        f"💰 Новый баланс: <b>{fmt_coins(new_user['balance'])}</b> коинов\n"
        f"📩 Уведомление игроку: {'отправлено' if notified else 'не доставлено'}",
        reply_markup=admin_menu_kb(),
    )


def sub_bonus_kb(campaign_id: str, channel_url: str, coins: int) -> InlineKeyboardMarkup:
    """Клавиатура рассылки: кнопка-ссылка на канал + кнопка получения бонуса."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Подписаться на канал",
            url=channel_url,
        )],
        [InlineKeyboardButton(
            text="✅ Забрать бонус",
            callback_data=f"claim_sub:{campaign_id}",
        )],
    ])


# ─────────────────────────────────────────────
#  БОНУС ЗА ПОДПИСКУ — FSM (Шаги 1-3) + рассылка
# ─────────────────────────────────────────────

@router.callback_query(F.data == "admin:sub_bonus")
async def admin_sub_bonus_start(call: CallbackQuery, state: FSMContext):
    """Шаг 0: Запускаем FSM создания акции."""
    if not is_admin(call.from_user.id):
        return
    await state.set_state(SubBonus.coins)
    await call.message.edit_text(
        "📢 <b>Создание бонуса за подписку</b>\n\n"
        "Шаг 1/3 — Введи сумму коинов, которую получат пользователи за подписку\n"
        "<i>Например: 5000</i>"
    )
    await call.answer()


@router.message(StateFilter(SubBonus.coins))
async def sub_bonus_step_coins(message: Message, state: FSMContext):
    """Шаг 1: Принимаем сумму коинов."""
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("❌ Введи целое положительное число больше 0. Попробуй ещё раз:")
        return
    coins = int(raw)
    await state.update_data(sub_coins=coins)
    await state.set_state(SubBonus.channel_id)
    await message.answer(
        f"✅ Награда: <b>🪙 {fmt_coins(coins)} коинов</b>\n\n"
        "Шаг 2/3 — Введи технический <b>ID канала</b>\n"
        "<i>Например: -100123456789</i>\n\n"
        "⚠️ Бот должен быть администратором в этом канале!"
    )


@router.message(StateFilter(SubBonus.channel_id))
async def sub_bonus_step_channel_id(message: Message, state: FSMContext):
    """Шаг 2: Принимаем технический ID канала."""
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    # Технический ID должен быть числом (может быть отрицательным)
    try:
        channel_id = int(raw)
    except ValueError:
        await message.answer(
            "❌ Неверный формат ID. Введи числовой ID канала\n"
            "<i>Например: -100123456789</i>"
        )
        return
    await state.update_data(sub_channel_id=str(channel_id))
    await state.set_state(SubBonus.channel_url)
    await message.answer(
        f"✅ ID канала сохранён: <code>{channel_id}</code>\n\n"
        "Шаг 3/3 — Введи публичную <b>ссылку или юзернейм</b> канала\n"
        "<i>Например: @my_channel или https://t.me/joinchat/...</i>"
    )


@router.message(StateFilter(SubBonus.channel_url))
async def sub_bonus_step_channel_url(message: Message, state: FSMContext):
    """Шаг 3: Принимаем ссылку и запускаем рассылку."""
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("❌ Ссылка не может быть пустой. Введи юзернейм или ссылку:")
        return

    # Нормализуем: @username → https://t.me/username
    if raw.startswith("@"):
        channel_url = f"https://t.me/{raw[1:]}"
    else:
        channel_url = raw

    data        = await state.get_data()
    coins       = data["sub_coins"]
    channel_id  = data["sub_channel_id"]
    # campaign_id = уникальный ключ: channel_id + coins + timestamp (исключает коллизии)
    import time as _time
    campaign_id = f"{channel_id}_{coins}_{int(_time.time())}"

    await state.clear()

    # Сохраняем акцию в БД
    await create_sub_campaign(campaign_id, coins, channel_id, channel_url)

    # Подтверждение админу перед рассылкой
    await message.answer(
        f"✅ <b>Акция создана! Запускаю рассылку...</b>\n\n"
        f"🪙 Награда: <b>{fmt_coins(coins)} коинов</b>\n"
        f"📢 Канал: {channel_url}\n"
        f"🆔 ID: <code>{channel_id}</code>"
    )

    # ── Рассылка ──────────────────────────────
    users   = await get_all_users()
    sent    = 0
    failed  = 0
    text    = (
        "🎁 <b>Внимание! Доступен новый бонус за подписку!</b>\n"
        "Подпишись на наш канал и забери бесплатные коины!\n\n"
        f"🪙 <b>Награда: {fmt_coins(coins)} коинов</b>"
    )
    kb = sub_bonus_kb(campaign_id, channel_url, coins)

    for u in users:
        try:
            await bot.send_message(u["user_id"], text, reply_markup=kb)
            sent += 1
        except Exception as e:
            if hasattr(e, "retry_after"):
                await asyncio.sleep(e.retry_after + 1)
                try:
                    await bot.send_message(u["user_id"], text, reply_markup=kb)
                    sent += 1
                    await asyncio.sleep(0.05)
                    continue  # переходим к следующему пользователю
                except Exception:
                    pass
                # retry_after-попытка не удалась — считаем ошибкой
            failed += 1
        # Задержка между сообщениями — защита от флуда Telegram (≈20 msg/s)
        await asyncio.sleep(0.05)

    await message.answer(
        f"📊 <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего пользователей: <b>{len(users)}</b>\n"
        f"📩 Успешно отправлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        reply_markup=admin_menu_kb(),
    )


# ─────────────────────────────────────────────
#  ПОЛУЧЕНИЕ БОНУСА — callback «claim_sub:»
# ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("claim_sub:"))
async def claim_sub_bonus(call: CallbackQuery):
    """Пользователь нажал «✅ Забрать бонус» — проверяем подписку и выдаём монеты."""
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    campaign_id = call.data.split(":", 1)[1]
    user_id     = call.from_user.id

    # 1. Проверяем, не получал ли уже этот бонус
    if await has_claimed_bonus(user_id, campaign_id):
        await call.answer("⚠️ Ты уже получил этот бонус!", show_alert=True)
        return

    # 2. Получаем данные акции
    campaign = await get_sub_campaign(campaign_id)
    if not campaign:
        await call.answer("❌ Акция не найдена или устарела.", show_alert=True)
        return

    coins      = campaign["coins"]
    channel_id = campaign["channel_id"]

    # 3. Проверяем подписку через Telegram API
    #
    # Важно: для каналов Telegram выбрасывает TelegramBadRequest с текстом
    # "CHAT_MEMBER_NOT_FOUND" если пользователь НЕ является участником —
    # это штатная ситуация, не ошибка конфигурации.
    # Бот при этом может быть просто участником канала, администратором быть НЕ обязательно.
    is_subscribed = False
    try:
        member = await bot.get_chat_member(chat_id=int(channel_id), user_id=user_id)
        is_subscribed = member.status not in ("left", "kicked", "banned")

    except TelegramBadRequest as e:
        err = str(e)
        if "CHAT_MEMBER_NOT_FOUND" in err or "user not found" in err.lower():
            # Пользователь не состоит в канале — штатная ситуация
            is_subscribed = False
        elif "chat not found" in err.lower():
            # Неверный ID канала или бот не добавлен в канал вообще
            log.error(f"[SubBonus] Канал не найден (channel_id={channel_id}): {e}")
            await call.answer(
                "⚠️ Ошибка: канал не найден. Сообщите администратору.",
                show_alert=True,
            )
            return
        else:
            log.warning(f"[SubBonus] TelegramBadRequest (channel_id={channel_id}): {e}")
            await call.answer(
                "⚠️ Не удалось проверить подписку. Попробуйте позже.",
                show_alert=True,
            )
            return

    except TelegramForbiddenError as e:
        # Бот заблокирован пользователем или кикнут из канала
        log.warning(f"[SubBonus] TelegramForbiddenError (channel_id={channel_id}): {e}")
        await call.answer(
            "⚠️ Бот не может проверить подписку. Сообщите администратору.",
            show_alert=True,
        )
        return

    except Exception as e:
        log.error(f"[SubBonus] Unexpected error (channel_id={channel_id}): {e}")
        await call.answer("⚠️ Произошла ошибка. Попробуйте позже.", show_alert=True)
        return

    # 4. Пользователь НЕ подписан
    if not is_subscribed:
        await call.answer(
            "❌ Вы не подписались на канал! Подпишитесь, чтобы забрать коины.",
            show_alert=True,
        )
        return

    # 5. Пользователь подписан — начисляем коины и фиксируем в БД
    await update_balance(user_id, coins)
    await mark_bonus_claimed(user_id, campaign_id)

    user    = await get_user(user_id)
    new_bal = user["balance"] if user else coins

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await call.message.answer(
        f"🎉 <b>Поздравляем!</b>\n\n"
        f"Тебе начислено <b>🪙 {fmt_coins(coins)} коинов!</b>\n"
        f"💰 Текущий баланс: <b>{fmt_coins(new_bal)} коинов</b>\n\n"
        f"Удачи в ставках! 🍀"
    )
    await call.answer()


# ── Список зарегистрированных игроков ───────
@router.callback_query(F.data == "admin:players_list")
async def admin_players_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    users = await get_all_users()
    count = len(users)

    if not users:
        await call.message.edit_text(
            "👥 Зарегистрированных игроков нет.",
            reply_markup=admin_menu_kb(),
        )
        return

    # Формируем нумерованный список (разбиваем по 30 пользователей)
    await call.message.edit_text(
        f"👥 <b>Список игроков</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Всего зарегистрировано: <b>{count} чел.</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Список будет отправлен следующими сообщениями...",
        reply_markup=admin_menu_kb(),
    )

    chunk_size = 30
    for chunk_idx in range(0, len(users), chunk_size):
        chunk = users[chunk_idx:chunk_idx + chunk_size]
        lines = []
        for i, u in enumerate(chunk, start=chunk_idx + 1):
            name    = user_display(u)
            uid_str = f"<code>{u['user_id']}</code>"
            bal     = fmt_coins(u["balance"])
            lines.append(f"{i}. {name} | ID: {uid_str} | 🪙 {bal}")
        await call.message.answer("\n".join(lines))
        await asyncio.sleep(0.1)

    await call.answer()


# ── Рассылка сообщения всем игрокам ─────────
@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(BroadcastMessage.text)
    await call.message.edit_text(
        "📣 <b>Рассылка сообщения всем игрокам</b>\n\n"
        "✏️ Введи текст сообщения, которое получат все зарегистрированные игроки.\n\n"
        "<i>Поддерживается HTML-разметка: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;, и т.д.</i>\n\n"
        "Для отмены напиши /admin"
    )
    await call.answer()


@router.message(StateFilter(BroadcastMessage.text))
async def admin_broadcast_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("❌ Текст не может быть пустым. Введи текст сообщения:")
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(BroadcastMessage.confirm)

    count = await get_users_count()
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить всем", callback_data="broadcast:confirm"),
            InlineKeyboardButton(text="❌ Отменить",       callback_data="broadcast:cancel"),
        ]
    ])
    # Пробуем показать предпросмотр с HTML; если разметка невалидна — показываем plain text
    try:
        await message.answer(
            f"📣 <b>Предпросмотр сообщения:</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Получателей: <b>{count} игроков</b>\n\n"
            f"Подтверди рассылку:",
            reply_markup=confirm_kb,
        )
    except Exception:
        # Невалидный HTML — показываем как plain text
        await message.answer(
            f"📣 Предпросмотр сообщения (plain text):\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Получателей: {count} игроков\n\n"
            f"⚠️ Текст содержит символы < > & — будет отправлен как plain text.\n"
            f"Подтверди рассылку:",
            reply_markup=confirm_kb,
            parse_mode=None,
        )


@router.callback_query(F.data == "broadcast:cancel", StateFilter(BroadcastMessage.confirm))
async def admin_broadcast_cancel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        "❌ Рассылка отменена.",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "broadcast:confirm", StateFilter(BroadcastMessage.confirm))
async def admin_broadcast_confirm(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    await state.clear()

    users  = await get_all_users()
    sent   = 0
    failed = 0

    await call.message.edit_text(
        f"📣 <b>Рассылка запущена...</b>\n\n"
        f"👥 Всего получателей: <b>{len(users)}</b>"
    )

    for u in users:
        try:
            await bot.send_message(u["user_id"], text, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception as e:
            # Если Telegram просит подождать — соблюдаем лимит
            if hasattr(e, "retry_after"):
                await asyncio.sleep(e.retry_after + 1)
                try:
                    await bot.send_message(u["user_id"], text, parse_mode=ParseMode.HTML)
                    sent += 1
                    await asyncio.sleep(0.05)
                    continue  # переходим к следующему пользователю
                except Exception:
                    pass
                # retry_after-попытка не удалась — пробуем plain text ниже
            # Если HTML невалиден или пользователь заблокировал бота — пробуем plain text
            try:
                await bot.send_message(u["user_id"], text, parse_mode=None)
                sent += 1
            except Exception:
                failed += 1
        await asyncio.sleep(0.05)

    await call.message.answer(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"👥 Всего пользователей: <b>{len(users)}</b>\n"
        f"📩 Успешно отправлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()



# ─────────────────────────────────────────────
#  УПРАВЛЕНИЕ МОДЕРАТОРАМИ (только admin)
# ─────────────────────────────────────────────

USERS_PAGE_SIZE = 10  # игроков на странице в списках выбора (модераторы / выдача токенов)


def users_page_kb(
    users: list,
    page: int,
    cb_prefix: str,
    back_cb: str,
    moders_ids: Optional[set] = None,
) -> InlineKeyboardMarkup:
    """Универсальная пагинированная клавиатура выбора игрока из списка.

    users      — полный список пользователей (срез на страницу делается тут же)
    page       — номер текущей страницы, с 0
    cb_prefix  — префикс callback_data для кнопки игрока: f"{cb_prefix}:{user_id}"
    back_cb    — callback_data кнопки «Назад/Отмена»
    moders_ids — если передано, игроки из этого множества помечаются 🛡 (для списка назначения модератора)
    """
    total_pages = max(1, (len(users) + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * USERS_PAGE_SIZE
    chunk = users[start:start + USERS_PAGE_SIZE]

    rows = []
    for u in chunk:
        name = f"@{u['username']}" if u.get("username") else (u.get("first_name") or f"ID {u['user_id']}")
        if moders_ids is not None and u["user_id"] in moders_ids:
            name = f"🛡 {name}"
        rows.append([InlineKeyboardButton(
            text=name,
            callback_data=f"{cb_prefix}:{u['user_id']}",
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"{cb_prefix}_page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"{cb_prefix}_page:{page + 1}"))
    if len(nav_row) > 1:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def moders_manage_kb(moders: list) -> InlineKeyboardMarkup:
    """Кнопки: добавить / удалить каждого модератора + назад."""
    rows = []
    for m in moders:
        name = f"@{m['username']}" if m.get("username") else (m.get("first_name") or f"ID {m['user_id']}")
        rows.append([InlineKeyboardButton(
            text=f"❌ Убрать {name}",
            callback_data=f"moder:remove:{m['user_id']}",
        )])
    rows.append([InlineKeyboardButton(text="➕ Добавить модератора", callback_data="moder:add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад",               callback_data="moder:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin:moders")
async def admin_moders_panel(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    moders = await get_moderators()
    if moders:
        lines = []
        for m in moders:
            name = f"@{m['username']}" if m.get("username") else (m.get("first_name") or "—")
            lines.append(f"🛡 {name}  <code>{m['user_id']}</code>")
        text = "🛡 <b>Модераторы</b>\n\n" + "\n".join(lines) + "\n\n<i>Нажми «Убрать» рядом с ником, или добавь нового:</i>"
    else:
        text = "🛡 <b>Модераторы</b>\n\nМодераторов пока нет."
    await call.message.edit_text(text, reply_markup=moders_manage_kb(moders))
    await call.answer()


@router.callback_query(F.data == "moder:back")
async def moder_back(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "🔐 <b>Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "admin_back")
async def admin_back_to_menu(call: CallbackQuery, state: FSMContext):
    """Универсальная кнопка «Назад» — сбрасывает FSM и открывает главное админ-меню."""
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        "🔐 <b>Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "noop")
async def noop_button(call: CallbackQuery):
    """Декоративная кнопка (например, счётчик страниц «2/3») — просто гасим часики."""
    await call.answer()


@router.callback_query(F.data == "moder:add")
async def moder_add_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    users  = await get_all_users()
    moders = await get_moderators()
    moders_ids = {m["user_id"] for m in moders}
    # Убираем самого админа из списка
    users = [u for u in users if u["user_id"] != ADMIN_ID]
    if not users:
        await call.answer("Нет зарегистрированных игроков.", show_alert=True)
        return
    await state.set_state(AddModer.choose_user)
    await call.message.edit_text(
        "➕ <b>Добавить модератора</b>\n\n"
        "Выбери пользователя из списка (🛡 — уже модератор):",
        reply_markup=users_page_kb(users, 0, "moder:pick", "admin:moders", moders_ids),
    )
    await call.answer()


@router.callback_query(F.data.startswith("moder:pick_page:"), StateFilter(AddModer.choose_user))
async def moder_pick_page(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    page = int(call.data.split(":")[2])
    users  = await get_all_users()
    moders = await get_moderators()
    moders_ids = {m["user_id"] for m in moders}
    users = [u for u in users if u["user_id"] != ADMIN_ID]
    await call.message.edit_reply_markup(
        reply_markup=users_page_kb(users, page, "moder:pick", "admin:moders", moders_ids)
    )
    await call.answer()


@router.callback_query(F.data.startswith("moder:pick:"), StateFilter(AddModer.choose_user))
async def moder_pick_user(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    user_id = int(call.data.split(":")[2])
    if await is_moderator_db(user_id):
        await call.answer("Этот пользователь уже модератор.", show_alert=True)
        return
    await add_moderator_db(user_id)
    await state.clear()
    # Пробуем уведомить нового модератора и сразу обновить его клавиатуру
    try:
        await bot.send_message(
            user_id,
            "🛡 <b>Поздравляем!</b>\n\nТебе выдана роль <b>модератора</b>.\n"
            "Кнопка 🔐 Панель теперь доступна в меню ниже.",
            reply_markup=main_menu_kb(is_staff=True),
        )
    except Exception:
        pass
    user = await get_user(user_id)
    name = user_display(user) if user else f"ID {user_id}"
    moders = await get_moderators()
    await call.message.edit_text(
        f"✅ <b>{name}</b> назначен модератором!\n\n"
        f"Он получит уведомление и увидит кнопку 🔐 Панель после /start.",
        reply_markup=moders_manage_kb(moders),
    )
    await call.answer()


@router.callback_query(F.data.startswith("moder:remove:"))
async def moder_remove(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_id = int(call.data.split(":")[2])
    await remove_moderator_db(user_id)

    # Принудительно сбрасываем FSM состояние модератора (если был в процессе)
    try:
        from aiogram.fsm.storage.base import StorageKey
        key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
        await dp.storage.set_state(key=key, state=None)
        await dp.storage.set_data(key=key, data={})
    except Exception:
        pass

    # Уведомляем бывшего модератора и обновляем его клавиатуру (без кнопки Панель)
    try:
        await bot.send_message(
            user_id,
            "ℹ️ Роль <b>модератора</b> была снята. Доступ к панели закрыт.",
            reply_markup=main_menu_kb(is_staff=False),
        )
    except Exception:
        pass

    moders = await get_moderators()
    await call.message.edit_text(
        "🛡 <b>Модератор удалён.</b>",
        reply_markup=moders_manage_kb(moders),
    )
    await call.answer()


# ─────────────────────────────────────────────
#  ПРОМОКОДЫ (АДМИН)
# ─────────────────────────────────────────────
@router.callback_query(F.data == "admin:create_promo")
async def admin_create_promo_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(CreatePromo.name)
    await call.message.edit_text(
        "🎁 <b>Создание промокода</b>\n\n"
        "📝 Шаг 1/3. Введи <b>название промокода</b>:\n"
        "<i>Например: FREE500 или START</i>"
    )
    await call.answer()


@router.message(StateFilter(CreatePromo.name))
async def create_promo_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    code = normalize_promo_code(message.text)
    if not code:
        await message.answer("❌ Название не может быть пустым. Введи название промокода:")
        return

    existing = await get_promo_code_db(code)
    if existing:
        await message.answer(
            f"❌ Промокод <b>{code}</b> уже существует. Введи другое название:"
        )
        return

    await state.update_data(code=code)
    await state.set_state(CreatePromo.activations)
    await message.answer(
        "📝 Шаг 2/3. Введи <b>количество активаций</b> (целое число):\n"
        "<i>Например: 10</i>"
    )


@router.message(StateFilter(CreatePromo.activations))
async def create_promo_activations(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    activations, error = validate_promo_activations(message.text)
    if error:
        await message.answer(error)
        return

    await state.update_data(activations=activations)
    await state.set_state(CreatePromo.amount)
    await message.answer(
        "📝 Шаг 3/3. Введи <b>сумму</b>, которую получит пользователь:\n"
        "<i>Например: 150 или 500.50</i>"
    )


@router.message(StateFilter(CreatePromo.amount))
async def create_promo_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    amount, error = validate_promo_amount(message.text)
    if error:
        await message.answer(error)
        return

    data = await state.get_data()
    code = data["code"]
    activations = data["activations"]

    created = await create_promo_code_db(code, amount, activations)
    await state.clear()

    if not created:
        # Крайне маловероятная гонка: код успели создать между шагом 1 и шагом 3
        await message.answer(
            f"❌ Промокод <b>{code}</b> уже существует. Попробуй создать промокод "
            f"с другим названием.",
            reply_markup=admin_menu_kb(),
        )
        return

    amount_str = fmt_coins(int(amount)) if amount == int(amount) else amount
    await message.answer(
        f"✅ Промокод <b>{code}</b> успешно создан!\n"
        f"💰 Сумма: <b>{amount_str}</b>\n"
        f"🔢 Активаций: <b>{activations}</b>",
        reply_markup=admin_menu_kb(),
    )


# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
import tempfile

LOCK_FILE = os.path.join(tempfile.gettempdir(), "standoff_bot.lock")


def acquire_lock():
    """Блокировочный файл — не даёт запустить второй процесс бота.

    Работает и на Linux/macOS (через fcntl), и на Windows (через msvcrt),
    чтобы второй запущенный процесс бота не дублировал сообщения.
    """
    lock_fd = open(LOCK_FILE, "w")

    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            log.error(
                "❌ Бот уже запущен! Завершаю процесс.\n"
                "Если это ошибка (старый процесс не закрылся) — "
                "удали файл: " + LOCK_FILE + " или перезагрузи компьютер."
            )
            raise SystemExit(1)
    else:
        import fcntl
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error(
                "❌ Бот уже запущен! Завершаю процесс.\n"
                "Если это ошибка — удали файл: " + LOCK_FILE
            )
            raise SystemExit(1)

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd  # держим открытым до конца процесса


async def main():
    await init_db()

    # Сбрасываем webhook на случай если был включён — иначе polling не получит апдейты
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook сброшен.")
    except Exception as e:
        log.warning(f"Не удалось сбросить webhook: {e}")

    log.info("БД инициализирована. Запуск бота...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    _lock = acquire_lock()
    try:
        asyncio.run(main())
    finally:
        _lock.close()
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
