"""Tes lapisan data (tanpa browser): dedup, ekspor CSV, impor CSV lama, status, daftar input."""

import csv
import glob
import io
import json
import os

import pytest

import konfigurasi as cfg
from penyimpanan import (
    KOLOM_CSV,
    PenyimpananUlasan,
    StatusTempat,
    baca_csv_toleran,
    baca_daftar_input,
    catat_gagal,
    catat_tutup,
    kunci_ulasan,
    norm,
    pangkas_daftar_input,
    rapikan_spasi,
)

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── Dedup & penyimpanan ──────────────────────────────────────────────────────

def test_dedup_berdasarkan_isi_bukan_nama_kueri(lingkungan):
    s = PenyimpananUlasan()
    assert s.tambah("Kopi A", "alamat", "Budi", 5, "sebulan lalu", "Enak banget") is True
    # spasi/huruf besar berbeda tetap dianggap ulasan yang sama
    assert s.tambah("  kopi   a ", "alamat", "BUDI", 5, "x", "  enak   BANGET ") is False
    # teks kosong tidak pernah disimpan
    assert s.tambah("Kopi A", "alamat", "Siti", 4, "x", "   ") is False
    # reviewer berbeda = ulasan berbeda
    assert s.tambah("Kopi A", "alamat", "Siti", 5, "x", "Enak banget") is True
    assert s.total() == 2
    assert s.jumlah_tempat("KOPI A") == 2


def test_dedup_berdasarkan_id_ulasan_walau_teks_diedit(lingkungan):
    s = PenyimpananUlasan()
    assert s.tambah("Kopi A", "a", "Budi", 5, "x", "Teks awal", id_ulasan="ID1") is True
    assert s.tambah("Kopi A", "a", "Budi", 5, "x", "Teks sudah diedit pemilik akun", id_ulasan="ID1") is False
    assert s.total() == 1


def test_data_bertahan_dan_dedup_berlaku_setelah_muat_ulang(lingkungan):
    s = PenyimpananUlasan()
    for i in range(3):
        assert s.tambah("Kopi A", "a", f"R{i}", 5, "x", f"ulasan {i}", id_ulasan=f"ID{i}")
    s.tutup()
    s2 = PenyimpananUlasan()
    assert s2.total() == 3
    assert s2.tambah("Kopi A", "a", "R1", 5, "x", "ulasan 1", id_ulasan="ID1") is False   # duplikat setelah restart
    assert s2.tambah("Kopi A", "a", "R9", 5, "x", "ulasan 9", id_ulasan="ID9") is True


def test_baris_terakhir_terpotong_karena_crash_tidak_merusak_data(lingkungan):
    s = PenyimpananUlasan()
    s.tambah("Kopi A", "a", "R1", 5, "x", "satu")
    s.tambah("Kopi A", "a", "R2", 5, "x", "dua")
    s.tutup()
    with open(cfg.FILE_MASTER, "a", encoding="utf-8") as f:
        f.write('{"kunci": "abc", "nama_tempat": "Kopi A", "teks_ul')        # terpotong tanpa newline
    s2 = PenyimpananUlasan()
    assert s2.total() == 2
    assert s2.tambah("Kopi A", "a", "R3", 5, "x", "tiga") is True
    s2.tutup()
    assert PenyimpananUlasan().total() == 3                                   # baris baru tidak menempel di baris rusak


def test_rating_tidak_valid_menjadi_none(lingkungan):
    s = PenyimpananUlasan()
    s.tambah("Kopi A", "a", "R1", "Wahyu adi wibowo", "x", "satu")
    s.tambah("Kopi A", "a", "R2", 9, "x", "dua")
    s.tambah("Kopi A", "a", "R3", "4", "x", "tiga")
    assert [r["rating"] for r in s.rekaman] == [None, None, 4]


# ─── Ekspor CSV ───────────────────────────────────────────────────────────────

