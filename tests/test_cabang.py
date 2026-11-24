"""
Tes revisi 'cabang persis': daftar dari Wanderlog (place ID), verifikasi cabang di Maps, audit & purge master.

Bagian atas tanpa browser; bagian bawah (integrasi) memakai Chrome terhadap mock Maps yang punya DUA cabang
dengan nama hampir sama - persis kasus Wheels (Juanda vs Martadinata) yang salah diambil sebelumnya.
"""

import csv
import json

import pytest

import konfigurasi as cfg
import scraper
import wanderlog
from audit import audit_master, cocokkan
from gmaps import (
    CabangSalahError,
    TempatTidakDitemukan,
    cid_dari_fid,
    cocok_cabang,
    jarak_meter,
    koordinat_dari_url,
    resolve_tempat,
)
from penyimpanan import PenyimpananUlasan, StatusTempat, baca_daftar_input, baca_referensi, norm, pangkas_daftar_input
from test_orkestrator import PbPalsu

# ─── Identitas dari Wanderlog ─────────────────────────────────────────────────

def test_place_id_didekode_ke_id_internal_maps():
    # Dua pasangan nyata: yang pertama dicocokkan dengan URL Google Maps asli milik pengguna (Two Hands Full Dago).
    assert wanderlog.fid_dari_place_id("ChIJ0wfmo4zmaC4RC89sbOwR2Ro") == "0x2e68e68ca3e607d3:0x1ad911ec6c6ccf0b"
    assert wanderlog.fid_dari_place_id("ChIJT3xaAvznaC4RJDnZ7npthlE") == "0x2e68e7fc025a7c4f:0x51866d7aeed93924"
    assert wanderlog.fid_dari_place_id("bukan-place-id") == "" and wanderlog.fid_dari_place_id("") == ""
    assert cid_dari_fid("0x2e68e7fc025a7c4f:0x51866d7aeed93924") == str(0x51866d7aeed93924)
    assert cid_dari_fid("") == "" and cid_dari_fid("xyz") == ""


def test_kunci_alamat_tahan_perbedaan_penulisan_tapi_membedakan_cabang():
    martadinata = wanderlog.kunci_alamat("LLRE Martadinata St No.65, Citarum, Bandung Wetan, Bandung City")
    assert martadinata == wanderlog.kunci_alamat("Jl. L. L. R.E. Martadinata No.65, Citarum, Kec. Bandung Wetan")
    assert martadinata != wanderlog.kunci_alamat("Jl. Ir. H. Juanda No.256, Sekeloa, Kecamatan Coblong")
    # jalan sama, nomor beda = cabang beda
    assert wanderlog.kunci_alamat("Jl. Ir. H. Juanda No.98, X") != wanderlog.kunci_alamat("Jl. Ir. H. Juanda No.113, X")
    assert wanderlog.kunci_alamat("Jl. Lombok No.28a, A") == wanderlog.kunci_alamat("Jl. Lombok No.28A, B")


@pytest.mark.parametrize("kategori, harapan", [
    (["Coffee shop"], True), (["Cafe", "Restaurant"], True), (["Restaurant", "Bar", "Cafe", "Lounge bar"], True),
    (["Espresso bar"], True), (["Lunch restaurant", "Cafe"], True), (["Coffee roastery"], True),
    (["Western restaurant"], False), (["Asian fusion restaurant", "Restaurant"], False),
    (["Eclectic restaurant"], False), ([], False),
])
def test_kategori_cafe_atau_coffeeshop_restoran_saja_dibuang(kategori, harapan):
    assert wanderlog.kategori_cafe(kategori) is harapan


def _halaman_detail(wid, nama, place_id, lat, lng, kategori, alamat):
    return ('<script>{"mapsPlace":{"name":"%s","latitude":%s,"longitude":%s,"admissionDetails":null},'
            '"placeMetadata":{"id":%s,"name":"%s","placePageType":"visible","placeId":"%s","sources":[]},'
            '"categories":%s,"hasDetails":true,"minMinutesSpent":null,"address":"%s","rating":4.9}</script>'
            % (nama, lat, lng, wid, nama, place_id, json.dumps(kategori), alamat))


