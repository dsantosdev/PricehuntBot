"""
=============================================================
  PricehuntBot — Busca de produtos em grupos do Telegram
=============================================================
Variáveis de ambiente obrigatórias (configure no Render):
    BOT_TOKEN    — token do @BotFather
    DATABASE_URL — postgres://user:pass@host:5432/dbname
                   (o Render preenche automaticamente ao
                    linkar um PostgreSQL ao serviço)

Opcionais:
    PORT         — porta HTTP para health-check (padrão: 10000)
=============================================================
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import asyncpg
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
#  CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PORT         = int(os.environ.get("PORT", 10000))
# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("PricehuntBot")

pool: asyncpg.Pool = None  # type: ignore

(
    SEARCH_PRODUCT, SEARCH_PRICE,
    ALERT_NAME, ALERT_PRICE, ALERT_EXPIRY,
    EDIT_CHOOSE, EDIT_FIELD, EDIT_VALUE,
) = range(8)


# ══════════════════════════════════════════════════════════════
#  BANCO DE DADOS
# ══════════════════════════════════════════════════════════════

async def init_db():
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT  NOT NULL,
            product     TEXT    NOT NULL,
            max_price   NUMERIC,
            expires_at  TIMESTAMPTZ,
            active      BOOLEAN NOT NULL DEFAULT TRUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS scan_cache (
            chat_id     BIGINT NOT NULL,
            user_id     BIGINT NOT NULL,
            chat_title  TEXT,
            PRIMARY KEY (chat_id, user_id)
        );
    """)
    log.info("PostgreSQL OK")


# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════

def price_fmt(v):
    if v is None:
        return "—"
    return f"R$ {float(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")


def expiry_fmt(v):
    if not v:
        return "Sem expiração"
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y %H:%M")
    try:
        return datetime.fromisoformat(str(v)).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(v)


def parse_duration(text: str):
    m = re.fullmatch(r"(\d+)\s*([dhm])", text.strip().lower())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=val), "h": timedelta(hours=val), "m": timedelta(minutes=val)}[unit]
    return datetime.now().astimezone() + delta


async def get_user_groups(user_id: int):
    rows = await pool.fetch("SELECT chat_id, chat_title FROM scan_cache WHERE user_id=$1", user_id)
    return [(r["chat_id"], r["chat_title"]) for r in rows]


async def refresh_group_cache(user_id: int, chat_id: int, chat_title: str):
    await pool.execute(
        """INSERT INTO scan_cache(chat_id, user_id, chat_title) VALUES($1,$2,$3)
           ON CONFLICT (chat_id, user_id) DO UPDATE SET chat_title=EXCLUDED.chat_title""",
        chat_id, user_id, chat_title,
    )


# ══════════════════════════════════════════════════════════════
#  TECLADOS
# ══════════════════════════════════════════════════════════════

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Buscar produto nos grupos", callback_data="search")],
        [InlineKeyboardButton("🔔 Criar alerta",              callback_data="alert_new")],
        [InlineKeyboardButton("📋 Meus alertas",              callback_data="alert_list")],
        [InlineKeyboardButton("❓ Ajuda",                     callback_data="help")],
    ])


def build_results_keyboard(results, product):
    buttons = []
    for r in results[:10]:
        buttons.append([InlineKeyboardButton(f"📂 {r['chat_title']}", url=r["link"])])
    safe = product[:40]
    buttons.append([InlineKeyboardButton("🔔 Criar alerta para este produto", callback_data=f"create_alert|{safe}")])
    buttons.append([InlineKeyboardButton("🏠 Menu principal", callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def alert_list_keyboard(alerts):
    buttons = []
    for a in alerts:
        status = "✅" if a["active"] else "🔕"
        label  = f"{status} {a['product'][:25]}"
        if a["max_price"]:
            label += f" • {price_fmt(a['max_price'])}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"alert_detail|{a['id']}")])
    buttons.append([InlineKeyboardButton("➕ Novo alerta", callback_data="alert_new")])
    buttons.append([InlineKeyboardButton("🏠 Menu",        callback_data="menu")])
    return InlineKeyboardMarkup(buttons)


def alert_detail_keyboard(alert_id, active):
    toggle = "🔕 Desativar" if active else "✅ Ativar"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Editar",    callback_data=f"alert_edit|{alert_id}")],
        [InlineKeyboardButton(toggle,         callback_data=f"alert_toggle|{alert_id}")],
        [InlineKeyboardButton("🗑️ Excluir",  callback_data=f"alert_delete|{alert_id}")],
        [InlineKeyboardButton("⬅️ Voltar",   callback_data="alert_list")],
    ])


def alert_edit_keyboard(alert_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Nome do produto", callback_data=f"edit_field|{alert_id}|product")],
        [InlineKeyboardButton("💰 Preço máximo",    callback_data=f"edit_field|{alert_id}|max_price")],
        [InlineKeyboardButton("⏰ Expiração",       callback_data=f"edit_field|{alert_id}|expires_at")],
        [InlineKeyboardButton("⬅️ Voltar",          callback_data=f"alert_detail|{alert_id}")],
    ])


