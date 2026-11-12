import asyncio
import logging
import os
import re
import time
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web

import calc
import monitor
import onchain
from calc import (ADDRESS_RE, build_result_message, calc_for_row, compute_position, esc, format_amount,
                  format_currency, format_price, is_stablecoin, position_summary, render_wallet_results,
                  row_prices, short_addr, symbols_needed, validate_number, validate_ticker, wallet_title)
from chains import CHAINS, enabled_chains
from db import DB
from prices import CoinMarketCapPriceFetcher

# =============================================================================
# CONFIG
# =============================================================================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан в переменных окружения")

MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
MAX_WALLETS = int(os.getenv("MAX_WALLETS", "5"))
COOLDOWN = 10  # сек между тяжёлыми ручными запросами

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage(), fsm_strategy=FSMStrategy.USER_IN_CHAT)
dp.message.filter(F.chat.type == "private")
db = DB(os.getenv("DB_PATH", "monitor.db"))
cmc = CoinMarketCapPriceFetcher(os.getenv("COINMARKETCAP_API_KEY", ""),
                                cache_ttl=int(os.getenv("PRICE_TTL", "240")))
_last_call: Dict[int, float] = {}


class Calc(StatesGroup):
    supply_ticker = State()
    borrow_ticker = State()
    supply_amount = State()
    supply_price_choice = State()
    supply_price_manual = State()
    borrow_price_choice = State()
    borrow_price_manual = State()
    max_ltv = State()
    lt = State()
    mode = State()
    ltv = State()
    borrow = State()
    done = State()           # расчёт показан: выбор «по расчёту» / «реальная позиция»
    watch_address = State()  # адрес для мониторинга по расчёту
    real_address = State()   # адрес для чтения реальной позиции
    real_confirm = State()   # реальная позиция показана, ждём подтверждения


# =============================================================================
# KEYBOARDS / HELPERS
# =============================================================================
def price_choice_kb(price: Optional[float]):
    rows = []
    if price is not None:
        rows.append([InlineKeyboardButton(text=f"📊 CoinMarketCap: {format_price(price)}",
                                          callback_data="price_cmc")])
    rows.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="price_manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)] for t, d in rows])


mode_kb = _kb(("🔢 По LTV", "mode_ltv"), ("💵 По сумме займа", "mode_borrow"))
result_kb = _kb(("🔗 Взять реальную позицию", "real_start"),
                ("🔔 Следить по расчёту", "watch_start"),
                ("🔄 Новый расчёт", "calc_new"))
skip_kb = _kb(("Пропустить (без адреса)", "watch_skip"))
back_kb = _kb(("↩️ Назад", "real_back"))
confirm_kb = _kb(("✅ Следить за кошельком", "real_confirm"), ("↩️ Назад", "real_back"))


async def send_blocks(message: types.Message, header: str, blocks: List[str], footer: str = "",
                      reply_markup=None):
    """Отправка с разбивкой по лимиту Telegram (4096 символов); клавиатура уходит с последним сообщением."""
    parts, cur = [], header
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > 3800:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{b}" if cur else b
    if footer:
        cur = f"{cur}\n\n{footer}" if cur else footer
    parts.append(cur)
    for i, text in enumerate(parts):
        await message.answer(text, reply_markup=reply_markup if i == len(parts) - 1 else None)


def _cooldown_ok(user_id: int) -> bool:
    now = time.time()
    if now - _last_call.get(user_id, 0) < COOLDOWN:
        return False
    _last_call[user_id] = now
    return True


def _chains_note() -> str:
    n = len(enabled_chains())
    return f"{n} сетях"


