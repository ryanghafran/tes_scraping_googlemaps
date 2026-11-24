"""
Tes integrasi: Chrome sungguhan (headless) terhadap mock Google Maps.

Inilah bukti bahwa mekanisme scroll/panen/pemulihan bekerja pada browser nyata, termasuk
perilaku "patologis" yang menjadi dugaan penyebab masalah di dunia nyata:
  - tab Ringkasan punya kartu preview (versi lama mengira itu tab Ulasan sudah terbuka)
  - pemuat ulasan yang macet kecuali digoyang / hanya bereaksi pada event wheel
  - DOM yang dirampingkan (virtualisasi), class CSS yang berubah total
  - CAPTCHA, browser yang mati di tengah jalan, daftar yang benar-benar macet
"""

import csv
import json
import logging
import os
from collections import Counter

import pytest

import konfigurasi as cfg
import gmaps
import scraper
from gmaps import (
    JS_IDS,
    JS_TAB_AKTIF,
    TempatTidakDitemukan,
    ambil_ulasan_tempat,
    buka_ulasan,
    resolve_tempat,
)
from penyimpanan import KOLOM_CSV, PenyimpananUlasan, StatusTempat, baca_daftar_input
from peramban import DiblokirGoogleError, LoginDiperlukanError, jalankan_diagnosa

pytestmark = pytest.mark.integrasi
CEPAT = {"latency": [40, 120]}


def tempat(nama, total=300, mock=None, **ekstra):
    spec = {"name": nama, "total": total, "mock": {**CEPAT, **(mock or {})}}
    spec.update(ekstra)
    return spec


def siap(pb, buat_mock, places, nama, config=None):
    m = buat_mock(places, config)
    return m, resolve_tempat(pb, nama), PenyimpananUlasan()


# ─── Mencari tempat & status buka/tutup ───────────────────────────────────────

def test_resolve_direct_match_membaca_semua_detail(pb, buat_mock):
    buat_mock([tempat("Kopi A", 1234, rating=4.6, address="Jl. Contoh No.9, Bandung")])
    info = resolve_tempat(pb, "Kopi A")
    assert info["nama_resmi"] == "Kopi A" and info["total_ulasan"] == 1234 and info["rating"] == 4.6
    assert info["alamat"] == "Jl. Contoh No.9, Bandung" and info["kategori"] == "Kafe"
    assert info["status_buka"] == "aktif" and "/maps/place/" in info["url"] and info["id_tempat"].startswith("0x2e68e")


def test_resolve_dari_daftar_hasil_memilih_yang_paling_mirip_apa_pun_urutannya(pb, buat_mock):
    places = [tempat("Kopi Ireng Dago"), tempat("Kopi Ireng"), tempat("Kopi Ireng Pasteur")]
    for urut_terbalik in (False, True):
        buat_mock(places, {"always_list": True, "reverse_list": urut_terbalik})
        assert resolve_tempat(pb, "Kopi Ireng")["nama_resmi"] == "Kopi Ireng"


def test_status_tutup_permanen_dan_sementara_terdeteksi_tanpa_alarm_palsu(pb, buat_mock):
    buat_mock([tempat("Kopi P", status="permanent"), tempat("Kopi S", status="temporary"),
               tempat("Kopi T", closed_now=True), tempat("Kopi U")])
    assert resolve_tempat(pb, "Kopi P")["status_buka"] == "tutup_permanen"
    assert resolve_tempat(pb, "Kopi S")["status_buka"] == "tutup_sementara"
    assert resolve_tempat(pb, "Kopi T")["status_buka"] == "aktif"        # 'Tutup ⋅ Buka pukul 10.00' hanya tutup saat ini
    assert resolve_tempat(pb, "Kopi U")["status_buka"] == "aktif"


def test_tempat_tidak_ada(pb, buat_mock):
    buat_mock([tempat("Kopi A")])
    with pytest.raises(TempatTidakDitemukan):
        resolve_tempat(pb, "Warung Tidak Ada Sama Sekali")


