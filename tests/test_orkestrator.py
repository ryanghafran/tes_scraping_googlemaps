"""
Tes orkestrator (scraper.py) TANPA browser: pembagian ronde, batas, kegagalan, pemulihan, istirahat.
`ambil_ulasan_tempat` diganti versi palsu sehingga tes cepat dan deterministik.
"""

import csv
import json
import os

import pytest

import konfigurasi as cfg
import scraper
from gmaps import HasilPanen, TabUlasanGagal, TempatTidakDitemukan
from penyimpanan import KOLOM_CSV, PenyimpananUlasan, StatusTempat, baca_daftar_input
from peramban import DiblokirGoogleError, Peramban


class PbPalsu:
    """Pengganti Peramban: mencatat panggilan, tidak membuka browser."""
    faktor_lambat = 1.0

    def __init__(self):
        self.diulang = self.dimulai = self.ditutup = 0
        self.debug = []

    def tidur(self, *a):
        pass

    def simpan_debug(self, label):
        self.debug.append(label)

    def ulang(self):
        self.diulang += 1

    def mulai(self):
        self.dimulai += 1

    def tutup(self):
        self.ditutup += 1


def siapkan_status(nama_list, total_ulasan=None):
    st = StatusTempat()
    for n in nama_list:
        st.catat(n, nama_resmi=n, url=f"https://maps/{n}", status_buka="aktif", total_ulasan=total_ulasan)
    return st


def ambil_palsu(kolam):
    """kolam = {nama: jumlah ulasan bersyarat teks yang tersedia}. Mengembalikan (fungsi, penghitung)."""
    diambil = {n: 0 for n in kolam}

    def fake(pb, store, info, kuota, urutan):
        n = info["nama_resmi"]
        ambil = min(kuota, kolam[n] - diambil[n])
        for i in range(diambil[n], diambil[n] + ambil):
            assert store.tambah(n, "alamat", f"R{i}", 5, "x", f"ulasan {i} {n}", id_ulasan=f"{n}-{i}")
        diambil[n] += ambil
        habis = diambil[n] >= kolam[n]
        return HasilPanen(baru=ambil, terlihat=diambil[n] * 2, habis=habis,
                          alasan="habis" if (habis and ambil < kuota) else "kuota")
    return fake, diambil


def jalankan_ulasan(daftar, status, store, pb=None):
    return scraper.tahap_ulasan(pb or PbPalsu(), daftar, status, store, scraper.Sesi())


# ─── Ronde & target ───────────────────────────────────────────────────────────

def test_ronde_membagi_sisa_target_ke_tempat_yang_masih_punya_ulasan_dan_tepat_di_target(lingkungan, monkeypatch):
    daftar = ["A", "B", "C"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    fake, diambil = ambil_palsu({"A": 500, "B": 30, "C": 500})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 300)
    jalankan_ulasan(daftar, status, store)
    # Ronde 1: +100 per tempat (B hanya punya 30 -> habis). Ronde 2: sisa 70 dibagi ke A dan C.
    assert store.total() == 300                      # TEPAT target, tidak kurang/lebih
    assert diambil["B"] == 30 and status.ambil("B")["habis"] is True
    assert diambil["A"] > 100 and diambil["C"] > 100 and abs(diambil["A"] - diambil["C"]) <= 35


