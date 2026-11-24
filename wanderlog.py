"""
wanderlog.py
============
Membuat / memperbarui `daftar_coffeeshop.csv` LANGSUNG dari daftar blog Wanderlog, lengkap dengan
identitas cabang yang persis (Google place ID, koordinat, alamat, kategori). Dengan begitu scraper
tidak lagi menebak cabang lewat pencarian nama.

Pemakaian:
    python wanderlog.py                       # ambil dari URL di konfigurasi, tulis daftar_coffeeshop.csv
    python wanderlog.py --url <url-daftar>    # daftar Wanderlog lain
    python wanderlog.py --dari-folder DIR     # baca halaman HTML yang sudah diunduh (tanpa internet)

Aturan kategori (sesuai ketentuan skripsi): HANYA cafe dan/atau coffee shop. Tempat yang kategorinya
restoran saja dibuang; restoran yang JUGA cafe/coffee shop tetap diambil. Yang dibuang dicatat di
`daftar_dibuang_wanderlog.csv` beserta alasannya.
"""

import argparse
import base64
import csv
import json
import os
import re
import struct
import sys
import time
import urllib.request

import konfigurasi as cfg

KOLOM_DAFTAR = ["nama_tempat", "alamat", "place_id", "fid", "lat", "lng", "kategori", "wanderlog_id"]

# Kata pada kategori Google yang dianggap "cafe / coffee shop".
KATA_CAFE = ("cafe", "café", "coffee", "kopi", "espresso", "kafe")


# ─── Identitas tempat ─────────────────────────────────────────────────────────

def fid_dari_place_id(place_id: str) -> str:
    """
    Google place ID 'ChIJ...' hanyalah penyandian base64 dari pasangan ID internal Maps. Hasilnya sama
    dengan potongan '0x2e68e7fc025a7c4f:0x51866d7aeed93924' di URL Google Maps, jadi cabang bisa
    dicocokkan PERSIS tanpa membandingkan alamat. String kosong jika bukan format ChIJ.
    """
    try:
        b = base64.urlsafe_b64decode(place_id + "=" * (-len(place_id) % 4))
        if len(b) < 20 or b[0] != 0x0A or b[2] != 0x09 or b[11] != 0x11:
            return ""
        a, c = struct.unpack("<QQ", b[3:11] + b[12:20])
        return f"0x{a:x}:0x{c:x}"
    except (ValueError, struct.error):
        return ""


def cid_dari_fid(fid: str) -> str:
    """Bagian kedua fid dalam desimal (dipakai URL 'maps?cid=...'). '' jika fid tidak valid."""
    m = re.fullmatch(r"0x[0-9a-f]+:0x([0-9a-f]+)", fid or "")
    return str(int(m.group(1), 16)) if m else ""


def kunci_alamat(alamat: str) -> tuple[str, str]:
    """
    Kunci perbandingan alamat jalan yang tahan perbedaan penulisan ('Jl. L. L. R.E. Martadinata No.65'
    sama dengan 'LLRE Martadinata St No.65'): (nama jalan tanpa spasi/titik, nomor). Hanya segmen pertama
    sebelum koma yang dipakai (kelurahan/kecamatan sering ditulis berbeda).
    """
    s = (alamat or "").lower().split(",")[0]
    m = re.search(r"\bno\.?\s*(\d+\w?)", s)
    nomor = m.group(1) if m else ""
    s = re.sub(r"\bno\.?\s*\d+\w?", "", s)
    s = re.sub(r"\b(jl|jalan|st|street)\b\.?", "", s)
    return re.sub(r"[^a-z0-9]", "", s), nomor


def kategori_cafe(kategori: list[str]) -> bool:
    """True jika salah satu kategori adalah cafe / coffee shop (restoran biasa saja -> False)."""
    return any(k in (x or "").lower() for x in kategori for k in KATA_CAFE)


# ─── Mengurai halaman Wanderlog ───────────────────────────────────────────────

def tautan_tempat(html_daftar: str) -> dict[str, str]:
    """{wanderlog_id: path halaman detail} dari halaman daftar, urutan sesuai halaman."""
    hasil: dict[str, str] = {}
    for path, wid in re.findall(r'href="(/place/details/(\d+)/[^"#?]*)"', html_daftar):
        hasil.setdefault(wid, path)
    return hasil


