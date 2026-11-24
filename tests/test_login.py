"""
Tes dukungan login Google (tanpa browser).

Latar belakang: tanpa login, Google Maps hanya menampilkan ±3-5 ulasan preview per tempat dan
memunculkan modal "Login untuk menikmati fitur terbaik dari Google Maps" saat 'Ulasan lainnya' dibuka.
Scraper harus (a) menyediakan cara login yang aman, (b) berhenti dengan instruksi jelas bila dinding
login muncul, tanpa menandai tempat sebagai gagal dan tanpa mengulang percobaan yang pasti sia-sia.
"""

import os

import pytest
from selenium.common.exceptions import WebDriverException

import konfigurasi as cfg
import peramban
import scraper
from peramban import LoginDiperlukanError, Peramban, cari_chrome, login_manual
from penyimpanan import PenyimpananUlasan
from test_orkestrator import PbPalsu, siapkan_status


# ─── Mencari Chrome ───────────────────────────────────────────────────────────

def test_kandidat_chrome_memprioritaskan_CHROME_BINARY(tmp_path, monkeypatch):
    palsu = tmp_path / "chrome-khusus"
    palsu.write_text("x")
    monkeypatch.setattr(cfg, "CHROME_BINARY", str(palsu))
    assert peramban._kandidat_chrome()[0] == str(palsu)
    assert cari_chrome() == str(palsu)


def test_cari_chrome_melewati_kandidat_yang_tidak_ada(tmp_path, monkeypatch):
    ada = tmp_path / "google-chrome"
    ada.write_text("x")
    monkeypatch.setattr(peramban, "_kandidat_chrome", lambda: ["/tidak/ada/chrome.exe", str(ada)])
    assert cari_chrome() == str(ada)
    monkeypatch.setattr(peramban, "_kandidat_chrome", lambda: ["/tidak/ada/chrome.exe"])
    assert cari_chrome() is None


# ─── Alur --login ─────────────────────────────────────────────────────────────

class ProsesPalsu:
    """Pengganti subprocess.Popen: mencatat argumen; poll() mengembalikan None beberapa kali lalu 0."""
    dibuat = []
    selesai_setelah = 3          # berapa kali poll() sebelum 'Chrome' dianggap sudah tertutup

    def __init__(self, args):
        self.args, self.n = args, 0
        ProsesPalsu.dibuat.append(self)

    def poll(self):
        self.n += 1
        return None if self.n < ProsesPalsu.selesai_setelah else 0

    def wait(self):
        return 0


@pytest.fixture
def login_siap(lingkungan, monkeypatch):
    ProsesPalsu.dibuat.clear()
    monkeypatch.setattr(peramban, "cari_chrome", lambda: "/palsu/chrome")
    monkeypatch.setattr(peramban.subprocess, "Popen", lambda args, **k: ProsesPalsu(args))
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(peramban.time, "sleep", lambda s: None)
    return ProsesPalsu


def test_login_manual_membuka_chrome_BIASA_dengan_profil_scraper(login_siap, capsys):
    assert login_manual() is True
    args = login_siap.dibuat[0].args
    assert args[0] == "/palsu/chrome"
    assert f"--user-data-dir={os.path.abspath(cfg.DIR_PROFIL)}" in args      # profil yang sama dengan yang dipakai Selenium
    assert any("accounts.google.com/ServiceLogin" in a for a in args)
    assert "--disable-background-mode" in args                               # agar Chrome benar-benar berhenti saat ditutup
    # Chrome BIASA: tanpa kendali otomatis, sebab Google sering menolak login di browser yang dikendalikan Selenium
    assert not any("remote-debugging" in a or "enable-automation" in a for a in args)
    keluar = capsys.readouterr().out
    assert "akun khusus" in keluar and ".chrome_profile" in keluar


def test_login_manual_memperingatkan_jika_chrome_belum_tertutup(login_siap, monkeypatch, capsys):
    monkeypatch.setattr(ProsesPalsu, "selesai_setelah", 10 ** 9)           # Chrome 'tidak pernah' tertutup
    assert login_manual() is True                                           # tidak menggantung selamanya
    assert "belum tertutup penuh" in capsys.readouterr().out


def test_login_manual_tanpa_chrome_memberi_petunjuk(lingkungan, monkeypatch, capsys):
    monkeypatch.setattr(peramban, "cari_chrome", lambda: None)
    assert login_manual() is False
    assert "CHROME_BINARY" in capsys.readouterr().out


def test_main_login_menjalankan_login_manual_lalu_verifikasi(lingkungan, monkeypatch):
    panggilan = []
    monkeypatch.setattr(scraper, "login_manual", lambda: panggilan.append("login") or True)
    monkeypatch.setattr(scraper, "verifikasi_login", lambda: panggilan.append("verifikasi") or 0)
    assert scraper.main(["--login"]) == 0
    assert panggilan == ["login", "verifikasi"]


def test_main_login_gagal_membuka_chrome_tidak_lanjut_verifikasi(lingkungan, monkeypatch):
    panggilan = []
    monkeypatch.setattr(scraper, "login_manual", lambda: False)
    monkeypatch.setattr(scraper, "verifikasi_login", lambda: panggilan.append("verifikasi") or 0)
    assert scraper.main(["--login"]) == 1 and panggilan == []


