# Google Maps Review Scraper — Coffee Shop Bandung

Script Python untuk mengumpulkan ulasan pelanggan coffee shop di Google Maps,
untuk keperluan riset skripsi analisis sentimen (NLP).

**Target:** 4.000–5.000 ulasan berteks dari daftar cafe di `daftar_coffeeshop.csv`, **dalam sekali jalan**.
Jika terhenti di tengah jalan (Ctrl+C, internet putus, CAPTCHA, Chrome tertutup) cukup jalankan lagi:
progres tersimpan dan ulasan yang sama tidak akan masuk dua kali.

---

## Cara Menjalankan

```bash
pip install -r requirements.txt     # Google Chrome harus sudah terpasang
python scraper.py --login           # SEKALI SAJA: login ke Google di profil scraper (lihat bagian di bawah)
python scraper.py
```

### Wajib: login Google sekali

Tanpa login, Google Maps hanya menampilkan ±3–5 ulasan *preview* per tempat. Saat "Ulasan lainnya" diklik muncul modal
**"Login untuk menikmati fitur terbaik dari Google Maps"** dan ulasan lengkap tidak bisa dibuka. Itulah penyebab utama
data lama hanya ±5 ulasan per tempat, dan **tidak ada trik scroll yang bisa melewatinya**: harus login.

```bash
python scraper.py --login
```

Chrome biasa akan terbuka dengan profil khusus scraper (`.chrome_profile/`). Login ke akun Google, buka satu tempat di
Maps dan pastikan "Ulasan lainnya" (atau tab "Ulasan") menampilkan daftar ulasan, **tutup jendela Chrome**, lalu tekan ENTER
di terminal. Scraper menunggu sampai profil benar-benar dilepas Chrome; jika masih ada Chrome yang memakainya, scraper
menawarkan mematikannya. Sesi login tersimpan di profil itu dan dipakai ulang pada jalan berikutnya.

- **Pakai akun Google khusus, bukan akun utama.** Mengotomatisasi Google Maps bertentangan dengan Ketentuan Layanan
  Google; risikonya akun yang dipakai dibatasi. Jangan berlebihan (jeda acak sudah diatur otomatis).
- Login sengaja dilakukan di Chrome biasa, bukan Chrome yang dikendalikan Selenium, karena Google sering menolak login
  pada browser otomatis.
- Jangan membagikan atau meng-commit folder `.chrome_profile/` (berisi sesi login Anda; sudah masuk `.gitignore`).
- Jika belum login, scraper memberi peringatan di awal. Jika dinding login muncul, scraper **berhenti dengan pesan
  yang jelas** (kode keluar 3) tanpa menandai tempat sebagai gagal; hasil Tahap 1 tetap tersimpan.

Yang terjadi saat `python scraper.py`:

1. **Tahap 1 — cek tempat.** Tiap nama di `daftar_coffeeshop.csv` dicari di Google Maps. Nama resmi, alamat, jumlah
   ulasan, dan **status buka/tutup** dibaca. Cafe berstatus **Tutup permanen** atau **Tutup sementara** otomatis
   **dibuang dari daftar** (salinan asli disimpan di `daftar_coffeeshop_sebelum_dipangkas.csv`, daftar yang dibuang
   dicatat di `daftar_coffeeshop_tutup.csv`).
2. **Tahap 2 — ambil ulasan per ronde.** Sisa target dibagi adil ke semua tempat yang masih punya ulasan
   (tempat kecil yang ulasannya habis tidak menghambat; sisanya dialihkan ke tempat yang masih punya banyak ulasan).
   Hasil disimpan setiap selesai satu tempat.

Perkiraan waktu untuk 5.000 ulasan: sekitar 1–2 jam (jeda sengaja diacak agar mirip manusia). Biarkan jendela Chrome
tetap terbuka (boleh ditutupi aplikasi lain, jangan di-minimize) dan jangan menutupnya.

### Uji cepat dulu (disarankan, ±3 menit)

