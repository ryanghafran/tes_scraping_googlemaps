"""Tes fungsi murni (tanpa browser): deteksi status tutup, parsing, kemiripan nama, sesi mati, halangan Google."""

import pytest
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    WebDriverException,
)

import konfigurasi as cfg
from gmaps import (
    angka_dari_teks,
    bersihkan_tanggal,
    deteksi_status_buka,
    id_tempat_dari_url,
    skor_kemiripan,
    url_pencarian,
)
from peramban import deteksi_halangan, ringkas_error, sesi_mati


# ─── Status tutup (permintaan pengguna: buang temporarily & permanently closed) ─

@pytest.mark.parametrize("teks, harapan", [
    ("Two Hands Full Dago 4,6 (3.128) Kafe Tutup permanen Jl. Ir. H. Juanda", "tutup_permanen"),
    ("Kopi X 4,2 (88) Kafe Tutup sementara Jl. Y", "tutup_sementara"),
    ("Kopi X 4,2 (88) Coffee shop Permanently closed 12 Main St", "tutup_permanen"),
    ("Kopi X 4,2 (88) Coffee shop Temporarily closed 12 Main St", "tutup_sementara"),
    ("KOPI X TUTUP PERMANEN", "tutup_permanen"),
    # Tutup HANYA saat ini (jam operasional) bukan tutup permanen/sementara:
    ("Kopi X 4,6 (900) Kafe Tutup ⋅ Buka pukul 10.00 Jl. Y", "aktif"),
    ("Kopi X Closed ⋅ Opens 10 AM", "aktif"),
    ("", "aktif"),
])
def test_deteksi_status_buka(teks, harapan):
    assert deteksi_status_buka(teks) == harapan


# ─── Parsing & utilitas ───────────────────────────────────────────────────────

@pytest.mark.parametrize("teks, harapan", [
    ("6.179 ulasan", 6179), ("(7.863)", 7863), ("1,234 reviews", 1234), ("", None), (None, None), ("tanpa angka", None),
    # singkatan: jangan dibaca naif sebagai 12 (akibatnya scraper mengira daftar sudah habis)
    ("1,2 rb ulasan", 1200), ("1.2K reviews", 1200), ("2,5 jt", 2500000), ("12 reviews", 12), ("45 ulasan", 45),
])
def test_angka_dari_teks(teks, harapan):
    assert angka_dari_teks(teks) == harapan


def test_id_tempat_dan_tanggal_dan_url():
    url = "https://www.google.com/maps/place/X/@-6.9,107.6,17z/data=!4m6!3m5!1s0x2e68e6b5e8b3a1d1:0x4d6b7c5a9f8e3a21!8m2!3d-6.9"
    assert id_tempat_dari_url(url) == "0x2e68e6b5e8b3a1d1:0x4d6b7c5a9f8e3a21"
    assert id_tempat_dari_url("https://www.google.com/maps/search/x") == ""
    assert bersihkan_tanggal("Diedit 3 bulan lalu") == "3 bulan lalu"
    assert bersihkan_tanggal("Edited a year ago") == "a year ago"
    assert bersihkan_tanggal("sebulan lalu") == "sebulan lalu"


def test_url_pencarian_aman_untuk_apostrof_dan_ampersand(monkeypatch):
    monkeypatch.setattr(cfg, "MAPS_BASE", "https://www.google.com/maps")
    monkeypatch.setattr(cfg, "BAHASA", "id")
    u = url_pencarian("Noah's Barn & Co")
    assert u == "https://www.google.com/maps/search/Noah%27s+Barn+%26+Co+Bandung?hl=id"


def test_kemiripan_nama_memilih_hasil_yang_benar():
    q = "Kopi Ireng"
    assert skor_kemiripan(q, "Kopi Ireng") > skor_kemiripan(q, "Kopi Anjis")
    assert skor_kemiripan("Kilogram", "KILOGRAM SPACE") >= 0.9            # nama input lebih pendek tapi termuat utuh
    assert skor_kemiripan("Sejiwa Coffee Kiputih", "Sejiwa Coffee, Kiputih") >= 0.9
    assert skor_kemiripan("Two Cents", "Warung Kopi Purnama") < 0.5
    assert skor_kemiripan("", "x") == 0.0


# ─── Sesi browser mati ────────────────────────────────────────────────────────

def test_sesi_mati_dikenali_dari_tipe_dan_pesan():
    assert sesi_mati(InvalidSessionIdException("invalid session id"))
    assert sesi_mati(NoSuchWindowException("no such window"))
    assert sesi_mati(WebDriverException("Message: chrome not reachable"))
    assert sesi_mati(WebDriverException("disconnected: not connected to DevTools"))
    assert sesi_mati(ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host"))
    # error biasa BUKAN sesi mati (jangan membuka ulang Chrome untuk setiap kegagalan kecil)
    assert not sesi_mati(WebDriverException("Message: element not interactable"))
    assert not sesi_mati(ValueError("angka salah"))


def test_ringkas_error_membuang_stacktrace_chromedriver():
    pesan = ("Message: invalid session id; For documentation on this error, please visit: https://x\n"
             "Stacktrace:\nSymbols not available. Dumping unresolved backtrace:\n\t(No symbol) [0x7ff676c5d655]\n" * 3)
    r = ringkas_error(InvalidSessionIdException(pesan))
    assert "\n" not in r and "Stacktrace" not in r and "No symbol" not in r
    assert r.startswith("InvalidSessionIdException:") and "invalid session id" in r and len(r) < 220


# ─── Halangan Google ──────────────────────────────────────────────────────────

class DriverPalsu:
    def __init__(self, url, teks, ada_panel=False):
        self._info = {"url": url, "teks": teks, "ada_panel": ada_panel}

    def execute_script(self, *_a):
        return self._info


@pytest.mark.parametrize("url, teks, ada_panel, harapan", [
    ("https://www.google.com/sorry/index?continue=x", "", False, "captcha"),
    ("https://www.google.com/maps/search/x", "Lalu lintas yang tidak biasa dari jaringan komputer Anda", False, "captcha"),
    ("https://www.google.com/maps/search/x", "Our systems have detected unusual traffic", False, "captcha"),
    ("https://consent.google.com/m?continue=x", "", False, "consent"),
    ("https://www.google.com/maps", "Sebelum Anda melanjutkan ke Google", False, "consent"),
    # Teks di halaman Maps yang normal (panel ada) tidak boleh memicu alarm palsu:
    ("https://www.google.com/maps/place/x", "Ulasan: tempatnya bukan robot, lalu lintas yang tidak biasa ramai", True, None),
    ("https://www.google.com/maps/place/x", "Kopi A 4,6 Kafe", True, None),
])
def test_deteksi_halangan(url, teks, ada_panel, harapan):
    assert deteksi_halangan(DriverPalsu(url, teks, ada_panel)) == harapan
