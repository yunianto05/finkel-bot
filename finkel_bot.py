"""
ZaQi Bot — Asisten Keuangan Keluarga via Telegram
=====================================================
Dependensi:
    pip install python-telegram-bot==20.7 gspread google-auth

Cara pakai:
    1. Buat bot baru via @BotFather di Telegram → dapat TOKEN
    2. Isi BOT_TOKEN dan CHAT_ID_ALLOWED di bagian Config
    3. (Opsional) Setup Google Sheets → isi GOOGLE_SHEET_ID & credentials
    4. Jalankan: python finkel_bot.py
"""

import logging
import json
import re
import os
from datetime import datetime, date, timedelta
from collections import defaultdict
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID_ALLOWED: list[int] = []                   # kosongkan = semua boleh, isi = whitelist

# Google Sheets (opsional — isi jika ingin auto-sync)
GOOGLE_SHEET_ID = ""                              # ID dari URL spreadsheet
GOOGLE_CREDENTIALS_FILE = "credentials.json"     # file JSON service account

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── In-memory storage (ganti dengan DB untuk production) ─────────────────────

transactions: list[dict] = []   # [{tanggal, deskripsi, kategori, jenis, nominal}]

# ─── Helpers ──────────────────────────────────────────────────────────────────

SEPARATOR  = "━━━━━━━━━━━━━━━━"
THIN_SEP   = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"

CATEGORY_EMOJI = {
    "Makanan":      "🍔",
    "Transportasi": "⛽",
    "Tagihan":      "⚡",
    "Hiburan":      "🎬",
    "Tabungan":     "💳",
    "Pendapatan":   "💰",
    "Lainnya":      "📦",
}

TYPO_MAP = {
    "mkn": "makan", "bsn": "bensin", "lst": "listrik",
    "trnsport": "transportasi", "blnja": "belanja",
    "ntn": "nonton", "minum": "minum", "krdt": "kredit",
}

KEYWORD_CATEGORY = {
    "makan":   "Makanan",  "minum": "Makanan",  "warung": "Makanan",
    "bakso":   "Makanan",  "nasi":  "Makanan",  "kopi":   "Makanan",
    "bensin":  "Transportasi", "ojek": "Transportasi", "grab": "Transportasi",
    "bus":     "Transportasi", "kereta": "Transportasi",
    "listrik": "Tagihan",  "air":   "Tagihan",  "wifi":   "Tagihan",
    "pulsa":   "Tagihan",  "token": "Tagihan",  "bpjs":   "Tagihan",
    "nonton":  "Hiburan",  "bioskop": "Hiburan", "game": "Hiburan",
    "netflix": "Hiburan",  "spotify": "Hiburan",
    "nabung":  "Tabungan", "tabung": "Tabungan", "investasi": "Tabungan",
    "gaji":    "Pendapatan", "bonus": "Pendapatan", "transfer": "Pendapatan",
    "freelance": "Pendapatan",
}


def normalize_text(text: str) -> str:
    """Koreksi typo umum."""
    words = text.lower().split()
    return " ".join(TYPO_MAP.get(w, w) for w in words)


def parse_nominal(text: str) -> Optional[int]:
    """Ubah '50rb', '2juta', '1.5jt' → angka integer."""
    text = text.lower().replace(".", "").replace(",", "")
    # format: angka + satuan
    m = re.search(r"(\d+(?:\.\d+)?)\s*(rb|ribu|k|jt|juta|m|miliar)?", text)
    if not m:
        return None
    angka = float(m.group(1))
    satuan = m.group(2) or ""
    if satuan in ("rb", "ribu", "k"):
        angka *= 1_000
    elif satuan in ("jt", "juta"):
        angka *= 1_000_000
    elif satuan in ("m", "miliar"):
        angka *= 1_000_000_000
    return int(angka)


def detect_category(text: str) -> str:
    text_lower = text.lower()
    for keyword, cat in KEYWORD_CATEGORY.items():
        if keyword in text_lower:
            return cat
    return "Lainnya"


def detect_jenis(text: str, kategori: str) -> str:
    if kategori == "Pendapatan":
        return "Masuk"
    masuk_keywords = ["masuk", "terima", "dapat", "gaji", "bonus", "income"]
    if any(k in text.lower() for k in masuk_keywords):
        return "Masuk"
    return "Keluar"