def test_antarmuka_bahasa_inggris_label_tab_urutan_dan_status(pb, buat_mock, monkeypatch):
    monkeypatch.setattr(cfg, "BAHASA", "en")
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 120), tempat("Kopi P", status="permanent")], "Kopi A")
    assert resolve_tempat(pb, "Kopi P")["status_buka"] == "tutup_permanen"      # 'Permanently closed'
    info = resolve_tempat(pb, "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 20, ["terbaru"])                    # tab 'Reviews', menu 'Newest'
    assert h.baru == 20 and h.alasan == "kuota"


# ─── Tab Ulasan: perbaikan akar masalah #1 ────────────────────────────────────

def test_kartu_preview_di_tab_ringkasan_bukan_bukti_tab_ulasan_terbuka(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200)], "Kopi A")
    assert len(pb.js(JS_IDS)["ids"]) == 3                      # ada 3 kartu preview di Ringkasan (jebakan versi lama)...
    assert pb.js(JS_TAB_AKTIF)["aktif"] is False               # ...tetapi tab Ulasan BELUM terbuka
    buka_ulasan(pb, info, "terbaru")
    st = pb.js(JS_TAB_AKTIF)
    assert st["aktif"] and st["alasan"] == "tab" and st["tab_terpilih"].lower() == "ulasan"
    assert len(pb.js(JS_IDS)["ids"]) >= 8


# ─── Memanen ratusan ulasan: perbaikan akar masalah #2 ────────────────────────

def test_panen_120_ulasan_lengkap_akurat_dan_tanpa_balasan_pemilik(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 400)], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 120, ["terbaru"])
    assert (h.baru, h.alasan, h.habis) == (120, "kuota", False)
    assert store.total() == 120 and h.terlihat > 150           # banyak kartu tanpa teks dilewati, bukan disimpan

    benar = {r["id"]: r for r in m.ulasan_bersyarat_teks("Kopi A")}
    ids = [r["id_ulasan"] for r in store.rekaman]
    assert len(set(ids)) == 120
    for r in store.rekaman:
        gt = benar[r["id_ulasan"]]                              # hanya ulasan yang memang berteks di Maps
        assert r["teks_ulasan"] == gt["teks"]                   # teks UTUH: tombol 'Selengkapnya' sudah dibuka
        assert (r["rating"], r["nama_reviewer"], r["tanggal"]) == (gt["rating"], gt["nama"], gt["tanggal"])
        assert "balasan pemilik" not in r["teks_ulasan"]
    assert any(len(r["teks_ulasan"]) > 250 for r in store.rekaman)           # ada ulasan panjang yang terambil utuh
    assert [int(i[-5:]) for i in ids] == sorted(int(i[-5:]) for i in ids)      # urutan 'Terbaru'

    # ulasan TANPA teks tapi DENGAN balasan pemilik tidak boleh tercatat sebagai ulasan
    jebakan = {r["id"] for r in m.places[0]["reviews"] if not r["teks"] and r["balasan"]}
    assert jebakan and not (jebakan & set(ids))


def test_tanggal_diedit_dibersihkan(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 100)], "Kopi A")
    ambil_ulasan_tempat(pb, store, info, 40, ["terbaru"])
    assert not any(r["tanggal"].lower().startswith("diedit") for r in store.rekaman)


def test_pemuat_yang_macet_digoyang_otomatis(pb, buat_mock, caplog):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 300, mock={"jiggle_at": 40})], "Kopi A")
    with caplog.at_level(logging.INFO):
        h = ambil_ulasan_tempat(pb, store, info, 90, ["terbaru"])
    assert (h.baru, h.alasan) == (90, "kuota")                 # jauh melewati titik macet di 40 kartu
    assert h.terlihat > 120
    assert any("digoyang" in r.message for r in caplog.records)


def test_pemuat_yang_hanya_bereaksi_pada_event_wheel(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200, mock={"needs_wheel": True})], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 50, ["terbaru"])
    assert (h.baru, h.alasan) == (50, "kuota")


def test_dom_virtualisasi_tetap_terpanen_utuh_karena_panen_tiap_langkah(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 300, mock={"virtualize_keep": 25})], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 100, ["terbaru"])
    assert (h.baru, h.alasan) == (100, "kuota")
    assert len({r["id_ulasan"] for r in store.rekaman}) == 100
    assert h.terlihat > 100                                    # lebih banyak dari yang ada di DOM kapan pun (maks 25)