def baca_detail(html: str, wid: str) -> dict | None:
    """Mengambil nama, place ID, koordinat, alamat & kategori dari halaman detail satu tempat."""
    meta = re.search(r'"placeMetadata":\{"id":%s,"name":"((?:[^"\\]|\\.)*)","placePageType":"[^"]*",'
                     r'"placeId":"([^"]*)"' % re.escape(str(wid)), html)
    peta = re.search(r'"mapsPlace":\{"name":"(?:[^"\\]|\\.)*","latitude":(-?[\d.]+),"longitude":(-?[\d.]+)', html)
    kat = re.search(r'"categories":(\[[^\]]*\]),"hasDetails"', html)
    alamat = re.search(r'"hasDetails":(?:true|false),[^}]*?"address":"((?:[^"\\]|\\.)*)"', html)
    if not (meta and peta and alamat):
        return None
    try:
        nama = json.loads('"%s"' % meta.group(1))
        kategori = json.loads(kat.group(1)) if kat else []
        alamat_s = json.loads('"%s"' % alamat.group(1))
    except ValueError:
        return None
    place_id = meta.group(2)
    return {
        "nama_tempat": nama, "alamat": alamat_s, "place_id": place_id, "fid": fid_dari_place_id(place_id),
        "lat": float(peta.group(1)), "lng": float(peta.group(2)), "kategori": " | ".join(kategori),
        "wanderlog_id": str(wid),
    }


def _unduh(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", errors="replace")


# ─── Daftar CSV ───────────────────────────────────────────────────────────────

def tulis_daftar(path: str, baris: list[dict]):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=KOLOM_DAFTAR, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for b in baris:
            w.writerow(b)


def pilah(semua: list[dict]) -> tuple[list[dict], list[dict]]:
    """(diterima, dibuang+alasan) menurut aturan kategori cafe/coffee shop."""
    ok, buang = [], []
    for d in semua:
        kat = [k.strip() for k in d["kategori"].split("|")]
        if kategori_cafe(kat):
            ok.append(d)
        else:
            buang.append({**d, "alasan": f"kategori hanya restoran: {d['kategori'] or '-'}"})
    return ok, buang


def bangun_daftar(html_daftar: str, ambil_detail, jeda: float = 0.8) -> tuple[list[dict], list[dict]]:
    """ambil_detail(path, wid) -> html. Mengembalikan (diterima, dibuang)."""
    semua = []
    for wid, path in tautan_tempat(html_daftar).items():
        d = baca_detail(ambil_detail(path, wid), wid)
        if d is None:
            print(f"  ! halaman detail {wid} tidak terbaca (struktur Wanderlog berubah?)", file=sys.stderr)
            continue
        semua.append(d)
        if jeda:
            time.sleep(jeda)
    return pilah(semua)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Bangun daftar_coffeeshop.csv dari daftar Wanderlog")
    p.add_argument("--url", default=getattr(cfg, "URL_WANDERLOG", "https://wanderlog.com/list/geoCategory/16946"))
    p.add_argument("--dari-folder", help="Folder berisi list.html + <wanderlog_id>.html (tanpa internet)")
    p.add_argument("--keluar", default=cfg.INPUT_CSV)
    o = p.parse_args(argv)

    if o.dari_folder:
        html_daftar = open(os.path.join(o.dari_folder, "list.html"), encoding="utf-8").read()

        def ambil(path, wid):
            return open(os.path.join(o.dari_folder, f"{wid}.html"), encoding="utf-8").read()
        jeda = 0
    else:
        print(f"Mengunduh daftar: {o.url}")
        html_daftar = _unduh(o.url)

        def ambil(path, wid):
            return _unduh("https://wanderlog.com" + path)
        jeda = 0.8

    ok, buang = bangun_daftar(html_daftar, ambil, jeda)
    if not ok:
        print("Tidak ada tempat yang terbaca; daftar lama TIDAK diubah.", file=sys.stderr)
        return 1
    if os.path.exists(o.keluar):
        akar, ekst = os.path.splitext(o.keluar)
        cad = f"{akar}_sebelum_wanderlog{ekst}"
        if not os.path.exists(cad):
            os.replace(o.keluar, cad)
            print(f"Daftar lama disimpan ke: {cad}")
    tulis_daftar(o.keluar, ok)
    print(f"{len(ok)} tempat ditulis ke {o.keluar}")
    if buang:
        dibuang = os.path.join(os.path.dirname(o.keluar) or ".", "daftar_dibuang_wanderlog.csv")
        with open(dibuang, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=KOLOM_DAFTAR + ["alasan"], lineterminator="\n")
            w.writeheader()
            w.writerows(buang)
        print(f"{len(buang)} dibuang (restoran saja):")
        for b in buang:
            print(f"  - {b['nama_tempat']}  [{b['kategori']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
