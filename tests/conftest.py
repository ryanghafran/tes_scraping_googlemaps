"""
Fixture bersama untuk semua tes.

Tes integrasi memakai Chrome sungguhan (headless) terhadap halaman palsu 'mock_maps'.
Jika Chrome/chromedriver tidak tersedia, tes integrasi otomatis di-skip.
Opsional: atur CHROME_BINARY dan CHROMEDRIVER_PATH bila Chrome Anda tidak terdeteksi otomatis.

Menjalankan:  pytest -q          (semua)      |      pytest -q -m "not integrasi"   (tanpa browser)
"""

import os
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (RAIZ, os.path.join(RAIZ, "tests", "mock_maps")):
    if p not in sys.path:
        sys.path.insert(0, p)

import konfigurasi as cfg  # noqa: E402
from server import MockMaps  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "integrasi: tes yang memakai Chrome sungguhan terhadap mock Maps")


@pytest.fixture
def lingkungan(tmp_path, monkeypatch):
    """Mengarahkan semua file/folder & jeda ke folder sementara dengan jeda super singkat."""
    def p(*bagian):
        return str(tmp_path.joinpath(*bagian))

    nilai = {
        "INPUT_CSV": p("daftar_coffeeshop.csv"), "OUTPUT_CSV": p("hasil_ulasan.csv"),
        "FILE_TUTUP": p("daftar_coffeeshop_tutup.csv"), "DIR_DATA": p("data"),
        "FILE_MASTER": p("data", "ulasan_master.jsonl"), "FILE_STATUS": p("data", "status_tempat.json"),
        "FILE_LEGACY_OK": p("data", "impor_legacy.selesai"), "LOG_RUN": p("scraper_run.log"),
        "LOG_GAGAL": p("log_gagal.txt"), "DIR_DEBUG": p("debug"), "DIR_PROFIL": p("profil"),
        "POLA_CSV_LEGACY": [p("hasil_ulasan*.csv")],
        "HEADLESS": True,
        "JEDA_SCROLL": (0.02, 0.05), "JEDA_ANTAR_TEMPAT": (0.02, 0.05), "JEDA_BUKA_HALAMAN": (0.05, 0.1),
        "TIMEOUT_MUAT_BATCH": 2.5, "MAKS_MACET": 3, "MAKS_RELOAD_PER_TEMPAT": 1,
        "TUNGGU_CAPTCHA_MENIT": 0.05, "MAKS_PERCOBAAN_TEMPAT": 3,
        "TARGET_TOTAL_ULASAN": 5000, "MAKS_ULASAN_PER_TEMPAT": 500, "URUTAN_ULASAN": ["terbaru"],
        "BATAS_ULASAN_PER_SESI": 0, "FILTER_KATEGORI": False, "BAHASA": "id", "LOKASI": "Bandung",
    }
    for k, v in nilai.items():
        monkeypatch.setattr(cfg, k, v)
    return tmp_path


@pytest.fixture
def buat_mock(monkeypatch):
    """Pabrik server mock; otomatis mengarahkan cfg.MAPS_BASE dan dimatikan saat tes selesai."""
    aktif = []

    def _buat(places, config=None):
        m = MockMaps(places, config)
        m.start()
        monkeypatch.setattr(cfg, "MAPS_BASE", m.base_url)
        aktif.append(m)
        return m

    yield _buat
    for m in aktif:
        m.stop()


@pytest.fixture(scope="session")
def chrome_tersedia(tmp_path_factory):
    """Coba menyalakan Chrome sekali; jika gagal, semua tes integrasi di-skip."""
    from peramban import Peramban
    mp = pytest.MonkeyPatch()
    mp.setattr(cfg, "DIR_PROFIL", str(tmp_path_factory.mktemp("profil_cek")))
    pb = Peramban(headless=True)
    try:
        pb.mulai()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Chrome/chromedriver tidak tersedia: {str(e)[:120]}")
    finally:
        pb.tutup()
        mp.undo()
    return True


@pytest.fixture
def pb(lingkungan, chrome_tersedia):
    """Peramban headless siap pakai untuk satu tes."""
    from peramban import Peramban
    browser = Peramban(headless=True)
    browser.mulai()
    yield browser
    browser.tutup()
