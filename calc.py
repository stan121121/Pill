"""Расчёты по позиции и форматирование сообщений (логика калькулятора + общие функции для мониторинга)."""
import html
from typing import Dict, Iterable, Optional, Tuple

from chains import ADDRESS_RE, chain_emoji, chain_name

INF = float("inf")

# Приводим к верхнему регистру: is_stablecoin() сравнивает по .upper()
STABLECOINS = {s.upper() for s in (
    "USDT", "USDC", "DAI", "USD1", "PYUSD", "U", "USDD", "USDG",
    "RLUSD", "EURC", "AUSD", "alUSD", "OUSD",
)}


def is_stablecoin(ticker: str) -> bool:
    return ticker.upper().strip() in STABLECOINS


def esc(s) -> str:
    return html.escape(str(s))


# ------------------------------------------------------------------ валидация
def validate_number(text: str, min_val: float = 0, max_val: Optional[float] = None) -> Tuple[bool, float, str]:
    try:
        value = float((text or "").replace(",", ".").strip())
    except ValueError:
        return False, 0, "Введите число"
    if value != value or value in (INF, -INF):
        return False, 0, "Введите число"
    if value <= min_val:
        return False, 0, f"Значение должно быть больше {min_val}"
    if max_val is not None and value > max_val:
        return False, 0, f"Значение не должно превышать {max_val}"
    return True, value, ""


def validate_ticker(text: str, max_length: int = 10) -> Tuple[bool, str, str]:
    ticker = (text or "").upper().strip()
    if not ticker:
        return False, "", "Введите тикер"
    if len(ticker) > max_length:
        return False, "", f"Тикер слишком длинный (макс. {max_length} символов)"
    if not ticker.isalnum():
        return False, "", "Тикер может содержать только буквы и цифры"
    return True, ticker, ""


