"""
penyimpanan.py
==============
Lapisan data scraper. Tujuan utamanya: data TIDAK PERNAH rusak/ganda lagi.

Masalah di versi lama:
  * Scraper membaca ulang 'hasil_ulasan.csv' untuk deteksi duplikat, padahal file itu sering
    dibuka/disimpan ulang di Excel (separator berubah, kutip rusak, kolom bergeser).
  * Kunci duplikat memakai nama dari kolom CSV (nama resmi di Maps) tetapi dibandingkan dengan
    nama kueri pencarian -> TIDAK PERNAH cocok -> tiap jalan ulang menambah ulasan yang sama.

Solusi:
  * Sumber kebenaran = 'data/ulasan_master.jsonl' (satu ulasan per baris, hanya ditambah/append,
    tidak pernah dibaca dari Excel). 'hasil_ulasan.csv' hanyalah EKSPOR yang dibuat ulang kapan saja.
  * Kunci duplikat = (nama tempat resmi + nama reviewer + teks) yang dinormalisasi, ditambah id
    ulasan dari Google Maps bila ada.
"""

from __future__ import annotations

import csv
import glob
import hashlib
import io
import json
import logging
import os
import re
import shutil
from collections import Counter
from datetime import datetime

import konfigurasi as cfg

log = logging.getLogger(__name__)

KOLOM_CSV = ["nama_tempat", "lokasi", "nama_reviewer", "rating", "tanggal", "teks_ulasan"]


# ─── Utilitas teks ────────────────────────────────────────────────────────────

def rapikan_spasi(s) -> str:
    """Ganti semua whitespace (termasuk newline) dengan satu spasi, lalu strip."""
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def norm(s) -> str:
    """Bentuk normal untuk perbandingan: spasi dirapikan + huruf kecil."""
    return rapikan_spasi(s).casefold()


def kunci_ulasan(nama_tempat, nama_reviewer, teks) -> str:
    """Kunci duplikat berbasis isi. Stabil walau file CSV diedit/di-save ulang."""
    mentah = f"{norm(nama_tempat)}|{norm(nama_reviewer)}|{norm(teks)}"
    return hashlib.sha1(mentah.encode("utf-8")).hexdigest()


def _ke_int_rating(nilai):
    """'5' -> 5; '' / None / teks lain -> None. Hanya menerima 1..5."""
    try:
        angka = int(float(str(nilai).strip().replace(",", ".")))
    except (ValueError, TypeError):
        return None
    return angka if 1 <= angka <= 5 else None


def _sekarang() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─── Pembaca CSV toleran (untuk mengimpor file lama yang rusak) ───────────────

def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse(teks: str, delim: str):
    return list(csv.reader(io.StringIO(teks, newline=""), delimiter=delim))


def _header_valid(baris) -> bool:
    h = [c.strip().strip('"').strip() for c in baris]
    return "nama_tempat" in h and "teks_ulasan" in h


def _bongkar_baris_terbungkus(rows):
    """
    Memperbaiki file yang SELURUH isi barisnya terbungkus menjadi satu sel berkutip, diikuti
    deretan separator kosong (akibat file koma dibaca dengan separator titik-koma lalu disimpan
    ulang di Excel). Isi sel pertama di-parse ulang sebagai baris CSV berseparator koma.
    """
    hasil = []
    for r in rows:
        if not r:
            continue
        sel = r[0]
        hasil.append(next(csv.reader(io.StringIO(sel, newline=""), delimiter=","), []))
    return hasil


def baca_csv_toleran(path: str):
    """
    Membaca CSV ulasan dalam format apa pun yang pernah dihasilkan proyek ini
    (separator ';' / ',' / tab, dengan atau tanpa BOM, trailing separator, baris terbungkus).

    Returns:
        (daftar_dict, jumlah_baris_rusak). Setiap dict berisi KOLOM_CSV; rating = int | None.
    """
    with open(path, "rb") as f:
        teks = _decode(f.read())

    rows = None
    for delim in (";", ",", "\t"):
        kandidat = _parse(teks, delim)
        if kandidat and _header_valid(kandidat[0]):
            rows = kandidat
            break
    if rows is None:  # coba selamatkan file yang barisnya terbungkus satu sel
        for delim in (";", ","):
            kandidat = _bongkar_baris_terbungkus(_parse(teks, delim))
            if kandidat and _header_valid(kandidat[0]):
                rows = kandidat
                break
    if rows is None:
        return [], 0

    header = [c.strip().strip('"').strip() for c in rows[0]]
    idx = {h: i for i, h in enumerate(header) if h}
    n_kolom = len(KOLOM_CSV)
    hasil, rusak = [], 0

    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        r = list(r)
        while len(r) > n_kolom and r[-1] == "":      # trailing separator
            r.pop()
        rec = _baris_ke_rekaman(r, idx)
        if rec is None and len(r) == n_kolom + 1:
            # Kasus nyata: 'Sejiwa Coffee, Kiputih' terpecah jadi dua kolom -> gabungkan kembali.
            rec = _baris_ke_rekaman([r[0].strip() + "," + r[1]] + r[2:], idx)
        if rec is None:
            rusak += 1
            continue
        hasil.append(rec)
    return hasil, rusak