def parse_transaction(raw: str) -> Optional[dict]:
    """Parse input bebas → dict transaksi. Return None jika gagal."""
    text = normalize_text(raw)
    nominal = parse_nominal(text)
    if not nominal:
        return None
    # Deskripsi: ambil teks sebelum nominal
    deskripsi = re.sub(r"\d+(?:\.\d+)?\s*(rb|ribu|k|jt|juta|m|miliar)?", "", text).strip()
    deskripsi = re.sub(r"\s+", " ", deskripsi).strip().title() or "Transaksi"
    kategori = detect_category(text)
    jenis    = detect_jenis(text, kategori)
    return {
        "tanggal":  date.today().isoformat(),
        "deskripsi": deskripsi,
        "kategori":  kategori,
        "jenis":     jenis,
        "nominal":   nominal,
    }


def format_rp(n: int) -> str:
    return f"Rp{n:,.0f}".replace(",", ".")


def get_saldo() -> tuple[int, int, int]:
    masuk  = sum(t["nominal"] for t in transactions if t["jenis"] == "Masuk")
    keluar = sum(t["nominal"] for t in transactions if t["jenis"] == "Keluar")
    return masuk, keluar, masuk - keluar


def get_top_categories(trx_list: list[dict], n: int = 4) -> list[tuple[str, int]]:
    totals: dict[str, int] = defaultdict(int)
    for t in trx_list:
        if t["jenis"] == "Keluar":
            totals[t["kategori"]] += t["nominal"]
    return sorted(totals.items(), key=lambda x: x[1], reverse=True)[:n]


def progress_bar(pct: int, length: int = 10) -> str:
    filled = round(pct / 100 * length)
    return "█" * max(0, filled) + "░" * max(0, length - filled)


def status_emoji(keluar: int, masuk: int) -> str:
    if masuk == 0:
        return "⚪"
    r = keluar / masuk
    if r < 0.5:  return "🟢"
    if r < 0.8:  return "🟡"
    return "🔴"


# ─── Format pesan Telegram ─────────────────────────────────────────────────────

def build_confirm_message(trx: dict, masuk: int, keluar: int, saldo: int) -> str:
    emoji_cat = CATEGORY_EMOJI.get(trx["kategori"], "📦")
    arrow = "📥" if trx["jenis"] == "Masuk" else "📤"
    sign  = "+" if trx["jenis"] == "Masuk" else "-"
    return (
        f"📌 *Transaksi dicatat!*\n"
        f"{SEPARATOR}\n"
        f"{arrow} *{trx['deskripsi']}*\n"
        f"💵 Nominal: `{sign}{format_rp(trx['nominal'])}`\n"
        f"{emoji_cat} Kategori: {trx['kategori']}\n"
        f"📅 Tanggal: {trx['tanggal']}\n"
        f"{SEPARATOR}\n"
        f"💰 Pemasukan:   `{format_rp(masuk)}`\n"
        f"💸 Pengeluaran: `{format_rp(keluar)}`\n"
        f"🏦 *Saldo:* `{format_rp(saldo)}`\n"
        f"{SEPARATOR}\n"
        f"_Ketik /laporan untuk ringkasan lengkap_"
    )


