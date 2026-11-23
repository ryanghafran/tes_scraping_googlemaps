"""
=============================================================================
  SCRAPER ULASAN GOOGLE MAPS - COFFEE SHOP BANDUNG
  Dibuat untuk keperluan riset skripsi: Analisis Sentimen NLP
=============================================================================

  Cara penggunaan:
    1. Pastikan sudah install dependency: pip install -r requirements.txt
    2. Pastikan Google Chrome sudah terinstall di komputer Anda
    3. Edit file 'daftar_coffeeshop.csv' lalu isi daftar nama coffee shop
    4. Jalankan script ini: python scraper.py

  Output:
    - hasil_ulasan.csv        -> file gabungan semua ulasan yang berhasil
    - log_gagal.txt           -> daftar tempat yang gagal di-scrape (untuk retry)
    - progress_selesai.txt    -> daftar tempat yang sudah selesai (untuk resume)
    - scraper_run.log         -> log lengkap seluruh jalannya program
=============================================================================
"""

# ─── Import Library ───────────────────────────────────────────────────────────

import os           # Untuk operasi file & folder
import re           # Regex untuk ekstrak angka dari teks rating
import csv          # Untuk konstanta quoting CSV
import time         # Untuk jeda/delay antar aksi
import random       # Untuk membuat jeda random agar tidak terdeteksi bot
import logging      # Untuk mencatat log ke file
import pandas as pd # Untuk membaca/menulis CSV

# Selenium: library untuk otomatisasi browser
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
)

# webdriver-manager: otomatis download & kelola ChromeDriver yang sesuai versi Chrome
from webdriver_manager.chrome import ChromeDriverManager


# ─── Konfigurasi Global ───────────────────────────────────────────────────────

INPUT_CSV        = "daftar_coffeeshop.csv"   # File input: daftar coffee shop
OUTPUT_CSV       = "hasil_ulasan.csv"         # File output: gabungan semua ulasan
LOG_GAGAL        = "log_gagal.txt"            # Tempat yang gagal di-scrape
LOG_SKIP         = "log_skip_kategori.txt"    # Tempat yang di-skip karena bukan coffee shop
PROGRESS_FILE    = "progress_selesai.txt"     # Tempat yang sudah selesai (resume)
LOKASI           = "Bandung"                  # Ditambahkan ke query pencarian
DELAY_MIN        = 3                          # Jeda minimum antar-request (detik)
DELAY_MAX        = 6                          # Jeda maksimum antar-request (detik)
MAX_SCROLL_TRIES = 80                         # Batas maksimum scroll (failsafe)
ELEMENT_TIMEOUT  = 15                         # Timeout menunggu elemen (detik)

# Kategori yang dianggap valid sebagai coffee shop
# Jika FILTER_KATEGORI = False, semua tempat di daftar_coffeeshop.csv akan di-scrape tanpa filter
FILTER_KATEGORI          = False
KATEGORI_VALID           = ["cafe", "coffee shop", "kedai kopi", "kafe", "coffee", "restoran", "bakery", "roti", "eatery"]

# Target batas ulasan per tempat (diatur 160 agar dari 39 cafe terkumpul 5.000 - 6.000 ulasan)
# 39 tempat x ~150-160 ulasan ≈ 5.800 - 6.200 ulasan
TARGET_ULASAN_PER_TEMPAT = 160


# ─── Setup Logging ────────────────────────────────────────────────────────────

# Logger akan print ke konsol DAN menyimpan ke file scraper_run.log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),                        # Tampil di terminal
        logging.FileHandler("scraper_run.log", "a"),    # Simpan ke file log
    ]
)
log = logging.getLogger(__name__)


# ─── Fungsi: Setup Browser Chrome ─────────────────────────────────────────────

def buat_driver() -> webdriver.Chrome:
    """
    Membuat dan mengkonfigurasi instance Chrome WebDriver.

    Mode 'headless' (tanpa jendela browser) dinonaktifkan secara default
    agar lebih mudah di-debug. Aktifkan headless jika ingin berjalan
    di background tanpa tampilan.

    Returns:
        webdriver.Chrome: Instance driver yang siap digunakan
    """
    options = Options()

    # --- Opsi anti-deteksi bot ---
    # Nonaktifkan flag yang menandai browser ini sebagai otomatis
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Ukuran jendela browser yang terlihat normal
    options.add_argument("--window-size=1280,900")

    # User-Agent: berpura-pura menjadi Chrome biasa (bukan bot)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    # Nonaktifkan pop-up notifikasi browser
    options.add_argument("--disable-notifications")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # Aktifkan baris ini untuk mode tanpa jendela (headless):
    # options.add_argument("--headless=new")

    # webdriver-manager otomatis download ChromeDriver yang cocok
    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=options)

    # Hapus properti 'webdriver' dari JavaScript agar tidak terdeteksi
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    return driver


# ─── Fungsi: Jeda Random ──────────────────────────────────────────────────────

def jeda(min_detik: float = DELAY_MIN, max_detik: float = DELAY_MAX):
    """
    Menunggu beberapa detik secara acak.

    Tujuan: Menghindari deteksi bot dari Google dengan tidak melakukan
    request secara berturut-turut dalam interval yang terlalu teratur.

    Args:
        min_detik: Batas bawah jeda
        max_detik: Batas atas jeda
    """
    durasi = random.uniform(min_detik, max_detik)
    log.info(f"  Menunggu {durasi:.1f} detik...")
    time.sleep(durasi)


