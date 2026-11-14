import sys
import customtkinter as ctk
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk, ImageFilter, ImageFont, ImageChops
from io import BytesIO
import json, os, math, base64, numpy as np, threading, queue, time, uuid, copy, shutil, hashlib, re, difflib
from datetime import datetime, date, timedelta
from collections import Counter

# ── X-ray folder watcher (watchdog) ─────────────────────────────────────────
try:
    from watchdog.observers import Observer as _WatchdogObserver
    from watchdog.events import FileSystemEventHandler as _FSEventHandler
    _WATCHDOG_OK = True
except ImportError:
    _WATCHDOG_OK = False
    class _FSEventHandler:          # no-op stub so _XRayFileHandler can inherit
        pass

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ── BlueX Futuristic Theme (tünd/dark) ────────────────────────────────────────
TH = {
    "bg":           "#0E1420",   # tünd göy-qara uygulama zemini
    "panel":        "#1A2233",   # slate kartlar
    "panel_soft":   "#161D2B",   # bir az daha tünd slate kart
    "sidebar":      "#081527",   # koyu lacivert kenar çubuğu
    "sidebar_row":  "#102640",   # pasif hasta satırı
    "sidebar_sub":  "#0C1E36",   # vaka akordeon zemini
    "sidebar_row2": "#14304F",   # pasif vaka satırı
    "accent":       "#2F7FF2",   # elektrik mavisi
    "accent_hover": "#1D4ED8",
    "accent_soft":  "#24344C",
    "txt":          "#E8EDF5",   # açıq boz-ağ metin
    "txt_sub":      "#8FA3C2",   # mavi-gri
    "txt_faint":    "#5E7093",
    "ok":           "#1BB281",   # teal (onay / müalicə)
    "ok_hover":     "#0B7A55",
    "indigo":       "#6366F1",
    "indigo_hover": "#4338CA",
    "danger_bg":    "#3A1F24",   # tünd fonda "Sil" düyməsi
    "danger_text":  "#FF8A8A",
    "danger_hover": "#4A262C",
}

def tint(hex_color, f=0.86):
    """Rengi beyaza doğru açar (chip zemini için). f: beyaz oranı 0-1."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    r = int(r + (255 - r) * f); g = int(g + (255 - g) * f); b = int(b + (255 - b) * f)
    return f"#{r:02x}{g:02x}{b:02x}"

def _hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
if getattr(sys, 'frozen', False):
    # PyInstaller EXE — write to a stable user-writable location
    DATA_FILE = os.path.join(os.environ.get('APPDATA', SCRIPT_DIR), 'BlueX', 'hastalar.json')
else:
    DATA_FILE = os.path.join(SCRIPT_DIR, 'hastalar.json')
XRAY_WATCH_DIR = r"E:\DRImage"   # varsayılan — Ayarlar'dan değiştirilebilir
XRAY_PUSH_JSON = r"C:\BlueX\xray_push.json"
XRAY_VR_CURRENT_JPG = r"C:\BlueX\xray_vr_current.jpg"  # decoded from rontgenler[i]["data"] — always exists on disk, unlike the original scanned file
VR_LOADOUT_FILE = r"C:\BlueX\vr_loadout.json"
XRAY_HTTP_PORT = 8091  # Quest XRay Viewer app fetches GET /xray/latest.jpg + /xray/meta.json from here

# In-memory cache the embedded HTTP server serves from — updated directly by
# _push_vr_xray_by_index, so the Quest app sees a new röntgen the moment it's
# selected (no separate xray_server.py / file-polling process needed anymore).
_XRAY_HTTP_LOCK = threading.Lock()
_XRAY_HTTP_JPEG = None
_XRAY_HTTP_META = {"patient_name": "", "timestamp": ""}


def _start_xray_http_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path == "/xray/latest.jpg":
                with _XRAY_HTTP_LOCK:
                    jpeg = _XRAY_HTTP_JPEG
                if jpeg is None:
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.send_header("Cache-Control", "no-cache, private")
                self.end_headers()
                self.wfile.write(jpeg)
            elif self.path == "/xray/meta.json":
                with _XRAY_HTTP_LOCK:
                    meta = dict(_XRAY_HTTP_META)
                body = json.dumps(meta, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    def _serve():
        try:
            ThreadingHTTPServer(("0.0.0.0", XRAY_HTTP_PORT), _Handler).serve_forever()
        except Exception as e:
            print(f"[XRayHTTP] server error: {e}")

    threading.Thread(target=_serve, daemon=True).start()
    print(f"[XRayHTTP] :{XRAY_HTTP_PORT} — /xray/latest.jpg, /xray/meta.json")

# uygulama simgesi (exe'de PyInstaller datas ile gelir, dev'de script dizininde)
ICON_FILE = os.path.join(getattr(sys, "_MEIPASS", SCRIPT_DIR), "app.ico")
AVATAR_FILE = os.path.join(getattr(sys, "_MEIPASS", SCRIPT_DIR), "avatar_robot.png")

# ── Ayarlar (bilgisayara özel — röntgen klasörü vb.) ─────────────────────────
AYAR_FILE = os.path.join(os.path.dirname(DATA_FILE), "ayarlar.json")

def ayar_yukle():
    try:
        with open(AYAR_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def ayar_kaydet(a):
    try:
        os.makedirs(os.path.dirname(AYAR_FILE) or ".", exist_ok=True)
        with open(AYAR_FILE, "w", encoding="utf-8") as f:
            json.dump(a, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Ayar] kaydedilemedi: {e}")

AYARLAR = ayar_yukle()

# ── Randevu Təqvimi (hastalardan bağımsız, ayrı dosya) ───────────────────────
RANDEVU_FILE = os.path.join(os.path.dirname(DATA_FILE), "randevular.json")

def randevu_yukle():
    try:
        with open(RANDEVU_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def randevu_kaydet(r):
    try:
        os.makedirs(os.path.dirname(RANDEVU_FILE) or ".", exist_ok=True)
        with open(RANDEVU_FILE, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Randevu] kaydedilemedi: {e}")

RANDEVU_STATUS = [
    ("gozleyir", "Gözləyir",     "#2563EB"),
    ("geldi",    "Gəldi",        "#0E9F6E"),
    ("gelmedi",  "Gəlmədi",      "#C0392B"),
    ("legv",     "Ləğv edildi",  "#8A94A6"),
]
RANDEVU_STATUS_LBL = {k: l for k, l, _ in RANDEVU_STATUS}
RANDEVU_STATUS_RENK = {k: c for k, _, c in RANDEVU_STATUS}
RANDEVU_STATUS_KEY = {l: k for k, l, _ in RANDEVU_STATUS}

RANDEVU_SAAT_BASI = 8     # təqvim cədvəli 08:00-dan başlayır
RANDEVU_SAAT_SONU = 22    # ...22:00-a qədər
RANDEVU_SAAT_PX   = 50    # bir saatlıq sətrin piksel hündürlüyü
RANDEVU_MUDDETLER = [15, 30, 45, 60, 90, 120]   # dəqiqə seçimləri

def randevu_renk(status_key):
    """İstifadəçi Ayarlar'dan rəngi dəyişibsə onu, yoxsa defolt rəngi qaytarır."""
    ozel = AYARLAR.get("randevu_renkler", {})
    return ozel.get(status_key) or RANDEVU_STATUS_RENK.get(status_key, "#2563EB")

# ── Dil paketi (EN / RU / TR) — Azərbaycan mətni əsas götürülür ─────────────
CURRENT_LANG = AYARLAR.get("dil", "az")

TRANSLATIONS = {
    "+  Yeni Pasient": {"en": "+  New Patient", "ru": "+  Новый пациент", "tr": "+  Yeni Hasta"},
    "+ Foto Əlavə Et": {"en": "+ Add Photo", "ru": "+ Добавить фото", "tr": "+ Foto Ekle"},
    "+ Qeyd Əlavə Et": {"en": "+ Add Note", "ru": "+ Добавить запись", "tr": "+ Not Ekle"},
    "+ Rentgen Əlavə Et": {"en": "+ Add X-Ray", "ru": "+ Добавить рентген", "tr": "+ Röntgen Ekle"},
    "+ Yeni Randevu": {"en": "+ New Appointment", "ru": "+ Новая запись", "tr": "+ Yeni Randevu"},
    "Ada görə axtar…": {"en": "Search by name…", "ru": "Поиск по имени…", "tr": "İsme göre ara…"},
    "Ayarlar": {"en": "Settings", "ru": "Настройки", "tr": "Ayarlar"},
    "BULUDDAKI HASTALAR": {"en": "PATIENTS IN CLOUD", "ru": "ПАЦИЕНТЫ В ОБЛАКЕ", "tr": "BULUTTAKİ HASTALAR"},
    "Bağlantı Hatası": {"en": "Connection Error", "ru": "Ошибка подключения", "tr": "Bağlantı Hatası"},
    "Bu Həftə": {"en": "This Week", "ru": "Эта неделя", "tr": "Bu Hafta"},
    "Bu fotoğrafı silmek istədiyinizə əminsiniz?": {"en": "Are you sure you want to delete this photo?", "ru": "Вы уверены, что хотите удалить это фото?", "tr": "Bu fotoğrafı silmek istediğinize emin misiniz?"},
    "Bu geçerli bir .bxd hasta dosyası değil.": {"en": "This is not a valid .bxd patient file.", "ru": "Это недопустимый файл пациента .bxd.", "tr": "Bu geçerli bir .bxd hasta dosyası değil."},
    "Bu randevunu silmək istədiyinizə əminsiniz?": {"en": "Are you sure you want to delete this appointment?", "ru": "Вы уверены, что хотите удалить эту запись?", "tr": "Bu randevuyu silmek istediğinize emin misiniz?"},
    "Bu röntgeni silmek istədiyinizə əminsiniz?": {"en": "Are you sure you want to delete this X-ray?", "ru": "Вы уверены, что хотите удалить этот снимок?", "tr": "Bu röntgeni silmek istediğinize emin misiniz?"},
    "Bu xəstə gəldi kimi qeyd edilsin?": {"en": "Mark this patient as arrived?", "ru": "Отметить пациента как прибывшего?", "tr": "Bu hasta geldi olarak işaretlensin mi?"},
    "Bu dişdəki qeyd olunmuş müalicəni geri almaq/dəyişmək istədiyinizə əminsiniz?": {"en": "Are you sure you want to undo/change the recorded treatment on this tooth?", "ru": "Вы уверены, что хотите отменить/изменить записанное лечение этого зуба?", "tr": "Bu dişteki kayıtlı tedaviyi geri almak/değiştirmek istediğinize emin misiniz?"},
    "Bulud": {"en": "Cloud", "ru": "Облако", "tr": "Bulut"},
    "Bulud sunucusu hələ qurulmayıb — proqram yalnız yerel işləyir.": {"en": "Cloud server not set up yet — the app only works locally.", "ru": "Облачный сервер ещё не настроен — программа работает только локально.", "tr": "Bulut sunucusu henüz kurulmamış — program yalnızca yerel çalışıyor."},
    "Buludda hasta tapılmadı.": {"en": "No patient found in the cloud.", "ru": "Пациент не найден в облаке.", "tr": "Bulutta hasta bulunamadı."},
    "Buluddakı verilər yerel verilərin üzərinə yazılacaq (hər iki tərəfdə eyni hasta varsa buludtakı qalacaq).\n\nDavam edilsin?": {
        "en": "Cloud data will overwrite local data (if the same patient exists on both sides, the cloud version stays).\n\nContinue?",
        "ru": "Облачные данные перезапишут локальные (если пациент есть с обеих сторон, останется облачная версия).\n\nПродолжить?",
        "tr": "Buluttaki veriler yerel verilerin üzerine yazılacak (her iki tarafta aynı hasta varsa buluttaki kalacak).\n\nDevam edilsin mi?"},
    "Buluddan Hasta Seç": {"en": "Select Patient from Cloud", "ru": "Выбрать пациента из облака", "tr": "Buluttan Hasta Seç"},
    "Daha Sonra": {"en": "Later", "ru": "Позже", "tr": "Daha Sonra"},
    "Defolt Rənglərə Sıfırla": {"en": "Reset to Default Colors", "ru": "Сбросить к цветам по умолчанию", "tr": "Varsayılan Renklere Sıfırla"},
    "Dental Tedavi Planlama": {"en": "Dental Treatment Planning", "ru": "Планирование стоматологического лечения", "tr": "Dental Tedavi Planlama"},
    "E-poçt": {"en": "Email", "ru": "Эл. почта", "tr": "E-posta"},
    "Foto yoxdur": {"en": "No photo", "ru": "Нет фото", "tr": "Foto yok"},
    "Geçersiz Dosya": {"en": "Invalid File", "ru": "Недопустимый файл", "tr": "Geçersiz Dosya"},
    "Giriş Et": {"en": "Log In", "ru": "Войти", "tr": "Giriş Yap"},
    "Güncelleme Kontrolü": {"en": "Update Check", "ru": "Проверка обновлений", "tr": "Güncelleme Kontrolü"},
    "HASTA": {"en": "PATIENT", "ru": "ПАЦИЕНТ", "tr": "HASTA"},
    "Haqqında": {"en": "About", "ru": "О программе", "tr": "Hakkında"},
    "Hastayı Sil": {"en": "Delete Patient", "ru": "Удалить пациента", "tr": "Hastayı Sil"},
    "Hastayı Kaydet": {"en": "Save Patient", "ru": "Сохранить пациента", "tr": "Hastayı Kaydet"},
    "Hata": {"en": "Error", "ru": "Ошибка", "tr": "Hata"},
    "Header-də \"Tedaviyə Keç / VR Düzenle / Layout Kaydet\" düymələrini göstər": {
        "en": "Show \"Go to Treatment / Edit VR / Save Layout\" buttons in the header",
        "ru": "Показывать кнопки «Перейти к лечению / Редактор VR / Сохранить макет» в шапке",
        "tr": "Header'da \"Tedaviye Geç / VR Düzenle / Layout Kaydet\" butonlarını göster"},
    "Hekim hesabı ilə giriş edin — eyni hesabla giriş edilən bütün\nkompüterlər hasta verilərini avtomatik paylaşır.": {
        "en": "Log in with your doctor account — all computers logged in\nwith the same account automatically share patient data.",
        "ru": "Войдите под учётной записью врача — все компьютеры,\nвошедшие под одной учётной записью, автоматически делятся данными пациентов.",
        "tr": "Hekim hesabınızla giriş yapın — aynı hesapla giriş yapan tüm\nbilgisayarlar hasta verilerini otomatik olarak paylaşır."},
    "Henüz röntgen eklenmedi": {"en": "No X-ray added yet", "ru": "Ещё не добавлен рентген-снимок", "tr": "Henüz röntgen eklenmedi"},
    "Hesab Yarat": {"en": "Create Account", "ru": "Создать аккаунт", "tr": "Hesap Oluştur"},
    "Kaydedildi": {"en": "Saved", "ru": "Сохранено", "tr": "Kaydedildi"},
    "Layout Kaydedildi": {"en": "Layout Saved", "ru": "Макет сохранён", "tr": "Layout Kaydedildi"},
    "Layout Kaydet (VR)": {"en": "Save Layout (VR)", "ru": "Сохранить макет (VR)", "tr": "Layout Kaydet (VR)"},
    "Müalicə": {"en": "Treatment", "ru": "Лечение", "tr": "Tedavi"},
    "NƏ QƏDƏR VAXT AYRILSIN?": {"en": "HOW MUCH TIME TO ALLOCATE?", "ru": "СКОЛЬКО ВРЕМЕНИ ВЫДЕЛИТЬ?", "tr": "NE KADAR SÜRE AYRILSIN?"},
    "PLANLANMIŞ": {"en": "PLANNED", "ru": "ЗАПЛАНИРОВАНО", "tr": "PLANLANAN"},
    "MÜALİCƏ OLUNMUŞ": {"en": "TREATED", "ru": "ПРОЛЕЧЕНО", "tr": "TEDAVİ EDİLEN"},
    "PNG, JPG, JPEG, BMP desteklenir": {"en": "PNG, JPG, JPEG, BMP supported", "ru": "Поддерживаются PNG, JPG, JPEG, BMP", "tr": "PNG, JPG, JPEG, BMP desteklenir"},
    "Pasient adı:": {"en": "Patient name:", "ru": "Имя пациента:", "tr": "Hasta adı:"},
    "Pasient seçin və ya yeni pasient əlavə edin": {"en": "Select a patient or add a new one", "ru": "Выберите пациента или добавьте нового", "tr": "Hasta seçin veya yeni hasta ekleyin"},
    "Plan": {"en": "Plan", "ru": "План", "tr": "Plan"},
    "QEYD": {"en": "NOTE", "ru": "ЗАМЕТКА", "tr": "NOT"},
    "Qeyd Dəftəri": {"en": "Notebook", "ru": "Блокнот", "tr": "Not Defteri"},
    "Qeyd başlığı...": {"en": "Note title...", "ru": "Заголовок заметки...", "tr": "Not başlığı..."},
    "Qeyd mətni boş ola bilməz.": {"en": "Note text cannot be empty.", "ru": "Текст заметки не может быть пустым.", "tr": "Not metni boş olamaz."},
    "Qeyd Əlavə Etmə Xətası": {"en": "Add Note Error", "ru": "Ошибка добавления заметки", "tr": "Not Ekleme Hatası"},
    "RENTGEN QOVLUĞU": {"en": "X-RAY FOLDER", "ru": "ПАПКА РЕНТГЕНА", "tr": "RÖNTGEN KLASÖRÜ"},
    "Randevu Rəngləri": {"en": "Appointment Colors", "ru": "Цвета записей", "tr": "Randevu Renkleri"},
    "Randevu Təqvimi": {"en": "Appointment Calendar", "ru": "Календарь записей", "tr": "Randevu Takvimi"},
    "Randevu Vaxtı": {"en": "Appointment Time", "ru": "Время записи", "tr": "Randevu Zamanı"},
    "Randevunu sil": {"en": "Delete appointment", "ru": "Удалить запись", "tr": "Randevuyu sil"},
    "Rentgen": {"en": "X-Ray", "ru": "Рентген", "tr": "Röntgen"},
    "Rentgen görüntülərinin avtomatik tutulacağı qovluq.": {"en": "The folder where X-ray images are captured automatically.", "ru": "Папка, из которой рентген-снимки захватываются автоматически.", "tr": "Röntgen görüntülerinin otomatik yakalanacağı klasör."},
    "SAAT (ss:dd)": {"en": "TIME (hh:mm)", "ru": "ВРЕМЯ (чч:мм)", "tr": "SAAT (ss:dd)"},
    "SEÇİLİ DİŞ": {"en": "SELECTED TOOTH", "ru": "ВЫБРАННЫЙ ЗУБ", "tr": "SEÇİLİ DİŞ"},
    "STATUS": {"en": "STATUS", "ru": "СТАТУС", "tr": "DURUM"},
    "STATUS RƏNGLƏRİ": {"en": "STATUS COLORS", "ru": "ЦВЕТА СТАТУСОВ", "tr": "DURUM RENKLERİ"},
    "Geri Al": {"en": "Undo", "ru": "Отменить", "tr": "Geri Al"},
    "Səsli Asistent": {"en": "Voice Assistant", "ru": "Голосовой помощник", "tr": "Sesli Asistan"},
    "Səsli Əmr": {"en": "Voice Command", "ru": "Голосовая команда", "tr": "Sesli Komut"},
    "SƏSLİ ASİSTENT": {"en": "VOICE ASSISTANT", "ru": "ГОЛОСОВОЙ ПОМОЩНИК", "tr": "SESLİ ASİSTAN"},
    "OYADICI SÖZ": {"en": "WAKE WORD", "ru": "СЛОВО АКТИВАЦИИ", "tr": "UYANDIRMA KELİMESİ"},
    "NÜMUNƏ İFADƏLƏR": {"en": "EXAMPLE PHRASES", "ru": "ПРИМЕРЫ ФРАЗ", "tr": "ÖRNEK İFADELER"},
    "Səsli asistenti aktivləşdir (davamlı dinləmə)": {
        "en": "Enable voice assistant (continuous listening)",
        "ru": "Включить голосового помощника (постоянное прослушивание)",
        "tr": "Sesli asistanı etkinleştir (sürekli dinleme)"},
    "Oyadıcı sözü deyib ardından pasientə randevu vaxtı və ya dişlərə ediləcək işləri səslə deyə bilərsiniz.": {
        "en": "Say the wake word, then speak the appointment time or the planned work for the patient's teeth.",
        "ru": "Произнесите слово активации, затем назовите время записи или планируемую работу по зубам пациента.",
        "tr": "Uyandırma kelimesini söyleyip ardından hasta için randevu saatini veya dişlere yapılacak işleri sesli söyleyebilirsiniz."},
    "Qeyd: ilk aktivləşdirmədə tanıma modeli (~500MB) internetdən bir dəfəlik endirilir, sonra tam yerli (offline) işləyir. Mikrofon davamlı dinlənilir, amma heç bir səs buluda göndərilmir — hər parça yalnız yerli emal olunub dərhal atılır.": {
        "en": "Note: on first activation the recognition model (~500MB) is downloaded once from the internet, after which it runs fully locally (offline). The microphone is listened to continuously, but no audio is ever sent to the cloud — each clip is processed locally and discarded immediately.",
        "ru": "Примечание: при первой активации модель распознавания (~500МБ) скачивается один раз из интернета, после чего работает полностью локально (офлайн). Микрофон слушается постоянно, но ни один звук не отправляется в облако — каждый фрагмент обрабатывается локально и сразу удаляется.",
        "tr": "Not: ilk etkinleştirmede tanıma modeli (~500MB) internetten bir kez indirilir, sonra tamamen yerel (çevrimdışı) çalışır. Mikrofon sürekli dinlenir, ancak hiçbir ses buluta gönderilmez — her parça yalnızca yerel olarak işlenip hemen atılır."},
    "ANTHROPIC API AÇARI (İSTƏYƏ BAĞLI)": {
        "en": "ANTHROPIC API KEY (OPTIONAL)",
        "ru": "КЛЮЧ API ANTHROPIC (НЕОБЯЗАТЕЛЬНО)",
        "tr": "ANTHROPIC API ANAHTARI (İSTEĞE BAĞLI)"},
    "Daxil etsəniz, əmrin mənasını yerli qaydalar əvəzinə Claude AI (Anthropic) təhlil edəcək — daha çevik, amma mətn (yalnız tanınan söz, SƏS/şəkil YOX) internetə göndərilir və öz hesabınızdan ödənilir. Boş qalsa, hər şey əvvəlki kimi tam yerli (offline) işləyir.": {
        "en": "If you provide one, Claude AI (Anthropic) will analyze the command's meaning instead of the local rules — more flexible, but the text (only the recognized words, NOT audio/images) is sent over the internet and billed to your own account. Leave it empty and everything works fully locally (offline) as before.",
        "ru": "Если указать ключ, смысл команды будет анализировать Claude AI (Anthropic) вместо локальных правил — более гибко, но текст (только распознанные слова, НЕ аудио/изображения) отправляется в интернет и оплачивается с вашего собственного счёта. Оставьте поле пустым, и всё будет работать полностью локально (офлайн), как раньше.",
        "tr": "Girerseniz, komutun anlamını yerel kurallar yerine Claude AI (Anthropic) analiz edecek — daha esnek, ancak metin (yalnızca tanınan kelimeler, SES/görüntü DEĞİL) internete gönderilir ve kendi hesabınızdan faturalandırılır. Boş bırakırsanız her şey eskisi gibi tamamen yerel (çevrimdışı) çalışır."},
    "ÜMUMİ AI SÖHBƏTİ (PUSH-TO-TALK)": {
        "en": "GENERAL AI CHAT (PUSH-TO-TALK)",
        "ru": "ОБЩИЙ ЧАТ С AI (PUSH-TO-TALK)",
        "tr": "GENEL AI SOHBETİ (PUSH-TO-TALK)"},
    "Düyməni basılı saxlayıb danışın — tanınan mətn Claude-a adi söhbət mesajı kimi göndərilir (yuxarıdaki Anthropic açarı tələb olunur), cavab həm yazılı, həm səslə (internet lazımdır) gəlir. Dental əmr tanınarsa, icra ETMƏZDƏN ƏVVƏL təsdiq istənir.": {
        "en": "Hold the key down and speak — the recognized text is sent to Claude as an ordinary chat message (requires the Anthropic key above); the reply comes back both as text and speech (needs internet). If a dental command is recognized, you are asked to confirm it BEFORE it is applied.",
        "ru": "Удерживайте клавишу и говорите — распознанный текст отправляется Claude как обычное сообщение в чате (требуется ключ Anthropic выше), ответ приходит и текстом, и голосом (нужен интернет). Если распознана стоматологическая команда, перед выполнением запрашивается подтверждение.",
        "tr": "Tuşu basılı tutup konuşun — tanınan metin Claude'a sıradan bir sohbet mesajı olarak gönderilir (yukarıdaki Anthropic anahtarı gerekir), yanıt hem yazılı hem sesli (internet gerekir) gelir. Dental bir komut tanınırsa, uygulanmadan ÖNCE onay istenir."},
    "Ümumi AI söhbətini aktivləşdir": {
        "en": "Enable general AI chat",
        "ru": "Включить общий чат с AI",
        "tr": "Genel AI sohbetini etkinleştir"},
    "DÜYMƏ (PUSH-TO-TALK)": {"en": "KEY (PUSH-TO-TALK)", "ru": "КЛАВИША (PUSH-TO-TALK)", "tr": "TUŞ (PUSH-TO-TALK)"},
    "SƏS (TTS)": {"en": "VOICE (TTS)", "ru": "ГОЛОС (TTS)", "tr": "SES (TTS)"},
    "AI Söhbət": {"en": "AI Chat", "ru": "Чат с AI", "tr": "AI Sohbet"},
    "Əvvəlcə yuxarıda Anthropic API açarını daxil edin.": {
        "en": "Please enter the Anthropic API key above first.",
        "ru": "Сначала введите ключ API Anthropic выше.",
        "tr": "Önce yukarıya Anthropic API anahtarını girin."},
    "Hazır": {"en": "Ready", "ru": "Готово", "tr": "Hazır"},
    "basılı saxla, danışın": {
        "en": "hold down and speak", "ru": "удерживайте и говорите", "tr": "basılı tutup konuşun"},
    "🎤 Dinləyirəm…": {"en": "🎤 Listening…", "ru": "🎤 Слушаю…", "tr": "🎤 Dinliyorum…"},
    "Mikrofon xətası": {"en": "Microphone error", "ru": "Ошибка микрофона", "tr": "Mikrofon hatası"},
    "⏳ Fikirləşirəm…": {"en": "⏳ Thinking…", "ru": "⏳ Думаю…", "tr": "⏳ Düşünüyorum…"},
    "🔊 Danışır…": {"en": "🔊 Speaking…", "ru": "🔊 Говорит…", "tr": "🔊 Konuşuyor…"},
    "Anthropic API açarı Ayarlar-da daxil edilməyib.": {
        "en": "No Anthropic API key has been entered in Settings.",
        "ru": "В настройках не указан ключ API Anthropic.",
        "tr": "Ayarlar'da Anthropic API anahtarı girilmemiş."},
    "Claude-a qoşulmaq alınmadı.": {
        "en": "Could not connect to Claude.", "ru": "Не удалось подключиться к Claude.",
        "tr": "Claude'a bağlanılamadı."},
    "(bəli/xeyr?)": {"en": "(yes/no?)", "ru": "(да/нет?)", "tr": "(evet/hayır?)"},
    "əməliyyat təklif edildi": {
        "en": "action proposed", "ru": "предложено действие", "tr": "işlem önerildi"},
    "Başa düşmədim, bir daha deyə bilərsiniz?": {
        "en": "I didn't understand, could you say that again?",
        "ru": "Я не поняла, можете повторить?",
        "tr": "Anlayamadım, tekrar söyler misiniz?"},
    "Əvvəlcə pasient seçin.": {"en": "Please select a patient first.", "ru": "Сначала выберите пациента.", "tr": "Önce bir hasta seçin."},
    "İmtina": {"en": "Cancel", "ru": "Отмена", "tr": "İptal"},
    "Səsli əmrlə əlavə edildi": {"en": "Added via voice command", "ru": "Добавлено голосовой командой", "tr": "Sesli komutla eklendi"},
    "Sil": {"en": "Delete", "ru": "Удалить", "tr": "Sil"},
    "Sonra": {"en": "Later", "ru": "Позже", "tr": "Sonra"},
    "TARİX (gg.aa.yyyy)": {"en": "DATE (dd.mm.yyyy)", "ru": "ДАТА (дд.мм.гггг)", "tr": "TARİH (gg.aa.yyyy)"},
    "UE5 VibeUE sunucusuna bağlanılamadı.\n\nUE5'in açık ve PIE modunda olduğunu kontrol edin.\n\nManuel olarak UE5 Output Log'unda çalıştırın:\npy C:/BlueX/vr_save_layout.py": {
        "en": "Could not connect to the UE5 VibeUE server.\n\nCheck that UE5 is open and in PIE mode.\n\nRun manually in the UE5 Output Log:\npy C:/BlueX/vr_save_layout.py",
        "ru": "Не удалось подключиться к серверу UE5 VibeUE.\n\nПроверьте, что UE5 открыт и находится в режиме PIE.\n\nЗапустите вручную в UE5 Output Log:\npy C:/BlueX/vr_save_layout.py",
        "tr": "UE5 VibeUE sunucusuna bağlanılamadı.\n\nUE5'in açık ve PIE modunda olduğunu kontrol edin.\n\nManuel olarak UE5 Output Log'unda çalıştırın:\npy C:/BlueX/vr_save_layout.py"},
    "VR Layout Editörü": {"en": "VR Layout Editor", "ru": "Редактор макета VR", "tr": "VR Layout Editörü"},
    "VR REJİMİ": {"en": "VR MODE", "ru": "РЕЖИМ VR", "tr": "VR MODU"},
    "Vaka Adını Değiştir": {"en": "Rename Case", "ru": "Переименовать случай", "tr": "Vaka Adını Değiştir"},
    "Vakayı Sil": {"en": "Delete Case", "ru": "Удалить случай", "tr": "Vakayı Sil"},
    "Xəbərdarlıq": {"en": "Warning", "ru": "Предупреждение", "tr": "Uyarı"},
    "Xəta": {"en": "Error", "ru": "Ошибка", "tr": "Hata"},
    "YENİ QEYD": {"en": "NEW NOTE", "ru": "НОВАЯ ЗАМЕТКА", "tr": "YENİ NOT"},
    "Yalnız alt qovluqlardakı faylları al": {"en": "Only take files from subfolders", "ru": "Брать файлы только из подпапок", "tr": "Sadece alt klasörlerdeki dosyaları al"},
    "Yeni Pasient": {"en": "New Patient", "ru": "Новый пациент", "tr": "Yeni Hasta"},
    "Yeni Rentgen Aşkarlandı": {"en": "New X-Ray Detected", "ru": "Обнаружен новый рентген-снимок", "tr": "Yeni Röntgen Algılandı"},
    "Yeni Sürüm Var": {"en": "New Version Available", "ru": "Доступна новая версия", "tr": "Yeni Sürüm Var"},
    "Yeni Vaka": {"en": "New Case", "ru": "Новый случай", "tr": "Yeni Vaka"},
    "Yeni vaka adı:": {"en": "New case name:", "ru": "Новое название случая:", "tr": "Yeni vaka adı:"},
    "Yükleme Hatası": {"en": "Loading Error", "ru": "Ошибка загрузки", "tr": "Yükleme Hatası"},
    "Yüklendi": {"en": "Loaded", "ru": "Загружено", "tr": "Yüklendi"},
    "Çıxış": {"en": "Log Out", "ru": "Выйти", "tr": "Çıkış"},
    "Ümumi": {"en": "General", "ru": "Общие", "tr": "Genel"},
    "İLKİN VƏZİYYƏT": {"en": "INITIAL CONDITION", "ru": "НАЧАЛЬНОЕ СОСТОЯНИЕ", "tr": "İLK DURUM"},
    "İNTRAORAL VƏ ÜZ FOTOLARI": {"en": "INTRAORAL & FACE PHOTOS", "ru": "ВНУТРИРОТОВЫЕ И ЛИЦЕВЫЕ ФОТО", "tr": "AĞIZ İÇİ VE YÜZ FOTOĞRAFLARI"},
    "İlkin Vəziyyət": {"en": "Initial Condition", "ru": "Начальное состояние", "tr": "İlk Durum"},
    "İpucu: boş xanaya klikləyib o saata randevu əlavə et": {"en": "Tip: click an empty slot to add an appointment at that time", "ru": "Совет: щёлкните по пустой ячейке, чтобы добавить запись на это время", "tr": "İpucu: boş hücreye tıklayıp o saate randevu ekleyin"},
    "Şifrə": {"en": "Password", "ru": "Пароль", "tr": "Şifre"},
    "Əvvəlcə bir hasta seçin.": {"en": "Please select a patient first.", "ru": "Сначала выберите пациента.", "tr": "Önce bir hasta seçin."},
    "Hasta adı boş ola bilməz.": {"en": "Patient name cannot be empty.", "ru": "Имя пациента не может быть пустым.", "tr": "Hasta adı boş olamaz."},
    "Tarix formatı yanlışdır (gg.aa.yyyy).": {"en": "Invalid date format (dd.mm.yyyy).", "ru": "Неверный формат даты (дд.мм.гггг).", "tr": "Tarih formatı hatalı (gg.aa.yyyy)."},
    "Ad Soyad": {"en": "Full Name", "ru": "ФИО", "tr": "Ad Soyad"},
    "Yaş": {"en": "Age", "ru": "Возраст", "tr": "Yaş"},
    "Telefon": {"en": "Phone", "ru": "Телефон", "tr": "Telefon"},
    "TC/ID": {"en": "ID Number", "ru": "Номер ID", "tr": "TC/Kimlik No"},
    "Qeyd": {"en": "Note", "ru": "Заметка", "tr": "Not"},
    "☁ BULUD SENKRONİZASYONU": {"en": "☁ CLOUD SYNC", "ru": "☁ ОБЛАЧНАЯ СИНХРОНИЗАЦИЯ", "tr": "☁ BULUT SENKRONİZASYONU"},
    "☁ Buluda Yüklə": {"en": "☁ Upload to Cloud", "ru": "☁ Загрузить в облако", "tr": "☁ Buluta Yükle"},
    "☁ Buluddan Yenilə": {"en": "☁ Refresh from Cloud", "ru": "☁ Обновить из облака", "tr": "☁ Buluttan Yenile"},
    "☁ Hasta Seç…": {"en": "☁ Select Patient…", "ru": "☁ Выбрать пациента…", "tr": "☁ Hasta Seç…"},
    "⚙  Ayarlar": {"en": "⚙  Settings", "ru": "⚙  Настройки", "tr": "⚙  Ayarlar"},
    "✓  Evet, Ekle": {"en": "✓  Yes, Add", "ru": "✓  Да, добавить", "tr": "✓  Evet, Ekle"},
    "✓ Gəldi": {"en": "✓ Arrived", "ru": "✓ Прибыл", "tr": "✓ Geldi"},
    "✗  Hayır, İptal": {"en": "✗  No, Cancel", "ru": "✗  Нет, отмена", "tr": "✗  Hayır, İptal"},
    "⬇  İndir ve Kur": {"en": "⬇  Download & Install", "ru": "⬇  Скачать и установить", "tr": "⬇  İndir ve Kur"},
    "🎨 Rənglər": {"en": "🎨 Colors", "ru": "🎨 Цвета", "tr": "🎨 Renkler"},
    "💾 Saxla": {"en": "💾 Save", "ru": "💾 Сохранить", "tr": "💾 Kaydet"},
    "📂  Pasient Yükle (.bxd)": {"en": "📂  Load Patient (.bxd)", "ru": "📂  Загрузить пациента (.bxd)", "tr": "📂  Hasta Yükle (.bxd)"},
    "📂 Gözat…": {"en": "📂 Browse…", "ru": "📂 Обзор…", "tr": "📂 Gözat…"},
    "📅  Randevu Təqvimi": {"en": "📅  Appointment Calendar", "ru": "📅  Календарь записей", "tr": "📅  Randevu Takvimi"},
    "🔄 Güncellemeleri Kontrol Et": {"en": "🔄 Check for Updates", "ru": "🔄 Проверить обновления", "tr": "🔄 Güncellemeleri Kontrol Et"},
    "🔍 Hasta axtar…": {"en": "🔍 Search patient…", "ru": "🔍 Поиск пациента…", "tr": "🔍 Hasta ara…"},
    "🔬  Tedaviyə Keç": {"en": "🔬  Go to Treatment", "ru": "🔬  Перейти к лечению", "tr": "🔬  Tedaviye Geç"},
    "🕒 Randevu vaxtı çatdı": {"en": "🕒 Appointment time has arrived", "ru": "🕒 Наступило время записи", "tr": "🕒 Randevu zamanı geldi"},
    "🗑 Randevunu Sil": {"en": "🗑 Delete Appointment", "ru": "🗑 Удалить запись", "tr": "🗑 Randevuyu Sil"},
    "🚀  Yeni Sürüm Mevcut": {"en": "🚀  New Version Available", "ru": "🚀  Доступна новая версия", "tr": "🚀  Yeni Sürüm Mevcut"},
    "🥽  VR Düzenle": {"en": "🥽  Edit VR", "ru": "🥽  Редактор VR", "tr": "🥽  VR Düzenle"},
    "🦷  P A S İ E N T L Ə R": {"en": "🦷  P A T I E N T S", "ru": "🦷  П А Ц И Е Н Т Ы", "tr": "🦷  H A S T A L A R"},
    "🦷  Yeni Rentgen Aşkarlandı": {"en": "🦷  New X-Ray Detected", "ru": "🦷  Обнаружен новый рентген-снимок", "tr": "🦷  Yeni Röntgen Algılandı"},

    # Əlavələr: dinamik/canvas yerlərdə istifadə olunur
    "Sağlam": {"en": "Healthy", "ru": "Здоровый", "tr": "Sağlıklı"},
    "Çürük": {"en": "Decay", "ru": "Кариес", "tr": "Çürük"},
    "Çəkim": {"en": "Extraction", "ru": "Удаление", "tr": "Çekim"},
    "Kanal": {"en": "Root Canal", "ru": "Канал", "tr": "Kanal"},
    "Kompozit Dolğu": {"en": "Composite Filling", "ru": "Композитная пломба", "tr": "Kompozit Dolgu"},
    "Qapaq": {"en": "Crown", "ru": "Коронка", "tr": "Kron"},
    "İmplant": {"en": "Implant", "ru": "Имплант", "tr": "İmplant"},
    "Əksik": {"en": "Missing", "ru": "Отсутствует", "tr": "Eksik"},
    "Kök": {"en": "Root", "ru": "Корень", "tr": "Kök"},
    "Gözləm": {"en": "Observation", "ru": "Наблюдение", "tr": "Gözlem"},
    "Diş Daşı": {"en": "Tartar", "ru": "Зубной камень", "tr": "Diş Taşı"},
    "Diş Daşı Təmizliyi": {"en": "Tartar Cleaning", "ru": "Чистка зубного камня", "tr": "Diş Taşı Temizliği"},
    "Aktiv": {"en": "Active", "ru": "Активно", "tr": "Aktif"},
    "Vaka": {"en": "Case", "ru": "Случай", "tr": "Vaka"},
    "Diş": {"en": "Tooth", "ru": "Зуб", "tr": "Diş"},

    "Gözləyir": {"en": "Waiting", "ru": "Ожидает", "tr": "Bekliyor"},
    "Gəldi": {"en": "Arrived", "ru": "Прибыл", "tr": "Geldi"},
    "Gəlmədi": {"en": "No-show", "ru": "Не пришёл", "tr": "Gelmedi"},
    "Ləğv edildi": {"en": "Cancelled", "ru": "Отменено", "tr": "İptal edildi"},
    "dəqiqə": {"en": "minutes", "ru": "минут", "tr": "dakika"},

    "Sağ Üst": {"en": "Upper Right", "ru": "Верхняя правая", "tr": "Sağ Üst"},
    "Sol Üst": {"en": "Upper Left", "ru": "Верхняя левая", "tr": "Sol Üst"},
    "Sol Alt": {"en": "Lower Left", "ru": "Нижняя левая", "tr": "Sol Alt"},
    "Sağ Alt": {"en": "Lower Right", "ru": "Нижняя правая", "tr": "Sağ Alt"},

    "Santral": {"en": "Central Incisor", "ru": "Центральный резец", "tr": "Santral"},
    "Lateral": {"en": "Lateral Incisor", "ru": "Боковой резец", "tr": "Lateral"},
    "Kanin": {"en": "Canine", "ru": "Клык", "tr": "Kanin"},
    "1.Premolar": {"en": "1st Premolar", "ru": "1-й премоляр", "tr": "1.Premolar"},
    "2.Premolar": {"en": "2nd Premolar", "ru": "2-й премоляр", "tr": "2.Premolar"},
    "1.Molar": {"en": "1st Molar", "ru": "1-й моляр", "tr": "1.Molar"},
    "2.Molar": {"en": "2nd Molar", "ru": "2-й моляр", "tr": "2.Molar"},
    "Yirmilik": {"en": "Wisdom Tooth", "ru": "Зуб мудрости", "tr": "Yirmi Yaş Dişi"},

    "B.e": {"en": "Mon", "ru": "Пн", "tr": "Pzt"},
    "Ç.a": {"en": "Tue", "ru": "Вт", "tr": "Sal"},
    "Ç": {"en": "Wed", "ru": "Ср", "tr": "Çar"},
    "C.a": {"en": "Thu", "ru": "Чт", "tr": "Per"},
    "C": {"en": "Fri", "ru": "Пт", "tr": "Cum"},
    "Ş": {"en": "Sat", "ru": "Сб", "tr": "Cmt"},
    "B": {"en": "Sun", "ru": "Вс", "tr": "Paz"},

    "Yan": {"en": "Jan", "ru": "Янв", "tr": "Oca"},
    "Fev": {"en": "Feb", "ru": "Фев", "tr": "Şub"},
    "Mar": {"en": "Mar", "ru": "Мар", "tr": "Mar"},
    "Apr": {"en": "Apr", "ru": "Апр", "tr": "Nis"},
    "May": {"en": "May", "ru": "Май", "tr": "May"},
    "İyn": {"en": "Jun", "ru": "Июн", "tr": "Haz"},
    "İyl": {"en": "Jul", "ru": "Июл", "tr": "Tem"},
    "Avq": {"en": "Aug", "ru": "Авг", "tr": "Ağu"},
    "Sen": {"en": "Sep", "ru": "Сен", "tr": "Eyl"},
    "Okt": {"en": "Oct", "ru": "Окт", "tr": "Eki"},
    "Noy": {"en": "Nov", "ru": "Ноя", "tr": "Kas"},
    "Dek": {"en": "Dec", "ru": "Дек", "tr": "Ara"},

    "Cinsiyət": {"en": "Gender", "ru": "Пол", "tr": "Cinsiyet"},
    "Kişi": {"en": "Male", "ru": "Мужской", "tr": "Erkek"},
    "Qadın": {"en": "Female", "ru": "Женский", "tr": "Kadın"},
    "Şəhər": {"en": "City", "ru": "Город", "tr": "Şehir"},
    "📊  Statistika": {"en": "📊  Statistics", "ru": "📊  Статистика", "tr": "📊  İstatistik"},
    "Statistika": {"en": "Statistics", "ru": "Статистика", "tr": "İstatistik"},
    "📊 Ümumi Məlumat": {"en": "📊 General Info", "ru": "📊 Общая информация", "tr": "📊 Genel Bilgi"},
    "Toplam Pasient": {"en": "Total Patients", "ru": "Всего пациентов", "tr": "Toplam Hasta"},
    "Cinsiyəti Qeyd Olunmayan": {"en": "Gender Not Recorded", "ru": "Пол не указан", "tr": "Cinsiyeti Belirtilmemiş"},
    "🦷 Çürük Analizi — Boşluq Yerləşməsi": {
        "en": "🦷 Cavity Analysis — Surface Location",
        "ru": "🦷 Анализ кариеса — расположение по поверхности",
        "tr": "🦷 Çürük Analizi — Yüzey Konumu"},
    "🦴 Diş Çəkimi Statistikası": {
        "en": "🦴 Tooth Extraction Statistics",
        "ru": "🦴 Статистика удаления зубов",
        "tr": "🦴 Diş Çekimi İstatistiği"},
    "📍 Şəhərə Görə Statistika": {
        "en": "📍 Statistics by City", "ru": "📍 Статистика по городам", "tr": "📍 Şehre Göre İstatistik"},
    "⚥ Cinsiyətə Görə Statistika": {
        "en": "⚥ Statistics by Gender", "ru": "⚥ Статистика по полу", "tr": "⚥ Cinsiyete Göre İstatistik"},
    "Şəhər:": {"en": "City:", "ru": "Город:", "tr": "Şehir:"},
    "pasient": {"en": "patients", "ru": "пациентов", "tr": "hasta"},
    "Çəkim sayı": {"en": "Extraction count", "ru": "Количество удалений", "tr": "Çekim sayısı"},
    "Çürük qeydi yoxdur.": {"en": "No cavities recorded.", "ru": "Нет данных о кариесе.", "tr": "Çürük kaydı yok."},
    "Çəkim qeydi yoxdur.": {"en": "No extractions recorded.", "ru": "Нет данных об удалении.", "tr": "Çekim kaydı yok."},
    "Şəhər məlumatı qeyd olunmayıb.": {
        "en": "No city information recorded.", "ru": "Информация о городе не указана.", "tr": "Şehir bilgisi girilmemiş."},
    "Bu şəhər üçün çürük qeydi yoxdur.": {
        "en": "No cavity records for this city.", "ru": "Нет данных о кариесе для этого города.",
        "tr": "Bu şehir için çürük kaydı yok."},
    "Filtrlər": {"en": "Filters", "ru": "Фильтры", "tr": "Filtreler"},
    "Hamısı": {"en": "All", "ru": "Все", "tr": "Tümü"},
    "I Sinif (O)": {"en": "Class I (O)", "ru": "Класс I (O)", "tr": "I. Sınıf (O)"},
    "II-III Sinif (M/D)": {
        "en": "Class II-III (M/D)", "ru": "Класс II-III (M/D)", "tr": "II-III. Sınıf (M/D)"},
    "V Sinif (V)": {"en": "Class V (V)", "ru": "Класс V (V)", "tr": "V. Sınıf (V)"},
    "Cins:": {"en": "Gender:", "ru": "Пол:", "tr": "Cinsiyet:"},
    "Sinif:": {"en": "Class:", "ru": "Класс:", "tr": "Sınıf:"},
    "Tarixdən:": {"en": "From date:", "ru": "С даты:", "tr": "Tarihten:"},
    "Tarixə:": {"en": "To date:", "ru": "До даты:", "tr": "Tarihe:"},
    "Yaşdan:": {"en": "From age:", "ru": "С возраста:", "tr": "Yaştan:"},
    "Yaşa:": {"en": "To age:", "ru": "До возраста:", "tr": "Yaşa:"},
    "Hesabla": {"en": "Calculate", "ru": "Рассчитать", "tr": "Hesapla"},
    "Tarix formatı: gg.aa.illll (məs: 01.01.2026)": {
        "en": "Date format: dd.mm.yyyy (e.g. 01.01.2026)",
        "ru": "Формат даты: дд.мм.гггг (напр. 01.01.2026)",
        "tr": "Tarih formatı: gg.aa.yyyy (örn: 01.01.2026)"},
    "Yaş rəqəm olmalıdır.": {
        "en": "Age must be a number.", "ru": "Возраст должен быть числом.", "tr": "Yaş bir sayı olmalıdır."},
    "Hesablamaya Daxil Pasient": {
        "en": "Patients Included", "ru": "Включено пациентов", "tr": "Hesaba Dahil Hasta"},
    "Bazada ümumi": {"en": "Total in database:", "ru": "Всего в базе:", "tr": "Veritabanında toplam:"},
    "pasient qeydiyyatdadır": {
        "en": "patients registered", "ru": "зарегистрированных пациентов", "tr": "hasta kayıtlı"},
    "Filtrə uyğun pasient tapılmadı.": {
        "en": "No patients match this filter.", "ru": "Нет пациентов, соответствующих фильтру.",
        "tr": "Bu filtreye uyan hasta bulunamadı."},
    "📈 Zaman Üzrə Dinamika": {
        "en": "📈 Trend Over Time", "ru": "📈 Динамика по времени", "tr": "📈 Zamana Göre Değişim"},
    "Bu tarix aralığı üçün məlumat yoxdur.": {
        "en": "No data for this date range.", "ru": "Нет данных за этот период.",
        "tr": "Bu tarih aralığı için veri yok."},
    "Saxla": {"en": "Save", "ru": "Сохранить", "tr": "Kaydet"},
    "Foto Adlandır": {"en": "Name Photo", "ru": "Назвать фото", "tr": "Fotoğrafı Adlandır"},
    "Foto adı (diş nömrəsi ilə, məs: #36 #35)": {
        "en": "Photo name (with tooth number, e.g. #36 #35)",
        "ru": "Название фото (с номером зуба, напр. #36 #35)",
        "tr": "Foto adı (diş numarasıyla, örn: #36 #35)"},
}

# hər dildəki mətndən (az/en/ru/tr — hansı hazırda ekranda olur-olsun) eyni girişə
# geri-axtarış — dil bir neçə dəfə dəyişsə belə düzgün işləsin deyə
_STR_TO_ENTRY = {}
for _az, _entry in TRANSLATIONS.items():
    _full = dict(_entry); _full["az"] = _az
    _STR_TO_ENTRY[_az] = _full
    for _v in _entry.values():
        _STR_TO_ENTRY[_v] = _full

def t(text):
    """Cari dilə görə mətni tərcümə edir; bilinməyən mətn olduğu kimi qalır."""
    entry = _STR_TO_ENTRY.get(text)
    if not entry:
        return text
    return entry.get(CURRENT_LANG, text)

def xray_watch_dir():
    return (AYARLAR.get("xray_klasoru") or "").strip() or XRAY_WATCH_DIR

def xray_subdir_only():
    return AYARLAR.get("alt_klasor_sarti", True)

def sesli_asistent_aktiv():
    return bool(AYARLAR.get("sesli_asistent_aktiv", False))

def sesli_oyadici_soz():
    return (AYARLAR.get("sesli_oyadici_soz") or "bulex").strip().lower()

def chat_asistent_aktiv():
    return bool(AYARLAR.get("chat_asistent_aktiv", False))

def chat_hotkey():
    return (AYARLAR.get("chat_hotkey") or "f9").strip().lower()

def tts_ses_adi():
    return AYARLAR.get("tts_ses") or "az-AZ-BabekNeural"

_TESDIQ_SOZLERI = {"beli", "ha", "yaz", "elave et", "tesdiq", "oldu", "bas"}
_LEGV_SOZLERI = {"xeyr", "yox", "legv", "legv et", "imtina", "dayan", "stop"}

def _chat_tesdiq_xeyr_yoxla(text):
    """Çat rejimində gözlənilən təsdiq/ləğv cavabını (bəli/hə/yaz vs
    xeyr/yox/ləğv) sərbəst danışılan mətndən tapır. `_az_sade()` ilə
    diakritiklər təmizləndikdən sonra söz-sərhədli axtarış aparır (sadə
    "in" yoxlaması "yazmayın" kimi sözlərdə yanlış uyğunluq verər)."""
    norm = f" {_az_sade(text or '')} "
    for soz in _LEGV_SOZLERI:
        if f" {soz} " in norm:
            return "legv"
    for soz in _TESDIQ_SOZLERI:
        if f" {soz} " in norm:
            return "tesdiq"
    return None

try:
    import pydicom as _pydicom
    _DCM_OK = True
except Exception:
    _DCM_OK = False

XRAY_UZANTILAR = (".bmp", ".jpg", ".jpeg", ".png", ".tif", ".tiff") + ((".dcm",) if _DCM_OK else ())

def dcm_to_png(path):
    """DICOM dosyasını 8-bit PNG'ye çevirir; PNG yolunu döndürür (hata→None)."""
    if not _DCM_OK:
        return None
    try:
        ds = _pydicom.dcmread(path)
        arr = ds.pixel_array.astype("float32")
        lo, hi = float(arr.min()), float(arr.max())
        if hi <= lo:
            return None
        arr = (arr - lo) / (hi - lo)
        if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
            arr = 1.0 - arr
        img = Image.fromarray((arr * 255).astype("uint8"))
        out_dir = os.path.join(os.path.dirname(DATA_FILE), "dcm_cache")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".png")
        img.save(out)
        return out
    except Exception as e:
        print(f"[XRay] DICOM çevrilemedi ({os.path.basename(path)}): {e}")
        return None

try:
    import rawpy as _rawpy
    _RAW_OK = True
except Exception:
    _RAW_OK = False

# Canon (.cr2/.cr3), Nikon (.nef/.nrw) və rawpy/LibRaw-ın dəstəklədiyi digər RAW
# formatları (.arw, .dng, .orf, .raf, .rw2 və s.) — foto aparatlarından birbaşa
# gələn kartlardakı fayllar.
RAW_UZANTILAR = (".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng", ".orf", ".raf", ".rw2") if _RAW_OK else ()
XRAY_UZANTILAR = XRAY_UZANTILAR + RAW_UZANTILAR

def raw_oxu(path):
    """RAW foto faylını (Canon/Nikon və s.) PIL Image-ə çevirir (xəta→None)."""
    if not _RAW_OK:
        return None
    try:
        with _rawpy.imread(path) as raw:
            arr = raw.postprocess(use_camera_wb=True, no_auto_bright=False, output_bps=8)
        return Image.fromarray(arr)
    except Exception as e:
        print(f"[Foto] RAW çevrilemedi ({os.path.basename(path)}): {e}")
        return None

def sekli_ac(path):
    """Yol istənilən dəstəklənən formatda ola bilər (adi şəkil, DICOM, ya da
    Canon/Nikon RAW) — hamısını PIL Image olaraq açır (xəta→None)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dcm":
        png = dcm_to_png(path)
        return Image.open(png) if png else None
    if ext in RAW_UZANTILAR:
        return raw_oxu(path)
    try:
        return Image.open(path)
    except Exception as e:
        print(f"[Foto] şəkil açılmadı ({os.path.basename(path)}): {e}")
        return None

def raw_cache_hazirla(path):
    """RAW foto faylını sıxışdırılmış JPEG olaraq keşə salır; yol qaytarır (xəta→None)."""
    img = raw_oxu(path)
    if img is None:
        return None
    try:
        out_dir = os.path.join(os.path.dirname(DATA_FILE), "raw_cache")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".jpg")
        with open(out, "wb") as f:
            f.write(sekli_sixisdir(img))
        return out
    except Exception as e:
        print(f"[Foto] RAW keşlənmədi ({os.path.basename(path)}): {e}")
        return None

def sekli_sixisdir(img, max_boyut=1600, keyfiyyet=82):
    """Böyük foto aparatı şəkillərini (RAW və ya adi JPEG/PNG) proqram daxilində
    saxlamaq/buluda göndərmək üçün ağlabatan ölçüyə sıxışdırır — JPEG bytes qaytarır."""
    img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_boyut:
        f = max_boyut / max(w, h)
        img = img.resize((max(1, int(w * f)), max(1, int(h * f))), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=keyfiyyet, optimize=True)
    return buf.getvalue()

# ── BULUD SENKRONİZASYONU (Supabase) ─────────────────────────────────────────
# Bu iki değer klinik geneli tek Supabase projesine aittir (hasta-verisi
# tarayıcıya/koda gömülmez — yalnız "hangi sunucu" bilgisi). Kurulum
# tamamlanınca buraya yazılır ve exe yeniden derlenir. Boş kaldığı sürece
# bulut özellikleri UI'da "yapılandırılmadı" olarak görünür, uygulama
# tamamen çevrimdışı çalışmaya devam eder.
SUPABASE_URL = "https://rletrqsnajhfpfnduwxo.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_1BgT7dDC8eYUQrRizR9vpw_v13OSKxB"

def cloud_configured():
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)

def _sb_timeout_for(data_bytes):
    """Rentgen/foto daxil hasta qeydləri 20+ MB ola bilir (canlı hastalar.json-da
    23 MB-a qədər görülüb) — sabit 15s timeout bu qeydlərin yüklənməsini yarıda
    kəsib 'vaxt bitdi' xətası verirdi (yavaş/orta sürətli yüklə bağlantısında
    böyük payload 15s-i asanlıqla keçir — canlı sınaqla 20MB üçün 84s ölçüldü).
    Payload ölçüsünə görə minimum ~80KB/s yükləmə sürəti fərz edilərək taban
    (15s) və tavan arasında miqyaslanır. Tavan 300s→900s qaldırıldı (2026-08-07):
    çox rentgen/foto şəkli olan bəzi real hastalar (30+ MB) 300s-lik köhnə
    tavanı da keçirdi, "vaxt bitdi" ilə həmin hasta cloud_push_all-da atlanırdı."""
    if not data_bytes:
        return 15
    return max(15, min(900, data_bytes // (80 * 1024)))

_SSL_BAGLAM_CACHE = {}

def ssl_baglam():
    """HTTPS sorğuları üçün SSL konteksti — Windows-un öz sertifikat mağazasında
    bəzən lazımi ARA-sertifikat olmur (brauzer bunu ilk ehtiyac olanda özü
    avtomatik yükləyib keşləyir, Python isə YOX) — bu, brauzerdə hər şey
    normal görünərkən Python-da "certificate verify failed" xətasına səbəb
    olur (tanınmış Windows+Python problemi). `certifi`-nin öz-özünə kifayət
    edən CA bundle-ı bunu keçir; certifi mövcud deyilsə sistem defoltuna düşür."""
    if "ctx" not in _SSL_BAGLAM_CACHE:
        try:
            import certifi, ssl
            _SSL_BAGLAM_CACHE["ctx"] = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_BAGLAM_CACHE["ctx"] = None
    return _SSL_BAGLAM_CACHE["ctx"]

def _sb_http(method, path, body=None, token=None, extra_headers=None, timeout=None):
    import urllib.request, urllib.error, socket
    if not cloud_configured():
        raise RuntimeError("Bulud hələ quraşdırılmayıb")
    url = SUPABASE_URL.rstrip("/") + path
    headers = {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {token or SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if timeout is None:
        timeout = _sb_timeout_for(len(data) if data else 0)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # urlopen'in `timeout` arqumenti bəzi Windows/antivirus/proksi konfiqurasiyalarında
    # TLS handshake mərhələsini əhatə etmir (bağlantı sonsuza qədər asılı qala bilər —
    # canlı istifadəçidə 10+ dəqiqə müşahidə edildi). Socket-səviyyəli qlobal timeout
    # əlavə ehtiyat kimi qoyulur, çağırışdan sonra əvvəlki dəyərə qaytarılır.
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_baglam()) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {msg[:300]}")
    except urllib.error.URLError as e:
        print(f"[DEBUG-BULUD] URLError raw: {type(e.reason).__name__}: {e.reason!r}".encode("ascii","replace").decode())
        raise RuntimeError(f"Bağlantı xətası: {e.reason}")
    except socket.timeout:
        print("[DEBUG-BULUD] socket.timeout caught")
        raise RuntimeError("Bağlantı xətası: vaxt bitdi (timeout)")
    except Exception as e:
        print(f"[DEBUG-BULUD] UNEXPECTED raw error: {type(e).__name__}: {e!r}".encode("ascii","replace").decode())
        raise
    finally:
        socket.setdefaulttimeout(prev_timeout)

def _sb_http_raw(method, path, data=None, token=None, content_type=None, timeout=60):
    """`_sb_http` ilə eyni (apikey/Authorization/timeout/SSL fix), amma JSON
    encode/decode ETMİR — Storage-dən şəkil bayt oxumaq/yazmaq üçün (gövdə
    artıq xam bayt, cavab da xam bayt kimi qaytarılır)."""
    import urllib.request, urllib.error, socket
    url = SUPABASE_URL.rstrip("/") + path
    headers = {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token or SUPABASE_ANON_KEY}"}
    if content_type:
        headers["Content-Type"] = content_type
    if data is not None:
        headers["x-upsert"] = "true"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_baglam()) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"HTTP {e.code}: {msg[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Bağlantı xətası: {e.reason}")
    except socket.timeout:
        raise RuntimeError("Bağlantı xətası: vaxt bitdi (timeout)")
    finally:
        socket.setdefaulttimeout(prev_timeout)

_IMG_CACHE_DIR = os.path.join(os.path.dirname(DATA_FILE), "cache_img")

def _img_cache_path(storage_path):
    os.makedirs(_IMG_CACHE_DIR, exist_ok=True)
    return os.path.join(_IMG_CACHE_DIR, storage_path.replace("/", "_"))

def sb_signup(email, password):
    return _sb_http("POST", "/auth/v1/signup", {"email": email, "password": password})

def sb_login(email, password):
    return _sb_http("POST", "/auth/v1/token?grant_type=password",
                     {"email": email, "password": password})

def sb_refresh(refresh_token):
    return _sb_http("POST", "/auth/v1/token?grant_type=refresh_token",
                     {"refresh_token": refresh_token})

# ── OTOMATİK GÜNCELLEME KONTROLÜ ──────────────────────────────────────────────
# GitHub Releases API-dən ("latest" release) sürüm oxunur — Supabase baza
# kvotası dolub READ-ONLY rejimə düşdüyü üçün (bax [[dental-supabase-quota-full]])
# bu mexanizm GitHub-a köçürülüb: hər yeni versiya GitHub Release olaraq
# yükləndikdə (bax [[reference-bluex-github]] release axını) bu funksiya onu
# avtomatik tapır, Supabase-dən asılılıq yoxdur.
APP_VERSION = "1.2.10"
GITHUB_REPO = "elikarayev8-lgtm/bluex-dental-planlama"

def _ver_tuple(s):
    parcalar = []
    for p in str(s).split("."):
        try: parcalar.append(int(p))
        except ValueError: parcalar.append(0)
    return tuple(parcalar)

def check_for_update():
    """GitHub-dakı ən son release-i sorğulayır. Mövcud versiyadan yenidirsə
    məlumat dict'i (surum/indirme_url/notlar) qaytarır, deyilsə ya da
    sorğu uğursuz olarsa None qaytarır (səssizcə)."""
    import urllib.request, urllib.error
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "BlueXDental",
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl_baglam()) as resp:
            rel = json.loads(resp.read())
    except Exception:
        return None
    surum = str(rel.get("tag_name", "")).lstrip("vV")
    if not surum or _ver_tuple(surum) <= _ver_tuple(APP_VERSION):
        return None
    asset = next((a for a in rel.get("assets", [])
                  if a.get("name", "").lower() == "bluexdental_setup.exe"), None)
    if not asset:
        return None
    return {
        "surum": surum,
        "indirme_url": asset["browser_download_url"],
        "notlar": rel.get("body") or "",
    }

def yeni_id(prefix):
    """Zaman damğası + qısa təsadüfi son əlavə — fərqli kompüterlərdə/hekimlərdə
    eyni saniyədə yaradılan qeydlərin bulud sinxronunda ID toqquşmasının qarşısını alır."""
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

def veri_yukle():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE,"r",encoding="utf-8") as f:
                # strict=False: bəzi hasta qeydlərində (Not Dəftəri, qeyd sahələri və s.)
                # ham sətir-sonu/tab kimi nəzarət simvolları ola bilər — JSON spesifikasiyası
                # bunları qadağan edir, amma strict=False bu sahələri normal mətn kimi qəbul edir.
                data = json.load(f, strict=False)
        except json.JSONDecodeError as e:
            _veri_faylini_zedelenmis_bildir(e)
            return {}
        # Eski format → vakalar'a geçiş
        changed = False
        for hid, h in data.items():
            if "vakalar" not in h:
                from datetime import datetime as _dt
                vid = yeni_id("vaka")
                h["vakalar"] = {vid: {
                    "ad": f"1. {t('Vaka')}",
                    "tarih": _dt.now().strftime("%d.%m.%Y"),
                    "plan":   h.pop("plan",   {}),
                    "tedavi": h.pop("tedavi", {}),
                }}
                changed = True
        if changed:
            veri_kaydet(data)
        return data
    return {}

def _veri_faylini_zedelenmis_bildir(hata):
    """DATA_FILE JSON kimi oxuna bilməyəndə: zədələnmiş faylı yan qovluğa köçürüb
    saxlayır (itməsin) və istifadəçiyə ham traceback əvəzinə anlaşılan mesaj göstərir."""
    korrupt_yol = None
    try:
        korrupt_yol = DATA_FILE + f".korrupt_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copyfile(DATA_FILE, korrupt_yol)
    except Exception:
        korrupt_yol = None
    yedek_qovlugu = os.path.join(os.path.dirname(DATA_FILE), "HastaYedekleri")
    mesaj = (
        "Verilər faylı (hastalar.json) zədələnib, oxuna bilmir:\n"
        f"{hata}\n\n"
        "Proqram BOŞ pasient siyahısı ilə açılacaq ki, işə davam edə biləsiniz.\n"
        + (f"Zədələnmiş fayl qorunub saxlanıldı:\n{korrupt_yol}\n\n" if korrupt_yol else "\n")
        + f"Hər pasientin ayrı ehtiyat nüsxəsi (.bxd) bu qovluqda ola bilər:\n{yedek_qovlugu}\n"
        "Oradan \"Pasient Yükle\" ilə pasientləri bir-bir geri yükləyə bilərsiniz."
    )
    try:
        from tkinter import messagebox
        messagebox.showerror("Verilər Faylı Zədələnib", mesaj)
    except Exception:
        print(mesaj)

_CLOUD_DIRTY = False    # yerelde değişiklik oldu, buluta gönderilmeyi bekliyor
_CLOUD_SYNC_LOCK = threading.Lock()  # manuel push/pull ile 20sn'lik otomatik tick'in
                                      # aynı anda çalışıp token yenileme yarışına girmesini engeller
_BACKUP_DIRTY = False   # yerelde değişiklik oldu, .bxd yedeyi bekliyor
_BACKUP_LAST_CHANGE = 0.0

# hastalar.json-un faktiki disk yazısı — pasient/foto sayı artdıqca hər kiçik
# əməliyyatda (məs. hər diş kliki) BÜTÜN bazanı sinxron yenidən yazmaq proqramı
# get-gedə yavaşladırdığından, bu DentalApp._hastalar_save_tick tərəfindən
# qısa (≈0.8s) gecikdirilərək arxa plan thread-ində aparılır (bax: veri_kaydet).
_HASTALAR_DIRTY       = False
_HASTALAR_LAST_CHANGE = 0.0

_CLOUD_SENT_HASH = {}   # hasta_id -> son uğurla buluda göndərilmiş/buluddan alınmış
                         # məzmunun hash-i — avtomatik sinxron BUNA görə YALNIZ
                         # HƏQİQƏTƏN dəyişmiş hastaları göndərir (hər 23 hastanı
                         # yenidən göndərmək əvəzinə tez-tez amma kiçik-kiçik sinxron)

def _hasta_hash(h):
    return hashlib.sha1(json.dumps(h, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

def _hastalar_disk_yaz(data):
    """hastalar.json-u atomik yazır: əvvəlcə .tmp faylına yaz, sonra os.replace() ilə
    əsl faylın üzərinə köçür — yazma yarıda kəsilsə (cərəyan, çökmə, sinxron zamanı
    bağlama) hastalar.json ya tam yeni halı ilə qalır, ya da heç toxunulmamış qalır,
    YARIMÇIQ/korrupt olmur."""
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    tmp_yol = DATA_FILE + ".tmp"
    with open(tmp_yol, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_yol, DATA_FILE)

def veri_kaydet(data):
    """hastalar.json-un dəyişdiyini işarələyir. Faktiki (bahalı) disk yazısı DƏRHAL
    deyil — DentalApp._hastalar_save_tick tərəfindən qısa müddət sonra arxa plan
    thread-ində aparılır (bax: yuxarıdakı _HASTALAR_DIRTY şərhi). Proqram bağlananda
    (_on_close) gözləyən yazı dərhal flush edilir ki, məlumat itməsin."""
    global _CLOUD_DIRTY, _BACKUP_DIRTY, _BACKUP_LAST_CHANGE
    global _HASTALAR_DIRTY, _HASTALAR_LAST_CHANGE
    _HASTALAR_DIRTY = True
    _HASTALAR_LAST_CHANGE = time.time()
    _CLOUD_DIRTY = True
    _BACKUP_DIRTY = True
    _BACKUP_LAST_CHANGE = time.time()



class _XRayFileHandler(_FSEventHandler):
    """watchdog handler — enqueues new x-ray image files (bmp/jpg/png/tif/dcm)."""
    def __init__(self, q: queue.Queue, watch_dir: str, subdir_only: bool = True):
        super().__init__()
        self._q = q
        self._dir = watch_dir
        self._subdir_only = subdir_only
        self._seen: set = set()

    def on_created(self, event):
        if not event.is_directory:
            self._isle(event.src_path)

    def on_moved(self, event):
        # bazı programlar önce geçici dosya yazıp sonra yeniden adlandırır
        if not event.is_directory:
            self._isle(getattr(event, "dest_path", None))

    def _isle(self, path):
        if not path or not path.lower().endswith(XRAY_UZANTILAR):
            return
        if self._subdir_only:
            # kök klasördeki geçici dosyaları atla (örn. E:\DRImage\Temp.bmp)
            rel = os.path.relpath(path, self._dir)
            if os.sep not in rel:
                return
        if path in self._seen:
            return
        # yazımı bitmemiş dosyayı okuma: boyut iki ölçümde sabitlenene kadar bekle
        try:
            prev = -1
            for _ in range(30):
                cur = os.path.getsize(path)
                if cur == prev and cur > 0:
                    break
                prev = cur
                time.sleep(0.3)
        except OSError:
            return
        ext = path.lower()
        if ext.endswith(".dcm"):
            png = dcm_to_png(path)
            if not png:
                return
            path_out = png
        elif ext.endswith(RAW_UZANTILAR):
            cached = raw_cache_hazirla(path)
            if not cached:
                return
            path_out = cached
        else:
            path_out = path
        self._seen.add(path)
        self._q.put(path_out)

# ── ARCH PNG (gömülü) ─────────────────────────────────────────────────────────
ARCH_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/4gHYSUNDX1BST0ZJTEUAAQEAAAHIAAAAAAQwAABtbnRyUkdCIFhZWiAH4AABAAEAAAAAAABhY3NwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAA9tYAAQAAAADTLQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAlkZXNjAAAA8AAAACRyWFlaAAABFAAAABRnWFlaAAABKAAAABRiWFlaAAABPAAAABR3dHB0AAABUAAAABRyVFJDAAABZAAAAChnVFJDAAABZAAAAChiVFJDAAABZAAAAChjcHJ0AAABjAAAADxtbHVjAAAAAAAAAAEAAAAMZW5VUwAAAAgAAAAcAHMAUgBHAEJYWVogAAAAAAAAb6IAADj1AAADkFhZWiAAAAAAAABimQAAt4UAABjaWFlaIAAAAAAAACSgAAAPhAAAts9YWVogAAAAAAAA9tYAAQAAAADTLXBhcmEAAAAAAAQAAAACZmYAAPKnAAANWQAAE9AAAApbAAAAAAAAAABtbHVjAAAAAAAAAAEAAAAMZW5VUwAAACAAAAAcAEcAbwBvAGcAbABlACAASQBuAGMALgAgADIAMAAxADb/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEPERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCAQAAwwDASIAAhEBAxEB/8QAHQABAAICAwEBAAAAAAAAAAAAAAYIBQcDBAkCAf/EAFgQAAEEAQIEAwYCBwUDBwoBDQEAAgMEBQYRBxIhMRNBUQgUImFxgTKRFSNCUmKCoRYzcqKxJJLBF0NTY4Oy0SUmNERzk6PC4fA1NjdldHaztLXD0tPU8f/EABQBAQAAAAAAAAAAAAAAAAAAAAD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCmSIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAiIgIiICIiAv0Ak7AElZbSOnslqjUFXB4mAzXLTwyNg8z/wAABuSfIAlW94Y+zRprBeDf1TMczdaOYQNPJAw/Mj4n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Wdygbf8AOAAn6Kvuivasw9mu2PVmDmp2G7B09J3PG/1PI7q36bu+qmEntLcLGQB7chkJH7f3YpOB/r0/qgk1bLaWx2hcdonhvmLmUkgtOdDKC2aRgnteNK5742hjWgPf326bDqe+E474upf1nw3nsMa58GeYGb+hfG4j/IFH5vam0Gwnw8flpAOx5GDf/MtS8WOP8WqNSYK/icTYqwYSwLMIllBMsoexwLgOw+ADz7lBbTU2Us4XOYjJSPstxTWzx23RRueI3kMdG94aCQ0crxv2BcN1FstLwmyOYymTqcQ83p+bMnmytHBZdkcN6Qjlc90Za5zXuAAJZyk/XcqBY32rdITV2uu4HKVpSPiax7HgH5Hcb/kszQ9pzhpP0syZOp/jrcw/ykoNmae1TgcNVxmN0npjLuxeNibDTq16UkbWxj+OblBJ3JLiepO5KYHES4zB4jA4aF2MwlJ9mZ8N4tsW5XTzOlc3mYQxgDnu7c+42HTbc6r1B7UHDylVe/FxZLKTgHlYIvBa4/4ndR+RWntXe1FrrJzv/QUFLCVifhYIhNIB83u6H7Nag3r7RV+npuHRuZa7a5j80yaMD8boQP1zR8i3YH6hbCsu01qvEVbHvtezWJbYrWILPK+N2x2cx7SC07EjoexIXnXqXWOp9SZN2SzWau3LJGwdJKSGDfflaOzR8gsWMhdAIFqUA9T8XdB6H5Kjw6xoE+fyuPkaw7g5fKeK0H6SvI/oolrX2iOHmm4HQYuw/M2GN5Y46bC2Ebdh4hG23+HmVGpLdmRhZJO9zT3BK4EGyuKnGjWOvpZobdv3HFPPwUK52jaPLmPd5+Z+wC1s5xc4ucSSe5K/EQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQEREBERAREQf/2Q=="

ORIG_W, ORIG_H = 780, 1024

TEETH_COORDS = {
    11:(371,144),     12:(326,157),     13:(288,183),     14:(259,216),     15:(246,261),     16:(235,316),     17:(230,378),     18:(226,447),     21:(423,146),     22:(468,154),     23:(506,182),     24:(533,220),     25:(547,262),     26:(556,315),     27:(562,380),     28:(566,446), 
    31:(415,824),     32:(451,817),     33:(479,803),     34:(509,765),     35:(530,717),     36:(537,658),     37:(550,579),     38:(559,526), 
    41:(377,827),     42:(342,820),     43:(312,801),     44:(282,761),     45:(269,718),     46:(250,655),     47:(243,590),     48:(235,522), 
}

def dis_r(n):
    p=n%10
    if p>=6: return 58
    if p>=4: return 48
    if p==3: return 42
    return 38


OVAL_COORDS_DEFAULT = {
    11: (369, 171),
    12: (328, 172),
    13: (309, 195),
    14: (286, 220),
    15: (274, 256),
    16: (279, 310),
    17: (270, 378),
    18: (265, 443),
    21: (419, 168),
    22: (455, 174),
    23: (477, 194),
    24: (499, 220),
    25: (510, 257),
    26: (511, 310),
    27: (518, 375),
    28: (517, 439),
    31: (408, 789),
    32: (440, 784),
    33: (463, 769),
    34: (482, 740),
    35: (494, 699),
    36: (498, 640),
    37: (503, 578),
    38: (518, 520),
    41: (377, 788),
    42: (350, 785),
    43: (329, 766),
    44: (308, 743),
    45: (297, 702),
    46: (296, 642),
    47: (280, 581),
    48: (273, 522),
}
UST = [18,17,16,15,14,13,12,11,21,22,23,24,25,26,27,28]
ALT = [48,47,46,45,44,43,42,41,31,32,33,34,35,36,37,38]
DIS_AD = {1:"Santral",2:"Lateral",3:"Kanin",4:"1.Premolar",
           5:"2.Premolar",6:"1.Molar",7:"2.Molar",8:"Yirmilik"}
KADRAN = {1:"Sağ Üst",2:"Sol Üst",3:"Sol Alt",4:"Sağ Alt"}
def dis_adi(n): return f"{t(KADRAN[n//10])} {t(DIS_AD[n%10])}"

TEDAVILER = [
    ("saglikli","Sağlam", None),
    ("curuk",   "Çürük",    (150, 94,  44 )),   # kron üzerinde çürük lekeleri — yalnız İlkin Vəziyyət sekmesinde
    ("cekim",   "Çəkim",    (220, 50,  50 )),
    ("kanal",   "Kanal",    (65,  105, 225)),
    ("dolgu",   "Kompozit Dolğu",    (0,   180, 120)),
    ("kron",    "Qapaq",     (180, 50,  220)),
    ("implant", "İmplant",  (34,  139, 34 )),
    ("eksik",   "Əksik",    (128, 128, 128)),
    ("kok",     "Kök",      (184, 122, 58 )),   # kron hissəsi dağılmış diş — yalnız İlkin Vəziyyət sekmesinde
    ("gozlem",  "Gözləm",   (255, 140, 0  )),
]
T_COLOR = {t[0]: t[2] for t in TEDAVILER}

CINSIYET_SECENEKLERI = [("", "—"), ("kisi", "Kişi"), ("qadin", "Qadın")]

# Statistika filtrləri — cinsiyət (Hamısı daxil) + Black sinifləşdirməsinə görə boşluq sinfi
# (checkbox-lar — I/II-III/V eyni anda bir neçəsi seçilə bilər; heç biri seçilməyibsə = Hamısı)
STAT_CINSIYET_SECENEKLERI = [("hamisi", "Hamısı"), ("kisi", "Kişi"), ("qadin", "Qadın")]
SINIF_SECENEKLERI = [("I", "I Sinif (O)"), ("II_III", "II-III Sinif (M/D)"), ("V", "V Sinif (V)")]
SINIF_YUZEYLER = {"I": ["O"], "II_III": ["M", "D"], "V": ["V"]}

# ── SƏSLİ ASİSTENT — mətn təhlili (mikrofon/modeldən asılı olmayan xalis funksiyalar) ──
# "Plan" tabındakı düymə dəstinin eynisi (bax _tbar: tab="plan" skip şərtləri) — kok/curuk
# yalnız İlkin Vəziyyət sekmesinə aiddir, Plan/Müalicə-yə səslə yazılmır (diaqnostik
# vəziyyətlərdir, "ediləcək/edilmiş iş" deyil).
_SESLI_TEDAVI_TIDLER_PLAN = {"saglikli", "cekim", "kanal", "dolgu", "kron", "implant", "eksik", "gozlem"}
# çəkim bir müalicə əməliyyatıdır, ilkin (diaqnostik) vəziyyətə aid deyil (bax _tbar) — səsli də eyni qaydaya tabedir
_SESLI_TEDAVI_TIDLER_ILKIN = (_SESLI_TEDAVI_TIDLER_PLAN - {"cekim"}) | {"curuk", "kok"}
_SESLI_TEDAVI_TIDLER_TEDAVI = _SESLI_TEDAVI_TIDLER_PLAN
_SESLI_TAB_TIDLER = {"plan": _SESLI_TEDAVI_TIDLER_PLAN, "ilkin": _SESLI_TEDAVI_TIDLER_ILKIN,
                      "tedavi": _SESLI_TEDAVI_TIDLER_TEDAVI}
_SESLI_TEDAVI_ETIKET_ALL = {tid: lbl for tid, lbl, _ in TEDAVILER}
_SESLI_TAB_ETIKET = {"plan": "Plan", "ilkin": "İlkin Vəziyyət", "tedavi": "Müalicə"}
_SESLI_TAB_METOD = {"plan": "_uygula_plan", "ilkin": "_uygula_ilkin", "tedavi": "_uygula_tedavi"}

def _netice_qisa_metn(netice):
    """Təsdiq dialoqu açılmazdan əvvəl avatar qabarcığında göstərilən qısa
    xülasə (səsli deyil, yalnız mətn)."""
    if netice["niyyet"] == "randevu":
        return f"{netice['xeste_adi']}: {netice['tarix']} {netice['saat']} — təsdiq lazımdır"
    dis = ", ".join(str(n) for n in netice["dis_nolari"])
    lbl = _SESLI_TEDAVI_ETIKET_ALL.get(netice["tid"], netice["tid"])
    tab_lbl = _SESLI_TAB_ETIKET.get(netice["tab"], netice["tab"])
    return f"Diş {dis} → {lbl} [{tab_lbl}] — təsdiq lazımdır"

_DIS_GECERLI = set(UST + ALT)

# "#36 ..." kimi başlayan not/foto adlarından diş nömrəsini çıxarmaq üçün (bax: _not_dis_no)
_NOT_DIS_RE = re.compile(r'^\s*#\s*(\d{2})\b')

def _not_dis_no(metin):
    """'#36 kanal planı' kimi başlayan mətndən (etibarlı dişdirsə) diş nömrəsini qaytarır."""
    m = _NOT_DIS_RE.match(metin or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if n in _DIS_GECERLI else None

def _not_edilmis_disler(h):
    """Pasientin qeyd dəftərindəki (notlar_liste) '#NN' ilə başlayan qeydlərdən
    istinad olunan diş nömrələrini set olaraq qaytarır (Plan/Müalicə tablarında
    nida nişanı üçün — bax ArchCanvas.not_disler)."""
    disler = set()
    for not_ in h.get("notlar_liste", []) or []:
        if isinstance(not_, str):
            continue
        n = _not_dis_no(not_.get("baslik", "")) or _not_dis_no(not_.get("metin", ""))
        if n:
            disler.add(n)
    return disler

def _foto_dis_nomreleri(dosya_adi):
    """Foto/rentgen adındakı BÜTÜN '#NN' (və ya '#NN.M') tokenlərini (etibarlı
    diş nömrələri) sıra ilə qaytarır — çox-dişli adlar üçün (məs. '#36 #35 #34')."""
    out = []
    for tok in (dosya_adi or "").split():
        n = _not_dis_no(tok)
        if n:
            out.append(n)
    return out

def _foto_ilk_dis(dosya_adi):
    """Foto adındakı İLK (əsas) diş nömrəsini qaytarır — adlandırmada ardıcıl
    nömrələmə (#36 -> #36.1 -> #36.2) bu əsas nömrəyə görə aparılır."""
    nos = _foto_dis_nomreleri(dosya_adi)
    return nos[0] if nos else None

AZ_BIRLIK = {"bir": 1, "iki": 2, "üç": 3, "uc": 3, "dörd": 4, "dord": 4,
             "beş": 5, "bes": 5, "altı": 6, "alti": 6, "yeddi": 7,
             "səkkiz": 8, "sekkiz": 8, "doqquz": 9,
             # sıra sayları ("16-cı diş" təbii nitqdə "on altıncı diş" kimi deyilir —
             # yalnız son hissə sıra şəkilçisi alır, "on" dəyişmir)
             "birinci": 1, "ikinci": 2, "üçüncü": 3, "ucuncu": 3,
             "dördüncü": 4, "dorduncu": 4, "beşinci": 5, "besinci": 5,
             "altıncı": 6, "alticinci": 6, "yeddinci": 7,
             "səkkizinci": 8, "sekkizinci": 8, "doqquzuncu": 9}
AZ_ONLUK = {"on": 10, "iyirmi": 20, "otuz": 30, "qırx": 40, "qirx": 40}

AZ_HEFTE_GUNLERI = {
    "bazar ertəsi": 0, "bazar ertesi": 0, "çərşənbə axşamı": 1, "cersenbe axsami": 1,
    "çərşənbə": 2, "cersenbe": 2, "cümə axşamı": 3, "cume axsami": 3,
    "cümə": 4, "cume": 4, "şənbə": 5, "senbe": 5, "bazar": 6,
}

_AZ_TRANSLIT = str.maketrans({"ə": "e", "ı": "i", "ö": "o", "ü": "u", "ş": "s", "ç": "c", "ğ": "g"})

def _az_sade(s):
    """Azərbaycan hərflərini (ə/ı/ö/ü/ş/ç/ğ) ASCII qarşılıqlarına çevirir — Whisper
    STT-nin diakritikləri tutarsız yazması (məs. "dolğu" / "dolgu") üzündən sözlərin
    tanınmaması problemini aradan qaldırmaq üçün, mətn müqayisələrindən əvvəl tətbiq edilir."""
    return s.lower().translate(_AZ_TRANSLIT)

def _metinden_dis_no(text):
    """Mətndə keçən keçərli FDI diş nömrələrini (11-18,21-28,31-38,41-48) tapır —
    rəqəmlə ("16") və ya söz-rəqəmlə ("on altı") yazılmış formaları dəstəkləyir."""
    tapilanlar = []
    for raw in re.findall(r"\d{1,2}", text):
        n = int(raw)
        if n in _DIS_GECERLI and n not in tapilanlar:
            tapilanlar.append(n)
    sozler = re.findall(r"[a-zçəğıöşüA-ZÇƏĞIÖŞÜ]+", text.lower())
    i = 0
    while i < len(sozler):
        soz = sozler[i]
        if soz in AZ_ONLUK:
            deger = AZ_ONLUK[soz]
            if i + 1 < len(sozler) and sozler[i + 1] in AZ_BIRLIK:
                deger += AZ_BIRLIK[sozler[i + 1]]
                i += 1
            if deger in _DIS_GECERLI and deger not in tapilanlar:
                tapilanlar.append(deger)
        i += 1
    return tapilanlar

def _metinden_tedavi(text, tid_set):
    """Mətndə TEDAVILER etiketlərindən, verilən `tid_set` (tab-a uyğun icazəli tid-lər)
    içindəkilərdən birinin keçib-keçmədiyini yoxlayır — tapılarsa tid qaytarır,
    tapılmazsa None."""
    alcaq = _az_sade(text)
    for tid in tid_set:
        lbl = _az_sade(_SESLI_TEDAVI_ETIKET_ALL[tid])
        if lbl in alcaq:
            return tid
    # tam etiketdən qısa, təbii tələffüz sinonimləri (hamısı _az_sade ilə artıq
    # diakritiksiz müqayisə olunur, ona görə "dolğu"/"dolgu" eyni "dolgu" kimi tutulur)
    SINONIM = {"dolgu": "dolgu", "qapaq": "kron", "implant": "implant",
               "cekim": "cekim", "eksik": "eksik", "gozlem": "gozlem", "saglam": "saglikli",
               "curuk": "curuk", "kok": "kok"}
    for soz, tid in SINONIM.items():
        if soz in alcaq and tid in tid_set:
            return tid
    return None

def _metinden_randevu_tarixi(text, indi=None):
    """Mətndəki 'bu gün/sabah/birigün/həftənin günü' ifadəsini ISO tarixə çevirir.
    Heç biri tapılmazsa bugünkü tarixi qaytarır (saat ayrıca təhlil olunur)."""
    indi = indi or datetime.now()
    alcaq = text.lower()
    if "birigün" in alcaq or "o biri gün" in alcaq or "o biri gun" in alcaq:
        return (indi + timedelta(days=2)).date().isoformat()
    if "sabah" in alcaq:
        return (indi + timedelta(days=1)).date().isoformat()
    if "bu gün" in alcaq or "bu gun" in alcaq or "bugün" in alcaq:
        return indi.date().isoformat()
    for gun_adi, hedef_weekday in AZ_HEFTE_GUNLERI.items():
        if gun_adi in alcaq:
            fark = (hedef_weekday - indi.weekday()) % 7
            fark = fark or 7   # eyni gün deyilibsə NÖVBƏTİ həftəyə düşsün
            return (indi + timedelta(days=fark)).date().isoformat()
    return indi.date().isoformat()

def _metinden_saat(text):
    """Mətndəki 'saat N' / 'saat N:MM' ifadəsini 'HH:MM' formatına çevirir, tapılmazsa None."""
    m = re.search(r"saat\s+(\d{1,2})[:\.]?(\d{2})?", text.lower())
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2)) if m.group(2) else 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{hh:02d}:{mm:02d}"

def _metinden_xeste_adi(text, hastalar):
    """Mətndə bilinən pasient adlarından biri keçirsə onu qaytarır (tapılmazsa None)."""
    alcaq = text.lower()
    adlar = sorted({h.get("ad", "") for h in hastalar.values() if h.get("ad")},
                   key=len, reverse=True)   # uzun adlar əvvəlcə — qismən üst-üstə düşməni önləyir
    for ad in adlar:
        if ad.lower() in alcaq:
            return ad
    return None

_AD_UCUN_RE = re.compile(r"([a-zA-ZçəğıöşüÇƏĞIÖŞÜ]+(?:\s+[a-zA-ZçəğıöşüÇƏĞIÖŞÜ]+)?)\s+üçün\b",
                          re.IGNORECASE)

def _metinden_yeni_xeste_adi(text):
    """Mətndə mövcud siyahıda OLMAYAN (yeni) pasient adı namizədini "<Ad> üçün" qəlibi
    ilə çıxarır (məs. "Aysel üçün on altıncı dişə kanal et"). Təsadüfi sözlərin ad kimi
    tutulmasının qarşısını almaq üçün YALNIZ bu aydın dil qəlibi qəbul edilir — sərbəst
    söz axtarışı yoxdur. Tapılmazsa None qaytarır."""
    m = _AD_UCUN_RE.search(text)
    if not m:
        return None
    ad = m.group(1).strip()
    return ad.title() if ad else None

def _oyadici_soz_tap(text, oyadici):
    """Mətndə oyadıcı sözü tapmağa çalışır. Əvvəlcə tam substring, tapılmazsa
    mətnin ilk bir neçə sözündən STT-nin fonetik səhvlərinə dözümlü bulanıq
    uyğunlaşdırma (SequenceMatcher) ilə axtarır (məs. "bulex" Whisper
    tərəfindən "buleks"/"bules" kimi yazıla bilər). Tapılarsa mətndəki bitmə
    indeksini (əmrin başladığı yer), tapılmazsa None qaytarır."""
    alcaq = _az_sade(text)
    oyadici = _az_sade(oyadici)
    idx = alcaq.find(oyadici)
    if idx != -1:
        return idx + len(oyadici)
    en_yaxsi = None
    sozler = list(re.finditer(r"[a-z]+", alcaq))[:5]
    # tək sözlər (Whisper oyadıcı sözü tam tutarsa)
    for m in sozler:
        soz = m.group()
        oxsarlik = difflib.SequenceMatcher(None, soz, oyadici).ratio()
        if oxsarlik >= 0.55 and (en_yaxsi is None or oxsarlik > en_yaxsi[0]):
            en_yaxsi = (oxsarlik, m.end())
    # qonşu söz cütləri (Whisper oyadıcı sözü iki hissəyə bölübsə, məs. "bu" + "leks")
    for m1, m2 in zip(sozler, sozler[1:]):
        cut = alcaq[m1.start():m2.end()]
        oxsarlik = difflib.SequenceMatcher(None, cut, oyadici).ratio()
        if oxsarlik >= 0.55 and (en_yaxsi is None or oxsarlik > en_yaxsi[0]):
            en_yaxsi = (oxsarlik, m2.end())
    return en_yaxsi[1] if en_yaxsi else None

def parse_sesli_emr(text, hastalar, aktif_hasta_adi=None):
    """Oyadıcı sözdən sonrakı mətni təhlil edib niyyəti müəyyən edir. Qaytarır:
    {"niyyet": "randevu", "xeste_adi", "yeni_pasient", "tarix", "saat"} və ya
    {"niyyet": "plan", "tab": "plan"/"ilkin"/"tedavi", "dis_nolari": [...], "tid",
     "xeste_adi", "yeni_pasient"} və ya None (anlaşılmadı)."""
    if not text or not text.strip():
        return None
    alcaq = _az_sade(text)
    tarix_bulundu = _metinden_saat(text) is not None or "randevu" in alcaq \
        or "sabah" in alcaq or "bugun" in alcaq or "bu gun" in alcaq \
        or "birigun" in alcaq or any(_az_sade(g) in alcaq for g in AZ_HEFTE_GUNLERI)
    if "ilkin" in alcaq:
        tab = "ilkin"
    elif "mualice" in alcaq or "edildi" in alcaq or "tamamlandi" in alcaq:
        tab = "tedavi"
    else:
        tab = "plan"
    dis_nolari = _metinden_dis_no(text)
    tid = _metinden_tedavi(text, _SESLI_TAB_TIDLER[tab])
    bilinen_ad = _metinden_xeste_adi(text, hastalar)
    yeni_ad = None if bilinen_ad else _metinden_yeni_xeste_adi(text)
    yeni_pasient = bool(yeni_ad and not bilinen_ad)
    if tarix_bulundu and not (dis_nolari and tid):
        xeste_adi = bilinen_ad or yeni_ad or aktif_hasta_adi
        if not xeste_adi:
            return None
        return {
            "niyyet": "randevu",
            "xeste_adi": xeste_adi,
            "yeni_pasient": yeni_pasient,
            "tarix": _metinden_randevu_tarixi(text),
            "saat": _metinden_saat(text) or "09:00",
        }
    if dis_nolari and tid:
        return {
            "niyyet": "plan",
            "tab": tab,
            "dis_nolari": dis_nolari,
            "tid": tid,
            "xeste_adi": bilinen_ad or yeni_ad,
            "yeni_pasient": yeni_pasient,
        }
    return None

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-5-5"

_CLAUDE_TOOL = {
    "name": "sesli_emr_qeydi",
    "description": "Azərbaycan dilində stomatoloji səsli əmrdən çıxarılan struktur məlumat.",
    "input_schema": {
        "type": "object",
        "properties": {
            "niyyet": {"type": "string", "enum": ["plan", "randevu", "anlasilmadi"],
                       "description": "'plan' = diş/müalicə əmri, 'randevu' = görüş təyini, "
                                       "'anlasilmadi' = məna çıxarıla bilmədi"},
            "tab": {"type": "string", "enum": ["plan", "ilkin", "tedavi"],
                    "description": "niyyet='plan' olanda: 'plan'=ediləcək iş (defolt), "
                                    "'ilkin'=hazırkı/ilkin vəziyyət (çürük/kök qalığı DAXİL), "
                                    "'tedavi'=artıq edilmiş iş (tarixçə)"},
            "dis_nolari": {"type": "array", "items": {"type": "integer"},
                           "description": "FDI diş nömrələri (11-18,21-28,31-38,41-48)"},
            "tid": {"type": "string",
                    "enum": ["saglikli", "cekim", "kanal", "dolgu", "kron", "implant",
                              "eksik", "gozlem", "curuk", "kok"],
                    "description": "'curuk'/'kok' YALNIZ tab='ilkin' olanda etibarlıdır"},
            "xeste_adi": {"type": ["string", "null"],
                          "description": "mətndə '<ad> üçün' DEYİLİBSƏ o ad; yoxdursa null "
                                          "(aktiv pasientə düşəcək)"},
            "yeni_pasient": {"type": "boolean",
                              "description": "xeste_adi verilən MÖVCUD pasientlər siyahısında "
                                              "yoxdursa true"},
            "tarix": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD (randevu üçün)"},
            "saat": {"type": ["string", "null"], "description": "HH:MM (randevu üçün)"},
        },
        "required": ["niyyet"],
    },
}

def _claude_parse_emr(text, hastalar, aktif_hasta_adi, api_key):
    """Whisper-dən gələn mətni Anthropic Claude API-sinə (Haiku 5.5) göndərib
    struktur əmr çıxarır — yerli regex/sinonim sisteminə (parse_sesli_emr)
    ALTERNATİV, daha çevik təhlil üçün. İstifadəçi öz API açarını Ayarlar-da
    daxil etdikdə işə düşür; şəbəkə/açar xətasında None qaytarır ki, çağıran
    tərəf yerli təhlilə geri qaytsın (offline fallback həmişə qalır)."""
    if not api_key or not text or not text.strip():
        return None
    import urllib.request, urllib.error, socket
    adlar = sorted({h.get("ad", "") for h in hastalar.values() if h.get("ad")})
    indi = datetime.now()
    sistem = (
        "Sən stomatoloji proqram üçün səsli əmrləri təhlil edən köməkçisən. "
        f"Bugünkü tarix: {indi.strftime('%Y-%m-%d')}. "
        "Mövcud pasient adları: " + (", ".join(adlar) if adlar else "(siyahı boşdur)") + ". "
        "Mətndə ad '<Ad> üçün' kimi aydın deyilməyibsə xeste_adi-ni null saxla. "
        "Diş nömrələri yalnız 11-18,21-28,31-38,41-48 ola bilər. "
        "VACİB: niyyet='plan' olanda tid VƏ dis_nolari sahələri HƏMİŞƏ doldurulmalıdır "
        "(heç vaxt boş qoyma) — tid mətndə deyilən müalicə/vəziyyətin kodu olmalıdır. "
        "Əmrin mənası çıxarıla bilmirsə niyyet='anlasilmadi' qaytar."
    )
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 512,
        "system": sistem,
        "tools": [_CLAUDE_TOOL],
        "tool_choice": {"type": "tool", "name": "sesli_emr_qeydi"},
        "messages": [{"role": "user", "content": text}],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2026-10-21",
        },
    )
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(20)
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl_baglam()) as resp:
            res = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API HTTP {e.code}: {e.read().decode('utf-8','ignore')[:200]}")
    finally:
        socket.setdefaulttimeout(prev_timeout)

    tool_block = next((b for b in res.get("content", []) if b.get("type") == "tool_use"), None)
    if not tool_block:
        return None
    return _claude_tool_girisini_neticeye_cevir(tool_block.get("input", {}), hastalar, aktif_hasta_adi, indi)

def _claude_tool_girisini_neticeye_cevir(r, hastalar, aktif_hasta_adi, indi):
    """`_CLAUDE_TOOL` sxemi ilə Claude-un qaytardığı xam `input` dict-ini
    (`_claude_parse_emr` VƏ ümumi söhbət rejimindəki `_claude_chat_cavab`
    arasında ORTAQ) yoxlanmış `netice` dict-inə çevirir, ya da keçərsiz
    olduqda `None` qaytarır. Claude-un nəticəsinə kor-koranə etibar edilmir —
    dis_nolari/tid/xeste_adi/yeni_pasient həmişə YERLİ qaydalara qarşı
    yoxlanılır (xüsusən yeni pasient yaratma/randevu kimi real-data təsirli
    əməllər üçün)."""
    niyyet = r.get("niyyet")
    if niyyet not in ("plan", "randevu"):
        return None
    adlar = sorted({h.get("ad", "") for h in hastalar.values() if h.get("ad")})

    xeste_adi = r.get("xeste_adi") or None
    yeni_pasient = bool(r.get("yeni_pasient")) if xeste_adi else False
    if xeste_adi and xeste_adi in adlar:
        yeni_pasient = False

    if niyyet == "randevu":
        xeste_adi = xeste_adi or aktif_hasta_adi
        if not xeste_adi:
            return None
        tarix = r.get("tarix")
        try:
            datetime.strptime(tarix, "%Y-%m-%d")
        except Exception:
            tarix = indi.date().isoformat()
        saat = r.get("saat")
        if not saat or not re.match(r"^\d{2}:\d{2}$", saat):
            saat = "09:00"
        return {"niyyet": "randevu", "xeste_adi": xeste_adi, "yeni_pasient": yeni_pasient,
                "tarix": tarix, "saat": saat}

    tab = r.get("tab") if r.get("tab") in _SESLI_TAB_TIDLER else "plan"
    tid = r.get("tid")
    if tid not in _SESLI_TAB_TIDLER[tab]:
        return None
    dis_nolari = [n for n in (r.get("dis_nolari") or []) if n in _DIS_GECERLI]
    if not dis_nolari:
        return None
    return {"niyyet": "plan", "tab": tab, "dis_nolari": dis_nolari, "tid": tid,
            "xeste_adi": xeste_adi, "yeni_pasient": yeni_pasient}

_CHAT_SISTEM_PROMPT_SABLON = (
    "Sən stomatoloji klinikanın proqramında işləyən AI köməkçisən, səsli söhbət "
    "rejimindəsən (cavabın ucadan səsləndirilir). Bugünkü tarix: {tarix}. "
    "Mövcud pasient adları: {adlar}. Hazırkı aktiv pasient: {aktif}. "
    "QAYDALAR: cavabların 1-2 qısa cümlə olsun, heç vaxt siyahı/markdown/* "
    "işlətmə, rəqəmləri sözlə yaz (məsələn '16' yerinə 'on altı'). "
    "İstifadəçi aydın şəkildə dental əməliyyat (diş/müalicə qeydi və ya randevu) "
    "İSTƏDİKDƏ 'sesli_emr_qeydi' alətini çağır — əks halda sadəcə söhbət et, "
    "sual-cavab ver, alət çağırma. "
    "VACİB: alət çağırarkən niyyet='plan' olanda tid VƏ dis_nolari sahələri "
    "HƏMİŞƏ doldurulmalıdır (heç vaxt boş qoyma) — tid mətndə deyilən "
    "müalicə/vəziyyətin kodu olmalıdır."
)

def _claude_chat_cavab(mesaj, hastalar, aktif_hasta_adi, api_key, tarixce):
    """Ümumi (push-to-talk) söhbət rejimi üçün Claude-a sorğu göndərir.
    `_claude_parse_emr`-dən fərqli olaraq `tool_choice` MƏCBURİ deyil — Claude
    ya sadə mətnlə cavablandırır (ümumi söhbət), ya da aydın dental əmr
    eşitdikdə `_CLAUDE_TOOL`-u çağırır. Qaytarır: `("text", metn)` və ya
    `("tool", netice)`; netice keçərsiz olarsa `("text", fallback_metn)`."""
    import urllib.request, urllib.error, socket
    indi = datetime.now()
    adlar = sorted({h.get("ad", "") for h in hastalar.values() if h.get("ad")})
    sistem = _CHAT_SISTEM_PROMPT_SABLON.format(
        tarix=indi.strftime("%Y-%m-%d"),
        adlar=(", ".join(adlar) if adlar else "(siyahı boşdur)"),
        aktif=(aktif_hasta_adi or "(seçilməyib)"),
    )
    messages = list(tarixce) + [{"role": "user", "content": mesaj}]
    body = {
        "model": ANTHROPIC_MODEL,
        # 400 token-lıq ilkin limit real sınaqda arabir qırılmağa səbəb oldu —
        # bu model tool_choice MƏCBURİ olmayanda bəzən "thinking" blokuna da
        # xeyli token sərf edir, nəticədə alət-çağırışının input-u yarımçıq
        # kəsilirdi (məs. dis_nolari/tid boş qalırdı).
        "max_tokens": 1024,
        "system": sistem,
        "tools": [_CLAUDE_TOOL],
        "messages": messages,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_API_URL, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2026-10-21",
        },
    )
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(25)
    try:
        with urllib.request.urlopen(req, timeout=25, context=ssl_baglam()) as resp:
            res = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API HTTP {e.code}: {e.read().decode('utf-8','ignore')[:200]}")
    finally:
        socket.setdefaulttimeout(prev_timeout)

    tool_block = next((b for b in res.get("content", []) if b.get("type") == "tool_use"), None)
    if tool_block:
        netice = _claude_tool_girisini_neticeye_cevir(tool_block.get("input", {}), hastalar, aktif_hasta_adi, indi)
        if netice:
            return ("tool", netice)
    metin = " ".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text").strip()
    return ("text", metin or t("Başa düşmədim, bir daha deyə bilərsiniz?"))

_TTS_MIXER_HAZIR = {"ok": False}

def _tts_de(metin, dayandir_event=None):
    """Mətni edge-tts ilə (bulud, internet lazımdır) səsləndirib pygame.mixer
    ilə çalır. Bütün gövdə qəsdən try/except-ə bükülüb: internet/xəta olsa
    SƏSSİZCƏ keçilir (mətn artıq çat pəncərəsində göstərilib — bu, offline
    fallback-ın yeganə forması). `dayandir_event` set olunarsa (yeni
    push-to-talk başladığında) çalma dərhal kəsilir."""
    try:
        import asyncio, tempfile, edge_tts, pygame
        voice = tts_ses_adi()

        async def _yarat(path):
            comm = edge_tts.Communicate(metin, voice)
            await comm.save(path)

        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="bluex_tts_")
        os.close(fd)
        try:
            asyncio.run(_yarat(path))
            if not _TTS_MIXER_HAZIR["ok"]:
                pygame.mixer.init()
                _TTS_MIXER_HAZIR["ok"] = True
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if dayandir_event is not None and dayandir_event.is_set():
                    pygame.mixer.music.stop()
                    break
                time.sleep(0.1)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
    except Exception as e:
        print(f"[TTS] səsləndirilmədi (internet/xəta?): {e}")

# ── SƏSLİ ASİSTENT AVATARI — yarı-həkim/yarı-robot üz (PIL ilə çəkilir, keşlənir) ──
_SESLI_AVATAR_CACHE = {}
_AVATAR_RENK = {"idle": (140, 140, 145), "listening": (46, 204, 113), "danisir": (52, 152, 219)}

def _sesli_avatar_uz():
    """İstifadəçinin öz şəklini (`avatar_robot.png` — ağ fonu şəffaflaşdırılmış,
    kənarları kəsilmiş robot-həkim stiker) bir dəfə yükləyib uyğun ölçüyə
    gətirir və keşləyir."""
    if "uz" in _SESLI_AVATAR_CACHE:
        return _SESLI_AVATAR_CACHE["uz"]
    img = Image.open(AVATAR_FILE).convert("RGBA")
    hedef_h = 190
    oran = hedef_h / img.height
    img = img.resize((max(1, int(img.width * oran)), hedef_h), Image.LANCZOS)
    _SESLI_AVATAR_CACHE["uz"] = img
    return img

def _sesli_avatar_halqali(renk, parlaqliq=1.0, pad=22):
    """Üz şəklinin ətrafına rəngli (hal göstərən) dairəvi-küncli çərçivə əlavə
    edir — nəbz animasiyası üçün hər çağırışda yeni tərkib (üz özü keşlənib,
    ucuz əməliyyatdır)."""
    uz = _sesli_avatar_uz()
    Sw, Sh = uz.width + pad * 2, uz.height + pad * 2
    out = Image.new("RGBA", (Sw, Sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    alpha = max(60, min(255, int(255 * parlaqliq)))
    d.rounded_rectangle([4, 4, Sw - 4, Sh - 4], radius=28, outline=tuple(renk) + (alpha,), width=7)
    out.alpha_composite(uz, (pad, pad))
    return out

_WHISPER_MODEL_CACHE = {}

def _whisper_model_dir():
    return os.path.join(os.path.dirname(DATA_FILE), "whisper_models")

def _whisper_cihaz_sec():
    """CUDA mövcuddursa daha dəqiq 'medium'+float16 modelini GPU-da, yoxdursa
    'small'+int8 modelini CPU-da seçir. ctranslate2 import/sorğu xətası
    (CUDA DLL-ləri yoxdur və s.) həmişə CPU-ya təhlükəsiz geri qayıdışdır."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "medium", "cuda", "float16"
    except Exception:
        pass
    return "small", "cpu", "int8"

def _whisper_model_yukle():
    """Whisper modelini bir dəfə yükləyib keşləyir (çağırışlar arası təkrar
    yüklənmir). İlk çağırışda model yerli keşdə yoxdursa internetdən endirilir
    (bir dəfəlik, ölçü GPU/CPU seçiminə görə dəyişir) — bu funksiya arxa-plan
    thread-dən çağırılmalıdır, UI thread-ini bloklamasın deyə."""
    if "model" not in _WHISPER_MODEL_CACHE:
        from faster_whisper import WhisperModel
        kesh_dir = _whisper_model_dir()
        os.makedirs(kesh_dir, exist_ok=True)
        olcu, cihaz, hesab_novu = _whisper_cihaz_sec()
        model = None
        if cihaz == "cuda":
            try:
                model = WhisperModel(olcu, device=cihaz, compute_type=hesab_novu, download_root=kesh_dir)
                # `ctranslate2.get_cuda_device_count()` kart/sürücünü görsə də, HƏQİQİ
                # inference üçün lazım olan runtime DLL-ləri (cuBLAS/cuDNN) çatışmaya
                # bilər — bu xəta yalnız transkripsiya ZAMANI üzə çıxır, konstruktor
                # zamanı yox. Ona görə kiçik (1 saniyəlik sükut) sınaq transkripsiya
                # ilə əvvəlcədən doğrulanır; uğursuz olsa təhlükəsiz CPU-ya keçilir.
                import numpy as _np_sinaq_mod
                list(model.transcribe(_np_sinaq_mod.zeros(16000, dtype="float32"), language="az")[0])
            except Exception as e:
                print(f"[Whisper] CUDA runtime işləmədi, CPU-ya keçilir: {e}")
                model = None
        if model is None:
            try:
                model = WhisperModel(olcu if cihaz != "cuda" else "small",
                                      device="cpu", compute_type="int8" if cihaz == "cuda" else hesab_novu,
                                      download_root=kesh_dir)
            except Exception as e:
                print(f"[Whisper] CPU ilə də yüklənmədi: {e}")
                raise
        _WHISPER_MODEL_CACHE["model"] = model
    return _WHISPER_MODEL_CACHE["model"]

# Her tedavinin hangi katmana uygulandığı
# "tac"=diş gövdesi, "kanal"=oval indikatör, "kron"=border
TEDAVI_KATMAN = {
    "saglikli": "temizle",
    "cekim":    "tac",
    "kanal":    "kanal",
    "dolgu":    "tac",
    "kron":     "kron",
    "implant":  "tac",
    "eksik":    "tac",
    "kok":      "tac",
    "curuk":    "kron",   # bağımsız katman: kron gibi herhangi bir tac ile birleşebilir
    "gozlem":   "tac",
}

# Arch merkezi (oval yönü için)
ARCH_CX, ARCH_CY = 395, 487

def dis_tedavi_default():
    return {"tac": "saglikli", "kanal": False, "kron": False}

YUZEYLER = ["M", "D", "O", "B", "L", "V"]   # Mesial, Distal, Oklüzal, Bukkal, Lingual/Palatal, V Sınıf (Servikal)
YUZEY_AD = {"M": "Mesial", "D": "Distal", "O": "Oklüzal", "B": "Bukkal",
            "L": "Lingual/Palatal", "V": "Servikal (V Sınıf)"}

def _norm_yuz(v):
    """Eski bool format (True=tüm yüzeyler) veya liste/set'i standart sıralı listeye çevirir."""
    if v is True: return list(YUZEYLER)
    if not v: return []
    return [y for y in YUZEYLER if y in v]

def dis_durum_str(durum):
    """Diş durumunu okunabilir string'e çevir"""
    parcalar = []
    if durum.get("kanal"): parcalar.append(t("Kanal"))
    tac = durum.get("tac","saglikli")
    # eski veri: "curuk"/"dolgu" doğrudan tac olarak saklanmış olabilir
    if tac == "curuk":
        parcalar.append(t("Çürük"))
    elif tac == "dolgu":
        parcalar.append(t("Kompozit Dolğu"))
    elif tac != "saglikli":
        for tid,lbl,_ in TEDAVILER:
            if tid == tac:
                parcalar.append(t(lbl))
                break
    if durum.get("dolgu") and tac != "dolgu": parcalar.append(t("Kompozit Dolğu") + "(" + "".join(durum["dolgu"]) + ")")
    if durum.get("curuk") and tac != "curuk": parcalar.append(t("Çürük") + "(" + "".join(durum["curuk"]) + ")")
    if durum.get("kron"): parcalar.append(t("Qapaq"))
    return " + ".join(parcalar) if parcalar else t("Sağlam")

def normalize_state(s):
    if isinstance(s, str):
        if s == "saglikli": return {"kanal":False,"kron":False,"curuk":[],"dolgu":[],"tac":None}
        if s == "kanal":    return {"kanal":True, "kron":False,"curuk":[],"dolgu":[],"tac":None}
        if s == "kron":     return {"kanal":False,"kron":True, "curuk":[],"dolgu":[],"tac":None}
        if s == "curuk":    return {"kanal":False,"kron":False,"curuk":list(YUZEYLER),"dolgu":[],"tac":None}
        if s == "dolgu":    return {"kanal":False,"kron":False,"curuk":[],"dolgu":list(YUZEYLER),"tac":None}
        return {"kanal":False,"kron":False,"curuk":[],"dolgu":[],"tac":s}
    if isinstance(s, dict):
        tac = s.get("tac")
        curuk = _norm_yuz(s.get("curuk", []))
        dolgu = _norm_yuz(s.get("dolgu", []))
        if tac == "saglikli": tac = None
        if tac == "curuk":     # eski veri: çürük tac olarak kaydedilmişti — bağımsız bayrağa taşı
            tac = None
            if not curuk: curuk = list(YUZEYLER)
        if tac == "dolgu":     # eski veri: dolgu tüm dişi kaplayan tac idi — tüm yüzeylere taşı
            tac = None
            if not dolgu: dolgu = list(YUZEYLER)
        return {"kanal":s.get("kanal",False),"kron":s.get("kron",False),"curuk":curuk,"dolgu":dolgu,"tac":tac}
    return {"kanal":False,"kron":False,"curuk":[],"dolgu":[],"tac":None}

def state_to_label(s):
    ns = normalize_state(s)
    parts = []
    if ns["kanal"]: parts.append(t("Kanal"))
    if ns["tac"]:
        lbl = next((x[1] for x in TEDAVILER if x[0]==ns["tac"]), ns["tac"].capitalize())
        parts.append(t(lbl))
    if ns["kron"]: parts.append(t("Qapaq"))
    return " + ".join(parts) if parts else t("Sağlam")

# ── ARCH GÖRÜNTÜSÜ VE MASKELER ────────────────────────────────────────────────
_arch_base = None   # orijinal numpy array
_oval_coords: dict = {}   # {tooth_num: (ox, oy)} — load_arch'ta doldurulur
_masks     = None   # {n: bool mask}
_nums      = None   # sorted tooth numbers

def load_arch():
    global _arch_base, _masks, _nums
    if _arch_base is not None: return
    from scipy.ndimage import label as sp_label
    from collections import defaultdict
    
    img_data = base64.b64decode(ARCH_B64)
    img = Image.open(BytesIO(img_data)).convert("RGB")
    _arch_base = np.array(img)
    H, W = _arch_base.shape[:2]
    gray = _arch_base.mean(axis=2)

    # Diş gövdesi pikselleri (arka plan ve koyu outline hariç)
    tooth_body = gray > 100

    # Bağlı bileşenler
    labeled, _ = sp_label(tooth_body)

    # Her diş için bileşen label'i bul
    tooth_labels = {}
    for n, (cx, cy) in TEETH_COORDS.items():
        lbl = labeled[cy, cx]
        if lbl == 0:
            for r in range(1, 25):
                found = False
                for dy in range(-r, r+1):
                    for dx in range(-r, r+1):
                        if abs(dy)==r or abs(dx)==r:
                            ny, nx = cy+dy, cx+dx
                            if 0<=ny<H and 0<=nx<W and labeled[ny,nx]>0:
                                lbl = labeled[ny,nx]; found = True; break
                    if found: break
                if found: break
        tooth_labels[n] = lbl

    # Aynı label'i paylaşan dişler
    label_to_teeth = defaultdict(list)
    for n, lbl in tooth_labels.items():
        label_to_teeth[lbl].append(n)

    _nums = sorted(TEETH_COORDS.keys())
    _masks = {}
    
    # Oval koordinatları — önce JSON dosyasına bak, yoksa gömülü default
    global _oval_coords
    oval_path = os.path.join(SCRIPT_DIR, "oval_coords.json")
    if os.path.exists(oval_path):
        raw = json.load(open(oval_path))
        _oval_coords = {int(k): tuple(v) for k,v in raw.items()}
    else:
        _oval_coords = {k: v for k, v in OVAL_COORDS_DEFAULT.items()}
    for lbl, teeth in label_to_teeth.items():
        comp_mask = (labeled == lbl)
        if len(teeth) == 1:
            _masks[teeth[0]] = comp_mask
        else:
            # Paylaşılan komponent — Voronoi ile böl
            comp_ys, comp_xs = np.where(comp_mask)
            for n in teeth: _masks[n] = np.zeros((H,W), dtype=bool)
            for py, px in zip(comp_ys, comp_xs):
                min_d, min_n = float("inf"), None
                for n in teeth:
                    cx, cy = TEETH_COORDS[n]
                    d = math.sqrt((px-cx)**2+(py-cy)**2)
                    if d < min_d: min_d=d; min_n=n
                _masks[min_n][py, px] = True

_img_cache = {}

def oval_pozisyon(n, oval_r=12):
    """_oval_coords'tan al, yoksa geometrik hesapla"""
    if n in _oval_coords:
        return _oval_coords[n]
    # Fallback: geometrik
    cx, cy = TEETH_COORDS[n]
    dx = ARCH_CX - cx; dy = ARCH_CY - cy
    mag = math.sqrt(dx*dx + dy*dy)
    if mag == 0: return cx, cy
    ux, uy = dx/mag, dy/mag
    r = dis_r(n)
    return int(cx + ux*(r+oval_r+4)), int(cy + uy*(r+oval_r+4))

def _catmull_rom_seg(p0, p1, p2, p3, n=30):
    pts = []
    for i in range(n):
        t = i / n
        t2, t3 = t*t, t*t*t
        x = 0.5*((2*p1[0])+(-p0[0]+p2[0])*t+(2*p0[0]-5*p1[0]+4*p2[0]-p3[0])*t2+(-p0[0]+3*p1[0]-3*p2[0]+p3[0])*t3)
        y = 0.5*((2*p1[1])+(-p0[1]+p2[1])*t+(2*p0[1]-5*p1[1]+4*p2[1]-p3[1])*t2+(-p0[1]+3*p1[1]-3*p2[1]+p3[1])*t3)
        pts.append((int(x), int(y)))
    return pts

def _smooth_chain(pts, n=30):
    if len(pts) < 2: return pts
    chain = [pts[0]] + list(pts) + [pts[-1]]
    result = []
    for i in range(1, len(chain)-2):
        result.extend(_catmull_rom_seg(chain[i-1], chain[i], chain[i+1], chain[i+2], n))
    result.append(pts[-1])
    return result

# Every-other-tooth keypoints for each arch — fewer control points → smoother spline.
# Uniform 75 px offset from tooth center (no dis_r dependency) → eliminates the
# tooth-size-driven wobble that made the old 16-point arc look jagged.
_UST_KEYS  = [18, 16, 14, 12, 11, 21, 23, 25, 27, 28]
_ALT_KEYS  = [48, 46, 44, 42, 41, 31, 33, 35, 37, 38]
_GUM_MARGIN = 75   # px from tooth center in arch-outward direction

def _draw_gum_arc(draw, tooth_keys, color=(160, 32, 240), width=4):
    pts = []
    for n in tooth_keys:
        cx, cy = TEETH_COORDS[n]
        dx = cx - ARCH_CX; dy = cy - ARCH_CY
        mag = math.sqrt(dx*dx + dy*dy) or 1
        pts.append((int(cx + dx/mag*_GUM_MARGIN), int(cy + dy/mag*_GUM_MARGIN)))
    if len(pts) < 2: return
    smooth = _smooth_chain(pts, n=30)
    if len(smooth) >= 2:
        draw.line(smooth, fill=color, width=width)

def get_arch_photo(tedaviler, display_w, display_h, dis_tasi=False):
    # tedaviler = {str(n): {"tac":..., "kanal":bool, "kron":bool}}
    key_items = []
    for k,v in sorted(tedaviler.items()):
        if isinstance(v, dict):
            key_items.append((str(k), v.get("tac",""), v.get("kanal",False), v.get("kron",False)))
        else:
            key_items.append((str(k), str(v), False, False))
    oval_hash = hash(tuple(sorted(_oval_coords.items())))
    key = (tuple(key_items), display_w, display_h, oval_hash, dis_tasi)
    if key in _img_cache: return _img_cache[key]

    load_arch()
    result = _arch_base.copy()
    H, W = result.shape[:2]
    draw_img = Image.fromarray(result)
    draw = ImageDraw.Draw(draw_img)

    for n_str, durum in tedaviler.items():
        n = int(n_str)
        if n not in _masks: continue
        mask = _masks[n]

        # Durum normalize et
        durum = normalize_state(durum)
        tac = durum.get("tac") or "saglikli"
        has_kanal = durum.get("kanal", False)
        has_kron  = durum.get("kron", False)

        # ── Diş gövdesi rengi ──
        if tac != "saglikli" and T_COLOR.get(tac):
            r2,g2,b2 = T_COLOR[tac]
            alpha = 0.60
            arr = np.array(draw_img)
            arr[mask,0] = np.clip(arr[mask,0]*(1-alpha)+r2*alpha,0,255).astype(np.uint8)
            arr[mask,1] = np.clip(arr[mask,1]*(1-alpha)+g2*alpha,0,255).astype(np.uint8)
            arr[mask,2] = np.clip(arr[mask,2]*(1-alpha)+b2*alpha,0,255).astype(np.uint8)
            draw_img = Image.fromarray(arr)
            draw = ImageDraw.Draw(draw_img)

        # ── Kanal oval indikatörü ──
        if has_kanal:
            kanal_r,kanal_g,kanal_b = T_COLOR["kanal"]
            ox, oy = oval_pozisyon(n)
            tr = dis_r(n)
            ow = max(int(tr*0.38), 9)
            oh = max(int(tr*0.28), 7)
            draw.ellipse([ox-ow, oy-oh, ox+ow, oy+oh],
                         fill=(kanal_r,kanal_g,kanal_b,220),
                         outline=(255,255,255,180))

        # ── Kron border highlight ──
        if has_kron:
            kron_r,kron_g,kron_b = T_COLOR["kron"]
            # Maskenin bounding box'ını bul
            ys2,xs2 = np.where(mask)
            if len(ys2) > 0:
                x0m,x1m = xs2.min()-3, xs2.max()+3
                y0m,y1m = ys2.min()-3, ys2.max()+3
                draw.ellipse([x0m,y0m,x1m,y1m],
                             outline=(kron_r,kron_g,kron_b),
                             width=3)

    if dis_tasi:
        _draw_gum_arc(draw, _UST_KEYS)
        _draw_gum_arc(draw, _ALT_KEYS)

    arr_final = np.array(draw_img)
    img = Image.fromarray(arr_final)
    img = img.resize((display_w, display_h), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    photo = tk.PhotoImage(data=base64.b64encode(buf.read()))
    _img_cache[key] = photo
    return photo

def invalidate_cache():
    _img_cache.clear()

def pixel_to_tooth(px, py, scale_x, scale_y):
    ox = px / scale_x; oy = py / scale_y
    best_n, best_d = None, float("inf")
    for n, (cx, cy) in TEETH_COORDS.items():
        d = math.sqrt((ox-cx)**2 + (oy-cy)**2)
        if d < dis_r(n) + 8 and d < best_d:
            best_d = d; best_n = n
    return best_n




# ── BLUEX DENTAL MAP (v4 anatomik tasarım) ────────────────────────────────────
M_SCALE = 1.6                # harita genel büyütme çarpanı
MAP_W, MAP_H = int(500 * M_SCALE), int(364 * M_SCALE)   # ArchCanvas ekran boyutu
_MS  = 2                     # tam sahne süperörnekleme
_MTS = 4                     # diş tile iç süperörnekleme (96x240 mantıksal → x4)

M_BG     = _hex_to_rgb(TH["panel"])       # xəritə kartının fonu (TH ilə eyni)
M_PANEL  = _hex_to_rgb(TH["panel_soft"])  # çip zemini (TH ilə eyni)
M_IVORY  = (240, 237, 228)
M_ROOT   = (228, 216, 178)
M_EDGE   = (128, 118, 100)
M_CANALC = (176, 162, 132)
M_NUM    = (150, 172, 204)
M_STEEL  = (152, 163, 178)
M_TARTAR = (146, 82, 196)
M_SEL    = (122, 170, 246)
M_COLORS = {
    "cekim":   (211, 68, 63),
    "kanal":   (72, 108, 214),
    "dolgu":   (34, 168, 118),
    "kron":    (168, 78, 205),
    "implant": (58, 141, 60),
    "gozlem":  (235, 146, 42),
    "eksik":   (110, 118, 132),
    "kok":     (184, 122, 58),
    "curuk":   (150, 94, 44),
}

MX0, MX1 = 30 * M_SCALE, 470 * M_SCALE
MSTEP = (MX1 - MX0) / 15.0
_MF   = MSTEP / 96.0         # 96 mantıksal diş genişliği → adım genişliği

M_UP_TILE, M_UP_OCC, M_UP_NUM = 24 * M_SCALE, 106 * M_SCALE, 133 * M_SCALE
M_DIV = 172 * M_SCALE
M_LO_NUM, M_LO_OCC, M_LO_TILE = 190 * M_SCALE, 200 * M_SCALE, 222 * M_SCALE
M_LEG1, M_LEG2 = 322 * M_SCALE, 346 * M_SCALE

def _mfont(sz, bold=False):
    try:
        return ImageFont.truetype(
            "C:/Windows/Fonts/segoeui%s.ttf" % ("b" if bold else ""), sz)
    except OSError:
        return ImageFont.load_default()

F_MNUM = _mfont(int(12 * M_SCALE * _MS))
F_MLEG = _mfont(int(10 * M_SCALE * _MS))
F_MSML = _mfont(int(9 * M_SCALE * _MS))
F_MBADGE = _mfont(int(10 * M_SCALE * _MS), bold=True)

def _m_kind(n):
    u = n % 10
    if u == 1: return "incisor_c"
    if u == 2: return "incisor_l"
    if u == 3: return "canine"
    if u in (4, 5): return "premolar"
    return "molar"

def _m_shade(c, f):
    return tuple(max(0, min(255, int(v * f))) for v in c)

def _m_chaikin(pts, iters=3):
    for _ in range(iters):
        out = []
        n = len(pts)
        for i in range(n):
            p, q = pts[i], pts[(i + 1) % n]
            out.append((p[0] * 0.75 + q[0] * 0.25, p[1] * 0.75 + q[1] * 0.25))
            out.append((p[0] * 0.25 + q[0] * 0.75, p[1] * 0.25 + q[1] * 0.75))
        pts = out
    return pts

def _m_scale_pts(pts, s, cx=48):
    return [((x - cx) * s + cx, y * s) for x, y in pts]

# Tile: 96x240 mantıksal, ÜST diş yönelimi (kök yukarıda). Kuron sınırı y≈118.
def _m_tooth_shapes(kind):
    if kind in ("incisor_c", "incisor_l"):
        sil = [(40,22),(34,60),(29,100),(25,122),(19,150),(22,186),(32,209),(48,215),
               (64,209),(74,186),(77,150),(71,122),(67,100),(62,60),(56,22),(48,16)]
        crown = [(25,122),(19,150),(22,186),(32,209),(48,215),(64,209),(74,186),(77,150),(71,122),(48,114)]
        canals = [[(48,32),(48,110)]]
        if kind == "incisor_l":
            sil, crown = _m_scale_pts(sil, 0.85), _m_scale_pts(crown, 0.85)
        return sil, crown, canals
    if kind == "canine":
        sil = [(41,14),(35,58),(29,100),(25,122),(17,152),(22,190),(35,207),(48,224),
               (61,207),(74,190),(79,152),(71,122),(67,100),(61,58),(55,14),(48,8)]
        crown = [(25,122),(17,152),(22,190),(35,207),(48,224),(61,207),(74,190),(79,152),(71,122),(48,114)]
        canals = [[(48,24),(48,110)]]
        return sil, crown, canals
    if kind == "premolar":
        sil = [(41,30),(34,66),(28,102),(24,122),(14,152),(18,190),(34,212),(48,216),
               (62,212),(78,190),(82,152),(72,122),(68,102),(62,66),(55,30),(48,24)]
        crown = [(24,122),(14,152),(18,190),(34,212),(48,216),(62,212),(78,190),(82,152),(72,122),(48,114)]
        canals = [[(48,38),(48,110)]]
        return sil, crown, canals
    sil = [(20,118),(15,72),(19,36),(27,24),(35,30),(39,66),(43,100),(48,108),
           (53,100),(57,66),(61,30),(69,24),(77,36),(81,72),(76,118),
           (85,152),(81,192),(63,214),(48,218),(33,214),(11,192),(7,152)]
    crown = [(20,118),(7,152),(11,192),(33,214),(48,218),(63,214),(81,192),(85,152),(76,118),(48,110)]
    canals = [[(26,38),(32,108)],[(70,38),(64,108)]]
    return sil, crown, canals

_m_tile_cache = {}

def _m_tooth_tile(kind, tac, kanal, kron, curuk, dolgu, upper, mesial_left):
    curuk, dolgu = tuple(curuk) if curuk else (), tuple(dolgu) if dolgu else ()
    key = (kind, tac, kanal, kron, curuk, dolgu, upper, mesial_left)
    if key in _m_tile_cache: return _m_tile_cache[key]
    TS = _MTS
    size = (96 * TS, 240 * TS)
    out_size = (int(96 * _MF * _MS), int(240 * _MF * _MS))
    t = Image.new("RGBA", size, (0, 0, 0, 0))
    td = ImageDraw.Draw(t)
    sil, crown, canals = _m_tooth_shapes(kind)
    sc = lambda pts: [(x * TS, y * TS) for x, y in pts]

    if tac == "eksik" and not kron:
        pts = sc(_m_chaikin(sil))
        for i in range(0, len(pts) - 4, 8):
            td.line(pts[i:i + 5], fill=(96, 104, 118, 210), width=5 * TS)
        if not upper:
            t = t.transpose(Image.FLIP_TOP_BOTTOM)
        tile = t.resize(out_size, Image.LANCZOS)
        _m_tile_cache[key] = tile
        return tile

    if tac == "kok":
        # kron hissəsi dağılmış — yalnız kök, kırık kenarlı
        td.polygon(sc(_m_chaikin(sil)), fill=(216, 196, 150, 255))
        c_col = M_COLORS["kanal"] if kanal else M_CANALC
        wd = (9 if kanal else 4) * TS
        for c0, c1 in canals:
            td.line((c0[0]*TS, c0[1]*TS, c1[0]*TS, c1[1]*TS), fill=c_col + (255,), width=wd)
        spts = sc(_m_chaikin(sil))
        td.line(spts + [spts[0]], fill=M_EDGE + (255,), width=5 * TS, joint="curve")
        # kırık hattının (servikal bölge) altındaki kron kısmını sil
        zig = [(0, 124)]
        for zi, zx in enumerate(range(6, 97, 10)):
            zig.append((zx, 132 if zi % 2 == 0 else 117))
        zig += [(96, 124), (96, 240), (0, 240)]
        td.polygon(sc(zig), fill=(0, 0, 0, 0))
        edge_col = _m_shade((184, 122, 58), 0.75)
        td.line(sc(zig[:-2]), fill=edge_col + (255,), width=5 * TS)
        if not upper:
            t = t.transpose(Image.FLIP_TOP_BOTTOM)
        tile = t.resize(out_size, Image.LANCZOS)
        _m_tile_cache[key] = tile
        return tile

    is_kopru = (tac == "eksik")   # eksik+kron: köprü ayağı — kök/vida yok, yalnız kron dişeti üzerinde
    crown_fill = M_IVORY if tac in (None, "eksik") else M_COLORS.get(tac)

    if tac == "implant":
        # kök yerine titanyum vida
        td.polygon([(44*TS,26*TS),(52*TS,26*TS),(60*TS,118*TS),(36*TS,118*TS)],
                   fill=M_STEEL + (255,), outline=_m_shade(M_STEEL, 0.62) + (255,))
        for yy in (44, 62, 80, 98):
            f = (yy - 26) / 92.0
            half = 11 + f * 8
            td.line((int((48 - half) * TS), yy * TS, int((48 + half) * TS), yy * TS),
                    fill=_m_shade(M_STEEL, 0.55) + (255,), width=4 * TS)
    elif not is_kopru:
        td.polygon(sc(_m_chaikin(sil)), fill=M_ROOT + (255,))

    td.polygon(sc(_m_chaikin(crown)), fill=crown_fill + (255,))

    if tac != "implant" and not is_kopru:
        c_col = M_COLORS["kanal"] if kanal else M_CANALC
        wd = (9 if kanal else 4) * TS
        for c0, c1 in canals:
            td.line((c0[0]*TS, c0[1]*TS, c1[0]*TS, c1[1]*TS), fill=c_col + (255,), width=wd)
        spts = sc(_m_chaikin(sil))
        td.line(spts + [spts[0]], fill=M_EDGE + (255,), width=5 * TS, joint="curve")

    cpts = sc(_m_chaikin(crown))
    if kron:
        ec, ew = M_COLORS["kron"], 10 * TS
    elif tac:
        ec, ew = _m_shade(crown_fill, 0.62), 5 * TS
    else:
        ec, ew = M_EDGE, 5 * TS
    td.line(cpts + [cpts[0]], fill=ec + (255,), width=ew, joint="curve")

    # kuron ışığı: kuron poligonuna maskelenmiş yumuşak parlaklık
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(cpts, fill=255)
    ccx = sum(p[0] for p in cpts) / len(cpts)
    ccy = sum(p[1] for p in cpts) / len(cpts)
    hl = Image.new("L", size, 0)
    ImageDraw.Draw(hl).ellipse((ccx - 26*TS, ccy - 30*TS, ccx + 14*TS, ccy + 6*TS), fill=78)
    hl = hl.filter(ImageFilter.GaussianBlur(9 * TS))
    hl = ImageChops.multiply(hl, mask)
    t.paste(Image.new("RGBA", size, (255, 255, 255, 255)), (0, 0), hl)

    if dolgu or curuk:
        # kron üzerinde yüzeye göre konumlanmış bölgeler: M/D=kronun sağ/sol yarısı
        # (mesial_left'e göre), O=insizal/okluzal uç (alt), V=servikal bant (üst,
        # dişeti kenarı) — B/L bu önden görünümde ayırt edilemediği için kronun
        # tamamına hafif bir ton olarak yansır.
        bxs = [p[0] for p in cpts]; bys = [p[1] for p in cpts]
        bx0, bx1 = min(bxs), max(bxs)
        by0, by1 = min(bys), max(bys)
        bh = by1 - by0
        bcx = (bx0 + bx1) / 2
        gap_v = bh * 0.028       # V/M-D/O bantları arasında görünür ayraç
        gap_h = (bx1 - bx0) * 0.045   # M ile D arasında görünür ayraç
        v_edge = by0 + bh * 0.22
        o_edge = by1 - bh * 0.28
        band_v = (bx0, by0, bx1, v_edge - gap_v)
        band_o = (bx0, o_edge + gap_v, bx1, by1)
        mid0, mid1 = v_edge + gap_v, o_edge - gap_v
        half_m = (bx0, mid0, bcx - gap_h, mid1)
        half_d = (bcx + gap_h, mid0, bx1, mid1)
        zone_boxes = {"O": band_o, "V": band_v,
                      "M": half_m if mesial_left else half_d,
                      "D": half_d if mesial_left else half_m}

        def _paint_yuz(zset, color, alpha):
            if not zset: return None
            layer = Image.new("RGBA", size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            for zn in zset:
                box = zone_boxes.get(zn)
                if box:
                    radius = min(box[2] - box[0], box[3] - box[1]) * 0.42
                    ld.rounded_rectangle(box, radius=radius, fill=color + (alpha,))
            if "B" in zset or "L" in zset:
                ld.polygon(cpts, fill=color + (int(alpha * 0.32),))
            layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
            return layer

        dolgu_layer = _paint_yuz(set(dolgu), M_COLORS["dolgu"], 195)
        if dolgu_layer: t = Image.alpha_composite(t, dolgu_layer)
        curuk_layer = _paint_yuz(set(curuk), (70, 44, 28), 205)
        if curuk_layer: t = Image.alpha_composite(t, curuk_layer)

    if not upper:
        t = t.transpose(Image.FLIP_TOP_BOTTOM)
    tile = t.resize(out_size, Image.LANCZOS)
    _m_tile_cache[key] = tile
    return tile

OCC_W, OCC_H = 92, 74   # oklüzal oval mantıksal tuval boyutu (eskiden 64x52 — büyütüldü)

def _m_occ_tile(kind, tac, kanal, kron, curuk, dolgu, upper, mesial_left):
    curuk, dolgu = tuple(curuk) if curuk else (), tuple(dolgu) if dolgu else ()
    key = ("occ", kind, tac, kanal, kron, curuk, dolgu, upper, mesial_left)
    if key in _m_tile_cache: return _m_tile_cache[key]
    TS = _MTS
    size = (OCC_W * TS, OCC_H * TS)
    out_size = (int(OCC_W * _MF * _MS), int(OCC_H * _MF * _MS))
    t = Image.new("RGBA", size, (0, 0, 0, 0))
    td = ImageDraw.Draw(t)
    fill = (245, 243, 236) if tac in (None, "eksik") else M_COLORS.get(tac)
    if tac == "eksik" and not kron:
        td.ellipse((9*TS, 9*TS, 83*TS, 65*TS), outline=(96, 104, 118), width=4 * TS)
    else:
        edge = _m_shade(fill, 0.6) if tac else (150, 143, 128)
        box = (9, 9, 83, 65) if kind == "molar" else (15, 9, 77, 65)
        td.ellipse(tuple(v * TS for v in box), fill=fill + (255,), outline=edge + (255,), width=4 * TS)
        fis = _m_shade(fill, 0.62)
        cx, cy = 46, 37
        if kind == "molar":
            td.line(((cx-15)*TS, cy*TS, (cx+15)*TS, cy*TS), fill=fis + (255,), width=4 * TS)
            td.line((cx*TS, (cy-11)*TS, cx*TS, (cy+11)*TS), fill=fis + (255,), width=4 * TS)
        elif kind == "premolar":
            td.line((cx*TS, (cy-11)*TS, cx*TS, (cy+11)*TS), fill=fis + (255,), width=4 * TS)
        else:
            td.arc(((cx-14)*TS, (cy-9)*TS, (cx+14)*TS, (cy+11)*TS), 20, 160, fill=fis + (255,), width=4 * TS)

        # ── yüzey (M/D/O/B/L) katmanı: oval "+"/diagonal ile 5 bölgeye ayrılır ──
        if curuk or dolgu:
            x0, y0, x1, y1 = box
            top_zone    = "B" if upper else "L"
            bottom_zone = "L" if upper else "B"
            left_zone   = "M" if mesial_left else "D"
            right_zone  = "D" if mesial_left else "M"
            zone_polys = {
                top_zone:    [(x0, y0), (x1, y0), (cx, cy)],
                right_zone:  [(x1, y0), (x1, y1), (cx, cy)],
                bottom_zone: [(x1, y1), (x0, y1), (cx, cy)],
                left_zone:   [(x0, y1), (x0, y0), (cx, cy)],
            }
            ell_mask = Image.new("L", size, 0)
            ImageDraw.Draw(ell_mask).ellipse(tuple(v * TS for v in box), fill=255)
            o_r = min((x1 - x0) / 2, (y1 - y0) / 2) * 0.42
            _OVAL_YUZ = {"M", "D", "O", "B", "L"}   # "V" (servikal) bu oklüzal görünümde yok — diş siluetinde
            dolgu_set, curuk_set = set(dolgu) & _OVAL_YUZ, set(curuk) & _OVAL_YUZ
            if dolgu_set:
                wedge = Image.new("RGBA", size, (0, 0, 0, 0))
                wd = ImageDraw.Draw(wedge)
                for zn, poly in zone_polys.items():
                    if zn in dolgu_set:
                        wd.polygon([(px * TS, py * TS) for px, py in poly], fill=M_COLORS["dolgu"] + (225,))
                if "O" in dolgu_set:
                    wd.ellipse(((cx-o_r)*TS,(cy-o_r)*TS,(cx+o_r)*TS,(cy+o_r)*TS), fill=M_COLORS["dolgu"] + (225,))
                wedge.putalpha(ImageChops.multiply(wedge.getchannel("A"), ell_mask))
                t = Image.alpha_composite(t, wedge)
            if curuk_set:
                dot = Image.new("RGBA", size, (0, 0, 0, 0))
                dd = ImageDraw.Draw(dot)
                dot_r = o_r * 0.62
                centroids = {
                    top_zone:    ((x0 + x1) / 2, y0 + (cy - y0) * 0.42),
                    bottom_zone: ((x0 + x1) / 2, cy + (y1 - cy) * 0.58),
                    left_zone:   (x0 + (cx - x0) * 0.42, (y0 + y1) / 2),
                    right_zone:  (cx + (x1 - cx) * 0.58, (y0 + y1) / 2),
                    "O": (cx, cy),
                }
                for zn in curuk_set:
                    zx, zy = centroids[zn]
                    dd.ellipse(((zx-dot_r)*TS,(zy-dot_r)*TS,(zx+dot_r)*TS,(zy+dot_r)*TS), fill=(70, 44, 28, 235))
                dot.putalpha(ImageChops.multiply(dot.getchannel("A"), ell_mask))
                t = Image.alpha_composite(t, dot)
            td = ImageDraw.Draw(t)

        if kanal:
            td.ellipse(((cx-9)*TS, (cy-9)*TS, (cx+9)*TS, (cy+9)*TS), fill=M_COLORS["kanal"] + (255,))
        if kron:
            td.ellipse((3*TS, 3*TS, (OCC_W-3)*TS, (OCC_H-3)*TS), outline=M_COLORS["kron"] + (255,), width=5 * TS)
    tile = t.resize(out_size, Image.LANCZOS)
    _m_tile_cache[key] = tile
    return tile

_map_base = None

def _m_base_img():
    global _map_base
    if _map_base is None:
        W, H = MAP_W * _MS, MAP_H * _MS
        # uygulama zemini üstünde yuvarlak köşeli kart
        img = Image.new("RGB", (W, H), _hex_to_rgb(TH["bg"]))
        card = Image.new("L", (W, H), 0)
        ImageDraw.Draw(card).rounded_rectangle((0, 0, W - 1, H - 1),
                                               radius=int(14 * M_SCALE * _MS), fill=255)
        img = Image.composite(Image.new("RGB", (W, H), M_BG), img, card)
        vig = Image.new("L", (MAP_W, MAP_H), 0)
        ImageDraw.Draw(vig).ellipse((MAP_W * 0.04, MAP_H * 0.05,
                                     MAP_W * 0.96, MAP_H * 0.95), fill=22)
        vig = vig.filter(ImageFilter.GaussianBlur(int(55 * M_SCALE))).resize((W, H), Image.BILINEAR)
        vig = ImageChops.multiply(vig, card)
        img = Image.composite(Image.new("RGB", (W, H), (16, 46, 88)), img, vig)
        d = ImageDraw.Draw(img)
        d.text(((MAP_W - 12 * M_SCALE) * _MS, 14 * M_SCALE * _MS), "ÜST ÇENE (MAXILLA)",
               font=F_MSML, fill=(122, 148, 184), anchor="rm")
        d.text(((MAP_W - 12 * M_SCALE) * _MS, 346 * M_SCALE * _MS), "ALT ÇENE (MANDİBULA)",
               font=F_MSML, fill=(122, 148, 184), anchor="rm")
        _map_base = img
    return _map_base.copy()

def _m_chip(d, x, ycen, name, col, cnt):
    S = _MS
    K = M_SCALE
    wname = d.textlength(name, font=F_MLEG)
    ctext = str(cnt)
    wcnt = d.textlength(ctext, font=F_MLEG)
    w = (8 + 9 + 6) * K * S + wname + 5 * K * S + wcnt + 8 * K * S
    d.rounded_rectangle((x, (ycen - 11 * K) * S, x + w, (ycen + 11 * K) * S),
                        radius=11 * K * S, fill=M_PANEL, outline=(52, 88, 138), width=1 * S)
    dx = x + 8 * K * S
    d.ellipse((dx, ycen * S - 4.5 * K * S, dx + 9 * K * S, ycen * S + 4.5 * K * S),
              fill=col, outline=_m_shade(col, 0.6), width=1 * S)
    tx = dx + (9 + 6) * K * S
    d.text((tx, ycen * S), name, font=F_MLEG, fill=(208, 222, 240), anchor="lm")
    d.text((tx + wname + 5 * K * S, ycen * S), ctext, font=F_MLEG, fill=(140, 162, 196), anchor="lm")
    return w

# ── STATİSTİKA — PIL ilə sadə diaqram çəkmə köməkçiləri ──────────────────────
def _sfont(sz, bold=False):
    try:
        return ImageFont.truetype(
            "C:/Windows/Fonts/segoeui%s.ttf" % ("b" if bold else ""), sz)
    except OSError:
        return ImageFont.load_default()

_SF_LBL  = _sfont(13)
_SF_LBLB = _sfont(13, True)
_SF_VAL  = _sfont(12, True)

STAT_RENG_KISI  = (47, 127, 242)   # TH accent — Kişi
STAT_RENG_QADIN = (242, 87, 127)   # Qadın (mavi ilə aydın ayrılan isti ton)
STAT_RENG_CURUK = (150, 94, 44)
STAT_RENG_CEKIM = (211, 68, 63)

def _stat_bar_chart(items, color, width=720, bar_h=26, gap=10,
                     left_pad=90, right_pad=60, top_pad=6, max_val=None):
    """items: [(etiket, deger), ...] — üfüqi çəki çubuqları. Boşdursa None qaytarır."""
    if not items:
        return None
    max_val = max_val or max((v for _, v in items), default=1) or 1
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    needed = max((probe.textlength(str(lbl), font=_SF_LBLB) for lbl, _ in items), default=0)
    left_pad = max(left_pad, int(needed) + 20)
    h = top_pad * 2 + len(items) * (bar_h + gap) - gap
    img = Image.new("RGBA", (width, max(h, 1)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    track_w = max(10, width - left_pad - right_pad)
    for i, (lbl, val) in enumerate(items):
        y0 = top_pad + i * (bar_h + gap)
        y1 = y0 + bar_h
        ym = (y0 + y1) / 2
        d.text((left_pad - 10, ym), str(lbl), font=_SF_LBLB, fill=(232, 237, 245), anchor="rm")
        d.rounded_rectangle((left_pad, y0, left_pad + track_w, y1),
                             radius=bar_h / 2, fill=(26, 34, 51))
        bw = max(6, track_w * (val / max_val)) if val else 0
        if bw:
            d.rounded_rectangle((left_pad, y0, left_pad + bw, y1), radius=bar_h / 2, fill=color)
        d.text((left_pad + track_w + 10, ym), str(val), font=_SF_VAL, fill=(232, 237, 245), anchor="lm")
    return img

def _stat_grouped_chart(categories, series, width=720, bar_h=20, gap_in=7, gap_out=18,
                         left_pad=130, right_pad=60, top_pad=6, max_val=None):
    """categories: [etiket,...]; series: [(ad, reng, {etiket: deger}), ...]."""
    if not categories or not series:
        return None
    vals = [s[2].get(c, 0) for s in series for c in categories]
    max_val = max_val or max(vals, default=1) or 1
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    needed = max((probe.textlength(str(c), font=_SF_LBLB) for c in categories), default=0)
    left_pad = max(left_pad, int(needed) + 20)
    group_h = bar_h * len(series) + gap_in * (len(series) - 1)
    h = top_pad * 2 + len(categories) * (group_h + gap_out) - gap_out
    img = Image.new("RGBA", (width, max(h, 1)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    track_w = max(10, width - left_pad - right_pad)
    for i, cat in enumerate(categories):
        gy0 = top_pad + i * (group_h + gap_out)
        d.text((left_pad - 10, gy0 + group_h / 2), str(cat), font=_SF_LBLB,
               fill=(232, 237, 245), anchor="rm")
        for j, (_name, color, data) in enumerate(series):
            y0 = gy0 + j * (bar_h + gap_in)
            y1 = y0 + bar_h
            val = data.get(cat, 0)
            d.rounded_rectangle((left_pad, y0, left_pad + track_w, y1),
                                 radius=bar_h / 2, fill=(26, 34, 51))
            bw = max(4, track_w * (val / max_val)) if val else 0
            if bw:
                d.rounded_rectangle((left_pad, y0, left_pad + bw, y1), radius=bar_h / 2, fill=color)
            d.text((left_pad + track_w + 10, (y0 + y1) / 2), str(val), font=_SF_VAL,
                   fill=(232, 237, 245), anchor="lm")
    return img

def _stat_legend_img(items, h=24, gap=22):
    """items: [(ad, reng), ...] — üfüqi rəng-nöqtəli lejant."""
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    x = 4
    widths = []
    for name, _c in items:
        w = 12 + 6 + d0.textlength(name, font=_SF_LBL)
        widths.append(w)
    total_w = int(sum(widths) + gap * len(items) + 8)
    img = Image.new("RGBA", (total_w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = 4
    for (name, color), w in zip(items, widths):
        d.ellipse((x, h / 2 - 6, x + 12, h / 2 + 6), fill=color)
        d.text((x + 18, h / 2), name, font=_SF_LBL, fill=(232, 237, 245), anchor="lm")
        x += w + gap
    return img

STAT_RENG_ARTIR  = (34, 197, 94)    # tendensiya yuxarı — yaşıl
STAT_RENG_AZALIR = (239, 68, 63)    # tendensiya aşağı — qırmızı

def _stat_trend(vals):
    """vals: xronoloji ədəd siyahısı. Birinci yarı ortalaması ilə ikinci yarı
    ortalamasını müqayisə edib (faiz_deyisim, artirmi) qaytarır — artirmi None
    olarsa tendensiya hesablamaq üçün kifayət qədər nöqtə yoxdur."""
    n = len(vals)
    if n < 2 or sum(vals) == 0:
        return 0.0, None
    mid = n // 2
    birinci  = vals[:mid] or vals[:1]
    ikinci   = vals[mid:] or vals[-1:]
    ort1 = sum(birinci) / len(birinci)
    ort2 = sum(ikinci) / len(ikinci)
    if ort1 == ort2:
        return 0.0, None
    pct = 100.0 if ort1 == 0 else (ort2 - ort1) / ort1 * 100
    return pct, (ort2 > ort1)

def _stat_line_chart(points, color, width=720, height=260,
                      left_pad=50, right_pad=30, top_pad=40, bottom_pad=44):
    """points: [(etiket, deger), ...] — xronoloji sırada. Kordinat oxlu xətt
    qrafiki + nöqtələr + ümumi tendensiyanı göstərən ox (↑/↓) və faiz dəyişim."""
    if not points:
        return None
    vals = [v for _, v in points]
    max_val = max(vals, default=0) or 1
    n = len(points)
    plot_w = max(10, width - left_pad - right_pad)
    plot_h = max(10, height - top_pad - bottom_pad)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Kordinat oxları
    d.line((left_pad, top_pad, left_pad, top_pad + plot_h), fill=(70, 84, 112), width=1)
    d.line((left_pad, top_pad + plot_h, left_pad + plot_w, top_pad + plot_h),
           fill=(70, 84, 112), width=1)
    d.text((left_pad - 10, top_pad), str(max_val), font=_SF_VAL,
           fill=(160, 175, 200), anchor="rm")
    d.text((left_pad - 10, top_pad + plot_h), "0", font=_SF_VAL,
           fill=(160, 175, 200), anchor="rm")

    def xy(i, v):
        x = left_pad + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)
        y = top_pad + plot_h - (plot_h * v / max_val if max_val else 0)
        return x, y

    coords = [xy(i, v) for i, (_, v) in enumerate(points)]
    if len(coords) > 1:
        d.line(coords, fill=color, width=3, joint="curve")
    for x, y in coords:
        d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color, outline=(14, 18, 28), width=1)

    max_labels = 8
    step = max(1, round(n / max_labels))
    for i, (lbl, v) in enumerate(points):
        if i % step and i != n - 1:
            continue
        x, y = coords[i]
        d.text((x, top_pad + plot_h + 8), str(lbl), font=_SF_LBL,
               fill=(160, 175, 200), anchor="mt")
        d.text((x, y - 10), str(v), font=_SF_VAL, fill=(232, 237, 245), anchor="mb")

    pct, artirmi = _stat_trend(vals)
    if artirmi is not None:
        trend_color = STAT_RENG_ARTIR if artirmi else STAT_RENG_AZALIR
        ox = "↑" if artirmi else "↓"
        d.text((left_pad + plot_w, top_pad - 16), f"{ox} {abs(pct):.0f}%",
               font=_SF_LBLB, fill=trend_color, anchor="rm")
    return img

def get_map_photo(tedaviler, dis_tasi=False, secili=None, band_lbl="Diş Daşı Təmizliyi",
                  ilkin=False, secili_cok=None, not_disler=None):
    S = _MS
    states = {}
    for k, v in tedaviler.items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        states[n] = normalize_state(v)
    not_set = set(not_disler) if not_disler else set()
    key_items = tuple(sorted((n, s["tac"], s["kanal"], s["kron"], tuple(s["curuk"]), tuple(s["dolgu"]))
                             for n, s in states.items()))
    cok_key = tuple(secili_cok) if secili_cok else None
    key = ("bluexmap", key_items, dis_tasi, secili, band_lbl, ilkin, cok_key, tuple(sorted(not_set)))
    if key in _img_cache: return _img_cache[key]
    if len(_img_cache) > 48: _img_cache.clear()

    img = _m_base_img()
    d = ImageDraw.Draw(img)
    bos = {"tac": None, "kanal": False, "kron": False, "curuk": [], "dolgu": []}
    sel = None
    cok_set = set(secili_cok) if secili_cok else set()
    sel_list = []
    for upper, row in ((True, UST), (False, ALT)):
        for i, n in enumerate(row):
            x = MX0 + i * MSTEP
            ns = states.get(n, bos)
            tac, kn, kr, cu, du = ns["tac"], ns["kanal"], ns["kron"], ns["curuk"], ns["dolgu"]
            kind = _m_kind(n)
            mesial_left = i >= len(row) // 2
            tile = _m_tooth_tile(kind, tac, kn, kr, cu, du, upper, mesial_left)
            ty = M_UP_TILE if upper else M_LO_TILE
            img.paste(tile, (int(x * S - tile.width / 2), int(ty * S)), tile)
            occ = _m_occ_tile(kind, tac, kn, kr, cu, du, upper, mesial_left)
            oy = M_UP_OCC if upper else M_LO_OCC
            img.paste(occ, (int(x * S - occ.width / 2), int(oy * S)), occ)
            ny = M_UP_NUM if upper else M_LO_NUM
            if n == secili or n in cok_set:
                col = (198, 218, 250)
            elif tac:
                col = M_COLORS.get(tac, M_NUM)
            elif kn:
                col = (108, 142, 235)
            elif kr:
                col = M_COLORS["kron"]
            elif du:
                col = M_COLORS["dolgu"]
            elif cu:
                col = M_COLORS["curuk"]
            else:
                col = M_NUM
            d.text((x * S, ny * S), str(n), font=F_MNUM, fill=col, anchor="mm")
            if n in not_set:   # qeyd dəftərində "#NN" ilə bu dişə istinad olunub — nida nişanı
                bx, by = x + 9 * M_SCALE, ny - 10 * M_SCALE
                r = 6.5 * M_SCALE
                d.ellipse((bx * S - r * S, by * S - r * S, bx * S + r * S, by * S + r * S),
                          fill=(226, 58, 58), outline=(18, 22, 32), width=1 * S)
                d.text((bx * S, by * S), "!", font=F_MBADGE, fill="white", anchor="mm")
            if n == secili:
                sel = (x, upper)
            if n in cok_set:
                sel_list.append((x, upper))

    # orta hat / diş taşı bandı
    K = M_SCALE
    d.text((12 * K * S, (M_DIV - 9 * K) * S), "SAĞ", font=F_MSML, fill=(110, 134, 170), anchor="lm")
    d.text(((MAP_W - 12 * K) * S, (M_DIV - 9 * K) * S), "SOL", font=F_MSML, fill=(110, 134, 170), anchor="rm")
    if dis_tasi:
        bx0, bx1 = (MX0 - 15 * K) * S, (MX1 + 15 * K) * S
        d.rounded_rectangle((bx0, (M_DIV - 2 * K) * S, bx1, (M_DIV + 2 * K) * S),
                            radius=2 * K * S, fill=M_TARTAR)
        for bx in (bx0, bx1):
            d.line((bx, (M_DIV - 5 * K) * S, bx, (M_DIV + 5 * K) * S), fill=M_TARTAR, width=2 * S)
        lbl = band_lbl
        lw = d.textlength(lbl, font=F_MSML) + 16 * K * S
        cxp = (MAP_W / 2) * S
        d.rounded_rectangle((cxp - lw / 2, (M_DIV - 9 * K) * S, cxp + lw / 2, (M_DIV + 9 * K) * S),
                            radius=9 * K * S, fill=(34, 24, 50), outline=(120, 80, 168), width=1 * S)
        d.text((cxp, M_DIV * S), lbl, font=F_MSML, fill=(214, 180, 236), anchor="mm")
    else:
        d.line(((MX0 - 18 * K) * S, M_DIV * S, (MX1 + 18 * K) * S, M_DIV * S),
               fill=(46, 82, 134), width=1 * S)
        d.line(((MAP_W / 2) * S, (M_DIV - 6 * K) * S, (MAP_W / 2) * S, (M_DIV + 6 * K) * S),
               fill=(64, 98, 152), width=1 * S)

    # seçili diş / bitişik aralık: parıltı + çerçeve
    frames = list(sel_list)
    if sel and sel not in frames:
        frames.append(sel)
    if frames:
        rects = []
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        tint_ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        td2 = ImageDraw.Draw(tint_ov)
        for x, upper in frames:
            if upper:
                fy0, fy1 = M_UP_TILE - 6 * K, M_UP_NUM + 10 * K
            else:
                fy0, fy1 = M_LO_NUM - 10 * K, M_LO_TILE + 240 * _MF + 6
            fx0, fx1 = (x - MSTEP / 2 - 1) * S, (x + MSTEP / 2 + 1) * S
            rect = (fx0, fy0 * S, fx1, fy1 * S)
            rects.append(rect)
            gd.rounded_rectangle(rect, radius=7 * K * S, outline=(96, 148, 235, 190), width=4 * S)
            td2.rounded_rectangle(rect, radius=7 * K * S, fill=(96, 148, 235, 22))
        glow = glow.filter(ImageFilter.GaussianBlur(4 * S))
        img = Image.alpha_composite(img.convert("RGBA"), glow)
        img = Image.alpha_composite(img, tint_ov).convert("RGB")
        d = ImageDraw.Draw(img)
        for rect in rects:
            d.rounded_rectangle(rect, radius=7 * K * S, outline=M_SEL, width=1 * S)

    # sayaçlı lejant çipleri (iki satır)
    cnt = {}
    healthy = 0
    for n in UST + ALT:
        ns = states.get(n, bos)
        if ns["tac"]: cnt[ns["tac"]] = cnt.get(ns["tac"], 0) + 1
        if ns["kanal"]: cnt["kanal"] = cnt.get("kanal", 0) + 1
        if ns["kron"]: cnt["kron"] = cnt.get("kron", 0) + 1
        if ns["curuk"]: cnt["curuk"] = cnt.get("curuk", 0) + 1
        if ns["dolgu"]: cnt["dolgu"] = cnt.get("dolgu", 0) + 1
        if not ns["tac"] and not ns["kanal"] and not ns["kron"] and not ns["curuk"] and not ns["dolgu"]:
            healthy += 1
    row1 = [(t("Sağlam"), M_IVORY, healthy)]
    if not ilkin:   # çəkim bir müalicə əməliyyatıdır, ilkin (diaqnostik) vəziyyətə aid deyil
        row1.append((t("Çəkim"), M_COLORS["cekim"], cnt.get("cekim", 0)))
    row1 += [(t("Kanal"), M_COLORS["kanal"], cnt.get("kanal", 0)),
             (t("Kompozit Dolğu"), M_COLORS["dolgu"], cnt.get("dolgu", 0)),
             (t("Qapaq"), M_COLORS["kron"], cnt.get("kron", 0))]
    row2 = [(t("İmplant"), M_COLORS["implant"], cnt.get("implant", 0)),
            (t("Əksik"), (90, 98, 112), cnt.get("eksik", 0))]
    if ilkin:
        row2 += [(t("Çürük"), M_COLORS["curuk"], cnt.get("curuk", 0)),
                 (t("Kök"), M_COLORS["kok"], cnt.get("kok", 0))]
    else:
        row2 += [(t("Gözləm"), M_COLORS["gozlem"], cnt.get("gozlem", 0))]
    row2 += [(t("Diş Daşı"), M_TARTAR, t("Aktiv") if dis_tasi else "—")]
    for ycen, items in ((M_LEG1, row1), (M_LEG2, row2)):
        lx = 14 * K * S
        for name, colr, c in items:
            lx += _m_chip(d, lx, ycen, name, colr, c) + 8 * K * S

    img = img.resize((MAP_W, MAP_H), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    photo = tk.PhotoImage(data=base64.b64encode(buf.read()))
    _img_cache[key] = photo
    return photo

_OCC_HW, _OCC_HH = 37 * _MF, 28 * _MF   # oklüzal ovalin yaklaşık yarı-boyutları (hit-test için)
_OCC_CY = 37   # ovalin mantıksal dikey merkezi (kutu 9..65 → orta 37)

# diş siluetinde (önden görünüm) M/D/O/V hit-testi için — _m_tooth_tile'daki
# band_v/band_o/half_m/half_d ile aynı (kind-bağımsız, genel) geometri
_TOOTH_BX0, _TOOTH_BX1 = 14, 82
_TOOTH_BY0, _TOOTH_BY1 = 110, 220
_T_BH = _TOOTH_BY1 - _TOOTH_BY0
_T_BW = _TOOTH_BX1 - _TOOTH_BX0
_T_GAP_V = _T_BH * 0.028
_T_GAP_H = _T_BW * 0.045
_T_V_EDGE = _TOOTH_BY0 + _T_BH * 0.22
_T_O_EDGE = _TOOTH_BY1 - _T_BH * 0.28
_T_BCX = (_TOOTH_BX0 + _TOOTH_BX1) / 2

def _occ_zone(dx, dy, i, row_len, upper):
    """Oklüzal oval içi bir noktanın hangi yüzeye (M/D/O/B/L) düştüğünü bulur —
    merkez daire=Oklüzal, diyagonallerle bölünen 4 üçgen=Bukkal/Lingual/Mesial/Distal
    (_m_occ_tile'daki görsel bölünmeyle birebir aynı geometri)."""
    nx, ny = dx / _OCC_HW, dy / _OCC_HH
    if nx * nx + ny * ny < 0.42:
        return "O"
    if abs(ny) >= abs(nx):
        toward_div = (dy > 0) if upper else (dy < 0)   # M_DIV'e (orta hatta) doğru = Lingual/Palatal
        return "L" if toward_div else "B"
    toward_mid = (dx > 0) if i < row_len / 2 else (dx < 0)   # ağız orta hattına doğru = Mesial
    return "M" if toward_mid else "D"

def _tile_zone(lx, ly, mesial_left):
    """Diş siluetinde (önden görünüm) mantıksal bir noktanın (0..96 x, 0..240
    y, üst-diş yönelimi) M/D/O/V bölgesini bulur — _m_tooth_tile'daki
    band_v/band_o/half_m/half_d ile birebir aynı geometri."""
    if not (_TOOTH_BX0 <= lx <= _TOOTH_BX1 and _TOOTH_BY0 <= ly <= _TOOTH_BY1):
        return None
    if ly < _T_V_EDGE - _T_GAP_V:
        return "V"
    if ly > _T_O_EDGE + _T_GAP_V:
        return "O"
    if ly < _T_V_EDGE + _T_GAP_V or ly > _T_O_EDGE - _T_GAP_V:
        return None   # bant ayracı (V/O ile M-D bandı arası boşluk)
    if lx < _T_BCX - _T_GAP_H:
        return "M" if mesial_left else "D"
    if lx > _T_BCX + _T_GAP_H:
        return "D" if mesial_left else "M"
    return None   # M/D ayracı

def _tile_zone_box(zone, mesial_left):
    """Verilen yüzeyin diş siluetindeki mantıksal kutusu (0..96 x, 0..240 y,
    üst-diş yönelimi, ayraçsız/tam bölge) — hover vurgusu için."""
    if zone == "V":
        return (_TOOTH_BX0, _TOOTH_BY0, _TOOTH_BX1, _T_V_EDGE)
    if zone == "O":
        return (_TOOTH_BX0, _T_O_EDGE, _TOOTH_BX1, _TOOTH_BY1)
    is_left = (zone == "M") == mesial_left
    x0, x1 = (_TOOTH_BX0, _T_BCX) if is_left else (_T_BCX, _TOOTH_BX1)
    return (x0, _T_V_EDGE, x1, _T_O_EDGE)

def map_hit(px, py):
    """Dönüş: (dis_no, yuzey, gorunum) ya da None. `gorunum` 'occ' (oklüzal
    daire) veya 'tile' (diş siluetinin önden görünümü) — M/D/O her iki
    görünümden de, B/L yalnız oklüzalden, V yalnız siluetten seçilebilir.
    `yuzey` None ise tanımlı bir bölgeye denk gelmemiş demektir (varsayılan
    olarak Oklüzal uygulanacak)."""
    i = int(round((px - MX0) / MSTEP))
    if i < 0 or i > 15: return None
    dx = px - (MX0 + i * MSTEP)
    if abs(dx) > MSTEP / 2 + 2: return None
    if M_UP_TILE - 8 <= py <= M_UP_NUM + 12 * M_SCALE:
        n, upper, oy, ty = UST[i], True, M_UP_OCC, M_UP_TILE
    elif M_LO_NUM - 12 * M_SCALE <= py <= M_LO_TILE + 240 * _MF + 8:
        n, upper, oy, ty = ALT[i], False, M_LO_OCC, M_LO_TILE
    else:
        return None
    dy = py - (oy + _OCC_CY * _MF)
    if abs(dx) <= _OCC_HW and abs(dy) <= _OCC_HH:
        return n, _occ_zone(dx, dy, i, 16, upper), "occ"
    ly = (py - ty) / _MF
    if not upper: ly = 240 - ly
    lx = 48 + dx / _MF
    return n, _tile_zone(lx, ly, i >= 8), "tile"

def _zone_hover_rect(n, zone, i, upper, view):
    """Verilen diş+yüzey+görünüm için hover vurgusunun map-space dikdörtgenini
    döndürür (map_hit ile aynı geometriye dayanır — 'seçeceğim bölge büyüsün' efekti)."""
    xcen = MX0 + i * MSTEP
    if view == "tile":
        ty = M_UP_TILE if upper else M_LO_TILE
        lx0, ly0, lx1, ly1 = _tile_zone_box(zone, i >= 8)
        if not upper:
            ly0, ly1 = 240 - ly1, 240 - ly0
        pad = 3
        return (xcen + (lx0 - 48) * _MF - pad, ty + ly0 * _MF - pad,
                xcen + (lx1 - 48) * _MF + pad, ty + ly1 * _MF + pad)
    oy = M_UP_OCC if upper else M_LO_OCC
    occ_cy = oy + _OCC_CY * _MF
    if zone == "O":
        dx = dy = 0
        rw = rh = min(_OCC_HW, _OCC_HH) * 0.5
    else:
        mesial_left = i >= 8
        top_zone    = "B" if upper else "L"
        bottom_zone = "L" if upper else "B"
        left_zone   = "M" if mesial_left else "D"
        right_zone  = "D" if mesial_left else "M"
        dx = -_OCC_HW * 0.5 if zone == left_zone else (_OCC_HW * 0.5 if zone == right_zone else 0)
        dy = -_OCC_HH * 0.5 if zone == top_zone else (_OCC_HH * 0.5 if zone == bottom_zone else 0)
        rw, rh = _OCC_HW * 0.4, _OCC_HH * 0.4
    return (xcen + dx - rw, occ_cy + dy - rh, xcen + dx + rw, occ_cy + dy + rh)

# ── ARCH CANVAS ───────────────────────────────────────────────────────────────
class ArchCanvas(tk.Canvas):
    DW = MAP_W
    DH = MAP_H

    def __init__(self, parent, on_click, on_range=None, on_long_press=None, **kw):
        super().__init__(parent, width=self.DW, height=self.DH,
                         bg=TH["bg"], highlightthickness=0, cursor="hand2", **kw)
        self.on_click = on_click
        self.on_range = on_range
        self.on_long_press = on_long_press   # (n) -> None — diş üzərində 0.5s basılı saxlananda
        self.tedaviler = {}
        self.dis_tasi = False
        self.secili = None
        self.band_label = t("Diş Daşı Təmizliyi")
        self.ilkin = False
        self.not_disler = set()   # qeyd dəftərində "#NN" ilə istinad olunan diş nömrələri (nida nişanı üçün)
        self._ref = None
        self._surukle_sira = None   # sürükleme başladığı diş sırası (UST/ALT)
        self._surukle_disler = []   # sürükleme ile o an kapsanan dişler
        self._surukle_zone = None   # basılan noktadaki yüzey (M/D/O/B/L/V) — dolgu/çürük için
        self._hover_key = None      # (dis_no, yuzey) — üzerinde durulan bölge, hover vurgusu için
        self._lp_timer = None       # uzun-basma (0.5s) üçün planlanmış `after` id-si
        self._lp_dis = None         # uzun-basma gözlənilən diş nömrəsi
        self._lp_fired = False      # bu basışda uzun-basma artıq tətikləndimi (buraxılanda klik tətbiq olunmasın)
        self.bind("<Button-1>", self._bas)
        self.bind("<B1-Motion>", self._surukle)
        self.bind("<ButtonRelease-1>", self._birak)
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.ciz()

    def ciz(self):
        self.delete("all")
        self._hover_key = None
        cok = self._surukle_disler if len(self._surukle_disler) > 1 else None
        photo = get_map_photo(self.tedaviler, dis_tasi=self.dis_tasi, secili=self.secili,
                              band_lbl=self.band_label, ilkin=self.ilkin, secili_cok=cok,
                              not_disler=self.not_disler)
        self._ref = photo
        self.create_image(0, 0, anchor="nw", image=photo)

    def _hover(self, e):
        hit = map_hit(e.x, e.y)
        self._set_hover(hit if (hit and hit[1]) else None)

    def _set_hover(self, key):
        if key == self._hover_key:
            return
        self._hover_key = key
        self.delete("hover")
        if key is None:
            return
        n, zone, view = key
        row = UST if n in UST else ALT
        i = row.index(n)
        rect = _zone_hover_rect(n, zone, i, row is UST, view)
        self.create_oval(*rect, outline="#F5CB5C", width=3, tags="hover")

    def set_tedavi(self, n, tid):
        self.tedaviler[str(n)] = tid
        self.secili = n
        self.ciz()

    def _bas(self, e):
        hit = map_hit(e.x, e.y)
        if hit is None:
            self._surukle_sira = None
            self._surukle_disler = []
            self._surukle_zone = None
            return
        n, zone = hit[0], hit[1]
        self._surukle_sira = UST if n in UST else ALT
        self._surukle_disler = [n]
        self._surukle_zone = zone
        self.secili = n
        self.ciz()
        self._lp_fired = False
        if self.on_long_press:
            self._lp_dis = n
            self._lp_timer = self.after(500, self._lp_tetikle)

    def _lp_tetikle(self):
        self._lp_timer = None
        if self._lp_dis is not None and self.on_long_press:
            self._lp_fired = True
            self.on_long_press(self._lp_dis)

    def _lp_iptal(self):
        if self._lp_timer is not None:
            self.after_cancel(self._lp_timer)
            self._lp_timer = None
        self._lp_dis = None

    def _surukle(self, e):
        if self._surukle_sira is None:
            return
        hit = map_hit(e.x, e.y)
        if hit is None or hit[0] not in self._surukle_sira:
            self._lp_iptal()
            return
        n = hit[0]
        if n != self._lp_dis:
            self._lp_iptal()
        i0 = self._surukle_sira.index(self._surukle_disler[0])
        i1 = self._surukle_sira.index(n)
        lo, hi = (i0, i1) if i0 <= i1 else (i1, i0)
        disler = self._surukle_sira[lo:hi + 1]
        if disler != self._surukle_disler:
            self._surukle_disler = disler
            self.secili = n
            self.ciz()

    def _birak(self, e):
        self._lp_iptal()
        lp_fired = self._lp_fired
        self._lp_fired = False
        disler = self._surukle_disler
        zone = self._surukle_zone
        self._surukle_sira = None
        self._surukle_disler = []
        self._surukle_zone = None
        if not disler:
            return
        if lp_fired:
            # uzun-basma artıq öz əməliyyatını etdi (məs. foto açdı) — buraxılanda
            # əlavə olaraq müalicə tətbiq olunmasın
            self.ciz()
            return
        if len(disler) > 1 and self.on_range:
            self.on_range(disler, zone)
        else:
            self.on_click(disler[0], zone)
        self.ciz()

    def yukle(self, td, dis_tasi=False):
        self.tedaviler = {str(k):v for k,v in td.items()}
        self.dis_tasi = dis_tasi
        self.secili = None
        self._surukle_sira = None
        self._surukle_disler = []
        self._surukle_zone = None
        self.ciz()

# ── ANA UYGULAMA ──────────────────────────────────────────────────────────────
class DentalApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(t("Dental Tedavi Planlama"))
        try:
            if os.path.exists(ICON_FILE): self.iconbitmap(ICON_FILE)
        except Exception: pass
        self.geometry("1400x820")
        # büyük harita için tam ekran çalışma alanı (mainloop başladıktan sonra)
        self.after(50, self._maximize)
        self.hastalar = veri_yukle()
        self.randevular = randevu_yukle()
        try: os.makedirs(self._BXD_BASE, exist_ok=True)   # yedek klasörü açılışta hazır olsun
        except Exception: pass
        self.aktif_hid = None
        self.aktif_vid = None          # aktif vaka ID
        self.expanded_hids = set()    # accordion açık hastalar
        self.secili_tedavi = "kanal"
        self.t_btnler = []
        self._dis_tasi_btn_p0 = None   # Plan tab button
        self._dis_tasi_btn_p1 = None   # Müalicə tab button
        self._dis_tasi_btn_pI = None   # İlkin Vəziyyət tab button
        self._last_dis_ilkin: dict = {}  # {(hid,vid): n} ilkin sekmesi son seçili diş
        self.hasta_arama_var = tk.StringVar()
        self.hasta_arama_var.trace_add("write", lambda *_: self._lista_guncelle())
        self._build()
        self._lista_guncelle()
        # ── X-ray folder watcher ──────────────────────────────────────────────
        self._xray_q              = queue.Queue()
        self._xray_observer       = None
        self._pending_xray_dialog = False
        self._xray_confirmed_session = None  # (hid, vid) last confirmed; None = ask again
        self._last_dis_per_vaka: dict = {}   # {(hid,vid): (n0, n1)} last selected tooth per arch
        self._vr_xray_idx: dict = {}         # {hid: index into rontgenler} currently shown on VR
        self._start_xray_watcher()
        _start_xray_http_server()
        self.after(2000, self._check_xray_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── hastalar.json-un gecikdirilmiş/arxa plan disk yazısı (performans) ──
        self._hastalar_save_islemde = False
        self.after(500, self._hastalar_save_tick)

        # ── 5 saniyəlik "dəyişiklikdən sonra yedəklə" dövrəsi (.bxd) ───────────
        self.after(1000, self._bxd_backup_tick)

        # ── Bulud senkronizasyonu (self.cloud_status_lbl _build() içində qurulub) ──
        self._cloud_tick_started = False
        if cloud_configured() and AYARLAR.get("sb_access_token"):
            self._cloud_tick_started = True
            self.after(3000, self._cloud_auto_sync_tick)

        # ── Otomatik güncelleme kontrolü (sessiz — açılıştan birkaç saniye sonra) ──
        self.after(4000, lambda: self._guncelleme_kontrol_et(sessiz=True))

        # ── Səsli asistent (aktiv olarsa, açılışdan 2sn sonra arxa planda başlayır) ──
        self._sesli_stop_event = threading.Event()
        self._sesli_thread = None
        if sesli_asistent_aktiv():
            self.after(2000, self._sesli_baslat)

        # ── Təkrarlanan pəncərələrin qarşısını almaq üçün referanslar (yalnız 1 instansı) ──
        self._randevu_win = None
        self._statistika_win = None
        self._foto_win = None

        # ── Ümumi AI Söhbəti (push-to-talk, F9) — wake-word sistemindən MÜSTƏQİL ──
        self._chat_win = None
        self._chat_hotkey_qeydli = None
        self._chat_ptt_aktiv = False
        self._chat_ptt_stream = None
        self._chat_ptt_buffer = []
        self._chat_tarixce = []
        self._chat_pending_confirm = None
        self._tts_stop_event = threading.Event()
        if chat_asistent_aktiv() and (AYARLAR.get("anthropic_api_key") or "").strip():
            self.after(2200, self._chat_baslat)

        # ── Randevu vaxtı çatanda "gəldi" təsdiqi (2 dəqiqə cavabsız qalsa avtomatik) ──
        self._randevu_prompted = set()
        self.after(15000, self._randevu_tick)

    def _maximize(self):
        try:
            self.state("zoomed")
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(1,weight=1)
        self.grid_rowconfigure(0,weight=1)

        sol = ctk.CTkFrame(self,width=240,corner_radius=0,fg_color=TH["sidebar"])
        sol.grid(row=0,column=0,sticky="nsew"); sol.grid_propagate(False)
        sol.grid_rowconfigure(6,weight=1); sol.grid_columnconfigure(0,weight=1)
        ctk.CTkLabel(sol,text=t("🦷  P A S İ E N T L Ə R"),font=ctk.CTkFont(size=13,weight="bold"),
                     text_color=TH["txt_faint"]).grid(row=0,column=0,padx=14,pady=(20,10),sticky="w")
        ctk.CTkButton(sol,text=t("+  Yeni Pasient"),height=38,corner_radius=10,
                      fg_color=TH["accent"],hover_color=TH["accent_hover"],
                      font=ctk.CTkFont(size=13,weight="bold"),
                      command=self._yeni_hasta).grid(row=1,column=0,padx=10,pady=(0,6),sticky="ew")
        ctk.CTkButton(sol,text=t("📅  Randevu Təqvimi"),height=38,corner_radius=10,
                      fg_color=TH["indigo"],hover_color=TH["indigo_hover"],
                      font=ctk.CTkFont(size=13,weight="bold"),
                      command=self._randevu_ac).grid(row=2,column=0,padx=10,pady=(0,6),sticky="ew")
        ctk.CTkButton(sol,text=t("📊  Statistika"),height=38,corner_radius=10,
                      fg_color=TH["ok"],hover_color=TH["ok_hover"],
                      font=ctk.CTkFont(size=13,weight="bold"),
                      command=self._statistika_ac).grid(row=3,column=0,padx=10,pady=(0,6),sticky="ew")
        ctk.CTkButton(sol,text=t("📂  Pasient Yükle (.bxd)"),height=34,corner_radius=10,
                      fg_color="transparent",border_width=1,border_color="#3B5E8F",
                      text_color="#B9CDEB",hover_color=TH["sidebar_row"],
                      command=self._hasta_yukle).grid(row=4,column=0,padx=10,pady=(0,10),sticky="ew")
        arama_f = ctk.CTkFrame(sol,fg_color="transparent")
        arama_f.grid(row=5,column=0,padx=10,pady=(0,6),sticky="ew")
        arama_f.grid_columnconfigure(0,weight=1)
        ctk.CTkEntry(arama_f,textvariable=self.hasta_arama_var,
                     placeholder_text=t("🔍 Hasta axtar…"),height=32,corner_radius=8,
                     border_color=TH["accent_soft"],fg_color=TH["sidebar_row"],
                     text_color="white").grid(row=0,column=0,sticky="ew")
        self.scroll = ctk.CTkScrollableFrame(sol,fg_color="transparent")
        self.scroll.grid(row=6,column=0,padx=4,pady=4,sticky="nsew")
        ctk.CTkButton(sol,text=t("⚙  Ayarlar"),height=32,corner_radius=10,
                      fg_color="transparent",border_width=1,border_color="#3B5E8F",
                      text_color="#B9CDEB",hover_color=TH["sidebar_row"],
                      command=self._ayarlar_ac).grid(row=7,column=0,padx=10,pady=(4,4),sticky="ew")
        self.cloud_status_lbl = ctk.CTkLabel(sol,text="",font=ctk.CTkFont(size=10),
                                             text_color="#7A99C4")
        self.cloud_status_lbl.grid(row=8,column=0,padx=12,pady=(0,2),sticky="w")
        self._cloud_status_guncelle()
        self.sesli_status_lbl = ctk.CTkLabel(sol,text="",font=ctk.CTkFont(size=10),
                                             text_color="#7A99C4")
        self.sesli_status_lbl.grid(row=9,column=0,padx=12,pady=(0,10),sticky="w")

        self.sag = ctk.CTkFrame(self,fg_color=TH["bg"],corner_radius=0)
        self.sag.grid(row=0,column=1,sticky="nsew")
        self.sag.grid_columnconfigure(0,weight=1); self.sag.grid_rowconfigure(0,weight=1)

        self.kars = ctk.CTkLabel(self.sag,text=t("Pasient seçin və ya yeni pasient əlavə edin"),
                                  font=ctk.CTkFont(size=15),text_color=TH["txt_sub"])
        self.kars.grid(row=0,column=0)

        self.icerik = ctk.CTkFrame(self.sag,fg_color=TH["bg"])
        self.icerik.grid(row=0,column=0,sticky="nsew")
        self.icerik.grid_remove()
        self.icerik.grid_columnconfigure(0,weight=1)
        self.icerik.grid_rowconfigure(1,weight=1)
        self._build_icerik()

    def _build_icerik(self):
        ust = ctk.CTkFrame(self.icerik,fg_color=TH["panel"],height=58,corner_radius=14)
        ust.grid(row=0,column=0,sticky="ew",padx=10,pady=(10,4)); ust.grid_propagate(False)
        ust.grid_columnconfigure(1,weight=1)
        self.kart_lbl = ctk.CTkLabel(ust,text="",font=ctk.CTkFont(size=16,weight="bold"),
                                      text_color=TH["txt"])
        self.kart_lbl.grid(row=0,column=0,padx=18,pady=14,sticky="w")
        sf = ctk.CTkFrame(ust,fg_color="transparent")
        sf.grid(row=0,column=1,padx=10,sticky="e")
        self.step_btn=[]
        for sid,etiket in [(2,"İlkin Vəziyyət"),(0,"Plan"),(1,"Müalicə")]:
            b=ctk.CTkButton(sf,text=t(etiket),width=110,height=32,corner_radius=16,
                            command=lambda s=sid:self._step(s))
            b.pack(side="left",padx=4); self.step_btn.append((sid,b))

        # VR düymələri — Ayarlar'dakı "VR Rejimi" açarı ilə göstərilib/gizlədilir
        self._vr_dugmeleri_sf = sf
        self._btn_tedaviye_kec = ctk.CTkButton(sf,text=t("🔬  Tedaviyə Keç"),width=150,height=32,corner_radius=16,
                       fg_color=TH["ok"],hover_color=TH["ok_hover"],
                       text_color="white",
                       font=ctk.CTkFont(size=12,weight="bold"),
                       command=self._tedaviye_kec)

        self._btn_vr_duzenle = ctk.CTkButton(sf,text=t("🥽  VR Düzenle"),width=120,height=32,corner_radius=16,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       text_color="white",
                       font=ctk.CTkFont(size=12,weight="bold"),
                       command=self._vr_duzenle)

        self._btn_vr_layout = ctk.CTkButton(sf,text=t("Layout Kaydet (VR)"),width=150,height=32,corner_radius=16,
                       fg_color=TH["indigo"],hover_color=TH["indigo_hover"],
                       text_color="white",
                       font=ctk.CTkFont(size=11,weight="bold"),
                       command=self._vr_layout_kaydet)
        self._vr_ui_guncelle()

        self.tab = ctk.CTkTabview(self.icerik,fg_color="transparent",
                                   segmented_button_fg_color=TH["accent_soft"],
                                   segmented_button_selected_color=TH["accent"],
                                   segmented_button_selected_hover_color=TH["accent_hover"],
                                   segmented_button_unselected_color="#8FB0DC",
                                   segmented_button_unselected_hover_color="#7FA0CE",
                                   text_color="white")
        self.tab.grid(row=1,column=0,sticky="nsew",padx=10,pady=(0,10))
        self.pI = self.tab.add(t("İlkin Vəziyyət"))
        self.p0 = self.tab.add(t("Plan"))
        self.p1 = self.tab.add(t("Müalicə"))
        self.p2 = self.tab.add(t("Qeyd Dəftəri"))
        self.p3 = self.tab.add(t("Rentgen"))
        self._build_pI(); self._build_p0(); self._build_p1()
        self._build_notlar(); self._build_rontgen()
        self._step(0)

    def _build_pI(self):
        self.pI.grid_columnconfigure(0,weight=1); self.pI.grid_rowconfigure(0,weight=1)
        content=ctk.CTkFrame(self.pI,fg_color="transparent")
        content.grid(row=0,column=0,sticky="nsew",padx=4,pady=4)
        content.grid_columnconfigure(0,weight=1); content.grid_rowconfigure(1,weight=1)
        self._tbar(content, 0, tab="ilkin")
        arch_f=ctk.CTkFrame(content,fg_color="transparent")
        arch_f.grid(row=1,column=0,sticky="nsew")
        self.arch2=ArchCanvas(arch_f,self._tikla2,on_range=self._tikla2_range,
                               on_long_press=self._dis_uzun_bas1)
        self.arch2.band_label = t("Diş Daşı")
        self.arch2.ilkin = True
        self.arch2.pack(side="left",anchor="n",padx=10,pady=8)

        infoI_f=ctk.CTkFrame(arch_f,fg_color=TH["panel"],width=270,corner_radius=12)
        infoI_f.pack(side="left",fill="y",padx=(0,8),pady=8)
        infoI_f.pack_propagate(False)
        ctk.CTkLabel(infoI_f,text=t("SEÇİLİ DİŞ"),
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.dis_info2=ctk.CTkLabel(infoI_f,text="—",font=ctk.CTkFont(size=14,weight="bold"),
                                     text_color=TH["txt"],wraplength=250,justify="left")
        self.dis_info2.pack(pady=4)
        ctk.CTkLabel(infoI_f,text=t("İLKİN VƏZİYYƏT"),
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.plan_liste2=ctk.CTkScrollableFrame(infoI_f,fg_color="transparent")
        self.plan_liste2.pack(fill="both",expand=True,padx=6,pady=4)

        foto_fI=ctk.CTkFrame(arch_f,fg_color=TH["panel"],corner_radius=12)
        foto_fI.pack(side="left",fill="both",expand=True,padx=(0,8),pady=8)
        ctk.CTkLabel(foto_fI,text=t("İNTRAORAL VƏ ÜZ FOTOLARI"),
                     font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["accent"]).pack(pady=(12,2),padx=10,anchor="w")
        ctk.CTkButton(foto_fI,text=t("+ Foto Əlavə Et"),height=28,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       command=lambda: self._foto_ekle("ilkin")).pack(padx=10,pady=(2,4),anchor="w")
        self.foto_scroll_pI=ctk.CTkScrollableFrame(foto_fI,fg_color="transparent")
        self.foto_scroll_pI.pack(fill="both",expand=True,padx=4,pady=(0,4))

    def _build_p0(self):
        self.p0.grid_columnconfigure(0,weight=1)
        self.p0.grid_rowconfigure(1,weight=1)
        form = ctk.CTkFrame(self.p0,fg_color=TH["panel"],corner_radius=12)
        form.grid(row=0,column=0,sticky="ew",padx=8,pady=(6,4))
        form.grid_columnconfigure((0,1,2,3,4,5,6),weight=1)
        for i,(lbl,attr) in enumerate([("Ad Soyad","p0_ad"),("Yaş","p0_yas"),
                ("Telefon","p0_tel"),("TC/ID","p0_tc"),("Qeyd","p0_not")]):
            lbl = t(lbl)
            ctk.CTkLabel(form,text=lbl,font=ctk.CTkFont(size=11,weight="bold"),
                         text_color=TH["txt_sub"]).grid(row=0,column=i,padx=8,pady=(8,2),sticky="w")
            e=ctk.CTkEntry(form,placeholder_text=lbl,height=32,corner_radius=8,
                           border_color=TH["accent_soft"],fg_color=TH["panel_soft"])
            e.grid(row=1,column=i,padx=8,pady=(0,8),sticky="ew")
            setattr(self,attr,e)
        ctk.CTkLabel(form,text=t("Cinsiyət"),font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["txt_sub"]).grid(row=0,column=5,padx=8,pady=(8,2),sticky="w")
        self.p0_cinsiyet_var = tk.StringVar(value=t("—"))
        self.p0_cinsiyet = ctk.CTkOptionMenu(form, variable=self.p0_cinsiyet_var,
                values=[t(l) for _,l in CINSIYET_SECENEKLERI],
                height=32, corner_radius=8,
                fg_color=TH["panel_soft"], button_color=TH["accent"],
                button_hover_color=TH["accent_hover"])
        self.p0_cinsiyet.grid(row=1,column=5,padx=8,pady=(0,8),sticky="ew")
        ctk.CTkLabel(form,text=t("Şəhər"),font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["txt_sub"]).grid(row=0,column=6,padx=8,pady=(8,2),sticky="w")
        self.p0_sheher=ctk.CTkEntry(form,placeholder_text=t("Şəhər"),height=32,corner_radius=8,
                       border_color=TH["accent_soft"],fg_color=TH["panel_soft"])
        self.p0_sheher.grid(row=1,column=6,padx=8,pady=(0,8),sticky="ew")
        kb=ctk.CTkButton(form,text=t("💾 Saxla"),width=90,height=32,corner_radius=8,
                          fg_color=TH["accent"],hover_color=TH["accent_hover"],
                          font=ctk.CTkFont(size=12,weight="bold"),command=self._kaydet)
        kb.grid(row=1,column=7,padx=8,pady=(0,8)); self.p0_kaydet=kb

        content=ctk.CTkFrame(self.p0,fg_color="transparent")
        content.grid(row=1,column=0,sticky="nsew",padx=4,pady=4)
        content.grid_columnconfigure(0,weight=1); content.grid_rowconfigure(1,weight=1)
        self._tbar(content, 0, tab="plan")
        arch_f=ctk.CTkFrame(content,fg_color="transparent")
        arch_f.grid(row=1,column=0,sticky="nsew")

        self.arch0=ArchCanvas(arch_f,self._tikla0,on_range=self._tikla0_range)
        self.arch0.pack(side="left",anchor="n",padx=10,pady=8)

        info_f=ctk.CTkFrame(arch_f,fg_color=TH["panel"],width=220,corner_radius=12)
        info_f.pack(side="left",fill="both",expand=True,padx=(0,8),pady=8)
        ctk.CTkLabel(info_f,text=t("SEÇİLİ DİŞ"),
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.dis_info=ctk.CTkLabel(info_f,text="—",font=ctk.CTkFont(size=14,weight="bold"),
                                    text_color=TH["txt"],wraplength=320,justify="left")
        self.dis_info.pack(pady=4)
        ctk.CTkLabel(info_f,text=t("PLANLANMIŞ"),
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.plan_liste=ctk.CTkScrollableFrame(info_f,fg_color="transparent")
        self.plan_liste.pack(fill="both",expand=True,padx=6,pady=4)

    def _build_p1(self):
        self.p1.grid_columnconfigure(0,weight=1); self.p1.grid_rowconfigure(0,weight=1)
        content=ctk.CTkFrame(self.p1,fg_color="transparent")
        content.grid(row=0,column=0,sticky="nsew",padx=4,pady=4)
        content.grid_columnconfigure(0,weight=1); content.grid_rowconfigure(1,weight=1)
        self._tbar(content, 0, tab="tedavi")
        arch_f=ctk.CTkFrame(content,fg_color="transparent")
        arch_f.grid(row=1,column=0,sticky="nsew")

        timeline_f=ctk.CTkFrame(content,fg_color=TH["panel"],corner_radius=12,height=128)
        timeline_f.grid(row=2,column=0,sticky="ew",pady=(6,0))
        timeline_f.grid_propagate(False)
        timeline_f.grid_columnconfigure(0,weight=1); timeline_f.grid_rowconfigure(1,weight=1)
        ctk.CTkLabel(timeline_f,text=t("MÜALİCƏ ZAMAN XƏTTİ"),
                     font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["accent"]).grid(row=0,column=0,sticky="w",padx=14,pady=(6,0))
        self.muayine_xetti=ctk.CTkFrame(timeline_f,fg_color="transparent")
        self.muayine_xetti.grid(row=1,column=0,sticky="nsew",padx=6,pady=(0,4))
        self.arch1=ArchCanvas(arch_f,self._tikla1,on_range=self._tikla1_range,
                               on_long_press=self._dis_uzun_bas1)
        self.arch1.pack(side="left",anchor="n",padx=10,pady=8)

        info1_f=ctk.CTkFrame(arch_f,fg_color=TH["panel"],width=270,corner_radius=12)
        info1_f.pack(side="left",fill="y",padx=(0,8),pady=8)
        info1_f.pack_propagate(False)
        ctk.CTkLabel(info1_f,text=t("SEÇİLİ DİŞ"),
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.dis_info1_f=ctk.CTkFrame(info1_f,fg_color="transparent")
        self.dis_info1_f.pack(fill="x",padx=4,pady=4)
        self._dis_info1_render(None, None)
        ctk.CTkLabel(info1_f,text=t("MÜALİCƏ OLUNMUŞ"),
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.plan_liste1=ctk.CTkScrollableFrame(info1_f,fg_color="transparent")
        self.plan_liste1.pack(fill="both",expand=True,padx=6,pady=4)

        foto_f1=ctk.CTkFrame(arch_f,fg_color=TH["panel"],corner_radius=12)
        foto_f1.pack(side="left",fill="both",expand=True,padx=(0,8),pady=8)
        ctk.CTkLabel(foto_f1,text=t("İNTRAORAL VƏ ÜZ FOTOLARI"),
                     font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["accent"]).pack(pady=(12,2),padx=10,anchor="w")
        ctk.CTkButton(foto_f1,text=t("+ Foto Əlavə Et"),height=28,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       command=lambda: self._foto_ekle("tedavi")).pack(padx=10,pady=(2,4),anchor="w")
        self.foto_scroll_p1=ctk.CTkScrollableFrame(foto_f1,fg_color="transparent")
        self.foto_scroll_p1.pack(fill="both",expand=True,padx=4,pady=(0,4))

    def _build_notlar(self):
        self.p2.grid_columnconfigure(0,weight=1)
        self.p2.grid_rowconfigure(1,weight=1)

        # Üst bar
        bar=ctk.CTkFrame(self.p2,fg_color=TH["panel"],corner_radius=12)
        bar.grid(row=0,column=0,sticky="ew",padx=8,pady=(6,4))
        bar.grid_columnconfigure(0,weight=1)
        self.not_baslik=ctk.CTkEntry(bar,placeholder_text=t("Qeyd başlığı..."),height=32,
                                      corner_radius=8,border_color=TH["accent_soft"],
                                      fg_color=TH["panel_soft"])
        self.not_baslik.grid(row=0,column=0,padx=10,pady=10,sticky="ew")
        self.not_ekle_btn=ctk.CTkButton(bar,text=t("+ Qeyd Əlavə Et"),width=130,height=32,
                                          corner_radius=8,fg_color=TH["accent"],
                                          hover_color=TH["accent_hover"],
                                          font=ctk.CTkFont(size=12,weight="bold"),
                                          command=self._not_ekle)
        self.not_ekle_btn.grid(row=0,column=1,padx=10,pady=10)

        # Not listesi
        self.not_scroll=ctk.CTkScrollableFrame(self.p2,fg_color="transparent")
        self.not_scroll.grid(row=1,column=0,sticky="nsew",padx=8,pady=4)

        # Alt: yeni not alanı
        alt=ctk.CTkFrame(self.p2,fg_color=TH["panel"],corner_radius=12)
        alt.grid(row=2,column=0,sticky="ew",padx=8,pady=4)
        alt.grid_columnconfigure(0,weight=1)
        ctk.CTkLabel(alt,text=t("YENİ QEYD"),
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).grid(
                     row=0,column=0,padx=10,pady=(8,2),sticky="w")
        self.not_metin=ctk.CTkTextbox(alt,height=80,corner_radius=8,
                                       border_width=1,border_color=TH["accent_soft"],
                                       fg_color=TH["panel_soft"])
        self.not_metin.grid(row=1,column=0,columnspan=2,padx=10,pady=(0,10),sticky="ew")

    def _not_ekle(self):
        try:
            if not self.aktif_hid:
                tk.messagebox.showwarning(t("Xəbərdarlıq"), t("Əvvəlcə bir hasta seçin."))
                return
            baslik = self.not_baslik.get().strip()
            metin  = self.not_metin.get("1.0", "end").strip()
            if not metin:
                tk.messagebox.showwarning(t("Xəbərdarlıq"), t("Qeyd mətni boş ola bilməz."))
                return
            h = self.hastalar[self.aktif_hid]
            h.setdefault("notlar_liste", []).append({
                "tarih":  datetime.now().strftime("%d.%m.%Y %H:%M"),
                "baslik": baslik or "Qeyd",
                "metin":  metin,
            })
            veri_kaydet(self.hastalar)
            self.not_baslik.delete(0, "end")
            self.not_metin.delete("1.0", "end")
            self._notlar_guncelle()
            self._not_disler_yenile()
            self.not_ekle_btn.configure(text="Əlavə edildi ✓", fg_color=TH["ok"])
            self.after(2000, lambda: self.not_ekle_btn.configure(
                text="+ Qeyd Əlavə Et", fg_color=TH["accent"]))
        except Exception as exc:
            import traceback
            tk.messagebox.showerror(t("Qeyd Əlavə Etmə Xətası"), traceback.format_exc())

    def _notlar_guncelle(self):
        for w in self.not_scroll.winfo_children(): w.destroy()
        if not self.aktif_hid: return
        h=self.hastalar[self.aktif_hid]
        notlar=h.get("notlar_liste",[])
        if isinstance(notlar,str): notlar=[{"tarih":"","baslik":"Qeyd","metin":notlar}]
        for i,not_ in enumerate(reversed(notlar)):
            kart=ctk.CTkFrame(self.not_scroll,fg_color=TH["panel"],
                               corner_radius=8)
            kart.pack(fill="x",padx=4,pady=4)
            kart.grid_columnconfigure(0,weight=1)
            # Başlık + tarih
            baslik_f=ctk.CTkFrame(kart,fg_color=TH["accent_soft"],corner_radius=8)
            baslik_f.pack(fill="x",padx=8,pady=(8,4))
            ctk.CTkLabel(baslik_f,
                         text=not_.get("baslik","Qeyd"),
                         font=ctk.CTkFont(size=12,weight="bold"),
                         text_color=TH["accent"]).pack(side="left",padx=8,pady=4)
            ctk.CTkLabel(baslik_f,
                         text=not_.get("tarih",""),
                         font=ctk.CTkFont(size=10),
                         text_color=TH["txt_faint"]).pack(side="right",padx=8,pady=4)
            # Metin
            ctk.CTkLabel(kart,
                         text=not_.get("metin",""),
                         font=ctk.CTkFont(size=12),
                         text_color=TH["txt"],
                         wraplength=500,anchor="w").pack(
                         anchor="w",padx=12,pady=(0,8))
            # Sil butonu
            idx=len(notlar)-1-i
            ctk.CTkButton(kart,text=t("Sil"),width=50,height=22,
                           fg_color=TH["danger_bg"],text_color=TH["danger_text"],
                           hover_color=TH["danger_hover"],
                           command=lambda ix=idx:self._not_sil(ix)).pack(
                           anchor="e",padx=8,pady=(0,6))

    def _not_sil(self,idx):
        if not self.aktif_hid: return
        h=self.hastalar[self.aktif_hid]
        notlar=h.get("notlar_liste",[])
        if 0<=idx<len(notlar):
            notlar.pop(idx)
            veri_kaydet(self.hastalar)
            self._notlar_guncelle()
            self._not_disler_yenile()

    def _not_disler_yenile(self):
        """Aktiv pasientin qeyd dəftərinə görə Plan/Müalicə xəritələrindəki
        '#NN' nida nişanlarını təzələyir (qeyd əlavə/silinəndə çağırılır)."""
        if not self.aktif_hid: return
        disler = _not_edilmis_disler(self.hastalar[self.aktif_hid])
        self.arch0.not_disler = disler
        self.arch0.ciz()
        self.arch1.not_disler = disler
        self.arch1.ciz()

    def _build_rontgen(self):
        self.p3.grid_columnconfigure(0,weight=1)
        self.p3.grid_rowconfigure(1,weight=1)

        bar=ctk.CTkFrame(self.p3,fg_color=TH["panel"],corner_radius=12)
        bar.grid(row=0,column=0,sticky="ew",padx=8,pady=(6,4))
        ctk.CTkButton(bar,text=t("+ Rentgen Əlavə Et"),height=34,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       font=ctk.CTkFont(size=12,weight="bold"),
                       command=self._rontgen_ekle).pack(side="left",padx=10,pady=10)
        ctk.CTkLabel(bar,text=t("PNG, JPG, JPEG, BMP desteklenir"),
                     font=ctk.CTkFont(size=11),text_color=TH["txt_sub"]).pack(
                     side="left",padx=6)

        # VR röntgen naviqasiyası — Quest-də göstərilən şəkli irəli/geri dəyişir
        ctk.CTkButton(bar,text="▶",width=34,height=34,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       font=ctk.CTkFont(size=13,weight="bold"),
                       command=lambda:self._xray_vr_nav(+1)).pack(side="right",padx=(4,10),pady=10)
        self.xray_vr_nav_lbl=ctk.CTkLabel(bar,text="",width=70,
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["txt_sub"])
        self.xray_vr_nav_lbl.pack(side="right",padx=4)
        ctk.CTkButton(bar,text="◀",width=34,height=34,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       font=ctk.CTkFont(size=13,weight="bold"),
                       command=lambda:self._xray_vr_nav(-1)).pack(side="right",padx=4,pady=10)

        self.rontgen_scroll=ctk.CTkScrollableFrame(self.p3,fg_color="transparent")
        self.rontgen_scroll.grid(row=1,column=0,sticky="nsew",padx=8,pady=4)

    def _rontgen_ekle(self):
        if not self.aktif_hid: return
        from tkinter import filedialog, messagebox
        raw_desen = " ".join(f"*{e}" for e in RAW_UZANTILAR)
        dosya=filedialog.askopenfilename(
            title="Rentgen Seç",
            filetypes=[("Görüntü","*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.dcm" +
                        (" " + raw_desen if raw_desen.strip() else "")),
                       ("RAW (Canon/Nikon)", raw_desen or "*.cr2"),
                       ("Tüm","*.*")])
        if not dosya: return
        import base64
        from datetime import datetime
        img = sekli_ac(dosya)
        if img is None:
            messagebox.showerror(t("Yükleme Hatası"), t("Şəkil açılmadı və ya dəstəklənməyən formatdır."))
            return
        b64 = base64.b64encode(sekli_sixisdir(img)).decode()
        h=self.hastalar[self.aktif_hid]
        h.setdefault("rontgenler",[]).append({
            "tarih":datetime.now().strftime("%d.%m.%Y %H:%M"),
            "dosya":os.path.splitext(os.path.basename(dosya))[0] + ".jpg",
            "data":b64
        })
        veri_kaydet(self.hastalar)
        self._auto_bxd_backup(self.aktif_hid)
        new_idx = len(h["rontgenler"]) - 1
        self._vr_xray_idx[self.aktif_hid] = new_idx
        self._push_vr_xray_by_index(self.aktif_hid, new_idx)
        self._rontgen_guncelle()

    def _rontgen_guncelle(self):
        for w in self.rontgen_scroll.winfo_children(): w.destroy()
        if not self.aktif_hid: return
        h = self.hastalar[self.aktif_hid]
        rontgenler = h.get("rontgenler", [])
        if not rontgenler:
            ctk.CTkLabel(self.rontgen_scroll,
                         text=t("Henüz röntgen eklenmedi"),
                         text_color="#aaa").pack(pady=20)
            return

        COLS   = 5     # images per row
        THUMB  = 140   # thumbnail max-width in pixels
        RADIUS = 12    # rounded corner radius

        from PIL import Image, ImageDraw, ImageTk
        from io import BytesIO

        def _rounded(img: Image.Image, r: int) -> ImageTk.PhotoImage:
            """Return an ImageTk.PhotoImage with rounded corners (panel bg)."""
            w, h = img.size
            mask = Image.new("L", (w, h), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
            out = Image.new("RGBA", (w, h), (255, 255, 255, 0))
            out.paste(img.convert("RGBA"), mask=mask)
            # Composite onto the panel color so tk.Label bg=TH["panel"] blends seamlessly
            bg = Image.new("RGBA", (w, h), (*_hex_to_rgb(TH["panel"]), 255))
            bg.paste(out, mask=out.split()[3])
            return ImageTk.PhotoImage(bg.convert("RGB"))

        for row_start in range(0, len(rontgenler), COLS):
            row_f = ctk.CTkFrame(self.rontgen_scroll, fg_color="transparent")
            row_f.pack(fill="x", padx=2, pady=2)
            for col, (i, r) in enumerate(
                    ((row_start + c, rontgenler[row_start + c])
                     for c in range(COLS)
                     if row_start + c < len(rontgenler))):
                kart = ctk.CTkFrame(row_f, fg_color=TH["panel"], corner_radius=10)
                kart.pack(side="left", padx=3, pady=3, anchor="n")
                # Filename + date
                ctk.CTkLabel(kart,
                             text=r.get("dosya", ""),
                             font=ctk.CTkFont(size=10, weight="bold"),
                             wraplength=THUMB).pack(padx=6, pady=(5, 0))
                ctk.CTkLabel(kart,
                             text=r.get("tarih", ""),
                             font=ctk.CTkFont(size=9),
                             text_color="#999").pack(padx=6, pady=(0, 3))
                # Thumbnail with rounded corners
                try:
                    img_data = (base64.b64decode(r["thumb_b64"]) if r.get("thumb_b64")
                                else self._img_bytes(r))
                    img = Image.open(BytesIO(img_data)).convert("RGB")
                    ratio = THUMB / img.width if img.width > THUMB else 1
                    img = img.resize((int(img.width * ratio),
                                      int(img.height * ratio)), Image.LANCZOS)
                    photo = _rounded(img, RADIUS)
                    lbl = tk.Label(kart, image=photo, bg=TH["panel"], cursor="hand2",
                                   borderwidth=0, highlightthickness=0)
                    lbl.image = photo
                    lbl.pack(padx=6, pady=2)
                    lbl.bind("<Button-1>", lambda e, rd=r, ri=i: self._rontgen_secip_goster(rd, ri))
                except Exception as exc:
                    ctk.CTkLabel(kart, text=f"Yüklenemedi:\n{exc}",
                                 text_color="red",
                                 wraplength=THUMB).pack(padx=6, pady=4)
                # Delete button
                ctk.CTkButton(kart, text=t("Sil"), width=44, height=20,
                               fg_color=TH["danger_bg"], text_color=TH["danger_text"],
                               hover_color=TH["danger_hover"],
                               font=ctk.CTkFont(size=10),
                               command=lambda ix=i: self._rontgen_sil(ix)).pack(
                               anchor="e", padx=5, pady=(1, 5))

    def _rontgen_secip_goster(self, r, idx):
        """Thumbnail klik: tam ekran popup + bu röntgeni Quest VR-də göstər."""
        if self.aktif_hid:
            self._vr_xray_idx[self.aktif_hid] = idx
            self._push_vr_xray_by_index(self.aktif_hid, idx)
            self._xray_vr_nav_label_guncelle()
        self._rontgen_tam_goster(r)

    def _rontgen_tam_goster(self, r):
        """Rentgeni tam ekran popup'ta göstər"""
        import base64
        from PIL import Image, ImageTk
        from io import BytesIO

        pencere = tk.Toplevel(self)
        pencere.title(r.get("dosya","Rentgen"))
        pencere.configure(bg="black")

        # Ekran boyutunu al
        sw = pencere.winfo_screenwidth()
        sh = pencere.winfo_screenheight()

        img_data = self._img_bytes(r)
        img = Image.open(BytesIO(img_data)).convert("RGB")

        # Ekrana sığdır
        max_w = sw - 100
        max_h = sh - 100
        ratio = min(max_w/img.width, max_h/img.height, 1.0)
        new_w = int(img.width*ratio)
        new_h = int(img.height*ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)

        photo = ImageTk.PhotoImage(img)

        canvas = tk.Canvas(pencere, width=new_w, height=new_h,
                           bg="black", highlightthickness=0)
        canvas.pack()
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas._img = photo  # referans tut

        # Bilgi + kapat
        alt = tk.Frame(pencere, bg="#111")
        alt.pack(fill="x")
        tk.Label(alt, text=r.get("dosya",""),
                 fg="white", bg="#111",
                 font=("Arial",11)).pack(side="left", padx=10, pady=6)
        tk.Label(alt, text=r.get("tarih",""),
                 fg="#888", bg="#111",
                 font=("Arial",10)).pack(side="left", padx=4)
        tk.Button(alt, text="Kapat", command=pencere.destroy,
                  bg="#333", fg="white",
                  relief="flat", padx=12).pack(side="right", padx=10, pady=6)

        # ESC ile kapat
        pencere.bind("<Escape>", lambda e: pencere.destroy())
        pencere.bind("<Button-1>", lambda e: pencere.destroy())

        # Pencereyi ortala
        pencere.update_idletasks()
        x = (sw - pencere.winfo_width()) // 2
        y = (sh - pencere.winfo_height()) // 2
        pencere.geometry(f"+{x}+{y}")

    def _rontgen_sil(self,idx):
        if not self.aktif_hid: return
        from tkinter import messagebox
        if not messagebox.askyesno(t("Sil"), t("Bu röntgeni silmek istədiyinizə əminsiniz?")):
            return
        h=self.hastalar[self.aktif_hid]
        r=h.get("rontgenler",[])
        if 0<=idx<len(r):
            r.pop(idx)
            veri_kaydet(self.hastalar)
            self._rontgen_guncelle()
            n = len(r)
            new_idx = min(self._vr_xray_idx.get(self.aktif_hid, n - 1), n - 1) if n else None
            self._vr_xray_idx[self.aktif_hid] = new_idx
            self._push_vr_xray_by_index(self.aktif_hid, new_idx)
            self._xray_vr_nav_label_guncelle()

    # ── FOTO (intraoral / üz) ─────────────────────────────────────────────────

    def _foto_ad_unikallasdir(self, ad, haric=None):
        """Yazılan foto adını əsas (ilk) diş nömrəsinə görə aktiv vakadakı
        (ilkin+plan+tedavi) mövcud fotolarla toqquşmayacaq şəkildə unikallaşdırır —
        toqquşma varsa ardıcıl '.N' şəkkisi əlavə edir (#36 -> #36.1 -> #36.2 ...).
        `haric`: unikallıq sayılarkən nəzərə ALINMAYACAQ foto obyekti (öz adını
        dəyişdirərkən özü ilə toqquşmasın deyə)."""
        ad = (ad or "").strip()
        ilk = _foto_ilk_dis(ad)
        if ilk is None or not self.aktif_hid or not self.aktif_vid:
            return ad
        vaka = self.hastalar[self.aktif_hid]["vakalar"][self.aktif_vid]
        mevcud = 0
        for key in ("fotolar_ilkin", "fotolar_plan", "fotolar_tedavi"):
            for f in vaka.get(key, []) or []:
                if f is haric:
                    continue
                if _foto_ilk_dis(f.get("dosya", "")) == ilk:
                    mevcud += 1
        if mevcud == 0:
            return ad
        tokens = ad.split(" ", 1)
        tokens[0] = f"{tokens[0]}.{mevcud}"
        return " ".join(tokens)

    def _foto_ad_dialogu(self, mevcut_ad, basliq, on_kaydet):
        """Foto adlandırma dialoqu — giriş sahəsi '#' ilə başlayır (ya da mövcud
        adla doldurulur), space basılanda avtomatik yeni '#' əlavə edir (çox-dişli
        foto adları üçün, məs. '#36 #35 #34'). Saxla basılanda unikallaşdırılmış
        adı `on_kaydet(ad)` callback-inə ötürür."""
        dlg = ctk.CTkToplevel(self, fg_color=TH["bg"])
        dlg.title(basliq)
        dlg.geometry("380x150")
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        ctk.CTkLabel(dlg, text=t("Foto adı (diş nömrəsi ilə, məs: #36 #35)"),
                     font=ctk.CTkFont(size=11), text_color=TH["txt_sub"]).pack(
                     padx=16, pady=(16, 4), anchor="w")
        e = ctk.CTkEntry(dlg, height=34, corner_radius=8,
                          border_color=TH["accent_soft"], fg_color=TH["panel_soft"])
        e.pack(padx=16, pady=4, fill="x")
        e.insert(0, mevcut_ad or "#")
        e.icursor("end")
        e.focus_set()

        def _space_sonrasi_hash(event):
            pos = e.index("insert")
            e.insert(pos, " #")
            e.icursor(pos + 2)
            return "break"
        e.bind("<KeyPress-space>", _space_sonrasi_hash)

        def _kaydet():
            yeni = e.get().strip()
            dlg.destroy()
            if yeni:
                on_kaydet(yeni)
        e.bind("<Return>", lambda ev: _kaydet())

        btn_f = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_f.pack(fill="x", padx=16, pady=(10, 16))
        ctk.CTkButton(btn_f, text=t("Saxla"), command=_kaydet, height=30,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"]).pack(side="right")
        ctk.CTkButton(btn_f, text=t("İmtina"), command=dlg.destroy, height=30,
                      fg_color=TH["panel_soft"], hover_color=TH["sidebar_row"],
                      text_color=TH["txt"]).pack(side="right", padx=(0, 8))
        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"+{(sw-dlg.winfo_width())//2}+{(sh-dlg.winfo_height())//2}")
        self._pencereni_one_cek(dlg)

    def _foto_yenidenadlandir(self, foto):
        """Mövcud bir fotonun adını dəyişdirmək üçün adlandırma dialoqunu açır
        (foto kartındakı ad yazısına klikləndikdə çağırılır)."""
        def _kaydet_et(ad):
            foto["dosya"] = self._foto_ad_unikallasdir(ad, haric=foto)
            veri_kaydet(self.hastalar)
            self._foto_guncelle()
        self._foto_ad_dialogu(foto.get("dosya", "#"), t("Foto Adlandır"), _kaydet_et)

    def _foto_ekle(self, tab):
        if not self.aktif_hid or not self.aktif_vid: return
        from tkinter import filedialog, messagebox
        from datetime import datetime
        raw_desen = " ".join(f"*{e}" for e in RAW_UZANTILAR)
        dosya = filedialog.askopenfilename(
            title="Foto Seç",
            filetypes=[("Görüntü","*.png *.jpg *.jpeg *.bmp *.tif *.tiff" +
                        (" " + raw_desen if raw_desen.strip() else "")),
                       ("RAW (Canon/Nikon)", raw_desen or "*.cr2"),
                       ("Tüm","*.*")])
        if not dosya: return
        img = sekli_ac(dosya)
        if img is None:
            messagebox.showerror(t("Yükleme Hatası"), t("Şəkil açılmadı və ya dəstəklənməyən formatdır."))
            return
        b64 = base64.b64encode(sekli_sixisdir(img)).decode()
        vaka = self.hastalar[self.aktif_hid]["vakalar"][self.aktif_vid]
        key  = {"plan": "fotolar_plan", "tedavi": "fotolar_tedavi",
                "ilkin": "fotolar_ilkin"}[tab]

        def _kaydet_et(ad):
            ad = self._foto_ad_unikallasdir(ad)
            vaka.setdefault(key, []).append({
                "tarih": datetime.now().strftime("%d.%m.%Y %H:%M"),
                "dosya": ad,
                "data":  b64,
            })
            veri_kaydet(self.hastalar)
            self._foto_guncelle()

        self._foto_ad_dialogu("#", t("Foto Adlandır"), _kaydet_et)

    def _foto_guncelle(self):
        for scroll in (self.foto_scroll_p1, self.foto_scroll_pI):
            for w in scroll.winfo_children(): w.destroy()
        if not self.aktif_hid or not self.aktif_vid: return
        vaka = self.hastalar[self.aktif_hid]["vakalar"].get(self.aktif_vid, {})

        COLS   = 2
        THUMB  = 100
        RADIUS = 8

        from PIL import Image, ImageDraw, ImageTk
        from io import BytesIO

        def _rounded(img, r):
            w, h = img.size
            mask = Image.new("L",(w,h),0)
            ImageDraw.Draw(mask).rounded_rectangle((0,0,w-1,h-1),radius=r,fill=255)
            out = Image.new("RGBA",(w,h),(255,255,255,0))
            out.paste(img.convert("RGBA"),mask=mask)
            bg = Image.new("RGBA",(w,h),(*_hex_to_rgb(TH["panel"]),255))
            bg.paste(out, mask=out.split()[3])
            return ImageTk.PhotoImage(bg.convert("RGB"))

        def _render(scroll, fotolar, tab):
            if not fotolar:
                ctk.CTkLabel(scroll, text=t("Foto yoxdur"),
                             text_color="#aaa",
                             font=ctk.CTkFont(size=10)).pack(pady=8)
                return
            for row_start in range(0, len(fotolar), COLS):
                row_f = ctk.CTkFrame(scroll, fg_color="transparent")
                row_f.pack(fill="x", padx=2, pady=2)
                for col, (i, foto) in enumerate(
                        ((row_start+c, fotolar[row_start+c])
                         for c in range(COLS)
                         if row_start+c < len(fotolar))):
                    kart = ctk.CTkFrame(row_f, fg_color=TH["panel"], corner_radius=8)
                    kart.pack(side="left", padx=2, pady=2, anchor="n")
                    ad_lbl = ctk.CTkLabel(kart, text=foto.get("dosya",""),
                                 font=ctk.CTkFont(size=9,weight="bold"),
                                 text_color=TH["accent"], cursor="hand2",
                                 wraplength=THUMB)
                    ad_lbl.pack(padx=4,pady=(4,0))
                    ad_lbl.bind("<Button-1>", lambda e, fd=foto: self._foto_yenidenadlandir(fd))
                    ctk.CTkLabel(kart, text=foto.get("tarih",""),
                                 font=ctk.CTkFont(size=8),
                                 text_color="#999").pack(padx=4,pady=(0,2))
                    try:
                        img_data = (base64.b64decode(foto["thumb_b64"]) if foto.get("thumb_b64")
                                    else self._img_bytes(foto))
                        img = Image.open(BytesIO(img_data)).convert("RGB")
                        ratio = THUMB/img.width if img.width > THUMB else 1
                        img   = img.resize((int(img.width*ratio),int(img.height*ratio)),Image.LANCZOS)
                        photo = _rounded(img, RADIUS)
                        lbl   = tk.Label(kart, image=photo, bg=TH["panel"], cursor="hand2",
                                         borderwidth=0, highlightthickness=0)
                        lbl.image = photo
                        lbl.pack(padx=4, pady=2)
                        lbl.bind("<Button-1>", lambda e, fd=foto: self._foto_tam_goster(fd))
                    except Exception as exc:
                        ctk.CTkLabel(kart, text=f"Yüklenemedi:\n{exc}",
                                     text_color="red",
                                     wraplength=THUMB).pack(padx=4,pady=4)
                    ctk.CTkButton(kart, text=t("Sil"), width=40, height=18,
                                   fg_color=TH["danger_bg"], text_color=TH["danger_text"],
                                   hover_color=TH["danger_hover"],
                                   font=ctk.CTkFont(size=9),
                                   command=lambda ix=i, t=tab: self._foto_sil(ix, t)).pack(
                                   anchor="e", padx=4, pady=(1,4))

        _render(self.foto_scroll_p1, vaka.get("fotolar_tedavi", []), "tedavi")
        _render(self.foto_scroll_pI, vaka.get("fotolar_ilkin",  []), "ilkin")

    def _galeri_qur(self):
        """Aktiv vaka/pasient üçün bütün foto+rentgen elementlərini (tip, obyekt)
        cütləri olaraq qaytarır — tam-ekran baxıcıdakı irəli/geri naviqasiya və
        foto<->rentgen keçidi üçün (bax _foto_tam_goster)."""
        if not self.aktif_hid or not self.aktif_vid: return []
        h = self.hastalar[self.aktif_hid]
        vaka = h.get("vakalar", {}).get(self.aktif_vid, {})
        galeri = []
        for key in ("fotolar_ilkin", "fotolar_plan", "fotolar_tedavi"):
            for f in vaka.get(key, []) or []:
                galeri.append(("foto", f))
        for r in h.get("rontgenler", []) or []:
            galeri.append(("rontgen", r))
        return galeri

    def _dis_foto_galerisi(self, n):
        """Adında (istənilən yerdə) '#n' istinadı olan bütün foto/rentgenləri
        (tip, obyekt) cütləri olaraq, tam qalereyadakı sırada qaytarır — çox-dişli
        adlar da (məs. '#36 #35 #34') n==34/35/36 üçün uyğun gəlir."""
        return [(tip, obj) for tip, obj in self._galeri_qur()
                if n in _foto_dis_nomreleri(obj.get("dosya", ""))]

    def _dis_uzun_bas1(self, n):
        """Müalicə və ya İlkin Vəziyyət tabında dişə 0.5s basılı saxlananda,
        adında həmin diş nömrəsi olan foto/rentgen(lər) varsa, onları (yalnız bu
        dişə aid olanları) tam-ekranda naviqasiya ilə açır — hansı dişin üstündə
        basılmışıqsa, onun şəkilləri arasında ardıcıl baxıla bilsin deyə."""
        galeri = self._dis_foto_galerisi(n)
        if not galeri:
            return
        tip, obj = galeri[0]
        self._foto_tam_goster(obj, tip=tip, galeri=galeri, idx=0)

    def _foto_tam_goster(self, foto, tip="foto", galeri=None, idx=None):
        from PIL import Image, ImageTk, ImageFilter, ImageEnhance
        from io import BytesIO
        if self._foto_win is not None and self._foto_win.winfo_exists():
            try: self._foto_win.destroy()
            except Exception: pass
            self._foto_win = None
        if galeri is None:
            galeri = self._galeri_qur()
            idx = next((i for i, (_tp, ob) in enumerate(galeri) if ob is foto), None)
        if idx is None:
            galeri, idx = [(tip, foto)], 0

        pencere = tk.Toplevel(self)
        self._foto_win = pencere
        pencere.configure(bg="black")
        sw = pencere.winfo_screenwidth()
        sh = pencere.winfo_screenheight()
        # Pəncərə ölçüsü BÜTÜN qalereya üçün sabitdir (şəkildən şəkilə dəyişmir) —
        # fərqli nisbətli (ensiz/enli) fotolar bu ölçüyə "contain" olaraq sığdırılır,
        # boş qalan kənarlar isə şəklin özünün bulanıqlaşdırılmış "cover" fonu ilə
        # doldurulur (qara zolaq əvəzinə).
        FW = min(int(sw * 0.62), 1500)
        FH = min(int(sh * 0.72), 950)

        canvas = tk.Canvas(pencere, width=FW, height=FH, bg="black", highlightthickness=0)
        canvas.pack()
        alt = tk.Frame(pencere, bg="#111")
        alt.pack(fill="x")
        ad_lbl = tk.Label(alt, fg="white", bg="#111", font=("Arial", 11))
        ad_lbl.pack(side="left", padx=10, pady=6)
        tarix_lbl = tk.Label(alt, fg="#888", bg="#111", font=("Arial", 10))
        tarix_lbl.pack(side="left", padx=4)
        konum_lbl = tk.Label(alt, fg="#888", bg="#111", font=("Arial", 10))
        konum_lbl.pack(side="left", padx=12)

        def _kapat():
            if self._foto_win is pencere:
                self._foto_win = None
            pencere.destroy()

        durum = {"idx": idx}

        def _goster(i):
            durum["idx"] = i
            tp, ob = galeri[i]
            try:
                img = Image.open(BytesIO(self._img_bytes(ob))).convert("RGB")
            except Exception as exc:
                from tkinter import messagebox
                messagebox.showerror(t("Yükleme Hatası"), f"Şəkil açılmadı:\n{exc}")
                return
            # Arxa fon: şəkli FW×FH-i tam örtəcək (cover) nisbətdə böyüdüb mərkəzdən
            # kəsir, sonra bulanıqlaşdırıb bir az tündləşdirir.
            bg_oran = max(FW / img.width, FH / img.height)
            bg = img.resize((max(1, round(img.width * bg_oran)),
                             max(1, round(img.height * bg_oran))), Image.LANCZOS)
            bx0, by0 = (bg.width - FW) // 2, (bg.height - FH) // 2
            bg = bg.crop((bx0, by0, bx0 + FW, by0 + FH))
            bg = bg.filter(ImageFilter.GaussianBlur(30))
            bg = ImageEnhance.Brightness(bg).enhance(0.55)
            # Ön plan: şəkli FW×FH-ə tam sığacaq (contain) nisbətdə kiçildib mərkəzə yerləşdirir.
            fg_oran = min(FW / img.width, FH / img.height, 1.0)
            fg = img.resize((max(1, round(img.width * fg_oran)),
                             max(1, round(img.height * fg_oran))), Image.LANCZOS)
            bg.paste(fg, ((FW - fg.width) // 2, (FH - fg.height) // 2))
            photo = ImageTk.PhotoImage(bg)
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=photo)
            canvas._img = photo
            tip_etiketi = t("🩻 Rentgen") if tp == "rontgen" else t("📷 Foto")
            pencere.title(f"{tip_etiketi} — {ob.get('dosya','')}")
            ad_lbl.configure(text=f"{tip_etiketi} — {ob.get('dosya','')}")
            tarix_lbl.configure(text=ob.get("tarih", ""))
            konum_lbl.configure(text=f"{i+1} / {len(galeri)}" if len(galeri) > 1 else "")

        def _naviqasiya(delta):
            if len(galeri) > 1:
                _goster((durum["idx"] + delta) % len(galeri))

        if len(galeri) > 1:
            tk.Button(pencere, text="◀", command=lambda: _naviqasiya(-1),
                      bg="#222", fg="white", activebackground="#333", activeforeground="white",
                      relief="flat", bd=0, font=("Arial", 18), width=2).place(
                      relx=0.0, rely=0.46, anchor="w", x=6)
            tk.Button(pencere, text="▶", command=lambda: _naviqasiya(1),
                      bg="#222", fg="white", activebackground="#333", activeforeground="white",
                      relief="flat", bd=0, font=("Arial", 18), width=2).place(
                      relx=1.0, rely=0.46, anchor="e", x=-6)

        tk.Button(alt, text="Kapat", command=_kapat,
                  bg="#333", fg="white", relief="flat", padx=12).pack(
                  side="right", padx=10, pady=6)
        # Foto sahəsinə klik artıq bağlamır (yalnız "Kapat" düyməsi/Esc) — şəkli
        # yaxından baxmaq/klikləmək istəyəndə pəncərə səhvən bağlanmasın deyə.
        pencere.bind("<Escape>",  lambda e: _kapat())
        pencere.bind("<Left>",    lambda e: _naviqasiya(-1))
        pencere.bind("<Right>",   lambda e: _naviqasiya(1))
        pencere.protocol("WM_DELETE_WINDOW", _kapat)

        # Pəncərə ölçüsü sabit olduğundan yerləşdirmə YALNIZ bir dəfə hesablanır —
        # şəkillər arasında keçiddə pəncərə sıçramır/ölçü dəyişmir.
        pencere.update_idletasks()
        pencere.geometry(f"+{(sw-pencere.winfo_width())//2}+{(sh-pencere.winfo_height())//2}")
        _goster(idx)
        self._pencereni_one_cek(pencere)

    def _foto_sil(self, idx, tab):
        if not self.aktif_hid or not self.aktif_vid: return
        from tkinter import messagebox
        if not messagebox.askyesno(t("Sil"), t("Bu fotoğrafı silmek istədiyinizə əminsiniz?")):
            return
        vaka    = self.hastalar[self.aktif_hid]["vakalar"].get(self.aktif_vid, {})
        key     = {"plan": "fotolar_plan", "tedavi": "fotolar_tedavi",
                   "ilkin": "fotolar_ilkin"}[tab]
        fotolar = vaka.get(key, [])
        if 0 <= idx < len(fotolar):
            fotolar.pop(idx)
            veri_kaydet(self.hastalar)
            self._foto_guncelle()

    def _tbar(self, parent, row, tab="plan"):
        tb=ctk.CTkFrame(parent,fg_color=TH["panel"],corner_radius=12)
        tb.grid(row=row,column=0,sticky="ew",padx=8,pady=(4,0))
        for tid,lbl,rgb in TEDAVILER:
            if tid in ("kok", "curuk") and tab != "ilkin": continue
            if tid == "gozlem" and tab == "ilkin": continue
            if tid == "cekim" and tab == "ilkin": continue   # çəkim bir müalicə əməliyyatıdır, ilkin (diaqnostik) vəziyyətə aid deyil
            renk=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}" if rgb else "#888888"
            metin = t(lbl)
            btn_w = max(76, 16 + len(metin) * 8)   # uzun etiketlər (məs. "Kompozit Dolğu") sığsın
            b=ctk.CTkButton(tb,text=metin,width=btn_w,height=28,corner_radius=14,
                             fg_color=tint(renk),hover_color=tint(renk,0.72),
                             text_color=renk,
                             font=ctk.CTkFont(size=12,weight="bold"),
                             command=lambda t=tid:self._tedavi_sec(t))
            b.pack(side="left",padx=(10 if tid==TEDAVILER[0][0] else 3, 3),pady=8)
            self.t_btnler.append((tid,b,renk))
        dt_txt = t("Diş Daşı") if tab == "ilkin" else t("Diş Daşı Təmizliyi")
        dt_btn=ctk.CTkButton(tb,text=dt_txt,width=max(90 if tab=="ilkin" else 150, 16+len(dt_txt)*8),height=28,corner_radius=14,
                              fg_color=tint("#a020f0"),hover_color=tint("#a020f0",0.72),
                              text_color="#a020f0",
                              font=ctk.CTkFont(size=12,weight="bold"),
                              command=lambda t=tab: self._toggle_dis_tasi(t))
        dt_btn.pack(side="left",padx=(12,2),pady=8)
        if tab == "plan":
            self._dis_tasi_btn_p0 = dt_btn
        elif tab == "tedavi":
            self._dis_tasi_btn_p1 = dt_btn
        else:
            self._dis_tasi_btn_pI = dt_btn

    def _tedavi_sec(self,tid):
        self.secili_tedavi=tid
        for t,b,renk in self.t_btnler:
            if t==tid: b.configure(fg_color=renk,text_color="white")
            else:       b.configure(fg_color=tint(renk),text_color=renk)

    def _toggle_dis_tasi(self, tab):
        if not self.aktif_hid or not self.aktif_vid: return
        vaka = self.hastalar[self.aktif_hid]["vakalar"][self.aktif_vid]
        key  = {"plan": "dis_tasi_temizligi_plan",
                "tedavi": "dis_tasi_temizligi_tedavi",
                "ilkin": "dis_tasi_temizligi_ilkin"}[tab]
        vaka[key] = not vaka.get(key, False)
        veri_kaydet(self.hastalar)
        self._apply_dis_tasi_tab(tab)

    def _apply_dis_tasi_tab(self, tab):
        if not self.aktif_hid or not self.aktif_vid: return
        vaka = self.hastalar[self.aktif_hid]["vakalar"][self.aktif_vid]
        if tab == "plan":
            flag = vaka.get("dis_tasi_temizligi_plan", False)
            invalidate_cache()
            self.arch0.dis_tasi = flag
            self.arch0.ciz()
            btn = self._dis_tasi_btn_p0
        elif tab == "tedavi":
            flag = vaka.get("dis_tasi_temizligi_tedavi", False)
            invalidate_cache()
            self.arch1.dis_tasi = flag
            self.arch1.ciz()
            btn = self._dis_tasi_btn_p1
        else:
            flag = vaka.get("dis_tasi_temizligi_ilkin", False)
            invalidate_cache()
            self.arch2.dis_tasi = flag
            self.arch2.ciz()
            btn = self._dis_tasi_btn_pI
        if btn:
            btn.configure(fg_color="#a020f0" if flag else tint("#a020f0"),
                          text_color="white"   if flag else "#a020f0")

    def _step(self,i):
        adlar = {0: t("Plan"), 1: t("Müalicə"), 2: t("İlkin Vəziyyət")}
        self.tab.set(adlar[i])
        # CTkTabview.set diğer sekmeleri 100ms gecikmeli grid_forget eder;
        # 100ms içinde ikinci bir set çağrısı seçili sekmeyi de gizleyip
        # içeriği bomboş bırakıyor — geciken forget'tan sonra yeniden grid'le
        self.after(150, self._tab_grid_fix)
        for sid,b in self.step_btn:
            b.configure(fg_color=TH["accent"] if sid==i else TH["accent_soft"],
                        text_color="white" if sid==i else TH["accent_hover"])

    def _tab_grid_fix(self):
        try: self.tab._set_grid_current_tab()
        except Exception: pass

    def _tikla0(self,n,zone=None):
        if not self.aktif_hid: return
        key = (self.aktif_hid, self.aktif_vid)
        _, n1 = self._last_dis_per_vaka.get(key, (None, None))
        self._last_dis_per_vaka[key] = (n, n1)
        self._uygula_plan(n, zone)

    def _tikla1(self,n,zone=None):
        if not self.aktif_hid: return
        key = (self.aktif_hid, self.aktif_vid)
        n0, _ = self._last_dis_per_vaka.get(key, (None, None))
        self._last_dis_per_vaka[key] = (n0, n)
        self._uygula_tedavi(n, zone)

    def _tikla2(self,n,zone=None):
        if not self.aktif_hid: return
        self._last_dis_ilkin[(self.aktif_hid, self.aktif_vid)] = n
        self._uygula_ilkin(n, zone)

    def _tikla0_range(self, ns, zone=None):
        """Sürükleyerek seçilen bitişik diş aralığına aynı tedaviyi (aynı yüzeyi) uygula."""
        for n in ns:
            self._tikla0(n, zone)

    def _tikla1_range(self, ns, zone=None):
        for n in ns:
            self._tikla1(n, zone)

    def _tikla2_range(self, ns, zone=None):
        for n in ns:
            self._tikla2(n, zone)

    def _yuz_toggle(self, mevcut, alan, zone):
        """Dolgu/Çürük: tek bir yüzeyi (M/D/O/B/L) açıp/kapatır. Oklüzal ovalin
        belirli bir bölgesine değil de diş gövdesine/numaraya tıklandıysa
        (zone=None) varsayılan olarak Oklüzal(O) yüzeyi kullanılır."""
        z = zone or "O"
        yuz = list(mevcut.get(alan) or [])
        if z in yuz: yuz.remove(z)
        else: yuz.append(z)
        mevcut[alan] = yuz

    def _uygula_ilkin(self,n,zone=None):
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        ilkin = h["vakalar"][self.aktif_vid].setdefault("ilkin", {})
        mevcut = normalize_state(ilkin.get(str(n), {}))
        tid = self.secili_tedavi
        if tid == "saglikli":
            mevcut = {"kanal": False, "kron": False, "curuk": [], "dolgu": [], "tac": None}
        elif tid == "kanal":
            mevcut["kanal"] = True
        elif tid == "kron":
            mevcut["kron"] = not mevcut.get("kron", False)
        elif tid == "curuk":
            self._yuz_toggle(mevcut, "curuk", zone)
        elif tid == "dolgu":
            self._yuz_toggle(mevcut, "dolgu", zone)
        else:
            mevcut["tac"] = tid
        ilkin[str(n)] = mevcut
        veri_kaydet(self.hastalar)
        self.arch2.set_tedavi(n, mevcut)
        metin = dis_durum_str(mevcut)
        self.dis_info2.configure(text=f"{t('Diş')} {n}\n{dis_adi(n)}\n{metin}")
        self._plan_guncelle2()

    def _plan_guncelle2(self):
        try:
            for w in self.plan_liste2.winfo_children(): w.destroy()
        except: pass
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        vaka = h.get("vakalar",{}).get(self.aktif_vid,{})
        for n_str,durum in sorted(vaka.get("ilkin",{}).items(),key=lambda x:int(x[0])):
            if isinstance(durum,str): durum={"tac":durum,"kanal":False,"kron":False}
            metin = dis_durum_str(durum)
            if metin==t("Sağlam"): continue
            row=ctk.CTkFrame(self.plan_liste2,fg_color="transparent")
            row.pack(fill="x",pady=2)
            ctk.CTkLabel(row,text=f"{t('Diş')} {n_str}",width=52,
                         font=ctk.CTkFont(size=11,weight="bold"),
                         text_color=TH["txt"]).pack(side="left")
            ctk.CTkLabel(row,text=metin,font=ctk.CTkFont(size=11),
                         text_color=TH["accent"]).pack(side="left",padx=4)

    def _uygula_plan(self,n,zone=None):
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        plan = h["vakalar"][self.aktif_vid].setdefault("plan", {})
        mevcut = normalize_state(plan.get(str(n), {}))
        tid = self.secili_tedavi
        if tid == "saglikli":
            mevcut = {"kanal": False, "kron": False, "curuk": [], "dolgu": [], "tac": None}
        elif tid == "kanal":
            mevcut["kanal"] = True
        elif tid == "kron":
            mevcut["kron"] = not mevcut.get("kron", False)
        elif tid == "curuk":
            self._yuz_toggle(mevcut, "curuk", zone)
        elif tid == "dolgu":
            self._yuz_toggle(mevcut, "dolgu", zone)
        else:
            mevcut["tac"] = tid
        plan[str(n)] = mevcut
        veri_kaydet(self.hastalar)
        self.arch0.set_tedavi(n, mevcut)
        metin = dis_durum_str(mevcut)
        self.dis_info.configure(text=f"{t('Diş')} {n}\n{dis_adi(n)}\n{metin}")
        self._plan_guncelle()

    def _tedavi_aksiyon_metni(self, eski, mevcut):
        """Köhnə/yeni vəziyyəti müqayisə edib, bu klikdə HANSI yeni əməliyyatın
        aparıldığını (tarixçə sətri üçün) mətn olaraq qaytarır; heç nə
        dəyişməyibsə (və ya sağlama sıfırlanıbsa) None qaytarır."""
        if mevcut.get("tac") and mevcut.get("tac") != eski.get("tac"):
            lbl = next((lbl for tid,lbl,_ in TEDAVILER if tid==mevcut["tac"]), mevcut["tac"])
            return t(lbl)
        if mevcut.get("kanal") and not eski.get("kanal"):
            return t("Kanal")
        if mevcut.get("kron") and not eski.get("kron"):
            return t("Qapaq")
        if mevcut.get("dolgu") and mevcut.get("dolgu") != eski.get("dolgu"):
            return t("Kompozit Dolğu") + " (" + "".join(mevcut["dolgu"]) + ")"
        if mevcut.get("curuk") and mevcut.get("curuk") != eski.get("curuk"):
            return t("Çürük") + " (" + "".join(mevcut["curuk"]) + ")"
        return None

    def _tedavi_kateqoriya(self, tid):
        """tarixçədə eyni gün+diş+kateqoriya qeydlərini birləştirmək üçün qrup açarı."""
        if tid in ("curuk", "dolgu", "kron", "kanal"):
            return tid
        return "tac"

    def _tedavi_tarix_qruplari(self, n, durum):
        """Seçili dişin CARİ vəziyyətini tərkib hissələrinə ayırıb, hər hissəni
        tedavi_tarixce-dəki ən son uyğun qeydin tarixinə görə qruplaşdırır — eyni
        tarixdə edilənlər bir qrupda (sətirdə, aralarında '+'), fərqli tarixdə
        edilənlər ayrı qruplarda (sətirlərdə) göstərilsin deyə. Tarixi tapılmayan
        (köhnə/idxal edilmiş məlumat) hissələr tarixsiz son qrupda toplanır.
        Qaytarır: [(tarix_or_"", [mətn, ...]), ...] — ən yeni tarix əvvəldə."""
        parcalar = []   # [(kateqoriya, mətn), ...] — dis_durum_str ilə eyni məntiq
        if durum.get("kanal"): parcalar.append(("kanal", t("Kanal")))
        tac = durum.get("tac", "saglikli")
        if tac == "curuk":
            parcalar.append(("curuk", t("Çürük")))
        elif tac == "dolgu":
            parcalar.append(("dolgu", t("Kompozit Dolğu")))
        elif tac and tac != "saglikli":
            for tid,lbl,_ in TEDAVILER:
                if tid == tac:
                    parcalar.append(("tac", t(lbl))); break
        if durum.get("dolgu") and tac != "dolgu":
            parcalar.append(("dolgu", t("Kompozit Dolğu") + "(" + "".join(durum["dolgu"]) + ")"))
        if durum.get("curuk") and tac != "curuk":
            parcalar.append(("curuk", t("Çürük") + "(" + "".join(durum["curuk"]) + ")"))
        if durum.get("kron"): parcalar.append(("kron", t("Qapaq")))
        if not parcalar:
            return []

        vaka = self.hastalar.get(self.aktif_hid, {}).get("vakalar", {}).get(self.aktif_vid, {})
        tarixce = vaka.get("tedavi_tarixce", [])

        def _tarix_tap(kateqoriya):
            for kayit in reversed(tarixce):
                if kayit.get("dis") == n and kayit.get("kateqoriya") == kateqoriya:
                    return kayit.get("tarix", "")
            return ""

        qrup_dict, qrup_sira = {}, []
        for kateqoriya, metin in parcalar:
            tarix = _tarix_tap(kateqoriya)
            if tarix not in qrup_dict:
                qrup_dict[tarix] = []
                qrup_sira.append(tarix)
            qrup_dict[tarix].append(metin)

        def _tarix_key(t_str):
            try: return datetime.strptime(t_str, "%d.%m.%Y")
            except (ValueError, TypeError): return datetime.min
        qrup_sira.sort(key=_tarix_key, reverse=True)   # ən yeni tarix əvvəldə, tarixsiz sonda
        return [(tarix, qrup_dict[tarix]) for tarix in qrup_sira]

    def _dis_info1_render(self, n, durum):
        """Müalicə tabındakı 'Seçili Diş' panelini doldurur: eyni tarixdə edilən
        əməliyyatlar '+' ilə bir sətirdə (yer çatmasa üfüqi scroll ilə), fərqli
        tarixdə edilənlər ayrı sətirlərdə göstərilir (bax _tedavi_tarix_qruplari)."""
        for w in self.dis_info1_f.winfo_children(): w.destroy()
        if n is None:
            ctk.CTkLabel(self.dis_info1_f, text="—", font=ctk.CTkFont(size=14,weight="bold"),
                         text_color=TH["txt"]).pack(pady=4)
            return
        ctk.CTkLabel(self.dis_info1_f, text=f"{t('Diş')} {n}\n{dis_adi(n)}",
                     font=ctk.CTkFont(size=14,weight="bold"), text_color=TH["txt"],
                     justify="center").pack(pady=(4,6))
        qruplar = self._tedavi_tarix_qruplari(n, durum)
        if not qruplar:
            ctk.CTkLabel(self.dis_info1_f, text=t("Sağlam"), font=ctk.CTkFont(size=13,weight="bold"),
                         text_color=TH["txt"]).pack()
            return
        for tarix, metinler in qruplar:
            if tarix:
                ctk.CTkLabel(self.dis_info1_f, text=tarix, font=ctk.CTkFont(size=10),
                             text_color=TH["txt_faint"]).pack(anchor="w", padx=2, pady=(6,0))
            sira_f = ctk.CTkScrollableFrame(self.dis_info1_f, fg_color="transparent",
                                             orientation="horizontal", height=32)
            sira_f.pack(fill="x", padx=0, pady=(0,2))
            for i, metin in enumerate(metinler):
                if i > 0:
                    ctk.CTkLabel(sira_f, text="+", font=ctk.CTkFont(size=13,weight="bold"),
                                 text_color=TH["accent"]).pack(side="left", padx=3)
                ctk.CTkLabel(sira_f, text=metin, font=ctk.CTkFont(size=13,weight="bold"),
                             text_color=TH["txt"]).pack(side="left", padx=1)

    def _tedavi_geri_al_mi(self, eski, tid, zone):
        """Bu klik, artıq qeyd olunmuş bir müalicəni GERİ ALACAQ (söndürəcək) mi?"""
        if tid == "saglikli":
            return bool(eski.get("kanal") or eski.get("kron")
                        or eski.get("curuk") or eski.get("dolgu") or eski.get("tac"))
        if tid == "kron":
            return bool(eski.get("kron"))
        if tid == "kanal":
            return bool(eski.get("kanal"))
        if tid in ("curuk", "dolgu"):
            z = zone or "O"
            return z in (eski.get(tid) or [])
        # çəkim/qapaq növləri/implant/diş daşı və s. — hər hansı "tac" tipi
        # tədavi: eyni düyməyə yenidən basmaq onu geri alır (söndürür)
        return eski.get("tac") == tid

    def _uygula_tedavi(self,n,zone=None):
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        vaka = h["vakalar"][self.aktif_vid]
        tedavi = vaka.setdefault("tedavi", {})
        eski = normalize_state(tedavi.get(str(n), {}))
        tid = self.secili_tedavi
        if self._tedavi_geri_al_mi(eski, tid, zone):
            from tkinter import messagebox
            if not messagebox.askyesno(
                    t("Geri Al"),
                    t("Bu dişdəki qeyd olunmuş müalicəni geri almaq/dəyişmək "
                      "istədiyinizə əminsiniz?")):
                return
        mevcut = normalize_state(tedavi.get(str(n), {}))
        if tid == "saglikli":
            mevcut = {"kanal": False, "kron": False, "curuk": [], "dolgu": [], "tac": None}
        elif tid == "kanal":
            mevcut["kanal"] = not mevcut.get("kanal", False)
        elif tid == "kron":
            mevcut["kron"] = not mevcut.get("kron", False)
        elif tid == "curuk":
            self._yuz_toggle(mevcut, "curuk", zone)
        elif tid == "dolgu":
            self._yuz_toggle(mevcut, "dolgu", zone)
        else:
            # eyni düyməyə yenidən basmaq (çəkim/qapaq növü/implant/diş daşı
            # və s.) artıq qeyd olunmuş tədavini geri alır (None-a söndürür)
            mevcut["tac"] = None if mevcut.get("tac") == tid else tid
        tedavi[str(n)] = mevcut
        aksiyon = self._tedavi_aksiyon_metni(eski, mevcut)
        bugun = datetime.now().strftime("%d.%m.%Y")
        kateqoriya = self._tedavi_kateqoriya(tid)
        tarixce = vaka.setdefault("tedavi_tarixce", [])
        # eyni gündə, eyni dişdə, eyni kateqoriyada əvvəlki qeyd varsa onu
        # YENİLƏ (ayrı-ayrı qeydlər yaranmasın); əməliyyat nəticəsi "boş"dursa
        # (geri alınıbsa) qeydi tamamilə sil.
        for i in range(len(tarixce) - 1, -1, -1):
            kayit = tarixce[i]
            if kayit.get("dis") == n and kayit.get("tarix") == bugun \
                    and kayit.get("kateqoriya") == kateqoriya:
                if aksiyon:
                    tarixce[i] = {"dis": n, "tarix": bugun, "aksiyon": aksiyon,
                                   "kateqoriya": kateqoriya}
                else:
                    tarixce.pop(i)
                break
        else:
            if aksiyon:
                tarixce.append({"dis": n, "tarix": bugun, "aksiyon": aksiyon,
                                 "kateqoriya": kateqoriya})
        veri_kaydet(self.hastalar)
        self.arch1.set_tedavi(n, mevcut)
        self._dis_info1_render(n, mevcut)
        self._plan_guncelle1()

    def _plan_guncelle1(self):
        try:
            for w in self.plan_liste1.winfo_children(): w.destroy()
        except: pass
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        vaka = h.get("vakalar",{}).get(self.aktif_vid,{})
        for kayit in reversed(vaka.get("tedavi_tarixce", [])):
            row=ctk.CTkFrame(self.plan_liste1,fg_color="transparent")
            row.pack(fill="x",pady=(2,6))
            ctk.CTkLabel(row,text=f"{t('Diş')} {kayit['dis']}",anchor="w",
                         font=ctk.CTkFont(size=11,weight="bold"),
                         text_color=TH["txt"]).pack(fill="x")
            ctk.CTkLabel(row,text=f"{kayit['aksiyon']}-{kayit['tarix']}",anchor="w",
                         justify="left",wraplength=190,font=ctk.CTkFont(size=11),
                         text_color=TH["accent"]).pack(fill="x")
        self._timeline_guncelle()

    _TIMELINE_RENG = {
        "kanal": TH["accent"], "kron": TH["indigo"], "curuk": "#E0A020",
        "dolgu": TH["ok"], "tac": "#C04FD6",
    }

    def _timeline_guncelle(self):
        try:
            for w in self.muayine_xetti.winfo_children(): w.destroy()
        except: pass
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        vaka = h.get("vakalar",{}).get(self.aktif_vid,{})
        tarixce = vaka.get("tedavi_tarixce", [])
        if not tarixce:
            ctk.CTkLabel(self.muayine_xetti,text=t("Hələ müalicə qeydi yoxdur"),
                         font=ctk.CTkFont(size=11),text_color=TH["txt_faint"]).pack(
                         side="left",padx=12,pady=18)
            return
        sonuncu = len(tarixce) - 1
        for i,kayit in enumerate(tarixce):
            reng = self._TIMELINE_RENG.get(kayit.get("kateqoriya"), TH["accent"])
            cari = (i == sonuncu)
            if i > 0:
                ctk.CTkFrame(self.muayine_xetti,height=2,width=22,
                             fg_color=TH["accent_soft"]).pack(side="left",anchor="n",pady=(13,0))
            addim = ctk.CTkFrame(self.muayine_xetti,fg_color="transparent",width=92)
            addim.pack(side="left",anchor="n")
            dairə = ctk.CTkLabel(addim,text="●",font=ctk.CTkFont(size=16 if cari else 11),
                                  text_color=reng,height=16)
            dairə.pack(pady=(6,0))
            ctk.CTkLabel(addim,
                         text=f"{t('Diş')} {kayit.get('dis','')} · {kayit.get('aksiyon','')}",
                         font=ctk.CTkFont(size=9,weight="bold"),
                         text_color=TH["txt"],wraplength=90,justify="center").pack()
            ctk.CTkLabel(addim,text=kayit.get("tarix",""),
                         font=ctk.CTkFont(size=8),text_color=TH["txt_faint"]).pack()

    def _plan_guncelle(self):
        try:
            for w in self.plan_liste.winfo_children(): w.destroy()
        except: pass
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        vaka = h.get("vakalar",{}).get(self.aktif_vid,{})
        for n_str,durum in sorted(vaka.get("plan",{}).items(),key=lambda x:int(x[0])):
            if isinstance(durum,str): durum={"tac":durum,"kanal":False,"kron":False}
            metin = dis_durum_str(durum)
            if metin==t("Sağlam"): continue
            row=ctk.CTkFrame(self.plan_liste,fg_color="transparent")
            row.pack(fill="x",pady=2)
            ctk.CTkLabel(row,text=f"{t('Diş')} {n_str}",width=52,
                         font=ctk.CTkFont(size=11,weight="bold"),
                         text_color=TH["txt"]).pack(side="left")
            ctk.CTkLabel(row,text=metin,font=ctk.CTkFont(size=11),
                         text_color=TH["accent"]).pack(side="left",padx=4)

    def _kaydet(self):
        if not self.aktif_hid: return
        h=self.hastalar[self.aktif_hid]
        for attr,key in [("p0_ad","ad"),("p0_yas","yas"),
                          ("p0_tel","tel"),("p0_tc","tc"),("p0_not","notlar_genel")]:
            h[key]=getattr(self,attr).get()
        secili_cins = self.p0_cinsiyet_var.get()
        h["cinsiyet"] = next((k for k,l in CINSIYET_SECENEKLERI if t(l)==secili_cins), "")
        h["sheher"] = self.p0_sheher.get().strip()
        veri_kaydet(self.hastalar)
        self._lista_guncelle()
        self.kart_lbl.configure(text=h.get("ad",""))
        self._auto_bxd_backup(self.aktif_hid)
        self.p0_kaydet.configure(text="Saxlandı",fg_color="#1B5E20")
        self.after(2000,lambda:self.p0_kaydet.configure(text="Saxla",fg_color=["#3a7ebf","#1f538d"]))

    _BXD_BASE = os.path.join(os.path.dirname(DATA_FILE), "HastaYedekleri")

    def _auto_bxd_backup(self, hid):
        try:
            h = self.hastalar.get(hid)
            if not h: return
            os.makedirs(self._BXD_BASE, exist_ok=True)
            ad = h.get("ad", "hasta").replace(" ", "_").replace("/", "-")
            path = os.path.join(self._BXD_BASE, f"{ad}.bxd")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"format": "bxd", "version": 1, "hasta": h}, f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _bxd_backup_tick(self):
        """Son dəyişiklikdən 5 saniyə sonra aktiv hastanı .bxd olaraq yedəkləyir."""
        global _BACKUP_DIRTY
        if (_BACKUP_DIRTY and self.aktif_hid and
                (time.time() - _BACKUP_LAST_CHANGE) >= 5.0):
            _BACKUP_DIRTY = False
            self._auto_bxd_backup(self.aktif_hid)
        self.after(1000, self._bxd_backup_tick)

    def _hastalar_save_tick(self):
        """Son dəyişiklikdən ≈0.8 saniyə sonra, hələ arxa planda yazma getmirsə,
        hastalar.json-u ayrı thread-də diskə yazır. Bir neçə əməliyyat (məs. bir
        neçə diş kliki) bir-birinin ardınca tez gələndə onları TƏK yazıya birləşdirir
        və bahalı json.dump+disk yazısını UI thread-indən çıxarır — pasient/foto
        sayı artdıqca hər kiçik əməliyyatın proqramı get-gedə yavaşlatmasının qarşısı."""
        global _HASTALAR_DIRTY
        if (_HASTALAR_DIRTY and not self._hastalar_save_islemde
                and (time.time() - _HASTALAR_LAST_CHANGE) >= 0.8):
            _HASTALAR_DIRTY = False
            self._hastalar_save_islemde = True
            snap = dict(self.hastalar)   # ana thread'də sığ kopya (bulud-sinxron ilə eyni pattern)

            def _is():
                try:
                    _hastalar_disk_yaz(snap)
                except Exception as e:
                    print(f"[Save] hastalar.json yazıla bilmədi: {e}")
                finally:
                    self._hastalar_save_islemde = False
            threading.Thread(target=_is, daemon=True).start()
        self.after(300, self._hastalar_save_tick)

    def _hastalar_save_flush(self):
        """Gözləyən (dirty) hastalar.json yazısını DƏRHAL (sinxron) icra edir —
        proqram bağlanarkən (_on_close) məlumat itməsin deyə."""
        global _HASTALAR_DIRTY
        if _HASTALAR_DIRTY:
            _HASTALAR_DIRTY = False
            try:
                _hastalar_disk_yaz(dict(self.hastalar))
            except Exception as e:
                print(f"[Save] hastalar.json flush edilə bilmədi: {e}")

    def _yukle(self, hid=None, vid=None):
        if hid is None: return
        # Reset X-ray auto-confirm when patient or case changes
        if (hid, vid) != self._xray_confirmed_session:
            self._xray_confirmed_session = None
        # Arch'ları temizle — eski vaka verisi kalmasın
        _img_cache.clear()
        self.aktif_hid = hid
        h = self.hastalar[hid]
        vakalar = h.get("vakalar", {})
        if vid is None:
            vid = next(iter(vakalar), None)
        self.aktif_vid = vid
        self.expanded_hids.add(hid)
        self.kars.grid_remove()
        self.icerik.grid(row=0,column=0,sticky="nsew")
        self.sag.grid_rowconfigure(0,weight=1)
        for attr,key in [("p0_ad","ad"),("p0_yas","yas"),
                          ("p0_tel","tel"),("p0_tc","tc"),("p0_not","notlar_genel")]:
            e=getattr(self,attr); e.delete(0,"end"); e.insert(0,h.get(key,""))
        self.p0_cinsiyet_var.set(t(dict(CINSIYET_SECENEKLERI).get(h.get("cinsiyet",""), "—")))
        self.p0_sheher.delete(0,"end"); self.p0_sheher.insert(0,h.get("sheher",""))
        vaka = vakalar.get(vid, {})
        plan_data   = {str(k): v for k,v in vaka.get("plan",   {}).items()}
        tedavi_data = {str(k): v for k,v in vaka.get("tedavi", {}).items()}
        ilkin_data  = {str(k): v for k,v in vaka.get("ilkin",  {}).items()}
        dis_tasi_plan   = vaka.get("dis_tasi_temizligi_plan",   False)
        dis_tasi_tedavi = vaka.get("dis_tasi_temizligi_tedavi", False)
        dis_tasi_ilkin  = vaka.get("dis_tasi_temizligi_ilkin",  False)
        # Arch belleklerini tamamen sıfırla
        self.arch0.tedaviler = {}
        self.arch1.tedaviler = {}
        self.arch2.tedaviler = {}
        _img_cache.clear()
        # Qeyd dəftərində "#NN" ilə istinad olunan dişlər — Plan/Müalicə xəritələrində nida nişanı
        not_disler = _not_edilmis_disler(h)
        self.arch0.not_disler = not_disler
        self.arch1.not_disler = not_disler
        # Sonra yükle — her arch kendi bağımsız bayrağıyla
        self.arch0.yukle(plan_data,   dis_tasi=dis_tasi_plan)
        self.arch1.yukle(tedavi_data, dis_tasi=dis_tasi_tedavi)
        self.arch2.yukle(ilkin_data,  dis_tasi=dis_tasi_ilkin)
        # Butonları bağımsız güncelle
        def _sync_btn(btn, flag):
            if btn:
                btn.configure(fg_color="#a020f0" if flag else "white",
                              text_color="white"   if flag else "#a020f0")
        _sync_btn(self._dis_tasi_btn_p0, dis_tasi_plan)
        _sync_btn(self._dis_tasi_btn_p1, dis_tasi_tedavi)
        _sync_btn(self._dis_tasi_btn_pI, dis_tasi_ilkin)
        vaka_ad = vaka.get("ad","")
        self.kart_lbl.configure(text=f"{h.get('ad','')}  ·  {vaka_ad}")
        self._plan_guncelle()
        self._plan_guncelle1()
        self._plan_guncelle2()
        self._notlar_guncelle()
        self._rontgen_guncelle()
        self._foto_guncelle()
        # Show this patient's latest röntgen on the Quest VR app
        n_rontgen = len(h.get("rontgenler", []))
        self._vr_xray_idx[hid] = (n_rontgen - 1) if n_rontgen else None
        self._push_vr_xray_by_index(hid, self._vr_xray_idx[hid])
        self._xray_vr_nav_label_guncelle()
        # Restore last selected tooth; fall back to first treated tooth if none remembered
        n0, n1 = self._last_dis_per_vaka.get((hid, vid), (None, None))
        if n0 is None and plan_data:
            n0 = int(min(plan_data.keys(), key=int))
        if n1 is None and tedavi_data:
            n1 = int(min(tedavi_data.keys(), key=int))
        if n0 is not None:
            self.arch0.secili = n0
            self.arch0.ciz()
            durum = normalize_state(plan_data.get(str(n0), {}))
            self.dis_info.configure(text=f"{t('Diş')} {n0}\n{dis_adi(n0)}\n{dis_durum_str(durum)}")
        else:
            self.arch0.secili = None
            self.dis_info.configure(text="—")
        if n1 is not None:
            self.arch1.secili = n1
            self.arch1.ciz()
            durum = normalize_state(tedavi_data.get(str(n1), {}))
            self._dis_info1_render(n1, durum)
        else:
            self.arch1.secili = None
            self._dis_info1_render(None, None)
        nI = self._last_dis_ilkin.get((hid, vid))
        if nI is None and ilkin_data:
            nI = int(min(ilkin_data.keys(), key=int))
        if nI is not None:
            self.arch2.secili = nI
            self.arch2.ciz()
            durum = normalize_state(ilkin_data.get(str(nI), {}))
            self.dis_info2.configure(text=f"{t('Diş')} {nI}\n{dis_adi(nI)}\n{dis_durum_str(durum)}")
        else:
            self.arch2.secili = None
            self.dis_info2.configure(text="—")
        self._step(0)
        # Lista güncellemeyi geciktir — bekleyen tkinter eventleri drain olsun
        self.after(150, self._lista_guncelle)

    def _lista_guncelle(self):
        for w in self.scroll.winfo_children(): w.destroy()
        q = self.hasta_arama_var.get().strip().casefold() if hasattr(self,"hasta_arama_var") else ""
        for hid,h in self.hastalar.items():
            if q and q not in h.get("ad","").casefold():
                continue
            self._render_hasta(hid, h)

    def _render_hasta(self, hid, h):
        ad = h.get("ad","İsimsiz")
        expanded = hid in self.expanded_hids
        vakalar = h.get("vakalar", {})

        # ── Hasta satırı ──
        satir = ctk.CTkFrame(self.scroll, corner_radius=10,
                              fg_color=TH["accent"] if hid==self.aktif_hid else TH["sidebar_row"],
                              cursor="hand2")
        satir.pack(fill="x", padx=4, pady=(3,0))

        # Ok
        ok_lbl = ctk.CTkLabel(satir, text="▼" if expanded else "▶",
                               width=16, font=ctk.CTkFont(size=10),
                               text_color="white" if hid==self.aktif_hid else TH["txt_faint"])
        ok_lbl.pack(side="left", padx=(8,0), pady=6)

        # İnisyal
        inits = "".join([x[0] for x in ad.split()[:2]]).upper()
        l1 = ctk.CTkLabel(satir, text=inits, width=28, height=28,
                            corner_radius=14,
                            fg_color="white" if hid==self.aktif_hid else TH["accent"],
                            font=ctk.CTkFont(size=10,weight="bold"),
                            text_color=TH["accent"] if hid==self.aktif_hid else "white")
        l1.pack(side="left", padx=(4,4), pady=6)

        # Ad + vaka sayısı
        l2 = ctk.CTkLabel(satir, text=ad,
                            font=ctk.CTkFont(size=12,weight="bold"),
                            text_color="white")
        l2.pack(side="left")
        ctk.CTkLabel(satir, text=f"  {len(vakalar)}v",
                      font=ctk.CTkFont(size=10), text_color=TH["txt_faint"]).pack(side="left")

        # Sol tık → toggle accordion
        for w in (satir,ok_lbl,l1,l2):
            w.bind("<Button-1>", lambda e,hid=hid: self._toggle_expand(hid=hid))
        # Sağ tık → menü
        for w in (satir,ok_lbl,l1,l2):
            w.bind("<Button-3>", lambda e,hid=hid: self._hasta_menu(e,hid))

        # ── Vaka listesi (accordion) ──
        if expanded:
            vf = ctk.CTkFrame(self.scroll, fg_color=TH["sidebar_sub"], corner_radius=8)
            vf.pack(fill="x", padx=(20,4), pady=(0,3))
            for vid, v in sorted(vakalar.items(), key=lambda x: x[1].get("tarih","")):
                aktif = (hid==self.aktif_hid and vid==self.aktif_vid)
                self._render_vaka(vf, hid, vid, v, aktif)

    def _render_vaka(self, parent, hid, vid, v, aktif):
        vf = ctk.CTkFrame(parent, corner_radius=8,
                           fg_color=TH["ok"] if aktif else TH["sidebar_row2"],
                           cursor="hand2")
        vf.pack(fill="x", padx=4, pady=2)
        ad = v.get("ad", "Vaka")
        tarih = v.get("tarih","")
        lbl = ctk.CTkLabel(vf, text=f"📋 {ad}",
                            font=ctk.CTkFont(size=11,weight="bold" if aktif else "normal"),
                            text_color="white" if aktif else "#D7E5F7")
        lbl.pack(side="left", padx=8, pady=4)
        ctk.CTkLabel(vf, text=tarih,
                      font=ctk.CTkFont(size=9), text_color=TH["txt_faint"]).pack(side="left")
        # Sol tık → vakayı yükle
        for w in (vf, lbl):
            w.bind("<Button-1>", lambda e,hid=hid,vid=vid: self._yukle(hid=hid,vid=vid))
        # Sağ tık → vaka menüsü
        for w in (vf, lbl):
            w.bind("<Button-3>", lambda e,hid=hid,vid=vid: self._vaka_menu(e,hid,vid))



    # ── shared EXE path ───────────────────────────────────────────────────────
    _UE5_EXE = r"E:\Unreal Projects\MyProject\Saved\StagedBuilds\Windows_Clean\MyProject.exe"

    @staticmethod
    def _find_ue5_window():
        """Return first visible HWND whose title contains a UE5/MyProject keyword."""
        import ctypes, ctypes.wintypes
        results = []
        user32 = ctypes.windll.user32
        def _cb(hwnd, _):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if user32.IsWindowVisible(hwnd) and any(
                k in buf.value.lower() for k in
                    ["myproject", "unreal", "vrtemplat", "mikroskop"]):
                results.append(hwnd)
            return True
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                          ctypes.wintypes.HWND,
                                          ctypes.wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return results[0] if results else None

    def _launch_or_focus_ue5(self):
        """Launch packaged EXE if not running; bring to foreground if it is."""
        import subprocess
        hwnd = self._find_ue5_window()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        elif os.path.exists(self._UE5_EXE):
            subprocess.Popen([self._UE5_EXE])

    def _vr_ui_guncelle(self):
        """Ayarlar'dakı 'VR Rejimi' açarına görə header'dəki 3 VR düyməsini göstərir/gizlədir."""
        acit = bool(AYARLAR.get("vr_rejimi", False))
        dugmeler = [(self._btn_tedaviye_kec, (14,4)),
                    (self._btn_vr_duzenle, (12,2)),
                    (self._btn_vr_layout, (2,4))]
        for btn, padx in dugmeler:
            if acit:
                btn.pack(side="left", padx=padx)
            else:
                btn.pack_forget()

    def _tedaviye_kec(self):
        """Hasta verisini JSON'a yaz, packaged EXE'yi başlat/ön plana getir."""
        import ctypes

        # Write current_patient.json from active patient
        if self.aktif_hid and self.aktif_vid:
            h    = self.hastalar[self.aktif_hid]
            vaka = h.get("vakalar", {}).get(self.aktif_vid, {})
            # Build human-readable treatment plan string
            tedavi_lines = []
            for n_str, durum in sorted(vaka.get("tedavi", {}).items(),
                                       key=lambda x: int(x[0])):
                if isinstance(durum, str):
                    durum = {"tac": durum, "kanal": False, "kron": False}
                txt = dis_durum_str(durum)
                if txt:
                    tedavi_lines.append(f"{t('Diş')} {n_str}: {txt}")
            plan_str = "\n".join(tedavi_lines) if tedavi_lines else ""

            # Push the currently-selected röntgen (decoded from embedded base64 —
            # robust even for manually-added röntgenler with no live file path)
            idx = self._vr_xray_idx.get(self.aktif_hid)
            self._push_vr_xray_by_index(self.aktif_hid, idx)
            xray_path = XRAY_VR_CURRENT_JPG if idx is not None else ""

            os.makedirs(r"C:\BlueX", exist_ok=True)
            patient_data = {
                "patient_name":    h.get("ad", ""),
                "age":             int(h.get("yas", 0) or 0),
                "treatment_plan":  plan_str,
                "xray_image_path": xray_path,
                "patient_id":      h.get("tc", ""),
                "timestamp":       datetime.now().isoformat(),
            }
            with open(r"C:\BlueX\current_patient.json", "w", encoding="utf-8") as f:
                json.dump(patient_data, f, ensure_ascii=False, indent=2)

        # Switch tab and minimize after short delay
        self._step(1)
        self.after(300, self.iconify)

        # Launch or focus packaged EXE
        self._launch_or_focus_ue5()


    def _toggle_expand(self, hid):
        if hid in self.expanded_hids:
            self.expanded_hids.discard(hid)
        else:
            self.expanded_hids.add(hid)
            # Hasta seçili değilse ilk vakayı yükle
            if self.aktif_hid != hid:
                self._yukle(hid=hid)
                return
        self._lista_guncelle()

    def _hasta_menu(self, event, hid):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"➕  {t('Yeni Vaka')}",
                          command=lambda: self._yeni_vaka(hid))
        menu.add_separator()
        menu.add_command(label=f"💾  {t('Hastayı Kaydet')}",
                          command=lambda: self._hasta_kaydet_hizli(hid))
        menu.add_separator()
        menu.add_command(label=f"🗑  {t('Hastayı Sil')}",
                          foreground="red",
                          command=lambda: self._hasta_sil(hid))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _vaka_menu(self, event, hid, vid):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"✏  {t('Vaka Adını Değiştir')}",
                          command=lambda: self._vaka_yeniden_adlandir(hid,vid))
        menu.add_separator()
        menu.add_command(label=f"🗑  {t('Vakayı Sil')}",
                          foreground="red",
                          command=lambda: self._vaka_sil(hid,vid))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _yeni_vaka(self, hid):
        import tkinter.messagebox as mb
        h = self.hastalar[hid]
        vakalar = h.setdefault("vakalar", {})

        # Köçürüləcək mənbə vaka: aktiv açıq vaka bu hastaya aiddirsə o,
        # əks halda siyahıdakı son (ən son əlavə edilmiş) vaka.
        kaynak_vid = None
        if vakalar:
            if hid == self.aktif_hid and self.aktif_vid in vakalar:
                kaynak_vid = self.aktif_vid
            else:
                kaynak_vid = list(vakalar.keys())[-1]

        kopyala = False
        if kaynak_vid:
            kaynak_ad = vakalar[kaynak_vid].get("ad", "əvvəlki vaka")
            kopyala = mb.askyesno(
                t("Yeni Vaka"),
                f"\"{kaynak_ad}\" içindəki İlkin Vəziyyət və Planlama\n"
                f"yeni vakaya köçürülsün?",
                parent=self)

        vid = yeni_id("vaka")
        n = len(vakalar) + 1
        yeni = {
            "ad": f"{n}. {t('Vaka')}",
            "tarih": datetime.now().strftime("%d.%m.%Y"),
            "plan": {}, "tedavi": {}
        }
        if kopyala and kaynak_vid:
            kaynak = vakalar[kaynak_vid]
            yeni["plan"]  = copy.deepcopy(kaynak.get("plan", {}))
            yeni["ilkin"] = copy.deepcopy(kaynak.get("ilkin", {}))
            yeni["dis_tasi_temizligi_plan"]  = kaynak.get("dis_tasi_temizligi_plan", False)
            yeni["dis_tasi_temizligi_ilkin"] = kaynak.get("dis_tasi_temizligi_ilkin", False)
            yeni["fotolar_ilkin"] = copy.deepcopy(kaynak.get("fotolar_ilkin", []))
        vakalar[vid] = yeni
        veri_kaydet(self.hastalar)
        self.expanded_hids.add(hid)
        self._lista_guncelle()
        self._yukle(hid=hid,vid=vid)

    # ── Bulud senkronizasyonu ────────────────────────────────────────────
    def _cloud_status_guncelle(self, metin=None):
        if not self.cloud_status_lbl:
            return
        if metin is not None:
            self.cloud_status_lbl.configure(text=metin)
            return
        if not cloud_configured():
            self.cloud_status_lbl.configure(text="☁ Bulud quraşdırılmayıb")
        elif AYARLAR.get("sb_access_token"):
            son = AYARLAR.get("sb_son_senkron")
            self.cloud_status_lbl.configure(
                text=f"☁ Son senkron: {son}" if son else "☁ Giriş edilib — senkron gözlənilir")
        else:
            self.cloud_status_lbl.configure(text="☁ Giriş edilməyib")

    def _cloud_authed_request(self, method, path, body=None, extra_headers=None, timeout=None):
        tok = AYARLAR.get("sb_access_token")
        if not tok:
            raise RuntimeError("Bulud girişi edilməyib")
        try:
            return _sb_http(method, path, body, token=tok, extra_headers=extra_headers, timeout=timeout)
        except RuntimeError as e:
            if "HTTP 401" in str(e) and AYARLAR.get("sb_refresh_token"):
                ref = sb_refresh(AYARLAR["sb_refresh_token"])
                if ref.get("access_token"):
                    AYARLAR["sb_access_token"] = ref["access_token"]
                    AYARLAR["sb_refresh_token"] = ref.get("refresh_token", AYARLAR["sb_refresh_token"])
                    ayar_kaydet(AYARLAR)
                    return _sb_http(method, path, body, token=AYARLAR["sb_access_token"], extra_headers=extra_headers, timeout=timeout)
            raise

    def _cloud_authed_request_raw(self, method, path, data=None, content_type=None, timeout=None):
        """`_cloud_authed_request` ilə eyni token/401-refresh məntiqi, Storage
        bayt oxuma/yazma üçün (`_sb_http_raw`)."""
        tok = AYARLAR.get("sb_access_token")
        if not tok:
            raise RuntimeError("Bulud girişi edilməyib")
        try:
            return _sb_http_raw(method, path, data=data, token=tok, content_type=content_type, timeout=timeout or 60)
        except RuntimeError as e:
            if "HTTP 401" in str(e) and AYARLAR.get("sb_refresh_token"):
                ref = sb_refresh(AYARLAR["sb_refresh_token"])
                if ref.get("access_token"):
                    AYARLAR["sb_access_token"] = ref["access_token"]
                    AYARLAR["sb_refresh_token"] = ref.get("refresh_token", AYARLAR["sb_refresh_token"])
                    ayar_kaydet(AYARLAR)
                    return _sb_http_raw(method, path, data=data, token=AYARLAR["sb_access_token"],
                                         content_type=content_type, timeout=timeout or 60)
            raise

    def _img_bytes(self, entry):
        """Röntgen/foto girişindən xam şəkil baytını qaytarır — köhnə format
        (`"data"`: b64) birbaşa decode olunur; yeni format (`"storage_path"`)
        əvvəlcə yerli keşdən (`%APPDATA%\\BlueX\\cache_img`), tapılmazsa
        Supabase Storage-dən endirilib keşlənir."""
        if entry.get("data"):
            return base64.b64decode(entry["data"])
        sp = entry.get("storage_path")
        if not sp:
            raise RuntimeError("Şəkil məlumatı yoxdur")
        cache_path = _img_cache_path(sp)
        if os.path.exists(cache_path):
            with open(cache_path, "rb") as f:
                return f.read()
        raw = self._cloud_authed_request_raw("GET", f"/storage/v1/object/patient-images/{sp}")
        try:
            with open(cache_path, "wb") as f:
                f.write(raw)
        except Exception:
            pass
        return raw

    def _auto_migrate_images(self, h, hid):
        """Hələ köhnə formatda (`"data"`: xam b64) olan röntgen/foto girişlərini
        buluda göndərməzdən ƏVVƏL sıxıb Supabase Storage-ə yükləyir və girişi
        yerində (`"storage_path"`+`"thumb_b64"`) formatına çevirir — bulud 500MB
        kvotasının yenidən dolmasının qarşısını alır (bax [[dental-supabase-quota-full]]).
        Bulud girişi yoxdursa (uid yoxdur) heç nə etmir."""
        uid = AYARLAR.get("sb_user_id")
        if not uid:
            return
        changed = False

        def _mig_one(entry, kind):
            nonlocal changed
            if not entry.get("data"):
                return
            try:
                raw = base64.b64decode(entry["data"])
                img = Image.open(BytesIO(raw))
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                full_img = img
                if kind == "foto" and full_img.width > 2000:
                    ratio = 2000 / full_img.width
                    full_img = full_img.resize((2000, int(full_img.height * ratio)), Image.LANCZOS)
                buf = BytesIO()
                full_img.save(buf, format="JPEG", quality=95 if kind == "rontgen" else 85)
                full_bytes = buf.getvalue()

                thumb_img = img
                if thumb_img.width > 300:
                    ratio = 300 / thumb_img.width
                    thumb_img = thumb_img.resize((300, int(thumb_img.height * ratio)), Image.LANCZOS)
                tbuf = BytesIO()
                thumb_img.save(tbuf, format="JPEG", quality=70)
                thumb_bytes = tbuf.getvalue()

                fname = f"{kind}_{uuid.uuid4().hex[:12]}.jpg"
                path = f"{uid}/{hid}/{fname}"
                self._cloud_authed_request_raw("POST", f"/storage/v1/object/patient-images/{path}",
                                                 data=full_bytes, content_type="image/jpeg")
                entry.pop("data", None)
                entry["storage_path"] = path
                entry["thumb_b64"] = base64.b64encode(thumb_bytes).decode()
                changed = True
            except Exception as e:
                print(f"[Miqrasiya] şəkil köçürülə bilmədi ({hid}): {e}")

        for r in h.get("rontgenler", []) or []:
            _mig_one(r, "rontgen")
        for vaka in (h.get("vakalar") or {}).values():
            for key in ("fotolar_plan", "fotolar_tedavi", "fotolar_ilkin"):
                for ph in vaka.get(key, []) or []:
                    _mig_one(ph, "foto")
        if changed:
            veri_kaydet(self.hastalar)

    def _cloud_authed_request_deneli(self, method, path, body=None, extra_headers=None, timeout=None, max_deneme=3):
        """`_cloud_authed_request`-in üzərinə keçici (transient) server-tərəfli
        xətalar üçün avtomatik təkrar cəhd qatı — canlı sınaqla təsdiqləndi:
        çox böyük (30-45+ MB) tək hasta payload-larında Cloudflare/Supabase
        ARA-SIRA "HTTP 520" (naməlum server xətası) qaytarır, AMMA EYNİ payload
        DƏRHAL təkrar sınandıqda adətən uğurla keçir (server-tərəfli müvəqqəti
        yükdür, sabit ölçü limiti DEYİL — 44MB uğurlu, 45MB bir sınaqda
        uğursuz/başqa sınaqda uğurlu, 46MB uğurlu ölçülüb)."""
        son_xeta = None
        for deneme in range(1, max_deneme + 1):
            try:
                return self._cloud_authed_request(method, path, body=body, extra_headers=extra_headers, timeout=timeout)
            except RuntimeError as e:
                s = str(e)
                keciciler = ("vaxt bitdi", "timeout", "HTTP 520", "HTTP 502", "HTTP 503", "HTTP 522", "HTTP 524")
                if deneme < max_deneme and any(k.lower() in s.lower() for k in keciciler):
                    son_xeta = e
                    time.sleep(3 * deneme)
                    continue
                raise
        raise son_xeta

    def cloud_push_all(self, snapshot=None, ilerleme=None, yalniz_deyisenler=False):
        """Yerel bütün hastaları buluda upsert edir (sətir-sətir insert/əks halda
        update) + bekleyen silmeleri işler.
        NOT: bulk 'Prefer: resolution=merge-duplicates' (ON CONFLICT DO UPDATE)
        Postgres RLS-də TƏZƏ sətirlər üçün belə UPDATE policy-nin USING şərtini
        yoxlayır və 403 verir (canlı sınaqla təsdiqləndi) — ona görə hər hasta
        üçün əvvəl sadə INSERT, 409 (unique_violation) gələrsə PATCH edilir.
        `ilerleme(i, toplam, hasta_adı)` hər hasta göndərilməzdən əvvəl çağırılır
        (UI-də "Göndərilir… 4/12 (Əli)" kimi göstərmək üçün).
        Bir hastanın öz vaxt-bitmə xətası (çox böyük rentgen/foto + yavaş bağlantı)
        ARTIQ bütün sinxronu DAYANDIRMIR — həmin hasta ATLANIR, digərləri davam edir
        (əvvəllər tək bir böyük/yavaş hasta bütün push-u bloklayırdı, xəstə sayı
        artdıqca bu ehtimal da artırdı — istifadəçi bildirdi: "az xəstədə problem
        olmurdu, xəstə sayı çoxaldıqda oldu")."""
        uid = AYARLAR.get("sb_user_id")
        if not uid:
            raise RuntimeError("Bulud girişi edilməyib")
        data = snapshot if snapshot is not None else self.hastalar
        if yalniz_deyisenler:
            # yalnız son uğurlu sinxrondan bəri HƏQİQƏTƏN dəyişmiş hastaları göndər —
            # avtomatik arxa-plan tick-i hər dəfə BÜTÜN hastaları YENİDƏN göndərmək
            # əvəzinə tez-tez amma kiçik-kiçik sinxronlaşsın deyə
            data = {hid: h for hid, h in data.items() if _hasta_hash(h) != _CLOUD_SENT_HASH.get(hid)}
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        toplam = len(data)
        basarili = 0
        basarisiz = []   # [(hasta_adı, səbəb)]
        for i, (hid, h) in enumerate(data.items(), start=1):
            if ilerleme:
                try: ilerleme(i, toplam, h.get("ad", hid))
                except Exception: pass
            try:
                self._auto_migrate_images(h, hid)
            except Exception as e:
                print(f"[Miqrasiya] {hid} üçün avtomatik şəkil köçürməsi uğursuz: {e}")
            row = {"hasta_id": hid, "owner": uid, "data": h, "updated_at": now}
            # keçici server xətaları (timeout, HTTP 520/502/503) üçün avtomatik təkrar
            # cəhd (bax _cloud_authed_request_deneli şərhi — böyük payload-larda bu
            # xətalar tez-tez MÜVƏQQƏTİDİR, dərhal təkrar sınandıqda keçir)
            keci_xetalari = ("vaxt bitdi", "timeout", "http 520", "http 502", "http 503", "http 522", "http 524")
            try:
                self._cloud_authed_request_deneli(
                    "POST", "/rest/v1/hastalar", body=[row],
                    extra_headers={"Prefer": "return=minimal"})
                basarili += 1
                _CLOUD_SENT_HASH[hid] = _hasta_hash(h)
            except RuntimeError as e:
                s = str(e)
                if "HTTP 409" in s or "23505" in s:
                    try:
                        self._cloud_authed_request_deneli(
                            "PATCH", f"/rest/v1/hastalar?hasta_id=eq.{hid}",
                            body={"data": h, "updated_at": now},
                            extra_headers={"Prefer": "return=minimal"})
                        basarili += 1
                        _CLOUD_SENT_HASH[hid] = _hasta_hash(h)
                    except RuntimeError as e2:
                        if any(k in str(e2).lower() for k in keci_xetalari):
                            # əsl xəta mətnini SAXLAYIRIQ (təxmini "çox böyük/yavaş" izahı
                            # yerinə) — server-tərəfli statement timeout (Postgres 57014) ilə
                            # həqiqi client-tərəfli socket timeout eyni "timeout" sözünü
                            # daşıyır, amma səbəbləri VƏ həlləri tамamilə fərqlidir
                            basarisiz.append((h.get("ad", hid), str(e2)[:200]))
                        else:
                            raise
                elif any(k in s.lower() for k in keci_xetalari):
                    basarisiz.append((h.get("ad", hid), s[:200]))
                else:
                    raise
        for hid in list(AYARLAR.get("silinen_hastalar", [])):
            try:
                self._cloud_authed_request("DELETE", f"/rest/v1/hastalar?hasta_id=eq.{hid}")
            except Exception as e:
                print(f"[Bulud] silme göndərilmədi ({hid}): {e}")
        AYARLAR["silinen_hastalar"] = []
        ayar_kaydet(AYARLAR)
        return basarili, basarisiz

    def _cloud_fetch_batch(self, hids, timeout=180):
        """Verilmiş hasta_id'ler üçün data'nı çəkir. Supabase-in pulsuz planında
        'authenticated' rolu üçün DB-sorğu vaxtı limiti var (canlı sınaqla:
        14 hastanı/83MB-ı TƏK sorğuda çəkmək ~9.6s-də sunucu-tərəfli
        'statement timeout' (57014) ilə uğursuz olur, amma kiçik qruplar
        (2-3 hasta) sorunsuz keçir) — bu yüzdən 57014 gələrsə qrup ikiyə
        bölünüb yenidən sınanır (tək hasta qalana qədər)."""
        ids_csv = ",".join(hids)
        path = f"/rest/v1/hastalar?select=hasta_id,data,updated_at&hasta_id=in.({ids_csv})"
        try:
            return self._cloud_authed_request("GET", path, timeout=timeout)
        except RuntimeError as e:
            if "57014" in str(e) and len(hids) > 1:
                mid = len(hids) // 2
                return (self._cloud_fetch_batch(hids[:mid], timeout)
                        + self._cloud_fetch_batch(hids[mid:], timeout))
            raise

    def cloud_pull_all(self):
        """Buluddakı bütün hastaları yerelə endirir (üzərinə yazar). Böyük
        cavab payload'ının (rentgen/foto daxil onlarla MB) tək sorğuda
        server-side statement timeout'a düşməməsi üçün hasta_id'ler kiçik
        qruplar halında çəkilir."""
        id_rows = self._cloud_authed_request("GET", "/rest/v1/hastalar?select=hasta_id", timeout=30)
        ids = [r["hasta_id"] for r in id_rows]
        toplam = 0
        BATCH = 3
        for i in range(0, len(ids), BATCH):
            for row in self._cloud_fetch_batch(ids[i:i + BATCH]):
                self.hastalar[row["hasta_id"]] = row["data"]
                # buludan indi endirilən məzmun artıq "sinxron" sayılır — avtomatik
                # tick bunu dərhal geri buluda göndərməyə çalışmasın
                _CLOUD_SENT_HASH[row["hasta_id"]] = _hasta_hash(row["data"])
                toplam += 1
        veri_kaydet(self.hastalar)
        return toplam

    def cloud_list_names(self):
        """Buluddakı hastaların yalnız id+ad siyahısını çəkir (foto/röntgen
        DAXİL OLMADAN) — tək-hasta seçim dialoqu üçün, tam datanı endirmədən."""
        rows = self._cloud_authed_request(
            "GET", "/rest/v1/hastalar?select=hasta_id,ad:data->>ad", timeout=30)
        return sorted(rows, key=lambda r: (r.get("ad") or "").lower())

    def cloud_pull_one(self, hid):
        """Yalnız TƏK bir hastanı (seçilən) buluddan çəkib yerelə yazır (üzərinə yazar)."""
        rows = self._cloud_authed_request(
            "GET", f"/rest/v1/hastalar?select=hasta_id,data&hasta_id=eq.{hid}", timeout=180)
        if not rows:
            raise RuntimeError("Hasta buludda tapılmadı")
        row = rows[0]
        self.hastalar[row["hasta_id"]] = row["data"]
        _CLOUD_SENT_HASH[row["hasta_id"]] = _hasta_hash(row["data"])
        veri_kaydet(self.hastalar)
        return row["data"].get("ad", "")

    def _cloud_start_after_auth(self):
        """Giriş/qeydiyyatdan dərhal sonra çağırılır: mövcud yerel verini dərhal
        buluda göndərmək üçün 'dirty' işarələyir və (bir dəfəlik) davamlı
        avtomatik-sinxron dövrəsini işə salır."""
        global _CLOUD_DIRTY
        _CLOUD_DIRTY = True
        if not getattr(self, "_cloud_tick_started", False):
            self._cloud_tick_started = True
            self.after(500, self._cloud_auto_sync_tick)

    def _cloud_auto_sync_tick(self):
        global _CLOUD_DIRTY
        if cloud_configured() and AYARLAR.get("sb_access_token") and _CLOUD_DIRTY:
            # Böyük hastalar (rentgen/foto) tək bir push'un uzun sürməsinə səbəb
            # ola bilir — əl ilə push davam edərkən bura girərsə qıfıl tutula
            # bilmir, o zaman _CLOUD_DIRTY-ni False etmirik ki, növbəti tick yenidən
            # sınasın (əks halda bu dəyişiklik heç vaxt buluda getməzdi).
            if _CLOUD_SYNC_LOCK.acquire(blocking=False):
                _CLOUD_DIRTY = False
                snap = dict(self.hastalar)   # ana thread'də sığ kopya — arxa plan thread'i üçün
                threading.Thread(target=self._cloud_auto_sync_worker,
                                  args=(snap,), kwargs={"yalniz_deyisenler": True}, daemon=True).start()
            else:
                print("[Bulud] avtomatik sinxron keçildi — başqa sinxron davam edir")
        # tez-tez yoxla (8s) — YALNIZ dəyişən hastalar göndərilir (yalniz_deyisenler),
        # ona görə tez-tez olması şəbəkəyə əlavə yük gətirmir (dəyişiklik yoxdursa
        # heç bir sorğu getmir, hash müqayisəsi tamamilə yerel/pulsuzdur)
        self.after(8000, self._cloud_auto_sync_tick)

    def _cloud_auto_sync_worker(self, snapshot, yalniz_deyisenler=False):
        try:
            self.cloud_push_all(snapshot, yalniz_deyisenler=yalniz_deyisenler)
            AYARLAR["sb_son_senkron"] = datetime.now().strftime("%H:%M")
            ayar_kaydet(AYARLAR)
            self.after(0, self._cloud_status_guncelle, None)
        except Exception as e:
            print(f"[Bulud] avtomatik sinxronizasiya xətası: {e}")
            self.after(0, self._cloud_status_guncelle, "☁ Bağlantı yoxdur")
        finally:
            _CLOUD_SYNC_LOCK.release()

    def _hasta_sec_dialog(self, parent, names, parent_set_status=None):
        """Buluddaki hasta id+ad siyahısını göstərib seçilən TƏK hastanı
        (`cloud_pull_one` ilə) yerelə endirir. `names`: [{"hasta_id","ad"}, ...]."""
        dlg = ctk.CTkToplevel(parent, fg_color=TH["bg"])
        dlg.title(t("Buluddan Hasta Seç"))
        dlg.geometry("380x460")
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        ctk.CTkLabel(dlg, text=t("BULUDDAKI HASTALAR"),
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", padx=16, pady=(16, 2))
        if not names:
            ctk.CTkLabel(dlg, text=t("Buludda hasta tapılmadı."),
                         font=ctk.CTkFont(size=12), text_color=TH["txt_sub"]
                         ).pack(anchor="w", padx=16, pady=(4, 12))
            return
        arama_var = tk.StringVar()
        ctk.CTkEntry(dlg, textvariable=arama_var, placeholder_text=t("Ada görə axtar…"),
                     height=32, border_color=TH["accent_soft"], fg_color=TH["panel_soft"]
                     ).pack(fill="x", padx=16, pady=(0, 8))
        liste = ctk.CTkScrollableFrame(dlg, fg_color=TH["panel"], corner_radius=10)
        liste.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        durum = ctk.CTkLabel(dlg, text="", font=ctk.CTkFont(size=11), text_color=TH["txt_sub"])
        durum.pack(anchor="w", padx=16, pady=(0, 12))

        def _yukle_sec(hid, ad):
            for w in liste.winfo_children():
                w.configure(state="disabled")
            durum.configure(text=f"'{ad}' endirilir…", text_color=TH["txt_sub"])
            def work():
                try:
                    endirilen_ad = self.cloud_pull_one(hid)
                    def ok():
                        self._lista_guncelle()
                        durum.configure(text=f"✓ '{endirilen_ad}' endirildi.", text_color=TH["ok"])
                        dlg.after(800, dlg.destroy)
                    dlg.after(0, ok)
                except Exception as e:
                    err = e
                    def fail():
                        for w in liste.winfo_children():
                            w.configure(state="normal")
                        durum.configure(text=f"✗ Xəta: {err}", text_color="#C0392B")
                    dlg.after(0, fail)
            threading.Thread(target=work, daemon=True).start()

        def _render(filtre=""):
            for w in liste.winfo_children(): w.destroy()
            f = filtre.strip().lower()
            for row in names:
                ad = row.get("ad") or "(isimsiz)"
                if f and f not in ad.lower():
                    continue
                ctk.CTkButton(liste, text=ad, anchor="w", height=34,
                              fg_color="transparent", hover_color=TH["accent_soft"],
                              text_color=TH["txt"],
                              command=lambda hid=row["hasta_id"], ad=ad: _yukle_sec(hid, ad)
                              ).pack(fill="x", pady=2)

        arama_var.trace_add("write", lambda *_: _render(arama_var.get()))
        _render()

    def _hasta_sil(self, hid):
        import tkinter.messagebox
        h = self.hastalar.get(hid,{})
        if tkinter.messagebox.askyesno(
                t("Hastayı Sil"),
                f"'{h.get('ad','')}' hastasını silmek istediğinizden emin misiniz?"):
            del self.hastalar[hid]
            veri_kaydet(self.hastalar)
            silinen = AYARLAR.setdefault("silinen_hastalar", [])
            if hid not in silinen: silinen.append(hid)
            ayar_kaydet(AYARLAR)
            if self.aktif_hid == hid:
                self.aktif_hid = None
                self.aktif_vid = None
                self.icerik.grid_remove()
                self.kars.grid(row=0,column=0)
            self.expanded_hids.discard(hid)
            self._lista_guncelle()

    def _vaka_sil(self, hid, vid):
        import tkinter.messagebox
        v = self.hastalar[hid]["vakalar"].get(vid,{})
        if tkinter.messagebox.askyesno(
                t("Vakayı Sil"),
                f"'{v.get('ad','')}' vakasını silmek istediğinizden emin misiniz?"):
            del self.hastalar[hid]["vakalar"][vid]
            veri_kaydet(self.hastalar)
            # Başka vaka varsa onu yükle
            vakalar = self.hastalar[hid].get("vakalar",{})
            if vakalar:
                nxt=next(iter(vakalar)); self._yukle(hid=hid,vid=nxt)
            else:
                self.aktif_vid = None
            self._lista_guncelle()

    def _vaka_yeniden_adlandir(self, hid, vid):
        d = ctk.CTkInputDialog(
            text=t("Yeni vaka adı:"),
            title=t("Vaka Adını Değiştir"))
        yeni = d.get_input()
        if yeni and yeni.strip():
            self.hastalar[hid]["vakalar"][vid]["ad"] = yeni.strip()
            veri_kaydet(self.hastalar)
            self._lista_guncelle()
            if self.aktif_hid==hid and self.aktif_vid==vid:
                h = self.hastalar[hid]
                v = h["vakalar"][vid]
                self.kart_lbl.configure(
                    text=f"{h.get('ad','')}  ·  {v.get('ad','')}")

    def _dil_uygula(self, yeni_lang):
        """Dil dəyişəndə bütün açıq pəncərələri (əsas + Ayarlar) təzə dildə
        yenidən qurur. Cari callback (Ayarlar'dakı düymənin özü) bitmədən
        onu daşıyan pəncərəni məhv etmək Tcl xətası verə bilər, ona görə
        yenidənqurma bir toxdan sonra (`after`) icra olunur."""
        global CURRENT_LANG
        if yeni_lang == CURRENT_LANG:
            return
        CURRENT_LANG = yeni_lang
        AYARLAR["dil"] = yeni_lang
        ayar_kaydet(AYARLAR)

        def _yenidenqur():
            aktif_hid, aktif_vid = self.aktif_hid, self.aktif_vid
            for w in list(self.winfo_children()):
                w.destroy()
            self._build()
            self._lista_guncelle()
            if aktif_hid:
                self.after(10, lambda: self._yukle(hid=aktif_hid, vid=aktif_vid))
            self.after(20, self._ayarlar_ac)   # istifadəçi Ayarlar'da idi — yeni dildə davam etsin

        self.after(10, _yenidenqur)

    def _ayarlar_ac(self):
        from tkinter import filedialog
        import tkinter.messagebox
        win = ctk.CTkToplevel(self, fg_color=TH["bg"])
        win.title(t("Ayarlar"))
        win.geometry("560x560")
        win.attributes("-topmost", True)
        win.grab_set()

        tab = ctk.CTkTabview(win, fg_color="transparent",
                              segmented_button_fg_color=TH["accent_soft"],
                              segmented_button_selected_color=TH["accent"],
                              segmented_button_selected_hover_color=TH["accent_hover"],
                              segmented_button_unselected_color="#8FB0DC",
                              text_color="white")
        tab.pack(fill="both", expand=True, padx=12, pady=12)
        t_umumi = tab.add(t("Ümumi"))
        t_bulud = tab.add(t("Bulud"))
        t_sesli = tab.add(t("Səsli Asistent"))
        t_haqqinda = tab.add(t("Haqqında"))

        DIL_SECENEKLERI = [("az", "Azərbaycan"), ("en", "English"), ("ru", "Русский"), ("tr", "Türkçe")]
        DIL_AD_TO_KOD = {ad: kod for kod, ad in DIL_SECENEKLERI}
        ctk.CTkLabel(t_umumi, text="DİL / LANGUAGE",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(12, 2))
        dil_var = tk.StringVar(value=next(ad for kod, ad in DIL_SECENEKLERI if kod == CURRENT_LANG))
        def _dil_secildi(secim):
            self._dil_uygula(DIL_AD_TO_KOD.get(secim, "az"))
        ctk.CTkOptionMenu(t_umumi, values=[ad for _, ad in DIL_SECENEKLERI], variable=dil_var,
                          width=200, height=32, corner_radius=8,
                          fg_color=TH["accent_soft"], text_color=TH["accent"],
                          button_color=TH["accent"], button_hover_color=TH["accent_hover"],
                          command=_dil_secildi).pack(anchor="w", pady=(0, 10))

        ctk.CTkFrame(t_umumi, height=1, fg_color="#3B5E8F").pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(t_umumi, text=t("RENTGEN QOVLUĞU"),
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(12, 2))
        ctk.CTkLabel(t_umumi, text=t("Rentgen görüntülərinin avtomatik tutulacağı qovluq."),
                     font=ctk.CTkFont(size=11),
                     text_color=TH["txt_sub"]).pack(anchor="w")
        row = ctk.CTkFrame(t_umumi, fg_color="transparent")
        row.pack(fill="x", pady=(6, 6))
        yol_var = tk.StringVar(value=xray_watch_dir())
        ctk.CTkEntry(row, textvariable=yol_var, height=32,
                     border_color=TH["accent_soft"], fg_color=TH["panel_soft"]).pack(side="left", fill="x", expand=True)
        def _gozat():
            d = filedialog.askdirectory(parent=win,
                                        initialdir=yol_var.get() if os.path.isdir(yol_var.get()) else "C:\\")
            if d:
                yol_var.set(os.path.normpath(d))
        ctk.CTkButton(row, text=t("📂 Gözat…"), width=100, height=32,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"],
                      command=_gozat).pack(side="left", padx=(8, 0))
        alt_var = tk.BooleanVar(value=xray_subdir_only())
        ctk.CTkCheckBox(t_umumi, text=t("Yalnız alt qovluqlardakı faylları al"),
                        variable=alt_var, font=ctk.CTkFont(size=11),
                        text_color=TH["txt"]).pack(anchor="w", pady=(0, 4))
        durum = ctk.CTkLabel(t_umumi, text="", font=ctk.CTkFont(size=11), text_color=TH["ok"])
        durum.pack(anchor="w")
        def _kaydet():
            yol = yol_var.get().strip()
            AYARLAR["xray_klasoru"] = yol
            AYARLAR["alt_klasor_sarti"] = bool(alt_var.get())
            ayar_kaydet(AYARLAR)
            self._restart_xray_watcher()
            if yol and not os.path.isdir(yol):
                durum.configure(text="⚠ Qovluq hələ yoxdur — yaranınca izlənəcək.",
                                text_color="#C0392B")
            else:
                durum.configure(text="✓ Saxlanıldı.", text_color=TH["ok"])
        ctk.CTkButton(t_umumi, text=t("💾 Saxla"), width=110, height=32, corner_radius=10,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"],
                      font=ctk.CTkFont(size=12, weight="bold"),
                      command=_kaydet).pack(anchor="e", pady=(4, 10))

        ctk.CTkFrame(t_umumi, height=1, fg_color="#3B5E8F").pack(fill="x", pady=(2, 10))

        ctk.CTkLabel(t_umumi, text=t("VR REJİMİ"),
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(0, 2))
        vr_var = tk.BooleanVar(value=bool(AYARLAR.get("vr_rejimi", False)))
        def _vr_toggle():
            AYARLAR["vr_rejimi"] = bool(vr_var.get())
            ayar_kaydet(AYARLAR)
            self._vr_ui_guncelle()
        ctk.CTkSwitch(t_umumi, text=t("Header-də \"Tedaviyə Keç / VR Düzenle / Layout Kaydet\" düymələrini göstər"),
                      variable=vr_var, onvalue=True, offvalue=False, command=_vr_toggle,
                      font=ctk.CTkFont(size=11), text_color=TH["txt"],
                      progress_color=TH["accent"]).pack(anchor="w", pady=(2, 10))

        cloud_frame = ctk.CTkFrame(t_bulud, fg_color="transparent")
        cloud_frame.pack(fill="both", expand=True)

        def _cloud_section_build():
            for w in cloud_frame.winfo_children(): w.destroy()
            ctk.CTkLabel(cloud_frame, text=t("☁ BULUD SENKRONİZASYONU"),
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(14, 2))
            if not cloud_configured():
                ctk.CTkLabel(cloud_frame,
                             text=t("Bulud sunucusu hələ qurulmayıb — proqram yalnız yerel işləyir."),
                             font=ctk.CTkFont(size=11), text_color=TH["txt_sub"]
                             ).pack(anchor="w", padx=18, pady=(0, 12))
                return
            email = AYARLAR.get("sb_email", "")

            def _friendly_err(e):
                s = str(e)
                if "user_already_exists" in s or "already registered" in s.lower():
                    return "Bu e-poçt artıq qeydiyyatdan keçib — \"Giriş Et\" düyməsini işlədin."
                if "invalid_credentials" in s or "Invalid login" in s:
                    return "E-poçt və ya şifrə yanlışdır."
                if "vaxt bitdi" in s or "timeout" in s.lower():
                    return "Bulud serverinə bağlantı vaxtı bitdi (server yavaş cavab verir ya da əlçatan deyil)."
                if "Bağlantı xətası" in s:
                    # DNS/SSL/proksi/firewall kimi konkret səbəblər ola bilər — "internet yoxdur"
                    # demək HƏMİŞƏ doğru deyil (istifadəçidə internet ola bilər, amma bu KONKRET
                    # server əlçatan olmaya bilər), ona görə əsl səbəbi gizlətmədən göstəririk
                    sebeb = s.split("Bağlantı xətası:", 1)[-1].strip()
                    return f"Bulud serverinə qoşulmaq mümkün olmadı ({sebeb}). İnternet varsa, bu server/domen bu şəbəkədə firewall/proksi tərəfindən bloklana bilər."
                return s

            if AYARLAR.get("sb_access_token"):
                ctk.CTkLabel(cloud_frame, text=f"Giriş edilib: {email}",
                             font=ctk.CTkFont(size=12, weight="bold"),
                             text_color=TH["ok"]).pack(anchor="w", padx=18, pady=(0, 8))
                btn_row = ctk.CTkFrame(cloud_frame, fg_color="transparent")
                btn_row.pack(anchor="w", padx=18, pady=(0, 4))
                cstatus = ctk.CTkLabel(cloud_frame, text="", font=ctk.CTkFont(size=11),
                                       text_color=TH["txt_sub"], justify="left", wraplength=560)
                busy2 = {"on": False}

                def _set_status(t, col=None):
                    try:
                        cstatus.configure(text=t, text_color=col or TH["txt_sub"])
                    except Exception:
                        pass

                def _set_busy2(v):
                    busy2["on"] = v
                    st = "disabled" if v else "normal"
                    try:
                        push_btn.configure(state=st)
                        pull_btn.configure(state=st)
                        pull_one_btn.configure(state=st)
                        logout_btn.configure(state=st)
                    except Exception:
                        pass

                def _push():
                    if busy2["on"]: return
                    _set_busy2(True)
                    _set_status("Göndərilir…")
                    def prog(i, toplam, ad):
                        win.after(0, lambda: _set_status(f"Göndərilir… {i}/{toplam} ({ad})"))
                    def work():
                        try:
                            with _CLOUD_SYNC_LOCK:   # avtomatik sinxronla üst-üstə düşməsin
                                n, basarisiz = self.cloud_push_all(ilerleme=prog)
                            AYARLAR["sb_son_senkron"] = datetime.now().strftime("%H:%M")
                            ayar_kaydet(AYARLAR)
                            def ok():
                                _set_busy2(False)
                                if basarisiz:
                                    detal = "\n".join(f"{ad}: {sebeb}" for ad, sebeb in basarisiz)
                                    _set_status(f"⚠ {n} hasta göndərildi, {len(basarisiz)} göndərilmədi:\n{detal}",
                                                "#C0392B")
                                else:
                                    _set_status(f"✓ {n} hasta göndərildi", TH["ok"])
                                self._cloud_status_guncelle()
                            win.after(0, ok)
                        except Exception as e:
                            err = e
                            def fail():
                                _set_busy2(False)
                                _set_status(f"✗ Xəta: {_friendly_err(err)}", "#C0392B")
                            win.after(0, fail)
                    threading.Thread(target=work, daemon=True).start()

                def _pull():
                    if busy2["on"]: return
                    if not tkinter.messagebox.askyesno(
                            "Buluddan Yenilə",
                            t("Buluddakı verilər yerel verilərin üzərinə yazılacaq "
                            "(hər iki tərəfdə eyni hasta varsa buludtakı qalacaq).\n\nDavam edilsin?"),
                            parent=win):
                        return
                    _set_busy2(True)
                    _set_status("Endirilir…")
                    def work():
                        try:
                            with _CLOUD_SYNC_LOCK:   # avtomatik sinxronla üst-üstə düşməsin
                                n = self.cloud_pull_all()
                            def ok():
                                _set_busy2(False)
                                _set_status(f"✓ {n} hasta endirildi", TH["ok"])
                                self._lista_guncelle()
                            win.after(0, ok)
                        except Exception as e:
                            err = e
                            def fail():
                                _set_busy2(False)
                                _set_status(f"✗ Xəta: {_friendly_err(err)}", "#C0392B")
                            win.after(0, fail)
                    threading.Thread(target=work, daemon=True).start()

                def _pull_one():
                    if busy2["on"]: return
                    _set_busy2(True)
                    _set_status("Hasta siyahısı çəkilir…")
                    def work():
                        try:
                            with _CLOUD_SYNC_LOCK:
                                names = self.cloud_list_names()
                            def ok():
                                _set_busy2(False)
                                _set_status("")
                                self._hasta_sec_dialog(win, names, _set_status)
                            win.after(0, ok)
                        except Exception as e:
                            err = e
                            def fail():
                                _set_busy2(False)
                                _set_status(f"✗ Xəta: {_friendly_err(err)}", "#C0392B")
                            win.after(0, fail)
                    threading.Thread(target=work, daemon=True).start()

                def _logout():
                    for k in ("sb_access_token", "sb_refresh_token", "sb_user_id"):
                        AYARLAR.pop(k, None)
                    _CLOUD_SENT_HASH.clear()   # başqa hesaba keçilə bilər, köhnə hash-lər yanlış olar
                    ayar_kaydet(AYARLAR)
                    self._cloud_status_guncelle()
                    _cloud_section_build()

                push_btn = ctk.CTkButton(btn_row, text=t("☁ Buluda Yüklə"), width=140, height=30,
                              fg_color=TH["accent"], hover_color=TH["accent_hover"],
                              command=_push)
                push_btn.pack(side="left", padx=(0, 6))
                pull_btn = ctk.CTkButton(btn_row, text=t("☁ Buluddan Yenilə"), width=150, height=30,
                              fg_color=TH["indigo"], hover_color=TH["indigo_hover"],
                              command=_pull)
                pull_btn.pack(side="left", padx=(0, 6))
                pull_one_btn = ctk.CTkButton(btn_row, text=t("☁ Hasta Seç…"), width=130, height=30,
                              fg_color="transparent", border_width=1, border_color=TH["indigo"],
                              text_color=TH["indigo"], hover_color=TH["accent_soft"],
                              command=_pull_one)
                pull_one_btn.pack(side="left", padx=(0, 6))
                logout_btn = ctk.CTkButton(btn_row, text=t("Çıxış"), width=80, height=30,
                              fg_color="transparent", border_width=1, border_color="#3B5E8F",
                              text_color="#B9CDEB", command=_logout)
                logout_btn.pack(side="left")
                cstatus.pack(anchor="w", padx=18, pady=(6, 12))
            else:
                ctk.CTkLabel(cloud_frame,
                             text=t("Hekim hesabı ilə giriş edin — eyni hesabla giriş edilən bütün\n"
                                  "kompüterlər hasta verilərini avtomatik paylaşır."),
                             justify="left", font=ctk.CTkFont(size=11),
                             text_color=TH["txt_sub"]).pack(anchor="w", padx=18, pady=(0, 8))
                erow = ctk.CTkFrame(cloud_frame, fg_color="transparent")
                erow.pack(fill="x", padx=18, pady=(0, 4))
                email_var = tk.StringVar(value=AYARLAR.get("sb_email", ""))
                pass_var = tk.StringVar()
                ctk.CTkEntry(erow, textvariable=email_var, placeholder_text=t("E-poçt"), height=32,
                             border_color=TH["accent_soft"], fg_color=TH["panel_soft"]
                             ).pack(side="left", fill="x", expand=True, padx=(0, 6))
                ctk.CTkEntry(erow, textvariable=pass_var, placeholder_text=t("Şifrə"), show="•", height=32,
                             border_color=TH["accent_soft"], fg_color=TH["panel_soft"], width=140
                             ).pack(side="left")
                cstatus = ctk.CTkLabel(cloud_frame, text="", font=ctk.CTkFont(size=11),
                                       text_color=TH["txt_sub"], justify="left", wraplength=560)
                busy = {"on": False}

                def _set_status(t, col=None):
                    try:
                        cstatus.configure(text=t, text_color=col or TH["txt_sub"])
                    except Exception:
                        pass   # pencerə artıq bağlanmış ola bilər

                def _set_busy(v):
                    busy["on"] = v
                    state_ = "disabled" if v else "normal"
                    try:
                        login_btn.configure(state=state_)
                        signup_btn.configure(state=state_)
                    except Exception:
                        pass

                def _login():
                    if busy["on"]: return
                    email, pw = email_var.get().strip(), pass_var.get()
                    if not email or not pw:
                        _set_status("E-poçt və şifrə daxil edin", "#C0392B"); return
                    _set_busy(True)
                    _set_status("Giriş edilir…")
                    def work():
                        try:
                            res = sb_login(email, pw)
                            if not res.get("access_token"):
                                raise RuntimeError(res.get("error_description") or res.get("msg") or "Bilinməyən xəta")
                            AYARLAR["sb_access_token"]  = res["access_token"]
                            AYARLAR["sb_refresh_token"] = res.get("refresh_token")
                            AYARLAR["sb_user_id"]       = res.get("user", {}).get("id")
                            AYARLAR["sb_email"]         = email
                            ayar_kaydet(AYARLAR)
                            def ok():
                                self._cloud_status_guncelle()
                                self._cloud_start_after_auth()
                                _cloud_section_build()
                            win.after(0, ok)
                        except Exception as e:
                            err = e
                            def fail():
                                _set_busy(False)
                                _set_status(f"✗ Giriş alınmadı: {_friendly_err(err)}", "#C0392B")
                            win.after(0, fail)
                    threading.Thread(target=work, daemon=True).start()

                def _signup():
                    if busy["on"]: return
                    email, pw = email_var.get().strip(), pass_var.get()
                    if not email or not pw:
                        _set_status("E-poçt və şifrə daxil edin", "#C0392B"); return
                    if len(pw) < 6:
                        _set_status("Şifrə ən az 6 simvol olmalıdır", "#C0392B"); return
                    _set_busy(True)
                    _set_status("Hesab yaradılır…")
                    def work():
                        try:
                            res = sb_signup(email, pw)
                            if res.get("access_token"):
                                AYARLAR["sb_access_token"]  = res["access_token"]
                                AYARLAR["sb_refresh_token"] = res.get("refresh_token")
                                AYARLAR["sb_user_id"]       = res.get("user", {}).get("id")
                                AYARLAR["sb_email"]         = email
                                ayar_kaydet(AYARLAR)
                                def ok():
                                    self._cloud_status_guncelle()
                                    self._cloud_start_after_auth()
                                    _cloud_section_build()
                                win.after(0, ok)
                            elif res.get("id"):
                                def ok2():
                                    _set_busy(False)
                                    _set_status(
                                        "✓ Hesab yaradıldı — e-poçtunuzu təsdiqləyib indi giriş edin", TH["ok"])
                                win.after(0, ok2)
                            else:
                                raise RuntimeError(res.get("msg") or res.get("error_description") or str(res)[:200])
                        except Exception as e:
                            err = e
                            def fail():
                                _set_busy(False)
                                _set_status(f"✗ Xəta: {_friendly_err(err)}", "#C0392B")
                            win.after(0, fail)
                    threading.Thread(target=work, daemon=True).start()

                brow = ctk.CTkFrame(cloud_frame, fg_color="transparent")
                brow.pack(anchor="w", padx=18, pady=(0, 4))
                login_btn = ctk.CTkButton(brow, text=t("Giriş Et"), width=100, height=30,
                              fg_color=TH["accent"], hover_color=TH["accent_hover"],
                              command=_login)
                login_btn.pack(side="left", padx=(0, 6))
                signup_btn = ctk.CTkButton(brow, text=t("Hesab Yarat"), width=110, height=30,
                              fg_color="transparent", border_width=1, border_color="#3B5E8F",
                              text_color="#B9CDEB", command=_signup)
                signup_btn.pack(side="left")
                cstatus.pack(anchor="w", padx=18, pady=(6, 12))

        _cloud_section_build()

        # ── Səsli Asistent ──
        ctk.CTkLabel(t_sesli, text=t("SƏSLİ ASİSTENT"),
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(12, 2))
        ctk.CTkLabel(t_sesli,
                     text=t("Oyadıcı sözü deyib ardından pasientə randevu vaxtı və ya dişlərə "
                            "ediləcək işləri səslə deyə bilərsiniz."),
                     font=ctk.CTkFont(size=11), text_color=TH["txt_sub"],
                     wraplength=480, justify="left").pack(anchor="w", pady=(0, 10))

        sesli_aktiv_var = tk.BooleanVar(value=sesli_asistent_aktiv())

        def _sesli_toggle():
            AYARLAR["sesli_asistent_aktiv"] = sesli_aktiv_var.get()
            ayar_kaydet(AYARLAR)
            if sesli_aktiv_var.get():
                self._sesli_baslat()
            else:
                self._sesli_dayandir()

        ctk.CTkCheckBox(t_sesli, text=t("Səsli asistenti aktivləşdir (davamlı dinləmə)"),
                        variable=sesli_aktiv_var, font=ctk.CTkFont(size=12),
                        text_color=TH["txt"], command=_sesli_toggle).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(t_sesli, text=t("OYADICI SÖZ"), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(4, 2))
        oyadici_var = tk.StringVar(value=sesli_oyadici_soz())
        oyadici_status = ctk.CTkLabel(t_sesli, text="", font=ctk.CTkFont(size=10),
                                       text_color="#2E7D32")

        def _oyadici_kaydet(*_):
            AYARLAR["sesli_oyadici_soz"] = (oyadici_var.get() or "bulex").strip().lower()
            ayar_kaydet(AYARLAR)
            try:
                oyadici_status.configure(text=t("✓ Saxlandı"))
                self.after(1500, lambda: oyadici_status.configure(text=""))
            except Exception:
                pass

        oyadici_entry = ctk.CTkEntry(t_sesli, textvariable=oyadici_var, width=200, height=32,
                                      border_color=TH["accent_soft"])
        oyadici_entry.pack(anchor="w", pady=(0, 2))
        # "FocusOut" tək başına kifayət deyildi — istifadəçi sözü yazıb Enter basanda
        # (fokus sahədən çıxmadığı üçün) heç nə saxlanmırdı; Return/KP_Enter də əlavə edildi.
        oyadici_entry.bind("<FocusOut>", _oyadici_kaydet)
        oyadici_entry.bind("<Return>", _oyadici_kaydet)
        oyadici_entry.bind("<KP_Enter>", _oyadici_kaydet)
        oyadici_status.pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(t_sesli, text=t("ANTHROPIC API AÇARI (İSTƏYƏ BAĞLI)"),
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(4, 2))
        ctk.CTkLabel(t_sesli,
                     text=t("Daxil etsəniz, əmrin mənasını yerli qaydalar əvəzinə Claude AI "
                            "(Anthropic) təhlil edəcək — daha çevik, amma mətn (yalnız tanınan "
                            "söz, SƏS/şəkil YOX) internetə göndərilir və öz hesabınızdan ödənilir. "
                            "Boş qalsa, hər şey əvvəlki kimi tam yerli (offline) işləyir."),
                     font=ctk.CTkFont(size=10), text_color=TH["txt_faint"],
                     wraplength=480, justify="left").pack(anchor="w", pady=(0, 6))
        api_key_var = tk.StringVar(value=AYARLAR.get("anthropic_api_key") or "")
        api_key_status = ctk.CTkLabel(t_sesli, text="", font=ctk.CTkFont(size=10),
                                       text_color="#2E7D32")

        def _api_key_kaydet(*_):
            AYARLAR["anthropic_api_key"] = api_key_var.get().strip()
            ayar_kaydet(AYARLAR)
            try:
                api_key_status.configure(text=t("✓ Saxlandı"))
                self.after(1500, lambda: api_key_status.configure(text=""))
            except Exception:
                pass

        api_key_entry = ctk.CTkEntry(t_sesli, textvariable=api_key_var, width=420, height=32,
                                      show="•", border_color=TH["accent_soft"])
        api_key_entry.pack(anchor="w", pady=(0, 2))
        api_key_entry.bind("<FocusOut>", _api_key_kaydet)
        api_key_entry.bind("<Return>", _api_key_kaydet)
        api_key_entry.bind("<KP_Enter>", _api_key_kaydet)
        api_key_status.pack(anchor="w", pady=(0, 8))

        ctk.CTkFrame(t_sesli, height=1, fg_color="#3B5E8F").pack(fill="x", pady=(2, 10))

        # ── Ümumi AI Söhbəti (push-to-talk) ──
        ctk.CTkLabel(t_sesli, text=t("ÜMUMİ AI SÖHBƏTİ (PUSH-TO-TALK)"),
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(t_sesli,
                     text=t("Düyməni basılı saxlayıb danışın — tanınan mətn Claude-a adi "
                            "söhbət mesajı kimi göndərilir (yuxarıdaki Anthropic açarı "
                            "tələb olunur), cavab həm yazılı, həm səslə (internet lazımdır) "
                            "gəlir. Dental əmr tanınarsa, icra ETMƏZDƏN ƏVVƏL təsdiq istənir."),
                     font=ctk.CTkFont(size=10), text_color=TH["txt_faint"],
                     wraplength=480, justify="left").pack(anchor="w", pady=(0, 6))

        chat_aktiv_var = tk.BooleanVar(value=chat_asistent_aktiv())

        def _chat_toggle():
            if chat_aktiv_var.get() and not (AYARLAR.get("anthropic_api_key") or "").strip():
                import tkinter.messagebox as mb
                mb.showwarning(t("AI Söhbət"), t("Əvvəlcə yuxarıda Anthropic API açarını daxil edin."))
                chat_aktiv_var.set(False)
                return
            AYARLAR["chat_asistent_aktiv"] = chat_aktiv_var.get()
            ayar_kaydet(AYARLAR)
            if chat_aktiv_var.get():
                self._chat_baslat()
            else:
                self._chat_dayandir()

        ctk.CTkCheckBox(t_sesli, text=t("Ümumi AI söhbətini aktivləşdir"),
                        variable=chat_aktiv_var, font=ctk.CTkFont(size=12),
                        text_color=TH["txt"], command=_chat_toggle).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(t_sesli, text=t("DÜYMƏ (PUSH-TO-TALK)"), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(4, 2))
        hotkey_var = tk.StringVar(value=chat_hotkey())
        hotkey_status = ctk.CTkLabel(t_sesli, text="", font=ctk.CTkFont(size=10),
                                      text_color="#2E7D32")

        def _hotkey_kaydet(*_):
            AYARLAR["chat_hotkey"] = (hotkey_var.get() or "f9").strip().lower()
            ayar_kaydet(AYARLAR)
            if self._chat_hotkey_qeydli:
                self._chat_hotkey_sil()
                self._chat_hotkey_qeydiyyat()
            try:
                hotkey_status.configure(text=t("✓ Saxlandı"))
                self.after(1500, lambda: hotkey_status.configure(text=""))
            except Exception:
                pass

        hotkey_entry = ctk.CTkEntry(t_sesli, textvariable=hotkey_var, width=120, height=32,
                                     border_color=TH["accent_soft"])
        hotkey_entry.pack(anchor="w", pady=(0, 2))
        hotkey_entry.bind("<FocusOut>", _hotkey_kaydet)
        hotkey_entry.bind("<Return>", _hotkey_kaydet)
        hotkey_entry.bind("<KP_Enter>", _hotkey_kaydet)
        hotkey_status.pack(anchor="w", pady=(0, 8))

        ctk.CTkLabel(t_sesli, text=t("SƏS (TTS)"), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(4, 2))
        ses_var = tk.StringVar(value=tts_ses_adi())

        def _ses_kaydet(secim):
            AYARLAR["tts_ses"] = secim
            ayar_kaydet(AYARLAR)

        ctk.CTkOptionMenu(t_sesli, variable=ses_var,
                          values=["az-AZ-BabekNeural", "az-AZ-BanuNeural"],
                          width=220, height=32, command=_ses_kaydet).pack(anchor="w", pady=(0, 10))

        ctk.CTkFrame(t_sesli, height=1, fg_color="#3B5E8F").pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(t_sesli, text=t("NÜMUNƏ İFADƏLƏR"), font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(t_sesli,
                     text="„Bulex, 16-cı dişdə kanal”\n"
                          "„Bulex, 24 və 25-ci dişlərə kompozit dolğu”\n"
                          "„Bulex, Əliyevə sabah saat üçdə randevu”",
                     font=ctk.CTkFont(size=11), text_color=TH["txt_sub"],
                     justify="left").pack(anchor="w")

        ctk.CTkLabel(t_sesli,
                     text=t("Qeyd: ilk aktivləşdirmədə tanıma modeli (~500MB) internetdən bir "
                            "dəfəlik endirilir, sonra tam yerli (offline) işləyir. Mikrofon "
                            "davamlı dinlənilir, amma heç bir səs buluda göndərilmir — hər "
                            "parça yalnız yerli emal olunub dərhal atılır."),
                     font=ctk.CTkFont(size=10), text_color=TH["txt_faint"],
                     wraplength=480, justify="left").pack(anchor="w", pady=(14, 4))

        ctk.CTkLabel(t_haqqinda, text=f"Sürüm: v{APP_VERSION}",
                     font=ctk.CTkFont(size=13, weight="bold"), text_color=TH["txt"]
                     ).pack(pady=(30, 10))
        ctk.CTkButton(t_haqqinda, text=t("🔄 Güncellemeleri Kontrol Et"), width=200, height=32,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"],
                      command=lambda: self._guncelleme_kontrol_et(sessiz=False)
                      ).pack()

    # ── Randevu Təqvimi ──────────────────────────────────────────────────
    # ── Randevu vaxtı çatanda avtomatik "gəldi" təsdiqi ─────────────────────
    def _randevu_tick(self):
        try:
            indi = datetime.now()
            for rid, r in list(self.randevular.items()):
                if r.get("status") != "gozleyir" or rid in self._randevu_prompted:
                    continue
                try:
                    baslangic = datetime.strptime(f"{r['tarix']} {r.get('saat','09:00')}", "%Y-%m-%d %H:%M")
                except Exception:
                    continue
                if baslangic <= indi:
                    self._randevu_prompted.add(rid)
                    self._randevu_confirm_dialog(rid, r)
        except Exception as e:
            print(f"[Randevu] tick xətası: {e}")
        self.after(30000, self._randevu_tick)

    def _randevu_confirm_dialog(self, rid, r):
        dlg = ctk.CTkToplevel(self, fg_color=TH["bg"])
        dlg.title(t("Randevu Vaxtı"))
        dlg.geometry("380x220")
        dlg.attributes("-topmost", True)
        ctk.CTkLabel(dlg, text=t("🕒 Randevu vaxtı çatdı"), font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=TH["accent"]).pack(pady=(20,6))
        ctk.CTkLabel(dlg, text=f"{r.get('hasta_ad','')}  ·  saat {r.get('saat','')}",
                     font=ctk.CTkFont(size=14, weight="bold"), text_color=TH["txt"]).pack(pady=2)
        ctk.CTkLabel(dlg, text=t("Bu xəstə gəldi kimi qeyd edilsin?"),
                     font=ctk.CTkFont(size=12), text_color=TH["txt_sub"]).pack(pady=(2,10))
        geri_sayim_lbl = ctk.CTkLabel(dlg, text="", font=ctk.CTkFont(size=10), text_color=TH["txt_faint"])
        geri_sayim_lbl.pack()

        bitdi = {"v": False}

        def _qeyd_et():
            if bitdi["v"]: return
            bitdi["v"] = True
            r2 = self.randevular.get(rid)
            if r2:
                r2["status"] = "geldi"
                randevu_kaydet(self.randevular)
            try: dlg.destroy()
            except Exception: pass

        def _legv():
            bitdi["v"] = True
            try: dlg.destroy()
            except Exception: pass

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=10)
        ctk.CTkButton(btn_row, text=t("✓ Gəldi"), width=110, height=34, corner_radius=10,
                      fg_color=TH["ok"], hover_color=TH["ok_hover"],
                      font=ctk.CTkFont(size=12, weight="bold"), command=_qeyd_et).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text=t("Sonra"), width=110, height=34, corner_radius=10,
                      fg_color="transparent", border_width=1, border_color=TH["accent_soft"],
                      text_color=TH["accent"], command=_legv).pack(side="left", padx=6)

        qalan = {"saniye": 120}
        def _geri_sayim():
            if bitdi["v"]:
                return
            try:
                geri_sayim_lbl.configure(
                    text=f"{qalan['saniye']} saniyə sonra avtomatik 'Gəldi' qəbul ediləcək")
            except Exception:
                return
            if qalan["saniye"] <= 0:
                _qeyd_et()
                return
            qalan["saniye"] -= 1
            dlg.after(1000, _geri_sayim)
        _geri_sayim()

    def _pencereni_one_cek(self, win):
        """Yeni açılan/artıq açıq olan pəncərəni digər proqramların ÜSTÜNƏ çıxarır.

        Sırf lift()/focus_force() bəzən Windows-da pəncərəni digər tətbiqlərin
        arxasında saxlayır — görünən taskbar fokus dəyişikliyi yalnız qısa
        topmost yanıb-sönməsi ilə etibarlı işləyir.
        """
        try:
            win.deiconify()
            win.lift()
            win.attributes("-topmost", True)
            win.focus_force()
            win.after(200, lambda: win.attributes("-topmost", False))
        except Exception:
            pass

    def _randevu_ac(self):
        import tkinter.messagebox as mb
        if self._randevu_win is not None and self._randevu_win.winfo_exists():
            self._pencereni_one_cek(self._randevu_win)
            return
        win = ctk.CTkToplevel(self, fg_color=TH["bg"])
        self._randevu_win = win
        win.title(t("Randevu Təqvimi"))
        win.geometry("1380x860")
        win.after(50, lambda: win.state("zoomed"))   # 08-22 tam sığsın deyə böyüdülmüş açılır
        self._pencereni_one_cek(win)
        # QƏSDƏN topmost/grab_set YOXDUR — bu pəncərə arxa planda açıq qalıb istifadəçi
        # eyni vaxtda əsas planlama pəncərəsində işləyə bilsin (minimize/keçid sərbəst olsun).
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        GUN_QISA = ["B.e","Ç.a","Ç","C.a","C","Ş","B"]
        AY_QISA  = ["Yan","Fev","Mar","Apr","May","İyn","İyl",
                    "Avq","Sen","Okt","Noy","Dek"]
        GOVDE_YUKSEKLIK = (RANDEVU_SAAT_SONU - RANDEVU_SAAT_BASI) * RANDEVU_SAAT_PX

        # CTk widgetləri (bant/gridxətt/saat etiketi) place() zamanı ekran DPI-na görə
        # avtomatik miqyaslanır (məs. 125% DPI-da 1.25x), amma bloklar üçün istifadə
        # olunan tk.Canvas (plain tkinter) bu miqyaslamanı ALMIR — nəticədə saat
        # xətləri ilə randevu blokları get-gedə (aşağı sətirlərdə daha çox) sürüşürdü.
        # `olcek` bu fərqi əl ilə kompensasiya edir.
        olcek = ctk.ScalingTracker.get_widget_scaling(win) or 1.0

        def hafte_bas(iso):
            d = date.fromisoformat(iso)
            return (d - timedelta(days=d.weekday())).isoformat()

        def y_px(saat_str):
            """Məntiqi (miqyaslanmamış) piksel — saat mətnindən sətir mövqeyi."""
            try:
                hh, mm = map(int, saat_str.split(":"))
            except Exception:
                hh, mm = RANDEVU_SAAT_BASI, 0
            dakika = (hh - RANDEVU_SAAT_BASI) * 60 + mm
            dakika = max(0, min(dakika, (RANDEVU_SAAT_SONU - RANDEVU_SAAT_BASI) * 60))
            return dakika * (RANDEVU_SAAT_PX / 60)

        def h_px(muddet_dk):
            """Məntiqi (miqyaslanmamış) piksel — müddətdən blok hündürlüyü."""
            return max(18, muddet_dk * (RANDEVU_SAAT_PX / 60))

        def px_to_saat(y_fiziki):
            """Canvas-dan gələn FİZİKİ (ekran) pikseli məntiqi vahidə çevirib saata çevirir."""
            y = y_fiziki / olcek
            dakika = int(y / (RANDEVU_SAAT_PX / 60))
            dakika = max(0, min(dakika, (RANDEVU_SAAT_SONU - RANDEVU_SAAT_BASI) * 60))
            dakika = round(dakika / 5) * 5
            hh = RANDEVU_SAAT_BASI + dakika // 60
            mm = dakika % 60
            return f"{hh:02d}:{mm:02d}"

        state = {"hafte": hafte_bas(date.today().isoformat())}

        # ── Üst: naviqasiya ──
        ust = ctk.CTkFrame(win, fg_color=TH["panel"], corner_radius=14, height=56)
        ust.grid(row=0, column=0, sticky="ew", padx=16, pady=(16,8)); ust.grid_propagate(False)
        ctk.CTkButton(ust, text="‹", width=36, height=36, corner_radius=10,
                      fg_color=TH["accent_soft"], text_color=TH["accent"],
                      hover_color=TH["sidebar_row"],
                      command=lambda: _hafte_kaydir(-1)).pack(side="left", padx=(14,4), pady=10)
        baslik_lbl = ctk.CTkLabel(ust, text="", font=ctk.CTkFont(size=15, weight="bold"),
                                   text_color=TH["txt"])
        baslik_lbl.pack(side="left", padx=10)
        ctk.CTkButton(ust, text="›", width=36, height=36, corner_radius=10,
                      fg_color=TH["accent_soft"], text_color=TH["accent"],
                      hover_color=TH["sidebar_row"],
                      command=lambda: _hafte_kaydir(1)).pack(side="left", padx=4, pady=10)
        ctk.CTkButton(ust, text=t("Bu Həftə"), width=90, height=32, corner_radius=8,
                      fg_color="transparent", border_width=1, border_color=TH["accent_soft"],
                      text_color=TH["accent"], hover_color=TH["sidebar_row"],
                      command=lambda: _bu_hafte()).pack(side="left", padx=(10,0), pady=10)
        ctk.CTkLabel(ust, text=t("İpucu: boş xanaya klikləyib o saata randevu əlavə et"),
                     font=ctk.CTkFont(size=10), text_color=TH["txt_faint"]).pack(side="left", padx=(16,0))
        ctk.CTkButton(ust, text=t("+ Yeni Randevu"), height=36, corner_radius=10,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"],
                      font=ctk.CTkFont(size=12, weight="bold"),
                      command=lambda: _form_ac()).pack(side="right", padx=14, pady=10)
        ctk.CTkButton(ust, text=t("🎨 Rənglər"), width=110, height=32, corner_radius=8,
                      fg_color="transparent", border_width=1, border_color=TH["accent_soft"],
                      text_color=TH["accent"], hover_color=TH["sidebar_row"],
                      command=lambda: _renk_ayarlari_ac()).pack(side="right", padx=(0,8), pady=10)

        # ── Cədvəl: saat oxu + 7 gün sütunu ──
        cedvel = ctk.CTkFrame(win, fg_color="transparent")
        cedvel.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0,16))
        cedvel.grid_columnconfigure(0, weight=0, minsize=64)
        for i in range(7):
            cedvel.grid_columnconfigure(i+1, weight=1, uniform="gun")
        cedvel.grid_rowconfigure(1, weight=1)

        ctk.CTkFrame(cedvel, fg_color="transparent", height=52).grid(
            row=0, column=0, sticky="ew", pady=(0,4))
        baslik_hucreleri = []
        for i in range(7):
            hb = ctk.CTkFrame(cedvel, corner_radius=10, height=52)
            hb.grid(row=0, column=i+1, sticky="ew", padx=3, pady=(0,4)); hb.grid_propagate(False)
            gun_ad_lbl = ctk.CTkLabel(hb, text="", font=ctk.CTkFont(size=11, weight="bold"))
            gun_ad_lbl.pack(pady=(8,0))
            gun_tarix_lbl = ctk.CTkLabel(hb, text="", font=ctk.CTkFont(size=13, weight="bold"))
            gun_tarix_lbl.pack()
            baslik_hucreleri.append((hb, gun_ad_lbl, gun_tarix_lbl))

        # Saat oxu + gün sütunları bir ŞAQULI SÜRÜŞDÜRÜLƏ BİLƏN sahədə (günlərin başlığı
        # yuxarıda sabit qalır) — 08-22 aralığı istənilən ekran hündürlüyündə görünsün deyə
        # (böyüdülmüş pəncərə belə hər zaman tam sığmaya bilər).
        govde = ctk.CTkScrollableFrame(cedvel, fg_color="transparent")
        govde.grid(row=1, column=0, columnspan=8, sticky="nsew")
        govde.grid_columnconfigure(0, weight=0, minsize=64)
        for i in range(7):
            govde.grid_columnconfigure(i+1, weight=1, uniform="gun")

        SAAT_XETT_RENGI = "#A9C0E0"   # saat sərhədləri — panelin ağ zeminində aydın görünsün deyə TH["accent_soft"]-dan qatı
        BANT_ACIQ, BANT_TUND = TH["panel"], "#F1F6FD"   # cüt/tək saatlar arası zolaqlama (əsl gridxətt effekti)

        saat_ekseni = ctk.CTkFrame(govde, fg_color="transparent", height=GOVDE_YUKSEKLIK)
        saat_ekseni.grid(row=0, column=0, sticky="new"); saat_ekseni.grid_propagate(False)
        for h_i, saat_deger in enumerate(range(RANDEVU_SAAT_BASI, RANDEVU_SAAT_SONU + 1)):
            ctk.CTkLabel(saat_ekseni, text=f"{saat_deger:02d}:00",
                         font=ctk.CTkFont(size=13, weight="bold"),
                         text_color=TH["txt"]).place(x=4, y=h_i * RANDEVU_SAAT_PX - 9)

        gun_frameleri, gun_bantlari = [], []
        for i in range(7):
            gf = ctk.CTkFrame(govde, fg_color=TH["panel"], corner_radius=10, height=GOVDE_YUKSEKLIK)
            gf.grid(row=0, column=i+1, sticky="new", padx=3); gf.grid_propagate(False)
            bantlar = []
            for h_i in range(RANDEVU_SAAT_SONU - RANDEVU_SAAT_BASI):
                bant = ctk.CTkFrame(gf, corner_radius=0, cursor="hand2",
                                     fg_color=BANT_ACIQ if h_i % 2 == 0 else BANT_TUND,
                                     height=RANDEVU_SAAT_PX)
                bant.place(x=0, y=h_i * RANDEVU_SAAT_PX, relwidth=1)
                bantlar.append(bant)
            for h_i in range(RANDEVU_SAAT_SONU - RANDEVU_SAAT_BASI + 1):
                ctk.CTkFrame(gf, height=2, fg_color=SAAT_XETT_RENGI).place(
                    x=0, y=h_i * RANDEVU_SAAT_PX, relwidth=1)
            gun_frameleri.append(gf)
            gun_bantlari.append(bantlar)

        gun_zaman_cizgisi = [None] * 7   # bugünkü sütunda canlı "indi" xətti (hər gün üçün ən çox 1 dənə)

        def _gun_randevulari(gun_iso):
            return [(rid, r) for rid, r in self.randevular.items() if r.get("tarix") == gun_iso]

        def _hafte_kaydir(delta):
            state["hafte"] = (date.fromisoformat(state["hafte"]) + timedelta(weeks=delta)).isoformat()
            _ciz()

        def _bu_hafte():
            state["hafte"] = hafte_bas(date.today().isoformat())
            _ciz()

        def _bos_alana_tikla(e, iso, taban_px):
            # taban_px məntiqi vahiddədir (bant indeksindən); e.y isə bandın öz
            # fiziki (miqyaslanmış) koordinat sistemindən gəlir — əvvəlcə eyni vahidə salırıq.
            _form_ac(gun_iso=iso, on_saat=px_to_saat(taban_px * olcek + e.y))

        def _kecmis_mi(r):
            try:
                baslangic = datetime.strptime(f"{r['tarix']} {r.get('saat','09:00')}", "%Y-%m-%d %H:%M")
                bitis = baslangic + timedelta(minutes=int(r.get("muddet", 30) or 30))
                return bitis < datetime.now()
            except Exception:
                return False

        def _ciz():
            win.update()   # sütun eni doğru ölçülsün deyə bloklardan əvvəl (tam update, təkcə idle-tasks yox)
            bas = date.fromisoformat(state["hafte"])
            son = bas + timedelta(days=6)
            baslik_lbl.configure(
                text=f"{bas.day} {t(AY_QISA[bas.month-1])} – {son.day} {t(AY_QISA[son.month-1])} {son.year}")
            bugun = date.today().isoformat()
            for i in range(7):
                gun_tarihi = bas + timedelta(days=i)
                iso = gun_tarihi.isoformat()
                aktif = iso == bugun
                hb, gun_ad_lbl, gun_tarix_lbl = baslik_hucreleri[i]
                hb.configure(fg_color=TH["accent"] if aktif else TH["panel"])
                gun_ad_lbl.configure(text=t(GUN_QISA[i]), text_color="white" if aktif else TH["txt_faint"])
                gun_tarix_lbl.configure(text=str(gun_tarihi.day), text_color="white" if aktif else TH["txt"])

                gf = gun_frameleri[i]
                for w in list(gf.winfo_children()):
                    if getattr(w, "_rndv_blok", False):
                        w.destroy()
                for h_i, bant in enumerate(gun_bantlari[i]):
                    bant.unbind("<Button-1>")
                    taban_px = h_i * RANDEVU_SAAT_PX
                    bant.bind("<Button-1>", lambda e, iso=iso, taban_px=taban_px: _bos_alana_tikla(e, iso, taban_px))
                kol_genislik = max(gf.winfo_width() - 4, 60)
                for rid, r in _gun_randevulari(iso):
                    _render_blok(gf, rid, r, kol_genislik)

                # Canlı "indi" xətti — yalnız bugünkü sütunda, saat oxu ilə tam üst-üstə
                # düşsün deyə eyni y_px()*olcek düsturu ilə (bloklarla eyni koordinat sistemi).
                # plain tk.Frame istifadə olunur (CTkFrame yox) — CTk widgetləri place() ilə
                # verilən y-i avtomatik miqyaslayır, bu da əl ilə *olcek ilə birgə İKİQAT
                # miqyaslamaya səbəb olurdu (bloklarda tk.Canvas seçilməsinin səbəbi də elə budur).
                if aktif:
                    y_fiziki = round(y_px(datetime.now().strftime("%H:%M")) * olcek)
                    cizgi = gun_zaman_cizgisi[i]
                    if cizgi is None or not cizgi.winfo_exists():
                        cizgi = tk.Frame(gf, height=3, bg="#E11D48", highlightthickness=0, bd=0)
                        gun_zaman_cizgisi[i] = cizgi
                    cizgi.place(x=0, y=y_fiziki, relwidth=1)
                    cizgi.lift()   # randevu bloklarının da üstündə görünsün
                elif gun_zaman_cizgisi[i] is not None:
                    if gun_zaman_cizgisi[i].winfo_exists():
                        gun_zaman_cizgisi[i].destroy()
                    gun_zaman_cizgisi[i] = None

        def _zaman_cizgisi_tik():
            if not win.winfo_exists():
                return
            _ciz()
            win.after(60000, _zaman_cizgisi_tik)
        win.after(60000, _zaman_cizgisi_tik)

        def _render_blok(gf, rid, r, kol_genislik):
            saat = r.get("saat", "09:00")
            muddet = int(r.get("muddet", 30) or 30)
            renk = randevu_renk(r.get("status", "gozleyir"))
            # h_px/y_px məntiqi vahiddədir — Canvas plain-tk olduğu üçün CTk-nin
            # avtomatik DPI miqyaslamasından keçmir, ona görə burda əl ilə tətbiq edilir
            # (əks halda bloklar saat xətlərindən aşağı sətirlərdə get-gedə sürüşürdü).
            yukseklik = max(round(h_px(muddet) * olcek), 1)
            kecmis = _kecmis_mi(r)

            # tk.Canvas — CTk widgetlərin place() ilə width/height qəbul etməməsi
            # məhdudiyyətindən yan keçir, həm də tam-dolu rəng + ştrix çəkməyə imkan verir.
            # Canvas-ın öz bg-si = status rəngi, ona görə kənarlar/künclər həmişə tam örtülü olur.
            cv = tk.Canvas(gf, width=kol_genislik, height=yukseklik,
                            highlightthickness=0, bd=0, cursor="hand2", bg=renk)
            cv._rndv_blok = True
            cv.place(x=2, y=round(y_px(saat) * olcek))

            if kecmis:
                acik = tint(renk, 0.4)
                adim = 9
                for x in range(-yukseklik, kol_genislik + yukseklik, adim):
                    cv.create_line(x, yukseklik, x + yukseklik, 0, fill=acik, width=1)

            cv.create_text(6, 3, text=f"{saat} · {r.get('hasta_ad','')}", anchor="nw",
                            fill="white", font=("Segoe UI", 9, "bold"),
                            width=kol_genislik - (22 if yukseklik >= 28 else 8))
            qeyd = r.get("qeyd", "").strip()
            if qeyd and yukseklik >= 38:
                cv.create_text(6, 18, text=qeyd, anchor="nw",
                                fill="white", font=("Segoe UI", 8),
                                width=kol_genislik - 10)
            cv.bind("<Button-1>", lambda e, rid=rid: _form_ac(rid))
            if yukseklik >= 28:
                sil_id = cv.create_text(kol_genislik - 8, 3, text="✕", anchor="ne",
                                         fill="white", font=("Segoe UI", 10, "bold"))
                def _sil_tikla(e, rid=rid):
                    _sil(rid)
                    return "break"
                cv.tag_bind(sil_id, "<Button-1>", _sil_tikla)

        def _sil(rid):
            if not mb.askyesno(t("Randevunu sil"), t("Bu randevunu silmək istədiyinizə əminsiniz?"), parent=win):
                return
            self.randevular.pop(rid, None)
            randevu_kaydet(self.randevular)
            _ciz()

        def _renk_ayarlari_ac():
            from tkinter import colorchooser
            dlg = ctk.CTkToplevel(win, fg_color=TH["bg"])
            dlg.title(t("Randevu Rəngləri"))
            dlg.geometry("320x300")
            dlg.attributes("-topmost", True)
            dlg.grab_set()
            ctk.CTkLabel(dlg, text=t("STATUS RƏNGLƏRİ"), font=ctk.CTkFont(size=12, weight="bold"),
                         text_color=TH["accent"]).pack(pady=(18,10))

            def _yenidenCiz_swatchlar():
                for w in list(satirlar.winfo_children()): w.destroy()
                for k, l, _ in RANDEVU_STATUS:
                    row = ctk.CTkFrame(satirlar, fg_color="transparent")
                    row.pack(fill="x", padx=18, pady=4)
                    ctk.CTkLabel(row, text=t(l), width=120, anchor="w",
                                 font=ctk.CTkFont(size=12)).pack(side="left")
                    ctk.CTkButton(row, text="", width=70, height=28, corner_radius=6,
                                  fg_color=randevu_renk(k), hover_color=randevu_renk(k),
                                  command=lambda k=k: _renk_sec(k)).pack(side="left", padx=8)

            def _renk_sec(k):
                secim = colorchooser.askcolor(color=randevu_renk(k), parent=dlg, title="Rəng seç")
                if secim and secim[1]:
                    ozel = AYARLAR.setdefault("randevu_renkler", {})
                    ozel[k] = secim[1]
                    ayar_kaydet(AYARLAR)
                    _yenidenCiz_swatchlar()
                    _ciz()

            satirlar = ctk.CTkFrame(dlg, fg_color="transparent")
            satirlar.pack(fill="x")
            _yenidenCiz_swatchlar()

            def _sifirla():
                AYARLAR["randevu_renkler"] = {}
                ayar_kaydet(AYARLAR)
                _yenidenCiz_swatchlar()
                _ciz()

            ctk.CTkButton(dlg, text=t("Defolt Rənglərə Sıfırla"), height=32, corner_radius=8,
                          fg_color="transparent", border_width=1, border_color=TH["accent_soft"],
                          text_color=TH["accent"], hover_color=TH["sidebar_row"],
                          command=_sifirla).pack(pady=16)

        def _form_ac(rid=None, gun_iso=None, on_saat=None):
            mevcut = self.randevular.get(rid, {}) if rid else {}
            dlg = ctk.CTkToplevel(win, fg_color=TH["bg"])
            dlg.title("Randevu" if not rid else "Randevunu Düzenlə")
            dlg.geometry("360x620")
            dlg.attributes("-topmost", True)
            dlg.grab_set()

            ctk.CTkLabel(dlg, text=t("HASTA"), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(16,2))
            isimler = sorted({h.get("ad", "") for h in self.hastalar.values() if h.get("ad")})
            hasta_var = tk.StringVar(value=mevcut.get("hasta_ad", ""))
            ctk.CTkComboBox(dlg, values=isimler, variable=hasta_var,
                             width=320, height=32, border_color=TH["accent_soft"]
                             ).pack(padx=18)

            ctk.CTkLabel(dlg, text=t("TARİX (gg.aa.yyyy)"), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(12,2))
            varsayilan_iso = mevcut.get("tarix") or gun_iso or date.today().isoformat()
            tarih_var = tk.StringVar(value=datetime.strptime(varsayilan_iso, "%Y-%m-%d").strftime("%d.%m.%Y"))
            ctk.CTkEntry(dlg, textvariable=tarih_var, width=320, height=32,
                         border_color=TH["accent_soft"]).pack(padx=18)

            ctk.CTkLabel(dlg, text=t("SAAT (ss:dd)"), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(12,2))
            saat_var = tk.StringVar(value=mevcut.get("saat") or on_saat or "09:00")
            ctk.CTkEntry(dlg, textvariable=saat_var, width=320, height=32,
                         border_color=TH["accent_soft"]).pack(padx=18)

            ctk.CTkLabel(dlg, text=t("NƏ QƏDƏR VAXT AYRILSIN?"), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(12,2))
            mevcut_muddet = int(mevcut.get("muddet", 30) or 30)
            muddet_var = tk.StringVar(value=f"{mevcut_muddet} {t('dəqiqə')}")
            ctk.CTkOptionMenu(dlg, values=[f"{m} {t('dəqiqə')}" for m in RANDEVU_MUDDETLER], variable=muddet_var,
                              width=320, height=32, corner_radius=8,
                              fg_color=TH["accent_soft"], text_color=TH["accent"],
                              button_color=TH["accent"], button_hover_color=TH["accent_hover"]
                              ).pack(padx=18)

            ctk.CTkLabel(dlg, text=t("STATUS"), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(12,2))
            status_var = tk.StringVar(value=t(RANDEVU_STATUS_LBL.get(mevcut.get("status", "gozleyir"))))
            ctk.CTkOptionMenu(dlg, values=[t(l) for _, l, _ in RANDEVU_STATUS], variable=status_var,
                              width=320, height=32, corner_radius=8,
                              fg_color=TH["accent_soft"], text_color=TH["accent"],
                              button_color=TH["accent"], button_hover_color=TH["accent_hover"]
                              ).pack(padx=18)

            ctk.CTkLabel(dlg, text=t("QEYD"), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(12,2))
            qeyd_box = ctk.CTkTextbox(dlg, width=320, height=60)
            qeyd_box.pack(padx=18)
            if mevcut.get("qeyd"):
                qeyd_box.insert("1.0", mevcut["qeyd"])

            hata_lbl = ctk.CTkLabel(dlg, text="", font=ctk.CTkFont(size=11), text_color="#C0392B")
            hata_lbl.pack(padx=18, pady=(6,0), anchor="w")

            def _kaydet_form():
                ad = hasta_var.get().strip()
                if not ad:
                    hata_lbl.configure(text=t("Hasta adı boş ola bilməz.")); return
                try:
                    iso = datetime.strptime(tarih_var.get().strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
                except ValueError:
                    hata_lbl.configure(text=t("Tarix formatı yanlışdır (gg.aa.yyyy).")); return
                saat = saat_var.get().strip() or "09:00"
                try:
                    muddet = int(muddet_var.get().split()[0])
                except Exception:
                    muddet = 30
                secilen_status = next((k for k, l, _ in RANDEVU_STATUS if t(l) == status_var.get()), "gozleyir")
                eslesen_hid = next((hid for hid, h in self.hastalar.items() if h.get("ad", "") == ad), None)
                rid2 = rid or yeni_id("randevu")
                self.randevular[rid2] = {
                    "hasta_id": eslesen_hid,
                    "hasta_ad": ad,
                    "tarix": iso,
                    "saat": saat,
                    "muddet": muddet,
                    "qeyd": qeyd_box.get("1.0", "end").strip(),
                    "status": secilen_status,
                }
                randevu_kaydet(self.randevular)
                state["hafte"] = hafte_bas(iso)
                dlg.destroy()
                _ciz()

            ctk.CTkButton(dlg, text=t("💾 Saxla"), width=140, height=34, corner_radius=10,
                          fg_color=TH["accent"], hover_color=TH["accent_hover"],
                          font=ctk.CTkFont(size=12, weight="bold"),
                          command=_kaydet_form).pack(pady=(16,6))
            if rid:
                ctk.CTkButton(dlg, text=t("🗑 Randevunu Sil"), width=160, height=30, corner_radius=8,
                              fg_color="transparent", border_width=1, border_color="#C0392B",
                              text_color="#C0392B", hover_color=TH["sidebar_row"],
                              command=lambda: (dlg.destroy(), _sil(rid))
                              ).pack()

        _ciz()

    def _yeni_hasta(self):
        d=ctk.CTkInputDialog(text=t("Pasient adı:"),title=t("Yeni Pasient"))
        ad=d.get_input()
        if ad:
            hid = yeni_id("hasta")
            vid = yeni_id("vaka")
            self.hastalar[hid] = {
                "ad": ad,
                "vakalar": {
                    vid: {
                        "ad": f"1. {t('Vaka')}",
                        "tarih": datetime.now().strftime("%d.%m.%Y"),
                        "plan": {}, "tedavi": {}
                    }
                }
            }
            veri_kaydet(self.hastalar)
            self._auto_bxd_backup(hid)
            self.expanded_hids.add(hid)
            self._lista_guncelle()
            self.after(100, lambda: self._yukle(hid=hid, vid=vid))

    def _hasta_kaydet_hizli(self, hid):
        """Sağ tık → Hastayı Kaydet: yerel HastaYedekleri qovluğuna dərhal
        yazır + bulud girişi varsa BÜTÜN hastaları arxa planda buluda göndərir."""
        import tkinter.messagebox
        h = self.hastalar.get(hid)
        if not h: return
        ad = h.get("ad", "Hasta")
        veri_kaydet(self.hastalar)
        self._auto_bxd_backup(hid)
        if cloud_configured() and AYARLAR.get("sb_access_token"):
            snap = dict(self.hastalar)
            threading.Thread(target=self._cloud_auto_sync_worker, args=(snap,), daemon=True).start()
            tkinter.messagebox.showinfo(
                t("Kaydedildi"),
                f"'{ad}' yedəkləndi və bütün hastalar buludla sinxronlaşdırılır…")
        else:
            tkinter.messagebox.showinfo(
                t("Kaydedildi"), f"'{ad}' yedəkləndi:\n{self._BXD_BASE}")

    def _hasta_yukle(self):
        """Import a .bxd patient file and add them to the current database."""
        import tkinter.filedialog, tkinter.messagebox
        dosya = tkinter.filedialog.askopenfilename(
            title="Hasta Projesi Yükle",
            filetypes=[("BlueX Dental Projesi", "*.bxd"), ("Tüm Dosyalar", "*.*")],
        )
        if not dosya: return
        try:
            with open(dosya, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            tkinter.messagebox.showerror(t("Yükleme Hatası"), f"Dosya okunamadı:\n{exc}")
            return
        if payload.get("format") != "bxd" or "hasta" not in payload:
            tkinter.messagebox.showerror(t("Geçersiz Dosya"),
                                         t("Bu geçerli bir .bxd hasta dosyası değil."))
            return
        h = payload["hasta"]
        new_hid = yeni_id("hasta")
        while new_hid in self.hastalar:   # ehtimal cüzidir, yenə də qorunaq
            new_hid = yeni_id("hasta")
        self.hastalar[new_hid] = h
        veri_kaydet(self.hastalar)
        self.expanded_hids.add(new_hid)
        self._lista_guncelle()
        self.after(100, lambda: self._yukle(hid=new_hid))
        tkinter.messagebox.showinfo(t("Yüklendi"),
                                    f"'{h.get('ad', '')}' hastası başarıyla yüklendi.")

    # ── Statistika ────────────────────────────────────────────────────────────
    def _statistika_hesabla(self, cinsiyet_filtr="hamisi", sinif_filtrler=None,
                             tarix_bas=None, tarix_son=None, yas_bas=None, yas_son=None):
        """Filtrlərə uyğun pasient/vaka alt-çoxluğu üzrə statistik məlumatları toplayır.

        cinsiyet_filtr: "hamisi" | "kisi" | "qadin"
        sinif_filtrler: seçili siniflərin açarlarından ibarət collection — "I"/"II_III"/"V"
                        bir neçəsi eyni anda seçilə bilər (SINIF_YUZEYLER-ə görə birləşir);
                        boş/None => bütün boşluq növləri.
        tarix_bas/tarix_son: date obyekti və ya None (limitsiz) — vaka tarixinə görə.
        yas_bas/yas_son: int və ya None (limitsiz) — pasient yaşına görə.
        """
        surf_all    = Counter()
        surf_city   = {}
        surf_gender = {"kisi": Counter(), "qadin": Counter()}
        cekim_tooth  = Counter()
        cekim_gender = {"kisi": Counter(), "qadin": Counter()}
        patient_gender = Counter()
        patient_city   = Counter()
        dahil_cins     = Counter()   # hesablamaya daxil olan pasientlərin cins bölgüsü
        dahil_pasient  = 0
        zaman_serisi   = Counter()   # {vaka tarixi: seçili sinifə uyğun çürük sayı} — zaman qrafiki üçün

        sinif_yuzeyler = None   # None => bütün boşluq növləri
        if sinif_filtrler:
            sinif_yuzeyler = set()
            for sf in sinif_filtrler:
                sinif_yuzeyler.update(SINIF_YUZEYLER.get(sf, []))

        for h in self.hastalar.values():
            cins = h.get("cinsiyet", "") or ""
            if cinsiyet_filtr != "hamisi" and cins != cinsiyet_filtr:
                continue
            try:
                yas = int(h.get("yas") or 0) or None
            except (TypeError, ValueError):
                yas = None
            if yas_bas is not None and (yas is None or yas < yas_bas):
                continue
            if yas_son is not None and (yas is None or yas > yas_son):
                continue
            sheher = (h.get("sheher", "") or "").strip()

            pasient_daxil = False
            for vaka in h.get("vakalar", {}).values():
                try:
                    vtarix = datetime.strptime(vaka.get("tarih", ""), "%d.%m.%Y").date()
                except (ValueError, TypeError):
                    vtarix = None
                if tarix_bas is not None and (vtarix is None or vtarix < tarix_bas):
                    continue
                if tarix_son is not None and (vtarix is None or vtarix > tarix_son):
                    continue
                pasient_daxil = True
                for n_str, durum in vaka.get("ilkin", {}).items():
                    ns = normalize_state(durum)
                    for y in ns["curuk"]:
                        if sinif_yuzeyler is not None and y not in sinif_yuzeyler:
                            continue
                        surf_all[y] += 1
                        if sheher:
                            surf_city.setdefault(sheher, Counter())[y] += 1
                        if cins in surf_gender:
                            surf_gender[cins][y] += 1
                        if vtarix is not None:
                            zaman_serisi[vtarix] += 1
                for n_str, durum in vaka.get("tedavi", {}).items():
                    ns = normalize_state(durum)
                    if ns["tac"] == "cekim":
                        try: n = int(n_str)
                        except (TypeError, ValueError): continue
                        cekim_tooth[n] += 1
                        if cins in cekim_gender:
                            cekim_gender[cins][n] += 1

            if pasient_daxil:
                dahil_pasient += 1
                dahil_cins[cins or "bilinmir"] += 1
                patient_gender[cins or "bilinmir"] += 1
                if sheher:
                    patient_city[sheher] += 1
        return {
            "surf_all": surf_all, "surf_city": surf_city, "surf_gender": surf_gender,
            "cekim_tooth": cekim_tooth, "cekim_gender": cekim_gender,
            "patient_gender": patient_gender, "patient_city": patient_city,
            "dahil_pasient": dahil_pasient, "dahil_cins": dahil_cins,
            "zaman_serisi": zaman_serisi,
        }

    def _statistika_ac(self):
        import tkinter.messagebox as stat_mb
        if self._statistika_win is not None and self._statistika_win.winfo_exists():
            self._pencereni_one_cek(self._statistika_win)
            return
        win = ctk.CTkToplevel(self, fg_color=TH["bg"])
        self._statistika_win = win
        win.title(t("Statistika"))
        win.geometry("980x860")
        win.after(50, lambda: win.state("zoomed"))
        self._pencereni_one_cek(win)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        # ── Filtr paneli (sabit, scroll olunmayan) ──
        filtr_f = ctk.CTkFrame(win, fg_color=TH["panel"], corner_radius=12)
        filtr_f.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))

        def _filtr_etiket(parent, text):
            ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["txt_sub"]).pack(side="left", padx=(12, 4), pady=10)

        _filtr_etiket(filtr_f, t("Cins:"))
        cins_var = tk.StringVar(value=t("Hamısı"))
        ctk.CTkOptionMenu(filtr_f, variable=cins_var,
                          values=[t(l) for _, l in STAT_CINSIYET_SECENEKLERI],
                          height=30, corner_radius=8, width=110,
                          fg_color=TH["panel_soft"], button_color=TH["accent"],
                          button_hover_color=TH["accent_hover"]).pack(side="left", pady=10)

        _filtr_etiket(filtr_f, t("Sinif:"))
        sinif_vars = {}   # key -> BooleanVar; heç biri seçilməyibsə = Hamısı (I/II-III/V eyni anda seçilə bilər)
        for key, lbl in SINIF_SECENEKLERI:
            var = tk.BooleanVar(value=False)
            sinif_vars[key] = var
            ctk.CTkCheckBox(filtr_f, text=t(lbl), variable=var,
                             checkbox_width=18, checkbox_height=18,
                             font=ctk.CTkFont(size=11),
                             fg_color=TH["accent"], hover_color=TH["accent_hover"],
                             border_color=TH["accent_soft"],
                             text_color=TH["txt"]).pack(side="left", padx=(2, 10), pady=10)

        _filtr_etiket(filtr_f, t("Tarixdən:"))
        tarix_bas_e = ctk.CTkEntry(filtr_f, placeholder_text="gg.aa.illll", height=30,
                                    width=100, corner_radius=8,
                                    border_color=TH["accent_soft"], fg_color=TH["panel_soft"])
        tarix_bas_e.pack(side="left", pady=10)
        _filtr_etiket(filtr_f, t("Tarixə:"))
        tarix_son_e = ctk.CTkEntry(filtr_f, placeholder_text="gg.aa.illll", height=30,
                                    width=100, corner_radius=8,
                                    border_color=TH["accent_soft"], fg_color=TH["panel_soft"])
        tarix_son_e.pack(side="left", pady=10)

        _filtr_etiket(filtr_f, t("Yaşdan:"))
        yas_bas_e = ctk.CTkEntry(filtr_f, height=30, width=60, corner_radius=8,
                                  border_color=TH["accent_soft"], fg_color=TH["panel_soft"])
        yas_bas_e.pack(side="left", pady=10)
        _filtr_etiket(filtr_f, t("Yaşa:"))
        yas_son_e = ctk.CTkEntry(filtr_f, height=30, width=60, corner_radius=8,
                                  border_color=TH["accent_soft"], fg_color=TH["panel_soft"])
        yas_son_e.pack(side="left", pady=10)

        hesabla_btn = ctk.CTkButton(filtr_f, text=t("Hesabla"), height=30, corner_radius=8,
                                     fg_color=TH["ok"], hover_color=TH["ok_hover"],
                                     font=ctk.CTkFont(size=12, weight="bold"))
        hesabla_btn.pack(side="left", padx=12, pady=10)

        scroll = ctk.CTkScrollableFrame(win, fg_color=TH["bg"])
        scroll.grid(row=1, column=0, sticky="nsew", padx=10, pady=(4, 10))

        def _basliq(text):
            ctk.CTkLabel(scroll, text=text, font=ctk.CTkFont(size=16, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=4, pady=(18, 6))

        def _kart():
            f = ctk.CTkFrame(scroll, fg_color=TH["panel"], corner_radius=12)
            f.pack(fill="x", padx=4, pady=4)
            return f

        def _img_goster(parent, pil_img, bosmsg, pad=(16, 12)):
            if pil_img is None:
                ctk.CTkLabel(parent, text=bosmsg, font=ctk.CTkFont(size=12),
                             text_color=TH["txt_faint"]).pack(padx=16, pady=16, anchor="w")
                return
            photo = ImageTk.PhotoImage(pil_img)
            lbl = tk.Label(parent, image=photo, bg=TH["panel"], borderwidth=0)
            lbl.image = photo
            lbl.pack(padx=pad[0], pady=pad[1], anchor="w")

        def _yuz_lbl(y):
            return f"{y} ({YUZEY_AD.get(y, y)})"

        def _oxu_filtrler():
            cinsiyet_filtr = next((k for k, l in STAT_CINSIYET_SECENEKLERI
                                    if t(l) == cins_var.get()), "hamisi")
            sinif_filtrler = [k for k, v in sinif_vars.items() if v.get()]

            def _tarix(entry):
                s = entry.get().strip()
                if not s: return None, False
                try: return datetime.strptime(s, "%d.%m.%Y").date(), False
                except ValueError: return None, True

            def _yas(entry):
                s = entry.get().strip()
                if not s: return None, False
                try: return int(s), False
                except ValueError: return None, True

            tarix_bas, err1 = _tarix(tarix_bas_e)
            tarix_son, err2 = _tarix(tarix_son_e)
            if err1 or err2:
                stat_mb.showerror(t("Xəta"), t("Tarix formatı: gg.aa.illll (məs: 01.01.2026)"))
                return None
            yas_bas, err3 = _yas(yas_bas_e)
            yas_son, err4 = _yas(yas_son_e)
            if err3 or err4:
                stat_mb.showerror(t("Xəta"), t("Yaş rəqəm olmalıdır."))
                return None
            return dict(cinsiyet_filtr=cinsiyet_filtr, sinif_filtrler=sinif_filtrler,
                        tarix_bas=tarix_bas, tarix_son=tarix_son,
                        yas_bas=yas_bas, yas_son=yas_son)

        def _render():
            filt = _oxu_filtrler()
            if filt is None:
                return
            for w in scroll.winfo_children():
                w.destroy()
            stat = self._statistika_hesabla(**filt)

            # ── Ümumi məlumat ──
            _basliq(t("📊 Ümumi Məlumat"))
            umumi = _kart()
            dahil_n  = stat["dahil_pasient"]
            kisi_n   = stat["dahil_cins"].get("kisi", 0)
            qadin_n  = stat["dahil_cins"].get("qadin", 0)
            bilinmir_n = stat["dahil_cins"].get("bilinmir", 0)
            tiles = ctk.CTkFrame(umumi, fg_color="transparent")
            tiles.pack(fill="x", padx=12, pady=(12, 4))
            for lbl, val, color in [
                (t("Hesablamaya Daxil Pasient"), dahil_n, TH["accent"]),
                (t("Kişi"), kisi_n, "#%02x%02x%02x" % STAT_RENG_KISI),
                (t("Qadın"), qadin_n, "#%02x%02x%02x" % STAT_RENG_QADIN),
                (t("Cinsiyəti Qeyd Olunmayan"), bilinmir_n, TH["txt_faint"]),
            ]:
                tile = ctk.CTkFrame(tiles, fg_color=TH["panel_soft"], corner_radius=10,
                                     width=190, height=72)
                tile.pack(side="left", padx=6)
                tile.pack_propagate(False)
                ctk.CTkLabel(tile, text=str(val), font=ctk.CTkFont(size=22, weight="bold"),
                             text_color=color).pack(pady=(12, 0))
                ctk.CTkLabel(tile, text=lbl, font=ctk.CTkFont(size=10),
                             text_color=TH["txt_sub"]).pack()
            ctk.CTkLabel(umumi, text=f"({t('Bazada ümumi')} {len(self.hastalar)} {t('pasient qeydiyyatdadır')})",
                         font=ctk.CTkFont(size=11), text_color=TH["txt_faint"]).pack(
                         anchor="w", padx=16, pady=(0, 12))
            if dahil_n == 0:
                ctk.CTkLabel(umumi, text=t("Filtrə uyğun pasient tapılmadı."),
                             font=ctk.CTkFont(size=12, weight="bold"),
                             text_color=TH["txt_faint"]).pack(anchor="w", padx=16, pady=(0, 12))

            # ── Çürük analizi (boşluq yerləşməsi, seçili sinifə görə) ──
            _basliq(t("🦷 Çürük Analizi — Boşluq Yerləşməsi"))
            curuk_f = _kart()
            toplam_curuk = sum(stat["surf_all"].values())
            items = sorted(stat["surf_all"].items(), key=lambda x: -x[1])
            items = [(f"{_yuz_lbl(y)} — {round(100*v/toplam_curuk) if toplam_curuk else 0}%", v)
                     for y, v in items]
            img = _stat_bar_chart(items, STAT_RENG_CURUK, width=720)
            _img_goster(curuk_f, img, t("Çürük qeydi yoxdur."))

            # ── Zaman üzrə dinamika (yalnız tarix aralığı seçilibsə) ──
            if filt["tarix_bas"] is not None or filt["tarix_son"] is not None:
                _basliq(t("📈 Zaman Üzrə Dinamika"))
                zaman_f = _kart()
                zaman = stat["zaman_serisi"]
                if not zaman:
                    _img_goster(zaman_f, None, t("Bu tarix aralığı üçün məlumat yoxdur."))
                else:
                    tarixler = sorted(zaman.keys())
                    span = (tarixler[-1] - tarixler[0]).days
                    if span <= 31:
                        bucket_key = lambda dt: dt
                        bucket_lbl = lambda k: k.strftime("%d.%m")
                    elif span <= 180:
                        bucket_key = lambda dt: dt - timedelta(days=dt.weekday())
                        bucket_lbl = lambda k: k.strftime("%d.%m")
                    else:
                        bucket_key = lambda dt: date(dt.year, dt.month, 1)
                        bucket_lbl = lambda k: k.strftime("%m.%Y")
                    bucketed = Counter()
                    for dt, cnt in zaman.items():
                        bucketed[bucket_key(dt)] += cnt
                    points = [(bucket_lbl(k), bucketed[k]) for k in sorted(bucketed.keys())]
                    imgz = _stat_line_chart(points, STAT_RENG_CURUK, width=720)
                    _img_goster(zaman_f, imgz, t("Bu tarix aralığı üçün məlumat yoxdur."))

            # ── Diş çəkimi statistikası ──
            _basliq(t("🦴 Diş Çəkimi Statistikası"))
            cekim_f = _kart()
            top_cekim = sorted(stat["cekim_tooth"].items(), key=lambda x: -x[1])[:14]
            items = [(str(n), v) for n, v in top_cekim]
            img = _stat_bar_chart(items, STAT_RENG_CEKIM, width=720)
            _img_goster(cekim_f, img, t("Çəkim qeydi yoxdur."))

            # ── Şəhərə görə statistika ──
            _basliq(t("📍 Şəhərə Görə Statistika"))
            sheher_f = _kart()
            sheherler = sorted(stat["surf_city"].keys())
            if not sheherler:
                ctk.CTkLabel(sheher_f, text=t("Şəhər məlumatı qeyd olunmayıb."),
                             font=ctk.CTkFont(size=12),
                             text_color=TH["txt_faint"]).pack(padx=16, pady=16, anchor="w")
            else:
                sec_f = ctk.CTkFrame(sheher_f, fg_color="transparent")
                sec_f.pack(fill="x", padx=12, pady=(12, 0), anchor="w")
                ctk.CTkLabel(sec_f, text=t("Şəhər:"), font=ctk.CTkFont(size=12, weight="bold"),
                             text_color=TH["txt_sub"]).pack(side="left", padx=(0, 8))
                sheher_var = tk.StringVar(value=sheherler[0])
                chart_holder = ctk.CTkFrame(sheher_f, fg_color="transparent")
                chart_holder.pack(fill="x", padx=4, pady=8, anchor="w")

                def _sheher_render(*_):
                    for w in chart_holder.winfo_children(): w.destroy()
                    sh = sheher_var.get()
                    cnt = stat["surf_city"].get(sh, Counter())
                    toplam_sh = sum(cnt.values()) or 1
                    n_pasient = stat["patient_city"].get(sh, 0)
                    ctk.CTkLabel(chart_holder, text=f"{sh} — {n_pasient} {t('pasient')}",
                                 font=ctk.CTkFont(size=12, weight="bold"),
                                 text_color=TH["txt"]).pack(anchor="w", pady=(0, 6))
                    sh_items = sorted(cnt.items(), key=lambda x: -x[1])
                    sh_items = [(f"{_yuz_lbl(y)} — {round(100*v/toplam_sh)}%", v) for y, v in sh_items]
                    img2 = _stat_bar_chart(sh_items, STAT_RENG_CURUK, width=680, left_pad=210)
                    _img_goster(chart_holder, img2, t("Bu şəhər üçün çürük qeydi yoxdur."), pad=(0, 0))

                ctk.CTkOptionMenu(sec_f, values=sheherler, variable=sheher_var,
                                  command=lambda _v: _sheher_render(),
                                  height=30, corner_radius=8,
                                  fg_color=TH["panel_soft"], button_color=TH["accent"],
                                  button_hover_color=TH["accent_hover"]).pack(side="left")
                _sheher_render()

            # ── Cinsiyətə görə statistika ──
            _basliq(t("⚥ Cinsiyətə Görə Statistika"))
            cins_f = _kart()
            legend = _stat_legend_img([(t("Kişi"), STAT_RENG_KISI), (t("Qadın"), STAT_RENG_QADIN)])
            legend_photo = ImageTk.PhotoImage(legend)
            legend_lbl = tk.Label(cins_f, image=legend_photo, bg=TH["panel"], borderwidth=0)
            legend_lbl.image = legend_photo
            legend_lbl.pack(padx=16, pady=(14, 4), anchor="w")

            all_surf = sorted(set(stat["surf_gender"]["kisi"]) | set(stat["surf_gender"]["qadin"]),
                               key=lambda y: -(stat["surf_gender"]["kisi"].get(y, 0)
                                               + stat["surf_gender"]["qadin"].get(y, 0)))
            if all_surf:
                cats = [_yuz_lbl(y) for y in all_surf]
                series = [(t("Kişi"), STAT_RENG_KISI,
                           {_yuz_lbl(y): stat["surf_gender"]["kisi"].get(y, 0) for y in all_surf}),
                          (t("Qadın"), STAT_RENG_QADIN,
                           {_yuz_lbl(y): stat["surf_gender"]["qadin"].get(y, 0) for y in all_surf})]
                img3 = _stat_grouped_chart(cats, series, width=720)
                _img_goster(cins_f, img3, t("Çürük qeydi yoxdur."))
            else:
                _img_goster(cins_f, None, t("Çürük qeydi yoxdur."))

            ctk.CTkLabel(cins_f, text=t("Çəkim sayı"), font=ctk.CTkFont(size=12, weight="bold"),
                         text_color=TH["txt_sub"]).pack(anchor="w", padx=16, pady=(4, 0))
            cekim_items = [(t("Kişi"), sum(stat["cekim_gender"]["kisi"].values())),
                           (t("Qadın"), sum(stat["cekim_gender"]["qadin"].values()))]
            img4 = _stat_bar_chart(cekim_items, STAT_RENG_CEKIM, width=720, bar_h=28)
            _img_goster(cins_f, img4, t("Çəkim qeydi yoxdur."))

        hesabla_btn.configure(command=_render)
        _render()

    # ── X-ray folder watcher methods ─────────────────────────────────────────

    def _start_xray_watcher(self):
        """Start watchdog observer in a daemon background thread."""
        if not _WATCHDOG_OK:
            print("[XRay] watchdog not installed — folder watcher disabled.\n"
                  "       Run: pip install watchdog")
            return
        wdir = xray_watch_dir()
        if not os.path.isdir(wdir):
            # klasör (henüz) yok — pes etme, 30 sn'de bir yeniden dene
            print(f"[XRay] Watch directory not found: {wdir} — retrying in 30s")
            self._xray_retry_id = self.after(30000, self._start_xray_watcher)
            return
        try:
            if wdir.startswith("\\\\"):
                # ağ paylaşımlarında dosya-sistemi event'leri gelmez — polling kullan
                from watchdog.observers.polling import PollingObserver as _Obs
            else:
                _Obs = _WatchdogObserver
            observer = _Obs()
            observer.schedule(_XRayFileHandler(self._xray_q, wdir, xray_subdir_only()),
                              wdir, recursive=True)
            observer.daemon = True
            observer.start()
            self._xray_observer = observer
            print(f"[XRay] Watching: {wdir}")
        except Exception as e:
            print(f"[XRay] Could not start watcher: {e}")

    def _restart_xray_watcher(self):
        if getattr(self, "_xray_retry_id", None):
            try: self.after_cancel(self._xray_retry_id)
            except Exception: pass
            self._xray_retry_id = None
        obs = getattr(self, "_xray_observer", None)
        if obs:
            try: obs.stop()
            except Exception: pass
            self._xray_observer = None
        self._start_xray_watcher()

    def _check_xray_queue(self):
        """Poll for new X-ray paths from the background thread (main-thread only)."""
        try:
            while not self._xray_q.empty():
                filepath = self._xray_q.get_nowait()
                if not self._pending_xray_dialog:
                    self._pending_xray_dialog = True
                    session = (self.aktif_hid, self.aktif_vid) if self.aktif_hid and self.aktif_vid else None
                    if session and session == self._xray_confirmed_session:
                        # Same patient+case already confirmed — auto-add silently
                        self.after(2000, lambda p=filepath: self._xray_auto_add(p))
                    else:
                        # Ask the doctor
                        self.after(2000, lambda p=filepath: self._show_xray_confirm(p))
        except Exception as e:
            print(f"[XRay] Queue poll error: {e}")
        finally:
            self.after(1500, self._check_xray_queue)

    def _show_xray_confirm(self, filepath: str):
        """Ask once per patient+case; shows who will receive the X-ray."""
        self._pending_xray_dialog = False
        if not os.path.exists(filepath):
            return

        # If no patient/case is open, silently discard (nothing to assign to)
        if not self.aktif_hid or not self.aktif_vid:
            return

        hid = self.aktif_hid
        vid = self.aktif_vid
        h   = self.hastalar.get(hid, {})
        v   = h.get("vakalar", {}).get(vid, {})
        hasta_ad = h.get("ad", "İsimsiz")
        vaka_ad  = v.get("ad", "")

        try:
            ftime = datetime.fromtimestamp(
                os.path.getmtime(filepath)).strftime("%d.%m.%Y %H:%M:%S")
        except Exception:
            ftime = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

        fname = os.path.basename(filepath)

        dlg = ctk.CTkToplevel(self)
        dlg.title(t("Yeni Rentgen Aşkarlandı"))
        dlg.geometry("480x230")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()

        ctk.CTkLabel(dlg,
                     text=t("🦷  Yeni Rentgen Aşkarlandı"),
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=TH["accent"]).pack(pady=(18, 2))
        ctk.CTkLabel(dlg,
                     text=f"{fname}  ·  {ftime}",
                     font=ctk.CTkFont(size=11),
                     text_color="#666").pack()
        ctk.CTkLabel(dlg,
                     text=f"Eklenecek:  {hasta_ad}  —  {vaka_ad}",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#1B5E20").pack(pady=(14, 4))

        def on_yes():
            self._xray_confirmed_session = (hid, vid)
            dlg.destroy()
            self._xray_ekle_from_file(filepath, hid)

        def on_no():
            dlg.destroy()

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=14)
        ctk.CTkButton(btn_row,
                      text=t("✓  Evet, Ekle"),
                      width=170, height=36,
                      fg_color="#1565C0", hover_color="#0D47A1",
                      font=ctk.CTkFont(weight="bold"),
                      command=on_yes).pack(side="left", padx=8)
        ctk.CTkButton(btn_row,
                      text=t("✗  Hayır, İptal"),
                      width=170, height=36,
                      fg_color="#888", hover_color="#555",
                      command=on_no).pack(side="left", padx=8)

        dlg.update_idletasks()
        px = self.winfo_x() + (self.winfo_width()  - dlg.winfo_width())  // 2
        py = self.winfo_y() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{px}+{py}")

    def _xray_auto_add(self, filepath: str):
        """Silently add X-ray to the current session patient without a dialog."""
        self._pending_xray_dialog = False
        if not os.path.exists(filepath) or not self.aktif_hid:
            return
        self._xray_ekle_from_file(filepath, self.aktif_hid)

    def _xray_ekle_from_file(self, filepath: str, hid: str):
        """Encode the .bmp, append to patient rontgenler, refresh UI, push to VR."""
        try:
            with open(filepath, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
            h = self.hastalar[hid]
            h.setdefault("rontgenler", []).append({
                "tarih": timestamp,
                "dosya": os.path.basename(filepath),
                "data":  b64,
            })
            veri_kaydet(self.hastalar)
            self._auto_bxd_backup(hid)
            # Refresh X-ray tab if the confirmed patient is currently open
            if hid == self.aktif_hid:
                self._rontgen_guncelle()
                try:
                    self.tab.set(t("Rentgen"))
                except Exception:
                    pass
            # Newly added röntgen becomes the current one shown on VR
            new_idx = len(self.hastalar[hid]["rontgenler"]) - 1
            self._vr_xray_idx[hid] = new_idx
            self._push_vr_xray_by_index(hid, new_idx)
            if hid == self.aktif_hid:
                self._xray_vr_nav_label_guncelle()
        except Exception as e:
            tk.messagebox.showerror(t("Xəta"), f"Rentgen əlavə edilmədi:\n{e}")

    def _push_vr_xray_by_index(self, hid: str, idx: int | None):
        """Decode rontgenler[idx]["data"] and push it straight into the embedded HTTP
        server's in-memory cache (Quest XRay Viewer app fetches it immediately, no
        polling delay) + XRAY_VR_CURRENT_JPG/XRAY_PUSH_JSON on disk for debugging.
        Reads the embedded base64 (always present) instead of the original scanned
        file path (which may no longer exist / was never recorded for manually-added
        röntgenler)."""
        global _XRAY_HTTP_JPEG, _XRAY_HTTP_META
        try:
            os.makedirs(os.path.dirname(XRAY_PUSH_JSON), exist_ok=True)
            h = self.hastalar.get(hid, {})
            rontgenler = h.get("rontgenler", [])
            if idx is None or not (0 <= idx < len(rontgenler)):
                # No röntgen for this patient — clear so the VR app doesn't keep
                # showing a previous, wrong patient's image.
                with _XRAY_HTTP_LOCK:
                    _XRAY_HTTP_JPEG = None
                    _XRAY_HTTP_META = {"patient_name": h.get("ad", ""), "timestamp": ""}
                payload = {"xray_path": "", "patient_name": h.get("ad", ""),
                           "timestamp": "", "done": False}
            else:
                r = rontgenler[idx]
                img = Image.open(BytesIO(self._img_bytes(r))).convert("RGB")
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=92)
                jpeg_bytes = buf.getvalue()
                img.save(XRAY_VR_CURRENT_JPG, format="JPEG", quality=92)
                timestamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                with _XRAY_HTTP_LOCK:
                    _XRAY_HTTP_JPEG = jpeg_bytes
                    _XRAY_HTTP_META = {"patient_name": h.get("ad", ""), "timestamp": timestamp}
                payload = {
                    "xray_path":    XRAY_VR_CURRENT_JPG.replace("\\", "/"),
                    "patient_name": h.get("ad", ""),
                    "timestamp":    timestamp,
                    "xray_index":   idx,
                    "xray_total":   len(rontgenler),
                    "done":         False,
                }
            with open(XRAY_PUSH_JSON, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[XRay] push_vr_xray_by_index error: {e}")

    def _xray_vr_nav(self, delta: int):
        """◀/▶ buttons: step through the active patient's röntgenler and push the new one."""
        if not self.aktif_hid:
            return
        h = self.hastalar[self.aktif_hid]
        n = len(h.get("rontgenler", []))
        if n == 0:
            return
        cur = self._vr_xray_idx.get(self.aktif_hid, n - 1)
        new_idx = max(0, min(n - 1, cur + delta))
        self._vr_xray_idx[self.aktif_hid] = new_idx
        self._push_vr_xray_by_index(self.aktif_hid, new_idx)
        self._xray_vr_nav_label_guncelle()

    def _xray_vr_nav_label_guncelle(self):
        if not hasattr(self, "xray_vr_nav_lbl"):
            return
        if not self.aktif_hid:
            self.xray_vr_nav_lbl.configure(text="")
            return
        h = self.hastalar[self.aktif_hid]
        n = len(h.get("rontgenler", []))
        if n == 0:
            self.xray_vr_nav_lbl.configure(text=t("Henüz röntgen eklenmedi"))
            return
        idx = self._vr_xray_idx.get(self.aktif_hid, n - 1)
        self.xray_vr_nav_lbl.configure(text=f"VR: {idx + 1} / {n}")

    # ── VR Layout Editor ──────────────────────────────────────────────────────

    def _vr_duzenle(self):
        """Write editor mode flag + placeholder data, then try to launch UE5."""
        import subprocess
        import tkinter.messagebox as mb

        os.makedirs(r"C:\BlueX", exist_ok=True)

        with open(r"C:\BlueX\vr_editor_mode.json", "w", encoding="utf-8") as f:
            json.dump({"editor_mode": True}, f)

        sample_patient = {
            "patient_name":   "Test Hasta",
            "age":            35,
            "treatment_plan": "[VR Layout Editörü] — Test modudur.",
            "xray_image_path": r"C:\BlueX\ue_editor_screenshot.png",
            "patient_id":     "VR-EDITOR-TEST",
            "timestamp":      "2026-06-23T00:00:00",
        }
        with open(r"C:\BlueX\current_patient.json", "w", encoding="utf-8") as f:
            json.dump(sample_patient, f, ensure_ascii=False, indent=2)

        sample_xray = r"C:\BlueX\ue_editor_screenshot.png"
        if os.path.exists(sample_xray):
            with open(r"C:\BlueX\xray_push.json", "w", encoding="utf-8") as f:
                json.dump({"xray_path": sample_xray,
                           "patient_name": "Test Hasta", "done": False}, f)

        # Launch packaged EXE (or focus if already running)
        self._launch_or_focus_ue5()

        if os.path.exists(self._UE5_EXE):
            mb.showinfo(
                t("VR Layout Editörü"),
                "Paketlenmiş uygulama başlatılıyor...\n\n"
                "1. Uygulama yüklenince 'VR Düzenle' modu aktif olur.\n"
                "2. Üç paneli istediğiniz konuma taşıyın.\n"
                "3. Hazır olunca 'Layout Kaydet (VR)' butonuna basın.\n\n"
                f"({self._UE5_EXE})"
            )
        else:
            mb.showwarning(
                t("VR Layout Editörü"),
                "Paketlenmiş uygulama bulunamadı:\n\n"
                f"{self._UE5_EXE}\n\n"
                "Lütfen önce projeyi paketleyin."
            )

    def _vr_layout_kaydet(self):
        """Call VibeUE MCP API to run vr_save_layout.py in UE5."""
        import urllib.request
        import urllib.error
        import tkinter.messagebox as mb

        code = ("import unreal; "
                "exec(open('C:/BlueX/vr_save_layout.py', encoding='utf-8').read())")
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method":  "tools/call",
            "params":  {
                "name":      "execute_python_code",
                "arguments": {"code": code},
            },
            "id": 1,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8088/mcp",
                data=payload,
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": "Bearer bluex2025",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            content = result.get("result", {}).get("content", [])
            text    = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict))

            mb.showinfo(
                t("Layout Kaydedildi"),
                "VR layout başarıyla kaydedildi!\n"
                "C:\\BlueX\\vr_loadout.json güncellendi.\n\n"
                f"{text[:300] or '(UE5 yanıtı alındı)'}"
            )

        except urllib.error.URLError:
            mb.showerror(
                t("Bağlantı Hatası"),
                t("UE5 VibeUE sunucusuna bağlanılamadı.\n\n"
                "UE5'in açık ve PIE modunda olduğunu kontrol edin.\n\n"
                "Manuel olarak UE5 Output Log'unda çalıştırın:\n"
                "py C:/BlueX/vr_save_layout.py")
            )
        except Exception as e:
            mb.showerror(
                t("Hata"),
                f"Layout kaydedilemedi:\n{e}\n\n"
                "Manuel olarak UE5 Output Log'unda çalıştırın:\n"
                "py C:/BlueX/vr_save_layout.py"
            )

    def _guncelleme_kontrol_et(self, sessiz=True):
        """Buluddaki en son sürümü kontrol eder. `sessiz=False` (Ayarlar'daki
        manuel buton) ise güncel/hata durumunda da kullanıcıya bilgi verir;
        sessiz (açılıştaki otomatik kontrol) yalnız yeni sürüm varsa konuşur."""
        def work():
            try:
                info = check_for_update()
                err = None
            except Exception as e:
                info, err = None, e
            def ui():
                if info:
                    self._guncelleme_dialog(info)
                elif not sessiz:
                    import tkinter.messagebox as mb
                    if err:
                        mb.showinfo(t("Güncelleme Kontrolü"),
                                     f"Güncelleme kontrol edilemedi:\n{err}")
                    else:
                        mb.showinfo(t("Güncelleme Kontrolü"),
                                     f"En güncel sürümü kullanıyorsunuz (v{APP_VERSION}).")
            self.after(0, ui)
        threading.Thread(target=work, daemon=True).start()

    def _guncelleme_dialog(self, info):
        yeni_surum = info.get("surum", "?")
        url = info.get("indirme_url", "")
        notlar = info.get("notlar") or ""
        dlg = ctk.CTkToplevel(self)
        dlg.title(t("Yeni Sürüm Var"))
        dlg.geometry("440x300")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        ctk.CTkLabel(dlg, text=t("🚀  Yeni Sürüm Mevcut"),
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=TH["accent"]).pack(pady=(18, 4))
        ctk.CTkLabel(dlg, text=f"Mevcut sürüm: v{APP_VERSION}   →   Yeni sürüm: v{yeni_surum}",
                     font=ctk.CTkFont(size=12), text_color=TH["txt"]).pack()
        if notlar:
            ctk.CTkLabel(dlg, text=notlar, font=ctk.CTkFont(size=11), text_color="#666",
                         justify="left", wraplength=380).pack(pady=(10, 0), padx=20)
        durum = ctk.CTkLabel(dlg, text="", font=ctk.CTkFont(size=11), text_color=TH["accent"])
        durum.pack(pady=(10, 4))
        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=10)

        def _sonra():
            dlg.destroy()

        def _indir():
            if not url:
                durum.configure(text="İndirme adresi tanımlı değil.", text_color="#C0392B")
                return
            indir_btn.configure(state="disabled")
            sonra_btn.configure(state="disabled")
            self._guncelleme_indir_kur(url, durum, dlg)

        indir_btn = ctk.CTkButton(btn_row, text=t("⬇  İndir ve Kur"), width=170, height=36,
                                   fg_color="#1565C0", hover_color="#0D47A1",
                                   font=ctk.CTkFont(weight="bold"), command=_indir)
        indir_btn.pack(side="left", padx=8)
        sonra_btn = ctk.CTkButton(btn_row, text=t("Daha Sonra"), width=140, height=36,
                                   fg_color="#888", hover_color="#555", command=_sonra)
        sonra_btn.pack(side="left", padx=8)

    def _guncelleme_indir_kur(self, url, durum_lbl, dlg):
        def work():
            import urllib.request, tempfile, subprocess
            try:
                tmp_path = os.path.join(tempfile.gettempdir(), "BlueXDental_Setup_update.exe")
                req = urllib.request.Request(url, headers={"User-Agent": "BlueXDental-Updater"})
                with urllib.request.urlopen(req, timeout=30, context=ssl_baglam()) as resp, open(tmp_path, "wb") as f:
                    total = resp.length or 0
                    okunan = 0
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk: break
                        f.write(chunk)
                        okunan += len(chunk)
                        if total:
                            yuzde = int(okunan * 100 / total)
                            self.after(0, lambda y=yuzde: durum_lbl.configure(
                                text=f"İndiriliyor… %{y}", text_color=TH["accent"]))
                def basar():
                    durum_lbl.configure(text="İndirildi, kurulum başlatılıyor…", text_color="#0E9F6E")
                    self.after(400, lambda: self._guncelleme_kur_baslat(tmp_path))
                self.after(0, basar)
            except Exception as e:
                err = e
                self.after(0, lambda: durum_lbl.configure(
                    text=f"İndirme başarısız: {err}", text_color="#C0392B"))
        threading.Thread(target=work, daemon=True).start()

    def _guncelleme_kur_baslat(self, setup_path):
        """İndirilen kurulum dosyasını başlatıp mevcut programı kapatır — Inno
        Setup, [Run] bölümündeki ayarla kurulum bitince yeni sürümü otomatik açar."""
        import subprocess
        try:
            subprocess.Popen([setup_path], close_fds=True)
        except Exception:
            pass
        self.after(300, self._on_close)

    # ── SƏSLİ ASİSTENT ──────────────────────────────────────────────────────
    def _sesli_status_configure(self, text, color=None):
        def ui():
            try:
                self.sesli_status_lbl.configure(text=text, text_color=color or "#7A99C4")
            except Exception:
                pass
        try:
            self.after(0, ui)
        except Exception:
            pass

    # ── SƏSLİ ASİSTENT AVATARI (yarı-həkim/yarı-robot, pəncərənin yuxarısında) ──
    def _sesli_avatar_yarat(self):
        """Çərçivəsiz, həmişə-üstdə kiçik pəncərə yaradır (əsas pəncərənin solunda,
        boş sahədə), siçanla sürüklənə bilir (mövqe yadda saxlanılır), və yalnız
        proqram ön planda olanda görünür (başqa pəncərələrdə görünməsin deyə).
        Artıq varsa yalnız göstərir (deiconify)."""
        win = getattr(self, "_avatar_win", None)
        if win and win.winfo_exists():
            try: win.deiconify()
            except Exception: pass
            self._sesli_avatar_hal("idle")
            return
        TRANSKEY = "#010203"
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=TRANSKEY)
        try:
            win.wm_attributes("-transparentcolor", TRANSKEY)
        except Exception:
            pass  # Windows-dan başqa platformada bu atribut yoxdur — pəncərə sadəcə normal fonlu qalır
        lbl = tk.Label(win, bg=TRANSKEY, bd=0, highlightthickness=0, cursor="fleur")
        lbl.pack()
        self._avatar_win = win
        self._avatar_lbl = lbl
        self._avatar_hal_adi = "idle"
        self._avatar_nebz_on = False
        self._avatar_qabarcik = None
        self._avatar_drag_data = None

        # siçanla sürükləmə — buraxılanda mövqe əsas pəncərəyə NİSBİ ofset kimi
        # AYARLAR-a yazılır (sonrakı açılışlarda da yadda qalsın deyə)
        lbl.bind("<ButtonPress-1>", self._sesli_avatar_surukle_basla)
        lbl.bind("<B1-Motion>", self._sesli_avatar_surukle)
        lbl.bind("<ButtonRelease-1>", self._sesli_avatar_surukle_bitir)

        # proqram arxa plana keçəndə avatar da gizlənsin (başqa pəncərələrin
        # üstündə görünməsin) — self VƏ avatar pəncərəsinin hər ikisi "bu proqrama
        # aiddir", ona görə ikisi arasında keçiddə gizlənməməli, yalnız HƏR İKİSİ
        # fokusu itirəndə (başqa tətbiqə keçiləndə)
        self.bind("<FocusOut>", lambda e: self.after(150, self._sesli_avatar_fokus_yoxla), add="+")
        self.bind("<FocusIn>", lambda e: self.after(150, self._sesli_avatar_fokus_yoxla), add="+")
        win.bind("<FocusOut>", lambda e: self.after(150, self._sesli_avatar_fokus_yoxla), add="+")
        win.bind("<FocusIn>", lambda e: self.after(150, self._sesli_avatar_fokus_yoxla), add="+")

        self._sesli_avatar_pozisyon()
        self.bind("<Configure>", lambda e: self._sesli_avatar_pozisyon(), add="+")
        self._sesli_avatar_hal("idle")

    def _sesli_avatar_fokus_yoxla(self):
        """Tkinter-in `focus_get()`-i bu proqramın pəncərələrindən HƏR HANSI biri
        fokusdaykən widget qaytarır, başqa tətbiqə keçiləndə isə None — buna görə
        əsas pəncərə/avatar arasında keçiddə gizlənmir, yalnız proqramdan tamamilə
        çıxanda (başqa pəncərə öndə olanda)."""
        win = getattr(self, "_avatar_win", None)
        if not win or not win.winfo_exists():
            return
        sesli_aktiv = bool(self._sesli_thread and self._sesli_thread.is_alive())
        chat_aktiv = bool(getattr(self, "_chat_hotkey_qeydli", None))
        if not (sesli_aktiv or chat_aktiv):
            return  # heç biri aktiv deyilsə _sesli_dayandir/_chat_dayandir onsuz da gizlədib
        try:
            odakli = self.focus_get()
        except Exception:
            odakli = None
        if odakli is None:
            try: win.withdraw()
            except Exception: pass
            qb = getattr(self, "_avatar_qabarcik", None)
            if qb and qb.winfo_exists():
                try: qb.withdraw()
                except Exception: pass
        else:
            try: win.deiconify()
            except Exception: pass

    def _sesli_avatar_surukle_basla(self, event):
        win = getattr(self, "_avatar_win", None)
        if not win: return
        self._avatar_drag_data = {"x": event.x_root, "y": event.y_root,
                                   "win_x": win.winfo_x(), "win_y": win.winfo_y()}

    def _sesli_avatar_surukle(self, event):
        dd = getattr(self, "_avatar_drag_data", None)
        win = getattr(self, "_avatar_win", None)
        if not dd or not win: return
        yeni_x = dd["win_x"] + (event.x_root - dd["x"])
        yeni_y = dd["win_y"] + (event.y_root - dd["y"])
        win.geometry(f"+{yeni_x}+{yeni_y}")

    def _sesli_avatar_surukle_bitir(self, event):
        win = getattr(self, "_avatar_win", None)
        if not win or not getattr(self, "_avatar_drag_data", None):
            return
        self._avatar_drag_data = None
        try:
            ofset = [win.winfo_x() - self.winfo_rootx(), win.winfo_y() - self.winfo_rooty()]
            AYARLAR["sesli_avatar_ofset"] = ofset
            ayar_kaydet(AYARLAR)
        except Exception:
            pass

    def _sesli_avatar_pozisyon(self):
        win = getattr(self, "_avatar_win", None)
        if not win or not win.winfo_exists():
            return
        if getattr(self, "_avatar_drag_data", None):
            return  # sürüklənmə davam edir — mövqeni poza bilərik, toxunma
        try:
            mx = self.winfo_rootx()
            my = self.winfo_rooty()
            uz = _sesli_avatar_uz()
            pad = 22
            w, h = uz.width + pad * 2, uz.height + pad * 2
            ofset = AYARLAR.get("sesli_avatar_ofset")
            if isinstance(ofset, (list, tuple)) and len(ofset) == 2:
                x, y = mx + ofset[0], my + ofset[1]
            else:
                # defolt: əsas pəncərənin SOLUNDA (boş ekran sahəsində), üst
                # kənardan kifayət qədər aşağı ki, baş kəsilməsin
                x = max(10, mx - w - 16)
                y = my + 90
            win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _sesli_avatar_hal(self, hal):
        """idle/listening/danisir halları arasında keçid — thread-təhlükəsiz
        (background thread-dən belə çağırıla bilər, _sesli_status_configure
        ilə eyni self.after(0,...) nümunəsi)."""
        def ui():
            win = getattr(self, "_avatar_win", None)
            lbl = getattr(self, "_avatar_lbl", None)
            if not win or not win.winfo_exists() or not lbl:
                return
            self._avatar_hal_adi = hal
            renk = _AVATAR_RENK.get(hal, _AVATAR_RENK["idle"])
            img = _sesli_avatar_halqali(renk, 1.0)
            photo = ImageTk.PhotoImage(img)
            lbl.configure(image=photo)
            lbl.image = photo
            if hal == "listening":
                self._avatar_nebz_tick(0.0)
            else:
                self._avatar_nebz_on = False
        try:
            self.after(0, ui)
        except Exception:
            pass

    def _avatar_nebz_tick(self, faz):
        if self._avatar_hal_adi != "listening":
            self._avatar_nebz_on = False
            return
        self._avatar_nebz_on = True
        win = getattr(self, "_avatar_win", None)
        lbl = getattr(self, "_avatar_lbl", None)
        if not win or not win.winfo_exists() or not lbl:
            return
        parlaqliq = 0.55 + 0.45 * abs(math.sin(faz))
        img = _sesli_avatar_halqali(_AVATAR_RENK["listening"], parlaqliq)
        photo = ImageTk.PhotoImage(img)
        lbl.configure(image=photo)
        lbl.image = photo
        self.after(120, lambda: self._avatar_nebz_tick(faz + 0.3))

    def _sesli_avatar_de(self, metin, muddet_ms=2200):
        """Avatarın altında qısa müddətli mətn 'qabarcığı' göstərir (SƏS YOX —
        yalnız vizual) — yalnız əsas anlarda çağırılmalıdır (oyadıcı söz
        eşidiləndə/təsdiq lazım olanda/əməliyyat bitəndə), hər status üçün yox."""
        def ui():
            win = getattr(self, "_avatar_win", None)
            if not win or not win.winfo_exists():
                return
            self._sesli_avatar_hal("danisir")
            qb = getattr(self, "_avatar_qabarcik", None)
            if not qb or not qb.winfo_exists():
                qb = tk.Toplevel(self)
                qb.overrideredirect(True)
                qb.attributes("-topmost", True)
                qb_lbl = tk.Label(qb, bg="#1B1E26", fg="white", font=("Segoe UI", 10),
                                   padx=10, pady=6, wraplength=220, justify="left")
                qb_lbl.pack()
                self._avatar_qabarcik = qb
                self._avatar_qabarcik_lbl = qb_lbl
            self._avatar_qabarcik_lbl.configure(text=metin)
            self._avatar_qabarcik.update_idletasks()
            try:
                # avatarın HƏQİQİ hazırkı mövqeyi (siçanla sürüklənmiş ola bilər)
                avatar_win = self._avatar_win
                avatar_x, avatar_y = avatar_win.winfo_x(), avatar_win.winfo_y()
                avatar_w, avatar_h = avatar_win.winfo_width(), avatar_win.winfo_height()
                w = self._avatar_qabarcik.winfo_reqwidth()
                self._avatar_qabarcik.geometry(
                    f"+{avatar_x + avatar_w // 2 - w // 2}+{avatar_y + avatar_h + 10}")
            except Exception:
                pass
            self._avatar_qabarcik.deiconify()

            def gizle():
                try: self._avatar_qabarcik.withdraw()
                except Exception: pass
                if getattr(self, "_avatar_win", None) and self._avatar_win.winfo_exists():
                    self._sesli_avatar_hal("listening")
            self.after(muddet_ms, gizle)
        try:
            self.after(0, ui)
        except Exception:
            pass

    def _sesli_avatar_gizle(self):
        win = getattr(self, "_avatar_win", None)
        if win and win.winfo_exists():
            try: win.withdraw()
            except Exception: pass
        qb = getattr(self, "_avatar_qabarcik", None)
        if qb and qb.winfo_exists():
            try: qb.withdraw()
            except Exception: pass
        self._avatar_nebz_on = False

    def _sesli_baslat(self):
        if self._sesli_thread and self._sesli_thread.is_alive():
            return
        try:
            import sounddevice  # noqa: F401 — mövcudluğu yoxlamaq üçün
        except Exception as e:
            self._sesli_status_configure(t("🎤 Səsli asistent işə düşmədi"), "#C0392B")
            print(f"[Sesli] sounddevice yuklenmedi: {e}")
            return
        self._sesli_stop_event.clear()
        self._sesli_status_configure(t("🎤 Model yüklənir…"), TH["accent"])
        self._sesli_avatar_yarat()
        self._sesli_thread = threading.Thread(target=self._sesli_loop, daemon=True)
        self._sesli_thread.start()

    def _sesli_dayandir(self):
        self._sesli_stop_event.set()
        self._sesli_status_configure("")
        self._sesli_avatar_gizle()

    def _sesli_loop(self):
        try:
            import sounddevice as sd
            import numpy as _np
            model = _whisper_model_yukle()
        except Exception as e:
            self._sesli_status_configure(t("🎤 Model yüklənmədi"), "#C0392B")
            print(f"[Sesli] model yuklenmedi: {e}")
            return
        self._sesli_status_configure(t("🎤 Dinləyir…"), "#0E9F6E")
        self._sesli_avatar_hal("listening")

        SR = 16000
        BLOK = 1600          # 0.1s @ 16kHz
        # Sabit RMS həddi (əvvəlki "ESIK=0.008") canlı sınaqda heç vaxt keçilmədi —
        # mikrofonun real səs səviyyəsi avadanlıqdan/qazancdan asılı olaraq çox dəyişir,
        # ona görə sabit rəqəm əvəzinə ətraf səs-küyə uyğunlaşan dinamik hədd istifadə olunur.
        MIN_ESIK = 0.0015    # mütləq minimum — tam sükunda belə yanlış tətiklənməsin
        ESIK_ZERB = 2.5      # dinamik hədd = ətraf səs-küy × bu əmsal
        ESIK_EMA = 0.03      # ətraf səs-küy təxmininin neçə sürətlə yenilənməsi
        noise_floor = 0.0025 # ilkin təxmin, canlı ölçmə ilə bir neçə saniyədə uyğunlaşır
        SESSIZLIK_SN = 0.7   # bu qədər davamlı sükut = nitq parçası bitdi
        MAKS_PARCA_SN = 8.0  # bir parça üçün maksimum müddət (asılı qalmasın)

        q = queue.Queue()

        def callback(indata, frames, time_info, status):
            q.put(indata[:, 0].copy())

        buffer = []
        danisiq_basladi = False
        sessiz_say = 0.0
        parca_sure = 0.0
        ilk_ses_vaxti = None
        basladi_vaxti = time.time()
        xeberdarliq_gosterildi = False

        try:
            with sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                 blocksize=BLOK, callback=callback):
                while not self._sesli_stop_event.is_set():
                    try:
                        chunk = q.get(timeout=0.2)
                    except queue.Empty:
                        # mikrofon heç nə tutmursa (icazə yoxdur/yanlış cihaz),
                        # bunu bir müddət sonra istifadəçiyə bildir
                        if not xeberdarliq_gosterildi and ilk_ses_vaxti is None \
                                and (time.time() - basladi_vaxti) > 20:
                            xeberdarliq_gosterildi = True
                            self._sesli_status_configure(
                                t("🎤 Mikrofon səs aşkarlamır — Windows mikrofon icazələrini yoxlayın"),
                                "#C0392B")
                        continue
                    enerji = float(_np.sqrt(_np.mean(chunk ** 2))) if len(chunk) else 0.0
                    sn = len(chunk) / SR
                    esik = max(MIN_ESIK, noise_floor * ESIK_ZERB)
                    if enerji > esik:
                        if ilk_ses_vaxti is None:
                            ilk_ses_vaxti = time.time()
                        buffer.append(chunk)
                        danisiq_basladi = True
                        sessiz_say = 0.0
                        parca_sure += sn
                    elif danisiq_basladi:
                        buffer.append(chunk)
                        sessiz_say += sn
                        parca_sure += sn
                    else:
                        # sakit dövr — ətraf səs-küy təxminini tədricən yenilə (yalnız
                        # danışıq olmayanda, əks halda nitq öz-özünü "küy" kimi öyrədərdi)
                        noise_floor = (1 - ESIK_EMA) * noise_floor + ESIK_EMA * enerji
                        if not xeberdarliq_gosterildi and ilk_ses_vaxti is None \
                                and (time.time() - basladi_vaxti) > 20:
                            xeberdarliq_gosterildi = True
                            self._sesli_status_configure(
                                t("🎤 Mikrofon səs aşkarlamır — Windows mikrofon icazələrini yoxlayın"),
                                "#C0392B")

                    if danisiq_basladi and (sessiz_say >= SESSIZLIK_SN or parca_sure >= MAKS_PARCA_SN):
                        audio = _np.concatenate(buffer)
                        buffer = []
                        danisiq_basladi = False
                        sessiz_say = 0.0
                        parca_sure = 0.0
                        if len(audio) / SR >= 0.4:
                            self._sesli_parca_isle(model, audio)
        except Exception as e:
            self._sesli_status_configure(t("🎤 Mikrofon xətası"), "#C0392B")
            print(f"[Sesli] mikrofon xetasi: {e}")

    def _sesli_parca_isle(self, model, audio):
        """Bir nitq parçasını yazıya köçürür, oyadıcı sözü axtarır, tapılsa
        əmri təhlil edib təsdiq dialoqunu açır. Bütün emal yerli (offline) baş
        verir, audio dərhal atılır — heç yerə göndərilmir/saxlanılmır.
        DİQQƏT: tapılıb-tapılmamasından asılı olmayaraq eşidilən mətni qısa
        müddət sidebar-da göstərir — mikrofon/tanıma işləyib-işləmədiyini
        istifadəçinin özünün görə bilməsi üçün (əsas diaqnostika aləti)."""
        oyadici = sesli_oyadici_soz()
        # domen lüğəti + oyadıcı sözü decoder-ə "ipucu" kimi verir — Whisper-in
        # məhdud Azərbaycan dili dəstəyi ilə bu terminləri düzgün yazma ehtimalını artırır
        ipucu = f"{oyadici}, kanal, dolğu, qapaq, çəkim, implant, əksik, gözləm, randevu, saat, sabah, bu gün."
        try:
            segments, _info = model.transcribe(
                audio, language="az", vad_filter=True, initial_prompt=ipucu)
            text = " ".join(seg.text for seg in segments).strip()
        except Exception as e:
            print(f"[Sesli] transkripsiya xetasi: {e}")
            return
        if not text:
            self._sesli_status_configure(t("🎤 (səs eşidildi, söz tanınmadı)"), "#7A99C4")
            self.after(2000, lambda: self._sesli_status_configure(t("🎤 Dinləyir…"), "#0E9F6E"))
            return
        self._sesli_status_configure(f"🎤 Eşidildi: “{text[:50]}”", "#7A99C4")
        son = _oyadici_soz_tap(text, oyadici)
        if son is None:
            self.after(2500, lambda: self._sesli_status_configure(t("🎤 Dinləyir…"), "#0E9F6E"))
            return
        self._sesli_avatar_de(t("👂 Dinləyirəm…"), 1800)
        emr_metni = text[son:].strip(" ,.:;!?-")
        if not emr_metni:
            self._sesli_status_configure(t("🎤 Dinləyir… (əmr gözlənilir)"), "#0E9F6E")
            self.after(2500, lambda: self._sesli_status_configure(t("🎤 Dinləyir…"), "#0E9F6E"))
            return
        aktif_adi = None
        if self.aktif_hid and self.aktif_hid in self.hastalar:
            aktif_adi = self.hastalar[self.aktif_hid].get("ad")
        netice = None
        api_key = (AYARLAR.get("anthropic_api_key") or "").strip()
        if api_key:
            try:
                netice = _claude_parse_emr(emr_metni, self.hastalar, aktif_adi, api_key)
            except Exception as e:
                print(f"[Sesli] Claude AI təhlili uğursuz, yerli təhlilə keçilir: {e}")
        if netice is None:
            netice = parse_sesli_emr(emr_metni, self.hastalar, aktif_hasta_adi=aktif_adi)
        if not netice:
            self._sesli_status_configure(t("🎤 Anlaşılmadı"), "#C0392B")
            self.after(2500, lambda: self._sesli_status_configure(t("🎤 Dinləyir…"), "#0E9F6E"))
            return
        self._sesli_avatar_de(_netice_qisa_metn(netice), 3500)
        self.after(0, lambda: self._sesli_konfirm_dialog(netice))

    def _sesli_konfirm_dialog(self, netice):
        if netice["niyyet"] == "plan":
            self._sesli_tedavi_dialog(netice)
        else:
            self._sesli_randevu_dialog(netice)

    def _sesli_yeni_pasient_yarat(self, ad):
        """Səsli əmrlə yeni pasient yaradır — manual '+ Yeni Pasient' (_yeni_hasta)
        ilə eyni minimal struktur (yalnız ad + 1 boş vaka), InputDialog olmadan
        (ad artıq səsdən gəlir). (hid, vid) qaytarır."""
        hid = yeni_id("hasta")
        vid = yeni_id("vaka")
        self.hastalar[hid] = {
            "ad": ad,
            "vakalar": {
                vid: {
                    "ad": f"1. {t('Vaka')}",
                    "tarih": datetime.now().strftime("%d.%m.%Y"),
                    "plan": {}, "tedavi": {}
                }
            }
        }
        veri_kaydet(self.hastalar)
        self._auto_bxd_backup(hid)
        self.expanded_hids.add(hid)
        self._lista_guncelle()
        return hid, vid

    def _sesli_tedavi_dialog(self, netice):
        """Plan/İlkin Vəziyyət/Müalicə (3 tab ortaq) üçün təsdiq dialoqu. Hədəf pasient
        ya hazırkı aktiv pasientdir, ya mətndə "<ad> üçün" ilə tapılan MÖVCUD pasientdir
        (təsdiqləndikdə ekran ona keçir), ya da tapılmayıb YENİ yaradılacaq pasientdir."""
        tab = netice["tab"]
        dis_nolari = netice["dis_nolari"]
        tid = netice["tid"]
        soylenen_ad = netice.get("xeste_adi")
        yeni_pasient = netice.get("yeni_pasient", False)

        hedef_hid = None
        nisan = None
        if yeni_pasient:
            goster_ad = soylenen_ad
            nisan = t("🆕 YENİ PASİENT")
        elif soylenen_ad:
            hedef_hid = next((hid for hid, h in self.hastalar.items()
                               if h.get("ad", "") == soylenen_ad), None)
            goster_ad = soylenen_ad
            if hedef_hid is None:
                # nəzəri olaraq baş verməməlidir (parse_sesli_emr artıq uyğunlaşdırıb),
                # amma qorunma: tapılmasa indiki aktiv pasientə düş
                hedef_hid = None
            elif hedef_hid != self.aktif_hid:
                nisan = t("↔ Pasient dəyişəcək")
        else:
            goster_ad = None

        if hedef_hid is None and not yeni_pasient:
            if not self.aktif_hid or not self.aktif_vid:
                import tkinter.messagebox as mb
                mb.showwarning(t("Səsli Əmr"), t("Əvvəlcə pasient seçin."))
                return
            hedef_hid = self.aktif_hid
            goster_ad = self.hastalar.get(self.aktif_hid, {}).get("ad", "")

        lbl = t(_SESLI_TEDAVI_ETIKET_ALL.get(tid, tid))
        tab_lbl = t(_SESLI_TAB_ETIKET[tab])
        dis_metni = ", ".join(f"{n} ({dis_adi(n)})" for n in dis_nolari)

        dlg = ctk.CTkToplevel(self, fg_color=TH["bg"])
        dlg.title(t("Səsli Əmr"))
        dlg.geometry("420x260")
        dlg.attributes("-topmost", True)
        ctk.CTkLabel(dlg, text=t("🎤 Anladıq"), font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=TH["accent"]).pack(pady=(20, 6))
        if nisan:
            ctk.CTkLabel(dlg, text=nisan, font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(pady=(0, 2))
        ctk.CTkLabel(dlg, text=goster_ad, font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=TH["txt"]).pack(pady=2)
        ctk.CTkLabel(dlg, text=f"[{tab_lbl}]  {t('Diş')} {dis_metni}\n→ {lbl}",
                     font=ctk.CTkFont(size=13), text_color=TH["txt_sub"],
                     wraplength=360, justify="center").pack(pady=(2, 10))

        def _tesdiq():
            nonlocal hedef_hid
            self._chat_pending_confirm = None
            if yeni_pasient:
                hedef_hid, vid = self._sesli_yeni_pasient_yarat(soylenen_ad)
                self._yukle(hedef_hid, vid)
            elif hedef_hid != self.aktif_hid:
                self._yukle(hedef_hid)
            self.secili_tedavi = tid
            metod = getattr(self, _SESLI_TAB_METOD[tab])
            for n in dis_nolari:
                metod(n, None)
            self._sesli_avatar_de(t("✓ Oldu!"), 1800)
            try: dlg.destroy()
            except Exception: pass

        def _legv():
            self._chat_pending_confirm = None
            self._sesli_avatar_de(t("Ləğv edildi"), 1500)
            try: dlg.destroy()
            except Exception: pass

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=10)
        ctk.CTkButton(btn_row, text=t("✓ Təsdiqlə"), width=130, height=34, corner_radius=10,
                      fg_color=TH["ok"], hover_color=TH["ok_hover"],
                      font=ctk.CTkFont(size=12, weight="bold"), command=_tesdiq).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text=t("İmtina"), width=110, height=34, corner_radius=10,
                      fg_color="transparent", border_width=1, border_color=TH["accent_soft"],
                      text_color=TH["accent"], command=_legv).pack(side="left", padx=6)
        self._chat_pending_confirm = {"tesdiq": _tesdiq, "legv": _legv}

    def _sesli_randevu_dialog(self, netice):
        xeste_adi = netice["xeste_adi"]
        yeni_pasient = netice.get("yeni_pasient", False)
        tarix_iso = netice["tarix"]
        saat = netice["saat"]
        tarix_goster = datetime.strptime(tarix_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
        eslesen_hid = None if yeni_pasient else next(
            (hid for hid, h in self.hastalar.items() if h.get("ad", "") == xeste_adi), None)

        dlg = ctk.CTkToplevel(self, fg_color=TH["bg"])
        dlg.title(t("Səsli Əmr"))
        dlg.geometry("400x240")
        dlg.attributes("-topmost", True)
        ctk.CTkLabel(dlg, text=t("🎤 Anladıq"), font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=TH["accent"]).pack(pady=(20, 6))
        if yeni_pasient:
            ctk.CTkLabel(dlg, text=t("🆕 YENİ PASİENT"), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(pady=(0, 2))
        ctk.CTkLabel(dlg, text=xeste_adi, font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=TH["txt"]).pack(pady=2)
        ctk.CTkLabel(dlg, text=f"{tarix_goster}  ·  {t('saat')} {saat}",
                     font=ctk.CTkFont(size=13), text_color=TH["txt_sub"]).pack(pady=(2, 10))

        def _tesdiq():
            nonlocal eslesen_hid
            self._chat_pending_confirm = None
            if yeni_pasient:
                eslesen_hid, _vid = self._sesli_yeni_pasient_yarat(xeste_adi)
            rid2 = yeni_id("randevu")
            self.randevular[rid2] = {
                "hasta_id": eslesen_hid,
                "hasta_ad": xeste_adi,
                "tarix": tarix_iso,
                "saat": saat,
                "muddet": 30,
                "qeyd": t("Səsli əmrlə əlavə edildi"),
                "status": "gozleyir",
            }
            randevu_kaydet(self.randevular)
            self._sesli_avatar_de(t("✓ Oldu!"), 1800)
            try: dlg.destroy()
            except Exception: pass

        def _legv():
            self._chat_pending_confirm = None
            self._sesli_avatar_de(t("Ləğv edildi"), 1500)
            try: dlg.destroy()
            except Exception: pass

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=10)
        ctk.CTkButton(btn_row, text=t("✓ Təsdiqlə"), width=130, height=34, corner_radius=10,
                      fg_color=TH["ok"], hover_color=TH["ok_hover"],
                      font=ctk.CTkFont(size=12, weight="bold"), command=_tesdiq).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text=t("İmtina"), width=110, height=34, corner_radius=10,
                      fg_color="transparent", border_width=1, border_color=TH["accent_soft"],
                      text_color=TH["accent"], command=_legv).pack(side="left", padx=6)
        # Ümumi çat rejimində (əgər bu dialoq ordan açılıbsa) növbəti push-to-talk
        # cavabında "bəli/xeyr" deyiləndə birbaşa bu closure-ları çağırmaq üçün:
        self._chat_pending_confirm = {"tesdiq": _tesdiq, "legv": _legv}

    # ── ÜMUMİ AI SÖHBƏTİ (push-to-talk, F9) — wake-word sistemindən MÜSTƏQİL ────
    def _chat_hazir_metni(self):
        return f"{t('Hazır')} — {chat_hotkey().upper()} {t('basılı saxla, danışın')}"

    def _chat_baslat(self):
        if self._chat_hotkey_qeydli:
            return
        try:
            import keyboard, sounddevice  # noqa: F401 — mövcudluq yoxlaması
        except Exception as e:
            print(f"[Chat] modul yüklənmədi (keyboard/sounddevice): {e}")
            return
        self._chat_pencere_yarat()
        self._chat_hotkey_qeydiyyat()

    def _chat_dayandir(self):
        self._chat_hotkey_sil()
        if self._chat_ptt_aktiv:
            self._chat_ptt_burax()
        win = self._chat_win
        if win is not None:
            try: win.withdraw()
            except Exception: pass

    def _chat_hotkey_qeydiyyat(self):
        try:
            import keyboard
            key = chat_hotkey()
            keyboard.on_press_key(key, lambda e: self.after(0, self._chat_ptt_bas))
            keyboard.on_release_key(key, lambda e: self.after(0, self._chat_ptt_burax))
            self._chat_hotkey_qeydli = key
        except Exception as e:
            print(f"[Chat] hotkey qeydiyyatı uğursuz: {e}")
            self._chat_hotkey_qeydli = None

    def _chat_hotkey_sil(self):
        if self._chat_hotkey_qeydli:
            try:
                import keyboard
                keyboard.unhook_all()
            except Exception:
                pass
            self._chat_hotkey_qeydli = None

    def _chat_pencere_yarat(self):
        win = self._chat_win
        if win is not None and win.winfo_exists():
            try:
                win.deiconify()
                win.lift()
            except Exception:
                pass
            return
        win = ctk.CTkToplevel(self, fg_color=TH["bg"])
        win.title(t("AI Söhbət"))
        win.geometry("360x480")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        self._chat_scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
        self._chat_scroll.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self._chat_status_lbl = ctk.CTkLabel(win, text=self._chat_hazir_metni(),
                                              font=ctk.CTkFont(size=11), text_color=TH["txt_sub"])
        self._chat_status_lbl.pack(pady=(0, 8))
        self._chat_win = win

    def _chat_status_configure(self, text):
        try:
            self._chat_status_lbl.configure(text=text)
        except Exception:
            pass

    def _chat_mesaj_goster(self, kim, metin):
        """kim: 'user' və ya 'ai' — çat pəncərəsinə mesaj qabarcığı əlavə edir."""
        if self._chat_win is None or not self._chat_win.winfo_exists():
            self._chat_pencere_yarat()
        try:
            sag = (kim == "user")
            bubble = ctk.CTkFrame(self._chat_scroll,
                                   fg_color=(TH["accent"] if sag else TH["sidebar_row"]),
                                   corner_radius=12)
            bubble.pack(anchor="e" if sag else "w", padx=8, pady=4)
            ctk.CTkLabel(bubble, text=metin, font=ctk.CTkFont(size=12),
                         text_color=("white" if sag else TH["txt"]), wraplength=260,
                         justify="left").pack(padx=10, pady=8)
        except Exception as e:
            print(f"[Chat] mesaj göstərilmədi: {e}")

    def _chat_ptt_bas(self):
        if self._chat_ptt_aktiv:
            return  # düymə basılı saxlanarkən OS-un təkrar "press" eventləri
        self._tts_stop_event.set()   # davam edən TTS-i dərhal kəs (bənd 4)
        self._tts_stop_event = threading.Event()
        try:
            import sounddevice as sd
        except Exception as e:
            print(f"[Chat] sounddevice yoxdur: {e}")
            return
        self._chat_ptt_buffer = []

        def callback(indata, frames, time_info, status):
            self._chat_ptt_buffer.append(indata[:, 0].copy())

        try:
            stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                     blocksize=1600, callback=callback)
            stream.start()
        except Exception as e:
            print(f"[Chat] mikrofon açılmadı: {e}")
            self._chat_status_configure(t("Mikrofon xətası"))
            return
        self._chat_ptt_stream = stream
        self._chat_ptt_aktiv = True
        self._chat_status_configure(t("🎤 Dinləyirəm…"))
        self._sesli_avatar_yarat()
        self._sesli_avatar_hal("listening")

    def _chat_ptt_burax(self):
        if not self._chat_ptt_aktiv:
            return
        self._chat_ptt_aktiv = False
        stream = self._chat_ptt_stream
        self._chat_ptt_stream = None
        try:
            if stream is not None:
                stream.stop(); stream.close()
        except Exception:
            pass
        buf = self._chat_ptt_buffer
        self._chat_ptt_buffer = []
        if not buf:
            self._chat_status_configure(self._chat_hazir_metni())
            return
        import numpy as _np
        audio = _np.concatenate(buf)
        self._chat_status_configure(t("⏳ Fikirləşirəm…"))
        threading.Thread(target=self._chat_ptt_isle, args=(audio,), daemon=True).start()

    def _chat_ptt_isle(self, audio):
        """Arxa-plan thread-də: transkripsiya → (gözlənilən təsdiq varsa onu
        həll et) → Claude-a göndər → nəticəni (mətn və ya alət-təklifi) göstər.
        Bütün UI toxunuşları `self.after(0, …)` ilə marşrutlanır."""
        import numpy as _np
        try:
            model = _whisper_model_yukle()
        except Exception as e:
            print(f"[Chat] whisper model: {e}")
            self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
            return
        if len(audio) / 16000.0 < 0.3:
            self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
            return
        ipucu = ("kanal, dolğu, qapaq, çəkim, implant, əksik, gözləm, randevu, "
                  "saat, sabah, bu gün, bəli, xeyr, ləğv et.")
        try:
            segments, _info = model.transcribe(audio, language="az", vad_filter=True,
                                                 initial_prompt=ipucu)
            text = " ".join(seg.text for seg in segments).strip()
        except Exception as e:
            print(f"[Chat] transkripsiya xetasi: {e}")
            self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
            return
        if not text:
            self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
            return
        self.after(0, lambda: self._chat_mesaj_goster("user", text))

        pending = self._chat_pending_confirm
        if pending:
            qerar = _chat_tesdiq_xeyr_yoxla(text)
            if qerar == "tesdiq":
                self.after(0, pending["tesdiq"])
                self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
                return
            if qerar == "legv":
                self.after(0, pending["legv"])
                self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
                return

        api_key = (AYARLAR.get("anthropic_api_key") or "").strip()
        if not api_key:
            self.after(0, lambda: self._chat_mesaj_goster(
                "ai", t("Anthropic API açarı Ayarlar-da daxil edilməyib.")))
            self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
            return

        aktif_adi = None
        if self.aktif_hid and self.aktif_hid in self.hastalar:
            aktif_adi = self.hastalar[self.aktif_hid].get("ad")
        try:
            novu, deyer = _claude_chat_cavab(text, self.hastalar, aktif_adi, api_key, self._chat_tarixce)
        except Exception as e:
            print(f"[Chat] Claude xetasi: {e}")
            self.after(0, lambda: self._chat_mesaj_goster("ai", t("Claude-a qoşulmaq alınmadı.")))
            self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))
            return

        self._chat_tarixce.append({"role": "user", "content": text})
        tts_event = self._tts_stop_event
        if novu == "tool":
            netice = deyer
            qisa = _netice_qisa_metn(netice) + " " + t("(bəli/xeyr?)")
            self._chat_tarixce.append({"role": "assistant", "content": f"[{t('əməliyyat təklif edildi')}]"})
            self.after(0, lambda: self._chat_mesaj_goster("ai", qisa))
            self.after(0, lambda: self._sesli_avatar_de(qisa, 3000))
            self.after(0, lambda: self._sesli_konfirm_dialog(netice))
            self.after(0, lambda: self._chat_status_configure(t("🔊 Danışır…")))
            _tts_de(qisa, tts_event)
        else:
            metin = deyer
            self._chat_tarixce.append({"role": "assistant", "content": metin})
            self.after(0, lambda: self._chat_mesaj_goster("ai", metin))
            self.after(0, lambda: self._sesli_avatar_de(metin[:60], 2500))
            self.after(0, lambda: self._chat_status_configure(t("🔊 Danışır…")))
            _tts_de(metin, tts_event)
        del self._chat_tarixce[:-10]
        if not (self._sesli_thread and self._sesli_thread.is_alive()):
            self.after(0, lambda: self._sesli_avatar_hal("idle"))
        self.after(0, lambda: self._chat_status_configure(self._chat_hazir_metni()))

    def _on_close(self):
        """Stop background watcher thread before destroying the window."""
        if self._xray_observer and self._xray_observer.is_alive():
            self._xray_observer.stop()
            self._xray_observer.join(timeout=2)
        self._sesli_dayandir()
        self._chat_hotkey_sil()
        # hastalar.json yazısı gecikdirilib (performans, bax: _hastalar_save_tick) —
        # bağlanmadan əvvəl hələ diskə yazılmamış son dəyişikliklər itməsin deyə gözlə.
        t0 = time.time()
        while self._hastalar_save_islemde and (time.time() - t0) < 3.0:
            time.sleep(0.05)
        self._hastalar_save_flush()
        self.destroy()


if __name__=="__main__":
    if "--net-test" in sys.argv:   # GECICI DEBUG - bulud baglanti sinamasi, isim bitince silinecek
        print("=== NET TEST BASLADI ===")
        print("SUPABASE_URL:", SUPABASE_URL)
        try:
            import certifi
            print("certifi modulu:", certifi.__file__)
            print("certifi.where():", certifi.where())
            print("cacert.pem mevcuddur:", os.path.exists(certifi.where()))
        except Exception as e:
            print("certifi YUKLENEMEDI:", type(e).__name__, repr(e))
        ctx = ssl_baglam()
        print("ssl_baglam() netice:", "REAL KONTEKST" if ctx is not None else "NONE (defolta düşür)")
        try:
            r = _sb_http("GET", "/rest/v1/hastalar?select=hasta_id&limit=1")
            print("SONUC: UGURLU:", r)
        except Exception as e:
            print("SONUC: XETA:", type(e).__name__, repr(e))
        print("=== NET TEST BITDI ===")
        sys.exit(0)
    app=DentalApp()
    app.mainloop()