def _baris_ke_rekaman(r, idx):
    try:
        rec = {k: r[idx[k]] for k in KOLOM_CSV}
    except (IndexError, KeyError):
        return None
    mentah_rating = str(rec["rating"]).strip()
    if mentah_rating != "" and _ke_int_rating(mentah_rating) is None:
        return None                                   # kolom bergeser -> baris rusak
    rec["rating"] = _ke_int_rating(mentah_rating)
    if not rapikan_spasi(rec["teks_ulasan"]):
        return None
    return rec


# ─── Penyimpanan ulasan (master JSONL) ────────────────────────────────────────

class PenyimpananUlasan:
    """Menyimpan ulasan secara append-only dan menjaga keunikannya."""

    def __init__(self, path: str | None = None):
        self.path = path or cfg.FILE_MASTER
        self.rekaman: list[dict] = []
        self._ids: set[str] = set()
        self._kunci: set[str] = set()
        self._per_tempat: Counter = Counter()
        self._fh = None
        self._muat()

    # -- muat ---------------------------------------------------------------
    def _muat(self):
        if not os.path.exists(self.path):
            return
        rusak = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for baris in f:
                baris = baris.strip()
                if not baris:
                    continue
                try:
                    r = json.loads(baris)
                    r["kunci"]  # harus ada
                except (json.JSONDecodeError, KeyError):
                    rusak += 1
                    continue
                self._daftarkan(r)
        if rusak:
            log.warning(f"{rusak} baris rusak di '{os.path.basename(self.path)}' dilewati")

    def _daftarkan(self, r: dict):
        self.rekaman.append(r)
        if r.get("id_ulasan"):
            self._ids.add(r["id_ulasan"])
        self._kunci.add(r["kunci"])
        self._per_tempat[norm(r["nama_tempat"])] += 1

    # -- tulis --------------------------------------------------------------
    def _buka(self):
        if self._fh is not None:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        perlu_newline = False
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            with open(self.path, "rb") as f:        # baris terakhir mungkin terpotong akibat crash
                f.seek(-1, os.SEEK_END)
                perlu_newline = f.read(1) != b"\n"
        self._fh = open(self.path, "a", encoding="utf-8")
        if perlu_newline:
            self._fh.write("\n")

    def tambah(self, nama_tempat, lokasi, nama_reviewer, rating, tanggal, teks,
               id_ulasan: str = "", sumber: str = "scrape", urutan: str = "", id_tempat: str = "") -> bool:
        """Menambah satu ulasan. Return True jika benar-benar baru (bukan duplikat, teks tidak kosong)."""
        teks = rapikan_spasi(teks)
        if not teks:
            return False
        nama_tempat = rapikan_spasi(nama_tempat)
        nama_reviewer = rapikan_spasi(nama_reviewer) or "Anonim"
        kunci = kunci_ulasan(nama_tempat, nama_reviewer, teks)
        if (id_ulasan and id_ulasan in self._ids) or kunci in self._kunci:
            return False
        r = {
            "kunci": kunci, "id_ulasan": id_ulasan, "id_tempat": id_tempat,
            "nama_tempat": nama_tempat, "lokasi": rapikan_spasi(lokasi),
            "nama_reviewer": nama_reviewer, "rating": _ke_int_rating(rating),
            "tanggal": rapikan_spasi(tanggal), "teks_ulasan": teks,
            "sumber": sumber, "urutan": urutan, "diambil": _sekarang(),
        }
        self._buka()
        self._fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._daftarkan(r)
        return True

    def tutup(self):
        if self._fh is not None:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
            except OSError:
                pass
            self._fh.close()
            self._fh = None

    # -- kueri --------------------------------------------------------------
    def total(self) -> int:
        return len(self.rekaman)

    def jumlah_tempat(self, nama_tempat: str) -> int:
        return self._per_tempat[norm(nama_tempat)]

    def per_tempat(self) -> Counter:
        return Counter({k: v for k, v in self._per_tempat.items()})

    # -- ekspor CSV ---------------------------------------------------------
    def ekspor_csv(self, path: str | None = None, urutan_tempat: list[str] | None = None) -> str:
        """
        Membuat ulang CSV hasil dari data master. Selalu format yang sama:
        UTF-8 dengan BOM, separator cfg.CSV_SEPARATOR, SEMUA kolom dikutip (aman untuk koma/newline).
        Jika file target terkunci (mis. terbuka di Excel), disimpan ke nama berstempel waktu.

        Returns: path file yang benar-benar ditulis.
        """
        path = path or cfg.OUTPUT_CSV
        urut = {}
        for i, n in enumerate(urutan_tempat or []):
            urut.setdefault(norm(n), i)
        # kelompokkan per tempat (urutan sesuai daftar input; tempat tak dikenal di akhir), stabil
        baris = sorted(self.rekaman, key=lambda r: urut.get(norm(r["nama_tempat"]), len(urut)))

        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=cfg.CSV_SEPARATOR, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerow(KOLOM_CSV)
            for r in baris:
                w.writerow([r["nama_tempat"], r["lokasi"], r["nama_reviewer"],
                            "" if r["rating"] is None else r["rating"], r["tanggal"], r["teks_ulasan"]])
        try:
            os.replace(tmp, path)
            return path
        except PermissionError:
            akar, ekst = os.path.splitext(path)
            alt = f"{akar}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ekst}"
            os.replace(tmp, alt)
            log.warning(f"'{os.path.basename(path)}' sedang terkunci (terbuka di Excel?). "
                        f"Hasil disimpan ke '{os.path.basename(alt)}' sebagai gantinya.")
            return alt

    # -- impor --------------------------------------------------------------
    def impor_csv(self, path: str, sumber: str = "impor"):
        """Memasukkan isi sebuah CSV ke master. Returns (dibaca, baru, rusak)."""
        rows, rusak = baca_csv_toleran(path)
        baru = 0
        for r in rows:
            if self.tambah(r["nama_tempat"], r["lokasi"], r["nama_reviewer"], r["rating"],
                           r["tanggal"], r["teks_ulasan"], sumber=sumber):
                baru += 1
        return len(rows), baru, rusak

    def impor_csv_lama_sekali(self):
        """Impor otomatis CSV hasil scraping versi lama (hanya sekali, ditandai file penanda)."""
        if os.path.exists(cfg.FILE_LEGACY_OK):
            return
        berkas = []
        for pola in cfg.POLA_CSV_LEGACY:
            berkas.extend(sorted(glob.glob(pola)))
        total_baru = 0
        for b in dict.fromkeys(berkas):                 # unik, urutan tetap
            try:
                dibaca, baru, rusak = self.impor_csv(b, sumber="impor-lama")
            except Exception as e:                      # satu file buruk tidak boleh menggagalkan semuanya
                log.warning(f"Impor '{os.path.basename(b)}' gagal: {e}")
                continue
            total_baru += baru
            log.info(f"Impor data lama '{os.path.basename(b)}': {dibaca} baris dibaca, "
                     f"{baru} ulasan unik baru, {rusak} baris rusak dilewati")
        os.makedirs(cfg.DIR_DATA, exist_ok=True)
        with open(cfg.FILE_LEGACY_OK, "w", encoding="utf-8") as f:
            f.write(f"{_sekarang()} | {total_baru} ulasan unik diimpor dari {len(berkas)} berkas\n")
        if total_baru:
            log.info(f"Total {total_baru} ulasan unik dari data lama diselamatkan ke master.")


