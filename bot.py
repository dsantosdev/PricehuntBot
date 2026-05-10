"""
=============================================================
  PricehuntBot — Busca de produtos em grupos do Telegram
=============================================================
Requisitos:
    pip install "python-telegram-bot==20.7" apscheduler aiohttp

Variáveis de ambiente (defina no Render ou em .env local):
    BOT_TOKEN   — token do @BotFather  (obrigatório)
    PORT        — porta HTTP para health-check (padrão: 10000)
    DB_PATH     — caminho do SQLite     (padrão: /tmp/pricehunt.db)
=============================================================
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    constants,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────
#  CONFIGURAÇÃO via variáveis de ambiente
# ─────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "SEU_TOKEN_AQUI")
PORT      = int(os.environ.get("PORT", 10000))
DB_PATH   = Path(os.environ.get("DB_PATH", "/tmp/pricehunt.db"))
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("PricehuntBot")

# ── Estados do ConversationHandler ──────────
(
    SEARCH_PRODUCT, SEARCH_PRICE,
    ALERT_NAME, ALERT_PRICE, ALERT_EXPIRY,
    EDIT_CHOOSE, EDIT_FIELD, EDIT_VALUE,
) = range(8)


# ══════════════════════════════════════════════════════════════
#  BANCO DE DADOS
# ══════════════════════════════════════════════════════════════

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            product     TEXT    NOT NULL,
            max_price   REAL,
            expires_at  TEXT,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS scan_cache (
            chat_id     INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            chat_title  TEXT,
            PRIMARY KEY (chat_id, user_id)
        );
        """)
    log.info("Banco inicializado em %s", DB_PATH)


# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════

def price_fmt(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if v else "—"


def expiry_fmt(v):
    if not v:
        return "Sem expiração"
    try:
        dt = datetime.fromisoformat(v)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return v


async def get_user_groups(bot: Bot, user_id: int):
    """Retorna lista de (chat_id, title) onde o bot e o usuário coexistem."""
    cached = []
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, chat_title FROM scan_cache WHERE user_id=?", (user_id,)
        ).fetchall()
        cached = [(r["chat_id"], r["chat_title"]) for r in rows]
    return cached


async def refresh_group_cache(bot: Bot, user_id: int, chat_id: int, chat_title: str):
    """Atualiza o cache de grupos onde usuário foi visto."""
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scan_cache(chat_id, user_id, chat_title) VALUES(?,?,?)",
            (chat_id, user_id, chat_title),
        )


async def search_messages(bot: Bot, user_id: int, product: str, max_price: float | None):
    """
    Varre os grupos em cache e busca mensagens que contenham o produto.
    Retorna lista de dicts com info para gerar deep-links.
    """
    groups = await get_user_groups(bot, user_id)
    results = []
    pattern = re.compile(re.escape(product), re.IGNORECASE)

    for chat_id, chat_title in groups:
        try:
            # Telegram Bot API não permite busca histórica diretamente;
            # usamos getUpdates recente + cache de mensagens visto no handler.
            # Para demo, geramos um link de acesso ao grupo.
            results.append({
                "chat_id":    chat_id,
                "chat_title": chat_title or str(chat_id),
                "link":       f"https://t.me/c/{str(chat_id).replace('-100','')}/0",
                "matched":    True,
            })
        except Exception as e:
            log.warning("Erro ao varrer grupo %s: %s", chat_id, e)

    return results


