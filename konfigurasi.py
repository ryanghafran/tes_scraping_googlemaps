"""
konfigurasi.py
==============
SEMUA pengaturan scraper ada di file ini. Ubah seperlunya, lalu jalankan:  python scraper.py

Sebagian besar pengaturan juga bisa diganti lewat opsi baris perintah, mis.:
    python scraper.py --target 4000 --headless
(lihat:  python scraper.py --help)
"""

import os

# Semua path relatif dihitung dari folder tempat file ini berada, jadi script
# boleh dijalankan dari folder mana pun.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _p(*bagian):
    return os.path.join(BASE_DIR, *bagian)


# ─── File input / output ──────────────────────────────────────────────────────

INPUT_CSV     = _p("daftar_coffeeshop.csv")    # Input: daftar nama coffee shop (kolom 'nama_tempat')
OUTPUT_CSV    = _p("hasil_ulasan.csv")         # OUTPUT utama (dibuat ulang otomatis dari data master)
CSV_SEPARATOR = ";"                            # ';' = langsung rapi di Excel Indonesia. Ganti ',' bila perlu.
FILE_TUTUP    = _p("daftar_coffeeshop_tutup.csv")  # Cafe tutup permanen/sementara yang dibuang dari daftar

# Sumber daftar tempat (blog Wanderlog). `python wanderlog.py` membuat INPUT_CSV dari sini,
# lengkap dengan Google place ID, koordinat & alamat tiap cabang.
URL_WANDERLOG = "https://wanderlog.com/list/geoCategory/16946"

DIR_DATA      = _p("data")
FILE_MASTER   = _p("data", "ulasan_master.jsonl")  # SUMBER KEBENARAN semua ulasan (jangan diedit manual)
FILE_STATUS   = _p("data", "status_tempat.json")   # Status & kemajuan tiap tempat (untuk resume)
FILE_LEGACY_OK = _p("data", "impor_legacy.selesai")

LOG_RUN       = _p("scraper_run.log")          # Log lengkap
LOG_GAGAL     = _p("log_gagal.txt")            # Ringkasan tempat yang gagal
DIR_DEBUG     = _p("debug")                    # Screenshot + HTML saat ada masalah (untuk diagnosis)
DIR_PROFIL    = _p(".chrome_profile")          # Profil Chrome khusus scraper (cookie/consent tersimpan)

# Berkas CSV lama yang akan diimpor SEKALI saat pertama kali scraper baru dijalankan,
# supaya ulasan yang sudah Anda kumpulkan tidak hilang & tidak terduplikasi.
POLA_CSV_LEGACY = [_p("hasil_ulasan*.csv"), _p("arsip", "*.csv")]


# ─── Target pengumpulan ───────────────────────────────────────────────────────

TARGET_TOTAL_ULASAN    = 5000   # Berhenti jika total ulasan BERTEKS (unik) sudah segini
MAKS_ULASAN_PER_TEMPAT = 500    # Batas keras per tempat agar dataset tidak didominasi satu cafe
MAKS_RONDE             = 6      # Maks putaran. Tiap ronde membagi sisa target ke tempat yang masih punya ulasan

# Urutan ulasan yang diambil. Pilihan: "relevan", "terbaru", "tertinggi", "terendah".
# Kuota tiap tempat dibagi rata ke urutan-urutan ini. Untuk dataset analisis sentimen yang
# lebih seimbang (ulasan negatif biasanya sedikit), pertimbangkan ["terbaru", "terendah"].
URUTAN_ULASAN = ["terbaru"]


# ─── Pencarian ────────────────────────────────────────────────────────────────

LOKASI    = "Bandung"                         # Ditambahkan ke kata kunci pencarian
BAHASA    = "id"                              # Bahasa antarmuka Google Maps (parameter hl=)
MAPS_BASE = "https://www.google.com/maps"

# Cabang persis: bila daftar input punya kolom place_id/fid, tempat dibuka lewat ID itu lalu DIVERIFIKASI
# (ID hasil buka harus sama, atau koordinat <= JARAK_MAKS_CABANG_METER dari koordinat Wanderlog).
# Jika tidak cocok, tempat TIDAK di-scrape (lebih baik kosong daripada salah cabang).
JARAK_MAKS_CABANG_METER = 150

