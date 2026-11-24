"""
Tes dukungan login Google (tanpa browser).

Latar belakang: tanpa login, Google Maps hanya menampilkan ±3-5 ulasan preview per tempat dan
memunculkan modal "Login untuk menikmati fitur terbaik dari Google Maps" saat 'Ulasan lainnya' dibuka.
Scraper harus (a) menyediakan cara login yang aman, (b) berhenti dengan instruksi jelas bila dinding
login muncul, tanpa menandai tempat sebagai gagal dan tanpa mengulang percobaan yang pasti sia-sia.
"""

import os
import subprocess
import sys
import time

import pytest
from selenium.common.exceptions import SessionNotCreatedException, WebDriverException

import konfigurasi as cfg
import peramban
import scraper
from peramban import (
    ChromeTidakBisaDibukaError,
    LoginDiperlukanError,
    Peramban,
    cari_chrome,
    jalankan_diagnosa,
    login_manual,
    matikan_proses_profil,
    proses_chrome_profil,
    tanda_profil_terkunci,
)
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
    selesai_setelah = 3            # berapa kali poll() sebelum 'Chrome' dianggap sudah tertutup
    sisa = []                      # proses Chrome yang (menurut pemeriksaan profil) masih memakai profil scraper
    sisa_setelah_popen = []        # 'sisa' yang muncul tepat setelah Chrome login diluncurkan
    dimatikan = 0

    def __init__(self, args):
        self.args, self.n = args, 0
        ProsesPalsu.dibuat.append(self)
        ProsesPalsu.sisa = list(ProsesPalsu.sisa_setelah_popen)

    def poll(self):
        self.n += 1
        return None if self.n < ProsesPalsu.selesai_setelah else 0

    def wait(self):
        return 0


def _matikan_palsu(profil):
    n = len(ProsesPalsu.sisa)
    ProsesPalsu.dimatikan += n
    ProsesPalsu.sisa = []
    return n


@pytest.fixture
def login_siap(lingkungan, monkeypatch):
    ProsesPalsu.dibuat.clear()
    ProsesPalsu.sisa, ProsesPalsu.sisa_setelah_popen, ProsesPalsu.dimatikan = [], [], 0
    monkeypatch.setattr(ProsesPalsu, "selesai_setelah", 3)
    monkeypatch.setattr(peramban, "cari_chrome", lambda: "/palsu/chrome")
    monkeypatch.setattr(peramban.subprocess, "Popen", lambda args, **k: ProsesPalsu(args))
    monkeypatch.setattr(peramban, "proses_chrome_profil", lambda profil: list(ProsesPalsu.sisa))
    monkeypatch.setattr(peramban, "matikan_proses_profil", _matikan_palsu)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(peramban.time, "sleep", lambda s: None)
    monkeypatch.setattr(peramban.sys, "stdin", StdinPalsu(False))            # non-interaktif kecuali tes mengubahnya
    return ProsesPalsu


def test_login_manual_membuka_chrome_BIASA_dengan_profil_scraper(login_siap, capsys):
    assert login_manual() is True
    args = login_siap.dibuat[0].args
    assert args[0] == "/palsu/chrome"
    assert f"--user-data-dir={os.path.abspath(cfg.DIR_PROFIL)}" in args      # profil yang sama dengan yang dipakai Selenium
    assert "--profile-directory=Default" in args                             # tanpa profile picker; sama dengan Selenium
    assert any("accounts.google.com/ServiceLogin" in a for a in args)
    assert "--disable-background-mode" in args                               # agar Chrome benar-benar berhenti saat ditutup
    # Chrome BIASA: tanpa kendali otomatis, sebab Google sering menolak login di browser yang dikendalikan Selenium
    assert not any("remote-debugging" in a or "enable-automation" in a for a in args)
    keluar = capsys.readouterr().out
    assert "akun khusus" in keluar and ".chrome_profile" in keluar