def _isi_sulit(s):
    s.tambah("Kopi A", "Jl. A No.1, Bandung", "Budi; \"Si Kopi\"", 5, "sebulan lalu",
             'Enak, murah; "mantap"\nbaris kedua\r\nbaris ketiga')
    s.tambah("Sejiwa Coffee, Kiputih", "Jl. Kiputih", "Siti", 3, "2 bulan lalu", "biasa saja")
    s.tambah("Kopi A", "Jl. A No.1, Bandung", "Andi", None, "setahun lalu", "tanpa rating")


def test_ekspor_csv_format_konsisten_dan_bisa_dibaca_balik(lingkungan):
    s = PenyimpananUlasan()
    _isi_sulit(s)
    path = s.ekspor_csv()
    mentah = open(path, "rb").read()
    assert mentah.startswith(b"\xef\xbb\xbf")                                  # UTF-8 BOM (Excel)
    teks = mentah.decode("utf-8-sig")
    assert len(teks.strip("\n").split("\n")) == 4                              # header + 3 baris: tidak ada newline liar
    rows = list(csv.reader(io.StringIO(teks, newline=""), delimiter=cfg.CSV_SEPARATOR))
    assert rows[0] == KOLOM_CSV
    assert all(len(r) == 6 for r in rows)
    assert rows[1][2] == 'Budi; "Si Kopi"'
    assert rows[1][5] == 'Enak, murah; "mantap" baris kedua baris ketiga'      # newline -> spasi, tanda baca utuh
    assert rows[1][3] == "5" and rows[3][3] == ""


def test_ekspor_csv_terbaca_pandas_seperti_biasa(lingkungan):
    pd = pytest.importorskip("pandas")
    s = PenyimpananUlasan()
    _isi_sulit(s)
    df = pd.read_csv(s.ekspor_csv(), sep=cfg.CSV_SEPARATOR, encoding="utf-8-sig")
    assert list(df.columns) == KOLOM_CSV
    assert len(df) == 3
    assert "Sejiwa Coffee, Kiputih" in set(df["nama_tempat"])