# ─── Status per tempat ────────────────────────────────────────────────────────

class StatusTempat:
    """
    Menyimpan hasil pencarian & kemajuan tiap tempat di 'data/status_tempat.json'.
    Kunci = nama sesuai daftar input (dinormalisasi).

    status_buka   : '' | 'aktif' | 'tutup_permanen' | 'tutup_sementara'
    status_scrape : 'belum' | 'sebagian' | 'selesai' | 'gagal' | 'tidak_ditemukan' | 'duplikat'
    habis         : True jika seluruh ulasan tempat itu sudah terambil / tidak ada lagi yang bisa diambil
    """

    DEFAULT = {
        "nama_input": "", "nama_resmi": "", "alamat": "", "kategori": "", "url": "", "id_tempat": "",
        "rating": None, "total_ulasan": None,
        "status_buka": "", "status_scrape": "belum", "habis": False,
        "percobaan_gagal": 0, "macet_di": None, "error_terakhir": "",
        "duplikat_dari": "", "diperbarui": "",
    }

    def __init__(self, path: str | None = None):
        self.path = path or cfg.FILE_STATUS
        self.data: dict[str, dict] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f).get("tempat", {})
            except (OSError, ValueError) as e:
                log.warning(f"'{os.path.basename(self.path)}' tidak terbaca ({e}); mulai dari kosong")

    def ambil(self, nama_input: str) -> dict:
        kunci = norm(nama_input)
        if kunci not in self.data:
            baru = dict(self.DEFAULT)
            baru["nama_input"] = rapikan_spasi(nama_input)
            self.data[kunci] = baru
        return self.data[kunci]

    def ada(self, nama_input: str) -> bool:
        return norm(nama_input) in self.data

    def simpan(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        for v in self.data.values():
            v.setdefault("diperbarui", "")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"versi": 1, "tempat": self.data}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def catat(self, nama_input: str, **perubahan) -> dict:
        info = self.ambil(nama_input)
        info.update(perubahan)
        info["diperbarui"] = _sekarang()
        self.simpan()
        return info