def test_class_css_berubah_total_masih_terbaca_lewat_atribut_struktural(pb, buat_mock):
    m, info, store = siap(pb, buat_mock,
                          [tempat("Kopi A", 200, mock={"variant": "B"}, balasan_di_b=True)], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 60, ["terbaru"])
    assert (h.baru, h.alasan) == (60, "kuota")
    benar = {r["id"]: r for r in m.ulasan_bersyarat_teks("Kopi A")}
    for r in store.rekaman:
        gt = benar[r["id_ulasan"]]
        assert r["teks_ulasan"] == gt["teks"]                  # bukan nama, tanggal, tombol, maupun balasan pemilik
        assert (r["rating"], r["nama_reviewer"], r["tanggal"]) == (gt["rating"], gt["nama"], gt["tanggal"])


def test_urutan_ganda_terbaru_dan_terendah_tanpa_duplikat(pb, buat_mock, monkeypatch):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 400)], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 80, ["terbaru", "terendah"])
    assert h.baru == 80 and len({r["id_ulasan"] for r in store.rekaman}) == 80
    terendah = [r["rating"] for r in store.rekaman if r["urutan"] == "terendah"]
    assert len(terendah) >= 20 and terendah == sorted(terendah)               # menaik: 1 bintang dulu
    assert terendah[0] == 1
    baru = [r["rating"] for r in store.rekaman if r["urutan"] == "terbaru"]
    assert len(baru) >= 20 and set(baru) >= {4, 5}


# ─── Akhir daftar, macet total, CAPTCHA, browser mati ─────────────────────────

def test_daftar_kecil_dikenali_habis_dengan_cepat(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi K", 45)], "Kopi K")
    h = ambil_ulasan_tempat(pb, store, info, 100, ["terbaru"])
    assert h.habis and h.alasan == "habis"
    assert h.baru == len(m.ulasan_bersyarat_teks("Kopi K")) == store.total()   # semua ulasan berteks terambil


def test_macet_total_berakhir_sebagai_sebagian_dengan_data_diagnosis(pb, buat_mock, lingkungan):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi M", 300, mock={"hard_stall_at": 50})], "Kopi M")
    h = ambil_ulasan_tempat(pb, store, info, 200, ["terbaru"])
    assert h.alasan == "macet" and not h.habis and h.terlihat == 50
    assert 0 < h.baru == store.total() <= 50
    berkas = os.listdir(cfg.DIR_DEBUG)
    assert any(b.startswith("macet_") and b.endswith(".png") for b in berkas)
    assert any(b.startswith("macet_") and b.endswith(".html") for b in berkas)


def test_captcha_pada_mode_headless_berhenti_rapi_dengan_screenshot(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 300, mock={"captcha_after_reqs": 3})], "Kopi A")
    with pytest.raises(DiblokirGoogleError):
        scraper.dengan_pemulihan(pb, ambil_ulasan_tempat, pb, store, info, 200, ["terbaru"], nama="Kopi A")
    assert 0 < store.total() < 200                              # data sebelum CAPTCHA tidak hilang
    assert any(b.startswith("captcha_") for b in os.listdir(cfg.DIR_DEBUG))


def test_browser_mati_di_tengah_jalan_dibuka_ulang_dan_tempat_tetap_selesai(pb, buat_mock, monkeypatch):
    """Bug versi lama: satu 'invalid session id' membuat 39 tempat gagal beruntun."""
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 300)], "Kopi A")
    asli, tercatat = gmaps.panen_ulasan, {"mati": 0}

    def sabotase(pb_, *a, **k):
        if not tercatat["mati"]:
            tercatat["mati"] = 1
            pb_.driver.quit()                                   # jendela Chrome 'tertutup' tiba-tiba
        return asli(pb_, *a, **k)

    monkeypatch.setattr(gmaps, "panen_ulasan", sabotase)
    h = scraper.dengan_pemulihan(pb, ambil_ulasan_tempat, pb, store, info, 50, ["terbaru"], nama="Kopi A")
    assert tercatat["mati"] == 1 and (h.baru, h.alasan) == (50, "kuota") and store.total() == 50