# ══════════════════════════════════════════════════════════════
#  HANDLERS — MENU / AJUDA
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (
        f"👋 Olá, *{user.first_name}*\\!\n\n"
        "Sou o *PricehuntBot* — monitoro produtos nos seus grupos do Telegram "
        "e aviso quando aparecerem dentro do seu orçamento\\.\n\n"
        "O que deseja fazer?"
    )
    await update.message.reply_text(
        text, parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_keyboard(),
    )


async def callback_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🏠 *Menu principal*",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_keyboard(),
    )


async def callback_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = (
        "ℹ️ *Como funciona*\n\n"
        "1\\. *Buscar produto* — digite o nome e o preço máximo opcional\\. "
        "O bot mostra atalhos para os grupos onde detectou o produto\\.\n\n"
        "2\\. *Criar alerta* — defina nome, preço máximo e validade \\(ambos opcionais\\)\\. "
        "O bot te notifica quando encontrar o produto nos grupos\\.\n\n"
        "3\\. *Meus alertas* — liste, edite, ative\\/desative ou exclua alertas\\.\n\n"
        "📌 *Como adicionar o bot a um grupo*\n"
        "Abra o grupo → toque no nome do grupo → Adicionar membros → pesquise o nome do bot\\.\n\n"
        "_Dica:_ o bot precisa ser membro do grupo para monitorar mensagens em tempo real\\."
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
        "🔍 *Busca de produto*\n\nDigite o nome \\(ou parte\\) do produto:",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return SEARCH_PRODUCT


async def search_got_product(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["search_product"] = update.message.text.strip()
    ctx.user_data["__conv_state"]   = "SEARCH_PRICE"
    await update.message.reply_text(
        f"💰 Preço máximo para _{ctx.user_data['search_product']}_?\n"
        "\\(valor ou /pular para sem limite\\)",
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
            await update.message.reply_text("⚠️ Valor inválido. Tente novamente ou /pular.")
            return SEARCH_PRICE

    uid    = update.effective_user.id
    groups = await get_user_groups(uid)

    price_txt = f" até {price_fmt(price)}" if price else ""
    header    = f"🔍 *{product}*{price_txt}\n\n"

    if not groups:
        await update.message.reply_text(
            header + "❌ Nenhum grupo encontrado\\. Adicione o bot a um grupo primeiro\\.",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]),
        )
        return ConversationHandler.END

    results = [
        {"chat_id": cid, "chat_title": title or str(cid),
         "link": f"https://t.me/c/{str(cid).replace('-100','')}/0"}
        for cid, title in groups
    ]
    lines = "\n".join(f"• {r['chat_title']}" for r in results[:10])
    await update.message.reply_text(
        header + lines,
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=build_results_keyboard(results, product),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
#  HANDLERS — CRIAR ALERTA
# ══════════════════════════════════════════════════════════════

async def callback_alert_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    product = None
    if "|" in q.data:
        product = q.data.split("|", 1)[1]
        ctx.user_data["alert_product"] = product

    if product:
        ctx.user_data["__conv_state"] = "ALERT_PRICE"
        await q.edit_message_text(
            f"💰 Alerta para *{product}*\\.\n\nPreço máximo? \\(ou /pular\\)",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )
        return ALERT_PRICE
    else:
        await q.edit_message_text(
            "🔔 *Novo alerta*\n\nDigite o nome do produto:",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )
        return ALERT_NAME


async def alert_got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["alert_product"]  = update.message.text.strip()
    ctx.user_data["__conv_state"]   = "ALERT_PRICE"
    await update.message.reply_text(
        f"💰 *{ctx.user_data['alert_product']}* — preço máximo? \\(ou /pular\\)",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return ALERT_PRICE


async def alert_got_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() not in ("/pular", "pular", "-"):
        raw = text.replace("R$","").replace(".","").replace(",",".").strip()
        try:
            ctx.user_data["alert_price"] = float(raw)
        except ValueError:
            await update.message.reply_text("⚠️ Valor inválido. Tente novamente ou /pular.")
            return ALERT_PRICE
    else:
        ctx.user_data["alert_price"] = None

    ctx.user_data["__conv_state"] = "ALERT_EXPIRY"
    await update.message.reply_text(
        "⏰ Por quanto tempo o alerta deve ficar ativo?\n"
        "Ex: `7d`, `24h`, `30m` ou /pular para sem expiração",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )
    return ALERT_EXPIRY


async def alert_got_expiry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() in ("/pular", "pular", "-"):
        return await _save_alert(update, ctx, expires_at=None)
    dt = parse_duration(text)
    if not dt:
        await update.message.reply_text(
            "⚠️ Use `7d`, `12h`, `30m` ou /pular\\.",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )
        return ALERT_EXPIRY
    return await _save_alert(update, ctx, expires_at=dt)


async def _save_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE, expires_at):
    uid     = update.effective_user.id
    product = ctx.user_data.get("alert_product", "")
    price   = ctx.user_data.get("alert_price")

    await pool.execute(
        "INSERT INTO alerts(user_id, product, max_price, expires_at) VALUES($1,$2,$3,$4)",
        uid, product, price, expires_at,
    )
    lines = [
        "✅ *Alerta criado\\!*", "",
        f"📦 Produto: `{product}`",
        f"💰 Preço máximo: {price_fmt(price)}",
        f"⏰ Expira em: {expiry_fmt(expires_at)}",
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


async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get("__conv_state", "")
    if state == "ALERT_PRICE":
        ctx.user_data["alert_price"]  = None
        ctx.user_data["__conv_state"] = "ALERT_EXPIRY"
        await update.message.reply_text(
            "⏰ Por quanto tempo o alerta deve ficar ativo?\n"
            "Ex: `7d`, `24h`, `30m` ou /pular",
            parse_mode=constants.ParseMode.MARKDOWN_V2,
        )
        return ALERT_EXPIRY
    if state == "ALERT_EXPIRY":
        return await _save_alert(update, ctx, expires_at=None)
    if state == "SEARCH_PRICE":
        return await search_got_price(update, ctx)
    await update.message.reply_text("Nada a pular aqui.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════
#  HANDLERS — LISTAR / DETALHAR / EDITAR / EXCLUIR
# ══════════════════════════════════════════════════════════════

async def callback_alert_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    alerts = await pool.fetch(
        "SELECT * FROM alerts WHERE user_id=$1 ORDER BY active DESC, created_at DESC", uid
    )
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
    a = await pool.fetchrow("SELECT * FROM alerts WHERE id=$1", alert_id)
    if not a:
        await q.edit_message_text("⚠️ Alerta não encontrado\\.", parse_mode=constants.ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar", callback_data="alert_list")]]))
        return
    status = "✅ Ativo" if a["active"] else "🔕 Inativo"
    text = (
        f"🔔 *Alerta \\#{a['id']}*\n\n"
        f"📦 Produto: `{a['product']}`\n"
        f"💰 Preço máximo: {price_fmt(a['max_price'])}\n"
        f"⏰ Expira em: {expiry_fmt(a['expires_at'])}\n"
        f"📅 Criado: {a['created_at'].strftime('%d/%m/%Y %H:%M')}\n"
        f"Status: {status}"
    )
    await q.edit_message_text(
        text, parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=alert_detail_keyboard(alert_id, bool(a["active"])),
    )


async def callback_alert_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    alert_id = int(q.data.split("|")[1])
    a = await pool.fetchrow("SELECT active FROM alerts WHERE id=$1", alert_id)
    if a:
        await pool.execute("UPDATE alerts SET active=$1 WHERE id=$2", not a["active"], alert_id)
    await q.answer("✅ Alerta atualizado!")
    q.data = f"alert_detail|{alert_id}"
    await callback_alert_detail(update, ctx)


async def callback_alert_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q        = update.callback_query
    await q.answer()
    alert_id = int(q.data.split("|")[1])
    await pool.execute("DELETE FROM alerts WHERE id=$1", alert_id)
    await q.edit_message_text(
        "🗑️ Alerta excluído\\.",
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
        f"✏️ *Editar alerta \\#{alert_id}* — o que alterar?",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
        reply_markup=alert_edit_keyboard(alert_id),
    )


async def callback_edit_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, alert_id, field = q.data.split("|")
    ctx.user_data["edit_alert_id"] = int(alert_id)
    ctx.user_data["edit_field"]    = field
    prompts = {
        "product":    "Digite o novo nome do produto:",
        "max_price":  "Novo preço máximo \\(ou /pular para remover\\):",
        "expires_at": "Nova expiração: `7d`, `12h`, `30m` \\(ou /pular para sem expiração\\):",
    }
    await q.edit_message_text(prompts[field], parse_mode=constants.ParseMode.MARKDOWN_V2)
    return EDIT_VALUE


async def edit_got_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text     = update.message.text.strip()
    alert_id = ctx.user_data.get("edit_alert_id")
    field    = ctx.user_data.get("edit_field")
    value    = None

    if field == "product":
        value = text
    elif field == "max_price":
        if text.lower() not in ("/pular", "pular", "-"):
            raw = text.replace("R$","").replace(".","").replace(",",".").strip()
            try:
                value = float(raw)
            except ValueError:
                await update.message.reply_text("⚠️ Valor inválido.")
                return EDIT_VALUE
    elif field == "expires_at":
        if text.lower() not in ("/pular", "pular", "-"):
            value = parse_duration(text)
            if not value:
                await update.message.reply_text("⚠️ Use `7d`, `12h`, `30m` ou /pular\\.", parse_mode=constants.ParseMode.MARKDOWN_V2)
                return EDIT_VALUE

    await pool.execute(f"UPDATE alerts SET {field}=$1 WHERE id=$2", value, alert_id)
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
#  MONITOR DE GRUPOS
# ══════════════════════════════════════════════════════════════

async def group_message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    if chat.type in ("group", "supergroup"):
        await refresh_group_cache(user.id, chat.id, chat.title or "")

    text = (msg.text or msg.caption or "").lower()
    if not text:
        return

    rows     = await pool.fetch("SELECT user_id FROM scan_cache WHERE chat_id=$1", chat.id)
    user_ids = [r["user_id"] for r in rows]
    if not user_ids:
        return

    alerts = await pool.fetch(
        "SELECT * FROM alerts WHERE active=TRUE AND user_id = ANY($1::bigint[])", user_ids
    )
    now = datetime.now().astimezone()

    for a in alerts:
        if a["expires_at"] and a["expires_at"] < now:
            await pool.execute("UPDATE alerts SET active=FALSE WHERE id=$1", a["id"])
            continue

        if not re.search(re.escape(a["product"]), text, re.IGNORECASE):
            continue

        price_ok = True
        if a["max_price"]:
            nums  = re.findall(r"\d+[.,]?\d*", text)
            found = []
            for n in nums:
                try:
                    found.append(float(n.replace(",",".")))
                except Exception:
                    pass
            price_ok = any(fp <= float(a["max_price"]) for fp in found) if found else True

        if not price_ok:
            continue

        try:
            link    = f"https://t.me/c/{str(chat.id).replace('-100','')}/{msg.message_id}"
            preview = (msg.text or "(mídia)")[:120]
            notif   = (
                f"🔔 *Alerta disparado\\!*\n\n"
                f"📦 Produto: `{a['product']}`\n"
                f"📂 Grupo: *{chat.title or chat.id}*\n"
                f"💬 Trecho: _{preview}_"
            )
            await ctx.bot.send_message(
                chat_id=a["user_id"],
                text=notif,
                parse_mode=constants.ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📩 Ir para a mensagem", url=link)
                ]]),
            )
        except Exception as e:
            log.warning("Erro ao notificar alerta %s: %s", a["id"], e)


# ══════════════════════════════════════════════════════════════
#  JOB — expirar alertas
# ══════════════════════════════════════════════════════════════

async def job_expire_alerts(bot: Bot):
    rows = await pool.fetch(
        """UPDATE alerts SET active=FALSE
           WHERE active=TRUE AND expires_at IS NOT NULL AND expires_at <= NOW()
           RETURNING id, user_id, product"""
    )
    for r in rows:
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
    global pool

    app = Application.builder().token(BOT_TOKEN).build()

    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(callback_search, pattern="^search$")],
        states={
            SEARCH_PRODUCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_got_product)],
            SEARCH_PRICE:   [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_got_price),
                CommandHandler("pular", cmd_skip),
            ],
        },
        fallbacks=[CommandHandler("cancelar", lambda u, c: ConversationHandler.END)],
        per_message=False,
    )

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
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION),
        group_message_handler,
    ))

    async def health(_req):
        return web.Response(text="OK")

    web_app = web.Application()
    web_app.router.add_get("/",       health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)

    async def run():
        global pool
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        await init_db()

        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("🌐 HTTP na porta %d", PORT)

        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            lambda: asyncio.ensure_future(job_expire_alerts(app.bot)),
            "interval", minutes=5, id="expire_alerts",
        )
        scheduler.start()

        async with app:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            log.info("🤖 PricehuntBot rodando!")
            await asyncio.Event().wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()