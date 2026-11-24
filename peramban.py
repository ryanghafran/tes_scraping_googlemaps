"""
peramban.py
===========
Pengelolaan browser Chrome (Selenium):
  * membuat Chrome dengan opsi yang benar (profil khusus, tanpa UA palsu, anti-throttling)
  * mendeteksi sesi browser yang MATI agar bisa dibuka ulang (versi lama: satu crash =
    39 tempat gagal berturut-turut dengan 'invalid session id')
  * mendeteksi halangan Google (CAPTCHA 'unusual traffic', halaman consent)
  * login Google manual di profil scraper (Google mewajibkan login untuk membuka ulasan lengkap)
  * menyimpan screenshot + HTML saat ada masalah, supaya penyebabnya bisa didiagnosis
"""

from __future__ import annotations

import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

import konfigurasi as cfg

log = logging.getLogger(__name__)


# ─── Exception khusus ─────────────────────────────────────────────────────────

class DiblokirGoogleError(Exception):
    """Google menampilkan CAPTCHA / 'lalu lintas tidak biasa' dan tidak terselesaikan."""


class ChromeTidakBisaDibukaError(RuntimeError):
    """Chrome gagal dibuka dengan profil scraper (biasanya profil masih dipakai proses Chrome lain)."""


class LoginDiperlukanError(Exception):
    """Google meminta login ('Login untuk menikmati fitur terbaik...') sebelum ulasan lengkap bisa dibuka."""


PESAN_LOGIN = ("Google meminta LOGIN untuk membuka ulasan lengkap (tanpa login hanya ±3-5 ulasan preview per tempat "
               "yang terlihat). Jalankan:  python scraper.py --login   lalu jalankan scraper lagi.")


# ─── Deteksi sesi browser mati ────────────────────────────────────────────────

_NAMA_ERROR_KONEKSI = {
    "MaxRetryError", "NewConnectionError", "ProtocolError", "RemoteDisconnected",
    "ConnectionResetError", "ConnectionRefusedError", "ConnectionAbortedError",
}
_PESAN_SESI_MATI = (
    "invalid session id", "no such window", "target window already closed", "chrome not reachable",
    "disconnected: not connected to devtools", "session deleted", "web view not found",
    "connection refused", "max retries exceeded", "connection aborted", "forcibly closed",
    "tab crashed", "target closed",
)


def sesi_mati(exc: BaseException) -> bool:
    """True jika exception berarti jendela/proses Chrome sudah tidak ada (perlu dibuka ulang)."""
    if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)):
        return True
    if type(exc).__name__ in _NAMA_ERROR_KONEKSI:
        return True
    pesan = str(exc).lower()
    return any(k in pesan for k in _PESAN_SESI_MATI)


def ringkas_error(exc: BaseException, maks: int = 220) -> str:
    """Pesan error satu baris tanpa stacktrace chromedriver yang panjang."""
    pesan = str(exc).split("Stacktrace:")[0]
    pesan = re.sub(r"For documentation on this error.*", "", pesan, flags=re.S)
    pesan = re.sub(r"\s+", " ", pesan).strip()
    return f"{type(exc).__name__}: {pesan}"[:maks]


# ─── Deteksi halangan Google ──────────────────────────────────────────────────

_JS_HALAMAN = """
return {url: location.href,
        teks: ((document.body && document.body.innerText) || '').slice(0, 1500),
        ada_panel: !!document.querySelector('div[role="main"], h1.DUwDvf, a.hfpxzc')};
"""

_POLA_CAPTCHA = ("lalu lintas yang tidak biasa", "unusual traffic", "bukan robot", "not a robot",
                 "verifikasi bahwa anda manusia", "verify you're human", "verify that you're human")
_POLA_CONSENT = ("sebelum anda melanjutkan", "before you continue")


def deteksi_halangan(driver) -> str | None:
    """Returns 'captcha', 'consent', atau None."""
    try:
        info = driver.execute_script(_JS_HALAMAN)
    except WebDriverException as e:
        if sesi_mati(e):
            raise
        return None
    url = (info.get("url") or "").lower()
    teks = (info.get("teks") or "").lower()
    if "/sorry/" in url or "recaptcha" in url:
        return "captcha"
    if "consent." in url:
        return "consent"
    if not info.get("ada_panel"):                    # teks hanya dipercaya jika panel Maps tidak ada
        if any(k in teks for k in _POLA_CAPTCHA):
            return "captcha"
        if any(k in teks for k in _POLA_CONSENT):
            return "consent"
    return None