Setelah `--login`, cek dulu dua hal sebelum jalan penuh (Google sewaktu-waktu mengubah tampilan Maps):

```bash
python scraper.py --cek-status                          # apakah pencarian & status buka/tutup terbaca? (tanpa mengambil ulasan)
python scraper.py --hanya "Two Cents" --target 30       # apakah tab Ulasan terbuka dan scroll memuat >10 ulasan?
```

Jika salah satunya bermasalah, lihat folder `debug/` (screenshot + HTML) dan `scraper_run.log`.

### Opsi

| Perintah | Fungsi |
|---|---|
| `python scraper.py` | Jalan normal sampai target tercapai |
| `python scraper.py --login` | Login ke Google di profil scraper (wajib sekali) |
| `python scraper.py --diagnosa` | Periksa kenapa Chrome tidak bisa dibuka (profil terkunci, versi tidak cocok, dll.) |
| `python scraper.py --cek-status` | Hanya cek cafe mana yang tutup, buang dari daftar, lalu berhenti |
| `python scraper.py --target 4000` | Ubah target total ulasan |
| `python scraper.py --maks-per-tempat 200` | Batasi ulasan per tempat (agar dataset beragam) |
| `python scraper.py --urutan terbaru terendah` | Ambil ulasan terbaru **dan** terendah (ulasan negatif biasanya sedikit; ini membuat dataset sentimen lebih seimbang) |
| `python scraper.py --batas-sesi 200 --istirahat 15` | **Mode hati-hati**: setelah 200 ulasan baru, browser ditutup dan istirahat 15 menit otomatis |
| `python scraper.py --headless` | Tanpa jendela Chrome (CAPTCHA tidak bisa diselesaikan manual) |
| `python scraper.py --hanya "kopi ireng" "noah"` | Hanya proses tempat yang namanya memuat kata tersebut |
| `python scraper.py --reset-gagal` | Coba lagi tempat yang sebelumnya gagal |
| `python scraper.py --ringkasan` | Lihat status semua tempat |
| `python scraper.py --ekspor` | Buat ulang `hasil_ulasan.csv` tanpa scraping |
| `python scraper.py --impor file.csv` | Masukkan ulasan dari CSV lain ke data master |

Semua pengaturan (target, jeda, urutan ulasan, dll.) ada di **`konfigurasi.py`**.

### Perlu menjalankan manual tiap 15 menit?

Tidak perlu. Scraper memanen ulasan sambil scroll dengan jeda acak, menyimpan hasil terus-menerus, dan mendeteksi
tanda-tanda dibatasi Google (CAPTCHA, pemuatan macet). Jika terjadi, kecepatan otomatis diperlambat; jika CAPTCHA muncul
di jendela Chrome, selesaikan manual dan scraper lanjut sendiri (menunggu maksimal 10 menit, lalu berhenti dengan
rapi). Jika ingin ekstra hati-hati, pakai `--batas-sesi 200 --istirahat 15` — itu otomatis meniru pola "200 ulasan
lalu istirahat 15 menit".

---

## Struktur Proyek

```
tes_scraping_googlemaps/
├── scraper.py             # PROGRAM UTAMA (jalankan ini): tahapan, ronde, opsi baris perintah
├── konfigurasi.py         # Semua pengaturan yang boleh Anda ubah
├── gmaps.py               # Logika khusus Google Maps: cari tempat, tab Ulasan, scroll, panen kartu
├── peramban.py            # Chrome/Selenium: pemulihan saat browser mati, deteksi CAPTCHA, login Google
├── penyimpanan.py         # Data master, anti-duplikat, ekspor CSV, impor CSV lama
├── daftar_coffeeshop.csv  # INPUT: daftar nama coffee shop (kolom 'nama_tempat')
├── requirements.txt
│
├── hasil_ulasan.csv       # OUTPUT akhir (dibuat ulang otomatis, aman dibuka di Excel)
├── data/
│   ├── ulasan_master.jsonl    # Data master semua ulasan (jangan diedit)
│   └── status_tempat.json     # Status & kemajuan tiap tempat (untuk resume)
├── daftar_coffeeshop_tutup.csv  # Cafe tutup yang dibuang dari daftar
├── log_gagal.txt          # Satu baris per kegagalan
├── scraper_run.log        # Log lengkap
├── debug/                 # Screenshot + HTML bila ada masalah (untuk diagnosis)
└── tests/                 # Pengujian otomatis
```