# =============================================================================
# КОМАНДЫ
# =============================================================================
async def begin_calc(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Введите <b>тикер залога</b>\n(например, ETH, BTC, SOL, USDC)")
    await state.set_state(Calc.supply_ticker)


@dp.message(Command("start"))
async def start_cmd(msg: types.Message, state: FSMContext):
    await state.clear()
    status = "✅" if cmc.is_available() else "❌"
    await msg.answer(
        "🤖 <b>DeFi Calculator + Position Monitor</b>\n\n"
        "1️⃣ Рассчитайте позицию: LTV, Health Factor, цена ликвидации\n"
        "2️⃣ Затем выберите:\n"
        "   🔗 <b>реальная позиция</b>: бот прочитает ваш кошелёк в Aave v3 по всем сетям "
        "(Ethereum, Arbitrum, Base, Monad и др.) и сверит с расчётом\n"
        "   🔔 <b>по расчёту</b>: слежение по параметрам калькулятора и живым ценам\n\n"
        f"📡 Цены CoinMarketCap: {status}\n"
        "Только адрес в Aave v3: ключи не нужны. /help — все команды\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    await begin_calc(msg, state)


@dp.message(Command("reset", "cancel"))
async def reset_cmd(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("✅ Сброшено. /start — новый расчёт")


@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    await msg.answer(
        "<b>📖 Команды</b>\n"
        "/start — калькулятор\n"
        "/add <code>0x… [название]</code> — следить за кошельком без калькулятора\n"
        "/list — всё, что на мониторинге (P1.. расчёты, W1.. кошельки)\n"
        "/status — HF сейчас\n"
        "/remove <code>P1</code> или <code>W1</code> — убрать\n"
        "/threshold <code>1.4</code> — порог алерта по HF\n"
        "/chains — сети и их состояние\n"
        "/stats — статистика\n\n"
        "<b>Два вида мониторинга</b>\n"
        "🔗 Кошелёк: реальные данные из блокчейна (Aave v3 и форки, все сети из /chains), "
        "учитывает проценты и любые ваши операции.\n"
        "🔔 По расчёту: HF считается по параметрам калькулятора и живым ценам CMC. "
        "Проценты и ваши операции не видны, после изменений пересоздайте.\n\n"
        "<b>Алерты</b>\n"
        "🟡 HF ниже порога (по умолчанию 1.5), 🔴 критично (при 1.5 это около 1.16). "
        "Пока позиция в опасной зоне, алерты повторяются (🟡 раз в час, 🔴 раз в 15 минут), "
        "затем придёт ✅.\n\n"
        "⚠️ Не охвачены: Aave V4 и позиции в других протоколах (Morpho, Compound, Euler). "
        "Новые позиции в неактивных сетях бот находит за ≤30 минут."
    )


@dp.message(Command("chains"))
async def chains_cmd(msg: types.Message):
    icon = {"ok": "✅", "unknown": "⏳", "unreachable": "⏳", "bad_config": "❌", "no_pool": "⚙️"}
    lines = []
    for c in CHAINS.values():
        extra = f" — {esc(c.detail)}" if c.detail else (" — проверяю…" if c.status == "unknown" else "")
        lines.append(f"{icon.get(c.status, '❔')} {c.emoji} {c.name}{extra}")
    await msg.answer("<b>🌐 Сети Aave v3</b>\n" + "\n".join(lines) +
                     "\n\n✅ работает · ⏳ RPC недоступен, повторю · ❌ адрес пула не прошёл проверку · "
                     "⚙️ нужен POOL_&lt;СЕТЬ&gt;")


@dp.message(Command("stats"))
async def stats_cmd(msg: types.Message):
    s = cmc.get_stats()
    ok = "настроен ✅" if cmc.is_available() else "не настроен ❌"
    cmc_age = (f"\nЦены обновлены {int((time.time() - cmc.last_ok_ts) // 60)} мин назад"
               if cmc.last_ok_ts else "")
    chain_age = (f"\nПоследняя проверка кошельков {int((time.time() - monitor.last_chain_cycle) // 60)} мин назад"
                 if monitor.last_chain_cycle else "")
    working = sum(1 for c in CHAINS.values() if c.status == "ok")
    await msg.answer(
        f"<b>📊 Статистика</b>\n\nCoinMarketCap: {ok}\nЗапросов: {s['api_calls']} "
        f"(успешно {s['success']}, ошибок {s['fail']}), из кэша: {s['cache_hits']}{cmc_age}\n"
        f"Сетей работает: {working}/{len(CHAINS)}{chain_age}"
    )


@dp.message(Command("list", "positions"))
async def list_cmd(msg: types.Message):
    uid = msg.from_user.id
    positions, wallets = await db.list_positions(uid), await db.list_wallets(uid)
    if not positions and not wallets:
        await msg.answer("На мониторинге ничего нет. /start — расчёт или /add 0x…")
        return
    out = []
    if wallets:
        out.append("<b>👛 Кошельки (on-chain)</b>")
        for i, w in enumerate(wallets, 1):
            lab = f"<b>{esc(w['label'])}</b> " if w["label"] else ""
            out.append(f"W{i}. {lab}<code>{w['address']}</code>")
    if positions:
        out.append("\n<b>🔔 По расчёту</b>")
        for i, p in enumerate(positions, 1):
            addr = f" · <code>{short_addr(p['address'])}</code>" if p["address"] else ""
            out.append(f"P{i}. <b>{esc(p['supply_ticker'])} → {esc(p['borrow_ticker'])}</b>: "
                       f"{format_amount(p['supply_amount'])} / {format_amount(p['borrow_tokens'])}{addr}")
    thr = await db.get_threshold(uid)
    await msg.answer("\n".join(out) + f"\n\nПорог HF: {thr:.2f}\n/status · /remove P1 или W1")


@dp.message(Command("remove"))
async def remove_cmd(msg: types.Message, command: CommandObject):
    uid = msg.from_user.id
    m = re.fullmatch(r"([pPwW])\s*(\d+)", (command.args or "").strip())
    if not m:
        await msg.answer("Использование: <code>/remove W1</code> или <code>/remove P2</code> (номер из /list)")
        return
    kind, idx = m.group(1).upper(), int(m.group(2))
    rows = await (db.list_wallets(uid) if kind == "W" else db.list_positions(uid))
    if not 1 <= idx <= len(rows):
        await msg.answer("❌ Нет такого номера. Смотрите /list")
        return
    rid = rows[idx - 1]["id"]
    await (db.remove_wallet(uid, rid) if kind == "W" else db.remove_position(uid, rid))
    await msg.answer("🗑 Убрано с мониторинга.")


@dp.message(Command("threshold"))
async def threshold_cmd(msg: types.Message, command: CommandObject):
    uid = msg.from_user.id
    arg = (command.args or "").strip()
    if not arg:
        await msg.answer(f"Текущий порог: <b>{await db.get_threshold(uid):.2f}</b>\n"
                         "Изменить: <code>/threshold 1.4</code>")
        return
    ok, value, _ = validate_number(arg, min_val=1.04, max_val=5)
    if not ok:
        await msg.answer("❌ Введите число от 1.05 до 5, например <code>/threshold 1.4</code>")
        return
    await db.set_threshold(uid, value)
    await msg.answer(f"✅ Порог HF: <b>{value:.2f}</b>")


@dp.message(Command("status"))
async def status_cmd(msg: types.Message):
    uid = msg.from_user.id
    positions, wallets = await db.list_positions(uid), await db.list_wallets(uid)
    if not positions and not wallets:
        await msg.answer("На мониторинге ничего нет. /start — расчёт или /add 0x…")
        return
    if not _cooldown_ok(uid):
        await msg.answer(f"⏳ Не чаще раза в {COOLDOWN} секунд.")
        return
    if wallets:
        await msg.answer(f"⏳ Читаю кошельки в {_chains_note()}…")
        all_results = await asyncio.gather(*(onchain.fetch_all(w["address"]) for w in wallets))
        for w, results in zip(wallets, all_results):
            blocks, failed = render_wallet_results(results)
            head = f"👛 {wallet_title(w['address'], w['label'])}"
            footer = f"⚠️ Нет данных: {', '.join(failed)}" if failed else ""
            if not blocks:
                head += "\nпозиций нет"
            await send_blocks(msg, head, blocks, footer)
    if positions:
        prices = await cmc.get_prices(symbols_needed(positions))
        for p in positions:
            pr = row_prices(p, prices)
            if pr is None:
                await msg.answer(f"{calc.position_title(p)}\n⚠️ Не удалось получить цены, попробуйте позже.")
            else:
                await msg.answer("🔔 " + position_summary(p, calc_for_row(p, *pr)))


# =============================================================================
# КАЛЬКУЛЯТОР (FSM)
# =============================================================================
@dp.message(Calc.supply_ticker)
async def process_supply_ticker(msg: types.Message, state: FSMContext):
    valid, ticker, error = validate_ticker(msg.text)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите тикер:")
        return
    await state.update_data(supply_ticker=ticker)
    await msg.answer(f"✅ Залог: <b>{ticker}</b>\n\n━━━━━━━━━━━━━━━━━━━━\n"
                     "Введите <b>тикер заёмного актива</b>\n(например, USDT, ETH)")
    await state.set_state(Calc.borrow_ticker)


@dp.message(Calc.borrow_ticker)
async def process_borrow_ticker(msg: types.Message, state: FSMContext):
    valid, ticker, error = validate_ticker(msg.text)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите тикер:")
        return
    await state.update_data(borrow_ticker=ticker)
    data = await state.get_data()
    await msg.answer(f"✅ Заём: <b>{ticker}</b>\n\n━━━━━━━━━━━━━━━━━━━━\n"
                     f"Введите <b>количество {data['supply_ticker']}</b> в залоге")
    await state.set_state(Calc.supply_amount)


@dp.message(Calc.supply_amount)
async def process_supply_amount(msg: types.Message, state: FSMContext):
    valid, value, error = validate_number(msg.text, min_val=0.000001)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите количество:")
        return
    await state.update_data(supply_amount=value)
    ticker = (await state.get_data())["supply_ticker"]
    await msg.answer(f"✅ Количество: {format_amount(value)} {ticker}")
    if cmc.is_available():
        price = await cmc.get_price_usd(ticker)
        if price is not None:
            await state.update_data(cmc_supply_price=price)
            await msg.answer(f"💱 <b>Цена {ticker} найдена:</b>\n• 📊 CoinMarketCap: {format_price(price)}\n\n"
                             "Выберите источник:", reply_markup=price_choice_kb(price))
            await state.set_state(Calc.supply_price_choice)
            return
    await msg.answer(f"❌ Не удалось получить цену {ticker} из API.\n━━━━━━━━━━━━━━━━━━━━\n"
                     f"Введите <b>цену {ticker} в USD</b> вручную:")
    await state.set_state(Calc.supply_price_manual)


@dp.callback_query(F.data.startswith("price_"), Calc.supply_price_choice)
async def process_supply_price_choice(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if cb.data == "price_manual" or data.get("cmc_supply_price") is None:
        await cb.message.edit_text(f"✏️ Введите <b>цену {data['supply_ticker']} в USD</b> вручную:")
        await state.set_state(Calc.supply_price_manual)
        return
    price = data["cmc_supply_price"]
    await state.update_data(supply_price=price, supply_price_source="cmc")
    await cb.message.edit_text(f"✅ Цена залога (CMC): {format_price(price)}")
    await ask_borrow_price(cb.message, state)


@dp.message(Calc.supply_price_manual)
async def process_supply_price_manual(msg: types.Message, state: FSMContext):
    valid, price, error = validate_number(msg.text, min_val=0.000001)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите цену:")
        return
    await state.update_data(supply_price=price, supply_price_source="manual")
    await msg.answer(f"✅ Цена залога (вручную): {format_price(price)}")
    await ask_borrow_price(msg, state)


async def ask_borrow_price(message: types.Message, state: FSMContext):
    ticker = (await state.get_data())["borrow_ticker"]
    if cmc.is_available():
        price = await cmc.get_price_usd(ticker)
        if price is not None:
            await state.update_data(cmc_borrow_price=price)
            await message.answer(f"💱 <b>Цена {ticker} найдена:</b>\n• 📊 CoinMarketCap: {format_price(price)}\n\n"
                                 "Выберите источник:", reply_markup=price_choice_kb(price))
            await state.set_state(Calc.borrow_price_choice)
            return
    await message.answer(f"❌ Не удалось получить цену {ticker} из API.\n━━━━━━━━━━━━━━━━━━━━\n"
                         f"Введите <b>цену {ticker} в USD</b> вручную:")
    await state.set_state(Calc.borrow_price_manual)


@dp.callback_query(F.data.startswith("price_"), Calc.borrow_price_choice)
async def process_borrow_price_choice(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if cb.data == "price_manual" or data.get("cmc_borrow_price") is None:
        await cb.message.edit_text(f"✏️ Введите <b>цену {data['borrow_ticker']} в USD</b> вручную:")
        await state.set_state(Calc.borrow_price_manual)
        return
    price = data["cmc_borrow_price"]
    await state.update_data(borrow_price=price, borrow_price_source="cmc")
    await cb.message.edit_text(f"✅ Цена займа (CMC): {format_price(price)}")
    await go_to_max_ltv(cb.message, state)


@dp.message(Calc.borrow_price_manual)
async def process_borrow_price_manual(msg: types.Message, state: FSMContext):
    valid, price, error = validate_number(msg.text, min_val=0.000001)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите цену:")
        return
    await state.update_data(borrow_price=price, borrow_price_source="manual")
    await msg.answer(f"✅ Цена займа (вручную): {format_price(price)}")
    await go_to_max_ltv(msg, state)


async def go_to_max_ltv(message: types.Message, state: FSMContext):
    d = await state.get_data()
    await message.answer(
        f"💰 Стоимость залога: <b>{format_currency(d['supply_amount'] * d['supply_price'])}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\nВведите <b>Maximum LTV</b> в %\n(например, 65)"
    )
    await state.set_state(Calc.max_ltv)


@dp.message(Calc.max_ltv)
async def process_max_ltv(msg: types.Message, state: FSMContext):
    valid, value, error = validate_number(msg.text, min_val=0.1, max_val=99.9)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите Max LTV (%):")
        return
    await state.update_data(max_ltv=value)
    d = await state.get_data()
    max_usd = d["supply_amount"] * d["supply_price"] * value / 100
    await msg.answer(
        f"✅ Max LTV: <b>{value:g}%</b>\n\n💰 <b>Максимально возможный заём:</b>\n"
        f"• <b>{format_currency(max_usd)}</b> = <b>{format_amount(max_usd / d['borrow_price'])} {d['borrow_ticker']}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\nВведите <b>порог ликвидации (LT)</b> в %\n(например, 80)"
    )
    await state.set_state(Calc.lt)


@dp.message(Calc.lt)
async def process_lt(msg: types.Message, state: FSMContext):
    valid, value, error = validate_number(msg.text, min_val=0.1, max_val=100)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите LT (%):")
        return
    max_ltv = (await state.get_data())["max_ltv"]
    if value < max_ltv:
        await msg.answer(f"❌ LT должен быть ≥ Max LTV ({max_ltv:g}%)\n\nВведите снова:")
        return
    await state.update_data(lt=value / 100)
    await msg.answer(f"✅ LT: {value:g}%\n\nВыберите режим расчёта:", reply_markup=mode_kb)
    await state.set_state(Calc.mode)


@dp.callback_query(F.data.startswith("mode_"), Calc.mode)
async def process_mode(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.data == "mode_ltv":
        await cb.message.edit_text("Введите желаемый <b>LTV (%)</b>:")
        await state.set_state(Calc.ltv)
    else:
        data = await state.get_data()
        await cb.message.edit_text(f"Введите <b>сумму займа в {data['borrow_ticker']}</b>:")
        await state.set_state(Calc.borrow)


@dp.message(Calc.ltv)
async def process_ltv(msg: types.Message, state: FSMContext):
    valid, ltv, error = validate_number(msg.text, min_val=0.1, max_val=99.9)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите LTV (%):")
        return
    d = await state.get_data()
    if ltv > d["max_ltv"]:
        await msg.answer(f"❌ LTV не может превышать Max LTV ({d['max_ltv']:g}%)\n\nВведите снова:")
        return
    borrow_usd = ltv / 100 * d["supply_amount"] * d["supply_price"]
    await state.update_data(borrow_tokens=borrow_usd / d["borrow_price"])
    await show_results(msg, state)


@dp.message(Calc.borrow)
async def process_borrow(msg: types.Message, state: FSMContext):
    valid, tokens, error = validate_number(msg.text, min_val=0.000001)
    if not valid:
        await msg.answer(f"❌ {error}\n\nВведите сумму займа:")
        return
    await state.update_data(borrow_tokens=tokens)
    await show_results(msg, state)


def _calc_from_state(d: dict) -> dict:
    return compute_position(d["supply_ticker"], d["borrow_ticker"], d["supply_amount"], d["supply_price"],
                            d["borrow_tokens"], d["borrow_price"], d["max_ltv"], d["lt"])


async def show_results(message: types.Message, state: FSMContext):
    d = await state.get_data()
    c = _calc_from_state(d)
    if c["ltv_percent"] > d["max_ltv"]:
        await message.answer(
            f"⚠️ <b>Внимание:</b> текущий LTV ({c['ltv_percent']:.2f}%) выше Max LTV ({d['max_ltv']:g}%).\n"
            "Позиция была бы сразу ликвидируема.\n\nУменьшите заём: введите другую сумму или /reset"
        )
        return
    await message.answer(
        build_result_message(d, c) +
        "\n\n━━━━━━━━━━━━━━━━━━━━\nЧто дальше?\n"
        "🔗 <b>Реальная позиция</b>: прочитаю ваш кошелёк в блокчейне и сверю с расчётом\n"
        "🔔 <b>По расчёту</b>: слежу за параметрами из калькулятора",
        reply_markup=result_kb,
    )
    await state.set_state(Calc.done)  # данные расчёта остаются в state


# =============================================================================
# 🔗 РЕАЛЬНАЯ ПОЗИЦИЯ (on-chain)
# =============================================================================
@dp.callback_query(F.data == "calc_new", Calc.done)
async def calc_new(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await begin_calc(cb.message, state)


@dp.callback_query(F.data == "real_start", Calc.done)
async def real_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if await db.count_wallets(cb.from_user.id) >= MAX_WALLETS:
        await cb.message.answer(f"❌ Лимит: {MAX_WALLETS} кошельков. Уберите лишний: /list, /remove W1")
        return
    await cb.message.answer(
        f"Отправьте <b>адрес кошелька</b> (0x…). Прочитаю позиции Aave v3 в {_chains_note()}.",
        reply_markup=back_kb,
    )
    await state.set_state(Calc.real_address)


@dp.callback_query(F.data == "real_back", StateFilter(Calc.real_address, Calc.real_confirm))
async def real_back(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Calc.done)
    await cb.message.answer("Выберите действие 👇", reply_markup=result_kb)


@dp.message(Calc.real_address)
async def real_address(msg: types.Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not ADDRESS_RE.match(text):
        await msg.answer("❌ Нужен адрес вида <code>0x</code> + 40 символов (ENS пока не поддерживается).",
                         reply_markup=back_kb)
        return
    address = text.lower()
    d = await state.get_data()
    await msg.answer(f"⏳ Читаю позиции в {_chains_note()}…")
    results = await onchain.fetch_all(address)
    c = _calc_from_state(d)
    blocks, failed = render_wallet_results(results, calc_hf=c["hf"])
    await state.update_data(real_address=address)

    header = f"🔗 <b>Реальные позиции</b> {wallet_title(address)}\nHF по расчёту: <b>{calc.format_number(c['hf'])}</b>"
    if not blocks:
        header += "\n\nПозиций Aave v3 не найдено. Можно всё равно следить: бот сообщит, когда она появится."
    footer = f"⚠️ Нет данных: {', '.join(failed)}" if failed else ""
    await send_blocks(msg, header, blocks, footer, reply_markup=confirm_kb)
    await state.set_state(Calc.real_confirm)


@dp.callback_query(F.data == "real_confirm", Calc.real_confirm)
async def real_confirm(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await state.get_data()
    uid, address = cb.from_user.id, d["real_address"]
    thr = await db.get_threshold(uid)
    if await db.add_wallet(uid, address, "") is None:
        text = "ℹ️ Этот кошелёк уже на мониторинге."
    else:
        text = (f"✅ <b>Кошелёк на мониторинге</b> {wallet_title(address)}\n"
                f"Слежу в {_chains_note()}, алерт при HF &lt; <b>{thr:.2f}</b>\n"
                f"Проверка раз в {max(1, monitor.CHAIN_INTERVAL // 60)} мин.\n\n"
                "/status · /list · /threshold")
    await state.clear()
    await cb.message.answer(text)


# =============================================================================
# 🔔 МОНИТОРИНГ ПО РАСЧЁТУ
# =============================================================================
async def resolve_live(ticker: str) -> Optional[bool]:
    """True — цена из CMC; False — фиксируем цену расчёта (только стейблкоины); None — невозможно."""
    if cmc.is_available() and await cmc.get_price_usd(ticker) is not None:
        return True
    return False if is_stablecoin(ticker) else None


@dp.callback_query(F.data == "watch_start", Calc.done)
async def watch_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    if await db.count_positions(cb.from_user.id) >= MAX_POSITIONS:
        await cb.message.answer(f"❌ Лимит: {MAX_POSITIONS} расчётных позиций. Уберите лишнюю: /list, /remove P1")
        return
    d = await state.get_data()
    sl, bl = await resolve_live(d["supply_ticker"]), await resolve_live(d["borrow_ticker"])
    bad = [t for t, v in ((d["supply_ticker"], sl), (d["borrow_ticker"], bl)) if v is None]
    if bad:
        await cb.message.answer(
            f"❌ Не получаю автоматическую цену {', '.join(bad)} из CoinMarketCap, "
            "поэтому следить по расчёту не смогу.\nПроверьте тикер и COINMARKETCAP_API_KEY. "
            "Вариант: 🔗 реальная позиция (цены не нужны)."
        )
        return
    await state.update_data(supply_live=sl, borrow_live=bl)
    await cb.message.answer(
        "Отправьте <b>адрес кошелька</b> (0x…), к которому относится позиция.\n"
        "Он нужен, чтобы отличать позиции друг от друга.", reply_markup=skip_kb)
    await state.set_state(Calc.watch_address)


async def save_position(message: types.Message, user_id: int, state: FSMContext, address: str):
    d = await state.get_data()
    await db.add_position(user_id, {**d, "address": address})
    thr = await db.get_threshold(user_id)
    c = _calc_from_state(d)
    fixed = [t for t, live in ((d["supply_ticker"], d["supply_live"]), (d["borrow_ticker"], d["borrow_live"]))
             if not live]
    note = f"\nℹ️ Цена {', '.join(fixed)} фиксирована из расчёта." if fixed else ""
    await state.clear()
    await message.answer(
        "✅ <b>Позиция на мониторинге (по расчёту)</b>\n"
        f"{d['supply_ticker']} → {d['borrow_ticker']}"
        + (f" · <code>{short_addr(address)}</code>" if address else "") + "\n"
        f"HF сейчас: <b>{calc.format_number(c['hf'])}</b>, алерт при HF &lt; <b>{thr:.2f}</b>\n"
        f"Проверка раз в {max(1, monitor.CALC_INTERVAL // 60)} мин.{note}\n\n"
        "/status · /list · /threshold"
    )


@dp.message(Calc.watch_address)
async def watch_address(msg: types.Message, state: FSMContext):
    text = (msg.text or "").strip()
    if not ADDRESS_RE.match(text):
        await msg.answer("❌ Нужен адрес вида <code>0x</code> + 40 символов. Или нажмите «Пропустить».",
                         reply_markup=skip_kb)
        return
    await save_position(msg, msg.from_user.id, state, text.lower())


@dp.callback_query(F.data == "watch_skip", Calc.watch_address)
async def watch_skip(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()
    await save_position(cb.message, cb.from_user.id, state, "")


@dp.callback_query(F.data.in_({"watch_start", "watch_skip", "calc_new", "real_start", "real_back", "real_confirm"}))
async def stale_callback(cb: types.CallbackQuery):
    await cb.answer("Это действие уже недоступно. Начните новый расчёт: /start", show_alert=True)


# =============================================================================
# 👛 КОШЕЛЁК БЕЗ КАЛЬКУЛЯТОРА: /add и просто адрес
# =============================================================================
async def add_wallet_flow(msg: types.Message, address: str, label: str):
    uid = msg.from_user.id
    if not ADDRESS_RE.match(address):
        await msg.answer("❌ Нужен адрес вида <code>0x</code> + 40 символов (ENS пока не поддерживается).")
        return
    address = address.lower()
    if await db.count_wallets(uid) >= MAX_WALLETS:
        await msg.answer(f"❌ Лимит: {MAX_WALLETS} кошельков. Уберите лишний: /list, /remove W1")
        return
    if await db.add_wallet(uid, address, label[:32]) is None:
        await msg.answer("ℹ️ Этот кошелёк уже на мониторинге.")
        return
    await msg.answer(f"✅ Слежу за {wallet_title(address, label[:32])}\n⏳ Читаю позиции в {_chains_note()}…")
    blocks, failed = render_wallet_results(await onchain.fetch_all(address))
    header = "" if blocks else "Позиций Aave v3 пока нет. Как только появится, бот начнёт следить."
    footer = f"⚠️ Нет данных: {', '.join(failed)}" if failed else ""
    await send_blocks(msg, header, blocks, footer)


@dp.message(Command("add"))
async def add_cmd(msg: types.Message, command: CommandObject):
    parts = (command.args or "").split(maxsplit=1)
    if not parts:
        await msg.answer("Использование: <code>/add 0x… [название]</code>")
        return
    await add_wallet_flow(msg, parts[0], parts[1].strip() if len(parts) > 1 else "")


@dp.message(StateFilter(None), F.text.regexp(r"^\s*0x[0-9a-fA-F]{40}(\s.*)?$"))
async def bare_address(msg: types.Message):
    parts = msg.text.strip().split(maxsplit=1)
    await add_wallet_flow(msg, parts[0], parts[1].strip() if len(parts) > 1 else "")


# =============================================================================
# HEALTH CHECK (Railway) + запуск
# =============================================================================
async def handle_health(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    port = int(os.environ.get("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Health server started on port %s", port)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await start_web_server()
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Калькулятор"),
        types.BotCommand(command="add", description="Следить за кошельком"),
        types.BotCommand(command="status", description="HF сейчас"),
        types.BotCommand(command="list", description="Что на мониторинге"),
        types.BotCommand(command="remove", description="Убрать (P1 / W1)"),
        types.BotCommand(command="threshold", description="Порог алерта по HF"),
        types.BotCommand(command="chains", description="Сети и их состояние"),
        types.BotCommand(command="help", description="Справка"),
    ])
    validation = asyncio.create_task(onchain.validate_all())  # самопроверка пулов в фоне
    monitors = asyncio.create_task(monitor.monitor_loops(bot, db, cmc))
    try:
        await dp.start_polling(bot)
    finally:
        monitors.cancel()
        validation.cancel()
        await cmc.close()
        await onchain.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