def test_berhenti_rapi_jika_semua_tempat_habis_sebelum_target(lingkungan, monkeypatch):
    daftar = ["A", "B"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    fake, _ = ambil_palsu({"A": 40, "B": 25})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    jalankan_ulasan(daftar, status, store)           # target 5000 jauh melebihi yang tersedia
    assert store.total() == 65
    assert status.ambil("A")["habis"] and status.ambil("B")["habis"]


def test_batas_maks_per_tempat_dihormati_agar_dataset_beragam(lingkungan, monkeypatch):
    daftar = ["A", "B", "C"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    fake, diambil = ambil_palsu({"A": 5000, "B": 5000, "C": 5000})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    monkeypatch.setattr(cfg, "MAKS_ULASAN_PER_TEMPAT", 50)
    jalankan_ulasan(daftar, status, store)
    assert diambil == {"A": 50, "B": 50, "C": 50}


def test_ulasan_lama_hasil_impor_dihitung_ke_kuota_tempat(lingkungan, monkeypatch):
    daftar = ["A", "B"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    for i in range(80):
        store.tambah("A", "x", f"lama{i}", 5, "x", f"teks lama {i}", sumber="impor-lama")
    fake, diambil = ambil_palsu({"A": 1000, "B": 1000})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 280)
    jalankan_ulasan(daftar, status, store)
    assert store.total() == 280
    # 'Mengisi sampai level sama': A sudah punya 80 jadi hanya +60, B +140 -> keduanya 140 (seimbang).
    assert (store.jumlah_tempat("A"), store.jumlah_tempat("B")) == (140, 140)
    assert diambil == {"A": 60, "B": 140}


def test_tempat_yang_gagal_di_ronde_pertama_dikejar_sampai_seimbang_di_ronde_berikutnya(lingkungan, monkeypatch):
    daftar = ["A", "B", "C"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    fake, diambil = ambil_palsu({n: 1000 for n in daftar})
    sudah_gagal = []

    def kadang_gagal(pb, store_, info, kuota, urutan):
        if info["nama_resmi"] == "A" and not sudah_gagal:
            sudah_gagal.append(1)
            raise TabUlasanGagal("gangguan sesaat")
        return fake(pb, store_, info, kuota, urutan)

    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", kadang_gagal)
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 300)
    monkeypatch.setattr(cfg, "MAKS_PERCOBAAN_TEMPAT", 1)
    jalankan_ulasan(daftar, status, store)
    assert (diambil["A"], diambil["B"], diambil["C"]) == (100, 100, 100)     # A tidak tertinggal selamanya
    assert status.ambil("A")["percobaan_gagal"] == 0                         # sukses mereset hitungan gagal


@pytest.mark.parametrize("hitung, kurang, maks, harapan", [
    ({"A": 0, "B": 0, "C": 0}, 300, 500, {"A": 100, "B": 100, "C": 100}),
    ({"A": 80, "B": 0}, 200, 500, {"A": 140, "B": 140}),                     # yang sudah banyak mendapat lebih sedikit
    ({"A": 100, "B": 100, "C": 0}, 100, 500, {"A": 100, "B": 100, "C": 100}),  # yang tertinggal dikejar dulu
    ({"A": 10, "B": 400}, 1000, 450, {"A": 450, "B": 450}),                  # dibatasi maks per tempat
    ({"A": 0, "B": 0}, 3, 500, {"A": 2, "B": 2}),                            # pembulatan ke atas (dipotong oleh target saat jalan)
    ({}, 100, 500, {}),
])
def test_rencana_batas_water_filling(hitung, kurang, maks, harapan):
    assert scraper.rencana_batas(hitung, kurang, maks) == harapan


def test_hanya_membatasi_target_pada_tempat_terpilih_agar_uji_cepat_berfungsi(lingkungan, monkeypatch):
    """'--hanya X --target 20' harus mengambil ±20 ulasan X walau data master (tempat lain) sudah ratusan."""
    status = siapkan_status(["A", "B"])
    store = PenyimpananUlasan()
    for i in range(220):                                              # data lama dari tempat lain / hasil impor
        store.tambah("B", "x", f"lama{i}", 5, "x", f"ulasan lama {i}")
    fake, diambil = ambil_palsu({"A": 1000, "B": 1000})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 20)
    assert scraper.hitung_total(["A"], status, store, terbatas=True) == 0
    assert scraper.hitung_total(["A"], status, store, terbatas=False) == 220
    scraper.tahap_ulasan(PbPalsu(), ["A"], status, store, scraper.Sesi(), terbatas=True)
    assert diambil == {"A": 20, "B": 0} and store.total() == 240      # B tidak disentuh


# ─── Kegagalan ────────────────────────────────────────────────────────────────

def test_gagal_beruntun_menghentikan_run_dengan_rapi_dan_log_ringkas(lingkungan, monkeypatch):
    daftar = ["A", "B", "C", "D"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    pb = PbPalsu()

    def selalu_gagal(*a, **k):
        raise TabUlasanGagal("tab Ulasan tidak berhasil dibuka (lihat folder debug/)")

    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", selalu_gagal)
    monkeypatch.setattr(cfg, "MAKS_GAGAL_BERUNTUN", 2)
    monkeypatch.setattr(cfg, "MAKS_PERCOBAAN_TEMPAT", 1)
    with pytest.raises(scraper.BerhentiError, match="berturut-turut"):
        jalankan_ulasan(daftar, status, store, pb)
    assert status.ambil("A")["status_scrape"] == "gagal" and status.ambil("A")["percobaan_gagal"] == 1
    assert status.ambil("C")["status_scrape"] == "belum"          # tidak dihajar terus setelah breaker menyala
    baris = open(cfg.LOG_GAGAL, encoding="utf-8").read().strip().split("\n")
    assert len(baris) == 2 and all("TabUlasanGagal" in b and len(b) < 300 for b in baris)


def test_nol_kartu_padahal_tempat_punya_ulasan_dihitung_GAGAL_bukan_selesai(lingkungan, monkeypatch):
    """Bug versi lama: 0 ulasan tetap dicatat 'selesai' sehingga tempat itu tak pernah dicoba lagi."""
    status, store = siapkan_status(["A"], total_ulasan=500), PenyimpananUlasan()
    pb = PbPalsu()
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat",
                        lambda *a, **k: HasilPanen(baru=0, terlihat=0, habis=False, alasan="kosong"))
    jalankan_ulasan(["A"], status, store, pb)
    s = status.ambil("A")
    assert s["status_scrape"] == "gagal" and s["percobaan_gagal"] == 1 and not s["habis"]
    assert "kosong_A" in pb.debug                                   # screenshot/HTML diagnosis disimpan


def test_nol_kartu_pada_tempat_tanpa_ulasan_adalah_habis_bukan_gagal(lingkungan, monkeypatch):
    status, store = siapkan_status(["A"], total_ulasan=0), PenyimpananUlasan()
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat",
                        lambda *a, **k: HasilPanen(baru=0, terlihat=0, habis=False, alasan="kosong"))
    jalankan_ulasan(["A"], status, store)
    assert status.ambil("A")["habis"] is True and status.ambil("A")["status_scrape"] == "selesai"