def test_login_manual_memperingatkan_jika_profil_masih_dipakai_chrome_setelah_ENTER(login_siap, capsys):
    login_siap.sisa_setelah_popen = [(4242, "chrome.exe")]                   # Chrome 'tidak pernah' melepas profil
    assert login_manual() is True                                           # tidak menggantung selamanya
    keluar = capsys.readouterr().out
    assert "Masih ada 1 proses Chrome" in keluar and "4242" in keluar and "Task Manager" in keluar
    assert login_siap.dimatikan == 0                                        # non-interaktif: tidak mematikan apa pun diam-diam


def test_login_manual_menawarkan_mematikan_proses_yang_masih_memakai_profil(login_siap, monkeypatch):
    login_siap.sisa_setelah_popen = [(4242, "chrome.exe"), (4243, "chrome.exe")]
    monkeypatch.setattr(peramban.sys, "stdin", StdinPalsu(True))
    monkeypatch.setattr("builtins.input", lambda *a: "y" if a and "Matikan paksa" in a[0] else "")
    assert login_manual() is True
    assert login_siap.dimatikan == 2


def test_login_manual_tidak_mulai_jika_ada_sisa_chrome_lama_dan_tidak_dimatikan(login_siap, capsys):
    """Sisa Chrome lama 'menelan' jendela login (Chrome kedua menyerahkan permintaan ke yang pertama lalu keluar)."""
    login_siap.sisa = [(111, "chrome.exe")]
    assert login_manual() is False
    assert login_siap.dibuat == [] and "Tutup dulu Chrome tersebut" in capsys.readouterr().out


def test_login_manual_mematikan_sisa_chrome_lama_bila_pengguna_setuju(login_siap, monkeypatch):
    login_siap.sisa = [(111, "chrome.exe")]
    monkeypatch.setattr(peramban.sys, "stdin", StdinPalsu(True))
    monkeypatch.setattr("builtins.input", lambda *a: "y" if a and "Matikan paksa" in a[0] else "")
    assert login_manual() is True
    assert login_siap.dimatikan == 1 and len(login_siap.dibuat) == 1


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


# ─── Profil Chrome terkunci: temuan dari komputer pengguna ────────────────────
# Setelah --login, Selenium gagal dengan "session not created: Chrome instance exited". Itu tanda khas profil
# yang masih dipakai proses Chrome lain. Login tersimpan di profil, jadi TIDAK boleh diam-diam pindah ke profil sementara.

@pytest.mark.parametrize("pesan, harapan", [
    ("session not created: Chrome instance exited. Examine ChromeDriver verbose log to determine the cause.", True),
    ("session not created: DevToolsActivePort file doesn't exist", True),
    ("session not created: user data directory is already in use", True),
    ("unknown error: Chrome failed to start: exited abnormally", True),
    ("session not created: This version of ChromeDriver only supports Chrome version 147", False),
    ("invalid argument: unrecognized chrome option", False),
])
def test_tanda_profil_terkunci(pesan, harapan):
    assert tanda_profil_terkunci(SessionNotCreatedException(pesan)) is harapan


class DriverPalsu:
    capabilities = {"browserVersion": "158.0.0", "chrome": {"chromedriverVersion": "158.0.1 (abc)"}}

    def set_page_load_timeout(self, detik):
        pass

    def execute_cdp_cmd(self, *a, **k):
        return {}

    def execute_script(self, *a, **k):
        return "Mozilla/5.0 Chrome"

    def quit(self):
        pass


@pytest.fixture
def pb_palsu(lingkungan, monkeypatch):
    """Peramban yang gagal membuka Chrome `gagal` kali (pesan khas profil terkunci) lalu berhasil."""
    monkeypatch.setattr(peramban.time, "sleep", lambda s: None)
    monkeypatch.setattr(peramban.sys, "stdin", StdinPalsu(False))
    kejadian = {"percobaan": 0, "dibunuh": 0, "pids": [], "gagal": 0}

    def siapkan(gagal, pids=None):
        pb = Peramban(headless=True)
        kejadian.update(percobaan=0, dibunuh=0, pids=list(pids or []), gagal=gagal)

        def mulai_sekali(profil, log_driver=None):
            kejadian["percobaan"] += 1
            if kejadian["percobaan"] <= kejadian["gagal"]:
                raise SessionNotCreatedException("session not created: Chrome instance exited. Examine ChromeDriver verbose log")
            pb.driver = DriverPalsu()

        def bunuh(profil):
            kejadian["dibunuh"] += len(kejadian["pids"])
            kejadian["pids"] = []
            return 1

        monkeypatch.setattr(pb, "_mulai_sekali", mulai_sekali)
        monkeypatch.setattr(peramban, "proses_chrome_profil", lambda profil: list(kejadian["pids"]))
        monkeypatch.setattr(peramban, "matikan_proses_profil", bunuh)
        return pb, kejadian

    return siapkan