# ------------------------------------------------------------------ форматирование
def format_currency(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def format_price(value: float) -> str:
    if value >= 1:
        return f"${value:,.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    if value >= 0.0001:
        return f"${value:.6f}"
    return f"${value:.8f}"


def format_number(value: float, decimals: int = 2) -> str:
    return "∞" if value == INF else f"{value:.{decimals}f}"


def format_amount(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    return f"{v:.6f}".rstrip("0").rstrip(".") or "0"


def short_addr(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


# ------------------------------------------------------------------ математика
def calculate_health_factor(collateral_usd: float, lt: float, borrow_usd: float) -> float:
    if borrow_usd <= 0:
        return INF
    return (collateral_usd * lt) / borrow_usd


def calculate_liquidation_price(borrow_usd: float, supply_amount: float, lt: float) -> float:
    denominator = supply_amount * lt
    return borrow_usd / denominator if denominator > 0 else 0.0


def get_position_status(hf: float) -> Tuple[str, str]:
    if hf <= 1.0:
        return "🔴 ЛИКВИДАЦИЯ", "🔴"
    if hf < 1.3:
        return "🟡 ВНИМАНИЕ", "🟡"
    if hf < 2.0:
        return "🟢 БЕЗОПАСНО", "🟢"
    return "🔵 ОЧЕНЬ БЕЗОПАСНО", "🔵"


def compute_position(
    supply_ticker: str, borrow_ticker: str,
    supply_amount: float, supply_price: float,
    borrow_tokens: float, borrow_price: float,
    max_ltv: float, lt: float,
) -> dict:
    """max_ltv — в процентах, lt — доля (0.8). Единая функция для калькулятора и мониторинга."""
    collateral_usd = supply_amount * supply_price
    borrow_usd = borrow_tokens * borrow_price
    current_ltv = (borrow_usd / collateral_usd) * 100 if borrow_usd > 0 and collateral_usd > 0 else 0.0
    hf = calculate_health_factor(collateral_usd, lt, borrow_usd)
    buffer = (hf - 1) * 100 if hf != INF else 100.0

    supply_stable = is_stablecoin(supply_ticker)
    borrow_stable = is_stablecoin(borrow_ticker)
    scenarios = []

    if supply_stable and not borrow_stable:
        # залог стейбл, заём волатильный → риск в росте цены заёмного токена
        for pct in (10, 20, 30, 40, 50):
            new_price = borrow_price * (1 + pct / 100)
            scenarios.append((f"+{pct}%", new_price,
                              calculate_health_factor(collateral_usd, lt, borrow_tokens * new_price)))
        liq_price = (collateral_usd * lt) / borrow_tokens if borrow_tokens > 0 else 0.0
        liq_ticker, liq_ref = borrow_ticker, borrow_price
        scen_title = f"рост цены {borrow_ticker}"
    else:
        # залог волатильный (или оба стейблы) → риск в падении залога
        for drop in (10, 20, 30, 40, 50):
            new_price = supply_price * (1 - drop / 100)
            scenarios.append((f"-{drop}%", new_price,
                              calculate_health_factor(collateral_usd * (1 - drop / 100), lt, borrow_usd)))
        liq_price = calculate_liquidation_price(borrow_usd, supply_amount, lt)
        liq_ticker, liq_ref = supply_ticker, supply_price
        scen_title = f"падение цены {supply_ticker}"

    return {
        "supply_ticker": supply_ticker, "borrow_ticker": borrow_ticker,
        "supply_price": supply_price, "borrow_price": borrow_price,
        "collateral_usd": collateral_usd, "borrow_usd": borrow_usd,
        "max_ltv_percent": max_ltv, "lt_percent": lt * 100,
        "ltv_percent": current_ltv, "hf": hf, "buffer": buffer,
        "max_borrow": collateral_usd * max_ltv / 100,
        "scenarios": scenarios, "scen_title": scen_title,
        "liq_price": liq_price, "liq_ticker": liq_ticker,
        "liq_move_pct": (liq_price / liq_ref - 1) * 100 if liq_price > 0 and liq_ref > 0 else None,
        "supply_is_stable": supply_stable, "borrow_is_stable": borrow_stable,
    }


# ------------------------------------------------------------------ сообщения калькулятора
def build_result_message(data: dict, c: dict) -> str:
    status, emoji = get_position_status(c["hf"])
    src = "CMC" if data.get("supply_price_source") == "cmc" else "вручную"
    liq = format_price(c["liq_price"]) if c["liq_price"] > 0 else "н/д"
    move = f" ({c['liq_move_pct']:+.1f}% от текущей)" if c["liq_move_pct"] is not None else ""

    text = (
        f"<b>{emoji} РАСЧЁТ ПОЗИЦИИ</b>\n"
        f"Статус: <b>{status}</b>\n\n"
        f"<b>💎 ЗАЛОГ:</b>\n"
        f"• Актив: <b>{data['supply_ticker']}</b>\n"
        f"• Количество: {format_amount(data['supply_amount'])}\n"
        f"• Цена: {format_price(c['supply_price'])} ({src})\n"
        f"• Стоимость: <b>{format_currency(c['collateral_usd'])}</b>\n\n"
        f"<b>💰 ЗАЁМ:</b>\n"
        f"• Актив: <b>{data['borrow_ticker']}</b>\n"
        f"• Сумма: {format_amount(data['borrow_tokens'])} {data['borrow_ticker']} = {format_currency(c['borrow_usd'])}\n\n"
        f"<b>⚙️ ПАРАМЕТРЫ:</b>\n"
        f"• Max LTV: {c['max_ltv_percent']:g}%\n"
        f"• Порог ликвидации (LT): {c['lt_percent']:.1f}%\n"
        f"• Текущий LTV: <b>{c['ltv_percent']:.2f}%</b>\n\n"
        f"<b>📊 РИСКИ:</b>\n"
        f"• Health Factor: <b>{format_number(c['hf'])}</b>\n"
        f"• Цена ликвидации {c['liq_ticker']}: <b>{liq}</b>{move}\n"
        f"• Запас прочности: <b>{c['buffer']:.1f}%</b>\n"
        f"• Макс. возможный заём: {format_currency(c['max_borrow'])}\n\n"
        f"<b>📉 СЦЕНАРИИ ({c['scen_title']}):</b>\n"
    )
    for label, price, hf in c["scenarios"]:
        text += f"• {label} ({format_price(price)}) → HF: {format_number(hf)}\n"
    if c["hf"] < 1.3:
        text += ("\n<b>⚠️ РЕКОМЕНДАЦИИ:</b>\n• Увеличьте залог\n• Уменьшите заём\n"
                 "• Подготовьте средства для пополнения")
    return text


# ------------------------------------------------------------------ данные позиции из БД
def position_title(row: dict) -> str:
    t = f"<b>{esc(row['supply_ticker'])} → {esc(row['borrow_ticker'])}</b>"
    if row.get("address"):
        t += f" · <code>{short_addr(row['address'])}</code>"
    return t


def symbols_needed(rows: Iterable[dict]) -> set:
    s = set()
    for r in rows:
        if r["supply_live"]:
            s.add(r["supply_ticker"])
        if r["borrow_live"]:
            s.add(r["borrow_ticker"])
    return s


def row_prices(row: dict, prices: Dict[str, float]) -> Optional[Tuple[float, float]]:
    """Живые цены, где они доступны, иначе цена из расчёта (для стейблкоинов). None — цены нет."""
    sp = prices.get(row["supply_ticker"]) if row["supply_live"] else row["supply_price"]
    bp = prices.get(row["borrow_ticker"]) if row["borrow_live"] else row["borrow_price"]
    if not sp or not bp:
        return None
    return sp, bp


def calc_for_row(row: dict, sp: float, bp: float) -> dict:
    return compute_position(
        row["supply_ticker"], row["borrow_ticker"], row["supply_amount"], sp,
        row["borrow_tokens"], bp, row["max_ltv"], row["lt"],
    )


def position_summary(row: dict, c: dict) -> str:
    _, emoji = get_position_status(c["hf"])
    liq = format_price(c["liq_price"]) if c["liq_price"] > 0 else "н/д"
    move = f" ({c['liq_move_pct']:+.1f}% от текущей)" if c["liq_move_pct"] is not None else ""
    return (
        f"{position_title(row)}\n"
        f"• Залог: {format_amount(row['supply_amount'])} {esc(row['supply_ticker'])} = {format_currency(c['collateral_usd'])}\n"
        f"• Заём: {format_amount(row['borrow_tokens'])} {esc(row['borrow_ticker'])} = {format_currency(c['borrow_usd'])}\n"
        f"• Цены: {esc(row['supply_ticker'])} {format_price(c['supply_price'])} · "
        f"{esc(row['borrow_ticker'])} {format_price(c['borrow_price'])}\n"
        f"• Health Factor: <b>{format_number(c['hf'])}</b> {emoji}\n"
        f"• Цена ликвидации {esc(c['liq_ticker'])}: <b>{liq}</b>{move}"
    )


HEADS = {
    "warn": "🟡 <b>Health Factor ниже порога</b>",
    "crit": "🔴 <b>КРИТИЧНО: позиция близка к ликвидации</b>",
    "remind_warn": "🟡 <b>Напоминание: HF всё ещё ниже порога</b>",
    "remind_crit": "🔴 <b>Напоминание: позиция в опасной зоне</b>",
    "improved": "🟢 <b>HF вырос, но всё ещё ниже порога</b>",
    "recovered": "✅ <b>Позиция вернулась в норму</b>",
    "closed": "✅ <b>Долг погашен</b>",
}


def alert_text(kind: str, row: dict, c: dict, threshold: float) -> str:
    text = f"{HEADS[kind]}\n\n{position_summary(row, c)}\n\nВаш порог HF: {threshold:.2f}"
    if kind in ("warn", "crit", "remind_warn", "remind_crit"):
        text += "\n\n💡 Добавьте залог или погасите часть займа, затем пересоздайте мониторинг."
    return text


# ------------------------------------------------------------------ реальные позиции (on-chain)
def wallet_title(address: str, label: str = "") -> str:
    name = f"<b>{esc(label)}</b> " if label else ""
    return f"{name}<code>{short_addr(address)}</code>"


def real_position_block(pos, calc_hf: Optional[float] = None) -> str:
    """pos — onchain.Position. calc_hf — HF из калькулятора для сверки."""
    lines = [
        f"{chain_emoji(pos.chain)} <b>{chain_name(pos.chain)}</b>",
        f"• Залог: {format_currency(pos.collateral_usd)}",
        f"• Долг: {format_currency(pos.debt_usd)}",
    ]
    if pos.debt_usd > 0:
        _, emoji = get_position_status(pos.health_factor)
        lines.append(
            f"• LTV: {pos.current_ltv * 100:.1f}% "
            f"(лимит {pos.ltv * 100:g}%, ликвидация {pos.liq_threshold * 100:g}%)"
        )
        lines.append(f"• Health Factor: <b>{format_number(pos.health_factor)}</b> {emoji}")
        drop = pos.drop_to_liquidation
        if drop == 0:
            lines.append("• ⚠️ HF ≤ 1: позиция в зоне ликвидации")
        elif drop is not None:
            lines.append(f"• До ликвидации: залог −{drop * 100:.1f}%")
        if calc_hf and calc_hf != INF and pos.health_factor != INF:
            delta = (pos.health_factor / calc_hf - 1) * 100
            lines.append(f"• К расчёту: HF {calc_hf:.2f} → {pos.health_factor:.2f} ({delta:+.0f}%)")
    else:
        lines.append("• Долга нет, ликвидация не грозит")
    return "\n".join(lines)


def render_wallet_results(results: dict, calc_hf: Optional[float] = None):
    """-> (блоки по сетям с позициями, названия сетей с ошибкой)."""
    blocks, failed = [], []
    for key, r in results.items():
        if isinstance(r, Exception):
            failed.append(chain_name(key))
        elif r.has_position:
            blocks.append(real_position_block(r, calc_hf))
    return blocks, failed


def wallet_alert_text(kind: str, title: str, pos, threshold: float) -> str:
    text = f"{HEADS[kind]}\n{title}\nВаш порог HF: {threshold:.2f}\n\n{real_position_block(pos)}"
    if kind in ("warn", "crit", "remind_warn", "remind_crit"):
        text += "\n\n💡 Добавьте залог или погасите часть долга в протоколе."
    return text