_JS_KLIK_CONSENT = """
var pola = arguments[0];
var els = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]'));
for (var i = 0; i < pola.length; i++) {
  var re = new RegExp('^\\\\s*' + pola[i] + '\\\\s*$', 'i');
  for (var j = 0; j < els.length; j++) {
    var t = (els[j].innerText || els[j].value || els[j].getAttribute('aria-label') || '');
    if (re.test(t)) { els[j].click(); return t.trim(); }
  }
}
return '';
"""


# ─── Proses Chrome yang memakai profil scraper ────────────────────────────────
# "session not created: Chrome instance exited" hampir selalu berarti folder profil MASIH DIPAKAI proses Chrome
# lain (jendela login yang belum benar-benar tertutup, atau sisa Chrome dari jalan sebelumnya yang crash):
# Chrome kedua menyerahkan permintaannya ke yang pertama lalu langsung keluar.

_TANDA_PROFIL_TERKUNCI = ("user data directory", "devtoolsactiveport", "chrome failed to start",
                          "chrome instance exited", "crashed")


def tanda_profil_terkunci(exc: BaseException) -> bool:
    """True jika pesan error saat membuka Chrome khas profil yang sedang dipakai proses Chrome lain."""
    pesan = str(exc).lower()
    return any(k in pesan for k in _TANDA_PROFIL_TERKUNCI)


def _tanpa_jendela() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def proses_chrome_profil(profil: str) -> list:
    """
    [(pid, keterangan)] proses Chrome yang dijalankan dengan --user-data-dir=<profil>.
    Hanya Chrome milik scraper yang cocok; Chrome biasa Anda (profil lain) tidak tersentuh.
    Mengembalikan [] bila tidak ada ATAU tidak bisa diperiksa.
    """
    try:
        if os.name == "nt":
            skrip = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'chrome.exe' -and $_.CommandLine -and "
                     "$_.CommandLine.IndexOf($env:SCRAPER_PROFIL, [StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
                     "ForEach-Object { $_.ProcessId }")
            hasil = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", skrip],
                                   capture_output=True, text=True, timeout=30,
                                   env=dict(os.environ, SCRAPER_PROFIL=profil), creationflags=_tanpa_jendela())
            return [(int(x), "chrome.exe") for x in hasil.stdout.split() if x.isdigit()]
        hasil = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=15)
        diri, out = {os.getpid(), os.getppid()}, []
        for baris in hasil.stdout.splitlines()[1:]:
            bagian = baris.strip().split(None, 1)
            if len(bagian) == 2 and bagian[0].isdigit() and int(bagian[0]) not in diri:
                if profil in bagian[1] and "chrom" in bagian[1].lower():
                    out.append((int(bagian[0]), bagian[1][:60]))
        return out
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def matikan_proses_profil(profil: str) -> int:
    """Menghentikan paksa proses Chrome yang memakai profil scraper. Returns jumlah proses yang dicoba dimatikan."""
    pids = [p for p, _ in proses_chrome_profil(profil)]
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20,
                               creationflags=_tanpa_jendela())
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
    if pids:
        time.sleep(2)
    return len(pids)


def _daftar_pid(pids: list) -> str:
    return ", ".join(str(p) for p, _ in pids[:8]) + (" ..." if len(pids) > 8 else "")


def _terminal_interaktif() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


# ─── Kelas Peramban ───────────────────────────────────────────────────────────

