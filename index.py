# -*- coding: utf-8 -*-
"""
Bot Telegram tra cứu điểm UNETI - BẢN VERCEL SERVERLESS (webhook).

Sửa so với bản cũ (index_old_backup.py):
  - BỎ curl_cffi (thường là nguyên nhân 403/lỗi build trên Vercel) -> dùng requests thường.
    Trang UNETI KHÔNG chạy Cloudflare proxy (CF chỉ làm DNS), nên không cần giả lập TLS Chrome.
  - BỎ cookie cf_clearance / UNETI_COOKIE: vô tác dụng với site này (đã kiểm chứng
    bằng phân tích DNS/headers + test requests thường chạy thẳng 200 OK).
  - BỎ Redis (Upstash): danh sách người dùng dùng biến môi trường OWNER_ID + ALLOWED_IDS.
  - PROXY_URL (tùy chọn): nếu Vercel vẫn bị 403 do dải IP cloud bị chặn,
    trỏ request qua proxy đặt ở Việt Nam: http://user:pass@host:port hoặc socks5://...
  - VERCEL_URL tự nhận từ request -> deploy ở đâu cũng chạy, không cần sửa code.

Biến môi trường (Vercel > Settings > Environment Variables):
  BOT_TOKEN    (bắt buộc) token từ @BotFather
  OWNER_ID     (nên set)  ID Telegram của chủ bot; bỏ trống = ai cũng dùng được
  ALLOWED_IDS  (tùy chọn) "111222333,444555666" - ID được phép dùng ngoài chủ bot
  PROXY_URL    (tùy chọn) xem trên

Sau khi deploy xong, mở  https://<ten-proj>.vercel.app/api/setup  để bot tự đăng ký webhook.
"""
import io
import re
import os
import time
import threading
import unicodedata
import difflib
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image
from bs4 import BeautifulSoup
from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ForceReply

from ocr import OCR

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)
ALLOWED_IDS = {x.strip() for x in os.environ.get("ALLOWED_IDS", "").split(",") if x.strip()}
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)  # serverless: đơn luồng

# ==========================================
# Cấu hình URL & headers crawler UNETI
# ==========================================
BASE_URL = "https://sinhvien.uneti.edu.vn"
URL_GET_TOKEN = BASE_URL + "/tra-cuu-thong-tin.html"
URL_POST_LOGIN = BASE_URL + "/tra-cuu-thong-tin.html?Length=14"
URL_CAPTCHA = BASE_URL + "/WebCommon/GetCaptcha"
URL_STUDENT_INFO = BASE_URL + "/tra-cuu/thong-tin-sinh-vien-by-qr-code.html"
URL_TRANSCRIPT = BASE_URL + "/tra-cuu/ket-qua-hoc-tap.html"

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "referer": URL_GET_TOKEN,
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
}

# ==========================================
# Session riêng cho từng luồng
# ==========================================
_local = threading.local()

def create_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    if PROXY_URL:  # dự phòng: route qua proxy VN nếu dải IP Vercel bị chặn
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    return s

class _SessionProxy:
    def __getattr__(self, name):
        s = getattr(_local, "s", None)
        if s is None:
            s = _local.s = create_session()
        return getattr(s, name)

session = _SessionProxy()

# ==========================================
# Load OCR (ONNX - nhẹ, chạy được trên serverless)
# ==========================================
def _find_asset(name):
    """Dò file ở nhiều vị trí: cạnh code, thư mục cha (root khi deploy), và CWD."""
    for d in (BASE_DIR, os.path.dirname(BASE_DIR), os.getcwd()):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(BASE_DIR, name)

try:
    ocr = OCR(_find_asset("best_model.onnx"))
    print("✅ Load OCR (best_model.onnx) thành công")
except Exception as e:
    print(f"❌ Load OCR thất bại: {e}")
    ocr = None

class Blocked(Exception):
    """Bị server từ chối (403)."""

