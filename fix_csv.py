"""
fix_csv.py
----------
Script untuk merapikan hasil_ulasan.csv menjadi hasil_ulasan_fixed.csv.

Masalah yang diperbaiki:
  - Teks ulasan yang mengandung newline (baris baru) menyebabkan baris CSV
    terpecah menjadi beberapa baris, sehingga kolom jadi berantakan.

Solusi:
  - Baca CSV dengan pandas (pandas bisa handle quoted field yang mengandung newline)
  - Bersihkan karakter newline di dalam teks ulasan -> ganti dengan spasi
  - Simpan ke file baru: hasil_ulasan_fixed.csv
"""

import pandas as pd
import re
from datetime import datetime

INPUT_FILE  = "hasil_ulasan.csv"

# Output file pakai timestamp agar tidak bentrok dengan file yang sedang terbuka
# Contoh nama: hasil_ulasan_fixed_20260921_200000.csv
timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_FILE = f"hasil_ulasan_fixed_{timestamp}.csv"

print(f"Membaca {INPUT_FILE}...")

# pandas otomatis handle quoted CSV field yang mengandung newline
df = pd.read_csv(INPUT_FILE, encoding="utf-8-sig")

print(f"Jumlah baris sebelum fix : {len(df)}")
print(f"Kolom                    : {list(df.columns)}")

# ── Bersihkan kolom teks_ulasan ──────────────────────────────────────────────
# Ganti semua \n, \r\n, \r di dalam teks dengan satu spasi
# lalu hapus spasi ganda yang mungkin muncul
def bersihkan_teks(teks):
    if pd.isna(teks):
        return ""
    teks = str(teks)
    teks = teks.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    teks = re.sub(r" +", " ", teks)   # Hapus spasi ganda
    return teks.strip()

df["teks_ulasan"] = df["teks_ulasan"].apply(bersihkan_teks)

# ── Bersihkan kolom lain yang mungkin ikut kotor ────────────────────────────
for kolom in ["nama_tempat", "lokasi", "nama_reviewer", "tanggal"]:
    if kolom in df.columns:
        df[kolom] = df[kolom].astype(str).str.strip()

# ── Hapus baris yang teks_ulasan-nya jadi kosong setelah dibersihkan ─────────
df = df[df["teks_ulasan"].str.len() > 0]

# ── Hapus duplikat (jika ada) ─────────────────────────────────────────────────
sebelum_dedup = len(df)
df = df.drop_duplicates(subset=["nama_tempat", "nama_reviewer", "teks_ulasan"])
setelah_dedup = len(df)

if sebelum_dedup != setelah_dedup:
    print(f"Duplikat dihapus         : {sebelum_dedup - setelah_dedup} baris")

# ── Reset index ───────────────────────────────────────────────────────────────
df = df.reset_index(drop=True)

print(f"Jumlah baris setelah fix : {len(df)}")

# ── Simpan ke file baru ───────────────────────────────────────────────────────
# quoting=1 (QUOTE_ALL) -> semua field dibungkus tanda kutip agar aman
import csv
try:
    df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig",   # utf-8-sig agar bisa dibuka Excel tanpa masalah encoding
        quoting=csv.QUOTE_ALL,  # Paksa semua kolom diapit tanda kutip -> lebih aman
    )
    print(f"\nSelesai! File bersih disimpan ke: {OUTPUT_FILE}")
    print("Buka file tersebut di Excel atau VS Code untuk memverifikasi.")
except PermissionError:
    print(f"\n[ERROR] Tidak bisa menyimpan ke '{OUTPUT_FILE}'.")
    print("        Kemungkinan file tersebut sedang terbuka di Excel atau program lain.")
    print("        Tutup file tersebut terlebih dahulu, lalu jalankan script ini lagi.")