# ─── End-to-end lewat scraper.main() ──────────────────────────────────────────

def tulis_input(nama_list):
    with open(cfg.INPUT_CSV, "w", encoding="utf-8", newline="") as f:
        f.write("nama_tempat\n" + "\n".join(nama_list) + "\n")


def baca_hasil():
    with open(cfg.OUTPUT_CSV, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=cfg.CSV_SEPARATOR))


def kunci_baris(baris):
    return {(b["nama_tempat"], b["nama_reviewer"], b["teks_ulasan"]) for b in baris}


def test_main_end_to_end_pangkas_tutup_ronde_resume_dan_tanpa_duplikat(lingkungan, buat_mock, chrome_tersedia):
    m = buat_mock([tempat("Kopi A", 200), tempat("Kopi B", 80, status="permanent"), tempat("Kopi C", 150),
                   tempat("Kopi D", 60, status="temporary"), tempat("Kopi E", 40)])
    tulis_input([" Kopi A", "Kopi B", "kopi a", "Kopi C", "Kopi D", "Kopi E"])     # ada spasi & nama ganda

    assert scraper.main(["--target", "120", "--headless"]) == 0
    baris = baca_hasil()
    assert len(baris) == 120 and len(kunci_baris(baris)) == 120                  # TEPAT target, tanpa duplikat
    assert list(baris[0].keys()) == KOLOM_CSV
    per = Counter(b["nama_tempat"] for b in baris)
    assert set(per) == {"Kopi A", "Kopi C", "Kopi E"}
    assert per["Kopi E"] == len(m.ulasan_bersyarat_teks("Kopi E"))               # tempat kecil: SEMUA ulasan berteksnya terambil
    assert abs(per["Kopi A"] - per["Kopi C"]) <= 1                               # sisa target dialihkan ke tempat yang masih punya
    assert per["Kopi A"] > 40 and per["Kopi C"] > 40                             # (bukan dibagi rata dengan tempat yang sudah habis)
    assert baca_daftar_input() == ["Kopi A", "Kopi C", "Kopi E"]                 # cafe tutup dibuang dari daftar
    tutup = open(cfg.FILE_TUTUP, encoding="utf-8-sig").read()
    assert "Kopi B" in tutup and "Kopi D" in tutup and "tutup_permanen" in tutup and "tutup_sementara" in tutup
    st = StatusTempat()
    assert st.ambil("Kopi E")["habis"] is True                                    # tempat kecil terkuras seluruhnya
    assert st.ambil("Kopi B")["status_scrape"] == "belum"                         # cafe tutup tidak pernah di-scrape

    # Jalan ulang dengan target sama: tidak ada yang berubah (resume)
    master = open(cfg.FILE_MASTER, encoding="utf-8").read()
    assert scraper.main(["--target", "120", "--headless"]) == 0
    assert open(cfg.FILE_MASTER, encoding="utf-8").read() == master

    # Naikkan target: hanya menambah ulasan baru, tidak ada duplikat
    assert scraper.main(["--target", "180", "--headless"]) == 0
    baris2 = baca_hasil()
    assert len(baris2) == 180 and len(kunci_baris(baris2)) == 180
    assert kunci_baris(baris) <= kunci_baris(baris2)                              # ulasan lama tetap ada