# ─── Fungsi: Baca & Catat Progress ───────────────────────────────────────────

def baca_progress() -> set:
    """
    Membaca file progress untuk mengetahui tempat mana yang sudah selesai.

    Berguna saat script dihentikan di tengah jalan dan dijalankan ulang:
    tempat yang sudah selesai akan di-skip agar tidak duplikat.

    Returns:
        set: Kumpulan nama tempat yang sudah berhasil di-scrape
    """
    if not os.path.exists(PROGRESS_FILE):
        return set()  # File belum ada, belum ada progress sama sekali

    with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
        selesai = {baris.strip() for baris in f if baris.strip()}

    log.info(f"Ditemukan {len(selesai)} tempat yang sudah pernah di-scrape (akan di-skip)")
    return selesai


def catat_progress(nama_tempat: str):
    """
    Mencatat satu nama tempat ke file progress setelah berhasil di-scrape.

    Args:
        nama_tempat: Nama coffee shop yang baru saja selesai di-scrape
    """
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(nama_tempat + "\n")


def catat_gagal(nama_tempat: str, alasan: str):
    """
    Mencatat nama tempat yang gagal di-scrape ke file log_gagal.txt.

    File ini berguna untuk mengetahui tempat mana yang perlu di-retry.

    Args:
        nama_tempat: Nama coffee shop yang gagal
        alasan: Pesan error atau keterangan kenapa gagal
    """
    with open(LOG_GAGAL, "a", encoding="utf-8") as f:
        f.write(f"{nama_tempat} | {alasan}\n")
    log.warning(f"  GAGAL: {nama_tempat} -- {alasan}")


# ─── Custom Exception: Bukan Coffee Shop ──────────────────────────────────────

class BukanCoffeeshopError(Exception):
    """
    Exception khusus yang dilempar ketika kategori tempat di Google Maps
    tidak termasuk dalam KATEGORI_VALID (cafe/coffee shop).

    Ini BUKAN error scraping — tempat berhasil dibuka, hanya saja
    ternyata bukan coffee shop (misal: murni restoran atau hotel).
    Diperlakukan berbeda dari Exception biasa di main loop.
    """
    pass


def catat_skip(nama_tempat: str, kategori_ditemukan: str):
    """
    Mencatat tempat yang di-skip karena bukan coffee shop ke log_skip_kategori.txt.

    Args:
        nama_tempat:       Nama tempat yang di-skip
        kategori_ditemukan: Teks kategori yang ditemukan di Google Maps
    """
    with open(LOG_SKIP, "a", encoding="utf-8") as f:
        f.write(f"{nama_tempat} | kategori: {kategori_ditemukan}\n")
    log.warning(f"  SKIP (bukan coffee shop): {nama_tempat} -- kategori: '{kategori_ditemukan}'")


# ─── Fungsi: Deteksi Separator CSV ───────────────────────────────────────────

def deteksi_separator(file_path: str) -> str:
    """Mendeteksi apakah file CSV menggunakan titik-koma (;) atau koma (,)."""
    if not os.path.exists(file_path):
        return ";"
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            baris_pertama = f.readline()
            return ";" if ";" in baris_pertama else ","
    except Exception:
        return ";"


# ─── Fungsi: Simpan Ulasan ke CSV ─────────────────────────────────────────────

def simpan_ke_csv(daftar_ulasan: list):
    """
    Menambahkan (append) daftar ulasan baru ke file CSV output.

    Jika file belum ada -> buat file baru dengan header.
    Jika file sudah ada -> tambahkan di bawah baris terakhir (tanpa header).

    Args:
        daftar_ulasan: List berisi dict per ulasan dengan key:
                       nama_tempat, lokasi, nama_reviewer, rating, tanggal, teks_ulasan
    """
    if not daftar_ulasan:
        return  # Tidak ada yang perlu disimpan

    df_baru = pd.DataFrame(daftar_ulasan)

    # Cek apakah file sudah ada untuk menentukan perlu header atau tidak
    file_sudah_ada = os.path.exists(OUTPUT_CSV)
    sep = deteksi_separator(OUTPUT_CSV) if file_sudah_ada else ";"

    try:
        df_baru.to_csv(
            OUTPUT_CSV,
            mode="a",                    # Mode append: tambah di bawah
            sep=sep,                     # Samakan separator dengan file yang ada
            header=not file_sudah_ada,   # Tulis header hanya jika file baru
            index=False,                 # Jangan tulis kolom index pandas
            encoding="utf-8-sig",        # utf-8-sig agar bisa dibuka Excel dengan benar
            quoting=csv.QUOTE_ALL,       # Bungkus semua kolom dengan tanda kutip agar aman
        )
        log.info(f"  Disimpan {len(daftar_ulasan)} ulasan ke '{OUTPUT_CSV}' (sep: '{sep}')")
    except PermissionError:
        log.error(f"  [PERMISSION ERROR] Tidak bisa menyimpan ke '{OUTPUT_CSV}'!")
        log.error("  Tutup file CSV tersebut dari Excel terlebih dahulu, lalu jalankan ulang script.")
        raise  # Lempar exception agar tercatat di log_gagal.txt