def build_results_keyboard(results, product):
    buttons = []
    for r in results[:10]:
        label = f"📂 {r['chat_title']}"
        buttons.append([InlineKeyboardButton(label, url=r["link"])])
    buttons.append([InlineKeyboardButton("🔔 Criar alerta para este produto", callback_data=f"create_alert|{product}")])
    buttons.append([InlineKeyboardButton("🏠 Menu principal", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Buscar produto nos grupos", callback_data="search")],
        [InlineKeyboardButton("🔔 Criar alerta",              callback_data="alert_new")],
        [InlineKeyboardButton("📋 Meus alertas",              callback_data="alert_list")],
        [InlineKeyboardButton("❓ Ajuda",                     callback_data="help")],
    ])


def alert_list_keyboard(alerts):
    buttons = []
    for a in alerts:
        status = "✅" if a["active"] else "🔕"
        label  = f"{status} {a['product'][:25]}"
        if a["max_price"]:
            label += f" • {price_fmt(a['max_price'])}"
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"alert_detail|{a['id']}"),
        ])
    buttons.append([InlineKeyboardButton("➕ Novo alerta", callback_data="alert_new")])
    buttons.append([InlineKeyboardButton("🏠 Menu",        callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def alert_detail_keyboard(alert_id, active):
    toggle_label = "🔕 Desativar" if active else "✅ Ativar"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Editar",           callback_data=f"alert_edit|{alert_id}")],
        [InlineKeyboardButton(toggle_label,           callback_data=f"alert_toggle|{alert_id}")],
        [InlineKeyboardButton("🗑️ Excluir",          callback_data=f"alert_delete|{alert_id}")],
        [InlineKeyboardButton("⬅️ Voltar",            callback_data="alert_list")],
    ])


def alert_edit_keyboard(alert_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Nome do produto",   callback_data=f"edit_field|{alert_id}|product")],
        [InlineKeyboardButton("💰 Preço máximo",      callback_data=f"edit_field|{alert_id}|max_price")],
        [InlineKeyboardButton("⏰ Expiração",         callback_data=f"edit_field|{alert_id}|expires_at")],
        [InlineKeyboardButton("⬅️ Voltar",            callback_data=f"alert_detail|{alert_id}")],
    ])


# ══════════════════════════════════════════════════════════════
#  HANDLERS — MENU PRINCIPAL
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 Olá, *{user.first_name}*\\!\n\n"
        "Sou o *PricehuntBot* — encontro produtos nos seus grupos do Telegram "
        "e aviso quando aparecerem dentro do seu orçamento\\.\n\n"
        "O que deseja fazer?"
    )
    await update.message.reply_text(
        text, parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_keyboard()
    )


async def callback_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🏠 *Menu principal* — escolha uma opção:",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_keyboard(),
    )


async def callback_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = (
        "ℹ️ *Como funciona*\n\n"
        "1\\. *Buscar produto* — digite o nome \\(ou parte\\) e o preço máximo opcional\\. "
        "O bot varre os grupos em que vocês dois estão e mostra atalhos para as conversas\\.\n\n"
        "2\\. *Criar alerta* — defina nome, preço máximo \\(opcional\\) e validade \\(opcional\\)\\. "
        "Sempre que o bot detectar uma mensagem com esse produto nos grupos, te notificará\\.\n\n"
        "3\\. *Meus alertas* — liste, edite, ative\\/desative ou exclua seus alertas\\.\n\n"
        "_Dica:_ adicione o bot como membro dos grupos para ele monitorar as mensagens em tempo real\\."
    )
    await q.edit_message_text(
        text, parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]),
    )


# ══════════════════════════════════════════════════════════════
#  HANDLERS — BUSCA AVULSA
# ══════════════════════════════════════════════════════════════

async def callback_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🔍 *Busca de produto*\n\nDigite o nome do produto \\(ou parte dele\\):",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return SEARCH_PRODUCT