def test_main_data_lama_dari_csv_tidak_diduplikasi_walau_nama_kueri_berbeda(lingkungan, buat_mock, chrome_tersedia):
    m = buat_mock([tempat("Kopi A Dago", 200)])
    tulis_input(["Kopi A"])                        # nama kueri BERBEDA dari nama resmi di Maps (kasus nyata)
    lama = m.ulasan_bersyarat_teks("Kopi A Dago")[:3]
    with open(lingkungan / "hasil_ulasan.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL)
        w.writerow(KOLOM_CSV)
        for r in lama:
            w.writerow(["Kopi A Dago", "alamat lama", r["nama"], r["rating"], r["tanggal"], r["teks"]])
    assert scraper.main(["--target", "40", "--headless"]) == 0
    baris = baca_hasil()
    assert len(baris) == 40 and len(kunci_baris(baris)) == 40
    master = [json.loads(x) for x in open(cfg.FILE_MASTER, encoding="utf-8")]
    assert sum(1 for r in master if r["sumber"] == "impor-lama") == 3


def test_main_hanya_mengambil_tempat_terpilih_saja_walau_master_sudah_besar(lingkungan, buat_mock, chrome_tersedia, capsys):
    """Uji cepat: python scraper.py --hanya "kopi a" --target 25"""
    buat_mock([tempat("Kopi A", 200), tempat("Kopi B", 200)])
    tulis_input(["Kopi A", "Kopi B"])
    s = PenyimpananUlasan()
    for i in range(100):                                       # data lama dari tempat lain
        s.tambah("Tempat Lain", "x", f"r{i}", 5, "x", f"teks lama {i}")
    s.tutup()
    assert scraper.main(["--hanya", "kopi a", "--target", "25", "--headless"]) == 0
    per = Counter(b["nama_tempat"] for b in baca_hasil())
    assert per["Kopi A"] == 25 and "Kopi B" not in per and per["Tempat Lain"] == 100
    assert "ULASAN TEMPAT TERPILIH: 25 / target 25" in capsys.readouterr().out      # bukan "125 / target 25" yang membingungkan


def test_main_cek_status_saja_tidak_mengambil_ulasan(lingkungan, buat_mock, chrome_tersedia):
    buat_mock([tempat("Kopi A", 100), tempat("Kopi B", 50, status="permanent")])
    tulis_input(["Kopi A", "Kopi B"])
    assert scraper.main(["--cek-status", "--headless"]) == 0
    assert baca_daftar_input() == ["Kopi A"]
    assert not os.path.exists(cfg.FILE_MASTER) or os.path.getsize(cfg.FILE_MASTER) == 0
    assert StatusTempat().ambil("Kopi A")["total_ulasan"] == 100


def test_main_tempat_dengan_daftar_ulasan_kosong_padahal_ada_ulasan_dicatat_gagal(lingkungan, buat_mock, chrome_tersedia):
    buat_mock([tempat("Kopi X", 500, jumlah_ulasan=0)])      # header bilang 500 ulasan, tetapi daftar kosong
    tulis_input(["Kopi X"])
    assert scraper.main(["--target", "50", "--headless"]) == 0
    s = StatusTempat().ambil("Kopi X")
    assert s["status_scrape"] == "gagal" and s["percobaan_gagal"] == 1 and not s["habis"]
    assert "Kopi X" in open(cfg.LOG_GAGAL, encoding="utf-8").read()
    assert any(b.startswith("kosong_") for b in os.listdir(cfg.DIR_DEBUG))


# ─── Dinding login Google (temuan dari screenshot pengguna) ───────────────────

def test_dinding_login_saat_ulasan_dibuka_menghasilkan_LoginDiperlukanError(pb, buat_mock, lingkungan):
    """Tanpa login, 'Ulasan lainnya' memunculkan modal 'Login untuk menikmati fitur terbaik dari Google Maps'."""
    buat_mock([tempat("Kopi A", 300)], {"logged_in": False, "login_wall": True})
    info = resolve_tempat(pb, "Kopi A")
    store = PenyimpananUlasan()
    with pytest.raises(LoginDiperlukanError, match="--login"):
        ambil_ulasan_tempat(pb, store, info, 50, ["terbaru"])
    assert store.total() == 0
    assert any(b.startswith("dinding_login_") for b in os.listdir(cfg.DIR_DEBUG))      # screenshot + HTML untuk diagnosis
    assert not gmaps.ada_dinding_login(pb)                                              # modal sudah ditutup


def test_dinding_login_di_tengah_scroll_menyimpan_data_sebelumnya_lalu_berhenti(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 300, mock={"login_wall_after_reqs": 3})], "Kopi A",
                          {"logged_in": True})
    with pytest.raises(LoginDiperlukanError):
        ambil_ulasan_tempat(pb, store, info, 200, ["terbaru"])
    assert 10 < store.total() < 60                                                      # ulasan sebelum dinding tidak hilang