def test_macet_di_titik_yang_sama_dua_kali_dianggap_ujung_daftar(lingkungan, monkeypatch):
    status, store = siapkan_status(["A"]), PenyimpananUlasan()
    seri = iter([HasilPanen(baru=0, terlihat=80, alasan="macet"), HasilPanen(baru=0, terlihat=82, alasan="macet")])
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", lambda *a, **k: next(seri))
    jalankan_ulasan(["A"], status, store)                           # run pertama: macet, tidak ada kemajuan
    s = status.ambil("A")
    assert s["status_scrape"] == "sebagian" and s["habis"] is False and s["macet_di"] == 80
    jalankan_ulasan(["A"], status, store)                           # run kedua: macet lagi di titik hampir sama
    assert status.ambil("A")["habis"] is True and status.ambil("A")["status_scrape"] == "selesai"


def test_diblokir_google_menghentikan_semuanya_dan_tidak_dicatat_sebagai_gagal(lingkungan, monkeypatch):
    status, store = siapkan_status(["A", "B"]), PenyimpananUlasan()

    def captcha(*a, **k):
        raise DiblokirGoogleError("CAPTCHA")

    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", captcha)
    with pytest.raises(DiblokirGoogleError):
        jalankan_ulasan(["A", "B"], status, store)
    assert status.ambil("A")["percobaan_gagal"] == 0               # diblokir bukan salah tempatnya


# ─── Pemulihan browser ────────────────────────────────────────────────────────

def test_dengan_pemulihan_membuka_ulang_chrome_saat_sesi_mati(lingkungan):
    from selenium.common.exceptions import InvalidSessionIdException
    pb, panggilan = PbPalsu(), []

    def fungsi():
        panggilan.append(1)
        if len(panggilan) < 3:
            raise InvalidSessionIdException("invalid session id")
        return "ok"

    assert scraper.dengan_pemulihan(pb, fungsi, nama="A") == "ok"
    assert pb.diulang == 2 and len(panggilan) == 3