### `hasil_ulasan.csv`

Format sama seperti sebelumnya (UTF-8 + BOM, pemisah `;`, semua kolom dikutip):

| Kolom | Keterangan |
|---|---|
| `nama_tempat` | Nama resmi di Google Maps |
| `lokasi` | Alamat lengkap |
| `nama_reviewer` | Nama pengguna Google Maps |
| `rating` | 1–5 |
| `tanggal` | Relatif, mis. "2 bulan lalu" (awalan "Diedit" dibuang) |
| `teks_ulasan` | Isi ulasan lengkap (tombol "Selengkapnya" dibuka otomatis) |

Hanya ulasan **berteks** yang disimpan. Membaca di pandas: `pd.read_csv("hasil_ulasan.csv", sep=";", encoding="utf-8-sig")`.

Pemisah bisa diganti di `konfigurasi.py` (`CSV_SEPARATOR`).

---

## Apa yang Diperbaiki

Analisis log dan data lama menunjukkan bahwa angka "±250" **bukan batas dari Google**: tiap tempat hanya
menghasilkan ±5 ulasan (total file lama: 264 baris, tetapi hanya **220 ulasan unik** dari seluruh `hasil_ulasan*.csv`).
Tangkapan layar Google Maps dari pengguna mengonfirmasi penyebab utamanya: **dinding login**.

| Masalah di versi lama | Perbaikan |
|---|---|
| **Dinding login Google.** Tanpa login hanya ±3–5 ulasan preview yang terlihat; klik "Ulasan lainnya" memunculkan modal login. Versi lama tidak mengenalinya: pengecekan "tab terbuka" lolos karena kartu preview ada, jadi hanya ±5 ulasan per tempat yang terambil tanpa pesan error apa pun. | Login sekali lewat `--login` (sesi tersimpan di profil scraper). Dinding login dideteksi: program berhenti dengan instruksi jelas dan tidak menandai tempat sebagai gagal. Peringatan dini bila profil belum login. |
| Tab "Ulasan" dianggap terbuka hanya karena ada kartu ulasan di halaman. | Tab diverifikasi lewat status tab terpilih (`aria-selected`), tombol "Urutkan", atau URL. Tombol di dalam kartu ulasan diabaikan. |
| Kartu dibaca sekali di akhir; kemacetan pemuatan tidak ditangani. | Kartu dipanen di **setiap langkah scroll** berdasarkan `data-review-id`. Bila macet: scroll naik-turun, event roda mouse, lalu muat ulang halaman. |
| Target "160 per tempat" dihitung dari jumlah *kartu* termasuk yang tanpa teks. | Target dihitung dari ulasan **berteks**; sisa target dibagi adil per ronde. |
| Duplikat: kunci anti-duplikat memakai nama kueri, padahal CSV berisi nama resmi → tidak pernah cocok, tiap jalan ulang menambah ulasan yang sama. | Kunci memakai nama resmi + reviewer + teks (dinormalisasi) dan id ulasan. |
| CSV dibaca ulang oleh scraper, padahal sering dibuka/disimpan ulang di Excel (separator, kutip, dan kolom jadi rusak). `fix_csv.py` membaca file `;` dengan pemisah koma sehingga menghasilkan file `_fixed_` yang berantakan. | Sumber data adalah `data/ulasan_master.jsonl` (tidak disentuh Excel). CSV hanya hasil ekspor yang dibuat ulang; bila terkunci Excel, disimpan ke nama berstempel waktu. `fix_csv.py` dan `fix_input_csv.py` dihapus. |
| "invalid session id": sekali Chrome tertutup/crash, loop tetap jalan sehingga ke-39 tempat langsung gagal (dan 937 baris stacktrace masuk `log_gagal.txt`). | Browser mati dikenali lalu Chrome dibuka ulang dan tempat diulang. Kegagalan beruntun menghentikan program dengan rapi. Log hanya satu baris per kegagalan. |
| Tempat dengan 0 ulasan tetap dicatat "selesai" sehingga tidak pernah dicoba lagi. | 0 kartu padahal Maps menyebut ada ulasan = **gagal** (screenshot + HTML disimpan di `debug/`). |
| User-Agent dipalsukan "Chrome/124" pada Chrome versi lain (mudah dikenali bot); `navigator.webdriver` hanya dihapus di halaman kosong awal. | UA asli dipakai; penyamaran dipasang untuk semua halaman; jendela di latar belakang tidak "ditidurkan" (flag anti-throttling). |
| Status tutup tidak dicek. | Tutup permanen/sementara terdeteksi (bahasa Indonesia & Inggris) dan dibuang dari daftar. |