# ─── Fungsi: Muat Ulasan Existing untuk Deduplikasi ──────────────────────────

def muat_ulasan_existing() -> set:
    """
    Membaca ulasan yang sudah tersimpan di CSV untuk deteksi duplikat.

    Kunci duplikat: kombinasi (nama_tempat + nama_reviewer + teks_ulasan).
    Dengan ini, kalau script dijalankan dua kali, ulasan yang sama tidak
    akan masuk dua kali ke CSV.

    Returns:
        set: Kumpulan string kunci unik dari ulasan yang sudah ada
    """
    if not os.path.exists(OUTPUT_CSV):
        return set()  # File belum ada, tidak ada duplikat

    sep = deteksi_separator(OUTPUT_CSV)
    try:
        df_existing = pd.read_csv(OUTPUT_CSV, sep=sep, encoding="utf-8-sig")
    except Exception as e:
        log.warning(f"Gagal membaca CSV ulasan existing: {e}")
        return set()

    if "nama_tempat" not in df_existing.columns or "teks_ulasan" not in df_existing.columns:
        log.warning(f"Kolom 'nama_tempat' atau 'teks_ulasan' tidak cocok di CSV (sep: '{sep}')")
        return set()

    # Buat kunci unik per ulasan dari 3 kolom yang digabung (normalisasi spasi)
    teks_norm = (
        df_existing["teks_ulasan"]
        .astype(str)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )
    kunci = set(
        df_existing["nama_tempat"].astype(str).str.strip()
        + "|"
        + df_existing["nama_reviewer"].astype(str).str.strip()
        + "|"
        + teks_norm
    )
    log.info(f"Ditemukan {len(kunci)} ulasan existing untuk cek duplikat (sep: '{sep}')")
    return kunci


# ─── Fungsi Utama: Scrape Satu Tempat ─────────────────────────────────────────

