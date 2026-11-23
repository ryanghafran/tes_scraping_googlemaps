# Google Maps Review Scraper — Coffee Shop Bandung

Script Python untuk mengumpulkan ulasan pelanggan coffee shop di Google Maps,  
untuk keperluan riset skripsi analisis sentimen menggunakan NLP.

---

## Struktur Proyek

```
tes_scraping_googlemaps/
├── scraper.py               # Script utama (jalankan ini)
├── daftar_coffeeshop.csv    # Input: daftar nama coffee shop
├── requirements.txt         # Daftar library yang dibutuhkan
├── README.md                # Panduan ini
│
├── hasil_ulasan.csv         # OUTPUT: semua ulasan terkumpul (dibuat otomatis)
├── log_gagal.txt            # OUTPUT: daftar tempat yang gagal di-scrape
├── progress_selesai.txt     # OUTPUT: daftar tempat yang sudah selesai
└── scraper_run.log          # OUTPUT: log lengkap jalannya program
```

---

## Cara Instalasi

### 1. Pastikan Python 3.9+ sudah terinstall

```bash
python --version
```

### 2. (Opsional) Buat virtual environment

```bash
python -m venv venv
venv\Scripts\activate     # Windows
# atau
source venv/bin/activate  # Linux/Mac
```

### 3. Install semua dependency

```bash
pip install -r requirements.txt
```

Library yang akan diinstall:
| Library | Kegunaan |
|---|---|
| `selenium` | Otomatisasi browser Chrome |
| `webdriver-manager` | Download ChromeDriver otomatis (tidak perlu manual) |
| `pandas` | Baca/tulis file CSV |

> **Catatan:** Google Chrome harus sudah terinstall di komputer Anda.  
> ChromeDriver akan didownload otomatis oleh `webdriver-manager`.

---

## Cara Menjalankan

### 1. Edit file input

Buka `daftar_coffeeshop.csv` dan isi daftar nama coffee shop:

```csv
nama_tempat
Kopi Kenangan Bandung
Starbucks Bandung
Anomali Coffee Bandung
```

> Kolom **harus** bernama `nama_tempat`.

### 2. Jalankan script

```bash
python scraper.py
```

Browser Chrome akan terbuka secara otomatis dan mulai melakukan scraping.

---

## Output

### `hasil_ulasan.csv`

File CSV utama berisi semua ulasan yang berhasil dikumpulkan:

| Kolom | Keterangan |
|---|---|
| `nama_tempat` | Nama coffee shop |
| `lokasi` | Kota (default: Bandung) |
| `nama_reviewer` | Nama pengguna Google Maps |
| `rating` | Nilai 1–5 bintang |
| `tanggal` | Format relatif, misal "2 bulan lalu" |
| `teks_ulasan` | Isi ulasan (teks lengkap) |

### `log_gagal.txt`

Daftar tempat yang gagal di-scrape, beserta alasannya.  
Salin nama-nama ini ke `daftar_coffeeshop.csv` dan jalankan ulang script untuk retry.

### `progress_selesai.txt`

Daftar tempat yang sudah berhasil di-scrape.  
Script akan **otomatis skip** tempat yang sudah ada di sini jika dijalankan ulang.

---

## Fitur Utama

- **Auto-scroll**: Scroll otomatis sampai semua ulasan termuat, dengan deteksi "sudah mentok"
- **Filter ulasan kosong**: Ulasan tanpa teks (rating-only) otomatis dilewati
- **Deduplikasi**: Ulasan yang sama tidak akan masuk dua kali ke CSV
- **Resume otomatis**: Jika script dihentikan di tengah, jalankan ulang dan ia akan lanjut dari tempat terakhir
- **Error handling**: Jika satu tempat gagal, program lanjut ke tempat berikutnya (tidak berhenti total)
- **Anti-bot delay**: Jeda random 3–7 detik antar-request
- **Progress indicator**: Terminal menampilkan status real-time

---

## Konfigurasi

Edit bagian `Konfigurasi Global` di `scraper.py` untuk menyesuaikan:

```python
LOKASI           = "Bandung"  # Ubah jika scraping kota lain
DELAY_MIN        = 3          # Jeda minimum (detik)
DELAY_MAX        = 7          # Jeda maksimum (detik)
MAX_SCROLL_TRIES = 50         # Batas maksimum scroll per tempat
ELEMENT_TIMEOUT  = 15         # Timeout menunggu elemen (detik)
```

---

## Tips & Troubleshooting

**Q: Script berhenti karena Chrome tidak ditemukan?**  
A: Pastikan Google Chrome sudah terinstall. Jika menggunakan browser lain, perlu modifikasi pada fungsi `buat_driver()`.

**Q: Banyak tempat masuk ke `log_gagal.txt`?**  
A: Kemungkinan Google mengubah struktur HTML-nya. Periksa CSS selector di fungsi `scrape_satu_tempat()`.

**Q: Ingin jalankan tanpa jendela browser (background)?**  
A: Aktifkan baris berikut di fungsi `buat_driver()`:
```python
options.add_argument("--headless=new")
```

**Q: Cara retry tempat yang gagal?**  
A: Salin isi `log_gagal.txt` (kolom pertama sebelum `|`) ke `daftar_coffeeshop.csv` dan jalankan ulang script.

---

## Struktur Data Contoh Output

```csv
nama_tempat,lokasi,nama_reviewer,rating,tanggal,teks_ulasan
Kopi Kenangan Bandung,Bandung,Budi Santoso,5,2 bulan lalu,"Kopinya enak banget dan tempatnya nyaman untuk kerja. WiFi kenceng!"
Kopi Kenangan Bandung,Bandung,Siti Rahayu,4,3 bulan lalu,"Pelayanannya ramah tapi antriannya lumayan panjang waktu weekend."
```

---

*Dibuat untuk keperluan skripsi — Analisis Sentimen Ulasan Coffee Shop menggunakan NLP*