def test_dengan_pemulihan_menyerah_setelah_batas_dan_tidak_mengulang_error_final(lingkungan, monkeypatch):
    monkeypatch.setattr(cfg, "MAKS_PERCOBAAN_TEMPAT", 3)
    pb, n = PbPalsu(), []

    def rusak():
        n.append(1)
        raise ValueError("bug")

    with pytest.raises(ValueError):
        scraper.dengan_pemulihan(pb, rusak, nama="A")
    assert len(n) == 3 and pb.diulang == 0                          # error biasa: coba lagi, tapi TIDAK membuka ulang Chrome

    for exc in (TempatTidakDitemukan("x"), DiblokirGoogleError("x")):
        n.clear()

        def final(exc=exc):
            n.append(1)
            raise exc

        with pytest.raises(type(exc)):
            scraper.dengan_pemulihan(pb, final, nama="A")
        assert len(n) == 1                                          # tidak diulang


# ─── Mode hati-hati ───────────────────────────────────────────────────────────

def test_istirahat_otomatis_setelah_batas_ulasan_per_sesi(lingkungan, monkeypatch):
    daftar = ["A", "B", "C", "D"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    fake, _ = ambil_palsu({n: 1000 for n in daftar})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    tidur = []
    monkeypatch.setattr(scraper.time, "sleep", lambda s: tidur.append(s))
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 400)
    monkeypatch.setattr(cfg, "BATAS_ULASAN_PER_SESI", 100)
    monkeypatch.setattr(cfg, "ISTIRAHAT_MENIT", 2)
    pb = PbPalsu()
    jalankan_ulasan(daftar, status, store, pb)
    assert store.total() == 400
    assert pb.ditutup >= 3 and pb.dimulai == pb.ditutup             # tiap istirahat: browser ditutup lalu dibuka lagi
    assert tidur.count(60) == 2 * pb.ditutup                        # 2 menit per istirahat


# ─── Tahap 1 (cek tempat) ─────────────────────────────────────────────────────

def test_tahap_resolve_berhenti_dini_jika_banyak_tempat_berturut_turut_gagal_dicek(lingkungan, monkeypatch):
    """Internet putus / Google membatasi: jangan menghabiskan 17 menit memeriksa 39 tempat satu per satu."""
    daftar = [f"T{i}" for i in range(10)]
    status = StatusTempat()
    dicoba = []

    def tak_ketemu(pb, nama, ref=None):
        dicoba.append(nama)
        raise TempatTidakDitemukan("tidak terbuka")

    monkeypatch.setattr(scraper, "resolve_tempat", tak_ketemu)
    monkeypatch.setattr(cfg, "MAKS_GAGAL_BERUNTUN", 3)
    with pytest.raises(scraper.BerhentiError, match="berturut-turut tidak bisa dicek"):
        scraper.tahap_resolve(PbPalsu(), daftar, status)
    assert len(dicoba) == 3                                         # berhenti setelah 3, bukan 10
    assert status.ambil("T0")["status_scrape"] == "tidak_ditemukan"


def test_tahap_resolve_keberhasilan_mereset_hitungan_gagal_dan_mendeteksi_duplikat(lingkungan, monkeypatch):
    daftar = ["A", "B", "A2", "C"]
    status = StatusTempat()

    def fake(pb, nama, ref=None):
        if nama == "B":
            raise TempatTidakDitemukan("x")
        resmi = {"A": "Kopi A", "A2": "Kopi A", "C": "Kopi C"}[nama]          # A2 resolve ke tempat yang sama dengan A
        return {"nama_resmi": resmi, "alamat": "x", "kategori": "Kafe", "url": f"https://maps/{resmi}",
                "id_tempat": f"0x{resmi}", "rating": 4.5, "total_ulasan": 100, "status_buka": "aktif", "kemiripan": None}

    monkeypatch.setattr(scraper, "resolve_tempat", fake)
    monkeypatch.setattr(cfg, "MAKS_GAGAL_BERUNTUN", 2)
    scraper.tahap_resolve(PbPalsu(), daftar, status)               # 1 gagal di tengah tidak menyalakan rem darurat
    assert status.ambil("A2")["status_scrape"] == "duplikat" and status.ambil("A2")["duplikat_dari"] == "A"
    assert not scraper.layak_scrape(status.ambil("A2")) and scraper.layak_scrape(status.ambil("A"))
    assert status.ambil("B")["status_scrape"] == "tidak_ditemukan" and status.ambil("C")["url"]