def test_ekspor_ke_nama_lain_jika_file_terkunci_excel(lingkungan, monkeypatch):
    s = PenyimpananUlasan()
    _isi_sulit(s)
    target = cfg.OUTPUT_CSV
    asli = os.replace

    def terkunci(src, dst, *a, **k):
        if dst == target:
            raise PermissionError("file dipakai Excel")
        return asli(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", terkunci)
    hasil = s.ekspor_csv()
    assert hasil != target and os.path.exists(hasil)
    assert "hasil_ulasan_" in os.path.basename(hasil)


def test_ekspor_mengelompokkan_per_tempat_sesuai_urutan_daftar(lingkungan):
    s = PenyimpananUlasan()
    s.tambah("B", "a", "r1", 5, "x", "b1")
    s.tambah("A", "a", "r1", 5, "x", "a1")
    s.tambah("B", "a", "r2", 5, "x", "b2")
    s.tambah("A", "a", "r2", 5, "x", "a2")
    rows = list(csv.reader(open(s.ekspor_csv(urutan_tempat=["A", "B"]), encoding="utf-8-sig", newline=""),
                           delimiter=cfg.CSV_SEPARATOR))
    assert [r[0] for r in rows[1:]] == ["A", "A", "B", "B"]


# ─── Impor CSV lama (format-format rusak yang benar-benar pernah muncul) ──────

def _tulis(path, isi, enc="utf-8-sig"):
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(isi)
    return str(path)


def test_baca_csv_titik_koma_dengan_trailing_separator_dan_nama_terpecah(tmp_path):
    # Persis pola hasil_ulasan.csv milik pengguna: header berakhir ';', nama bertanda koma terpecah.
    isi = (
        "nama_tempat;lokasi;nama_reviewer;rating;tanggal;teks_ulasan;\n"
        "Two Hands Full Dago;Jl. Ir. H. Juanda No.113, Bandung;Kevin;2;seminggu lalu;Pelayanan kurang;\n"
        "Sejiwa Coffee; Kiputih;Jl. Kiputih No.1A, Ciumbuleuit;Wahyu;5;sebulan lalu;Hi Guys ketemu lagi\n"
        'Sejiwa Coffee, Kiputih;Jl. Kiputih No.1A, Ciumbuleuit;Dash;3;3 bulan lalu;"Tau ""Legend"" kopishop"\n'
    )
    rows, rusak = baca_csv_toleran(_tulis(tmp_path / "a.csv", isi))
    assert rusak == 0 and len(rows) == 3
    assert rows[0]["nama_tempat"] == "Two Hands Full Dago" and rows[0]["rating"] == 2
    assert rows[1]["nama_tempat"] == "Sejiwa Coffee, Kiputih"           # nama terpecah dipulihkan
    assert rows[1]["nama_reviewer"] == "Wahyu" and rows[1]["rating"] == 5
    assert rows[2]["teks_ulasan"] == 'Tau "Legend" kopishop'


def test_baca_csv_koma_dengan_newline_di_dalam_sel(tmp_path):
    isi = ('nama_tempat,lokasi,nama_reviewer,rating,tanggal,teks_ulasan\n'
           'Blue Doors,"Jl. Alkateri No.2, Braga",Hendar,5,4 bulan lalu,"Saya datang.\n\nSuasana enak."\n')
    rows, rusak = baca_csv_toleran(_tulis(tmp_path / "b.csv", isi))
    assert rusak == 0 and len(rows) == 1
    assert "Suasana enak" in rows[0]["teks_ulasan"] and rows[0]["lokasi"] == "Jl. Alkateri No.2, Braga"


def test_baca_csv_seluruh_baris_terbungkus_satu_sel(tmp_path):
    # Hasil memuat file koma dengan separator ';' lalu menyimpan ulang (mirip hasil_ulasan_fixed_20261121_211917.csv)
    isi = ('"nama_tempat,""lokasi"",""nama_reviewer"",""rating"",""tanggal"",""teks_ulasan""";;;;;\n'
           '"Blue Doors,""Jl. Alkateri No.2, Braga"",""Hendar"",""5"",""4 bulan lalu"",""Enak banget""";;;;;\n')
    rows, rusak = baca_csv_toleran(_tulis(tmp_path / "c.csv", isi))
    assert rusak == 0 and len(rows) == 1
    assert rows[0]["nama_reviewer"] == "Hendar" and rows[0]["teks_ulasan"] == "Enak banget"


def test_baca_csv_encoding_windows_dan_file_tak_dikenal(tmp_path):
    isi = "nama_tempat;lokasi;nama_reviewer;rating;tanggal;teks_ulasan\nNimna Bookcafé;Jl. X;Ani;4;x;Nyaman\n"
    p = tmp_path / "d.csv"
    p.write_bytes(isi.encode("cp1252"))
    rows, _ = baca_csv_toleran(str(p))
    assert rows[0]["nama_tempat"] == "Nimna Bookcafé"
    asing = _tulis(tmp_path / "e.csv", "kolom_lain,x\n1,2\n")
    assert baca_csv_toleran(asing) == ([], 0)


def test_impor_csv_lama_hanya_sekali_dan_tanpa_duplikat(lingkungan):
    d = lingkungan
    isi1 = ("nama_tempat;lokasi;nama_reviewer;rating;tanggal;teks_ulasan\n"
            "Kopi A;Jl. A;Budi;5;x;satu\nKopi A;Jl. A;Budi;5;x;satu\nKopi A;Jl. A;Siti;4;x;dua\n")
    isi2 = ("nama_tempat,lokasi,nama_reviewer,rating,tanggal,teks_ulasan\n"
            "Kopi A,Jl. A,Siti,4,x,dua\nKopi B,Jl. B,Andi,3,x,tiga\n")
    _tulis(d / "hasil_ulasan.csv", isi1)
    _tulis(d / "hasil_ulasan_backup.csv", isi2)
    s = PenyimpananUlasan()
    s.impor_csv_lama_sekali()
    assert s.total() == 3 and os.path.exists(cfg.FILE_LEGACY_OK)
    _tulis(d / "hasil_ulasan_baru.csv", "nama_tempat;lokasi;nama_reviewer;rating;tanggal;teks_ulasan\nKopi C;J;Z;5;x;empat\n")
    s.impor_csv_lama_sekali()                       # sudah ditandai -> tidak impor lagi
    assert s.total() == 3


def test_impor_data_asli_repo_menghasilkan_220_ulasan_unik(lingkungan):
    berkas = sorted(glob.glob(os.path.join(RAIZ, "hasil_ulasan*.csv")))
    if len(berkas) < 5:
        pytest.skip("CSV asli pengguna tidak ada di repo")
    s = PenyimpananUlasan()
    for b in berkas:
        s.impor_csv(b)
    assert s.total() == 220
    assert s.jumlah_tempat("Sejiwa Coffee, Kiputih") == 5          # baris yang namanya terpecah ikut terselamatkan
    assert max(s.per_tempat().values()) <= 9                       # tidak ada tempat dengan > 9 ulasan: bukti scroll tak pernah jalan


# ─── Status, daftar input, catatan ────────────────────────────────────────────

def test_status_tempat_persisten(lingkungan):
    st = StatusTempat()
    st.catat("Kopi A", nama_resmi="Kopi A Dago", status_buka="aktif", total_ulasan=1234)
    st2 = StatusTempat()
    assert st2.ambil("kopi  a")["nama_resmi"] == "Kopi A Dago" and st2.ambil("Kopi A")["total_ulasan"] == 1234
    assert st2.ambil("Lain")["status_scrape"] == "belum"


def test_baca_daftar_input_cp1252_spasi_dan_nama_ganda(lingkungan):
    with open(cfg.INPUT_CSV, "wb") as f:
        f.write("nama_tempat\nTwo Hands Full\n Two Cents\n Nimna Bookcafé\ntwo   cents\n\n  \n".encode("cp1252"))
    assert baca_daftar_input() == ["Two Hands Full", "Two Cents", "Nimna Bookcafé"]


def test_pangkas_daftar_input_membuat_cadangan_sekali_dan_menulis_utf8(lingkungan):
    with open(cfg.INPUT_CSV, "wb") as f:
        f.write("nama_tempat\nKopi A\nKopi B\nNimna Bookcafé\n".encode("cp1252"))
    cadangan = pangkas_daftar_input({"Kopi B"})
    assert open(cadangan, "rb").read().startswith(b"nama_tempat\nKopi A\nKopi B")
    assert baca_daftar_input() == ["Kopi A", "Nimna Bookcafé"]
    assert "Nimna Bookcafé" in open(cfg.INPUT_CSV, encoding="utf-8").read()      # kini UTF-8 murni
    pangkas_daftar_input({"Kopi A"})                                             # panggilan kedua
    assert b"Kopi B" in open(cadangan, "rb").read()                              # cadangan asli tidak tertimpa


def test_catat_tutup_tanpa_duplikat_dan_log_gagal_satu_baris(lingkungan):
    baris = [{"nama_input": "Kopi B", "nama_resmi": "Kopi B Dago", "status": "tutup_permanen", "url": "u"}]
    catat_tutup(baris)
    catat_tutup(baris)
    isi = open(cfg.FILE_TUTUP, encoding="utf-8-sig").read().strip().split("\n")
    assert len(isi) == 2 and "tutup_permanen" in isi[1]
    catat_gagal("Kopi A", "pesan\npanjang\n\nbaris banyak " + "x" * 1000)
    baris_log = open(cfg.LOG_GAGAL, encoding="utf-8").read().strip().split("\n")
    assert len(baris_log) == 1 and baris_log[0].startswith("Kopi A | ") and len(baris_log[0]) < 400


def test_fungsi_normalisasi():
    assert rapikan_spasi(" a \n\t b ") == "a b"
    assert norm("  HaLo   DUNIA ") == "halo dunia"
    assert kunci_ulasan("A", "B", "teks  x") == kunci_ulasan(" a ", "b", "TEKS X")
    assert kunci_ulasan("A", "B", "teks") != kunci_ulasan("A", "C", "teks")