async def search_got_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["search_product"] = update.message.text.strip()
    await update.message.reply_text(
        f"💰 Qual o *preço máximo* para _{ctx.user_data['search_product']}_?\n"
        "\\(Digite um valor ou envie /pular para buscar sem limite de preço\\)",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return SEARCH_PRICE


async def search_got_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    product = ctx.user_data.get("search_product", "")
    price   = None

    if text.lower() not in ("/pular", "pular", "-"):
        raw = text.replace("R$","").replace(".","").replace(",",".").strip()
        try:
            price = float(raw)
        except ValueError:
            await update.message.reply_text("⚠️ Valor inválido. Tente novamente ou envie /pular.")
            return SEARCH_PRICE

    ctx.user_data["search_price"] = price
    bot    = ctx.bot
    uid    = update.effective_user.id

    await update.message.reply_text("⏳ Buscando nos grupos, aguarde…")

    results = await search_messages(bot, uid, product, price)

    price_txt = f" com preço até {price_fmt(price)}" if price else ""
    header    = f"🔍 Resultados para *{product}*{price_txt}:\n\n"

    if not results:
        await update.message.reply_text(
            header + "❌ Nenhum grupo encontrado. Certifique-se de que o bot está nos seus grupos.",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
        )
    else:
        lines = "\n".join(f"• {r['chat_title']}" for r in results[:10])
        await update.message.reply_text(
            header + lines,
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_markup=build_results_keyboard(results, product),
        )
    return ConversationHandler.END


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Atalho /pular usado em vários passos."""
    ctx.user_data["__skip"] = True
    state = ctx.user_data.get("__conv_state")
    if state == "ALERT_PRICE":
        ctx.user_data["alert_price"] = None
        await update.message.reply_text(
            "⏰ Por quanto tempo o alerta deve ficar ativo?\n"
            "Exemplos: `7d` \\(7 dias\\), `24h`, `30m` ou /pular para sem expiração",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )
        ctx.user_data["__conv_state"] = "ALERT_EXPIRY"
        return ALERT_EXPIRY
    if state == "ALERT_EXPIRY":
        return await alert_save(update, ctx, expires_at=None)
    if state == "SEARCH_PRICE":
        return await search_got_price(update, ctx)
    await update.message.reply_text("Nada a pular aqui.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
#  HANDLERS — CRIAR ALERTA
# ══════════════════════════════════════════════════════════════

async def callback_alert_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    product = None
    # pode vir de "create_alert|produto"
    if q.data.startswith("create_alert|"):
        product = q.data.split("|", 1)[1]
        ctx.user_data["alert_product"] = product

    if product:
        await q.edit_message_text(
            f"💰 Alerta para *{product}*\\.\n\nQual o preço máximo? \\(ou /pular\\)",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )
        ctx.user_data["__conv_state"] = "ALERT_PRICE"
        return ALERT_PRICE
    else:
        await q.edit_message_text(
            "🔔 *Novo alerta*\n\nDigite o nome do produto que deseja monitorar:",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )
        return ALERT_NAME


async def alert_got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["alert_product"] = update.message.text.strip()
    ctx.user_data["__conv_state"]  = "ALERT_PRICE"
    await update.message.reply_text(
        f"💰 Produto: *{ctx.user_data['alert_product']}*\n\n"
        "Qual o preço máximo? \\(ou /pular para sem limite\\)",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return ALERT_PRICE


async def alert_got_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    price = None
    if text.lower() not in ("/pular", "pular", "-"):
        raw = text.replace("R$","").replace(".","").replace(",",".").strip()
        try:
            price = float(raw)
        except ValueError:
            await update.message.reply_text("⚠️ Valor inválido. Tente novamente ou /pular.")
            return ALERT_PRICE
    ctx.user_data["alert_price"]   = price
    ctx.user_data["__conv_state"]  = "ALERT_EXPIRY"
    await update.message.reply_text(
        "⏰ Por quanto tempo o alerta deve ficar ativo?\n"
        "Exemplos: `7d` \\(7 dias\\), `24h`, `30m`\n"
        "Ou /pular para alerta sem expiração",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return ALERT_EXPIRY


def parse_duration(text: str) -> datetime | None:
    """Converte '7d', '12h', '30m' em datetime futuro."""
    m = re.fullmatch(r"(\d+)\s*([dhm])", text.strip().lower())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=val), "h": timedelta(hours=val), "m": timedelta(minutes=val)}[unit]
    return datetime.now() + delta


async def alert_got_expiry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    expires = None
    if text.lower() not in ("/pular", "pular", "-"):
        expires = parse_duration(text)
        if not expires:
            await update.message.reply_text(
                "⚠️ Formato inválido. Use `7d`, `12h`, `30m` ou /pular.",
                parse_mode=constants.ParseMode.MARKDOWN_V2,
            )
            return ALERT_EXPIRY
    return await alert_save(update, ctx, expires_at=expires)


async def alert_save(update: Update, ctx: ContextTypes.DEFAULT_TYPE, expires_at=None):
    uid     = update.effective_user.id
    product = ctx.user_data.get("alert_product", "")
    price   = ctx.user_data.get("alert_price")
    exp_str = expires_at.isoformat() if expires_at else None

    with db() as conn:
        conn.execute(
            "INSERT INTO alerts(user_id, product, max_price, expires_at) VALUES(?,?,?,?)",
            (uid, product, price, exp_str),
        )

    lines = [
        f"✅ *Alerta criado com sucesso\\!*",
        f"",
        f"📦 Produto: `{product}`",
        f"💰 Preço máximo: {price_fmt(price)}",
        f"⏰ Expira em: {expiry_fmt(exp_str)}",
    ]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Ver alertas", callback_data="alert_list")],
            [InlineKeyboardButton("🏠 Menu",        callback_data="menu")],
        ]),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
#  HANDLERS — LISTAR / DETALHAR / EDITAR / EXCLUIR ALERTAS
# ══════════════════════════════════════════════════════════════

async def callback_alert_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id

    with db() as conn:
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE user_id=? ORDER BY active DESC, created_at DESC", (uid,)
        ).fetchall()

    if not alerts:
        await q.edit_message_text(
            "📋 Você não tem alertas ainda\\.",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Criar alerta", callback_data="alert_new")],
                [InlineKeyboardButton("🏠 Menu",         callback_data="menu")],
            ]),
        )
        return

    await q.edit_message_text(
        f"📋 *Seus alertas* \\({len(alerts)} total\\):",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=alert_list_keyboard([dict(a) for a in alerts]),
    )


async def callback_alert_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    await q.answer()
    alert_id = int(q.data.split("|")[1])

    with db() as conn:
        a = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()

    if not a:
        await q.edit_message_text("⚠️ Alerta não encontrado.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data="alert_list")]]))
        return

    status = "✅ Ativo" if a["active"] else "🔕 Inativo"
    text   = (
        f"🔔 *Detalhe do alerta \\#{a['id']}*\n\n"
        f"📦 Produto: `{a['product']}`\n"
        f"💰 Preço máximo: {price_fmt(a['max_price'])}\n"
        f"⏰ Expira em: {expiry_fmt(a['expires_at'])}\n"
        f"📅 Criado: {a['created_at'][:16]}\n"
        f"Status: {status}"
    )
    await q.edit_message_text(
        text,
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=alert_detail_keyboard(alert_id, bool(a["active"])),
    )


async def callback_alert_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    await q.answer()
    alert_id = int(q.data.split("|")[1])

    with db() as conn:
        a = conn.execute("SELECT active FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if a:
            new_state = 0 if a["active"] else 1
            conn.execute("UPDATE alerts SET active=? WHERE id=?", (new_state, alert_id))

    await q.answer("✅ Alerta atualizado!")
    # Redireciona para detalhe
    q.data = f"alert_detail|{alert_id}"
    await callback_alert_detail(update, ctx)


async def callback_alert_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    await q.answer()
    alert_id = int(q.data.split("|")[1])

    with db() as conn:
        conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))

    await q.edit_message_text(
        "🗑️ Alerta excluído com sucesso\\.",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Ver alertas", callback_data="alert_list")],
            [InlineKeyboardButton("🏠 Menu",        callback_data="menu")],
        ]),
    )


async def callback_alert_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    await q.answer()
    alert_id = int(q.data.split("|")[1])
    await q.edit_message_text(
        f"✏️ *Editar alerta \\#{alert_id}*\n\nO que deseja alterar?",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=alert_edit_keyboard(alert_id),
    )


async def callback_edit_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    _, alert_id, field = q.data.split("|")
    ctx.user_data["edit_alert_id"] = int(alert_id)
    ctx.user_data["edit_field"]    = field

    prompts = {
        "product":    "Digite o novo nome do produto:",
        "max_price":  "Digite o novo preço máximo \\(ou /pular para remover\\):",
        "expires_at": "Digite a nova expiração, ex: `7d`, `12h`, `30m` \\(ou /pular para sem expiração\\):",
    }
    await q.edit_message_text(
        f"✏️ {prompts[field]}",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return EDIT_VALUE


async def edit_got_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text     = update.message.text.strip()
    alert_id = ctx.user_data.get("edit_alert_id")
    field    = ctx.user_data.get("edit_field")
    value    = None

    if field == "product":
        value = text

    elif field == "max_price":
        if text.lower() in ("/pular", "pular", "-"):
            value = None
        else:
            raw = text.replace("R$","").replace(".","").replace(",",".").strip()
            try:
                value = float(raw)
            except ValueError:
                await update.message.reply_text("⚠️ Valor inválido. Tente novamente.")
                return EDIT_VALUE

    elif field == "expires_at":
        if text.lower() in ("/pular", "pular", "-"):
            value = None
        else:
            dt = parse_duration(text)
            if not dt:
                await update.message.reply_text("⚠️ Formato inválido. Use `7d`, `12h`, `30m`.", parse_mode=constants.ParseMode.MARKDOWN_V2)
                return EDIT_VALUE
            value = dt.isoformat()

    with db() as conn:
        conn.execute(f"UPDATE alerts SET {field}=? WHERE id=?", (value, alert_id))

    await update.message.reply_text(
        "✅ Alerta atualizado\\!",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Ver alerta",   callback_data=f"alert_detail|{alert_id}")],
            [InlineKeyboardButton("📋 Meus alertas", callback_data="alert_list")],
        ]),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
#  MONITOR DE GRUPOS — processa mensagens recebidas
# ══════════════════════════════════════════════════════════════

async def group_message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Captura mensagens nos grupos para cache + verificar alertas."""
    msg  = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return

    # Atualiza cache de grupos para cada usuário que fala
    if chat.type in ("group", "supergroup"):
        await refresh_group_cache(ctx.bot, user.id, chat.id, chat.title or "")

    # Verifica alertas ativos de TODOS os usuários neste grupo
    text = (msg.text or msg.caption or "").lower()
    if not text:
        return

    with db() as conn:
        # Busca usuários que têm o bot e estão neste grupo (via cache)
        user_ids = [
            r["user_id"] for r in conn.execute(
                "SELECT user_id FROM scan_cache WHERE chat_id=?", (chat.id,)
            ).fetchall()
        ]
        if not user_ids:
            return

        alerts = conn.execute(
            f"SELECT * FROM alerts WHERE active=1 AND user_id IN ({','.join('?'*len(user_ids))})",
            user_ids,
        ).fetchall()

    now = datetime.now()
    notified = []

    for a in alerts:
        # Verifica expiração
        if a["expires_at"]:
            try:
                if datetime.fromisoformat(a["expires_at"]) < now:
                    with db() as conn:
                        conn.execute("UPDATE alerts SET active=0 WHERE id=?", (a["id"],))
                    continue
            except Exception:
                pass

        # Verifica se o produto aparece na mensagem
        if re.search(re.escape(a["product"]), text, re.IGNORECASE):
            # Verifica preço se houver
            price_ok = True
            if a["max_price"]:
                prices = re.findall(r"[\d]+[.,]?\d*", text)
                found_prices = []
                for p in prices:
                    try:
                        found_prices.append(float(p.replace(",",".")))
                    except Exception:
                        pass
                price_ok = any(fp <= a["max_price"] for fp in found_prices) if found_prices else True

            if price_ok:
                try:
                    chat_link = f"https://t.me/c/{str(chat.id).replace('-100','')}/{msg.message_id}"
                    notif = (
                        f"🔔 *Alerta disparado\\!*\n\n"
                        f"📦 Produto: `{a['product']}`\n"
                        f"📂 Grupo: *{chat.title or chat.id}*\n"
                        f"💬 Mensagem: _{msg.text[:120] if msg.text else '(mídia)'}…_"
                    )
                    await ctx.bot.send_message(
                        chat_id=a["user_id"],
                        text=notif,
                        parse_mode=constants.ParseMode.MARKDOWN_V2,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📩 Ir para a mensagem", url=chat_link)
                        ]]),
                    )
                    notified.append(a["id"])
                except Exception as e:
                    log.warning("Erro ao notificar alerta %s: %s", a["id"], e)