# ─── Pangkas cafe tutup, ringkasan, ekspor ────────────────────────────────────

def test_pangkas_tutup_membuang_dari_daftar_dan_mencatat(lingkungan):
    with open(cfg.INPUT_CSV, "w", encoding="utf-8", newline="") as f:
        f.write("nama_tempat\nKopi A\nKopi B\nKopi C\n")
    st = StatusTempat()
    st.catat("Kopi A", nama_resmi="Kopi A", status_buka="aktif", url="u1")
    st.catat("Kopi B", nama_resmi="Kopi B Dago", status_buka="tutup_permanen", url="u2")
    st.catat("Kopi C", nama_resmi="Kopi C", status_buka="tutup_sementara", url="u3")
    sisa = scraper.pangkas_tutup(["Kopi A", "Kopi B", "Kopi C"], st)
    assert sisa == ["Kopi A"] and baca_daftar_input() == ["Kopi A"]
    tutup = open(cfg.FILE_TUTUP, encoding="utf-8-sig").read()
    assert "tutup_permanen" in tutup and "tutup_sementara" in tutup and "Kopi B Dago" in tutup
    assert not scraper.layak_scrape(st.ambil("Kopi B"))             # tetap dilewati walau namanya ditambahkan kembali ke daftar


def test_main_ekspor_dan_ringkasan_tidak_membuka_browser(lingkungan, monkeypatch, capsys):
    with open(cfg.INPUT_CSV, "w", encoding="utf-8", newline="") as f:
        f.write("nama_tempat\nKopi A\n")
    s = PenyimpananUlasan()
    s.tambah("Kopi A", "a", "R", 5, "x", "halo dunia")
    s.tutup()

    def terlarang(self):
        raise AssertionError("Chrome tidak boleh dibuka untuk --ekspor/--ringkasan")

    monkeypatch.setattr(Peramban, "mulai", terlarang)
    assert scraper.main(["--ekspor"]) == 0
    assert scraper.main(["--ringkasan"]) == 0
    rows = list(csv.reader(open(cfg.OUTPUT_CSV, encoding="utf-8-sig", newline=""), delimiter=cfg.CSV_SEPARATOR))
    assert rows[0] == KOLOM_CSV and len(rows) == 2
    assert "TOTAL ULASAN UNIK: 1" in capsys.readouterr().out


def test_main_tidak_membuka_browser_jika_target_sudah_tercapai(lingkungan, monkeypatch):
    with open(cfg.INPUT_CSV, "w", encoding="utf-8", newline="") as f:
        f.write("nama_tempat\nKopi A\n")
    siapkan_status(["Kopi A"])
    s = PenyimpananUlasan()
    for i in range(10):
        s.tambah("Kopi A", "a", f"R{i}", 5, "x", f"teks {i}")
    s.tutup()

    def terlarang(self):
        raise AssertionError("Chrome tidak perlu dibuka: target sudah tercapai")

    monkeypatch.setattr(Peramban, "mulai", terlarang)
    assert scraper.main(["--target", "10"]) == 0
    assert len(list(csv.reader(open(cfg.OUTPUT_CSV, encoding="utf-8-sig", newline=""), delimiter=cfg.CSV_SEPARATOR))) == 11