def test_modal_login_yang_bisa_ditutup_dan_tab_tetap_terbuka_tidak_dianggap_dinding(pb, buat_mock):
    """Tanpa login_wall di mock, tab Ulasan terbuka normal walau pengguna belum login: tidak boleh alarm palsu."""
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200)], "Kopi A", {"logged_in": False})
    h = ambil_ulasan_tempat(pb, store, info, 30, ["terbaru"])
    assert (h.baru, h.alasan) == (30, "kuota")


def test_status_login_masuk_dan_keluar(pb, buat_mock):
    for logged_in, harapan in ((True, "masuk"), (False, "keluar")):
        m = buat_mock([tempat("Kopi A")], {"logged_in": logged_in})
        pb.buka(m.base_url + "/search/Kopi+A+Bandung?hl=id")
        assert gmaps.status_login(pb) == harapan


def test_main_dinding_login_berhenti_dengan_instruksi_kode_3_tanpa_menandai_gagal(
        lingkungan, buat_mock, chrome_tersedia, capsys):
    buat_mock([tempat("Kopi A", 300), tempat("Kopi C", 200)], {"logged_in": False, "login_wall": True})
    tulis_input(["Kopi A", "Kopi C"])
    assert scraper.main(["--target", "50", "--headless"]) == 3
    keluar = capsys.readouterr().out
    assert "BELUM login" in keluar                          # peringatan dini sebelum mulai mengambil ulasan
    assert "python scraper.py --login" in keluar            # instruksi akhir yang jelas
    st = StatusTempat()
    assert st.ambil("Kopi A")["percobaan_gagal"] == 0 and st.ambil("Kopi A")["status_scrape"] != "gagal"
    assert st.ambil("Kopi A")["url"] and st.ambil("Kopi C")["url"]       # hasil Tahap 1 tersimpan: setelah login tidak diulang
    assert not os.path.exists(cfg.LOG_GAGAL)


def test_belum_login_tanpa_dinding_hanya_memperingatkan_dan_tetap_lanjut(lingkungan, buat_mock, chrome_tersedia, capsys):
    buat_mock([tempat("Kopi A", 200)], {"logged_in": False})
    tulis_input(["Kopi A"])
    assert scraper.main(["--target", "30", "--headless"]) == 0
    assert "BELUM login" in capsys.readouterr().out
    assert len(baca_hasil()) == 30


def test_total_ulasan_dibaca_dari_label_ulasan_lainnya_jika_blok_rating_tidak_ada(pb, buat_mock):
    """Cadangan: label tombol 'Ulasan lainnya (2.283)' seperti pada screenshot Maps asli."""
    m = buat_mock([tempat("Kopi A", 2283)])
    pb.buka(m.base_url + "/search/Kopi+A+Bandung?hl=id")
    gmaps._tunggu_keadaan(pb)
    pb.js("document.querySelector('div.F7nice').remove();"
          "document.getElementById('more-reviews').textContent='Ulasan lainnya (2.283)';")
    detail = pb.js(gmaps.JS_DETAIL_TEMPAT)
    assert detail["total"] == "2.283" and gmaps.angka_dari_teks(detail["total"]) == 2283


# ─── Tab asli Maps: Ringkasan | Menu | Ulasan | Tentang (dari screenshot pengguna) ─

def test_tab_ulasan_dicari_lewat_label_bukan_posisi_tab_menu_tidak_ikut_terbuka(pb, buat_mock):
    """Tab ke-2 di Maps asli adalah 'Menu'. Tanpa atribut role=tab, tab Ulasan harus dikenali dari LABEL-nya."""
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200)], "Kopi A", {"tabs_without_role": True})
    buka_ulasan(pb, info, "terbaru")
    st = pb.js(JS_TAB_AKTIF)
    assert st["aktif"] and st["alasan"] in ("tombol-urutkan", "url")        # tanpa role=tab: dikenali dari tombol Urutkan / URL
    assert "MENU-MOCK" not in pb.js("return document.body.innerText")        # tab 'Menu' tidak pernah diklik
    assert len(pb.js(JS_IDS)["ids"]) >= 8


