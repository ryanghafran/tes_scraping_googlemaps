"""
peramban.py
===========
Pengelolaan browser Chrome (Selenium):
  * membuat Chrome dengan opsi yang benar (profil khusus, tanpa UA palsu, anti-throttling)
  * mendeteksi sesi browser yang MATI agar bisa dibuka ulang (versi lama: satu crash =
    39 tempat gagal berturut-turut dengan 'invalid session id')
  * mendeteksi halangan Google (CAPTCHA 'unusual traffic', halaman consent)
  * menyimpan screenshot + HTML saat ada masalah, supaya penyebabnya bisa didiagnosis
"""

from __future__ import annotations

import logging
import os
import random
import re
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


# ─── Kelas Peramban ───────────────────────────────────────────────────────────

class Peramban:
    """Pembungkus Chrome WebDriver dengan pemulihan otomatis."""

    def __init__(self, headless: bool | None = None):
        self.headless = cfg.HEADLESS if headless is None else headless
        self.driver = None
        self.faktor_lambat = 1.0          # naik otomatis saat ada tanda-tanda dibatasi Google
        self._jumlah_debug = 0

    # -- siklus hidup ---------------------------------------------------------
    def _opsi(self, profil: str) -> Options:
        o = Options()
        if self.headless:
            o.add_argument("--headless=new")
        if cfg.CHROME_BINARY:
            o.binary_location = cfg.CHROME_BINARY
        o.add_argument(f"--user-data-dir={profil}")
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

    def _service(self):
        if cfg.CHROMEDRIVER_PATH:
            return Service(cfg.CHROMEDRIVER_PATH)
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            return Service(ChromeDriverManager().install())
        except Exception as e:                   # tanpa internet / paket tak ada -> Selenium Manager
            log.warning(f"webdriver-manager gagal ({ringkas_error(e, 120)}); memakai Selenium Manager bawaan")
            return None

    def mulai(self):
        profil = os.path.abspath(cfg.DIR_PROFIL)
        os.makedirs(profil, exist_ok=True)
        servis = self._service()
        terakhir = None
        for percobaan in range(2):
            try:
                opsi = self._opsi(profil)
                self.driver = (webdriver.Chrome(service=servis, options=opsi) if servis
                               else webdriver.Chrome(options=opsi))
                break
            except (SessionNotCreatedException, WebDriverException) as e:
                terakhir = e
                pesan = str(e).lower()
                profil_terpakai = any(k in pesan for k in
                                      ("user data directory", "devtoolsactiveport", "chrome failed to start", "crashed"))
                if percobaan == 0 and profil_terpakai:
                    profil = f"{profil}_{time.strftime('%H%M%S')}"
                    os.makedirs(profil, exist_ok=True)
                    log.warning("Profil Chrome sedang dipakai Chrome lain (sisa proses lama?). "
                                f"Memakai profil sementara. Detail: {ringkas_error(e, 140)}")
                    continue
                raise
        if self.driver is None:                  # pengaman; seharusnya tak tercapai
            raise terakhir
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
        """Tutup lalu buka Chrome baru (dipakai saat sesi mati)."""
        self.tutup()
        time.sleep(2)
        self.mulai()

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
