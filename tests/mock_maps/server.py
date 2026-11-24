"""
Server mock "Google Maps" untuk pengujian lokal.

Google Maps tidak bisa diakses dari lingkungan CI/sandbox, jadi scraper diuji terhadap
halaman palsu (maps.html) yang meniru perilaku yang relevan:
  - pencarian: hasil langsung (direct match) atau daftar hasil
  - panel tempat: nama, rating, kategori, status 'Tutup permanen/sementara', alamat
  - tab Ikhtisar yang berisi kartu preview ulasan (jebakan: kartu ada tapi tab Ulasan BELUM terbuka)
  - tab Ulasan: lazy-load 10 kartu per scroll, spinner, 'Selengkapnya', balasan pemilik,
    ulasan tanpa teks, menu urutan
  - perilaku patologis: macet sampai di-scroll naik-turun (jiggle), macet total (hard stall),
    harus ada event wheel, virtualisasi DOM, redirect captcha.

Jalankan manual:  python tests/mock_maps/server.py   (lalu buka http://127.0.0.1:8765/maps/search/Kopi+A)
"""

import json
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps.html")

TANGGAL = ["sehari lalu", "seminggu lalu", "2 minggu lalu", "sebulan lalu", "3 bulan lalu",
           "6 bulan lalu", "setahun lalu", "2 tahun lalu", "3 tahun lalu"]
KALIMAT = [
    "Kopinya enak dan tempatnya nyaman untuk kerja.", "Pelayanan ramah tapi antrean cukup panjang saat akhir pekan.",
    "Harga sedikit mahal untuk porsinya, tapi suasananya bagus.", "Wifi kencang, colokan banyak, cocok buat ngerjain tugas.",
    "Parkiran sempit dan susah kalau bawa mobil.", "Dessert-nya juara, wajib coba signature latte.",
    "Pesanan datang lama sekali dan kopinya kurang panas.", "Interior estetik, banyak spot foto.",
]
KALIMAT_PANJANG = (" Saya datang bersama keluarga pada sore hari dan memesan beberapa menu berbeda untuk dicoba."
                   " Secara keseluruhan pengalaman kami menyenangkan, meskipun ada beberapa hal kecil yang bisa diperbaiki,"
                   " misalnya kebersihan meja dan kecepatan penyajian ketika tempat sedang ramai pengunjung.")


def buat_ulasan(idx_tempat: int, nama: str, total: int, text_ratio: float, pakai_balasan: bool = True,
                long_ratio: float = 0.3):
    rng = random.Random(1000 + idx_tempat)
    hasil = []
    rels = list(range(total))
    rng.shuffle(rels)
    for i in range(total):
        rating = rng.choices([5, 4, 3, 2, 1], weights=[55, 25, 10, 5, 5])[0]
        ada_teks = rng.random() < text_ratio
        teks = ""
        if ada_teks:
            teks = f"Ulasan nomor {i} tentang {nama}. {rng.choice(KALIMAT)}"
            if rng.random() < long_ratio:    # ulasan panjang -> tampil terpotong + tombol Selengkapnya
                teks += KALIMAT_PANJANG
        balasan = ""
        if pakai_balasan and rng.random() < 0.15:
            balasan = f"Terima kasih atas kunjungannya, balasan pemilik untuk ulasan {i}."
        tanggal = TANGGAL[min(len(TANGGAL) - 1, int(i / max(1, total) * len(TANGGAL)))]
        tanggal_raw = ("Diedit " + tanggal) if i % 11 == 7 else tanggal
        hasil.append({
            "idx": i, "rel": rels[i], "id": f"ChZDSUhN{idx_tempat:02d}{i:05d}",
            "nama": f"Reviewer {idx_tempat}-{i}", "rating": rating, "teks": teks, "balasan": balasan,
            "tanggal": tanggal, "tanggal_raw": tanggal_raw,
        })
    return hasil


class MockMaps:
    """Server HTTP kecil di thread terpisah. Gunakan sebagai context manager."""

    def __init__(self, places, config=None, port=0):
        self.config = config or {}
        self.places = []
        for n, spec in enumerate(places):
            spec = dict(spec)
            nama = spec["name"]
            total = spec.get("total", 120)                      # angka yang DITAMPILKAN di header ("(1.234)")
            jumlah = spec.get("jumlah_ulasan", total)           # ulasan yang benar-benar ada di daftar
            mock = dict(spec.get("mock", {}))
            p = {
                "name": nama, "fid": f"0x2e68e{n:04x}:0x{(n + 1) * 7919:x}",
                "address": spec.get("address", f"Jl. Contoh No.{n + 1}, Kota Bandung, Jawa Barat 4011{n}"),
                "place_id": spec.get("place_id", f"ChIJmock{n:04d}"),
                "lat": spec.get("lat", round(-6.9 - n * 0.002, 6)), "lng": spec.get("lng", round(107.6 + n * 0.002, 6)),
                "id_menuju": spec.get("id_menuju", ""),     # place_id/cid tempat ini 'salah arah' ke tempat lain (nama)
                "category": spec.get("category", "Kafe"), "status": spec.get("status", "open"),
                "closed_now": spec.get("closed_now", False),
                "rating": spec.get("rating", 4.5), "total": total, "mock": mock,
                "reviews": buat_ulasan(n, nama, jumlah, spec.get("text_ratio", 0.6),
                                       pakai_balasan=(mock.get("variant", "A") == "A" or spec.get("balasan_di_b", False)),
                                       long_ratio=spec.get("long_ratio", 0.3)),
            }
            if "fid" in spec:
                p["fid"] = spec["fid"]
            p["cid"] = str(int(p["fid"].split(":")[1], 16))
            self.places.append(p)
        self._port = port
        self._httpd = None
        self._thread = None

    # ── ground truth untuk asersi ──
    def ulasan_bersyarat_teks(self, nama):
        for p in self.places:
            if p["name"] == nama:
                return [r for r in p["reviews"] if r["teks"]]
        raise KeyError(nama)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/maps"

    def start(self):
        outer = self
        html_tpl = open(HTML_PATH, encoding="utf-8").read()
        payload = json.dumps({"places": self.places, "config": self.config}, ensure_ascii=False).replace("</", "<\\/")
        html = html_tpl.replace("__MOCK_DATA__", payload).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # senyap
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if path.startswith("/sorry/"):
                    body = ("<!doctype html><meta charset=utf-8><title>Sorry</title><h1>Lalu lintas yang tidak biasa</h1>"
                            "<p>Sistem kami mendeteksi lalu lintas yang tidak biasa dari jaringan komputer Anda.</p>").encode()
                elif path.startswith("/maps"):
                    body = html
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


if __name__ == "__main__":
    demo = [
        {"name": "Kopi A", "total": 300},
        {"name": "Kopi B Dago", "total": 80, "status": "permanent"},
        {"name": "Kopi C", "total": 60, "status": "temporary"},
    ]
    with MockMaps(demo, port=8765) as m:
        print("Mock berjalan di", m.base_url, "(Ctrl+C untuk berhenti)")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