def test_ringkasan_teks_memuat_status_dan_total(lingkungan):
    st = siapkan_status(["A", "B"], total_ulasan=1234)
    st.catat("B", status_buka="tutup_permanen")
    s = PenyimpananUlasan()
    s.tambah("A", "x", "R", 5, "x", "satu")
    out = scraper.ringkasan_teks(["A", "B"], st, s)
    assert "TOTAL ULASAN UNIK: 1 / target 5000" in out and "1.234" in out and "tutup permanen" in out


def test_ringkasan_dengan_hanya_menampilkan_total_tempat_terpilih_bukan_total_seluruh_data(lingkungan):
    st = siapkan_status(["A", "B"])
    s = PenyimpananUlasan()
    for i in range(30):
        s.tambah("A", "x", f"r{i}", 5, "x", f"teks a {i}")
    for i in range(100):
        s.tambah("B", "x", f"r{i}", 5, "x", f"teks b {i}")
    terbatas = scraper.ringkasan_teks(["A"], st, s, terbatas=True)
    assert "ULASAN TEMPAT TERPILIH: 30 / target 5000" in terbatas and "total seluruh data: 130" in terbatas
    assert "TOTAL ULASAN UNIK" not in terbatas
    biasa = scraper.ringkasan_teks(["A", "B"], st, s)
    assert "TOTAL ULASAN UNIK: 130 / target 5000" in biasa


def test_peringatan_kualitas_data_ditampilkan_per_tempat(lingkungan, monkeypatch, capsys):
    status, store = siapkan_status(["A"]), PenyimpananUlasan()
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", lambda *a, **k: HasilPanen(
        baru=5, terlihat=10, alasan="kuota", peringatan="30 dari 30 ulasan tanpa rating"))
    jalankan_ulasan(["A"], status, store)
    keluar = capsys.readouterr().out
    assert "PERINGATAN KUALITAS DATA: 30 dari 30 ulasan tanpa rating" in keluar and "debug/" in keluar


def test_pelambatan_hanya_sementara_dan_pulih_bertahap_setelah_tempat_yang_berhasil(lingkungan, monkeypatch):
    """Temuan dari log pengguna: faktor jeda 3.0x tidak pernah turun sehingga sisa proses 3x lebih lambat."""
    daftar = ["A", "B", "C", "D"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    fake, _ = ambil_palsu({n: 1000 for n in daftar})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 400)
    pb = PbPalsu()
    pb.faktor_lambat = 3.0
    jalankan_ulasan(daftar, status, store, pb)
    assert 1.0 <= pb.faktor_lambat < 1.5                       # 3.0 -> 2.25 -> 1.69 -> 1.27 -> 1.0


def test_tempat_yang_hanya_butuh_sedikit_ulasan_tidak_dikunjungi_demi_efisiensi(lingkungan, monkeypatch):
    """Temuan log pengguna: 33 tempat dibuka satu per satu hanya untuk menambah 1 ulasan (30-60 detik per kunjungan)."""
    daftar = ["A", "B", "C"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    for n, c in {"A": 100, "B": 156, "C": 156}.items():
        for i in range(c):
            store.tambah(n, "x", f"lama{i}", 5, "x", f"lama {n} {i}")
    fake, diambil = ambil_palsu({n: 1000 for n in daftar})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 100 + 156 + 156 + 70)
    jalankan_ulasan(daftar, status, store)
    assert diambil == {"A": 61, "B": 0, "C": 0}              # B dan C hanya butuh +5: tidak dibuka


def test_berhenti_bila_sisa_target_tersebar_terlalu_tipis(lingkungan, monkeypatch):
    daftar = ["A", "B", "C"]
    status, store = siapkan_status(daftar), PenyimpananUlasan()
    for n in daftar:
        for i in range(156):
            store.tambah(n, "x", f"lama{i}", 5, "x", f"lama {n} {i}")
    fake, diambil = ambil_palsu({n: 1000 for n in daftar})
    monkeypatch.setattr(scraper, "ambil_ulasan_tempat", fake)
    monkeypatch.setattr(cfg, "TARGET_TOTAL_ULASAN", 3 * 156 + 3)
    jalankan_ulasan(daftar, status, store)
    assert diambil == {"A": 0, "B": 0, "C": 0}               # tidak ada kunjungan sia-sia untuk +1 ulasan