# Filter kategori (opsional, default mati). Hanya dipakai jika kategori berhasil dibaca.
FILTER_KATEGORI = False
KATEGORI_VALID  = ["cafe", "coffee shop", "kedai kopi", "kafe", "coffee", "restoran",
                   "bakery", "roti", "eatery", "toko kue"]


# ─── Browser ──────────────────────────────────────────────────────────────────

HEADLESS        = False   # True = tanpa jendela. Jangan diaktifkan jika ingin bisa menyelesaikan CAPTCHA manual.
CHROME_BINARY   = os.environ.get("CHROME_BINARY", "")        # Opsional: path chrome.exe khusus
CHROMEDRIVER_PATH = os.environ.get("CHROMEDRIVER_PATH", "")  # Opsional: path chromedriver sendiri
TIMEOUT_HALAMAN = 45      # Detik maks menunggu satu halaman dimuat
TIMEOUT_ELEMEN  = 15      # Detik maks menunggu elemen penting muncul


# ─── Kecepatan & anti-bot ─────────────────────────────────────────────────────
# Semua jeda diacak di antara dua angka (detik). Google Maps memuat 10 ulasan per scroll.

JEDA_SCROLL       = (1.0, 2.2)    # Jeda antar scroll
JEDA_ANTAR_TEMPAT = (5.0, 10.0)   # Jeda antar tempat
JEDA_BUKA_HALAMAN = (2.0, 4.0)    # Jeda setelah membuka halaman

# Mode hati-hati (meniru "100-200 ulasan lalu istirahat 15 menit", tapi otomatis):
# setelah mengumpulkan sekian ulasan BARU dalam satu sesi, browser ditutup dan scraper istirahat.
# 0 = nonaktif (jalan terus). Dicek tiap selesai satu tempat.
BATAS_ULASAN_PER_SESI = 0
ISTIRAHAT_MENIT       = 15

# Jika Google menampilkan CAPTCHA, scraper menunggu Anda menyelesaikannya manual di jendela
# Chrome (hanya jika HEADLESS = False). Setelah batas waktu ini scraper berhenti dengan rapi
# dan bisa dijalankan lagi nanti (progres tersimpan).
TUNGGU_CAPTCHA_MENIT = 10


# ─── Ketahanan (robustness) ───────────────────────────────────────────────────

MAKS_PERCOBAAN_TEMPAT = 3     # Percobaan ulang per tempat dalam satu kali proses (mis. browser mati)
MAKS_GAGAL_TOTAL      = 5     # Setelah segini kali gagal (di semua sesi), tempat dilewati sampai --reset-gagal
MAKS_GAGAL_BERUNTUN   = 5     # Berhenti jika segini tempat gagal berturut-turut (kemungkinan diblokir)

TIMEOUT_MUAT_BATCH    = 9     # Detik menunggu kartu baru muncul setelah scroll
MAKS_MACET            = 4     # Berapa kali scroll tanpa hasil sebelum dianggap macet/habis
TIMEOUT_DAFTAR_SETELAH_URUTAN = 25   # Detik menunggu daftar terisi kembali setelah memilih urutan
TIMEOUT_DAFTAR_KOSONG = 15           # Detik tambahan sebelum daftar yang masih kosong dinyatakan kosong
MIN_TAMBAH_PER_KUNJUNGAN = 10        # Tempat yang hanya perlu < segini ulasan baru tidak dibuka (1 kunjungan = 30-60 detik)
MAKS_RELOAD_PER_TEMPAT = 1    # Muat ulang halaman tempat jika macet sebelum daftar habis (di praktik jarang menolong)
MAKS_LANGKAH_SCROLL   = 900   # Pengaman: maks scroll per tempat per urutan
MAKS_MENIT_PER_TEMPAT = 25    # Pengaman: maks waktu per tempat per urutan
MAKS_DEBUG_PER_SESI   = 12    # Maks screenshot/HTML debug per sesi