# ==========================================
# Các hàm crawler
# ==========================================
def get_captcha():
    for _ in range(10):
        try:
            r = session.get(URL_CAPTCHA, timeout=15)
            if r.status_code == 403:
                raise Blocked("Bị từ chối (403) khi lấy captcha - IP của Vercel có thể bị chặn, hãy set PROXY_URL")
            if r.status_code != 200:
                continue
            img = Image.open(io.BytesIO(r.content))
            text, ms = ocr.predict_pil(img)
            text = text.strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{4}", text):
                continue
            return text
        except Blocked:
            raise
        except Exception as e:
            print("OCR ERROR:", e)
            time.sleep(0.5)
    raise Exception("OCR captcha thất bại sau 10 lần thử")

def get_token():
    r = session.get(URL_GET_TOKEN, timeout=15)
    if r.status_code == 403:
        raise Blocked("Bị từ chối (403) khi mở trang tra cứu - IP của Vercel có thể bị chặn, hãy set PROXY_URL")
    soup = BeautifulSoup(r.text, "html.parser")
    token = soup.find("input", {"name": "__RequestVerificationToken"})
    if token is None:
        raise Exception("Không tìm thấy Token (trang trả về khác bình thường)")
    return token["value"]

def login(msv, captcha, token):
    payload = {
        "__RequestVerificationToken": token, "MaSinhVien": msv, "HoTen": "", "NgaySinh": "",
        "LopHoc": "", "SoCMND": "", "Captcha": captcha, "X-Requested-With": "XMLHttpRequest",
    }
    headers = {"content-type": "application/x-www-form-urlencoded; charset=UTF-8",
               "x-requested-with": "XMLHttpRequest", "origin": BASE_URL}
    r = session.post(URL_POST_LOGIN, data=payload, headers=headers, timeout=15)
    if r.status_code == 403:
        raise Blocked("Bị từ chối (403) khi đăng nhập")
    return r

def extract_guid(html):
    soup = BeautifulSoup(html, "html.parser")
    a = soup.find(attrs={"data-guid": True})
    return a["data-guid"] if a else None

def attempt_login(msv):
    for i in range(5):
        try:
            token = get_token()
            captcha = get_captcha()
            resp = login(msv, captcha, token)
            try:
                data = resp.json()
                if "Errors" in data and "Captcha" in data["Errors"]:
                    continue
            except Exception:
                pass
            guid = extract_guid(resp.text)
            if guid:
                return guid
        except Blocked:
            raise
        except Exception as e:
            print("LOGIN ERROR:", e)
    return None