def test_baca_halaman_wanderlog_dan_pilah_kategori():
    daftar = ('<a href="/place/details/11/kopi-a">A</a><a href="/place/details/11/kopi-a#x">dup</a>'
              '<a href="/place/details/22/resto-b">B</a>')
    assert wanderlog.tautan_tempat(daftar) == {"11": "/place/details/11/kopi-a", "22": "/place/details/22/resto-b"}
    halaman = {
        "11": _halaman_detail(11, "Kopi A", "ChIJ0wfmo4zmaC4RC89sbOwR2Ro", -6.89, 107.61, ["Coffee shop"], "Jl. A No.1, Bandung"),
        "22": _halaman_detail(22, "Resto B", "ChIJT3xaAvznaC4RJDnZ7npthlE", -6.9, 107.6, ["Western restaurant"], "Jl. B No.2"),
    }
    ok, buang = wanderlog.bangun_daftar(daftar, lambda path, wid: halaman[wid], jeda=0)
    assert [d["nama_tempat"] for d in ok] == ["Kopi A"] and ok[0]["fid"] == "0x2e68e68ca3e607d3:0x1ad911ec6c6ccf0b"
    assert ok[0]["lat"] == -6.89 and ok[0]["alamat"] == "Jl. A No.1, Bandung"
    assert [d["nama_tempat"] for d in buang] == ["Resto B"] and "restoran" in buang[0]["alasan"]
    assert wanderlog.baca_detail("<html>struktur berubah</html>", "1") is None


def test_tempat_berlabel_tutup_di_wanderlog_dibuang_dari_daftar():
    def halaman(wid, nama, pid, badge=""):
        h = _halaman_detail(wid, nama, pid, -6.9, 107.6, ["Coffee shop"], f"Jl. {nama} No.1")
        return h + (f'<div class="badge CategoryAndPriceBadges__sizeLg">{badge}</div>' if badge else "")
    daftar = "".join(f'<a href="/place/details/{i}/x">x</a>' for i in (1, 2, 3, 4))
    isi = {"1": halaman(1, "Buka", "ChIJ0wfmo4zmaC4RC89sbOwR2Ro"),
           "2": halaman(2, "TutupPermanen", "ChIJT3xaAvznaC4RJDnZ7npthlE", "Permanently Closed"),
           "3": halaman(3, "TutupSementara", "ChIJ-1_7xErmaC4RtEVWbHb_QIE", "Temporarily Closed"),
           # teks 'Permanently closed' di jadwal harian ('Sunday: Permanently closed') BUKAN label tutup tempat
           "4": halaman(4, "BukaTapiHariMinggu", "ChIJIfJPAEvmaC4R5W007AZKRAE") + "<span>Sunday: Permanently closed</span>"}
    ok, buang = wanderlog.bangun_daftar(daftar, lambda path, wid: isi[wid], jeda=0)
    assert [d["nama_tempat"] for d in ok] == ["Buka", "BukaTapiHariMinggu"]
    alasan = {d["nama_tempat"]: d["alasan"] for d in buang}
    assert "tutup permanen" in alasan["TutupPermanen"] and "tutup sementara" in alasan["TutupSementara"]