def test_chrome_instance_exited_sesaat_dicoba_ulang_lalu_berhasil(pb_palsu):
    pb, k = pb_palsu(gagal=1)                       # mis. jendela login masih sempat menutup diri
    pb.mulai()
    assert k["percobaan"] == 2 and pb.driver is not None and k["dibunuh"] == 0


def test_chrome_instance_exited_terus_menerus_error_jelas_dengan_pid_dan_langkah_perbaikan(pb_palsu):
    pb, k = pb_palsu(gagal=99, pids=[(4242, "chrome.exe")])
    with pytest.raises(ChromeTidakBisaDibukaError) as info:
        pb.mulai()
    pesan = str(info.value)
    assert k["percobaan"] == 3
    assert "PID 4242" in pesan and "Task Manager" in pesan and "--diagnosa" in pesan and "--login" in pesan
    assert "masih dipakai proses Chrome lain" in pesan
    assert k["dibunuh"] == 0                        # non-interaktif: tidak mematikan proses apa pun diam-diam


def test_proses_sisa_ditawarkan_untuk_dimatikan_di_terminal_interaktif(pb_palsu, monkeypatch):
    pb, k = pb_palsu(gagal=1, pids=[(4242, "chrome.exe")])
    monkeypatch.setattr(peramban.sys, "stdin", StdinPalsu(True))
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    pb.mulai()
    assert k["dibunuh"] == 1 and k["percobaan"] == 2


def test_jawaban_tidak_pada_tawaran_mematikan_proses_tidak_mematikan_apa_pun(pb_palsu, monkeypatch):
    pb, k = pb_palsu(gagal=99, pids=[(4242, "chrome.exe")])
    monkeypatch.setattr(peramban.sys, "stdin", StdinPalsu(True))
    monkeypatch.setattr("builtins.input", lambda *a: "")
    with pytest.raises(ChromeTidakBisaDibukaError):
        pb.mulai()
    assert k["dibunuh"] == 0


def test_ulang_mematikan_sisa_proses_sesi_sebelumnya_tanpa_bertanya(pb_palsu, monkeypatch):
    pb, k = pb_palsu(gagal=0, pids=[(4242, "chrome.exe")])
    monkeypatch.setattr(peramban.sys, "stdin", StdinPalsu(True))

    def jangan_tanya(*a):
        raise AssertionError("ulang() tidak boleh bertanya: itu sisa proses milik scraper sendiri")

    monkeypatch.setattr("builtins.input", jangan_tanya)
    pb.ulang()
    assert k["dibunuh"] == 1 and pb.driver is not None


def test_error_chrome_lain_tidak_diulang_sebagai_profil_terkunci(lingkungan, monkeypatch):
    percobaan = []

    def chrome_palsu(service=None, options=None):
        percobaan.append(1)
        raise WebDriverException("session not created: This version of ChromeDriver only supports Chrome version 147")

    monkeypatch.setattr(peramban.webdriver, "Chrome", chrome_palsu)
    monkeypatch.setattr(Peramban, "_service", lambda self, log_driver=None: None)
    with pytest.raises(WebDriverException, match="only supports Chrome version"):
        Peramban(headless=True).mulai()
    assert len(percobaan) == 1


