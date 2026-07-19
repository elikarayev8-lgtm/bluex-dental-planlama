import sys
import customtkinter as ctk
import tkinter as tk
from PIL import Image, ImageDraw, ImageTk, ImageFilter, ImageFont, ImageChops
from io import BytesIO
import json, os, math, base64, numpy as np, threading, queue, time, uuid
from datetime import datetime

# ── X-ray folder watcher (watchdog) ─────────────────────────────────────────
try:
    from watchdog.observers import Observer as _WatchdogObserver
    from watchdog.events import FileSystemEventHandler as _FSEventHandler
    _WATCHDOG_OK = True
except ImportError:
    _WATCHDOG_OK = False
    class _FSEventHandler:          # no-op stub so _XRayFileHandler can inherit
        pass

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

# ── BlueX Futuristic Theme (mavi-beyaz) ──────────────────────────────────────
TH = {
    "bg":           "#EAF2FC",   # buz mavisi uygulama zemini
    "panel":        "#FFFFFF",   # beyaz kartlar
    "panel_soft":   "#F2F7FE",   # hafif mavi kart
    "sidebar":      "#0B2447",   # koyu lacivert kenar çubuğu
    "sidebar_row":  "#153B6B",   # pasif hasta satırı
    "sidebar_sub":  "#0E2F58",   # vaka akordeon zemini
    "sidebar_row2": "#1B4878",   # pasif vaka satırı
    "accent":       "#2563EB",   # elektrik mavisi
    "accent_hover": "#1D4ED8",
    "accent_soft":  "#DBEAFE",
    "txt":          "#0F2447",   # lacivert metin
    "txt_sub":      "#5B7398",   # mavi-gri
    "txt_faint":    "#8AA3C5",
    "ok":           "#0E9F6E",   # teal (onay / müalicə)
    "ok_hover":     "#0B7A55",
    "indigo":       "#4F46E5",
    "indigo_hover": "#4338CA",
}

def tint(hex_color, f=0.86):
    """Rengi beyaza doğru açar (chip zemini için). f: beyaz oranı 0-1."""
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    r = int(r + (255 - r) * f); g = int(g + (255 - g) * f); b = int(b + (255 - b) * f)
    return f"#{r:02x}{g:02x}{b:02x}"

SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
if getattr(sys, 'frozen', False):
    # PyInstaller EXE — write to a stable user-writable location
    DATA_FILE = os.path.join(os.environ.get('APPDATA', SCRIPT_DIR), 'BlueX', 'hastalar.json')
else:
    DATA_FILE = os.path.join(SCRIPT_DIR, 'hastalar.json')
XRAY_WATCH_DIR = r"E:\DRImage"   # varsayılan — Ayarlar'dan değiştirilebilir
XRAY_PUSH_JSON = r"C:\BlueX\xray_push.json"
VR_LOADOUT_FILE = r"C:\BlueX\vr_loadout.json"

# uygulama simgesi (exe'de PyInstaller datas ile gelir, dev'de script dizininde)
ICON_FILE = os.path.join(getattr(sys, "_MEIPASS", SCRIPT_DIR), "app.ico")

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

def xray_watch_dir():
    return (AYARLAR.get("xray_klasoru") or "").strip() or XRAY_WATCH_DIR