def test_wanderlog_main_dari_folder_menulis_daftar_dan_yang_dibuang(tmp_path):
    (tmp_path / "list.html").write_text('<a href="/place/details/11/a">A</a><a href="/place/details/22/b">B</a>', encoding="utf-8")
    (tmp_path / "11.html").write_text(_halaman_detail(11, "Kopi A", "ChIJ0wfmo4zmaC4RC89sbOwR2Ro", -6.89, 107.61,
                                                      ["Cafe"], "Jl. A No.1, Bandung"), encoding="utf-8")
    (tmp_path / "22.html").write_text(_halaman_detail(22, "Resto B", "ChIJT3xaAvznaC4RJDnZ7npthlE", -6.9, 107.6,
                                                      ["Restaurant"], "Jl. B No.2"), encoding="utf-8")
    keluar = tmp_path / "daftar.csv"
    keluar.write_text("nama_tempat\nlama\n", encoding="utf-8")
    assert wanderlog.main(["--dari-folder", str(tmp_path), "--keluar", str(keluar)]) == 0
    rows = list(csv.DictReader(open(keluar, encoding="utf-8")))
    assert [r["nama_tempat"] for r in rows] == ["Kopi A"] and rows[0]["place_id"] == "ChIJ0wfmo4zmaC4RC89sbOwR2Ro"
    assert (tmp_path / "daftar_sebelum_wanderlog.csv").read_text(encoding="utf-8") == "nama_tempat\nlama\n"
    assert "Resto B" in (tmp_path / "daftar_dibuang_wanderlog.csv").read_text(encoding="utf-8")
    # halaman daftar kosong/terbaca gagal -> daftar lama TIDAK ditimpa
    (tmp_path / "list.html").write_text("<html></html>", encoding="utf-8")
    assert wanderlog.main(["--dari-folder", str(tmp_path), "--keluar", str(keluar)]) == 1
    assert "Kopi A" in keluar.read_text(encoding="utf-8")


# ─── Verifikasi cabang (tanpa browser) ────────────────────────────────────────

REF = {"nama": "Wheels", "alamat": "LLRE Martadinata St No.65, Citarum", "fid": "0x2e68e7fc025a7c4f:0x51866d7aeed93924",
       "lat": -6.905586, "lng": 107.617391, "place_id": "x"}
URL_RIAU = "https://maps/place/W/@-6.905586,107.617391,17z/data=!4m6!3m5!1s0x2e68e7fc025a7c4f:0x51866d7aeed93924!8m2!3d-6.905586!4d107.617391"
URL_JUANDA = "https://maps/place/W/@-6.8998,107.6101,17z/data=!4m6!3m5!1s0x2e68e6aaaaaaaaaa:0x1111!8m2!3d-6.8998!4d107.6101"


def test_koordinat_dan_jarak():
    assert koordinat_dari_url(URL_RIAU) == (-6.905586, 107.617391)
    assert koordinat_dari_url("https://maps/place/x/@-6.9,107.6,17z") == (-6.9, 107.6)
    assert koordinat_dari_url("https://maps/search/x") is None
    assert 600 < jarak_meter((-6.905586, 107.617391), (-6.8998, 107.6101)) < 1500
    assert jarak_meter((1, 1), (1, 1)) == 0


def test_cocok_cabang_id_sama_koordinat_dekat_atau_alamat_sama():
    assert cocok_cabang(REF["fid"], URL_RIAU, "", REF) == (True, "")
    # ID berubah (Google menggabung entri) tapi lokasinya sama -> tetap cabang yang sama
    assert cocok_cabang("0xbaru:0xbaru", URL_RIAU, "", REF)[0]
    # alamat sama walau koordinat URL tidak ada
    assert cocok_cabang("", "https://maps/place/x", "Jl. L. L. R.E. Martadinata No.65, Citarum", REF)[0]
    # CABANG LAIN (Juanda): ID beda, jauh, alamat beda -> ditolak dengan alasan yang jelas
    ok, alasan = cocok_cabang("0x2e68e6aaaaaaaaaa:0x1111", URL_JUANDA, "Jl. Ir. H. Juanda No.256, Sekeloa", REF)
    assert not ok and "cabang berbeda" in alasan and "m dari lokasi" in alasan


# ─── Daftar input, audit & purge ──────────────────────────────────────────────