def test_profil_terkunci_diulang_dengan_profil_YANG_SAMA_tanpa_profil_sementara(lingkungan, monkeypatch):
    """Profil sementara berarti kehilangan login -> sengaja tidak dipakai."""
    dipakai = []

    def chrome_palsu(service=None, options=None):
        dipakai.append(next(a for a in options.arguments if a.startswith("--user-data-dir")))
        raise WebDriverException("session not created: user data directory is already in use")

    monkeypatch.setattr(peramban.webdriver, "Chrome", chrome_palsu)
    monkeypatch.setattr(Peramban, "_service", lambda self, log_driver=None: None)
    monkeypatch.setattr(peramban.time, "sleep", lambda s: None)
    monkeypatch.setattr(peramban, "proses_chrome_profil", lambda profil: [])
    with pytest.raises(ChromeTidakBisaDibukaError, match="masih dipakai proses Chrome lain"):
        Peramban(headless=True).mulai()
    assert len(dipakai) == 3 and len(set(dipakai)) == 1


@pytest.mark.skipif(os.name == "nt", reason="cabang POSIX (di Windows memakai PowerShell/taskkill)")
def test_proses_chrome_profil_menemukan_dan_mematikan_hanya_yang_memakai_profil(tmp_path):
    profil_a, profil_b = str(tmp_path / "profil_a"), str(tmp_path / "profil_b")
    kode = "import time; time.sleep(60)"
    pa = subprocess.Popen([sys.executable, "-c", kode, "chrome", f"--user-data-dir={profil_a}"])
    pb_ = subprocess.Popen([sys.executable, "-c", kode, "chrome", f"--user-data-dir={profil_b}"])
    try:
        time.sleep(0.5)
        assert [pid for pid, _ in proses_chrome_profil(profil_a)] == [pa.pid]
        assert matikan_proses_profil(profil_a) == 1
        pa.wait(timeout=10)
        assert pb_.poll() is None                   # Chrome dengan profil lain (mis. Chrome biasa) tidak tersentuh
        assert proses_chrome_profil(profil_a) == []
    finally:
        for pr in (pa, pb_):
            pr.kill()
            pr.wait()


# ─── Diagnosa Chrome ──────────────────────────────────────────────────────────

def _atur_diagnosa(monkeypatch, berhasil_profil_scraper, berhasil_profil_sementara):
    profil = os.path.abspath(cfg.DIR_PROFIL)

    def mulai_sekali(self, folder, log_driver=None):
        ok = berhasil_profil_scraper if folder == profil else berhasil_profil_sementara
        if not ok:
            raise SessionNotCreatedException("session not created: Chrome instance exited")
        self.driver = DriverPalsu()

    monkeypatch.setattr(Peramban, "_mulai_sekali", mulai_sekali)
    monkeypatch.setattr(peramban, "proses_chrome_profil", lambda p: [(4242, "chrome.exe")])


def test_diagnosa_profil_scraper_bisa_dibuka(lingkungan, monkeypatch, capsys):
    _atur_diagnosa(monkeypatch, True, True)
    assert jalankan_diagnosa() == 0
    keluar = capsys.readouterr().out
    assert "BERHASIL" in keluar and "chromedriver 158.0.1" in keluar and "PID 4242" in keluar
    assert os.path.exists(os.path.join(cfg.DIR_DEBUG, "chromedriver_diagnosa.log"))


def test_diagnosa_menyimpulkan_profil_terkunci_bila_hanya_profil_kosong_yang_bisa_dibuka(lingkungan, monkeypatch, capsys):
    _atur_diagnosa(monkeypatch, False, True)
    assert jalankan_diagnosa() == 1
    keluar = capsys.readouterr().out
    assert "GAGAL" in keluar and "BERHASIL" in keluar
    assert "terkunci" in keluar and "chromedriver_diagnosa.log" in keluar


def test_diagnosa_menyimpulkan_masalah_umum_bila_semua_gagal(lingkungan, monkeypatch, capsys):
    _atur_diagnosa(monkeypatch, False, False)
    assert jalankan_diagnosa() == 1
    assert "masalah ada pada Chrome/chromedriver itu sendiri" in capsys.readouterr().out


def test_main_diagnosa_memanggil_jalankan_diagnosa(lingkungan, monkeypatch):
    monkeypatch.setattr(scraper, "jalankan_diagnosa", lambda: 7)
    assert scraper.main(["--diagnosa"]) == 7


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