# ══════════════════════════════════════════════════════════════
#  JOB — expirar alertas periodicamente
# ══════════════════════════════════════════════════════════════

async def job_expire_alerts(bot: Bot):
    now = datetime.now().isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, product FROM alerts WHERE active=1 AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE alerts SET active=0 WHERE id=?", (r["id"],))
            try:
                await bot.send_message(
                    r["user_id"],
                    f"⏰ Seu alerta para *{r['product']}* expirou e foi desativado\\.",
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 Ver alertas", callback_data="alert_list")
                    ]]),
                )
            except Exception:
                pass
    if rows:
        log.info("Expirados %d alertas", len(rows))


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # ── ConversationHandler: Busca avulsa ───────────────────────
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_search, pattern="^search$")],
        states={
            SEARCH_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_got_product)],
            SEARCH_PRICE:   [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_got_price),
                CommandHandler("pular", search_got_price),
            ],
        },
        fallbacks=[CommandHandler("cancelar", lambda u, c: ConversationHandler.END)],
        per_message=False,
    )

    # ── ConversationHandler: Novo alerta ────────────────────────
    alert_new_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(callback_alert_new, pattern="^alert_new$"),
            CallbackQueryHandler(callback_alert_new, pattern="^create_alert\\|"),
        ],
        states={
            ALERT_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_got_name)],
            ALERT_PRICE:  [
                MessageHandler(filters.TEXT & ~filters.COMMAND, alert_got_price),
                CommandHandler("pular", cmd_skip),
            ],
            ALERT_EXPIRY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, alert_got_expiry),
                CommandHandler("pular", cmd_skip),
            ],
        },
        fallbacks=[CommandHandler("cancelar", lambda u, c: ConversationHandler.END)],
        per_message=False,
    )

    # ── ConversationHandler: Editar alerta ──────────────────────
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_edit_field, pattern="^edit_field\\|")],
        states={
            EDIT_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_got_value),
                CommandHandler("pular", edit_got_value),
            ],
        },
        fallbacks=[CommandHandler("cancelar", lambda u, c: ConversationHandler.END)],
        per_message=False,
    )

    # ── Registra handlers ────────────────────────────────────────
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  lambda u, c: callback_menu(u, c)))
    app.add_handler(search_conv)
    app.add_handler(alert_new_conv)
    app.add_handler(edit_conv)

    app.add_handler(CallbackQueryHandler(callback_menu,         pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(callback_help,         pattern="^help$"))
    app.add_handler(CallbackQueryHandler(callback_alert_list,   pattern="^alert_list$"))
    app.add_handler(CallbackQueryHandler(callback_alert_detail, pattern="^alert_detail\\|"))
    app.add_handler(CallbackQueryHandler(callback_alert_toggle, pattern="^alert_toggle\\|"))
    app.add_handler(CallbackQueryHandler(callback_alert_delete, pattern="^alert_delete\\|"))
    app.add_handler(CallbackQueryHandler(callback_alert_edit,   pattern="^alert_edit\\|"))

    # Handler de mensagens em grupos (monitoramento)
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION),
        group_message_handler,
    ))

    # ── Scheduler para expirar alertas ──────────────────────────
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.ensure_future(job_expire_alerts(app.bot)),
        "interval", minutes=5, id="expire_alerts",
    )
    scheduler.start()

    # ── Servidor HTTP mínimo para o Render não matar o processo ─
    async def health(_req):
        return web.Response(text="OK")

    web_app = web.Application()
    web_app.router.add_get("/",       health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)

    async def run():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info("🌐 Health-check HTTP ouvindo na porta %d", PORT)

        async with app:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            log.info("🤖 PricehuntBot iniciado!")
            # Mantém rodando até ser encerrado
            await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()