def tulis_daftar(path, baris):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=wanderlog.KOLOM_DAFTAR, lineterminator="\n")
        w.writeheader()
        for b in baris:
            w.writerow({k: b.get(k, "") for k in wanderlog.KOLOM_DAFTAR})


BARIS = [
    {"nama_tempat": "Wheels Coffee Roasters - Heritage Lifestyle Hub (Riau)", "alamat": "LLRE Martadinata St No.65, Citarum, Bandung",
     "place_id": "ChIJT3xaAvznaC4RJDnZ7npthlE", "fid": "0x2e68e7fc025a7c4f:0x51866d7aeed93924", "lat": "-6.905586", "lng": "107.617391"},
    {"nama_tempat": "Kopi Anjis", "alamat": "Jl. Bengawan No.34, Cihapit, Bandung", "place_id": "ChIJq", "fid": "0x2e68e000:0x2",
     "lat": "-6.909154", "lng": "107.627716"},
]


WHEELS_KUNCI = norm("Wheels Coffee Roasters - Heritage Lifestyle Hub (Riau)")


def test_baca_referensi_dan_pangkas_mempertahankan_kolom(lingkungan):
    tulis_daftar(cfg.INPUT_CSV, BARIS)
    ref = baca_referensi()
    assert ref["kopi anjis"]["fid"] == "0x2e68e000:0x2" and ref["kopi anjis"]["lat"] == -6.909154
    assert "Citarum" in ref[WHEELS_KUNCI]["alamat"]      # koma dalam alamat aman
    pangkas_daftar_input({"Kopi Anjis"})
    assert baca_daftar_input() == ["Wheels Coffee Roasters - Heritage Lifestyle Hub (Riau)"]
    assert list(baca_referensi()) == [WHEELS_KUNCI]       # kolom lain tetap ada
    assert baca_referensi()[WHEELS_KUNCI]["place_id"] == "ChIJT3xaAvznaC4RJDnZ7npthlE"
    # daftar lama (hanya nama_tempat) tetap didukung
    with open(cfg.INPUT_CSV, "w", encoding="utf-8") as f:
        f.write("nama_tempat\nKopi A\n")
    assert baca_referensi() == {} and baca_daftar_input() == ["Kopi A"]


def isi_master(store):
    L_RIAU, L_JUANDA = "Jl. L. L. R.E. Martadinata No.65, Citarum, Kec. Bandung Wetan", "Jl. Ir. H. Juanda No.256, Sekeloa"
    for i in range(3):
        store.tambah("Wheels Coffee Roasters - Juanda (Dago)", L_JUANDA, f"R{i}", 5, "x", f"enak banget {i}", id_tempat="0x2e68e6aaaaaaaaaa:0x1111")
    for i in range(2):
        store.tambah("Wheels Coffee Roasters - Heritage Lifestyle Hub (Riau)", L_RIAU, f"S{i}", 4, "x", f"mantap {i}",
                     id_tempat="0x2e68e7fc025a7c4f:0x51866d7aeed93924")
    for i in range(2):      # ulasan lama tanpa id_tempat: dicocokkan lewat alamat
        store.tambah("Kopi Anjis", "Jl. Bengawan No.34, Cihapit, Kec. Bandung Wetan", f"T{i}", 3, "x", f"biasa {i}")
    store.tambah("Warung Entah", "Jl. Tidak Ada No.1", "U", 1, "x", "tidak ada di daftar")


def test_audit_membedakan_cabang_benar_dan_salah(lingkungan):
    tulis_daftar(cfg.INPUT_CSV, BARIS)
    store = PenyimpananUlasan()
    isi_master(store)
    ref = baca_referensi()
    hasil = audit_master(store.rekaman, ref)
    assert hasil["benar"] == 4 and hasil["salah"] == 4          # 3 Juanda + 1 'Warung Entah'
    salah = {k["nama"] for k in hasil["kelompok"] if not k["cocok_dengan"]}
    assert salah == {"Wheels Coffee Roasters - Juanda (Dago)", "Warung Entah"}
    assert hasil["belum_ada"] == []