Data lama **tidak hilang**: pada jalan pertama, semua `hasil_ulasan*.csv` diimpor sekali ke data master
(220 ulasan unik) dan dihitung ke kuota tiap tempat. Setelah itu `hasil_ulasan.csv` diganti dengan versi bersih
hasil ekspor (tanpa duplikat). Berkas CSV lama (`hasil_ulasan_backup.csv`, `hasil_ulasan_fixed_*.csv`, dst.) tidak
diubah dan boleh diarsipkan setelah jalan pertama selesai; `progress_selesai.txt` dan `log_skip_kategori.txt`
tidak dipakai lagi.

---

## Tips & Troubleshooting

**Total ulasan kurang dari target?**
Lihat ringkasan di akhir (atau `python scraper.py --ringkasan`). Kolom "Di Maps" menunjukkan jumlah ulasan tiap tempat
(termasuk yang tanpa teks, biasanya sekitar separuhnya). Solusi: tambah nama cafe di `daftar_coffeeshop.csv`, naikkan
`--maks-per-tempat`, atau tambahkan `--urutan terbaru terendah`.

**Chrome tertutup / crash di tengah jalan?**
Ditangani otomatis (Chrome dibuka ulang). Jika sering terjadi, tutup semua jendela Chrome sisa proses lama, lalu
jalankan ulang. Scraper memakai profil Chrome terpisah (`.chrome_profile/`), bukan profil Chrome harian Anda.

**Muncul CAPTCHA?**
Selesaikan manual di jendela Chrome; scraper lanjut sendiri. Bila berulang, pakai mode hati-hati
(`--batas-sesi 200 --istirahat 15`), tunggu 30–60 menit, atau ganti jaringan (mis. hotspot HP).

**Error "session not created: Chrome instance exited" / "Chrome tidak bisa dibuka dengan profil scraper"?**
Hampir selalu berarti folder profil `.chrome_profile` masih dipakai proses Chrome lain: jendela login (`--login`) yang
belum benar-benar tertutup, atau sisa Chrome dari jalan sebelumnya yang crash. Scraper menunggu lalu mencoba lagi dan,
di terminal interaktif, **menawarkan mematikan proses Chrome yang memakai profil scraper** (Chrome biasa Anda aman).
Jika masih gagal: tutup semua jendela Chrome, akhiri proses "Google Chrome" di Task Manager (atau restart komputer), lalu
jalankan ulang. Untuk mengetahui penyebab pastinya jalankan:

```bash
python scraper.py --diagnosa
```

lalu kirim keluarannya beserta `debug/chromedriver_diagnosa.log`. Perintah itu menguji membuka Chrome dengan profil
scraper dan dengan profil kosong, sehingga jelas apakah masalahnya di profil (terkunci/rusak) atau di Chrome/chromedriver.