# ─── Daftar input ─────────────────────────────────────────────────────────────

def baca_daftar_input(path: str | None = None) -> list[str]:
    """
    Membaca nama-nama tempat dari CSV input. Mencoba beberapa encoding (UTF-8, Windows-1252),
    membuang spasi berlebih & nama kosong, serta nama ganda (tanpa membedakan huruf besar/kecil).
    """
    path = path or cfg.INPUT_CSV
    with open(path, "rb") as f:
        teks = _decode(f.read())
    rows = _parse(teks, ",")
    if not rows:
        return []
    header = [c.strip().strip('"').strip().lstrip("﻿") for c in rows[0]]
    if "nama_tempat" not in header:
        raise ValueError(f"Kolom 'nama_tempat' tidak ditemukan di '{path}' (kolom: {header})")
    ix = header.index("nama_tempat")
    nama, terlihat = [], set()
    for r in rows[1:]:
        if ix >= len(r):
            continue
        n = rapikan_spasi(r[ix])
        if n and norm(n) not in terlihat:
            terlihat.add(norm(n))
            nama.append(n)
    return nama


def pangkas_daftar_input(nama_dihapus: set[str], path: str | None = None) -> str:
    """
    Menghapus nama-nama tertentu dari CSV input. Salinan asli disimpan SEKALI di
    '<nama>_sebelum_dipangkas.csv'. File baru ditulis UTF-8.
    Returns path cadangan.
    """
    path = path or cfg.INPUT_CSV
    akar, ekst = os.path.splitext(path)
    cadangan = f"{akar}_sebelum_dipangkas{ekst}"
    if not os.path.exists(cadangan):
        shutil.copy2(path, cadangan)
    hapus = {norm(n) for n in nama_dihapus}
    sisa = [n for n in baca_daftar_input(path) if norm(n) not in hapus]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["nama_tempat"])
        for n in sisa:
            w.writerow([n])
    return cadangan


def catat_tutup(baris: list[dict], path: str | None = None):
    """Menambah cafe yang tutup ke 'daftar_coffeeshop_tutup.csv' (tanpa duplikat)."""
    path = path or cfg.FILE_TUTUP
    kolom = ["nama_input", "nama_resmi", "status", "url", "dicek_pada"]
    ada = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                ada.add(norm(r.get("nama_input", "")))
    baru = not os.path.exists(path)
    with open(path, "a", encoding="utf-8-sig" if baru else "utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if baru:
            w.writerow(kolom)
        for b in baris:
            if norm(b["nama_input"]) in ada:
                continue
            w.writerow([b["nama_input"], b.get("nama_resmi", ""), b["status"], b.get("url", ""), _sekarang()])


def catat_gagal(nama: str, alasan: str, path: str | None = None):
    """Satu baris ringkas per kegagalan (tanpa stacktrace panjang)."""
    path = path or cfg.LOG_GAGAL
    alasan = rapikan_spasi(alasan)[:300]
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{nama} | {alasan} | {_sekarang()}\n")