def test_audit_tempat_di_daftar_yang_belum_punya_ulasan_dilaporkan(lingkungan):
    tulis_daftar(cfg.INPUT_CSV, BARIS)
    assert audit_master([], baca_referensi())["belum_ada"] == [b["nama_tempat"] for b in BARIS]


def test_cli_audit_hanya_melapor_dan_terapkan_membuang_dengan_cadangan(lingkungan, capsys):
    tulis_daftar(cfg.INPUT_CSV, BARIS)
    store = PenyimpananUlasan()
    isi_master(store)
    store.tutup()

    assert scraper.main(["--audit"]) == 0
    keluar = capsys.readouterr().out
    assert "SALAH" in keluar and "Belum ada yang diubah" in keluar
    assert PenyimpananUlasan().total() == 8                      # tidak ada yang berubah

    assert scraper.main(["--audit", "--terapkan"]) == 0
    keluar = capsys.readouterr().out
    assert "4 ulasan DIBUANG" in keluar
    sisa = PenyimpananUlasan()
    assert sisa.total() == 4 and sisa.jumlah_tempat("Wheels Coffee Roasters - Juanda (Dago)") == 0
    assert sisa.jumlah_tempat("Wheels Coffee Roasters - Heritage Lifestyle Hub (Riau)") == 2
    import glob
    cad = glob.glob(str(lingkungan / "data" / "ulasan_master.sebelum_purge_*.jsonl"))
    assert len(cad) == 1 and len(open(cad[0], encoding="utf-8").read().splitlines()) == 8     # cadangan utuh
    csv_isi = open(cfg.OUTPUT_CSV, encoding="utf-8-sig").read()
    assert "Juanda" not in csv_isi and "mantap 0" in csv_isi
    # ulasan baru tidak bisa 'hidup kembali' lewat dedup: kunci lama ikut dibuang, bukan tertinggal
    assert sisa.tambah("Wheels Coffee Roasters - Juanda (Dago)", "x", "R0", 5, "x", "enak banget 0") is True


def test_audit_membuang_ulasan_tempat_tutup_walau_cabangnya_benar(lingkungan, capsys):
    """Kopi Ireng: ada di Wanderlog (cabang benar) tetapi di Google Maps TUTUP -> ulasannya tidak boleh ikut."""
    tulis_daftar(cfg.INPUT_CSV, BARIS)
    store = PenyimpananUlasan()
    isi_master(store)
    store.tutup()
    st = StatusTempat()
    st.catat("Kopi Anjis", nama_resmi="Kopi Anjis", alamat="Jl. Bengawan No.34, Cihapit, Kec. Bandung Wetan",
             status_buka="tutup_permanen", url="u", id_tempat="0x2e68e000:0x2")
    with open(cfg.FILE_TUTUP, "w", encoding="utf-8-sig") as f:       # tempat tutup yang sudah dipangkas dari daftar
        f.write("nama_input,nama_resmi,status,url,dicek_pada\nTemu,Warung Entah,tutup_sementara,u,x\n")

    assert scraper.main(["--audit"]) == 0
    keluar = capsys.readouterr().out
    assert "TUTUP" in keluar and "tutup permanen" in keluar and "Belum ada yang diubah" in keluar
    assert PenyimpananUlasan().total() == 8

    assert scraper.main(["--audit", "--terapkan"]) == 0
    sisa = PenyimpananUlasan()
    assert sisa.jumlah_tempat("Kopi Anjis") == 0                      # tutup permanen (menurut status)
    assert sisa.jumlah_tempat("Warung Entah") == 0                    # tutup (menurut daftar_coffeeshop_tutup.csv)
    assert sisa.jumlah_tempat("Wheels Coffee Roasters - Heritage Lifestyle Hub (Riau)") == 2
    assert sisa.total() == 2


