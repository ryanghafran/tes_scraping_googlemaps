"""
=============================================================================
  SCRAPER ULASAN GOOGLE MAPS - COFFEE SHOP BANDUNG
  Untuk riset skripsi: Analisis Sentimen NLP
=============================================================================

  Cara pakai:
    1. pip install -r requirements.txt       (Google Chrome harus terpasang)
    2. Isi 'daftar_coffeeshop.csv' (kolom: nama_tempat)
    3. python scraper.py --login             (SEKALI SAJA: Google mewajibkan login untuk membuka ulasan lengkap)
    4. python scraper.py

  Satu kali jalan sudah cukup untuk 4000-5000 ulasan. Jika berhenti di tengah jalan
  (Ctrl+C, listrik mati, CAPTCHA, internet putus) cukup jalankan lagi: progres tersimpan
  dan duplikat tidak akan masuk.

  Opsi berguna (lengkap: python scraper.py --help):
    python scraper.py --login                 login Google di profil scraper (wajib sekali; pakai akun khusus)
    python scraper.py --diagnosa              periksa kenapa Chrome tidak bisa dibuka
    python scraper.py --cek-status            cek cafe mana yang tutup lalu berhenti
    python scraper.py --target 4000           ubah target total ulasan
    python scraper.py --batas-sesi 200 --istirahat 15   mode hati-hati (istirahat otomatis)
    python scraper.py --urutan terbaru terendah         ambil ulasan terbaru + terendah (data lebih seimbang)
    python scraper.py --ekspor                buat ulang hasil_ulasan.csv tanpa scraping
    python scraper.py --ringkasan             lihat status semua tempat

  Berkas keluaran:
    hasil_ulasan.csv                  -> hasil akhir (dibuat ulang otomatis; aman dibuka di Excel)
    data/ulasan_master.jsonl          -> data master (jangan diedit)
    data/status_tempat.json           -> status tiap tempat (untuk resume)
    daftar_coffeeshop_tutup.csv       -> cafe tutup permanen/sementara yang dibuang dari daftar
    log_gagal.txt, scraper_run.log    -> catatan masalah & log lengkap
    debug/                            -> screenshot + HTML bila ada masalah (kirim ke pembuat bila perlu)
=============================================================================
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

import konfigurasi as cfg
from gmaps import TabUlasanGagal, TempatTidakDitemukan, ambil_ulasan_tempat, resolve_tempat, status_login
from penyimpanan import (
    PenyimpananUlasan,
    StatusTempat,
    baca_daftar_input,
    catat_gagal,
    catat_tutup,
    norm,
    pangkas_daftar_input,
)
from peramban import (
    PESAN_LOGIN,
    ChromeTidakBisaDibukaError,
    DiblokirGoogleError,
    LoginDiperlukanError,
    Peramban,
    jalankan_diagnosa,
    login_manual,
    ringkas_error,
    sesi_mati,
)

log = logging.getLogger("scraper")


class BerhentiError(Exception):
    """Dihentikan dengan sengaja (mis. terlalu banyak kegagalan beruntun)."""


@dataclass
class Sesi:
    baru: int = 0            # ulasan baru yang dikumpulkan sejak istirahat terakhir


# ─── Logging & tampilan ───────────────────────────────────────────────────────

def siapkan_logging():
    root = logging.getLogger()
    if root.handlers:
        return
    for aliran in (sys.stdout, sys.stderr):   # konsol Windows (cp1252) tidak boleh crash karena karakter khusus
        try:
            aliran.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.FileHandler(cfg.LOG_RUN, "a", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    for nama in ("urllib3", "selenium", "WDM"):
        logging.getLogger(nama).setLevel(logging.WARNING)


def garis(judul: str = "", ch: str = "-"):
    print("\n" + ch * 66)
    if judul:
        print("  " + judul)
        print(ch * 66)


# ─── Pemulihan ────────────────────────────────────────────────────────────────

def dengan_pemulihan(pb: Peramban, fungsi, *args, nama: str = ""):
    """
    Menjalankan fungsi scraping; jika browser mati -> buka Chrome baru lalu ulangi (maks
    MAKS_PERCOBAAN_TEMPAT kali). Error lain juga diulang beberapa kali dengan jeda.
    """
    terakhir = None
    for percobaan in range(1, cfg.MAKS_PERCOBAAN_TEMPAT + 1):
        try:
            return fungsi(*args)
        except (DiblokirGoogleError, LoginDiperlukanError, TempatTidakDitemukan, KeyboardInterrupt):
            raise
        except Exception as e:
            terakhir = e
            if sesi_mati(e):
                log.warning(f"  Browser tertutup/crash ({ringkas_error(e, 100)}). "
                            f"Membuka Chrome baru (percobaan {percobaan}/{cfg.MAKS_PERCOBAAN_TEMPAT})...")
                pb.ulang()
            else:
                log.warning(f"  Gagal: {ringkas_error(e)} (percobaan {percobaan}/{cfg.MAKS_PERCOBAAN_TEMPAT})")
                pb.simpan_debug(f"error_{nama}")
                if percobaan < cfg.MAKS_PERCOBAAN_TEMPAT:
                    pb.tidur(3, 6)
    raise terakhir


# ─── Tahap 1: cari tiap tempat, baca status, pangkas yang tutup ───────────────

def _cari_duplikat(status: StatusTempat, daftar: list[str], nama: str, info: dict) -> str:
    """Nama lain di daftar yang sudah resolve ke tempat yang sama (id/URL sama)."""
    for lain in daftar:
        if lain == nama:
            continue
        s = status.ambil(lain)
        if s["url"] and s["status_scrape"] != "duplikat" and (
                (info["id_tempat"] and s["id_tempat"] == info["id_tempat"]) or s["url"] == info["url"]):
            return lain
    return ""


def tahap_resolve(pb: Peramban, daftar: list[str], status: StatusTempat):
    belum = [n for n in daftar if not status.ambil(n)["url"]]
    if not belum:
        return
    garis(f"TAHAP 1: mengecek {len(belum)} tempat di Google Maps (status buka/tutup, jumlah ulasan)", "=")
    gagal_beruntun = 0

    def rem_darurat():
        if gagal_beruntun >= cfg.MAKS_GAGAL_BERUNTUN:
            raise BerhentiError(
                f"{gagal_beruntun} tempat berturut-turut tidak bisa dicek; kemungkinan internet terputus atau "
                "Google membatasi akses. Progres tersimpan. Periksa koneksi / tunggu 30-60 menit lalu jalankan ulang.")

    for i, nama in enumerate(belum, 1):
        print(f"[{i}/{len(belum)}] {nama}")
        log.info(f"[{i}/{len(belum)}] Cek tempat: {nama}")
        try:
            info = dengan_pemulihan(pb, resolve_tempat, pb, nama, nama=nama)
        except TempatTidakDitemukan as e:
            status.catat(nama, status_scrape="tidak_ditemukan", error_terakhir=str(e))
            catat_gagal(nama, str(e))
            log.warning(f"  Tidak ditemukan: {e}")
            gagal_beruntun += 1
            rem_darurat()
            continue
        except (DiblokirGoogleError, LoginDiperlukanError, KeyboardInterrupt):
            raise
        except Exception as e:
            status.catat(nama, error_terakhir=ringkas_error(e))
            catat_gagal(nama, ringkas_error(e))
            log.error(f"  Gagal mengecek '{nama}': {ringkas_error(e)}")
            gagal_beruntun += 1
            rem_darurat()
            continue
        gagal_beruntun = 0

        perubahan = dict(nama_resmi=info["nama_resmi"], alamat=info["alamat"], kategori=info["kategori"],
                         url=info["url"], id_tempat=info["id_tempat"], rating=info["rating"],
                         total_ulasan=info["total_ulasan"], status_buka=info["status_buka"],
                         error_terakhir="")
        dup = _cari_duplikat(status, daftar, nama, info)
        if dup:
            perubahan.update(status_scrape="duplikat", duplikat_dari=dup)
            log.warning(f"  '{nama}' ternyata tempat yang sama dengan '{dup}' -> dilewati")
        elif cfg.FILTER_KATEGORI and info["kategori"] and not any(
                k in info["kategori"].lower() for k in cfg.KATEGORI_VALID):
            perubahan.update(status_scrape="bukan_cafe")
            log.warning(f"  Kategori '{info['kategori']}' bukan cafe/coffee shop -> dilewati")
        status.catat(nama, **perubahan)
        if info["status_buka"] != "aktif":
            print(f"    -> {info['status_buka'].upper().replace('_', ' ')} (akan dibuang dari daftar)")
        if i < len(belum):
            pb.tidur(*cfg.JEDA_ANTAR_TEMPAT)


def pangkas_tutup(daftar: list[str], status: StatusTempat) -> list[str]:
    """Membuang cafe tutup permanen/sementara dari daftar input (cadangan dibuat otomatis)."""
    tutup = [n for n in daftar if status.ambil(n)["status_buka"] in ("tutup_permanen", "tutup_sementara")]
    if not tutup:
        return daftar
    catat_tutup([{"nama_input": n, "nama_resmi": status.ambil(n)["nama_resmi"],
                  "status": status.ambil(n)["status_buka"], "url": status.ambil(n)["url"]} for n in tutup])
    try:
        cadangan = pangkas_daftar_input(set(tutup))
        log.info(f"{len(tutup)} cafe tutup dibuang dari '{cfg.INPUT_CSV}' (cadangan: {cadangan})")
    except OSError as e:
        log.warning(f"Daftar input tidak bisa ditulis ({e}); cafe tutup tetap dilewati lewat data status")
    print(f"\nCafe TUTUP yang dibuang dari daftar ({len(tutup)}):")
    for n in tutup:
        print(f"  - {n}  [{status.ambil(n)['status_buka'].replace('_', ' ')}]  -> daftar_coffeeshop_tutup.csv")
    return [n for n in daftar if n not in tutup]


# ─── Tahap 2: ambil ulasan per ronde ──────────────────────────────────────────

def layak_scrape(s: dict) -> bool:
    return bool(
        s["url"] and s["status_buka"] == "aktif"
        and s["status_scrape"] not in ("duplikat", "tidak_ditemukan", "bukan_cafe")
        and not s["habis"] and s["percobaan_gagal"] < cfg.MAKS_GAGAL_TOTAL
    )


def hitung_total(daftar: list[str], status: StatusTempat, store: PenyimpananUlasan, terbatas: bool) -> int:
    """
    Jumlah ulasan yang dihitung ke target. Normalnya seluruh isi data master; dengan --hanya (terbatas)
    hanya ulasan milik tempat yang dipilih, supaya uji cepat seperti '--hanya "Two Cents" --target 20' berfungsi.
    """
    if not terbatas:
        return store.total()
    return sum(store.jumlah_tempat(status.ambil(n)["nama_resmi"]) for n in daftar if status.ambil(n)["nama_resmi"])


def rencana_batas(hitung: dict, kurang: int, maks: int) -> dict:
    """
    Membagi sisa target dengan cara 'mengisi sampai level yang sama' (water-filling): naikkan level L
    sampai jumlah tambahan seluruh tempat >= kurang. Tempat yang sudah punya lebih banyak ulasan
    (hasil impor / ronde sebelumnya) mendapat lebih sedikit, tempat yang tertinggal (mis. gagal di
    ronde sebelumnya) dikejar lebih dulu, sehingga total per tempat tetap seimbang.

    Args:
        hitung: {nama: jumlah ulasan tersimpan sekarang} untuk tempat yang masih bisa bertambah
        kurang: jumlah ulasan yang masih dibutuhkan
        maks:   batas keras per tempat
    Returns: {nama: batas jumlah ulasan baru untuk ronde ini} (tidak pernah di bawah jumlah sekarang)
    """
    if not hitung:
        return {}
    level = min(hitung.values())
    while level < maks and sum(max(0, min(level, maks) - c) for c in hitung.values()) < kurang:
        level += 1
    level = min(level, maks)
    return {n: max(c, level) for n, c in hitung.items()}


def istirahat(pb: Peramban, sesi: Sesi):
    menit = cfg.ISTIRAHAT_MENIT
    garis(f"MODE HATI-HATI: {sesi.baru} ulasan terkumpul -> istirahat {menit} menit (browser ditutup)")
    log.info(f"Istirahat {menit} menit setelah {sesi.baru} ulasan")
    pb.tutup()
    for sisa in range(menit, 0, -1):
        print(f"  ... {sisa} menit lagi")
        time.sleep(60)
    pb.faktor_lambat = 1.0
    pb.mulai()
    sesi.baru = 0


def tahap_ulasan(pb: Peramban, daftar: list[str], status: StatusTempat, store: PenyimpananUlasan, sesi: Sesi,
                 terbatas: bool = False):
    target = cfg.TARGET_TOTAL_ULASAN
    gagal_beruntun = 0

    def total() -> int:
        return hitung_total(daftar, status, store, terbatas)

    urutan_nama = [status.ambil(n)["nama_resmi"] for n in daftar]

    for ronde in range(1, cfg.MAKS_RONDE + 1):
        kurang = target - total()
        if kurang <= 0:
            break
        aktif = [n for n in daftar if layak_scrape(status.ambil(n))
                 and store.jumlah_tempat(status.ambil(n)["nama_resmi"]) < cfg.MAKS_ULASAN_PER_TEMPAT]
        if not aktif:
            log.info("Tidak ada tempat yang masih bisa menambah ulasan.")
            break
        hitung = {n: store.jumlah_tempat(status.ambil(n)["nama_resmi"]) for n in aktif}
        batas_ronde = rencana_batas(hitung, kurang, cfg.MAKS_ULASAN_PER_TEMPAT)
        # Satu kunjungan memakan 30-60 detik: tempat yang hanya perlu beberapa ulasan baru dilewati.
        bermakna = {n for n in aktif if batas_ronde[n] - hitung[n] >= cfg.MIN_TAMBAH_PER_KUNJUNGAN}
        if not bermakna:
            log.info(f"Sisa target ({kurang}) tersebar terlalu tipis (kurang dari {cfg.MIN_TAMBAH_PER_KUNJUNGAN} ulasan "
                     "per tempat); berhenti.")
            break
        batas_ronde = {n: (b if n in bermakna else hitung[n]) for n, b in batas_ronde.items()}
        level = max(batas_ronde.values())
        garis(f"RONDE {ronde}: butuh {kurang} ulasan lagi; tiap tempat diisi sampai ~{level} ulasan "
              f"({sum(1 for n in aktif if batas_ronde[n] > hitung[n])} dari {len(aktif)} tempat ikut)", "=")
        tambah_ronde = 0

        for idx, nama in enumerate(aktif, 1):
            if total() >= target:
                break
            info = status.ambil(nama)
            sudah = store.jumlah_tempat(info["nama_resmi"])
            perlu = min(batas_ronde[nama] - sudah, target - total())
            if perlu <= 0:
                continue
            print(f"\n[{idx}/{len(aktif)}] {info['nama_resmi']}  (sudah {sudah}, ambil hingga +{perlu})")
            log.info(f"[{idx}/{len(aktif)}] {info['nama_resmi']}: sudah {sudah}, kuota +{perlu}")

            try:
                hasil = dengan_pemulihan(pb, ambil_ulasan_tempat, pb, store, info, perlu, cfg.URUTAN_ULASAN, nama=nama)
                if hasil.alasan == "kosong":
                    if info["total_ulasan"] == 0:
                        hasil.habis = True              # memang tidak punya ulasan
                    else:
                        pb.simpan_debug(f"kosong_{nama}")
                        raise TabUlasanGagal("daftar ulasan kosong (0 kartu termuat)")
            except (DiblokirGoogleError, LoginDiperlukanError, KeyboardInterrupt):
                raise
            except Exception as e:
                gagal_beruntun += 1
                status.catat(nama, status_scrape="gagal", percobaan_gagal=info["percobaan_gagal"] + 1,
                             error_terakhir=ringkas_error(e))
                catat_gagal(nama, ringkas_error(e))
                log.error(f"  GAGAL: {ringkas_error(e)}")
                if gagal_beruntun >= cfg.MAKS_GAGAL_BERUNTUN:
                    raise BerhentiError(
                        f"{gagal_beruntun} tempat gagal berturut-turut; kemungkinan Google membatasi akses. "
                        "Progres tersimpan. Tunggu 30-60 menit (atau ganti jaringan) lalu jalankan ulang.")
                continue

            # ── hasil tempat ini
            habis = hasil.habis
            macet_di = None
            if hasil.alasan == "macet":
                macet_di = hasil.terlihat
                sebelumnya = info.get("macet_di")
                if sebelumnya is not None and abs(sebelumnya - hasil.terlihat) <= 5:
                    habis = True                        # macet di titik yang sama dua kali -> anggap ujung daftar
                    log.info(f"  Macet lagi di sekitar {hasil.terlihat} kartu: dianggap sudah habis")
            status.catat(
                nama, habis=habis, macet_di=macet_di, error_terakhir="",
                status_scrape="selesai" if (habis or hasil.alasan == "kuota") else "sebagian",
                percobaan_gagal=0 if (hasil.baru > 0 or habis) else info["percobaan_gagal"])
            store.ekspor_csv(urutan_tempat=urutan_nama)
            tambah_ronde += hasil.baru
            sesi.baru += hasil.baru
            if hasil.alasan == "kuota" and pb.faktor_lambat > 1.0:     # pelambatan hanya sementara: pulih bertahap
                pb.faktor_lambat = max(1.0, pb.faktor_lambat * 0.75)
            gagal_beruntun = gagal_beruntun + 1 if (hasil.baru == 0 and not habis) else 0
            if gagal_beruntun >= cfg.MAKS_GAGAL_BERUNTUN:
                raise BerhentiError(
                    f"{gagal_beruntun} tempat berturut-turut tanpa ulasan baru; kemungkinan Google membatasi akses. "
                    "Progres tersimpan. Tunggu 30-60 menit (atau ganti jaringan) lalu jalankan ulang.")
            print(f"    +{hasil.baru} ulasan ({hasil.alasan}) | total tempat ini: "
                  f"{store.jumlah_tempat(info['nama_resmi'])} | TOTAL: {total()} / {target}")
            log.info(f"  Selesai: +{hasil.baru} ({hasil.alasan}); total {total()}/{target}")
            if hasil.peringatan:
                print(f"    PERINGATAN KUALITAS DATA: {hasil.peringatan}")
                print("    (kemungkinan tampilan Google Maps berubah; periksa folder debug/ lalu kirim ke pembuat scraper)")

            if cfg.BATAS_ULASAN_PER_SESI and sesi.baru >= cfg.BATAS_ULASAN_PER_SESI and total() < target:
                istirahat(pb, sesi)
            elif idx < len(aktif) and total() < target:
                pb.tidur(*cfg.JEDA_ANTAR_TEMPAT)

        if tambah_ronde == 0:
            log.info("Ronde ini tidak menambah ulasan sama sekali; berhenti.")
            break


# ─── Ringkasan ────────────────────────────────────────────────────────────────

def ringkasan_teks(daftar: list[str], status: StatusTempat, store: PenyimpananUlasan, terbatas: bool = False) -> str:
    baris = ["", "=" * 78, "  RINGKASAN", "=" * 78,
             f"  {'Tempat':<38}{'Status':<17}{'Tersimpan':>9}{'Di Maps':>9}"]
    for n in daftar:
        s = status.ambil(n)
        nama = (s["nama_resmi"] or n)[:36]
        st = s["status_buka"] if s["status_buka"] not in ("", "aktif") else (
            "habis" if s["habis"] else s["status_scrape"])
        jml = store.jumlah_tempat(s["nama_resmi"]) if s["nama_resmi"] else 0
        di_maps = f"{s['total_ulasan']:,}".replace(",", ".") if s["total_ulasan"] else "-"
        baris.append(f"  {nama:<38}{st.replace('_', ' '):<17}{jml:>9}{di_maps:>9}")
    if terbatas:       # --hanya: target dihitung untuk tempat terpilih saja
        baris += ["-" * 78, f"  ULASAN TEMPAT TERPILIH: {hitung_total(daftar, status, store, True)} / target "
                            f"{cfg.TARGET_TOTAL_ULASAN}   (total seluruh data: {store.total()})"]
    else:
        baris += ["-" * 78, f"  TOTAL ULASAN UNIK: {store.total()} / target {cfg.TARGET_TOTAL_ULASAN}"]
    tutup = [n for n in status.data.values() if n["status_buka"] in ("tutup_permanen", "tutup_sementara")]
    if tutup:
        baris.append(f"  Cafe tutup (dibuang dari daftar): {len(tutup)}  -> {cfg.FILE_TUTUP}")
    baris.append("=" * 78)
    return "\n".join(baris)


# ─── Login Google ─────────────────────────────────────────────────────────────

def cek_login_awal(pb: Peramban):
    """
    Peringatan dini (tidak menghentikan program): apakah Chrome scraper sudah login ke Google?
    Tanpa login Google hanya menampilkan ±3-5 ulasan preview per tempat.
    """
    def baca():
        pb.buka(f"{cfg.MAPS_BASE}?hl={cfg.BAHASA}")
        pb.tidur(2.0, 3.0)
        return status_login(pb)

    try:
        st = dengan_pemulihan(pb, baca, nama="cek_login")
    except (DiblokirGoogleError, LoginDiperlukanError, KeyboardInterrupt):
        raise
    except Exception as e:
        log.info(f"Status login tidak bisa diperiksa ({ringkas_error(e, 100)}); dilanjutkan.")
        return
    if st == "masuk":
        log.info("Login Google terdeteksi pada profil scraper.")
    elif st == "keluar":
        print("\n" + "!" * 66)
        print("  PERINGATAN: Chrome scraper BELUM login ke Google.")
        print("  Tanpa login, Google biasanya hanya menampilkan ±3-5 ulasan per tempat")
        print("  dan meminta login saat 'Ulasan lainnya' dibuka.")
        print("  Disarankan berhenti sekarang lalu jalankan:  python scraper.py --login")
        print("!" * 66 + "\n")
        log.warning("Chrome scraper belum login ke Google; ulasan lengkap kemungkinan tidak bisa dibuka")
        if sys.stdin is not None and sys.stdin.isatty():           # terminal interaktif: jangan buang waktu diam-diam
            jawab = input("  Tetap lanjut tanpa login? [y/N] ").strip().lower()
            if jawab not in ("y", "ya"):
                raise LoginDiperlukanError(PESAN_LOGIN)
    else:
        log.info("Status login Google tidak bisa dipastikan; dilanjutkan (dinding login akan dideteksi saat ulasan dibuka).")


def verifikasi_login() -> int:
    """Membuka Chrome scraper sebentar untuk memeriksa apakah login tersimpan. Returns kode keluar."""
    pb = Peramban()
    try:
        pb.mulai()
        pb.buka(f"{cfg.MAPS_BASE}?hl={cfg.BAHASA}")
        pb.tidur(2.5, 3.5)
        st = status_login(pb)
    except ChromeTidakBisaDibukaError as e:
        print("\n" + str(e))
        return 1
    except Exception as e:
        print(f"\nTidak bisa memeriksa status login: {ringkas_error(e)}")
        return 1
    finally:
        pb.tutup()
    if st == "masuk":
        print("\nLogin Google terdeteksi dan tersimpan di profil scraper. Sekarang jalankan:  python scraper.py")
        return 0
    if st == "keluar":
        print("\nLogin BELUM terdeteksi di profil scraper. Ulangi  python scraper.py --login  dan pastikan Anda "
              "benar-benar login (avatar akun tampil di pojok kanan atas Google Maps) sebelum menutup Chrome.\n"
              "Jika Anda yakin sudah login (deteksi otomatis ini hanya perkiraan), uji langsung:  "
              "python scraper.py --hanya \"Two Cents\" --target 30")
        return 1
    print("\nStatus login tidak bisa dipastikan otomatis. Coba uji langsung:  "
          "python scraper.py --hanya \"Two Cents\" --target 30")
    return 0


def jalankan_login() -> int:
    if not login_manual():
        return 1
    print("\nMemeriksa status login (Chrome akan terbuka sebentar)...")
    return verifikasi_login()


# ─── Program utama ────────────────────────────────────────────────────────────

def buat_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Scraper ulasan Google Maps untuk coffee shop (skripsi analisis sentimen).")
    p.add_argument("--target", type=int, help=f"target total ulasan (default {cfg.TARGET_TOTAL_ULASAN})")
    p.add_argument("--maks-per-tempat", type=int, help=f"batas ulasan per tempat (default {cfg.MAKS_ULASAN_PER_TEMPAT})")
    p.add_argument("--urutan", nargs="+", choices=["relevan", "terbaru", "tertinggi", "terendah"],
                   help="urutan ulasan yang diambil, mis. --urutan terbaru terendah")
    p.add_argument("--headless", action="store_true", help="jalankan Chrome tanpa jendela (CAPTCHA tidak bisa diselesaikan manual)")
    p.add_argument("--login", action="store_true",
                   help="login ke Google di profil scraper (WAJIB sekali: tanpa login Google tidak membuka ulasan lengkap)")
    p.add_argument("--diagnosa", action="store_true",
                   help="periksa kenapa Chrome tidak bisa dibuka (profil terkunci, versi tidak cocok, dll.)")
    p.add_argument("--cek-status", action="store_true", help="hanya cek status buka/tutup tiap tempat lalu berhenti")
    p.add_argument("--ekspor", action="store_true", help="hanya buat ulang hasil_ulasan.csv dari data master")
    p.add_argument("--ringkasan", action="store_true", help="tampilkan ringkasan status lalu berhenti")
    p.add_argument("--impor", nargs="+", metavar="CSV", help="masukkan ulasan dari file CSV lain ke data master")
    p.add_argument("--batas-sesi", type=int, help="istirahat otomatis setelah N ulasan baru (0 = mati)")
    p.add_argument("--istirahat", type=int, help="lama istirahat dalam menit (default %d)" % cfg.ISTIRAHAT_MENIT)
    p.add_argument("--reset-gagal", action="store_true", help="coba lagi tempat yang sebelumnya gagal/diabaikan")
    p.add_argument("--hanya", nargs="+", metavar="NAMA", help="hanya proses tempat yang namanya memuat kata ini")
    return p


def terapkan_opsi(o: argparse.Namespace):
    if o.target:
        cfg.TARGET_TOTAL_ULASAN = o.target
    if o.maks_per_tempat:
        cfg.MAKS_ULASAN_PER_TEMPAT = o.maks_per_tempat
    if o.urutan:
        cfg.URUTAN_ULASAN = o.urutan
    if o.batas_sesi is not None:
        cfg.BATAS_ULASAN_PER_SESI = o.batas_sesi
    if o.istirahat:
        cfg.ISTIRAHAT_MENIT = o.istirahat


def jalankan(o: argparse.Namespace) -> int:
    siapkan_logging()
    terapkan_opsi(o)
    if o.diagnosa:
        return jalankan_diagnosa()
    if o.login:
        return jalankan_login()
    garis("GOOGLE MAPS REVIEW SCRAPER - Coffee Shop Bandung", "=")

    try:
        daftar = baca_daftar_input()
    except (OSError, ValueError) as e:
        log.error(f"Tidak bisa membaca daftar input: {e}")
        return 1
    terbatas = bool(o.hanya)
    if o.hanya:
        kata = [norm(k) for k in o.hanya]
        daftar = [n for n in daftar if any(k in norm(n) for k in kata)]
    log.info(f"{len(daftar)} tempat di daftar input")

    store = PenyimpananUlasan()
    store.impor_csv_lama_sekali()
    for berkas in o.impor or []:
        dibaca, baru, rusak = store.impor_csv(berkas)
        log.info(f"Impor '{berkas}': {dibaca} baris, {baru} baru, {rusak} rusak dilewati")
    status = StatusTempat()
    if o.reset_gagal:
        for n in daftar:
            s = status.ambil(n)
            if s["status_scrape"] in ("gagal", "tidak_ditemukan") or s["percobaan_gagal"]:
                status.catat(n, status_scrape="belum", percobaan_gagal=0, error_terakhir="", macet_di=None)

    def selesai(kode: int = 0) -> int:
        store.tutup()
        urutan = [status.ambil(n)["nama_resmi"] for n in daftar]
        path = store.ekspor_csv(urutan_tempat=urutan)
        print(ringkasan_teks(daftar, status, store, terbatas))
        print(f"  Hasil CSV : {path}")
        print(f"  Log       : {cfg.LOG_RUN}\n")
        return kode

    if o.ekspor or o.ringkasan:
        return selesai(0)

    daftar = pangkas_tutup(daftar, status)          # cafe yang sudah diketahui tutup dibuang dulu (tanpa browser)

    def perlu_ulasan() -> bool:
        return hitung_total(daftar, status, store, terbatas) < cfg.TARGET_TOTAL_ULASAN and any(
            layak_scrape(status.ambil(n))
            and store.jumlah_tempat(status.ambil(n)["nama_resmi"]) < cfg.MAKS_ULASAN_PER_TEMPAT for n in daftar)

    butuh_resolve = any(not status.ambil(n)["url"] for n in daftar)
    if not butuh_resolve and (o.cek_status or not perlu_ulasan()):
        log.info("Tidak ada yang perlu dikerjakan: semua tempat sudah dicek dan "
                 "target tercapai / tidak ada tempat yang bisa menambah ulasan.")
        return selesai(0)

    pb = Peramban(headless=True if o.headless else None)
    sesi = Sesi()
    kode = 0
    try:
        pb.mulai()
        if not o.cek_status and hitung_total(daftar, status, store, terbatas) < cfg.TARGET_TOTAL_ULASAN:
            cek_login_awal(pb)                       # peringatan dini: jangan menunggu Tahap 1 selesai baru tahu belum login
        if butuh_resolve:
            tahap_resolve(pb, daftar, status)
            daftar = pangkas_tutup(daftar, status)
        aktif = [n for n in daftar if layak_scrape(status.ambil(n))]
        kapasitas = sum(status.ambil(n)["total_ulasan"] or 0 for n in aktif)
        kapasitas_txt = f"{kapasitas:,}".replace(",", ".")
        print(f"\nTempat aktif: {len(aktif)} | total ulasan di Google Maps "
              f"(semua, termasuk tanpa teks): {kapasitas_txt}")
        if o.cek_status:
            return selesai(0)
        tahap_ulasan(pb, daftar, status, store, sesi, terbatas)
        if hitung_total(daftar, status, store, terbatas) >= cfg.TARGET_TOTAL_ULASAN:
            log.info("TARGET TERCAPAI.")
        else:
            log.info("Pengumpulan selesai tetapi target belum tercapai (semua tempat sudah diambil/habis atau macet). "
                     "Tambahkan tempat di daftar, naikkan --maks-per-tempat, atau coba --urutan terbaru terendah.")
    except KeyboardInterrupt:
        log.warning("Dihentikan pengguna (Ctrl+C). Progres tersimpan; jalankan lagi untuk melanjutkan.")
        kode = 130
    except LoginDiperlukanError as e:
        log.error(f"BERHENTI: {e}")
        print(f"\n[BERHENTI] {e}")
        kode = 3
    except ChromeTidakBisaDibukaError as e:
        log.error(f"BERHENTI: {str(e).splitlines()[0]}")
        print(f"\n[BERHENTI] {e}")
        kode = 2
    except (DiblokirGoogleError, BerhentiError) as e:
        log.error(f"BERHENTI: {e}")
        print(f"\n[BERHENTI] {e}")
        kode = 2
    except Exception as e:
        log.exception(f"Error tak terduga: {ringkas_error(e)}")
        kode = 1
    finally:
        pb.tutup()
    return selesai(kode)


def main(argv=None) -> int:
    return jalankan(buat_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