def test_empat_tab_dengan_role_tab_terbaca_sebagai_ulasan(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 120)], "Kopi A")
    assert pb.js("return Array.from(document.querySelectorAll('[role=tab]')).map(t => t.innerText.trim())") == \
        ["Ringkasan", "Menu", "Ulasan", "Tentang"]
    h = ambil_ulasan_tempat(pb, store, info, 30, ["terbaru"])
    assert (h.baru, h.alasan) == (30, "kuota")


# ─── Diagnosa Chrome dengan Chrome sungguhan ──────────────────────────────────

def test_diagnosa_dengan_chrome_sungguhan_berhasil_dan_menulis_log_chromedriver(lingkungan, chrome_tersedia, capsys):
    assert jalankan_diagnosa() == 0
    keluar = capsys.readouterr().out
    assert "BERHASIL" in keluar and "KESIMPULAN" in keluar
    log_driver = os.path.join(cfg.DIR_DEBUG, "chromedriver_diagnosa.log")
    assert os.path.exists(log_driver) and os.path.getsize(log_driver) > 0


def test_tombol_urutkan_di_tab_ringkasan_bukan_bukti_tab_ulasan_terbuka(pb, buat_mock):
    """Jika deretan tab terbaca, HANYA tab terpilih yang menentukan: tombol Urutkan bisa saja muncul di tab lain."""
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200)], "Kopi A", {"sort_on_overview": True})
    st = pb.js(JS_TAB_AKTIF)
    assert st["tombol_urutkan"] is True                          # sinyal 'longgar' menyala...
    assert st["aktif"] is False and st["tab_terpilih"] == "Ringkasan"     # ...tetapi tab Ulasan BELUM terbuka
    buka_ulasan(pb, info, "terbaru")
    st = pb.js(JS_TAB_AKTIF)
    assert st["aktif"] and st["alasan"] == "tab" and st["tab_terpilih"] == "Ulasan"


def test_tab_yang_sudah_aktif_tanpa_klik_dicatat_alasannya_di_log(pb, buat_mock, caplog):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 100)], "Kopi A")
    pb.js("document.querySelector('[role=tab][data-tab-index=\"2\"]').click();")
    with caplog.at_level(logging.INFO):
        assert gmaps._klik_tab_ulasan(pb) is True
    pesan = " ".join(r.message for r in caplog.records)
    assert "sudah aktif tanpa klik" in pesan and "tab terpilih: 'Ulasan'" in pesan and "lewat: tab" in pesan


# ─── Pemeriksa kualitas data: jangan biarkan kolom kosong merusak dataset diam-diam ───

def test_peringatan_kualitas_bila_rating_tidak_terbaca_dan_data_diagnosis_disimpan(pb, buat_mock, lingkungan):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200, mock={"no_rating_label": True})], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 30, ["terbaru"])
    assert h.baru == 30 and "tanpa rating" in h.peringatan
    assert any(b.startswith("kualitas_") for b in os.listdir(cfg.DIR_DEBUG))


def test_peringatan_kualitas_bila_selengkapnya_tidak_terbuka(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200, mock={"expand_noop": True}, long_ratio=0.7)], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 40, ["terbaru"])
    assert "masih terpotong" in h.peringatan


def test_tanpa_masalah_tidak_ada_peringatan_kualitas(pb, buat_mock):
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 200)], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 40, ["terbaru"])
    assert h.peringatan == ""


def test_daftar_lambat_dimuat_ulang_setelah_urutan_dipilih_tidak_dianggap_kosong(pb, buat_mock):
    """
    Temuan dari log pengguna: tepat setelah memilih urutan 'terendah' daftar sempat KOSONG sebentar; tempat yang
    pemuatannya lambat dinyatakan 'daftar ulasan kosong (0 kartu termuat)' padahal ulasannya ribuan.
    """
    m, info, store = siap(pb, buat_mock, [tempat("Kopi A", 300, mock={"sort_reload_ms": 4000})], "Kopi A")
    h = ambil_ulasan_tempat(pb, store, info, 20, ["terendah"])
    assert (h.alasan, h.baru) == ("kuota", 20)