def xray_subdir_only():
    return AYARLAR.get("alt_klasor_sarti", True)

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
    (15s) və tavan (300s) arasında miqyaslanır."""
    if not data_bytes:
        return 15
    return max(15, min(300, data_bytes // (80 * 1024)))

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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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

def sb_signup(email, password):
    return _sb_http("POST", "/auth/v1/signup", {"email": email, "password": password})

def sb_login(email, password):
    return _sb_http("POST", "/auth/v1/token?grant_type=password",
                     {"email": email, "password": password})

def sb_refresh(refresh_token):
    return _sb_http("POST", "/auth/v1/token?grant_type=refresh_token",
                     {"refresh_token": refresh_token})

# ── OTOMATİK GÜNCELLEME KONTROLÜ ──────────────────────────────────────────────
# `app_surumler` tablosunda (bulud, herkese açık okunabilir) en son sürüm
# satırını okur; installer/supabase_surum_schema.sql ile kurulur. Yeni sürüm
# yayınlanırken bu tabloya tek bir satır eklenir (surum, indirme_url, notlar).
APP_VERSION = "1.1.0"

def _ver_tuple(s):
    parcalar = []
    for p in str(s).split("."):
        try: parcalar.append(int(p))
        except ValueError: parcalar.append(0)
    return tuple(parcalar)

def check_for_update():
    """Buluddaki en son sürümü sorgular. Mevcut sürümden yeniyse bilgi
    dict'i (surum/indirme_url/notlar) döndürür, değilse ya da bulud
    yapılandırılmamışsa/tablo yoksa None döndürür (sessizce)."""
    if not cloud_configured():
        return None
    try:
        rows = _sb_http("GET",
            "/rest/v1/app_surumler?select=surum,indirme_url,notlar&order=created_at.desc&limit=1",
            timeout=15)
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    if _ver_tuple(row.get("surum", "0")) > _ver_tuple(APP_VERSION):
        return row
    return None

def yeni_id(prefix):
    """Zaman damğası + qısa təsadüfi son əlavə — fərqli kompüterlərdə/hekimlərdə
    eyni saniyədə yaradılan qeydlərin bulud sinxronunda ID toqquşmasının qarşısını alır."""
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

def veri_yukle():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE,"r",encoding="utf-8") as f:
            data = json.load(f)
        # Eski format → vakalar'a geçiş
        changed = False
        for hid, h in data.items():
            if "vakalar" not in h:
                from datetime import datetime as _dt
                vid = yeni_id("vaka")
                h["vakalar"] = {vid: {
                    "ad": "1. Vaka",
                    "tarih": _dt.now().strftime("%d.%m.%Y"),
                    "plan":   h.pop("plan",   {}),
                    "tedavi": h.pop("tedavi", {}),
                }}
                changed = True
        if changed:
            with open(DATA_FILE,"w",encoding="utf-8") as f:
                json.dump(data,f,ensure_ascii=False,indent=2)
        return data
    return {}

_CLOUD_DIRTY = False    # yerelde değişiklik oldu, buluta gönderilmeyi bekliyor
_CLOUD_SYNC_LOCK = threading.Lock()  # manuel push/pull ile 20sn'lik otomatik tick'in
                                      # aynı anda çalışıp token yenileme yarışına girmesini engeller
_BACKUP_DIRTY = False   # yerelde değişiklik oldu, .bxd yedeyi bekliyor
_BACKUP_LAST_CHANGE = 0.0

def veri_kaydet(data):
    global _CLOUD_DIRTY, _BACKUP_DIRTY, _BACKUP_LAST_CHANGE
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE,"w",encoding="utf-8") as f:
        json.dump(data,f,ensure_ascii=False,indent=2)
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
        if path.lower().endswith(".dcm"):
            png = dcm_to_png(path)
            if not png:
                return
            path_out = png
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
def dis_adi(n): return f"{KADRAN[n//10]} {DIS_AD[n%10]}"

TEDAVILER = [
    ("saglikli","Sağlıklı", None),
    ("curuk",   "Çürük",    (150, 94,  44 )),   # kron üzerinde çürük lekeleri — yalnız İlkin Vəziyyət sekmesinde
    ("cekim",   "Çekim",    (220, 50,  50 )),
    ("kanal",   "Kanal",    (65,  105, 225)),
    ("dolgu",   "Dolgu",    (0,   180, 120)),
    ("kron",    "Kron",     (180, 50,  220)),
    ("implant", "İmplant",  (34,  139, 34 )),
    ("eksik",   "Eksik",    (128, 128, 128)),
    ("kok",     "Kök",      (184, 122, 58 )),   # kron hissəsi dağılmış diş — yalnız İlkin Vəziyyət sekmesinde
    ("gozlem",  "Gözlem",   (255, 140, 0  )),
]
T_COLOR = {t[0]: t[2] for t in TEDAVILER}

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
    if durum.get("kanal"): parcalar.append("Kanal")
    tac = durum.get("tac","saglikli")
    # eski veri: "curuk"/"dolgu" doğrudan tac olarak saklanmış olabilir
    if tac == "curuk":
        parcalar.append("Çürük")
    elif tac == "dolgu":
        parcalar.append("Dolgu")
    elif tac != "saglikli":
        for tid,lbl,_ in TEDAVILER:
            if tid == tac:
                parcalar.append(lbl)
                break
    if durum.get("dolgu") and tac != "dolgu": parcalar.append("Dolgu(" + "".join(durum["dolgu"]) + ")")
    if durum.get("curuk") and tac != "curuk": parcalar.append("Çürük(" + "".join(durum["curuk"]) + ")")
    if durum.get("kron"): parcalar.append("Kron")
    return " + ".join(parcalar) if parcalar else "Sağlıklı"

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
    if ns["kanal"]: parts.append("Kanal")
    if ns["tac"]:
        lbl = next((x[1] for x in TEDAVILER if x[0]==ns["tac"]), ns["tac"].capitalize())
        parts.append(lbl)
    if ns["kron"]: parts.append("Kron")
    return " + ".join(parts) if parts else "Sağlıklı"

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

M_BG     = (11, 36, 71)      # kenar çubuğu laciverti (#0B2447)
M_PANEL  = (21, 59, 107)     # çip zemini (#153B6B)
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
        # açık uygulama zemini üstünde yuvarlak köşeli koyu kart
        img = Image.new("RGB", (W, H), (234, 242, 252))
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

def get_map_photo(tedaviler, dis_tasi=False, secili=None, band_lbl="Diş Daşı Təmizliyi",
                  ilkin=False, secili_cok=None):
    S = _MS
    states = {}
    for k, v in tedaviler.items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        states[n] = normalize_state(v)
    key_items = tuple(sorted((n, s["tac"], s["kanal"], s["kron"], tuple(s["curuk"]), tuple(s["dolgu"]))
                             for n, s in states.items()))
    cok_key = tuple(secili_cok) if secili_cok else None
    key = ("bluexmap", key_items, dis_tasi, secili, band_lbl, ilkin, cok_key)
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
    row1 = [("Sağlıklı", M_IVORY, healthy),
            ("Çekim", M_COLORS["cekim"], cnt.get("cekim", 0)),
            ("Kanal", M_COLORS["kanal"], cnt.get("kanal", 0)),
            ("Dolgu", M_COLORS["dolgu"], cnt.get("dolgu", 0)),
            ("Kron", M_COLORS["kron"], cnt.get("kron", 0))]
    row2 = [("İmplant", M_COLORS["implant"], cnt.get("implant", 0)),
            ("Eksik", (90, 98, 112), cnt.get("eksik", 0))]
    if ilkin:
        row2 += [("Çürük", M_COLORS["curuk"], cnt.get("curuk", 0)),
                 ("Kök", M_COLORS["kok"], cnt.get("kok", 0))]
    else:
        row2 += [("Gözlem", M_COLORS["gozlem"], cnt.get("gozlem", 0))]
    row2 += [("Diş Daşı", M_TARTAR, "Aktiv" if dis_tasi else "—")]
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

    def __init__(self, parent, on_click, on_range=None, **kw):
        super().__init__(parent, width=self.DW, height=self.DH,
                         bg=TH["bg"], highlightthickness=0, cursor="hand2", **kw)
        self.on_click = on_click
        self.on_range = on_range
        self.tedaviler = {}
        self.dis_tasi = False
        self.secili = None
        self.band_label = "Diş Daşı Təmizliyi"
        self.ilkin = False
        self._ref = None
        self._surukle_sira = None   # sürükleme başladığı diş sırası (UST/ALT)
        self._surukle_disler = []   # sürükleme ile o an kapsanan dişler
        self._surukle_zone = None   # basılan noktadaki yüzey (M/D/O/B/L/V) — dolgu/çürük için
        self._hover_key = None      # (dis_no, yuzey) — üzerinde durulan bölge, hover vurgusu için
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
                              band_lbl=self.band_label, ilkin=self.ilkin, secili_cok=cok)
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

    def _surukle(self, e):
        if self._surukle_sira is None:
            return
        hit = map_hit(e.x, e.y)
        if hit is None or hit[0] not in self._surukle_sira:
            return
        n = hit[0]
        i0 = self._surukle_sira.index(self._surukle_disler[0])
        i1 = self._surukle_sira.index(n)
        lo, hi = (i0, i1) if i0 <= i1 else (i1, i0)
        disler = self._surukle_sira[lo:hi + 1]
        if disler != self._surukle_disler:
            self._surukle_disler = disler
            self.secili = n
            self.ciz()

    def _birak(self, e):
        disler = self._surukle_disler
        zone = self._surukle_zone
        self._surukle_sira = None
        self._surukle_disler = []
        self._surukle_zone = None
        if not disler:
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
        self.title("Dental Tedavi Planlama")
        try:
            if os.path.exists(ICON_FILE): self.iconbitmap(ICON_FILE)
        except Exception: pass
        self.geometry("1400x820")
        # büyük harita için tam ekran çalışma alanı (mainloop başladıktan sonra)
        self.after(50, self._maximize)
        self.hastalar = veri_yukle()
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
        self._start_xray_watcher()
        self.after(2000, self._check_xray_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── 5 saniyəlik "dəyişiklikdən sonra yedəklə" dövrəsi (.bxd) ───────────
        self.after(1000, self._bxd_backup_tick)

        # ── Bulud senkronizasyonu (self.cloud_status_lbl _build() içində qurulub) ──
        self._cloud_tick_started = False
        if cloud_configured() and AYARLAR.get("sb_access_token"):
            self._cloud_tick_started = True
            self.after(3000, self._cloud_auto_sync_tick)

        # ── Otomatik güncelleme kontrolü (sessiz — açılıştan birkaç saniye sonra) ──
        self.after(4000, lambda: self._guncelleme_kontrol_et(sessiz=True))

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
        sol.grid_rowconfigure(4,weight=1); sol.grid_columnconfigure(0,weight=1)
        ctk.CTkLabel(sol,text="🦷  H A S T A L A R",font=ctk.CTkFont(size=13,weight="bold"),
                     text_color=TH["txt_faint"]).grid(row=0,column=0,padx=14,pady=(20,10),sticky="w")
        ctk.CTkButton(sol,text="+  Yeni Hasta",height=38,corner_radius=10,
                      fg_color=TH["accent"],hover_color=TH["accent_hover"],
                      font=ctk.CTkFont(size=13,weight="bold"),
                      command=self._yeni_hasta).grid(row=1,column=0,padx=10,pady=(0,6),sticky="ew")
        ctk.CTkButton(sol,text="📂  Hasta Yükle (.bxd)",height=34,corner_radius=10,
                      fg_color="transparent",border_width=1,border_color="#3B5E8F",
                      text_color="#B9CDEB",hover_color=TH["sidebar_row"],
                      command=self._hasta_yukle).grid(row=2,column=0,padx=10,pady=(0,10),sticky="ew")
        arama_f = ctk.CTkFrame(sol,fg_color="transparent")
        arama_f.grid(row=3,column=0,padx=10,pady=(0,6),sticky="ew")
        arama_f.grid_columnconfigure(0,weight=1)
        ctk.CTkEntry(arama_f,textvariable=self.hasta_arama_var,
                     placeholder_text="🔍 Hasta axtar…",height=32,corner_radius=8,
                     border_color=TH["accent_soft"],fg_color=TH["sidebar_row"],
                     text_color="white").grid(row=0,column=0,sticky="ew")
        self.scroll = ctk.CTkScrollableFrame(sol,fg_color="transparent")
        self.scroll.grid(row=4,column=0,padx=4,pady=4,sticky="nsew")
        ctk.CTkButton(sol,text="⚙  Ayarlar",height=32,corner_radius=10,
                      fg_color="transparent",border_width=1,border_color="#3B5E8F",
                      text_color="#B9CDEB",hover_color=TH["sidebar_row"],
                      command=self._ayarlar_ac).grid(row=5,column=0,padx=10,pady=(4,4),sticky="ew")
        self.cloud_status_lbl = ctk.CTkLabel(sol,text="",font=ctk.CTkFont(size=10),
                                             text_color="#7A99C4")
        self.cloud_status_lbl.grid(row=6,column=0,padx=12,pady=(0,10),sticky="w")
        self._cloud_status_guncelle()

        self.sag = ctk.CTkFrame(self,fg_color=TH["bg"],corner_radius=0)
        self.sag.grid(row=0,column=1,sticky="nsew")
        self.sag.grid_columnconfigure(0,weight=1); self.sag.grid_rowconfigure(0,weight=1)

        self.kars = ctk.CTkLabel(self.sag,text="Hasta seçin veya yeni hasta ekleyin",
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
        for sid,t in [(2,"İlkin Vəziyyət"),(0,"Plan"),(1,"Müalicə")]:
            b=ctk.CTkButton(sf,text=t,width=110,height=32,corner_radius=16,
                            command=lambda s=sid:self._step(s))
            b.pack(side="left",padx=4); self.step_btn.append((sid,b))

        # Tedaviye Geç butonu

        ctk.CTkButton(sf,text="🔬  Tedaviyə Keç",width=150,height=32,corner_radius=16,
                       fg_color=TH["ok"],hover_color=TH["ok_hover"],
                       text_color="white",
                       font=ctk.CTkFont(size=12,weight="bold"),
                       command=self._tedaviye_kec).pack(side="left",padx=(14,4))

        ctk.CTkButton(sf,text="🥽  VR Düzenle",width=120,height=32,corner_radius=16,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       text_color="white",
                       font=ctk.CTkFont(size=12,weight="bold"),
                       command=self._vr_duzenle).pack(side="left",padx=(12,2))

        ctk.CTkButton(sf,text="Layout Kaydet (VR)",width=150,height=32,corner_radius=16,
                       fg_color=TH["indigo"],hover_color=TH["indigo_hover"],
                       text_color="white",
                       font=ctk.CTkFont(size=11,weight="bold"),
                       command=self._vr_layout_kaydet).pack(side="left",padx=(2,4))

        self.tab = ctk.CTkTabview(self.icerik,fg_color="transparent",
                                   segmented_button_fg_color=TH["accent_soft"],
                                   segmented_button_selected_color=TH["accent"],
                                   segmented_button_selected_hover_color=TH["accent_hover"],
                                   segmented_button_unselected_color="#8FB0DC",
                                   segmented_button_unselected_hover_color="#7FA0CE",
                                   text_color="white")
        self.tab.grid(row=1,column=0,sticky="nsew",padx=10,pady=(0,10))
        self.pI = self.tab.add("İlkin Vəziyyət")
        self.p0 = self.tab.add("Plan")
        self.p1 = self.tab.add("Müalicə")
        self.p2 = self.tab.add("Not Defteri")
        self.p3 = self.tab.add("Röntgen")
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
        self.arch2=ArchCanvas(arch_f,self._tikla2,on_range=self._tikla2_range)
        self.arch2.band_label = "Diş Daşı"
        self.arch2.ilkin = True
        self.arch2.pack(side="left",anchor="n",padx=10,pady=8)

        infoI_f=ctk.CTkFrame(arch_f,fg_color=TH["panel"],width=220,corner_radius=12)
        infoI_f.pack(side="left",fill="y",padx=(0,8),pady=8)
        infoI_f.pack_propagate(False)
        ctk.CTkLabel(infoI_f,text="SEÇİLİ DİŞ",
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.dis_info2=ctk.CTkLabel(infoI_f,text="—",font=ctk.CTkFont(size=14,weight="bold"),
                                     text_color=TH["txt"],wraplength=200)
        self.dis_info2.pack(pady=4)
        ctk.CTkLabel(infoI_f,text="İLKİN VƏZİYYƏT",
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.plan_liste2=ctk.CTkScrollableFrame(infoI_f,fg_color="transparent")
        self.plan_liste2.pack(fill="both",expand=True,padx=6,pady=4)

        foto_fI=ctk.CTkFrame(arch_f,fg_color=TH["panel"],corner_radius=12)
        foto_fI.pack(side="left",fill="both",expand=True,padx=(0,8),pady=8)
        ctk.CTkLabel(foto_fI,text="İNTRAORAL VƏ ÜZ FOTOLARI",
                     font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["accent"]).pack(pady=(12,2),padx=10,anchor="w")
        ctk.CTkButton(foto_fI,text="+ Foto Əlavə Et",height=28,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       command=lambda: self._foto_ekle("ilkin")).pack(padx=10,pady=(2,4),anchor="w")
        self.foto_scroll_pI=ctk.CTkScrollableFrame(foto_fI,fg_color="transparent")
        self.foto_scroll_pI.pack(fill="both",expand=True,padx=4,pady=(0,4))

    def _build_p0(self):
        self.p0.grid_columnconfigure(0,weight=1)
        self.p0.grid_rowconfigure(1,weight=1)
        form = ctk.CTkFrame(self.p0,fg_color=TH["panel"],corner_radius=12)
        form.grid(row=0,column=0,sticky="ew",padx=8,pady=(6,4))
        form.grid_columnconfigure((0,1,2,3,4),weight=1)
        for i,(lbl,attr) in enumerate([("Ad Soyad","p0_ad"),("Yaş","p0_yas"),
                ("Telefon","p0_tel"),("TC/ID","p0_tc"),("Qeyd","p0_not")]):
            ctk.CTkLabel(form,text=lbl,font=ctk.CTkFont(size=11,weight="bold"),
                         text_color=TH["txt_sub"]).grid(row=0,column=i,padx=8,pady=(8,2),sticky="w")
            e=ctk.CTkEntry(form,placeholder_text=lbl,height=32,corner_radius=8,
                           border_color=TH["accent_soft"],fg_color=TH["panel_soft"])
            e.grid(row=1,column=i,padx=8,pady=(0,8),sticky="ew")
            setattr(self,attr,e)
        kb=ctk.CTkButton(form,text="💾 Saxla",width=90,height=32,corner_radius=8,
                          fg_color=TH["accent"],hover_color=TH["accent_hover"],
                          font=ctk.CTkFont(size=12,weight="bold"),command=self._kaydet)
        kb.grid(row=1,column=5,padx=8,pady=(0,8)); self.p0_kaydet=kb

        content=ctk.CTkFrame(self.p0,fg_color="transparent")
        content.grid(row=1,column=0,sticky="nsew",padx=4,pady=4)
        content.grid_columnconfigure(0,weight=1); content.grid_rowconfigure(1,weight=1)
        self._tbar(content, 0, tab="plan")
        arch_f=ctk.CTkFrame(content,fg_color="transparent")
        arch_f.grid(row=1,column=0,sticky="nsew")

        self.arch0=ArchCanvas(arch_f,self._tikla0,on_range=self._tikla0_range)
        self.arch0.pack(side="left",anchor="n",padx=10,pady=8)

        info_f=ctk.CTkFrame(arch_f,fg_color=TH["panel"],width=220,corner_radius=12)
        info_f.pack(side="left",fill="y",padx=(0,8),pady=8)
        info_f.pack_propagate(False)
        ctk.CTkLabel(info_f,text="SEÇİLİ DİŞ",
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.dis_info=ctk.CTkLabel(info_f,text="—",font=ctk.CTkFont(size=14,weight="bold"),
                                    text_color=TH["txt"],wraplength=200)
        self.dis_info.pack(pady=4)
        ctk.CTkLabel(info_f,text="PLANLANMIŞ",
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.plan_liste=ctk.CTkScrollableFrame(info_f,fg_color="transparent")
        self.plan_liste.pack(fill="both",expand=True,padx=6,pady=4)

        foto_f=ctk.CTkFrame(arch_f,fg_color=TH["panel"],corner_radius=12)
        foto_f.pack(side="left",fill="both",expand=True,padx=(0,8),pady=8)
        ctk.CTkLabel(foto_f,text="İNTRAORAL VƏ ÜZ FOTOLARI",
                     font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["accent"]).pack(pady=(12,2),padx=10,anchor="w")
        ctk.CTkButton(foto_f,text="+ Foto Əlavə Et",height=28,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       command=lambda: self._foto_ekle("plan")).pack(padx=10,pady=(2,4),anchor="w")
        self.foto_scroll_p0=ctk.CTkScrollableFrame(foto_f,fg_color="transparent")
        self.foto_scroll_p0.pack(fill="both",expand=True,padx=4,pady=(0,4))

    def _build_p1(self):
        self.p1.grid_columnconfigure(0,weight=1); self.p1.grid_rowconfigure(0,weight=1)
        content=ctk.CTkFrame(self.p1,fg_color="transparent")
        content.grid(row=0,column=0,sticky="nsew",padx=4,pady=4)
        content.grid_columnconfigure(0,weight=1); content.grid_rowconfigure(1,weight=1)
        self._tbar(content, 0, tab="tedavi")
        arch_f=ctk.CTkFrame(content,fg_color="transparent")
        arch_f.grid(row=1,column=0,sticky="nsew")
        self.arch1=ArchCanvas(arch_f,self._tikla1,on_range=self._tikla1_range)
        self.arch1.pack(side="left",anchor="n",padx=10,pady=8)

        info1_f=ctk.CTkFrame(arch_f,fg_color=TH["panel"],width=220,corner_radius=12)
        info1_f.pack(side="left",fill="y",padx=(0,8),pady=8)
        info1_f.pack_propagate(False)
        ctk.CTkLabel(info1_f,text="SEÇİLİ DİŞ",
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.dis_info1=ctk.CTkLabel(info1_f,text="—",font=ctk.CTkFont(size=14,weight="bold"),
                                     text_color=TH["txt"],wraplength=200)
        self.dis_info1.pack(pady=4)
        ctk.CTkLabel(info1_f,text="PLANLANMIŞ",
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).pack(pady=(16,4))
        self.plan_liste1=ctk.CTkScrollableFrame(info1_f,fg_color="transparent")
        self.plan_liste1.pack(fill="both",expand=True,padx=6,pady=4)

        foto_f1=ctk.CTkFrame(arch_f,fg_color=TH["panel"],corner_radius=12)
        foto_f1.pack(side="left",fill="both",expand=True,padx=(0,8),pady=8)
        ctk.CTkLabel(foto_f1,text="İNTRAORAL VƏ ÜZ FOTOLARI",
                     font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["accent"]).pack(pady=(12,2),padx=10,anchor="w")
        ctk.CTkButton(foto_f1,text="+ Foto Əlavə Et",height=28,corner_radius=8,
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
        self.not_baslik=ctk.CTkEntry(bar,placeholder_text="Not başlığı...",height=32,
                                      corner_radius=8,border_color=TH["accent_soft"],
                                      fg_color=TH["panel_soft"])
        self.not_baslik.grid(row=0,column=0,padx=10,pady=10,sticky="ew")
        self.not_ekle_btn=ctk.CTkButton(bar,text="+ Not Ekle",width=110,height=32,
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
        ctk.CTkLabel(alt,text="YENİ NOT",
                     font=ctk.CTkFont(size=11,weight="bold"),text_color=TH["accent"]).grid(
                     row=0,column=0,padx=10,pady=(8,2),sticky="w")
        self.not_metin=ctk.CTkTextbox(alt,height=80,corner_radius=8,
                                       border_width=1,border_color=TH["accent_soft"],
                                       fg_color=TH["panel_soft"])
        self.not_metin.grid(row=1,column=0,columnspan=2,padx=10,pady=(0,10),sticky="ew")

    def _not_ekle(self):
        try:
            if not self.aktif_hid:
                tk.messagebox.showwarning("Uyarı", "Önce bir hasta seçin.")
                return
            baslik = self.not_baslik.get().strip()
            metin  = self.not_metin.get("1.0", "end").strip()
            if not metin:
                tk.messagebox.showwarning("Uyarı", "Not metni boş olamaz.")
                return
            h = self.hastalar[self.aktif_hid]
            h.setdefault("notlar_liste", []).append({
                "tarih":  datetime.now().strftime("%d.%m.%Y %H:%M"),
                "baslik": baslik or "Not",
                "metin":  metin,
            })
            veri_kaydet(self.hastalar)
            self.not_baslik.delete(0, "end")
            self.not_metin.delete("1.0", "end")
            self._notlar_guncelle()
            self.not_ekle_btn.configure(text="Eklendi ✓", fg_color=TH["ok"])
            self.after(2000, lambda: self.not_ekle_btn.configure(
                text="+ Not Ekle", fg_color=TH["accent"]))
        except Exception as exc:
            import traceback
            tk.messagebox.showerror("Not Ekle Hatası", traceback.format_exc())

    def _notlar_guncelle(self):
        for w in self.not_scroll.winfo_children(): w.destroy()
        if not self.aktif_hid: return
        h=self.hastalar[self.aktif_hid]
        notlar=h.get("notlar_liste",[])
        if isinstance(notlar,str): notlar=[{"tarih":"","baslik":"Not","metin":notlar}]
        for i,not_ in enumerate(reversed(notlar)):
            kart=ctk.CTkFrame(self.not_scroll,fg_color="white",
                               corner_radius=8)
            kart.pack(fill="x",padx=4,pady=4)
            kart.grid_columnconfigure(0,weight=1)
            # Başlık + tarih
            baslik_f=ctk.CTkFrame(kart,fg_color=TH["accent_soft"],corner_radius=8)
            baslik_f.pack(fill="x",padx=8,pady=(8,4))
            ctk.CTkLabel(baslik_f,
                         text=not_.get("baslik","Not"),
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
            ctk.CTkButton(kart,text="Sil",width=50,height=22,
                           fg_color="#ffeeee",text_color="#cc3333",
                           hover_color="#ffcccc",
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

    def _build_rontgen(self):
        self.p3.grid_columnconfigure(0,weight=1)
        self.p3.grid_rowconfigure(1,weight=1)

        bar=ctk.CTkFrame(self.p3,fg_color=TH["panel"],corner_radius=12)
        bar.grid(row=0,column=0,sticky="ew",padx=8,pady=(6,4))
        ctk.CTkButton(bar,text="+ Röntgen Ekle",height=34,corner_radius=8,
                       fg_color=TH["accent"],hover_color=TH["accent_hover"],
                       font=ctk.CTkFont(size=12,weight="bold"),
                       command=self._rontgen_ekle).pack(side="left",padx=10,pady=10)
        ctk.CTkLabel(bar,text="PNG, JPG, JPEG, BMP desteklenir",
                     font=ctk.CTkFont(size=11),text_color=TH["txt_sub"]).pack(
                     side="left",padx=6)

        self.rontgen_scroll=ctk.CTkScrollableFrame(self.p3,fg_color="transparent")
        self.rontgen_scroll.grid(row=1,column=0,sticky="nsew",padx=8,pady=4)

    def _rontgen_ekle(self):
        if not self.aktif_hid: return
        from tkinter import filedialog
        dosya=filedialog.askopenfilename(
            title="Röntgen Seç",
            filetypes=[("Görüntü","*.png *.jpg *.jpeg *.bmp"),("Tüm","*.*")])
        if not dosya: return
        import base64
        from datetime import datetime
        with open(dosya,"rb") as f:
            b64=base64.b64encode(f.read()).decode()
        h=self.hastalar[self.aktif_hid]
        h.setdefault("rontgenler",[]).append({
            "tarih":datetime.now().strftime("%d.%m.%Y %H:%M"),
            "dosya":os.path.basename(dosya),
            "data":b64
        })
        veri_kaydet(self.hastalar)
        self._auto_bxd_backup(self.aktif_hid)
        self._rontgen_guncelle()

    def _rontgen_guncelle(self):
        for w in self.rontgen_scroll.winfo_children(): w.destroy()
        if not self.aktif_hid: return
        h = self.hastalar[self.aktif_hid]
        rontgenler = h.get("rontgenler", [])
        if not rontgenler:
            ctk.CTkLabel(self.rontgen_scroll,
                         text="Henüz röntgen eklenmedi",
                         text_color="#aaa").pack(pady=20)
            return

        COLS   = 5     # images per row
        THUMB  = 140   # thumbnail max-width in pixels
        RADIUS = 12    # rounded corner radius

        from PIL import Image, ImageDraw, ImageTk
        from io import BytesIO

        def _rounded(img: Image.Image, r: int) -> ImageTk.PhotoImage:
            """Return an ImageTk.PhotoImage with rounded corners (white bg)."""
            w, h = img.size
            mask = Image.new("L", (w, h), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
            out = Image.new("RGBA", (w, h), (255, 255, 255, 0))
            out.paste(img.convert("RGBA"), mask=mask)
            # Composite onto white so tk.Label bg="white" blends seamlessly
            bg = Image.new("RGBA", (w, h), (255, 255, 255, 255))
            bg.paste(out, mask=out.split()[3])
            return ImageTk.PhotoImage(bg.convert("RGB"))

        for row_start in range(0, len(rontgenler), COLS):
            row_f = ctk.CTkFrame(self.rontgen_scroll, fg_color="transparent")
            row_f.pack(fill="x", padx=2, pady=2)
            for col, (i, r) in enumerate(
                    ((row_start + c, rontgenler[row_start + c])
                     for c in range(COLS)
                     if row_start + c < len(rontgenler))):
                kart = ctk.CTkFrame(row_f, fg_color="white", corner_radius=10)
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
                    img_data = base64.b64decode(r["data"])
                    img = Image.open(BytesIO(img_data)).convert("RGB")
                    ratio = THUMB / img.width if img.width > THUMB else 1
                    img = img.resize((int(img.width * ratio),
                                      int(img.height * ratio)), Image.LANCZOS)
                    photo = _rounded(img, RADIUS)
                    lbl = tk.Label(kart, image=photo, bg="white", cursor="hand2",
                                   borderwidth=0, highlightthickness=0)
                    lbl.image = photo
                    lbl.pack(padx=6, pady=2)
                    lbl.bind("<Button-1>", lambda e, rd=r: self._rontgen_tam_goster(rd))
                except Exception as exc:
                    ctk.CTkLabel(kart, text=f"Yüklenemedi:\n{exc}",
                                 text_color="red",
                                 wraplength=THUMB).pack(padx=6, pady=4)
                # Delete button
                ctk.CTkButton(kart, text="Sil", width=44, height=20,
                               fg_color="#ffeeee", text_color="#cc3333",
                               hover_color="#ffcccc",
                               font=ctk.CTkFont(size=10),
                               command=lambda ix=i: self._rontgen_sil(ix)).pack(
                               anchor="e", padx=5, pady=(1, 5))

    def _rontgen_tam_goster(self, r):
        """Röntgeni tam ekran popup'ta göster"""
        import base64
        from PIL import Image, ImageTk
        from io import BytesIO

        pencere = tk.Toplevel(self)
        pencere.title(r.get("dosya","Röntgen"))
        pencere.configure(bg="black")

        # Ekran boyutunu al
        sw = pencere.winfo_screenwidth()
        sh = pencere.winfo_screenheight()

        img_data = base64.b64decode(r["data"])
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
        if not messagebox.askyesno("Sil", "Bu röntgeni silmek istədiyinizə əminsiniz?"):
            return
        h=self.hastalar[self.aktif_hid]
        r=h.get("rontgenler",[])
        if 0<=idx<len(r):
            r.pop(idx)
            veri_kaydet(self.hastalar)
            self._rontgen_guncelle()

    # ── FOTO (intraoral / üz) ─────────────────────────────────────────────────

    def _foto_ekle(self, tab):
        if not self.aktif_hid or not self.aktif_vid: return
        from tkinter import filedialog
        from datetime import datetime
        dosya = filedialog.askopenfilename(
            title="Foto Seç",
            filetypes=[("Görüntü","*.png *.jpg *.jpeg *.bmp"),("Tüm","*.*")])
        if not dosya: return
        with open(dosya,"rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        vaka = self.hastalar[self.aktif_hid]["vakalar"][self.aktif_vid]
        key  = {"plan": "fotolar_plan", "tedavi": "fotolar_tedavi",
                "ilkin": "fotolar_ilkin"}[tab]
        vaka.setdefault(key,[]).append({
            "tarih": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "dosya": os.path.basename(dosya),
            "data":  b64,
        })
        veri_kaydet(self.hastalar)
        self._foto_guncelle()

    def _foto_guncelle(self):
        for scroll in (self.foto_scroll_p0, self.foto_scroll_p1, self.foto_scroll_pI):
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
            bg = Image.new("RGBA",(w,h),(255,255,255,255))
            bg.paste(out, mask=out.split()[3])
            return ImageTk.PhotoImage(bg.convert("RGB"))

        def _render(scroll, fotolar, tab):
            if not fotolar:
                ctk.CTkLabel(scroll, text="Foto yoxdur",
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
                    kart = ctk.CTkFrame(row_f, fg_color="white", corner_radius=8)
                    kart.pack(side="left", padx=2, pady=2, anchor="n")
                    ctk.CTkLabel(kart, text=foto.get("dosya",""),
                                 font=ctk.CTkFont(size=9,weight="bold"),
                                 wraplength=THUMB).pack(padx=4,pady=(4,0))
                    ctk.CTkLabel(kart, text=foto.get("tarih",""),
                                 font=ctk.CTkFont(size=8),
                                 text_color="#999").pack(padx=4,pady=(0,2))
                    try:
                        img = Image.open(BytesIO(base64.b64decode(foto["data"]))).convert("RGB")
                        ratio = THUMB/img.width if img.width > THUMB else 1
                        img   = img.resize((int(img.width*ratio),int(img.height*ratio)),Image.LANCZOS)
                        photo = _rounded(img, RADIUS)
                        lbl   = tk.Label(kart, image=photo, bg="white", cursor="hand2",
                                         borderwidth=0, highlightthickness=0)
                        lbl.image = photo
                        lbl.pack(padx=4, pady=2)
                        lbl.bind("<Button-1>", lambda e, fd=foto: self._foto_tam_goster(fd))
                    except Exception as exc:
                        ctk.CTkLabel(kart, text=f"Yüklenemedi:\n{exc}",
                                     text_color="red",
                                     wraplength=THUMB).pack(padx=4,pady=4)
                    ctk.CTkButton(kart, text="Sil", width=40, height=18,
                                   fg_color="#ffeeee", text_color="#cc3333",
                                   hover_color="#ffcccc",
                                   font=ctk.CTkFont(size=9),
                                   command=lambda ix=i, t=tab: self._foto_sil(ix, t)).pack(
                                   anchor="e", padx=4, pady=(1,4))

        _render(self.foto_scroll_p0, vaka.get("fotolar_plan",   []), "plan")
        _render(self.foto_scroll_p1, vaka.get("fotolar_tedavi", []), "tedavi")
        _render(self.foto_scroll_pI, vaka.get("fotolar_ilkin",  []), "ilkin")

    def _foto_tam_goster(self, foto):
        from PIL import Image, ImageTk
        from io import BytesIO
        pencere = tk.Toplevel(self)
        pencere.title(foto.get("dosya","Foto"))
        pencere.configure(bg="black")
        sw = pencere.winfo_screenwidth()
        sh = pencere.winfo_screenheight()
        img   = Image.open(BytesIO(base64.b64decode(foto["data"]))).convert("RGB")
        ratio = min((sw-100)/img.width, (sh-100)/img.height, 1.0)
        img   = img.resize((int(img.width*ratio), int(img.height*ratio)), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        canvas = tk.Canvas(pencere, width=img.width, height=img.height,
                           bg="black", highlightthickness=0)
        canvas.pack()
        canvas.create_image(0,0,anchor="nw",image=photo)
        canvas._img = photo
        alt = tk.Frame(pencere, bg="#111")
        alt.pack(fill="x")
        tk.Label(alt, text=foto.get("dosya",""), fg="white", bg="#111",
                 font=("Arial",11)).pack(side="left",padx=10,pady=6)
        tk.Label(alt, text=foto.get("tarih",""), fg="#888", bg="#111",
                 font=("Arial",10)).pack(side="left",padx=4)
        tk.Button(alt, text="Kapat", command=pencere.destroy,
                  bg="#333", fg="white", relief="flat", padx=12).pack(
                  side="right", padx=10, pady=6)
        pencere.bind("<Escape>",   lambda e: pencere.destroy())
        pencere.bind("<Button-1>", lambda e: pencere.destroy())
        pencere.update_idletasks()
        pencere.geometry(f"+{(sw-pencere.winfo_width())//2}+{(sh-pencere.winfo_height())//2}")

    def _foto_sil(self, idx, tab):
        if not self.aktif_hid or not self.aktif_vid: return
        from tkinter import messagebox
        if not messagebox.askyesno("Sil", "Bu fotoğrafı silmek istədiyinizə əminsiniz?"):
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
        ctk.CTkLabel(tb,text="TEDAVİ",font=ctk.CTkFont(size=11,weight="bold"),
                     text_color=TH["accent"]).pack(side="left",padx=(12,8),pady=8)
        for tid,lbl,rgb in TEDAVILER:
            if tid in ("kok", "curuk") and tab != "ilkin": continue
            if tid == "gozlem" and tab == "ilkin": continue
            renk=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}" if rgb else "#888888"
            b=ctk.CTkButton(tb,text=lbl,width=76,height=28,corner_radius=14,
                             fg_color=tint(renk),hover_color=tint(renk,0.72),
                             text_color=renk,
                             font=ctk.CTkFont(size=12,weight="bold"),
                             command=lambda t=tid:self._tedavi_sec(t))
            b.pack(side="left",padx=3,pady=8)
            self.t_btnler.append((tid,b,renk))
        dt_txt = "Diş Daşı" if tab == "ilkin" else "Diş Daşı Təmizliyi"
        dt_btn=ctk.CTkButton(tb,text=dt_txt,width=90 if tab=="ilkin" else 150,height=28,corner_radius=14,
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
        adlar = {0: "Plan", 1: "Müalicə", 2: "İlkin Vəziyyət"}
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
        self.dis_info2.configure(text=f"Diş {n}\n{dis_adi(n)}\n{metin}")
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
            if metin=="Sağlıklı": continue
            row=ctk.CTkFrame(self.plan_liste2,fg_color="transparent")
            row.pack(fill="x",pady=2)
            ctk.CTkLabel(row,text=f"Diş {n_str}",width=52,
                         font=ctk.CTkFont(size=11,weight="bold"),
                         text_color="#333").pack(side="left")
            ctk.CTkLabel(row,text=metin,font=ctk.CTkFont(size=11),
                         text_color="#1565C0").pack(side="left",padx=4)

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
        self.dis_info.configure(text=f"Diş {n}\n{dis_adi(n)}\n{metin}")
        self._plan_guncelle()

    def _uygula_tedavi(self,n,zone=None):
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        tedavi = h["vakalar"][self.aktif_vid].setdefault("tedavi", {})
        mevcut = normalize_state(tedavi.get(str(n), {}))
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
        tedavi[str(n)] = mevcut
        veri_kaydet(self.hastalar)
        self.arch1.set_tedavi(n, mevcut)
        metin = dis_durum_str(mevcut)
        self.dis_info1.configure(text=f"Diş {n}\n{dis_adi(n)}\n{metin}")
        self._plan_guncelle1()

    def _plan_guncelle1(self):
        try:
            for w in self.plan_liste1.winfo_children(): w.destroy()
        except: pass
        if not self.aktif_hid or not self.aktif_vid: return
        h = self.hastalar[self.aktif_hid]
        vaka = h.get("vakalar",{}).get(self.aktif_vid,{})
        for n_str,durum in sorted(vaka.get("tedavi",{}).items(),key=lambda x:int(x[0])):
            if isinstance(durum,str): durum={"tac":durum,"kanal":False,"kron":False}
            metin = dis_durum_str(durum)
            if metin=="Sağlıklı": continue
            row=ctk.CTkFrame(self.plan_liste1,fg_color="transparent")
            row.pack(fill="x",pady=2)
            ctk.CTkLabel(row,text=f"Diş {n_str}",width=52,
                         font=ctk.CTkFont(size=11,weight="bold"),
                         text_color="#333").pack(side="left")
            ctk.CTkLabel(row,text=metin,font=ctk.CTkFont(size=11),
                         text_color="#1565C0").pack(side="left",padx=4)

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
            if metin=="Sağlıklı": continue
            row=ctk.CTkFrame(self.plan_liste,fg_color="transparent")
            row.pack(fill="x",pady=2)
            ctk.CTkLabel(row,text=f"Diş {n_str}",width=52,
                         font=ctk.CTkFont(size=11,weight="bold"),
                         text_color="#333").pack(side="left")
            ctk.CTkLabel(row,text=metin,font=ctk.CTkFont(size=11),
                         text_color="#1565C0").pack(side="left",padx=4)

    def _kaydet(self):
        if not self.aktif_hid: return
        h=self.hastalar[self.aktif_hid]
        for attr,key in [("p0_ad","ad"),("p0_yas","yas"),
                          ("p0_tel","tel"),("p0_tc","tc"),("p0_not","notlar_genel")]:
            h[key]=getattr(self,attr).get()
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
            self.dis_info.configure(text=f"Diş {n0}\n{dis_adi(n0)}\n{dis_durum_str(durum)}")
        else:
            self.arch0.secili = None
            self.dis_info.configure(text="—")
        if n1 is not None:
            self.arch1.secili = n1
            self.arch1.ciz()
            durum = normalize_state(tedavi_data.get(str(n1), {}))
            self.dis_info1.configure(text=f"Diş {n1}\n{dis_adi(n1)}\n{dis_durum_str(durum)}")
        else:
            self.arch1.secili = None
            self.dis_info1.configure(text="—")
        nI = self._last_dis_ilkin.get((hid, vid))
        if nI is None and ilkin_data:
            nI = int(min(ilkin_data.keys(), key=int))
        if nI is not None:
            self.arch2.secili = nI
            self.arch2.ciz()
            durum = normalize_state(ilkin_data.get(str(nI), {}))
            self.dis_info2.configure(text=f"Diş {nI}\n{dis_adi(nI)}\n{dis_durum_str(durum)}")
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
                    tedavi_lines.append(f"Diş {n_str}: {txt}")
            plan_str = "\n".join(tedavi_lines) if tedavi_lines else ""

            # Latest x-ray file path (if any rontgen saved as a file)
            rontgenler = h.get("rontgenler", [])
            xray_path  = ""
            for r in reversed(rontgenler):
                fp = r.get("dosya_tam", r.get("dosya", ""))
                if fp and os.path.exists(fp):
                    xray_path = fp
                    break

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

            if xray_path:
                with open(XRAY_PUSH_JSON, "w", encoding="utf-8") as f:
                    json.dump({"xray_path": xray_path,
                               "patient_name": h.get("ad", ""),
                               "done": False}, f, ensure_ascii=False)

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
        menu.add_command(label="➕  Yeni Vaka",
                          command=lambda: self._yeni_vaka(hid))
        menu.add_separator()
        menu.add_command(label="💾  Hastayı Kaydet",
                          command=lambda: self._hasta_kaydet_hizli(hid))
        menu.add_separator()
        menu.add_command(label="🗑  Hastayı Sil",
                          foreground="red",
                          command=lambda: self._hasta_sil(hid))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _vaka_menu(self, event, hid, vid):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="✏  Vaka Adını Değiştir",
                          command=lambda: self._vaka_yeniden_adlandir(hid,vid))
        menu.add_separator()
        menu.add_command(label="🗑  Vakayı Sil",
                          foreground="red",
                          command=lambda: self._vaka_sil(hid,vid))
        try: menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    def _yeni_vaka(self, hid):
        h = self.hastalar[hid]
        vid = yeni_id("vaka")
        vakalar = h.setdefault("vakalar", {})
        n = len(vakalar) + 1
        vakalar[vid] = {
            "ad": f"{n}. Vaka",
            "tarih": datetime.now().strftime("%d.%m.%Y"),
            "plan": {}, "tedavi": {}
        }
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

    def cloud_push_all(self, snapshot=None):
        """Yerel bütün hastaları buluda upsert edir (sətir-sətir insert/əks halda
        update) + bekleyen silmeleri işler.
        NOT: bulk 'Prefer: resolution=merge-duplicates' (ON CONFLICT DO UPDATE)
        Postgres RLS-də TƏZƏ sətirlər üçün belə UPDATE policy-nin USING şərtini
        yoxlayır və 403 verir (canlı sınaqla təsdiqləndi) — ona görə hər hasta
        üçün əvvəl sadə INSERT, 409 (unique_violation) gələrsə PATCH edilir."""
        uid = AYARLAR.get("sb_user_id")
        if not uid:
            raise RuntimeError("Bulud girişi edilməyib")
        data = snapshot if snapshot is not None else self.hastalar
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        for hid, h in data.items():
            row = {"hasta_id": hid, "owner": uid, "data": h, "updated_at": now}
            try:
                self._cloud_authed_request(
                    "POST", "/rest/v1/hastalar", body=[row],
                    extra_headers={"Prefer": "return=minimal"})
            except RuntimeError as e:
                if "HTTP 409" in str(e) or "23505" in str(e):
                    self._cloud_authed_request(
                        "PATCH", f"/rest/v1/hastalar?hasta_id=eq.{hid}",
                        body={"data": h, "updated_at": now},
                        extra_headers={"Prefer": "return=minimal"})
                else:
                    raise
        for hid in list(AYARLAR.get("silinen_hastalar", [])):
            try:
                self._cloud_authed_request("DELETE", f"/rest/v1/hastalar?hasta_id=eq.{hid}")
            except Exception as e:
                print(f"[Bulud] silme göndərilmədi ({hid}): {e}")
        AYARLAR["silinen_hastalar"] = []
        ayar_kaydet(AYARLAR)
        return len(data)

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
            # Böyük hastalar (rentgen/foto) tək bir push'un 20sn-dən çox sürməsinə
            # səbəb ola bilir — əl ilə push davam edərkən bura girərsə qıfıl tutula
            # bilmir, o zaman _CLOUD_DIRTY-ni False etmirik ki, növbəti tick yenidən
            # sınasın (əks halda bu dəyişiklik heç vaxt buluda getməzdi).
            if _CLOUD_SYNC_LOCK.acquire(blocking=False):
                _CLOUD_DIRTY = False
                snap = dict(self.hastalar)   # ana thread'də sığ kopya — arxa plan thread'i üçün
                threading.Thread(target=self._cloud_auto_sync_worker, args=(snap,), daemon=True).start()
            else:
                print("[Bulud] avtomatik sinxron keçildi — başqa sinxron davam edir")
        self.after(20000, self._cloud_auto_sync_tick)   # 20 saniyədə bir yoxla

    def _cloud_auto_sync_worker(self, snapshot):
        try:
            self.cloud_push_all(snapshot)
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
        dlg.title("Buluddan Hasta Seç")
        dlg.geometry("380x460")
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        ctk.CTkLabel(dlg, text="BULUDDAKI HASTALAR",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", padx=16, pady=(16, 2))
        if not names:
            ctk.CTkLabel(dlg, text="Buludda hasta tapılmadı.",
                         font=ctk.CTkFont(size=12), text_color=TH["txt_sub"]
                         ).pack(anchor="w", padx=16, pady=(4, 12))
            return
        arama_var = tk.StringVar()
        ctk.CTkEntry(dlg, textvariable=arama_var, placeholder_text="Ada görə axtar…",
                     height=32, border_color=TH["accent_soft"], fg_color="white"
                     ).pack(fill="x", padx=16, pady=(0, 8))
        liste = ctk.CTkScrollableFrame(dlg, fg_color="white", corner_radius=10)
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
                "Hastayı Sil",
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
                "Vakayı Sil",
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
            text="Yeni vaka adı:",
            title="Vaka Adını Değiştir")
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

    def _ayarlar_ac(self):
        from tkinter import filedialog
        import tkinter.messagebox
        win = ctk.CTkToplevel(self, fg_color=TH["bg"])
        win.title("Ayarlar")
        win.geometry("640x560")
        win.attributes("-topmost", True)
        win.grab_set()
        ctk.CTkLabel(win, text="RÖNTGEN KLASÖRÜ",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(18, 2))
        ctk.CTkLabel(win, text="Röntgen programının görüntüleri kaydettiği klasörü seç. Bu klasöre düşen yeni\n"
                               "görüntüler (bmp / jpg / png / tif" + (" / dcm" if _DCM_OK else "") + ") otomatik yakalanıp açık hastaya eklenir.",
                     justify="left", font=ctk.CTkFont(size=11),
                     text_color=TH["txt_sub"]).pack(anchor="w", padx=18)
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=10)
        yol_var = tk.StringVar(value=xray_watch_dir())
        ctk.CTkEntry(row, textvariable=yol_var, height=32,
                     border_color=TH["accent_soft"], fg_color="white").pack(side="left", fill="x", expand=True)
        def _gozat():
            d = filedialog.askdirectory(parent=win,
                                        initialdir=yol_var.get() if os.path.isdir(yol_var.get()) else "C:\\")
            if d:
                yol_var.set(os.path.normpath(d))
        ctk.CTkButton(row, text="📂 Gözat…", width=100, height=32,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"],
                      command=_gozat).pack(side="left", padx=(8, 0))
        alt_var = tk.BooleanVar(value=xray_subdir_only())
        ctk.CTkCheckBox(win, text="Yalnız alt klasörlere düşen dosyaları al (kökteki geçici dosyaları yoksay)",
                        variable=alt_var, font=ctk.CTkFont(size=11),
                        text_color=TH["txt"]).pack(anchor="w", padx=18, pady=(0, 6))
        durum = ctk.CTkLabel(win, text="", font=ctk.CTkFont(size=11), text_color=TH["ok"])
        durum.pack(anchor="w", padx=18)
        def _kaydet():
            yol = yol_var.get().strip()
            AYARLAR["xray_klasoru"] = yol
            AYARLAR["alt_klasor_sarti"] = bool(alt_var.get())
            ayar_kaydet(AYARLAR)
            self._restart_xray_watcher()
            if yol and not os.path.isdir(yol):
                durum.configure(text="⚠ Klasör şu an yok — yol kaydedildi, klasör oluşunca izlenmeye başlanacak.",
                                text_color="#C0392B")
            else:
                durum.configure(text="✓ Kaydedildi — klasör izleme yeniden başlatıldı.", text_color=TH["ok"])
        ctk.CTkButton(win, text="💾 Kaydet", width=120, height=34, corner_radius=10,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"],
                      font=ctk.CTkFont(size=12, weight="bold"),
                      command=_kaydet).pack(anchor="e", padx=18, pady=12)

        ctk.CTkFrame(win, height=1, fg_color="#3B5E8F").pack(fill="x", padx=18, pady=(2, 0))
        cloud_frame = ctk.CTkFrame(win, fg_color="transparent")
        cloud_frame.pack(fill="both", expand=True)

        def _cloud_section_build():
            for w in cloud_frame.winfo_children(): w.destroy()
            ctk.CTkLabel(cloud_frame, text="☁ BULUD SENKRONİZASYONU",
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=TH["accent"]).pack(anchor="w", padx=18, pady=(14, 2))
            if not cloud_configured():
                ctk.CTkLabel(cloud_frame,
                             text="Bulud sunucusu hələ qurulmayıb — proqram yalnız yerel işləyir.",
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
                if "timeout" in s.lower() or "Bağlantı xətası" in s or "URL ERROR" in s:
                    return "İnternet bağlantısı yoxdur, zəifdir, ya da bulud əlçatan deyil (vaxt bitdi)."
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
                    def work():
                        try:
                            with _CLOUD_SYNC_LOCK:   # avtomatik sinxronla üst-üstə düşməsin
                                n = self.cloud_push_all()
                            AYARLAR["sb_son_senkron"] = datetime.now().strftime("%H:%M")
                            ayar_kaydet(AYARLAR)
                            def ok():
                                _set_busy2(False)
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
                            "Buluddakı verilər yerel verilərin üzərinə yazılacaq "
                            "(hər iki tərəfdə eyni hasta varsa buludtakı qalacaq).\n\nDavam edilsin?",
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
                    ayar_kaydet(AYARLAR)
                    self._cloud_status_guncelle()
                    _cloud_section_build()

                push_btn = ctk.CTkButton(btn_row, text="☁ Buluda Yüklə", width=140, height=30,
                              fg_color=TH["accent"], hover_color=TH["accent_hover"],
                              command=_push)
                push_btn.pack(side="left", padx=(0, 6))
                pull_btn = ctk.CTkButton(btn_row, text="☁ Buluddan Yenilə", width=150, height=30,
                              fg_color=TH["indigo"], hover_color=TH["indigo_hover"],
                              command=_pull)
                pull_btn.pack(side="left", padx=(0, 6))
                pull_one_btn = ctk.CTkButton(btn_row, text="☁ Hasta Seç…", width=130, height=30,
                              fg_color="transparent", border_width=1, border_color=TH["indigo"],
                              text_color=TH["indigo"], hover_color=TH["accent_soft"],
                              command=_pull_one)
                pull_one_btn.pack(side="left", padx=(0, 6))
                logout_btn = ctk.CTkButton(btn_row, text="Çıxış", width=80, height=30,
                              fg_color="transparent", border_width=1, border_color="#3B5E8F",
                              text_color="#B9CDEB", command=_logout)
                logout_btn.pack(side="left")
                cstatus.pack(anchor="w", padx=18, pady=(6, 12))
            else:
                ctk.CTkLabel(cloud_frame,
                             text="Hekim hesabı ilə giriş edin — eyni hesabla giriş edilən bütün\n"
                                  "kompüterlər hasta verilərini avtomatik paylaşır.",
                             justify="left", font=ctk.CTkFont(size=11),
                             text_color=TH["txt_sub"]).pack(anchor="w", padx=18, pady=(0, 8))
                erow = ctk.CTkFrame(cloud_frame, fg_color="transparent")
                erow.pack(fill="x", padx=18, pady=(0, 4))
                email_var = tk.StringVar(value=AYARLAR.get("sb_email", ""))
                pass_var = tk.StringVar()
                ctk.CTkEntry(erow, textvariable=email_var, placeholder_text="E-poçt", height=32,
                             border_color=TH["accent_soft"], fg_color="white"
                             ).pack(side="left", fill="x", expand=True, padx=(0, 6))
                ctk.CTkEntry(erow, textvariable=pass_var, placeholder_text="Şifrə", show="•", height=32,
                             border_color=TH["accent_soft"], fg_color="white", width=140
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
                login_btn = ctk.CTkButton(brow, text="Giriş Et", width=100, height=30,
                              fg_color=TH["accent"], hover_color=TH["accent_hover"],
                              command=_login)
                login_btn.pack(side="left", padx=(0, 6))
                signup_btn = ctk.CTkButton(brow, text="Hesab Yarat", width=110, height=30,
                              fg_color="transparent", border_width=1, border_color="#3B5E8F",
                              text_color="#B9CDEB", command=_signup)
                signup_btn.pack(side="left")
                cstatus.pack(anchor="w", padx=18, pady=(6, 12))

        _cloud_section_build()

        ctk.CTkFrame(win, height=1, fg_color="#3B5E8F").pack(fill="x", padx=18, pady=(6, 0))
        guncelleme_row = ctk.CTkFrame(win, fg_color="transparent")
        guncelleme_row.pack(fill="x", padx=18, pady=(10, 14))
        ctk.CTkLabel(guncelleme_row, text=f"Sürüm: v{APP_VERSION}",
                     font=ctk.CTkFont(size=11), text_color=TH["txt_sub"]).pack(side="left")
        ctk.CTkButton(guncelleme_row, text="🔄 Güncellemeleri Kontrol Et", width=200, height=30,
                      fg_color=TH["accent"], hover_color=TH["accent_hover"],
                      command=lambda: self._guncelleme_kontrol_et(sessiz=False)
                      ).pack(side="right")

    def _yeni_hasta(self):
        d=ctk.CTkInputDialog(text="Hasta adı:",title="Yeni Hasta")
        ad=d.get_input()
        if ad:
            hid = yeni_id("hasta")
            vid = yeni_id("vaka")
            self.hastalar[hid] = {
                "ad": ad,
                "vakalar": {
                    vid: {
                        "ad": "1. Vaka",
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
                "Kaydedildi",
                f"'{ad}' yedəkləndi və bütün hastalar buludla sinxronlaşdırılır…")
        else:
            tkinter.messagebox.showinfo(
                "Kaydedildi", f"'{ad}' yedəkləndi:\n{self._BXD_BASE}")

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
            tkinter.messagebox.showerror("Yükleme Hatası", f"Dosya okunamadı:\n{exc}")
            return
        if payload.get("format") != "bxd" or "hasta" not in payload:
            tkinter.messagebox.showerror("Geçersiz Dosya",
                                         "Bu geçerli bir .bxd hasta dosyası değil.")
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
        tkinter.messagebox.showinfo("Yüklendi",
                                    f"'{h.get('ad', '')}' hastası başarıyla yüklendi.")

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
        dlg.title("Yeni Röntgen Algılandı")
        dlg.geometry("480x230")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()

        ctk.CTkLabel(dlg,
                     text="🦷  Yeni Röntgen Algılandı",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="#1565C0").pack(pady=(18, 2))
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
                      text="✓  Evet, Ekle",
                      width=170, height=36,
                      fg_color="#1565C0", hover_color="#0D47A1",
                      font=ctk.CTkFont(weight="bold"),
                      command=on_yes).pack(side="left", padx=8)
        ctk.CTkButton(btn_row,
                      text="✗  Hayır, İptal",
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
                    self.tab.set("Röntgen")
                except Exception:
                    pass
            # Push raw file path to VR
            self._push_xray_to_vr(filepath, hid)
        except Exception as e:
            tk.messagebox.showerror("Hata", f"Röntgen eklenemedi:\n{e}")

    def _push_xray_to_vr(self, filepath: str, hid: str):
        """Write XRAY_PUSH_JSON so BP_XRayPanel's timer picks up the new image."""
        try:
            os.makedirs(os.path.dirname(XRAY_PUSH_JSON), exist_ok=True)
            payload = {
                "xray_path":    filepath.replace("\\", "/"),
                "patient_name": self.hastalar[hid].get("ad", ""),
                "timestamp":    datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                "done":         False,
            }
            with open(XRAY_PUSH_JSON, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[XRay] push_xray_to_vr error: {e}")

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
                "VR Layout Editörü",
                "Paketlenmiş uygulama başlatılıyor...\n\n"
                "1. Uygulama yüklenince 'VR Düzenle' modu aktif olur.\n"
                "2. Üç paneli istediğiniz konuma taşıyın.\n"
                "3. Hazır olunca 'Layout Kaydet (VR)' butonuna basın.\n\n"
                f"({self._UE5_EXE})"
            )
        else:
            mb.showwarning(
                "VR Layout Editörü",
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
                "Layout Kaydedildi",
                "VR layout başarıyla kaydedildi!\n"
                "C:\\BlueX\\vr_loadout.json güncellendi.\n\n"
                f"{text[:300] or '(UE5 yanıtı alındı)'}"
            )

        except urllib.error.URLError:
            mb.showerror(
                "Bağlantı Hatası",
                "UE5 VibeUE sunucusuna bağlanılamadı.\n\n"
                "UE5'in açık ve PIE modunda olduğunu kontrol edin.\n\n"
                "Manuel olarak UE5 Output Log'unda çalıştırın:\n"
                "py C:/BlueX/vr_save_layout.py"
            )
        except Exception as e:
            mb.showerror(
                "Hata",
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
                        mb.showinfo("Güncelleme Kontrolü",
                                     f"Güncelleme kontrol edilemedi:\n{err}")
                    else:
                        mb.showinfo("Güncelleme Kontrolü",
                                     f"En güncel sürümü kullanıyorsunuz (v{APP_VERSION}).")
            self.after(0, ui)
        threading.Thread(target=work, daemon=True).start()

    def _guncelleme_dialog(self, info):
        yeni_surum = info.get("surum", "?")
        url = info.get("indirme_url", "")
        notlar = info.get("notlar") or ""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Yeni Sürüm Var")
        dlg.geometry("440x300")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        ctk.CTkLabel(dlg, text="🚀  Yeni Sürüm Mevcut",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="#1565C0").pack(pady=(18, 4))
        ctk.CTkLabel(dlg, text=f"Mevcut sürüm: v{APP_VERSION}   →   Yeni sürüm: v{yeni_surum}",
                     font=ctk.CTkFont(size=12), text_color="#333").pack()
        if notlar:
            ctk.CTkLabel(dlg, text=notlar, font=ctk.CTkFont(size=11), text_color="#666",
                         justify="left", wraplength=380).pack(pady=(10, 0), padx=20)
        durum = ctk.CTkLabel(dlg, text="", font=ctk.CTkFont(size=11), text_color="#1565C0")
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

        indir_btn = ctk.CTkButton(btn_row, text="⬇  İndir ve Kur", width=170, height=36,
                                   fg_color="#1565C0", hover_color="#0D47A1",
                                   font=ctk.CTkFont(weight="bold"), command=_indir)
        indir_btn.pack(side="left", padx=8)
        sonra_btn = ctk.CTkButton(btn_row, text="Daha Sonra", width=140, height=36,
                                   fg_color="#888", hover_color="#555", command=_sonra)
        sonra_btn.pack(side="left", padx=8)

    def _guncelleme_indir_kur(self, url, durum_lbl, dlg):
        def work():
            import urllib.request, tempfile, subprocess
            try:
                tmp_path = os.path.join(tempfile.gettempdir(), "BlueXDental_Setup_update.exe")
                req = urllib.request.Request(url, headers={"User-Agent": "BlueXDental-Updater"})
                with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_path, "wb") as f:
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
                                text=f"İndiriliyor… %{y}", text_color="#1565C0"))
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

    def _on_close(self):
        """Stop background watcher thread before destroying the window."""
        if self._xray_observer and self._xray_observer.is_alive():
            self._xray_observer.stop()
            self._xray_observer.join(timeout=2)
        self.destroy()


if __name__=="__main__":
    if "--net-test" in sys.argv:   # GECICI DEBUG - bulud baglanti sinamasi, isim bitince silinecek
        print("=== NET TEST BASLADI ===")
        print("SUPABASE_URL:", SUPABASE_URL)
        try:
            r = _sb_http("GET", "/rest/v1/hastalar?select=hasta_id&limit=1")
            print("SONUC: UGURLU:", r)
        except Exception as e:
            print("SONUC: XETA:", type(e).__name__, repr(e))
        print("=== NET TEST BITDI ===")
        sys.exit(0)
    app=DentalApp()
    app.mainloop()