class Peramban:
    """Pembungkus Chrome WebDriver dengan pemulihan otomatis."""

    def __init__(self, headless: bool | None = None):
        self.headless = cfg.HEADLESS if headless is None else headless
        self.driver = None
        self.faktor_lambat = 1.0          # naik otomatis saat ada tanda-tanda dibatasi Google
        self._jumlah_debug = 0
        self._path_driver = None          # hasil webdriver-manager (di-cache; '' = pakai Selenium Manager)

    # -- siklus hidup ---------------------------------------------------------
    def _opsi(self, profil: str) -> Options:
        o = Options()
        if self.headless:
            o.add_argument("--headless=new")
        if cfg.CHROME_BINARY:
            o.binary_location = cfg.CHROME_BINARY
        o.add_argument(f"--user-data-dir={profil}")
        o.add_argument("--profile-directory=Default")     # profil yang sama dengan jendela login; tanpa profile picker
        o.add_argument("--window-size=1366,900")
        o.add_argument("--lang=id-ID")
        o.add_argument("--disable-blink-features=AutomationControlled")
        o.add_argument("--disable-notifications")
        o.add_argument("--no-first-run")
        o.add_argument("--no-default-browser-check")
        # Jendela yang tertutup aplikasi lain / di latar belakang TIDAK boleh 'tertidur',
        # kalau tidak lazy-load ulasan berhenti dan scroll tampak macet.
        o.add_argument("--disable-background-timer-throttling")
        o.add_argument("--disable-renderer-backgrounding")
        o.add_argument("--disable-backgrounding-occluded-windows")
        if os.name != "nt":                      # hanya Linux/Mac/container; di Windows memicu banner peringatan
            o.add_argument("--no-sandbox")
            o.add_argument("--disable-dev-shm-usage")
            o.add_argument("--disable-gpu")
        o.add_experimental_option("excludeSwitches", ["enable-automation"])
        o.add_experimental_option("useAutomationExtension", False)
        o.add_experimental_option("prefs", {"intl.accept_languages": "id-ID,id,en-US,en"})
        o.page_load_strategy = "eager"           # jangan tunggu semua gambar/tile peta
        # Catatan: User-Agent SENGAJA tidak dipalsukan. UA 'Chrome/124' pada Chrome versi lain
        # tidak konsisten dengan fitur browser dan justru mudah dikenali sebagai bot.
        return o

    def _service(self, log_driver: str | None = None):
        """Service chromedriver. log_driver: file log verbose chromedriver (untuk diagnosis kegagalan membuka Chrome)."""
        kw = dict(log_output=log_driver, service_args=["--verbose"]) if log_driver else {}
        if cfg.CHROMEDRIVER_PATH:
            return Service(cfg.CHROMEDRIVER_PATH, **kw)
        if self._path_driver is None:
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                self._path_driver = ChromeDriverManager().install()
            except Exception as e:               # tanpa internet / paket tak ada -> Selenium Manager bawaan
                log.warning(f"webdriver-manager gagal ({ringkas_error(e, 120)}); memakai Selenium Manager bawaan")
                self._path_driver = ""
        return Service(self._path_driver, **kw) if self._path_driver else Service(**kw)

    def _mulai_sekali(self, profil: str, log_driver: str | None = None):
        self.driver = webdriver.Chrome(service=self._service(log_driver), options=self._opsi(profil))

    @staticmethod
    def _izin_matikan(pids: list) -> bool:
        """Menawarkan mematikan proses Chrome sisa (hanya yang memakai profil scraper)."""
        print(f"\n  Masih ada {len(pids)} proses Chrome yang memakai profil scraper (PID {_daftar_pid(pids)}).")
        print("  Biasanya ini jendela Chrome dari '--login' yang belum benar-benar tertutup, atau sisa Chrome dari jalan sebelumnya.")
        if not _terminal_interaktif():
            return False
        try:
            jawab = input("  Matikan paksa proses tersebut? (hanya Chrome milik scraper; Chrome biasa Anda aman) [y/N] ")
        except EOFError:
            return False
        return jawab.strip().lower() in ("y", "ya")

    @staticmethod
    def _pesan_profil_terkunci(e: BaseException, pids: list) -> str:
        baris = ["Chrome tidak bisa dibuka dengan profil scraper (folder .chrome_profile).",
                 f"Detail: {ringkas_error(e, 200)}",
                 "Penyebab paling umum: profil masih dipakai proses Chrome lain (jendela login yang belum benar-benar "
                 "tertutup, atau sisa Chrome dari jalan sebelumnya)."]
        if pids:
            baris.append(f"Proses Chrome yang masih memakai profil: PID {_daftar_pid(pids)}.")
        baris += ["Cara memperbaiki: (1) tutup semua jendela Chrome yang dibuka lewat --login; (2) di Task Manager akhiri "
                  "proses 'Google Chrome' yang tersisa (atau restart komputer); (3) jalankan ulang.",
                  "Masih gagal? Jalankan:  python scraper.py --diagnosa  lalu kirim hasilnya beserta "
                  "debug/chromedriver_diagnosa.log."]
        return "\n".join(baris)

    def mulai(self, matikan_sisa: bool = False):
        """
        Membuka Chrome dengan profil scraper. Jika profil terkunci proses Chrome lain: menunggu & mencoba lagi,
        menawarkan mematikan proses sisa itu, lalu memberi error yang jelas. SENGAJA tidak pindah ke profil
        sementara karena sesi login Google tersimpan di profil ini.
        matikan_sisa=True: proses sisa langsung dimatikan tanpa bertanya (dipakai saat memulihkan sesi yang mati).
        """
        profil = os.path.abspath(cfg.DIR_PROFIL)
        os.makedirs(profil, exist_ok=True)
        if matikan_sisa:
            n = matikan_proses_profil(profil)
            if n:
                log.warning(f"{n} proses Chrome sisa yang memakai profil scraper dimatikan")
        maks = 3
        for percobaan in range(1, maks + 1):
            try:
                self._mulai_sekali(profil)
                break
            except (SessionNotCreatedException, WebDriverException) as e:
                if not tanda_profil_terkunci(e):
                    raise
                pids = proses_chrome_profil(profil)
                if percobaan == maks:
                    raise ChromeTidakBisaDibukaError(self._pesan_profil_terkunci(e, pids)) from e
                if pids and (matikan_sisa or self._izin_matikan(pids)):
                    matikan_proses_profil(profil)
                    continue
                log.warning("Chrome tidak bisa dibuka dengan profil scraper (kemungkinan profil masih dipakai Chrome "
                            f"lain). Menunggu 8 detik lalu mencoba lagi ({percobaan}/{maks - 1})...")
                time.sleep(8)
        self.driver.set_page_load_timeout(cfg.TIMEOUT_HALAMAN)
        self._anti_deteksi()
        log.info(f"Chrome siap (versi {self.driver.capabilities.get('browserVersion', '?')}, "
                 f"headless={self.headless})")

    def _anti_deteksi(self):
        try:
            # addScriptToEvaluateOnNewDocument bertahan di SEMUA navigasi (execute_script biasa tidak).
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"})
            ua = self.driver.execute_script("return navigator.userAgent")
            if "HeadlessChrome" in ua:           # mode headless menyebut dirinya sendiri; pakai UA asli versi yang sama
                self.driver.execute_cdp_cmd("Emulation.setUserAgentOverride", {
                    "userAgent": ua.replace("HeadlessChrome", "Chrome"),
                    "acceptLanguage": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"})
        except Exception as e:
            log.debug(f"Pengaturan anti-deteksi dilewati: {ringkas_error(e)}")

    def tutup(self):
        d, self.driver = self.driver, None
        if d is None:
            return
        try:
            d.quit()
        except Exception:
            try:
                d.service.stop()                  # sesi sudah mati: matikan chromedriver paksa
            except Exception:
                pass

    def ulang(self):
        """Tutup lalu buka Chrome baru (dipakai saat sesi mati). Sisa proses Chrome sesi sebelumnya dimatikan."""
        self.tutup()
        time.sleep(2)
        self.mulai(matikan_sisa=True)

    # -- bantu ---------------------------------------------------------------
    def js(self, script: str, *args):
        return self.driver.execute_script(script, *args)

    def tidur(self, minimal: float, maksimal: float | None = None):
        maksimal = minimal if maksimal is None else maksimal
        time.sleep(random.uniform(minimal, maksimal) * self.faktor_lambat)

    def perlambat(self, faktor: float = 1.4, batas: float = 3.0):
        self.faktor_lambat = min(batas, self.faktor_lambat * faktor)
        log.info(f"  Kecepatan diperlambat (faktor jeda sekarang {self.faktor_lambat:.1f}x)")

    def buka(self, url: str):
        """Membuka URL, lalu memeriksa CAPTCHA/consent."""
        try:
            self.driver.get(url)
        except TimeoutException:
            log.warning("  Halaman lambat dimuat; dilanjutkan dengan yang sudah termuat")
            try:
                self.driver.execute_script("window.stop();")
            except WebDriverException as e:
                if sesi_mati(e):
                    raise
        self.periksa_halangan()

    # -- halangan Google ---------------------------------------------------------
    def periksa_halangan(self):
        jenis = deteksi_halangan(self.driver)
        if jenis == "consent":
            self._tangani_consent()
            jenis = deteksi_halangan(self.driver)
        if jenis == "captcha":
            self._tangani_captcha()

    def _tangani_consent(self):
        for _ in range(2):
            teks = self.driver.execute_script(
                _JS_KLIK_CONSENT, ["Tolak semua", "Reject all", "Terima semua", "Accept all"])
            if teks:
                log.info(f"  Halaman consent Google: mengklik '{teks}'")
                time.sleep(2.5)
                if deteksi_halangan(self.driver) != "consent":
                    return
            time.sleep(1)
        log.warning("  Halaman consent Google tidak bisa ditutup otomatis")

    def _tangani_captcha(self):
        self.perlambat(1.6)
        self.simpan_debug("captcha")
        if self.headless:
            raise DiblokirGoogleError(
                "Google menampilkan CAPTCHA (mode headless tidak bisa menyelesaikannya). "
                "Jalankan tanpa --headless, atau tunggu beberapa jam / ganti jaringan lalu jalankan ulang.")
        menit = cfg.TUNGGU_CAPTCHA_MENIT
        print("\n" + "!" * 66)
        print("  GOOGLE MENAMPILKAN CAPTCHA.")
        print("  Selesaikan secara manual di jendela Chrome yang terbuka.")
        print(f"  Scraper menunggu maksimal {menit} menit, lalu lanjut otomatis.")
        print("!" * 66 + "\n")
        log.warning(f"CAPTCHA terdeteksi; menunggu penyelesaian manual (maks {menit} menit)")
        batas = time.time() + menit * 60
        while time.time() < batas:
            time.sleep(4)
            if deteksi_halangan(self.driver) != "captcha":
                log.info("CAPTCHA terselesaikan, melanjutkan.")
                time.sleep(2)
                return
        raise DiblokirGoogleError(
            f"CAPTCHA tidak diselesaikan dalam {menit} menit. Progres tersimpan; "
            "jalankan ulang scraper nanti (disarankan tunggu 30-60 menit).")

    # -- diagnosis ------------------------------------------------------------
    def simpan_debug(self, label: str) -> str | None:
        """Menyimpan screenshot + HTML panel ke folder debug/. Dibatasi jumlahnya per sesi."""
        if self._jumlah_debug >= cfg.MAKS_DEBUG_PER_SESI or self.driver is None:
            return None
        try:
            os.makedirs(cfg.DIR_DEBUG, exist_ok=True)
            nama = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")[:50] + "_" + time.strftime("%Y%m%d_%H%M%S")
            dasar = os.path.join(cfg.DIR_DEBUG, nama)
            self.driver.save_screenshot(dasar + ".png")
            html = self.driver.execute_script(
                "var m=document.querySelector('div[role=\"main\"]')||document.body; "
                "return 'URL: '+location.href+'\\n\\n'+m.outerHTML;")
            with open(dasar + ".html", "w", encoding="utf-8") as f:
                f.write(html[:1_500_000])
            self._jumlah_debug += 1
            log.info(f"  Data diagnosis disimpan: debug/{nama}.png / .html")
            return nama
        except Exception as e:
            log.debug(f"Gagal menyimpan debug: {ringkas_error(e)}")
            return None


# ─── Login Google manual ──────────────────────────────────────────────────────
# Google Maps menampilkan "Login untuk menikmati fitur terbaik dari Google Maps" saat pengunjung
# TANPA login mencoba membuka ulasan lengkap ("Ulasan lainnya"): tanpa login hanya ±3-5 ulasan
# preview yang terlihat. Solusinya: login sekali di profil Chrome khusus scraper. Login dilakukan
# di Chrome BIASA (bukan yang dikendalikan Selenium) karena Google sering menolak login pada
# browser otomatis; sesi tersimpan di folder profil sehingga dipakai ulang oleh scraper.

URL_LOGIN = ("https://accounts.google.com/ServiceLogin?hl={bahasa}"
             "&continue=https%3A%2F%2Fwww.google.com%2Fmaps%3Fhl%3D{bahasa}")

PETUNJUK_LOGIN = """
==================================================================
  LOGIN GOOGLE UNTUK SCRAPER
==================================================================
  Chrome akan terbuka dengan profil khusus scraper (folder .chrome_profile).

   1. Login ke akun Google. DISARANKAN pakai akun khusus (bukan akun utama):
      Google bisa membatasi akun yang dipakai untuk scraping.
   2. Setelah login, buka salah satu tempat di Google Maps dan pastikan
      tombol "Ulasan lainnya" benar-benar menampilkan daftar ulasan.
   3. TUTUP jendela Chrome tersebut (tombol X), tunggu beberapa detik,
      lalu kembali ke sini dan tekan ENTER.

  Jangan membagikan atau meng-commit folder .chrome_profile (berisi sesi login Anda).
==================================================================
"""


def _kandidat_chrome() -> list:
    """Daftar lokasi Chrome yang layak dicoba, berurutan: CHROME_BINARY, lokasi umum Windows/Mac, lalu PATH."""
    kandidat = []
    if cfg.CHROME_BINARY:
        kandidat.append(cfg.CHROME_BINARY)
    if os.name == "nt":
        for nama_env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            dasar = os.environ.get(nama_env)
            if dasar:
                kandidat.append(os.path.join(dasar, "Google", "Chrome", "Application", "chrome.exe"))
    elif sys.platform == "darwin":
        kandidat.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for nama in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        lokasi = shutil.which(nama)
        if lokasi:
            kandidat.append(lokasi)
    return kandidat


def cari_chrome() -> str | None:
    """Lokasi Chrome pertama yang benar-benar ada, atau None."""
    for k in _kandidat_chrome():
        if k and os.path.exists(k):
            return k
    return None


def login_manual() -> bool:
    """
    Membuka Chrome biasa dengan profil scraper agar pengguna bisa login ke Google, lalu menunggu sampai
    Chrome benar-benar ditutup (profil terkunci selama ada proses Chrome yang memakainya).
    Returns True jika Chrome berhasil dibuka dan profilnya sudah dilepas.
    """
    chrome = cari_chrome()
    if not chrome:
        print("Chrome tidak ditemukan. Atur variabel lingkungan CHROME_BINARY ke lokasi chrome.exe, "
              "mis. C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe")
        return False
    profil = os.path.abspath(cfg.DIR_PROFIL)
    os.makedirs(profil, exist_ok=True)
    sisa = proses_chrome_profil(profil)
    if sisa:                                      # sisa Chrome lama akan 'menelan' jendela login ini dan langsung keluar
        if Peramban._izin_matikan(sisa):
            matikan_proses_profil(profil)
        else:
            print("  Tutup dulu Chrome tersebut (atau akhiri di Task Manager), lalu jalankan --login lagi.")
            return False
    print(PETUNJUK_LOGIN)
    proses = subprocess.Popen([
        chrome, f"--user-data-dir={profil}", "--profile-directory=Default", "--no-first-run",
        "--no-default-browser-check", "--disable-background-mode",
        f"--lang={cfg.BAHASA}-ID" if cfg.BAHASA == "id" else f"--lang={cfg.BAHASA}",
        URL_LOGIN.format(bahasa=cfg.BAHASA)])
    try:
        input("  Tekan ENTER di sini setelah selesai login dan jendela Chrome ditutup... ")
    except EOFError:                              # tanpa terminal interaktif: tunggu sampai Chrome ditutup
        proses.wait()
    for _ in range(20):                           # beri waktu Chrome menutup diri & menyimpan cookie ke disk
        if proses.poll() is not None:
            break
        time.sleep(1)
    # Proses yang kita luncurkan bisa saja sudah selesai sementara Chrome-nya (anak proses) masih hidup:
    # yang menentukan adalah apakah masih ada proses Chrome yang memakai profil.
    for _ in range(30):
        if not proses_chrome_profil(profil):
            return True
        time.sleep(2)
    sisa = proses_chrome_profil(profil)
    print(f"\n  Masih ada {len(sisa)} proses Chrome yang memakai profil scraper (PID {_daftar_pid(sisa)}).")
    if Peramban._izin_matikan(sisa):
        matikan_proses_profil(profil)
    else:
        print("  Tutup jendelanya atau akhiri proses 'Google Chrome' di Task Manager sebelum menjalankan scraper; "
              "kalau tidak profil akan terkunci.")
    return True


# ─── Diagnosa Chrome ──────────────────────────────────────────────────────────

def jalankan_diagnosa() -> int:
    """
    Mengumpulkan fakta saat Chrome tidak bisa dibuka (mis. 'session not created: Chrome instance exited'):
    lokasi Chrome, status profil, proses yang memakai profil, dan uji membuka Chrome dengan profil scraper
    lalu dengan profil sementara, disertai log verbose chromedriver di debug/chromedriver_diagnosa.log.
    """
    profil = os.path.abspath(cfg.DIR_PROFIL)
    os.makedirs(cfg.DIR_DEBUG, exist_ok=True)
    file_log = os.path.join(cfg.DIR_DEBUG, "chromedriver_diagnosa.log")
    open(file_log, "w", encoding="utf-8").close()
    print("=" * 66 + "\n  DIAGNOSA CHROME\n" + "=" * 66)
    print(f"- Sistem operasi   : {sys.platform}, Python {sys.version.split()[0]}")
    print(f"- Chrome           : {cari_chrome() or 'TIDAK DITEMUKAN (atur CHROME_BINARY)'}")
    print(f"- CHROMEDRIVER_PATH: {cfg.CHROMEDRIVER_PATH or '(tidak diatur; otomatis)'}")
    print(f"- Folder profil    : {profil} ({'ada' if os.path.isdir(profil) else 'belum ada'})")
    if os.name == "nt":
        print(f"- lockfile profil  : {'ADA (profil sedang dipakai Chrome)' if os.path.exists(os.path.join(profil, 'lockfile')) else 'tidak ada'}")
    pids = proses_chrome_profil(profil)
    print(f"- Proses Chrome yang memakai profil: {('PID ' + _daftar_pid(pids)) if pids else 'tidak ada / tidak bisa diperiksa'}")

    def coba(nama: str, folder: str):
        pb = Peramban()
        try:
            pb._mulai_sekali(folder, log_driver=file_log)
            kap = pb.driver.capabilities
            print(f"- Uji buka Chrome dengan {nama}: BERHASIL (Chrome {kap.get('browserVersion', '?')}, "
                  f"chromedriver {str(kap.get('chrome', {}).get('chromedriverVersion', '?')).split()[0]})")
            return True
        except Exception as e:                    # noqa: BLE001
            print(f"- Uji buka Chrome dengan {nama}: GAGAL -> {ringkas_error(e, 220)}")
            return False
        finally:
            pb.tutup()

    sementara = tempfile.mkdtemp(prefix="scraper_diagnosa_")
    try:
        if coba("profil scraper", profil):
            kesimpulan = "Chrome bisa dibuka dengan profil scraper. Jalankan scraper seperti biasa."
            kode = 0
        elif coba("profil SEMENTARA (kosong)", sementara):
            kesimpulan = ("Chrome bisa dibuka dengan profil kosong tetapi GAGAL dengan profil scraper: profil terkunci "
                          "proses Chrome lain atau rusak. Tutup semua Chrome milik scraper / akhiri chrome.exe di Task "
                          "Manager lalu coba lagi. Bila tetap gagal, ganti nama folder .chrome_profile lalu jalankan "
                          "--login lagi (login perlu diulang).")
            kode = 1
        else:
            kesimpulan = ("Chrome GAGAL dibuka bahkan dengan profil kosong: masalah ada pada Chrome/chromedriver itu "
                          "sendiri (versi tidak cocok, antivirus, atau izin). Kirim keluaran ini dan "
                          "debug/chromedriver_diagnosa.log.")
            kode = 1
    finally:
        shutil.rmtree(sementara, ignore_errors=True)
    print("-" * 66 + f"\nKESIMPULAN: {kesimpulan}\nLog chromedriver: {file_log}\n" + "=" * 66)
    return kode