def test_cek_ulang_mengosongkan_status_buka_semua_tempat(lingkungan, monkeypatch):
    tulis_daftar(cfg.INPUT_CSV, BARIS[:1])
    st = StatusTempat()
    n = BARIS[0]["nama_tempat"]
    st.catat(n, nama_resmi="W", url="u", id_tempat="0x1:0x2", status_buka="aktif", status_scrape="selesai")
    dipanggil = []
    monkeypatch.setattr(scraper, "Peramban", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("browser dibuka")))
    with pytest.raises(RuntimeError, match="browser dibuka"):        # status kosong -> harus butuh browser (resolve ulang)
        scraper.main(["--cek-ulang", "--cek-status"])
    s = StatusTempat().ambil(n)
    assert s["url"] == "" and s["status_buka"] == "" and s["status_scrape"] == "selesai"


def test_cli_audit_tanpa_kolom_identitas_memberi_petunjuk(lingkungan, capsys):
    with open(cfg.INPUT_CSV, "w", encoding="utf-8") as f:
        f.write("nama_tempat\nKopi A\n")
    assert scraper.main(["--audit"]) == 1
    assert "python wanderlog.py" in capsys.readouterr().out


def test_selaraskan_status_mereset_tempat_yang_cabangnya_salah(lingkungan):
    ref = {"kopi anjis": {**REF, "nama": "Kopi Anjis"}}
    st = StatusTempat()
    st.catat("Kopi Anjis", nama_resmi="Kopi Anjis", url=URL_JUANDA, id_tempat="0x2e68e6aaaaaaaaaa:0x1111",
             alamat="Jl. Ir. H. Juanda No.256", status_buka="aktif", status_scrape="selesai", habis=True)
    assert scraper.selaraskan_status(["Kopi Anjis"], st, ref) == ["Kopi Anjis"]
    s = st.ambil("Kopi Anjis")
    assert s["url"] == "" and s["status_scrape"] == "belum" and not s["habis"] and s["nama_input"] == "Kopi Anjis"
    # yang sudah benar tidak disentuh
    st.catat("Kopi Anjis", url=URL_RIAU, id_tempat=REF["fid"], alamat="x")
    assert scraper.selaraskan_status(["Kopi Anjis"], st, ref) == [] and st.ambil("Kopi Anjis")["url"] == URL_RIAU


def test_tahap_resolve_salah_cabang_ditandai_dan_tidak_dicoba_terus(lingkungan, monkeypatch):
    st = StatusTempat()
    panggilan = []

    def fake(pb, nama, ref=None):
        panggilan.append((nama, ref))
        raise CabangSalahError("hanya cabang lain")

    monkeypatch.setattr(scraper, "resolve_tempat", fake)
    scraper.tahap_resolve(PbPalsu(), ["Wheels"], st, {"wheels": REF})
    assert panggilan == [("Wheels", REF)]                          # ref diteruskan ke resolve
    s = st.ambil("Wheels")
    assert s["status_scrape"] == "salah_cabang" and "cabang lain" in s["error_terakhir"]
    assert not scraper.layak_scrape({**s, "url": "u", "status_buka": "aktif"})
    assert not scraper.perlu_resolve(s)                            # tidak dicoba lagi tiap dijalankan...
    scraper.main(["--ringkasan"])                                  # (smoke) ...dan ringkasan tetap jalan


# ─── Integrasi: Chrome terhadap mock dengan dua cabang bernama mirip ──────────

pytestmark_integrasi = pytest.mark.integrasi
CEPAT = {"latency": [40, 120]}