def get_student_info(guid):
    payload = {"k": guid, "isKetQua": "true"}
    resp = session.post(URL_STUDENT_INFO, data=payload, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
    if resp.status_code == 403:
        raise Blocked("Bị từ chối (403) khi lấy thông tin sinh viên")
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    info = {}
    for row in soup.select(".form-group.row"):
        label = row.find("span")
        value = row.find("b")
        if label and value:
            info[label.get_text(strip=True).replace(":", "")] = value.get_text(" ", strip=True)
    for lb in soup.find_all("label"):
        title = lb.find("span")
        value = lb.find(class_="bold")
        if title and value:
            info[title.get_text(strip=True).replace(":", "")] = value.get_text(" ", strip=True)
    return info

def parse_semester(name, rows):
    subjects = []
    summary = {}
    for tr in rows:
        tds = tr.find_all('td')
        is_summary_row = False
        for td in tds:
            if td.get('colspan') == '2' and 'hidden' not in td.get('class', []):
                text = td.get_text(' ', strip=True)
                if ':' in text:
                    key, val = text.split(':', 1)
                    summary[key.strip()] = val.strip()
                is_summary_row = True
        if is_summary_row:
            continue
        if len(tds) >= 4:
            first_td = tds[0]
            if first_td.get('style') and 'min-width' in first_td.get('style', ''):
                sub = {'stt': first_td.get_text(strip=True)}
                if len(tds) > 1: sub['ma_lhp'] = tds[1].get_text(strip=True)
                if len(tds) > 2: sub['ten_mon'] = tds[2].get_text(strip=True)
                if len(tds) > 3: sub['tin_chi'] = tds[3].get_text(strip=True)
                for td in tds:
                    title = td.get('title')
                    if title == 'DiemTongKet1': sub['diem_tong_ket'] = td.get_text(strip=True)
                    elif title == 'DiemChu': sub['diem_chu'] = td.get_text(strip=True)
                if 'ma_lhp' in sub:
                    subjects.append(sub)
    return {'ten_dot': name, 'subjects': subjects, 'summary': summary}

def parse_transcript(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    table = soup.find('table', id='xemDiem') or soup.find('table', class_=lambda c: c and 'tableFixHead' in c)
    tbody = table.find('tbody') if table else soup.find('tbody')
    if not tbody:
        return []
    semesters = []
    current_semester = None
    current_rows = []
    for tr in tbody.find_all('tr'):
        if tr.find('td', class_='row-head'):
            if current_semester is not None:
                semesters.append(parse_semester(current_semester, current_rows))
            current_semester = tr.find('td', class_='row-head').get_text(strip=True)
            current_rows = []
            continue
        if current_semester is not None:
            current_rows.append(tr)
    if current_semester is not None:
        semesters.append(parse_semester(current_semester, current_rows))
    return semesters

def get_transcript(guid):
    url = f"{URL_TRANSCRIPT}?k={guid}"
    resp = session.get(url, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
    if resp.status_code == 403:
        raise Blocked("Bị từ chối (403) khi lấy bảng điểm")
    if resp.status_code != 200:
        return None
    return parse_transcript(resp.text)

def process_one_student(msv):
    result = {"msv": msv, "success": False, "info": None, "transcript": None, "error": ""}
    try:
        guid = attempt_login(msv)
    except Blocked as e:
        result["error"] = f"⚠️ {e}"
        return result
    if not guid:
        result["error"] = "Không lấy được GUID sau 5 lần thử (OCR captcha chưa đúng?)"
        return result
    info = get_student_info(guid)
    if not info:
        result["error"] = "Không lấy được thông tin sinh viên"
        return result
    result["success"] = True
    result["info"] = info
    result["transcript"] = get_transcript(guid)
    return result

def format_telegram_messages(result):
    if not result["success"]:
        return [f"❌ Lỗi tra cứu MSSV {result['msv']}:\n{result['error']}"]
    info, transcript = result["info"], result["transcript"]
    info_msg = (
        f"🎓 THÔNG TIN SINH VIÊN\n"
        f"▪️ MSSV: {result['msv']}\n"
        f"▪️ Họ tên: {info.get('Họ tên', info.get('Họ và tên', 'N/A'))}\n"
        f"▪️ Lớp: {info.get('Lớp học', info.get('Lớp', 'N/A'))}\n"
        f"▪️ Khoa: {info.get('Khoa', 'N/A')}\n"
        f"▪️ Chuyên ngành: {info.get('Chuyên ngành', 'N/A')}\n"
        f"▪️ Cơ sở: {info.get('Cơ sở', 'N/A')}\n\n"
        f"📊 KẾT QUẢ HỌC TẬP"
    )
    msg_list = [info_msg]
    if transcript:
        for sem in transcript:
            sem_msg = f"🗓 {sem['ten_dot']}\n"
            if sem.get('summary'):
                for key, val in sem['summary'].items():
                    sem_msg += f"{key}: {val}\n"
            sem_msg += "\nĐiểm các môn:\n"
            for sub in sem['subjects']:
                sem_msg += f" 🔹 {sub['ten_mon']}: {sub.get('diem_tong_ket', '-')}\n"
            msg_list.append(sem_msg)
    return msg_list

# ==========================================
# TRA NHIỀU MSSV + XẾP HẠNG THEO MÔN
# ==========================================
MAX_MSV = 20          # tối đa số MSSV mỗi lần (giảm so với local để an toàn trong 60s serverless)
MAX_WORKERS = 6
CACHE_TTL = 2 * 3600
_cache = {}

MSV_RE = re.compile(r'^(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5,}$')

def split_tokens(text):
    return [t for t in re.split(r'[\s,;]+', text.strip()) if t]

def is_msv_list(text):
    toks = split_tokens(text)
    return bool(toks) and all(MSV_RE.match(t) for t in toks)

def dedupe(items):
    seen, out = set(), []
    for x in items:
        k = x.upper()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out

def msvs_from_replied(replied):
    text = replied.text or ""
    if replied.from_user and replied.from_user.is_bot:
        found = []
        for line in text.splitlines():
            toks = split_tokens(line)
            if toks and MSV_RE.match(toks[0]):
                found.append(toks[0])
        return dedupe(found)
    return dedupe([t for t in split_tokens(text) if MSV_RE.match(t)])

def norm(s):
    s = unicodedata.normalize('NFD', s.replace('đ', 'd').replace('Đ', 'D'))
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9 ]+', ' ', s.lower()).split()

def to_float(x):
    try:
        return float(str(x).replace(',', '.').strip())
    except Exception:
        return None

def subject_matches(query_words, name):
    name_words = norm(name)
    return bool(query_words) and all(any(nw.startswith(qw) for nw in name_words) for qw in query_words)

def get_result_cached(msv):
    item = _cache.get(msv.upper())
    if item and time.time() - item[0] < CACHE_TTL:
        return item[1]
    try:
        res = process_one_student(msv)
    except Exception as e:
        res = {"msv": msv, "success": False, "info": None, "transcript": None, "error": str(e)}
    if res["success"]:
        _cache[msv.upper()] = (time.time(), res)
    return res

def get_results(msvs):
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        return list(ex.map(get_result_cached, msvs))

def student_name(res):
    info = res.get("info") or {}
    return info.get("Họ tên", info.get("Họ và tên", res["msv"]))

def student_class(res):
    info = res.get("info") or {}
    return info.get("Lớp học", info.get("Lớp", ""))

def button_label(r):
    if not r["success"]:
        return f"❌ {r['msv']}" if r["error"] != "chưa tra lại" else f"👤 {r['msv']}"
    parts = [r["msv"], student_name(r)]
    extra = student_class(r) or (r.get("info") or {}).get("Khoa", "")
    if extra:
        parts.append(extra)
    return " - ".join(parts)

def format_list_text(results):
    return f"📋 Đã tra {len(results)} MSSV - bấm vào tên để xem chi tiết:"

def list_markup(results):
    kb = InlineKeyboardMarkup(row_width=1)
    for r in results:
        kb.add(InlineKeyboardButton(button_label(r), callback_data=f"s:{r['msv']}"))
    kb.add(InlineKeyboardButton("📊 Check điểm theo môn", callback_data="q"))
    return kb

def cached_only(msv):
    item = _cache.get(msv.upper())
    if item:
        return item[1]
    return {"msv": msv, "success": False, "info": None, "transcript": None, "error": "chưa tra lại"}

def cached_list_results(msvs):
    return [cached_only(m) for m in msvs]

def detail_pages(res, limit=3800):
    msgs = format_telegram_messages(res)
    pages, cur = [], ""
    for m in msgs:
        if cur and len(cur) + len(m) + 2 > limit:
            pages.append(cur)
            cur = m
        else:
            cur = (cur + "\n\n" + m) if cur else m
    if cur:
        pages.append(cur)
    return [p[:4000] for p in pages] or ["Không có dữ liệu."]

def detail_markup(msv, page, total):
    kb = InlineKeyboardMarkup(row_width=3)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Trước", callback_data=f"s:{msv}:{page-1}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("Sau ▶️", callback_data=f"s:{msv}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton("⬅️ Quay lại", callback_data="b"))
    return kb

def safe_edit(chat_id, message_id, text, markup):
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
    except Exception as e:
        if "not modified" not in str(e):
            raise

def list_msvs_from_call(call):
    rt = call.message.reply_to_message
    if rt and rt.text and is_msv_list(rt.text):
        return dedupe(split_tokens(rt.text))
    return []

def rank_subject(results, query, top=None, max_groups=5):
    """Điểm môn `query` của từng sinh viên, sắp xếp cao -> thấp."""
    qwords = norm(query)
    groups = {}
    all_names = set()
    for r in results:
        if not r["success"] or not r["transcript"]:
            continue
        for sem in r["transcript"]:
            for sub in sem["subjects"]:
                name = sub.get("ten_mon", "").strip()
                all_names.add(name)
                if not subject_matches(qwords, name):
                    continue
                score = to_float(sub.get("diem_tong_ket"))
                if score is None:
                    continue
                g = groups.setdefault(name, {})
                old = g.get(r["msv"])
                if old is None or score > old[0]:  # học lại thì lấy điểm cao nhất
                    g[r["msv"]] = (score, sub.get("diem_chu", ""), student_name(r))

    if not groups:
        close = difflib.get_close_matches(" ".join(qwords), [" ".join(norm(n)) for n in all_names], n=3, cutoff=0.5)
        hint = ""
        if close:
            originals = [n for n in all_names if " ".join(norm(n)) in close]
            hint = "\nCó phải bạn muốn: " + ", ".join(originals) + "?"
        return [f"❌ Không tìm thấy môn nào khớp với \"{query}\" trong nhóm MSSV này (hoặc chưa có điểm).{hint}"]

    msgs = []
    names = sorted(groups)
    for name in names[:max_groups]:
        rows = sorted(groups[name].items(), key=lambda kv: -kv[1][0])
        if top:
            rows = rows[:top]
        lines = [f"🏆 {name} - {len(groups[name])} SV có điểm (cao → thấp)"]
        for i, (msv, (score, grade, full)) in enumerate(rows, 1):
            g = f" ({grade})" if grade else ""
            lines.append(f"{i}. {full} - {msv}: " + f"{score:.2f}".replace(".", ",") + g)
        missing = [(student_name(r) + " - " if r["success"] else "") + r["msv"] for r in results
                   if r["msv"] not in groups[name]]
        if missing:
            lines.append("\n⏳ Chưa có điểm / không học: " + "; ".join(missing))
        msgs.append("\n".join(lines))
    if len(names) > max_groups:
        msgs.append(f"ℹ️ Còn {len(names) - max_groups} môn khác cũng khớp, hãy gõ tên môn cụ thể hơn.")
    return msgs

# ==========================================
# PHÂN QUYỀN: OWNER_ID + ALLOWED_IDS (biến môi trường, không cần Redis)
# ==========================================
_notified = set()

def is_owner(uid):
    return bool(OWNER_ID) and uid == OWNER_ID

def is_allowed(uid):
    if not OWNER_ID:      # chưa set OWNER_ID -> mở cho tất cả
        return True
    return is_owner(uid) or str(uid) in ALLOWED_IDS

def deny(chat_id, user):
    bot.send_message(chat_id,
                     f"⛔ Bạn chưa được cấp quyền dùng bot.\n"
                     f"ID Telegram của bạn: {user.id}\nHãy gửi ID này cho chủ bot.")
    if OWNER_ID and user.id not in _notified:
        _notified.add(user.id)
        name = " ".join(filter(None, [user.first_name, user.last_name])) or "?"
        uname = f" (@{user.username})" if user.username else ""
        try:
            bot.send_message(OWNER_ID, f"🔔 {name}{uname} - ID {user.id} xin quyền dùng bot.\n"
                                       f"Để cho phép: thêm ID vào biến ALLOWED_IDS rồi redeploy.")
        except Exception as e:
            print("NOTIFY OWNER ERROR:", e)

# ==========================================
# TELEGRAM HANDLERS
# ==========================================
@bot.message_handler(commands=['myid'])
def cmd_myid(message):
    bot.reply_to(message, f"ID Telegram của bạn: {message.from_user.id}")

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_allowed(message.from_user.id):
        return deny(message.chat.id, message.from_user)
    bot.reply_to(message, "👋 Gửi Mã Sinh Viên (MSSV) để mình tra cứu điểm giúp nhé.")

@bot.message_handler(func=lambda message: bool(message.text))
def handle_text(message):
    if not is_allowed(message.from_user.id):
        return deny(message.chat.id, message.from_user)
    text = message.text.strip()
    try:
        if is_msv_list(text):
            msvs = dedupe(split_tokens(text))
            if len(msvs) > MAX_MSV:
                bot.send_message(message.chat.id, f"⚠️ Tối đa {MAX_MSV} MSSV mỗi lần, mình chỉ tra {MAX_MSV} MSSV đầu.")
                msvs = msvs[:MAX_MSV]
            status_msg = bot.send_message(message.chat.id, f"🔍 Đang tra cứu {len(msvs)} MSSV...")
            results = get_results(msvs)
            try:
                bot.delete_message(message.chat.id, status_msg.message_id)
            except Exception:
                pass
            if len(results) == 1:
                for msg in format_telegram_messages(results[0]):
                    bot.send_message(message.chat.id, msg)
            else:
                bot.reply_to(message, format_list_text(results), reply_markup=list_markup(results))
            return

        if message.reply_to_message and message.reply_to_message.text:
            msvs = msvs_from_replied(message.reply_to_message)
            if not msvs:
                bot.send_message(message.chat.id, "⚠️ Tin nhắn bạn reply không có MSSV nào.")
                return
            msvs = msvs[:MAX_MSV]
            status_msg = bot.send_message(message.chat.id, f"🔍 Đang xếp hạng môn \"{text}\" trong {len(msvs)} MSSV...")
            results = get_results(msvs)
            try:
                bot.delete_message(message.chat.id, status_msg.message_id)
            except Exception:
                pass
            for msg in rank_subject(results, text):
                bot.send_message(message.chat.id, msg)
            return

        bot.send_message(message.chat.id, "Gửi 1 hoặc nhiều MSSV (mỗi MSSV 1 dòng), rồi reply tin nhắn đó kèm tên môn để xếp hạng.")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Lỗi: {str(e)}")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data or ""
    chat_id, mid = call.message.chat.id, call.message.message_id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if not is_allowed(call.from_user.id):
        return
    try:
        if data.startswith("s:"):
            parts = data.split(":")
            msv = parts[1]
            page = int(parts[2]) if len(parts) > 2 else 0
            res = get_result_cached(msv)
            pages = detail_pages(res)
            page = max(0, min(page, len(pages) - 1))
            text = pages[page] + (f"\n\n📄 Trang {page+1}/{len(pages)}" if len(pages) > 1 else "")
            safe_edit(chat_id, mid, text, detail_markup(msv, page, len(pages)))

        elif data == "b":
            msvs = list_msvs_from_call(call)
            if not msvs:
                bot.send_message(chat_id, "⚠️ Không khôi phục được danh sách, hãy gửi lại các MSSV.")
                return
            results = cached_list_results(msvs)
            safe_edit(chat_id, mid, format_list_text(results), list_markup(results))

        elif data == "q":
            msvs = list_msvs_from_call(call)
            if not msvs:
                bot.send_message(chat_id, "⚠️ Không khôi phục được danh sách, hãy gửi lại các MSSV.")
                return
            prompt = "✏️ Nhập tên môn muốn xem điểm (vd: CAD, tiếng anh)\n\nÁp dụng cho:\n" + "\n".join(msvs)
            bot.send_message(chat_id, prompt, reply_markup=ForceReply(input_field_placeholder="Tên môn..."))
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        bot.send_message(chat_id, f"❌ Lỗi: {str(e)}")

# ==========================================
# FLASK WEBHOOK (VERCEL)
# ==========================================
app = Flask(__name__)

@app.route('/api/index', methods=['POST'])
@app.route('/index', methods=['POST'])
def webhook():
    try:
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    except Exception:
        import traceback
        print(traceback.format_exc())
        return '', 500

@app.route('/api/setup')
def setup_webhook():
    # Tự nhận URL từ request -> deploy ở đâu cũng chạy, không cần sửa code sau deploy
    base = request.url_root.replace("http://", "https://").rstrip("/")
    webhook_url = f"{base}/api/index"
    if bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"]):
        return f"✅ Setup Webhook thành công với URL: {webhook_url}", 200
    return "❌ Setup Webhook thất bại (kiểm tra BOT_TOKEN)!", 500

@app.route('/')
def home():
    return "Bot Telegram UNETI đang chạy trên Vercel!"