def scrape_satu_tempat(driver: webdriver.Chrome, nama_tempat: str, ulasan_existing: set) -> list:
    """
    Melakukan scraping seluruh ulasan untuk satu coffee shop di Google Maps.

    Alur kerja:
      1. Buka Google Maps dan cari nama tempat + lokasi
      2. Klik hasil pencarian pertama
      2.5 Ekstrak alamat lengkap dari panel detail
      3. Klik tab "Ulasan"
      4. Scroll otomatis sampai semua ulasan termuat
      5. Klik semua tombol "Selengkapnya" agar teks penuh terbaca
      6. Ekstrak data tiap ulasan
      7. Filter: skip yang teksnya kosong & skip yang sudah ada di CSV

    Args:
        driver:          Instance Selenium WebDriver yang sedang aktif
        nama_tempat:     Nama coffee shop yang akan di-scrape
        ulasan_existing: Set kunci ulasan yang sudah ada (untuk deduplikasi)

    Returns:
        list: Daftar dict ulasan yang berhasil diekstrak (bisa kosong jika tidak ada)

    Raises:
        Exception: Jika terjadi error fatal pada proses scraping
    """
    wait = WebDriverWait(driver, ELEMENT_TIMEOUT)

    # ── Langkah 1: Buka Google Maps dan cari tempat ──────────────────────────
    query    = f"{nama_tempat} {LOKASI}"
    url_maps = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"

    log.info(f"  Membuka: {url_maps}")
    driver.get(url_maps)
    jeda(2, 4)  # Tunggu halaman selesai dimuat

    # ── Langkah 2: Buka halaman detail tempat ────────────────────────────────
    # Google Maps memiliki 2 kemungkinan perilaku:
    # A) Direct Match: Google Maps langsung membuka profil tempat (/maps/place/...)
    # B) Search List: Muncul daftar hasil pencarian (elemen a.hfpxzc)
    try:
        # Cek apakah Google Maps langsung mengarah ke halaman profil tempat
        sudah_di_detail = (
            "/maps/place/" in driver.current_url
            or len(driver.find_elements(By.XPATH, '//button[@role="tab"][contains(., "Ulasan") or contains(., "Reviews")]')) > 0
            or len(driver.find_elements(By.CSS_SELECTOR, "h1.DUwDvf, h1.fontHeadlineLarge")) > 0
        )

        if sudah_di_detail:
            log.info("  Google Maps langsung membuka detail tempat (direct match)")
        else:
            # Tunggu elemen hasil pencarian pertama muncul atau redirect
            try:
                hasil_pertama = wait.until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "a.hfpxzc"))
                )
                hasil_pertama.click()
                log.info("  Berhasil klik hasil pencarian pertama")
                jeda(2, 4)
            except TimeoutException:
                # Cek fallback apakah saat menunggu, halaman sudah redirect ke detail
                if "/maps/place/" in driver.current_url or driver.find_elements(By.CSS_SELECTOR, "h1.DUwDvf, h1.fontHeadlineLarge"):
                    log.info("  Halaman detail tempat sudah terbuka (direct match)")
                else:
                    raise Exception("Tidak ada hasil pencarian ditemukan / timeout saat klik result")
    except Exception as e:
        if "Tidak ada hasil pencarian" in str(e):
            raise
        if "/maps/place/" in driver.current_url:
            log.info("  Sudah berada di halaman detail tempat")
        else:
            raise

    # ── Langkah 2.5: Ekstrak nama resmi & alamat lengkap dari panel detail ─────
    # Setelah klik result, Google Maps menampilkan panel detail di sebelah kiri.
    # Di sini kita ambil DUA informasi:
    #   a) Nama resmi tempat (dari <h1>) → untuk kolom nama_tempat di CSV
    #   b) Alamat lengkap               → untuk kolom lokasi di CSV

    # ── a) Nama Resmi Tempat ─────────────────────────────────────────────────
    # Nama resmi tampil di heading <h1> panel detail, misal: "Starbucks Coffee Soekarno-Hatta"
    # Ini lebih akurat daripada query pencarian yang kita ketik sendiri.
    nama_tempat_asli = nama_tempat  # Default: gunakan nama query jika tidak ditemukan
    try:
        # Selector utama: h1 dengan class fontHeadlineLarge (dipakai Google Maps)
        elemen_nama = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "h1.DUwDvf, h1.fontHeadlineLarge")
            )
        )
        nama_tempat_asli = elemen_nama.text.strip()
        log.info(f"  Nama resmi tempat: {nama_tempat_asli}")
    except TimeoutException:
        # Fallback: ambil dari atribut aria-label pada elemen heading
        try:
            elemen_nama = driver.find_element(By.XPATH, '//h1')
            nama_tempat_asli = elemen_nama.text.strip() or nama_tempat
            log.info(f"  Nama resmi (fallback): {nama_tempat_asli}")
        except NoSuchElementException:
            log.warning("  Nama resmi tidak ditemukan, menggunakan nama query sebagai default")

    # ── c) Validasi Kategori Tempat ──────────────────────────────────────────
    # Google Maps menampilkan kategori/tipe tempat di bawah nama (h1),
    # contoh: "Coffee shop" atau "Coffee shop · Kafe · Restoran"
    # Selector: div.skqShb atau button.DkEaL biasanya berisi teks kategori.
    # Jika kategori tidak mengandung kata kunci coffee shop → tempat di-skip.
    kategori_teks = ""  # Default kosong jika tidak ditemukan
    try:
        # Coba beberapa selector kategori yang umum dipakai Google Maps
        for selector in ["div.skqShb", "button.DkEaL", "span.mgr77e"]:
            elemen_kat = driver.find_elements(By.CSS_SELECTOR, selector)
            if elemen_kat:
                kategori_teks = elemen_kat[0].text.strip()
                break
        log.info(f"  Kategori tempat: '{kategori_teks}'")
    except Exception:
        log.warning("  Kategori tidak bisa diekstrak, lanjutkan tanpa filter")

    # Lakukan pengecekan hanya jika kategori berhasil diekstrak dan filter diaktifkan
    if FILTER_KATEGORI and kategori_teks:
        tags = kategori_teks.lower()
        if not any(kat in tags for kat in KATEGORI_VALID):
            # Tempat ini bukan coffee shop (misal: restoran, hotel, toko)
            # Lempar BukanCoffeeshopError agar main loop bisa handle secara terpisah
            raise BukanCoffeeshopError(
                f"Kategori '{kategori_teks}' tidak termasuk coffee shop/cafe"
            )

    # ── b) Alamat Lengkap ────────────────────────────────────────────────────
    alamat_lengkap = LOKASI  # Default: gunakan nama kota jika alamat tidak ditemukan
    try:
        # Selector utama: button alamat di panel detail Google Maps
        # Teks alamat ada di dalam elemen .Io6YTe di dalam button tersebut
        elemen_alamat = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'button[data-item-id="address"] .Io6YTe')
            )
        )
        alamat_lengkap = elemen_alamat.text.strip()
        log.info(f"  Alamat ditemukan: {alamat_lengkap}")
    except TimeoutException:
        # Fallback 1: coba selector alternatif (Google Maps kadang pakai class berbeda)
        try:
            elemen_alamat = driver.find_element(
                By.CSS_SELECTOR, "button[data-tooltip='Salin alamat'] .Io6YTe"
            )
            alamat_lengkap = elemen_alamat.text.strip()
            log.info(f"  Alamat (fallback 1): {alamat_lengkap}")
        except NoSuchElementException:
            # Fallback 2: coba ambil dari aria-label pada elemen alamat
            try:
                elemen_alamat = driver.find_element(
                    By.XPATH,
                    '//button[contains(@aria-label, "Alamat") or contains(@aria-label, "Address")]'
                )
                alamat_lengkap = elemen_alamat.get_attribute("aria-label") or LOKASI
                # Bersihkan prefix "Alamat: " jika ada
                alamat_lengkap = alamat_lengkap.replace("Alamat: ", "").replace("Address: ", "").strip()
                log.info(f"  Alamat (fallback 2): {alamat_lengkap}")
            except NoSuchElementException:
                log.warning("  Alamat tidak ditemukan, menggunakan nama kota sebagai default")

    # ── Helper: Tutup popup/modal login Google jika muncul ──────────────────
    def tutup_modal_jika_ada():
        """
        Menutup popup login Google Maps jika muncul.
        PENTING: Hanya gunakan Escape — JANGAN klik tombol 'Tutup' karena
        bisa salah mengenai tombol close sidebar panel detail tempat!
        """
        try:
            # Cek apakah ada dialog/modal overlay yang aktif
            # Selector spesifik untuk modal login Google: ada teks "Login" sebagai button
            ada_modal = driver.find_elements(
                By.XPATH,
                '//div[@role="dialog"] | '
                '//div[contains(@class,"modal")] | '
                '//button[normalize-space(text())="Login" or normalize-space(text())="Masuk"]'
            )
            if ada_modal:
                # Gunakan Escape — cara paling aman, tidak salah klik elemen lain
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(1)
                log.info("  Modal popup login ditutup via Escape")
                return True
        except Exception:
            pass
        return False

    # ── Langkah 3: Klik tab "Ulasan" ─────────────────────────────────────────
    # Tutup modal jika ada, lalu tunggu panel detail fully loaded
    tutup_modal_jika_ada()
    jeda(2, 3)  # Beri waktu panel detail Google Maps render sempurna

    tab_terklik = False
    SELECTOR_KARTU_AKTIF = "div.jftiEf"  # default

    # Selector kartu ulasan yang akan dicek untuk verifikasi tab berhasil terbuka
    SELECTOR_KARTU = [
        "div.jftiEf",               # class lama
        "div[data-review-id]",       # berbasis atribut (lebih stabil, tidak berubah)
        "div.MyEned",
        "div.GHT2ce",
        "div[jscontroller][data-review-id]",
    ]

    # Coba berbagai cara klik tab ulasan — dari yang paling spesifik ke fallback
    STRATEGI_KLIK_TAB = [
        # Selector CSS tombol di bawah judul tempat
        ("css", 'button[aria-label*="ulasan"]'),
        ("css", 'button[aria-label*="Ulasan"]'),
        ("css", 'button[aria-label*="reviews"]'),
        ("css", 'button[aria-label*="Reviews"]'),
        ("css", 'button[jsaction*="pane.rating"]'),
        ("css", 'div.F7nice button'),
        # Selector XPath tab
        ("xpath", '//button[@role="tab"][contains(., "Ulasan")]'),
        ("xpath", '//button[@role="tab"][contains(., "Reviews")]'),
        ("xpath", '//button[@data-tab-index="1"]'),
        ("xpath", '//button[contains(@aria-label,"Ulasan")]'),
        ("xpath", '//button[contains(@aria-label,"Reviews")]'),
    ]

    for metode, selector in STRATEGI_KLIK_TAB:
        if tab_terklik:
            break
        try:
            if metode == "css":
                elems = driver.find_elements(By.CSS_SELECTOR, selector)
            else:
                elems = driver.find_elements(By.XPATH, selector)

            if not elems:
                continue

            for el in elems:
                try:
                    if not el.is_displayed():
                        continue
                    try:
                        el.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", el)
                    log.info(f"  Klik tab ulasan via: {selector}")
                    jeda(3, 4)  # Tunggu panel ulasan render

                    # Tutup modal jika terpicu setelah klik (pakai Escape, bukan klik Tutup)
                    tutup_modal_jika_ada()

                    # Verifikasi: cek apakah kartu ulasan sudah muncul
                    for sel_kartu in SELECTOR_KARTU:
                        kartu_cek = driver.find_elements(By.CSS_SELECTOR, sel_kartu)
                        if kartu_cek:
                            tab_terklik = True
                            SELECTOR_KARTU_AKTIF = sel_kartu
                            log.info(f"  Tab ulasan BERHASIL terbuka via '{selector}'. Kartu: '{sel_kartu}' ({len(kartu_cek)})")
                            break
                    if tab_terklik:
                        break
                except Exception:
                    continue
        except Exception:
            continue

    if not tab_terklik:
        log.warning("  PERINGATAN: Tab ulasan tidak berhasil dibuka!")
        # Log semua button yang ada untuk diagnosis
        semua_btn = driver.find_elements(By.CSS_SELECTOR, "button")
        for b in semua_btn[:15]:
            try:
                log.info(f"    DEBUG btn: role={b.get_attribute('role')} aria='{b.get_attribute('aria-label')}' "
                         f"jsaction='{b.get_attribute('jsaction')}' text='{b.text[:40]}'")
            except Exception:
                pass

    jeda(1, 2)

    # 3. Urutkan "Terbaru" — HANYA dari dalam panel ulasan, BUKAN sidebar
    try:
        sort_btn = driver.find_elements(
            By.XPATH,
            '//button[@data-value="Sort" or @aria-label="Sort reviews" '
            'or contains(@aria-label, "Urutkan ulasan") '
            'or contains(@aria-label, "Sort reviews")]'
        )
        if sort_btn:
            try:
                sort_btn[0].click()
            except Exception:
                driver.execute_script("arguments[0].click();", sort_btn[0])
            jeda(1, 1.5)
            tutup_modal_jika_ada()

            # Pilih opsi "Terbaru" hanya dari dalam dropdown menu
            opsi_terbaru = driver.find_elements(
                By.XPATH,
                '//div[@role="menu"]//div[contains(text(),"Terbaru") or contains(text(),"Newest")] '
                '| //div[@role="listbox"]//li[contains(.,"Terbaru") or contains(.,"Newest")] '
                '| //li[@data-index="1"][ancestor::div[@role="menu" or @role="listbox"]]'
            )
            if opsi_terbaru:
                try:
                    opsi_terbaru[0].click()
                except Exception:
                    driver.execute_script("arguments[0].click();", opsi_terbaru[0])
                log.info("  Ulasan berhasil diurutkan ke 'Terbaru' (Newest)")
                jeda(2, 3)
                tutup_modal_jika_ada()
        else:
            log.info("  Tombol sort tidak ditemukan, lanjut dengan urutan default")
    except Exception as e:
        log.warning(f"  Info sortir: {e} (melanjutkan dengan urutan default)")
        tutup_modal_jika_ada()

    # ── Langkah 4: Auto-scroll sampai ulasan termuat ─────────────────────────
    log.info("  Memulai auto-scroll untuk memuat ulasan...")
    jeda(2, 3)

    jumlah_sebelumnya   = 0
    tidak_berubah_count = 0
    MAKS_TIDAK_BERUBAH  = 7  # Beri toleransi 7x jika jaringan agak lambat

    # Selector kartu ulasan: coba dari SELECTOR_KARTU_AKTIF, fallback ke list lengkap
    SEMUA_SELECTOR_KARTU = [
        SELECTOR_KARTU_AKTIF,
        "div.jftiEf",
        "div[data-review-id]",
        "div.MyEned",
        "div.GHT2ce",
        "div[jscontroller][data-review-id]",
    ]
    # Hapus duplikat tapi jaga urutan
    seen = set()
    SEMUA_SELECTOR_KARTU = [x for x in SEMUA_SELECTOR_KARTU if not (x in seen or seen.add(x))]

    def cari_kartu_ulasan():
        """Coba semua selector kartu ulasan, kembalikan yang pertama berhasil."""
        for sel in SEMUA_SELECTOR_KARTU:
            hasil = driver.find_elements(By.CSS_SELECTOR, sel)
            if hasil:
                return hasil, sel
        return [], SELECTOR_KARTU_AKTIF

    for percobaan in range(MAX_SCROLL_TRIES):
        kartu_ulasan, sel_aktif = cari_kartu_ulasan()
        jumlah_sekarang = len(kartu_ulasan)

        log.info(f"  Scroll ke-{percobaan + 1}: {jumlah_sekarang} ulasan termuat (sel: {sel_aktif})")

        # Cek apakah target ulasan per tempat sudah tercapai
        if TARGET_ULASAN_PER_TEMPAT and jumlah_sekarang >= TARGET_ULASAN_PER_TEMPAT:
            log.info(f"  Target {TARGET_ULASAN_PER_TEMPAT} ulasan tercapai untuk tempat ini.")
            break

        # ── Cari kontainer scrollable lewat JS traversal dari kartu terakhir ──
        kontainer_scroll = None
        if kartu_ulasan:
            try:
                kontainer_scroll = driver.execute_script("""
                    var card = arguments[0];
                    var p = card.parentElement;
                    while (p && p !== document.body) {
                        if (p.scrollHeight > p.clientHeight + 40 && p.clientHeight > 150) {
                            return p;
                        }
                        p = p.parentElement;
                    }
                    return document.querySelector('div.m6QErb.DxyBCb.kA9KIf.dS8AEf')
                        || document.querySelector('div.m6QErb.DxyBCb')
                        || document.querySelector('div[role="feed"]');
                """, kartu_ulasan[-1])
            except Exception:
                pass

        # Jika tidak ada kartu, coba scroll kontainer ulasan langsung
        if not kartu_ulasan:
            try:
                kontainer_scroll = driver.execute_script(
                    "return document.querySelector('div.m6QErb.DxyBCb') "
                    "|| document.querySelector('div[role=\"feed\"]');"
                )
            except Exception:
                pass

        # ── Scroll kontainer ulasan ──
        if kontainer_scroll:
            try:
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", kontainer_scroll)
            except Exception:
                pass
            try:
                kontainer_scroll.send_keys(Keys.PAGE_DOWN)
            except Exception:
                pass

        # ── Native Mouse Wheel Scroll via ActionChains pada Kartu Terakhir ──
        if kartu_ulasan:
            try:
                origin = ScrollOrigin.from_element(kartu_ulasan[-1])
                ActionChains(driver).scroll_from_origin(origin, 0, 2500).perform()
            except Exception:
                pass
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({behavior: 'instant', block: 'end'});",
                    kartu_ulasan[-1]
                )
            except Exception:
                pass

        jeda(2.0, 3.5)  # Beri waktu Google Maps memuat batch ulasan baru via network

        # Cek apakah jumlah ulasan bertambah
        kartu_setelah_scroll, _ = cari_kartu_ulasan()
        jumlah_setelah = len(kartu_setelah_scroll)
        if jumlah_setelah == jumlah_sekarang:
            tidak_berubah_count += 1
            if tidak_berubah_count >= MAKS_TIDAK_BERUBAH:
                log.info("  Semua ulasan sudah termuat (tidak ada tambahan baru)")
                break
        else:
            tidak_berubah_count = 0
            jumlah_sebelumnya   = jumlah_setelah
            log.info(f"  -> Ulasan bertambah menjadi {jumlah_setelah} ulasan")
    else:
        log.warning(f"  Batas scroll maksimum {MAX_SCROLL_TRIES}x tercapai.")

    # ── Langkah 5: Klik tombol "Selengkapnya" ────────────────────────────────
    try:
        # Coba beberapa selector tombol "Selengkapnya"
        for sel_lbm in ["button.w8nwRe", "button[aria-label*='Selengkapnya']", "button[aria-label*='more']"]:
            tombol_selengkapnya = driver.find_elements(By.CSS_SELECTOR, sel_lbm)
            if tombol_selengkapnya:
                for btn in tombol_selengkapnya:
                    try:
                        driver.execute_script("arguments[0].click();", btn)
                    except Exception:
                        pass
                log.info(f"  Diklik {len(tombol_selengkapnya)} tombol 'Selengkapnya' (sel: {sel_lbm})")
                break
        jeda(1, 2)
    except Exception:
        pass

    # ── Langkah 6: Ekstrak data dari tiap kartu ulasan ───────────────────────
    log.info("  Mengekstrak data ulasan...")

    # Ambil ulang semua kartu setelah scroll & expand selesai
    kartu_ulasan, sel_aktif_final = cari_kartu_ulasan()
    log.info(f"  Total kartu ditemukan untuk ekstrak: {len(kartu_ulasan)} (sel: {sel_aktif_final})")
    daftar_ulasan = []

    for kartu in kartu_ulasan:
        try:

            # --- Ekstrak Nama Reviewer (dengan fallback selector) ---
            nama_reviewer = "Anonim"
            for sel_nama in ["div.d4r55", "div.rogA2c", "span.d4r55", "button.WEBjve"]:
                try:
                    el = kartu.find_element(By.CSS_SELECTOR, sel_nama)
                    teks = el.text.strip() or el.get_attribute("aria-label") or ""
                    if teks:
                        nama_reviewer = teks
                        break
                except NoSuchElementException:
                    continue

            # --- Ekstrak Rating (dengan fallback) ---
            rating = None
            for sel_rating in ["span.kvMYJc", "span[role='img'][aria-label*='bintang']", "span[role='img'][aria-label*='star']"]:
                try:
                    el = kartu.find_element(By.CSS_SELECTOR, sel_rating)
                    aria_label = el.get_attribute("aria-label") or ""
                    angka = re.search(r"\d+", aria_label)
                    if angka:
                        rating = int(angka.group())
                        break
                except NoSuchElementException:
                    continue

            # --- Ekstrak Teks Ulasan (dengan fallback) ---
            teks_ulasan = ""
            for sel_teks in ["span.wiI7pd", "span.MyEned", "div.MyEned", "span[lang]", "div.review-full-text"]:
                try:
                    el = kartu.find_element(By.CSS_SELECTOR, sel_teks)
                    t = el.text.strip()
                    if t:
                        teks_ulasan = t
                        break
                except NoSuchElementException:
                    continue

            # --- Filter: Skip ulasan tanpa teks ---
            if not teks_ulasan:
                continue

            # Bersihkan newline
            teks_ulasan_bersih = teks_ulasan.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
            teks_ulasan_bersih = re.sub(r"\s+", " ", teks_ulasan_bersih).strip()
            if not teks_ulasan_bersih:
                continue

            # --- Ekstrak Tanggal (dengan fallback) ---
            tanggal = ""
            for sel_tgl in ["span.rsqaWe", "span.dehysf", "span[class*='date']"]:
                try:
                    el = kartu.find_element(By.CSS_SELECTOR, sel_tgl)
                    t = el.text.strip()
                    if t:
                        tanggal = t
                        break
                except NoSuchElementException:
                    continue

            # --- Deduplikasi ---
            kunci_unik = f"{nama_tempat}|{nama_reviewer}|{teks_ulasan_bersih}"
            if kunci_unik in ulasan_existing:
                continue

            # --- Tambahkan ke daftar hasil ---
            ulasan_existing.add(kunci_unik)
            daftar_ulasan.append({
                "nama_tempat"   : nama_tempat_asli,
                "lokasi"        : alamat_lengkap,
                "nama_reviewer" : nama_reviewer,
                "rating"        : rating,
                "tanggal"       : tanggal,
                "teks_ulasan"   : teks_ulasan_bersih,
            })

        except StaleElementReferenceException:
            # Terjadi saat elemen hilang dari DOM karena halaman di-re-render
            log.warning("  StaleElementReferenceException pada satu kartu, dilewati")
            continue
        except Exception as e:
            log.warning(f"  Gagal ekstrak satu ulasan: {e}")
            continue

    log.info(f"  Total {len(daftar_ulasan)} ulasan baru berhasil diekstrak dari '{nama_tempat}'")
    return daftar_ulasan