def dua_cabang():
    """Wheels Juanda (nama persis sama dgn kueri -> pencarian nama memilihnya) & Wheels Riau (yang diminta)."""
    return [
        {"name": "Wheels Coffee Roasters", "total": 50, "address": "Jl. Ir. H. Juanda No.256, Sekeloa, Bandung",
         "fid": "0x2e68e6aaaaaaaaaa:0x1111", "place_id": "ChIJjuanda", "lat": -6.8998, "lng": 107.6101, "mock": CEPAT},
        {"name": "Wheels Coffee Roasters - Riau", "total": 50, "address": "Jl. L. L. R.E. Martadinata No.65, Bandung",
         "fid": "0x2e68e7fc025a7c4f:0x51866d7aeed93924", "place_id": "ChIJriau", "lat": -6.905586, "lng": 107.617391, "mock": CEPAT},
    ]


REF_RIAU = {"nama": "Wheels Coffee Roasters - Riau", "alamat": "LLRE Martadinata St No.65, Citarum",
            "fid": "0x2e68e7fc025a7c4f:0x51866d7aeed93924", "lat": -6.905586, "lng": 107.617391, "place_id": "ChIJriau"}


@pytest.mark.integrasi
def test_tanpa_ref_pencarian_nama_memilih_cabang_yang_salah_inilah_bug_aslinya(pb, buat_mock):
    buat_mock(dua_cabang(), {"always_list": True})
    assert resolve_tempat(pb, "Wheels Coffee Roasters")["alamat"].startswith("Jl. Ir. H. Juanda")


@pytest.mark.integrasi
def test_dengan_ref_cabang_yang_diminta_yang_dibuka(pb, buat_mock):
    buat_mock(dua_cabang(), {"always_list": True})
    info = resolve_tempat(pb, "Wheels Coffee Roasters", REF_RIAU)
    assert info["nama_resmi"] == "Wheels Coffee Roasters - Riau" and info["alamat"].startswith("Jl. L. L. R.E. Martadinata")
    assert info["id_tempat"] == REF_RIAU["fid"]


@pytest.mark.integrasi
def test_place_id_tidak_dikenal_cadangan_lewat_cid(pb, buat_mock):
    buat_mock(dua_cabang())
    ref = {**REF_RIAU, "place_id": "ChIJtidakada"}
    assert resolve_tempat(pb, "Wheels", ref)["nama_resmi"] == "Wheels Coffee Roasters - Riau"


@pytest.mark.integrasi
def test_hanya_cabang_lain_yang_bisa_dibuka_ditolak_bukan_diam_diam_dipakai(pb, buat_mock):
    places = dua_cabang()
    places[1]["id_menuju"] = "Wheels Coffee Roasters"           # place ID & cid Riau 'diarahkan' Google ke Juanda
    buat_mock(places)
    with pytest.raises(CabangSalahError, match="cabang berbeda"):
        resolve_tempat(pb, "Wheels Coffee Roasters - Riau", REF_RIAU)
    # CabangSalahError adalah TempatTidakDitemukan (alur gagal yang sudah ada tetap berlaku)
    assert issubclass(CabangSalahError, TempatTidakDitemukan)


@pytest.mark.integrasi
def test_jalur_pencarian_nama_alamat_dipakai_bila_id_gagal_dan_koordinat_menjadi_penentu(pb, buat_mock):
    buat_mock(dua_cabang(), {"always_list": True})
    # place ID & fid sama-sama tak dikenal Maps (mis. entri digabung Google) -> jatuh ke pencarian nama+alamat;
    # hasilnya diterima hanya karena koordinatnya <= 150 m dari koordinat Wanderlog.
    ref = {**REF_RIAU, "place_id": "ChIJtidakada", "fid": "0x1:0x2", "alamat": "Riau"}
    info = resolve_tempat(pb, "Wheels Coffee Roasters - Riau", ref)
    assert info["nama_resmi"] == "Wheels Coffee Roasters - Riau" and info["id_tempat"] != ref["fid"]
    # koordinat Wanderlog meleset jauh (bukan cabang ini) -> ditolak
    with pytest.raises(CabangSalahError):
        resolve_tempat(pb, "Wheels Coffee Roasters - Riau", {**ref, "lat": -6.80, "lng": 107.50})