def build_report(period: str = "bulanan", name: str = "Keluarga") -> str:
    today = date.today()
    if period == "harian":
        trx = [t for t in transactions if t["tanggal"] == today.isoformat()]
        label = f"Harian · {today.strftime('%d %B %Y')}"
        header_emoji = "📋"
    elif period == "mingguan":
        week_ago = today - timedelta(days=7)
        trx = [t for t in transactions if t["tanggal"] >= week_ago.isoformat()]
        label = f"Mingguan · s/d {today.strftime('%d %B %Y')}"
        header_emoji = "📆"
    else:
        trx = [t for t in transactions
               if t["tanggal"][:7] == today.strftime("%Y-%m")]
        label = f"Bulanan · {today.strftime('%B %Y')}"
        header_emoji = "📊"

    masuk  = sum(t["nominal"] for t in trx if t["jenis"] == "Masuk")
    keluar = sum(t["nominal"] for t in trx if t["jenis"] == "Keluar")
    saldo_periode = masuk - keluar
    _, _, saldo_total = get_saldo()

    top_cats = get_top_categories(trx)
    rank_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]

    spend_pct = round(keluar / masuk * 100) if masuk else 0
    hemat_pct = 100 - spend_pct

    lines = [
        f"{header_emoji} *LAPORAN {period.upper()}*",
        SEPARATOR,
        f"📅 {label}",
        f"👨‍👩‍👧 {name}",
        "",
        SEPARATOR,
        f"💰 *Pemasukan*    `{format_rp(masuk)}`",
        f"💸 *Pengeluaran*  `{format_rp(keluar)}`",
        THIN_SEP,
        f"🏦 *Saldo periode* `{format_rp(saldo_periode)}`",
        f"💎 *Saldo total*   `{format_rp(saldo_total)}`",
        "",
        f"{status_emoji(keluar, masuk)} Status: {'Keuangan sehat! 🎉' if spend_pct < 50 else 'Perhatikan pengeluaran ⚠️' if spend_pct < 80 else 'Melebihi batas aman ❗'}",
    ]

    if top_cats:
        lines += [
            "",
            SEPARATOR,
            "📌 *Top Pengeluaran*",
        ]
        for i, (cat, amt) in enumerate(top_cats):
            emoji = CATEGORY_EMOJI.get(cat, "📦")
            lines.append(f"{rank_emojis[i]} {emoji} {cat}: `{format_rp(amt)}`")

    if masuk > 0:
        lines += [
            "",
            SEPARATOR,
            "📊 *Rasio Pengeluaran*",
            f"`{progress_bar(spend_pct)}` {spend_pct}%",
            f"💚 Tingkat hemat: *{hemat_pct}%*",
        ]

    lines += [
        "",
        SEPARATOR,
        f"🤖 _ZaQi Bot · Otomatis dicatat_",
        f"⏱ {datetime.now().strftime('%H:%M')} WIB",
    ]

    return "\n".join(lines)


def build_history_message(limit: int = 10) -> str:
    if not transactions:
        return "📭 Belum ada transaksi tercatat."
    recent = transactions[-limit:][::-1]
    lines = [f"📜 *Riwayat {limit} Transaksi Terakhir*", SEPARATOR]
    for t in recent:
        arrow = "📥" if t["jenis"] == "Masuk" else "📤"
        sign  = "+" if t["jenis"] == "Masuk" else "-"
        emoji = CATEGORY_EMOJI.get(t["kategori"], "📦")
        lines.append(
            f"{arrow} {emoji} *{t['deskripsi']}*\n"
            f"   `{sign}{format_rp(t['nominal'])}` · {t['tanggal']}"
        )
        lines.append(THIN_SEP)
    _, _, saldo = get_saldo()
    lines.append(f"🏦 *Saldo saat ini:* `{format_rp(saldo)}`")
    return "\n".join(lines)


# ─── Google Sheets sync (opsional) ────────────────────────────────────────────

def sync_to_gsheet(trx: dict) -> bool:
    """Append satu baris transaksi ke Google Sheets. Return True jika berhasil."""
    if not GOOGLE_SHEET_ID:
        return False
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scope)
        gc    = gspread.authorize(creds)
        sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
        sheet.append_row([
            trx["tanggal"],
            trx["deskripsi"],
            trx["kategori"],
            trx["jenis"],
            trx["nominal"],
        ])
        return True
    except Exception as e:
        logger.error(f"GSheets sync error: {e}")
        return False


# ─── Telegram handlers ────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    if not CHAT_ID_ALLOWED:
        return True
    return update.effective_chat.id in CHAT_ID_ALLOWED


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📊 Laporan Harian"),  KeyboardButton("📆 Laporan Mingguan")],
        [KeyboardButton("📋 Laporan Bulanan"), KeyboardButton("📜 Riwayat")],
        [KeyboardButton("❓ Bantuan")],
    ],
    resize_keyboard=True,
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (
        "👋 *Halo! Selamat datang di ZaQi Bot!*\n"
        f"{SEPARATOR}\n"
        "Saya siap membantu mencatat keuangan keluarga kamu.\n\n"
        "Cukup ketik transaksi dengan bahasa bebas:\n"
        "• `beli makan siang 25rb`\n"
        "• `gaji masuk 5 juta`\n"
        "• `bayar token listrik 200rb`\n\n"
        f"{SEPARATOR}\n"
        "📌 *Perintah tersedia:*\n"
        "/laporan — ringkasan keuangan\n"
        "/harian — laporan hari ini\n"
        "/mingguan — laporan 7 hari\n"
        "/bulanan — laporan bulan ini\n"
        "/riwayat — 10 transaksi terakhir\n"
        "/export — ekspor data JSON\n"
        "/reset — hapus semua data\n"
        "/bantuan — panduan lengkap"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=MAIN_KEYBOARD)