**Muncul pesan "[BERHENTI] Google meminta LOGIN untuk membuka ulasan lengkap"?**
Jalankan `python scraper.py --login` (lihat bagian "Wajib: login Google sekali"), lalu jalankan scraper lagi. Jika
scraper melapor "Profil Chrome scraper sedang dipakai Chrome lain", tutup semua jendela Chrome yang dibuka lewat
`--login` dan matikan sisa `chrome.exe` di Task Manager. Hasil Tahap 1 (nama resmi, status tutup, jumlah ulasan)
sudah tersimpan sehingga tidak diulang.

**Login tidak berhasil / akun dibatasi Google?**
Jangan ulangi terus-menerus. Alternatifnya: layanan pihak ketiga yang menjalankan pengambilan data untuk Anda lewat API
(mis. Apify, Outscraper, SerpApi; umumnya berbayar atau berkuota gratis terbatas, periksa harga dan kuota terbaru).
Hasilnya bisa dimasukkan dengan `python scraper.py --impor hasil.csv` (kolom harus dinamai `nama_tempat`, `lokasi`,
`nama_reviewer`, `rating`, `tanggal`, `teks_ulasan`). API resmi Google Places hanya mengembalikan maksimal 5 ulasan per
tempat sehingga tidak cukup untuk target ini.

**Salah memilih tempat / cabang?**
Nama resmi yang terpilih tampil di log dan di kolom `nama_tempat`. Jika berbeda dari yang dimaksud, tulis nama di
`daftar_coffeeshop.csv` lebih spesifik (mis. tambahkan nama jalan).

**Muncul "PERINGATAN KUALITAS DATA"?**
Artinya lebih dari 20% ulasan yang baru dipanen tidak punya rating / tanggal / nama reviewer, atau teksnya masih
terpotong (tombol "Selengkapnya" tidak terbuka). Biasanya tampilan Google Maps berubah dan salah satu bacaan kolom tidak
cocok lagi. Scraper tetap berjalan dan menyimpan screenshot + HTML ke `debug/` (nama berawalan `kualitas_`); kirim
folder itu agar bacaannya bisa diperbaiki. Hentikan dulu bila Anda tidak ingin data yang kolomnya kosong ikut tersimpan.

**Masih macet / hasil aneh?**
Folder `debug/` berisi screenshot + HTML saat terjadi masalah (tab Ulasan gagal dibuka, daftar kosong, macet, CAPTCHA).
Sertakan folder itu beserta `scraper_run.log` saat meminta bantuan.

**Mulai dari nol?** Hapus folder `data/` (data master + status). Data lama di `hasil_ulasan*.csv` akan diimpor ulang.

**Chrome tidak ditemukan / chromedriver gagal diunduh?**
Atur variabel lingkungan `CHROME_BINARY` (path `chrome.exe`) dan/atau `CHROMEDRIVER_PATH` (path chromedriver yang
versinya sama dengan Chrome Anda).

**Catatan etika:** nama reviewer adalah data pribadi. Untuk dataset yang dipublikasikan, pertimbangkan membuang
kolom `nama_reviewer` setelah pengumpulan selesai.

---

## Pengujian

Google Maps tidak bisa diakses dari lingkungan pengujian otomatis, jadi scraper diuji terhadap halaman tiruan
(`tests/mock_maps`) yang meniru perilaku penting Google Maps (kartu preview di tab Ringkasan, lazy-load 10 ulasan per
scroll, pemuat yang macet, CAPTCHA, dinding login, browser yang mati, dst.) memakai Chrome sungguhan.

```bash
pip install pytest pandas
pytest -q                          # semua tes (tes integrasi dilewati otomatis bila Chrome tidak tersedia)
pytest -q -m "not integrasi"       # tanpa browser
```

> Pengujian terhadap tiruan membuktikan logika scraper benar, tetapi **bukan** jaminan Google tidak mengubah tampilannya.
> Jika suatu saat tab Ulasan gagal dibuka / kartu tidak terbaca, lihat folder `debug/`.

---

*Dibuat untuk keperluan skripsi — Analisis Sentimen Ulasan Coffee Shop menggunakan NLP*