# ─── Profil Chrome: login tersimpan di profil, jangan diam-diam diganti ───────

def test_profil_terkunci_diulang_dengan_profil_YANG_SAMA_lalu_error_jelas(lingkungan, monkeypatch):
    """Profil sementara berarti kehilangan login -> sengaja tidak dipakai."""
    dipakai = []

    def chrome_palsu(service=None, options=None):
        dipakai.append(next(a for a in options.arguments if a.startswith("--user-data-dir")))
        raise WebDriverException("session not created: user data directory is already in use")

    monkeypatch.setattr(peramban.webdriver, "Chrome", chrome_palsu)
    monkeypatch.setattr(Peramban, "_service", lambda self: None)
    monkeypatch.setattr(peramban.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="sedang dipakai Chrome lain"):
        Peramban(headless=True).mulai()
    assert len(dipakai) == 3 and len(set(dipakai)) == 1


def test_error_chrome_lain_tidak_diulang_sebagai_profil_terkunci(lingkungan, monkeypatch):
    percobaan = []

    def chrome_palsu(service=None, options=None):
        percobaan.append(1)
        raise WebDriverException("session not created: This version of ChromeDriver only supports Chrome version 147")

    monkeypatch.setattr(peramban.webdriver, "Chrome", chrome_palsu)
    monkeypatch.setattr(Peramban, "_service", lambda self: None)
    with pytest.raises(WebDriverException, match="only supports Chrome version"):
        Peramban(headless=True).mulai()
    assert len(percobaan) == 1


# ─── Dinding login menghentikan run, bukan dianggap kegagalan tempat ──────────

def test_dinding_login_menghentikan_tahap_ulasan_tanpa_menandai_gagal_atau_mengulang(lingkungan, monkeypatch):
    status, store = siapkan_status(["A", "B"]), PenyimpananUlasan()
    dipanggil = []

    def dinding(*a, **k):
        dipanggil.append(1)
        raise LoginDiperlukanError(peramban.PESAN_LOGIN)

    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", dinding)
    monkeypatch.setattr(cfg, "MAKS_PERCOBAAN_TEMPAT", 3)
    with pytest.raises(LoginDiperlukanError, match="--login"):
        scraper.tahap_ulasan(PbPalsu(), ["A", "B"], status, store, scraper.Sesi())
    assert len(dipanggil) == 1                                              # tidak diulang 3x, tidak lanjut ke tempat B
    assert status.ambil("A")["percobaan_gagal"] == 0 and status.ambil("A")["status_scrape"] == "belum"
    assert not os.path.exists(cfg.LOG_GAGAL)


def test_pesan_login_menyebut_perintah_yang_harus_dijalankan():
    assert "python scraper.py --login" in peramban.PESAN_LOGIN


# ─── Peringatan dini di awal (sebelum Tahap 1) ────────────────────────────────

class PbLogin(PbPalsu):
    def buka(self, url):
        pass


class StdinPalsu:
    def __init__(self, interaktif):
        self.interaktif = interaktif

    def isatty(self):
        return self.interaktif


def _atur_cek_login(monkeypatch, status, interaktif, jawaban="n"):
    pertanyaan = []
    monkeypatch.setattr(scraper, "status_login", lambda pb: status)
    monkeypatch.setattr(scraper.sys, "stdin", StdinPalsu(interaktif))
    monkeypatch.setattr("builtins.input", lambda *a: pertanyaan.append(a) or jawaban)
    return pertanyaan


def test_belum_login_di_terminal_interaktif_ditanya_dan_default_berhenti(lingkungan, monkeypatch, capsys):
    pertanyaan = _atur_cek_login(monkeypatch, "keluar", interaktif=True, jawaban="")
    with pytest.raises(LoginDiperlukanError, match="--login"):
        scraper.cek_login_awal(PbLogin())
    assert len(pertanyaan) == 1 and "BELUM login" in capsys.readouterr().out


@pytest.mark.parametrize("jawaban", ["y", "Y", "ya"])
def test_belum_login_tetap_lanjut_jika_pengguna_menjawab_ya(lingkungan, monkeypatch, jawaban):
    _atur_cek_login(monkeypatch, "keluar", interaktif=True, jawaban=jawaban)
    scraper.cek_login_awal(PbLogin())                      # tidak melempar


def test_belum_login_tanpa_terminal_interaktif_hanya_peringatan(lingkungan, monkeypatch, capsys):
    pertanyaan = _atur_cek_login(monkeypatch, "keluar", interaktif=False)
    scraper.cek_login_awal(PbLogin())                      # mis. dijalankan terjadwal: jangan menggantung menunggu input
    assert pertanyaan == [] and "BELUM login" in capsys.readouterr().out


@pytest.mark.parametrize("status", ["masuk", "tidak_jelas"])
def test_login_terdeteksi_atau_tidak_jelas_tidak_bertanya(lingkungan, monkeypatch, capsys, status):
    pertanyaan = _atur_cek_login(monkeypatch, status, interaktif=True)
    scraper.cek_login_awal(PbLogin())
    assert pertanyaan == [] and "BELUM login" not in capsys.readouterr().out