async def cmd_bantuan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (
        "❓ *Panduan ZaQi Bot*\n"
        f"{SEPARATOR}\n"
        "📝 *Format input transaksi:*\n"
        "`[deskripsi] [nominal]`\n\n"
        "*Contoh valid:*\n"
        "• `makan bakso 15rb`\n"
        "• `bensin motor 50000`\n"
        "• `gaji masuk 4.5jt`\n"
        "• `bayar listrik 250rb`\n"
        "• `nonton netflix 54rb`\n\n"
        "*Singkatan nominal:*\n"
        "`rb / ribu / k` → ×1.000\n"
        "`jt / juta` → ×1.000.000\n\n"
        f"{SEPARATOR}\n"
        "*Kategori otomatis:*\n"
        "🍔 Makanan · ⛽ Transportasi · ⚡ Tagihan\n"
        "🎬 Hiburan · 💳 Tabungan · 💰 Pendapatan\n"
        "📦 Lainnya"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_laporan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = build_report("bulanan")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_harian(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(build_report("harian"), parse_mode=ParseMode.MARKDOWN)


async def cmd_mingguan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(build_report("mingguan"), parse_mode=ParseMode.MARKDOWN)


async def cmd_bulanan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(build_report("bulanan"), parse_mode=ParseMode.MARKDOWN)


async def cmd_riwayat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(build_history_message(), parse_mode=ParseMode.MARKDOWN)


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not transactions:
        await update.message.reply_text("📭 Belum ada data untuk diekspor.")
        return
    data = json.dumps(transactions, ensure_ascii=False, indent=2)
    filename = f"finkel_export_{date.today().isoformat()}.json"
    await update.message.reply_document(
        document=data.encode("utf-8"),
        filename=filename,
        caption=f"📤 Ekspor {len(transactions)} transaksi · {date.today().strftime('%d %B %Y')}",
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    transactions.clear()
    await update.message.reply_text(
        "🗑 Semua data transaksi telah dihapus.\nMulai pencatatan baru kapan saja!",
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = update.message.text.strip()

    # Tombol keyboard pintas
    shortcuts = {
        "📊 Laporan Harian":   cmd_harian,
        "📆 Laporan Mingguan": cmd_mingguan,
        "📋 Laporan Bulanan":  cmd_bulanan,
        "📜 Riwayat":          cmd_riwayat,
        "❓ Bantuan":          cmd_bantuan,
    }
    if text in shortcuts:
        await shortcuts[text](update, ctx)
        return

    # Kata kunci laporan
    if any(k in text.lower() for k in ["laporan", "ringkasan", "summary", "rekap"]):
        period = "bulanan"
        if "hari" in text.lower():   period = "harian"
        if "minggu" in text.lower(): period = "mingguan"
        await update.message.reply_text(build_report(period), parse_mode=ParseMode.MARKDOWN)
        return

    # Coba parse sebagai transaksi
    trx = parse_transaction(text)
    if trx:
        transactions.append(trx)
        masuk, keluar, saldo = get_saldo()

        # Sync ke Google Sheets (jika dikonfigurasi)
        synced = sync_to_gsheet(trx)
        msg = build_confirm_message(trx, masuk, keluar, saldo)
        if synced:
            msg += "\n✅ _Tersimpan ke Google Sheets_"

        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            "🤔 Hmm, saya kurang paham maksudnya.\n\n"
            "Coba format: *deskripsi + nominal*\n"
            "Contoh: `makan siang 25rb` atau `gaji masuk 5jt`\n\n"
            "Ketik /bantuan untuk panduan lengkap.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        logger.error("⚠️  Harap isi BOT_TOKEN terlebih dahulu di bagian Config!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("bantuan",  cmd_bantuan))
    app.add_handler(CommandHandler("laporan",  cmd_laporan))
    app.add_handler(CommandHandler("harian",   cmd_harian))
    app.add_handler(CommandHandler("mingguan", cmd_mingguan))
    app.add_handler(CommandHandler("bulanan",  cmd_bulanan))
    app.add_handler(CommandHandler("riwayat",  cmd_riwayat))
    app.add_handler(CommandHandler("export",   cmd_export))
    app.add_handler(CommandHandler("reset",    cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 ZaQi Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