# ─── Fungsi Utama: Orchestrator ───────────────────────────────────────────────

def main():
    """
    Fungsi utama yang mengatur seluruh alur program:
      1. Baca daftar coffee shop dari CSV input
      2. Cek progress (tempat yang sudah selesai -> skip)
      3. Muat ulasan existing (untuk deduplikasi)
      4. Inisialisasi browser Chrome
      5. Loop: scrape tiap tempat satu per satu
      6. Simpan hasil & catat progress setelah tiap tempat selesai
      7. Catat tempat yang gagal ke log
      8. Tutup browser dan tampilkan ringkasan
    """
    print("=" * 65)
    print("  GOOGLE MAPS REVIEW SCRAPER -- Coffee Shop Bandung")
    print("  Untuk keperluan skripsi: Analisis Sentimen NLP")
    print("=" * 65)

    # ── Baca daftar coffee shop dari file CSV input ────────────────────────
    if not os.path.exists(INPUT_CSV):
        log.error(f"File input '{INPUT_CSV}' tidak ditemukan!")
        log.error("Buat file CSV dengan kolom 'nama_tempat' berisi daftar nama coffee shop.")
        return

    # Coba baca CSV dengan beberapa encoding secara berurutan.
    # Ini mengatasi masalah UnicodeDecodeError jika file disimpan
    # dengan encoding selain UTF-8 (misal: dari Excel yang pakai latin-1).
    df_input = None
    for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
        try:
            df_input = pd.read_csv(INPUT_CSV, encoding=enc)
            log.info(f"File input berhasil dibaca dengan encoding: {enc}")
            break
        except (UnicodeDecodeError, Exception):
            continue

    if df_input is None:
        log.error("Gagal membaca file CSV input dengan semua encoding yang dicoba.")
        log.error("Coba simpan ulang file CSV dengan encoding UTF-8 di Excel/Notepad.")
        return

    if "nama_tempat" not in df_input.columns:
        log.error("Kolom 'nama_tempat' tidak ditemukan di file CSV input!")
        return

    # Ambil list nama tempat, buang nilai kosong, dan strip spasi di depan/belakang
    # Strip penting karena kadang nama ada leading space: " Two Cents" -> "Two Cents"
    daftar_tempat = (
        df_input["nama_tempat"]
        .dropna()
        .str.strip()          # Hapus spasi di awal & akhir nama
        .str.replace(r"\s+", " ", regex=True)  # Hapus spasi ganda di tengah
        .tolist()
    )
    # Buang nama yang jadi kosong setelah di-strip
    daftar_tempat = [n for n in daftar_tempat if n]
    total_tempat  = len(daftar_tempat)


    log.info(f"Total {total_tempat} coffee shop akan di-scrape")

    # ── Baca progress sebelumnya (untuk resume jika script pernah dihentikan) ──
    sudah_selesai = baca_progress()

    # ── Muat ulasan yang sudah ada di CSV (untuk deduplikasi) ─────────────
    ulasan_existing = muat_ulasan_existing()

    # ── Inisialisasi browser Chrome ────────────────────────────────────────
    log.info("Menginisialisasi browser Chrome...")
    driver = buat_driver()

    try:
        # ── Loop: proses tiap coffee shop satu per satu ────────────────────
        for indeks, nama_tempat in enumerate(daftar_tempat, start=1):
            nama_tempat = nama_tempat.strip()

            # Progress indicator: tampilkan status ke terminal
            print(f"\n{'─' * 65}")
            print(f"  Scraping tempat {indeks}/{total_tempat}: {nama_tempat}")
            print(f"{'─' * 65}")
            log.info(f"[{indeks}/{total_tempat}] Memulai: {nama_tempat}")

            # ── Cek apakah tempat ini sudah pernah berhasil di-scrape ──────
            # Jika sudah ada di progress_selesai.txt, lewati
            if nama_tempat in sudah_selesai:
                log.info(f"  SKIP -- '{nama_tempat}' sudah ada di progress, dilewati")
                continue

            # ── Scrape ulasan untuk tempat ini ─────────────────────────────
            try:
                hasil = scrape_satu_tempat(driver, nama_tempat, ulasan_existing)

                # Simpan ke CSV segera setelah tiap tempat selesai.
                # Ini penting: jika script crash di tengah jalan,
                # data yang sudah di-scrape tidak akan hilang.
                simpan_ke_csv(hasil)

                # Catat tempat ini sebagai selesai di file progress
                catat_progress(nama_tempat)
                sudah_selesai.add(nama_tempat)

                log.info(f"  Selesai: {nama_tempat} ({len(hasil)} ulasan baru)")

            except BukanCoffeeshopError as e:
                # Tempat ditemukan di Maps tapi bukan coffee shop → skip, bukan error
                # Tetap catat ke progress agar tidak diproses ulang saat re-run
                catat_skip(nama_tempat, str(e))
                catat_progress(nama_tempat)
                sudah_selesai.add(nama_tempat)
                print(f"  [SKIP] Bukan coffee shop: {nama_tempat}")

            except Exception as e:
                # Jika terjadi error pada satu tempat:
                # - Catat ke log_gagal.txt untuk di-retry nanti
                # - Lanjut ke tempat berikutnya (JANGAN berhenti total)
                catat_gagal(nama_tempat, str(e))
                log.error(f"  Error pada '{nama_tempat}': {e}")
                continue  # Lanjut ke tempat berikutnya

            # ── Jeda antar-tempat ───────────────────────────────────────────
            if indeks < total_tempat:
                jeda(DELAY_MIN, DELAY_MAX)

    finally:
        # Blok finally SELALU dijalankan, bahkan jika ada exception
        # Pastikan browser selalu ditutup dengan rapi
        log.info("Menutup browser...")
        driver.quit()

    # ── Ringkasan akhir ─────────────────────────────────────────────────────
    print(f"\n{'=' * 65}")
    print("  SCRAPING SELESAI!")
    print(f"  Hasil ulasan tersimpan di : {OUTPUT_CSV}")
    print(f"  Tempat yang gagal         : {LOG_GAGAL}")
    print(f"  Log lengkap               : scraper_run.log")
    print(f"{'=' * 65}\n")
    log.info("Semua proses selesai.")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Script hanya berjalan kalau dieksekusi langsung (bukan di-import sebagai module)
    main()
