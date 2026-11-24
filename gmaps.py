"""
gmaps.py
========
Semua logika yang khusus untuk Google Maps:
  * mencari & memilih tempat, membaca nama/alamat/jumlah ulasan/STATUS TUTUP
  * membuka tab "Ulasan" dengan verifikasi yang benar
  * memilih urutan ulasan
  * memanen ulasan sambil scroll (inti perbaikan masalah "stuck ~250 data")

Penyebab utama data hanya ~5-10 ulasan per tempat di versi lama:
  1. Tab "Ulasan" dianggap sudah terbuka hanya karena ADA kartu ulasan di halaman, padahal tab
     "Ikhtisar" juga menampilkan beberapa kartu preview. Scroll lalu dilakukan di tab yang salah.
  2. Kartu dibaca sekali di AKHIR; jumlah kartu di DOM dipakai sebagai satu-satunya sinyal.
  3. Tidak ada pemulihan saat pemuatan macet (scroll naik-turun, muat ulang halaman).
Versi ini memanen kartu pada SETIAP langkah scroll berdasarkan data-review-id (stabil, tidak
bergantung nama class yang sering berubah), dan memakai tangga pemulihan saat macet.
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

import konfigurasi as cfg
from peramban import PESAN_LOGIN, LoginDiperlukanError, Peramban, sesi_mati

log = logging.getLogger(__name__)


# ─── Exception ────────────────────────────────────────────────────────────────

class TempatTidakDitemukan(Exception):
    """Pencarian tidak menghasilkan tempat apa pun."""


class TabUlasanGagal(Exception):
    """Halaman tempat / tab Ulasan tidak bisa dibuka."""


# ─── JavaScript ───────────────────────────────────────────────────────────────
# Semua interaksi DOM dilakukan lewat JavaScript (satu kali jalan per aksi): jauh lebih cepat
# daripada ratusan find_element, dan kebal terhadap StaleElementReferenceException.

_JS_UMUM = r"""
function norm(s) { return (s || '').replace(/\s+/g, ' ').trim(); }
function nampak(el) { return !!(el && el.getClientRects && el.getClientRects().length); }
var POLA_BALASAN = /^(tanggapan dari pemilik|response from the owner|balasan dari pemilik)/i;
function dalamBalasan(el, kartu) {
  if (el.closest && el.closest('.CDe7pd')) return true;
  var p = el.parentElement;
  while (p && p !== kartu) {
    if (POLA_BALASAN.test(norm(p.textContent).slice(0, 50))) return true;
    p = p.parentElement;
  }
  return false;
}
function idSintetis(el) {
  var n = el.querySelector('.d4r55'), t = el.querySelector('.rsqaWe');
  return 'x:' + norm(n ? n.textContent : '') + '|' + norm(t ? t.textContent : '');
}
// Kartu ulasan yang tampak: elemen TERLUAR untuk tiap data-review-id.
function kartuTerlihat() {
  var peta = {}, urut = [];
  document.querySelectorAll('[data-review-id]').forEach(function (el) {
    var id = el.getAttribute('data-review-id');
    if (!id || !nampak(el)) return;
    var lama = peta[id];
    if (!lama) { peta[id] = el; urut.push(id); }
    else if (el.contains(lama)) { peta[id] = el; }
  });
  if (!urut.length) {   // cadangan bila atribut data-review-id suatu hari dihapus Google
    document.querySelectorAll('div.jftiEf').forEach(function (el) {
      if (!nampak(el)) return;
      var id = idSintetis(el);
      if (!peta[id]) { peta[id] = el; urut.push(id); }
    });
  }
  return urut.map(function (id) { return [id, peta[id]]; });
}
// Semua leluhur kartu pertama yang benar-benar bisa di-scroll (dari yang terdalam). Jika ada beberapa
// kontainer bertingkat, semuanya di-scroll agar tidak salah memilih satu.
function daftarScroller() {
  var k = kartuTerlihat();
  var el = k.length ? k[0][1].parentElement : null, out = [];
  while (el && el !== document.body && el !== document.documentElement) {
    var cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 4) out.push(el);
    el = el.parentElement;
  }
  if (!out.length) {
    var f = document.querySelector('div.m6QErb.DxyBCb.kA9KIf.dS8AEf') || document.querySelector('div.m6QErb[role="feed"]');
    if (f) out.push(f);
  }
  return out;
}
function cariScroller() { var s = daftarScroller(); return s.length ? s[0] : null; }
function daftarExpand(kartu) {
  var hasil = [], bs = kartu.querySelectorAll('button');
  for (var i = 0; i < bs.length; i++) {
    var b = bs[i];
    if (!nampak(b) || dalamBalasan(b, kartu)) continue;
    var lab = (b.getAttribute('aria-label') || '').toLowerCase();
    var tx = norm(b.innerText || b.textContent).toLowerCase();
    if ((b.classList && b.classList.contains('w8nwRe')) || /^(lihat lebih banyak|see more)/.test(lab) ||
        lab.indexOf('selengkapnya') >= 0 || tx === 'selengkapnya' || tx === 'lihat selengkapnya' || tx === 'more') hasil.push(b);
  }
  return hasil;
}
"""

# Status halaman setelah pencarian: halaman tempat / daftar hasil / kosong.
JS_KEADAAN = _JS_UMUM + r"""
var h1 = document.querySelector('h1.DUwDvf, h1.fontHeadlineLarge') || document.querySelector('div[role="main"] h1');
var url = location.href;
var tempat = !!(h1 && norm(h1.innerText || h1.textContent) && url.indexOf('/maps/place/') >= 0);
var hasil = document.querySelectorAll('a.hfpxzc').length;
var txt = norm(document.body ? document.body.innerText : '').slice(0, 800).toLowerCase();
var kosong = !tempat && !hasil && /tidak dapat menemukan|can't find|cannot find|tidak ada hasil|no results found/.test(txt);
return {tempat: tempat, hasil: hasil, kosong: kosong, url: url};
"""

JS_KANDIDAT_HASIL = _JS_UMUM + r"""
var peta = {}, out = [];
document.querySelectorAll('a.hfpxzc, div[role="feed"] a[href*="/maps/place/"]').forEach(function (a) {
  if (!a.href || peta[a.href]) return;
  peta[a.href] = 1;
  out.push({nama: a.getAttribute('aria-label') || norm(a.textContent), href: a.href});
});
return out.slice(0, 8);
"""

JS_DETAIL_TEMPAT = _JS_UMUM + r"""
function t(el) { return el ? norm(el.innerText || el.textContent) : ''; }
var main = document.querySelector('div[role="main"]') || document.body;
var h1 = main.querySelector('h1.DUwDvf, h1.fontHeadlineLarge') || main.querySelector('h1') || document.querySelector('h1.DUwDvf');
var alamat = main.querySelector('button[data-item-id="address"] .Io6YTe') || main.querySelector('[data-item-id="address"] .Io6YTe');
var alamatEl = main.querySelector('[data-item-id="address"]');
var kat = main.querySelector('button.DkEaL, button[jsaction*="category"]');
var rate = main.querySelector('div.F7nice');
var rating = '', total = '';
if (rate) {
  var r1 = rate.querySelector('span[aria-hidden="true"]'); rating = t(r1);
  var r2 = rate.querySelector('[aria-label*="ulasan" i], [aria-label*="review" i]');
  total = r2 ? (r2.getAttribute('aria-label') || t(r2)) : '';
  if (!total) { var m = t(rate).match(/\(([\d.,]+)\)/); total = m ? m[1] : ''; }
}
if (!total) {   // cadangan: label tombol 'Ulasan lainnya (2.283)'
  var tombolTotal = Array.from(main.querySelectorAll('button, a, [role="button"]'));
  for (var q = 0; q < tombolTotal.length && !total; q++) {
    var mt = t(tombolTotal[q]).match(/^(?:ulasan lainnya|more reviews|lihat semua ulasan|see all reviews)\s*\(([\d.,]+)\)/i);
    if (mt) total = mt[1];
  }
}
return {nama: t(h1),
        alamat: t(alamat) || (alamatEl ? (alamatEl.getAttribute('aria-label') || t(alamatEl)) : ''),
        kategori: t(kat), rating: rating, total: total,
        head: norm(main.innerText || '').slice(0, 1000), url: location.href};
"""

JS_TAB_AKTIF = _JS_UMUM + r"""
var tabs = Array.from(document.querySelectorAll('[role="tab"]'));
var sel = tabs.filter(function (x) { return x.getAttribute('aria-selected') === 'true'; })[0];
var label = '';
if (sel) {
  label = norm(sel.innerText);
  if (!label) label = (sel.getAttribute('aria-label') || '').split(/\s+(?:untuk|for)\s+/i)[0];
}
var tabUlasan = !!(sel && /^(ulasan|reviews?)$/i.test(label));
var tombolUrut = !!Array.from(document.querySelectorAll('button[data-value="Sort"], button[aria-label*="Urutkan" i], button[aria-label*="Sort" i]')).filter(nampak)[0];
var urlUlasan = /!9m1!1b1/.test(location.href);
return {aktif: tabUlasan || tombolUrut || urlUlasan,
        alasan: tabUlasan ? 'tab' : (tombolUrut ? 'tombol-urutkan' : (urlUlasan ? 'url' : '')),
        tab_terpilih: label, jumlah_tab: tabs.length};
"""

# Kandidat elemen untuk membuka tab Ulasan, dari yang paling pasti ke yang paling longgar.
# Tombol DI DALAM kartu ulasan (mis. 'Lihat lebih banyak ulasan ...') sengaja diabaikan.
JS_KANDIDAT_TAB = _JS_UMUM + r"""
function dalamKartu(el) { return !!(el.closest && el.closest('[data-review-id], .jftiEf')); }
function labelTab(el) { return norm(el.innerText) || (el.getAttribute('aria-label') || '').split(/\s+(?:untuk|for)\s+/i)[0]; }
var out = [], dipakai = [];
function tambah(el, s) { if (dipakai.indexOf(el) < 0 && nampak(el)) { dipakai.push(el); out.push({el: el, s: s}); } }
Array.from(document.querySelectorAll('[role="tab"]')).forEach(function (el) {
  if (/^(ulasan|reviews?)$/i.test(labelTab(el))) tambah(el, 'role=tab');
});
var idx1 = document.querySelector('button[data-tab-index="1"]');
if (idx1 && !/^(ikhtisar|overview|tentang|about)$/i.test(labelTab(idx1))) tambah(idx1, 'data-tab-index=1');
Array.from(document.querySelectorAll('button, [role="button"]')).forEach(function (el) {
  if (dalamKartu(el)) return;
  var lab = (el.getAttribute('aria-label') || '');
  if (/^[\d.,]+\s*(ulasan|reviews?)\b/i.test(lab) || /\b[\d.,]+\s*(ulasan|reviews?)$/i.test(lab)) tambah(el, 'ringkasan-rating');
});
Array.from(document.querySelectorAll('button, a, [role="button"]')).forEach(function (el) {
  if (dalamKartu(el)) return;
  var tx = norm(el.innerText || el.getAttribute('aria-label') || '');
  if (/^(lihat semua ulasan|ulasan lainnya|more reviews|see all reviews|semua ulasan)/i.test(tx)) tambah(el, 'ulasan-lainnya');
});
return out;
"""

JS_TOMBOL_URUT = _JS_UMUM + r"""
return Array.from(document.querySelectorAll('button[data-value="Sort"], button[aria-label*="Urutkan" i], button[aria-label*="Sort" i], [role="button"][aria-label*="Urutkan" i]')).filter(nampak)[0] || null;
"""

JS_ITEM_MENU = _JS_UMUM + r"""
return Array.from(document.querySelectorAll('[role="menuitemradio"], [role="menu"] [role="menuitem"], [role="menu"] li'))
  .filter(nampak).map(function (el) { return {el: el, teks: norm(el.innerText || el.textContent)}; });
"""

# Dinding login Google: modal "Login untuk menikmati fitur terbaik dari Google Maps" (tombol 'Tutup' dan 'Login'),
# muncul saat pengunjung tanpa login mencoba membuka ulasan lengkap.
JS_DINDING_LOGIN = _JS_UMUM + r"""
var POLA_TOMBOL = /^(login|log in|sign in|masuk)$/i;
var teks = norm(document.body ? document.body.innerText : '').slice(0, 30000);
var polaTeks = /login untuk menikmati|masuk untuk menikmati|sign in to (get|enjoy|unlock)/i.test(teks);
var dialog = Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')).filter(nampak);
var struktur = dialog.some(function (d) {
  var t = norm(d.innerText || '');
  var adaLogin = Array.from(d.querySelectorAll('button, a, [role="button"]')).some(function (b) {
    return POLA_TOMBOL.test(norm(b.innerText || b.getAttribute('aria-label') || ''));
  });
  return adaLogin && t.length < 1500;
});
return {ada: polaTeks || struktur, via: polaTeks ? 'teks' : (struktur ? 'dialog' : '')};
"""

JS_TUTUP_DINDING = _JS_UMUM + r"""
var POLA = /^(tutup|close|nanti saja|not now|no thanks|tidak, terima kasih)$/i;
var dialog = Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"]')).filter(nampak);
for (var i = 0; i < dialog.length; i++) {
  var bs = Array.from(dialog[i].querySelectorAll('button, a, [role="button"]'));
  for (var j = 0; j < bs.length; j++) {
    var tx = norm(bs[j].innerText || bs[j].getAttribute('aria-label') || '');
    if (POLA.test(tx)) { bs[j].click(); return tx; }
  }
}
return '';
"""

# Apakah Chrome scraper sedang login ke Google? 'masuk' | 'keluar' | 'tidak_jelas' (heuristik; hanya untuk peringatan).
JS_STATUS_LOGIN = _JS_UMUM + r"""
var els = Array.from(document.querySelectorAll('a, button, [role="button"]')).filter(nampak);
var masuk = els.some(function (el) {
  return /^(akun google|google account)\b/i.test(el.getAttribute('aria-label') || '') ||
         /accounts\.google\.com\/SignOutOptions/.test(el.getAttribute('href') || '');
});
var keluar = els.some(function (el) {
  if (el.closest('[role="dialog"], [aria-modal="true"]')) return false;
  return /^(login|log in|sign in|masuk)$/i.test(norm(el.innerText || '')) ||
         /accounts\.google\.com\/ServiceLogin/.test(el.getAttribute('href') || '');
});
return masuk ? 'masuk' : (keluar ? 'keluar' : 'tidak_jelas');
"""

JS_IDS = _JS_UMUM + r"""
var k = kartuTerlihat();
var sp = document.querySelector('[role="progressbar"], .qjESne');
var sc = cariScroller();
return {ids: k.map(function (x) { return x[0]; }), spinner: nampak(sp), ada_scroller: !!sc, sh: sc ? sc.scrollHeight : 0};
"""

JS_SCROLL_BAWAH = _JS_UMUM + r"""
var k = kartuTerlihat();
if (k.length) { try { k[k.length - 1][1].scrollIntoView({block: 'end'}); } catch (e) {} }   // kartu terakhir ke dasar layar
var s = daftarScroller();
if (!s.length) return null;
var sc = s[0], sebelum = sc.scrollTop;
s.forEach(function (el) { el.scrollTop = el.scrollHeight; });                                // lalu benar-benar ke dasar daftar
var r = sc.getBoundingClientRect();
var y = Math.max(r.top + 30, Math.min(window.innerHeight - 30, r.top + r.height / 2));
return {sebelum: sebelum, sesudah: sc.scrollTop, sh: sc.scrollHeight, ch: sc.clientHeight, x: r.left + r.width / 2, y: y};
"""

JS_SCROLL_NAIK = _JS_UMUM + r"""
var s = daftarScroller();
if (!s.length) return null;
var sc = s[0], px = arguments[0];
s.forEach(function (el) { el.scrollTop = Math.max(0, el.scrollTop - px); });
var r = sc.getBoundingClientRect();
var y = Math.max(r.top + 30, Math.min(window.innerHeight - 30, r.top + r.height / 2));
return {sebelum: 0, sesudah: sc.scrollTop, sh: sc.scrollHeight, ch: sc.clientHeight, x: r.left + r.width / 2, y: y};
"""

JS_EXPAND = _JS_UMUM + r"""
var n = 0;
kartuTerlihat().forEach(function (x) {
  if (x[1].getAttribute('data-sc-ok') === x[0]) return;        // sudah dipanen utuh: tidak perlu diperiksa lagi
  daftarExpand(x[1]).forEach(function (b) { try { b.click(); n++; } catch (e) {} });
});
return n;
"""

# Memanen kartu yang BELUM pernah dipanen (ditandai atribut data-sc-ok=<id> pada elemennya).
# Kartu yang masih terpotong ('Selengkapnya') ditunda sampai 3x pengecekan.
JS_PANEN = _JS_UMUM + r"""
var daftar = kartuTerlihat();
var kelasDikenal = daftar.some(function (x) { return !!x[1].querySelector('.d4r55, .kvMYJc, .rsqaWe, .wiI7pd, .MyEned'); });
var SAMPAH = /local guide|\bfoto\b|\bphotos?\b|^\d+\s*(ulasan|reviews?)$/i;

function ekstrak(kartu, id) {
  var o = {id: id, nama: '', rating: null, tanggal: '', teks: ''};
  var n = kartu.querySelector('.d4r55');
  o.nama = n ? norm(n.innerText || n.textContent) : '';
  if (!o.nama) o.nama = norm(kartu.getAttribute('aria-label') || '');
  if (!o.nama) {
    var a = kartu.querySelector('a[href*="/maps/contrib/"], button[data-href*="/maps/contrib/"]');
    if (a) o.nama = norm(a.getAttribute('aria-label') || a.innerText || a.textContent).replace(/^(foto|photo)\s+/i, '');
  }
  var rt = kartu.querySelector('.kvMYJc[aria-label]');
  if (rt) { var d0 = (rt.getAttribute('aria-label') || '').match(/\d+/); if (d0) o.rating = parseInt(d0[0], 10); }
  if (o.rating === null) {      // class berubah: cari elemen gambar berlabel bintang, urutan kata bebas ("5 bintang" / "Bintang 5")
    var imgs = kartu.querySelectorAll('[role="img"][aria-label]');
    for (var i = 0; i < imgs.length; i++) {
      if (dalamBalasan(imgs[i], kartu)) continue;
      var lab = imgs[i].getAttribute('aria-label') || '';
      var m = lab.match(/(\d+(?:[.,]\d+)?)\s*(?:bintang|stars?)/i) || lab.match(/(?:bintang|stars?)\s*(\d+(?:[.,]\d+)?)/i);
      if (m) { o.rating = Math.round(parseFloat(m[1].replace(',', '.'))); break; }
    }
  }
  var td = kartu.querySelector('.rsqaWe');
  o.tanggal = td ? norm(td.innerText || td.textContent) : '';
  if (!o.tanggal) {
    var mm = norm(kartu.innerText || kartu.textContent).match(/(?:diedit\s+)?(?:\d+\s*|se)(?:menit|jam|hari|minggu|bulan|tahun)\s+lalu|(?:edited\s+)?(?:a|an|\d+)\s+(?:minute|hour|day|week|month|year)s?\s+ago/i);
    if (mm) o.tanggal = mm[0];
  }
  var teks = '';
  var spans = kartu.querySelectorAll('span.wiI7pd');
  for (var s = 0; s < spans.length && !teks; s++) {
    if (!dalamBalasan(spans[s], kartu)) teks = norm(spans[s].innerText || spans[s].textContent);
  }
  if (!teks) {
    var boxes = kartu.querySelectorAll('div.MyEned, span.MyEned, div.review-full-text');
    for (var b = 0; b < boxes.length && !teks; b++) {
      if (dalamBalasan(boxes[b], kartu)) continue;
      var klon = boxes[b].cloneNode(true);
      klon.querySelectorAll('button').forEach(function (x) { x.remove(); });
      teks = norm(klon.textContent);
    }
  }
  if (!teks && !kelasDikenal) {   // class Google berubah semua: ambil blok teks daun terpanjang yang bukan nama/tanggal/balasan
    var terbaik = '';
    kartu.querySelectorAll('span, div').forEach(function (e) {
      if (e.children.length > 0) return;
      if ((e.closest && e.closest('button, a, [role="img"]')) || dalamBalasan(e, kartu)) return;
      var tt = norm(e.textContent);
      if (tt.length <= terbaik.length || tt === o.nama || /\b(lalu|ago)$/i.test(tt) || (tt.length < 80 && SAMPAH.test(tt))) return;
      terbaik = tt;
    });
    teks = terbaik;
  }
  o.teks = teks;
  return o;
}

var hasil = [];
daftar.forEach(function (x) {
  var id = x[0], kartu = x[1];
  if (kartu.getAttribute('data-sc-ok') === id) return;
  var terpotong = daftarExpand(kartu).length > 0;
  if (terpotong) {
    var n = parseInt(kartu.getAttribute('data-sc-coba') || '0', 10) + 1;
    kartu.setAttribute('data-sc-coba', String(n));
    if (n < 3) return;                      // tunggu tombol 'Selengkapnya' selesai membuka teks
  }
  var o = ekstrak(kartu, id);
  o.terpotong = terpotong;
  kartu.setAttribute('data-sc-ok', id);
  hasil.push(o);
});
return {kartu: hasil, ids: daftar.map(function (x) { return x[0]; }), kelas_dikenal: kelasDikenal};
"""


# ─── Utilitas ─────────────────────────────────────────────────────────────────

POLA_URUTAN = {
    "relevan": r"relevan|relevant",
    "terbaru": r"terbaru|newest",
    "tertinggi": r"tertinggi|highest",
    "terendah": r"terendah|lowest",
}
_POLA_TUTUP_PERMANEN = re.compile(r"tutup permanen|permanently closed", re.I)
_POLA_TUTUP_SEMENTARA = re.compile(r"tutup sementara|temporarily closed", re.I)


def deteksi_status_buka(teks_header: str) -> str:
    """'tutup_permanen' | 'tutup_sementara' | 'aktif' dari teks bagian atas panel tempat."""
    if _POLA_TUTUP_PERMANEN.search(teks_header or ""):
        return "tutup_permanen"
    if _POLA_TUTUP_SEMENTARA.search(teks_header or ""):
        return "tutup_sementara"
    return "aktif"


def angka_dari_teks(teks) -> int | None:
    """
    '6.179 ulasan' -> 6179 | '(7.863)' -> 7863 | '1,234 reviews' -> 1234 |
    '1,2 rb ulasan' -> 1200 | '1.2K reviews' -> 1200 | '2,5 jt' -> 2500000 | tanpa angka -> None.
    (Singkatan harus dikenali: '1,2 rb' dibaca naif sebagai 12 akan membuat scraper mengira daftar sudah habis.)
    """
    m = re.search(r"(\d[\d.,]*)\s*(rb|ribu|k|jt|juta|m)?(?![a-z])", str(teks or "").lower())
    if not m:
        return None
    angka, sufiks = m.group(1), m.group(2)
    if sufiks:
        try:
            nilai = float(angka.replace(",", "."))
        except ValueError:
            return None
        return int(round(nilai * (1_000 if sufiks in ("rb", "ribu", "k") else 1_000_000)))
    digit = re.sub(r"\D", "", angka)
    return int(digit) if digit else None


def _norm_nama(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def skor_kemiripan(kueri: str, nama: str) -> float:
    """0..1: seberapa mirip nama hasil pencarian dengan nama di daftar input."""
    a, b = _norm_nama(kueri), _norm_nama(nama)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = a.split(), b.split()
    if set(ta) <= set(tb):                       # semua kata input termuat; makin sedikit kata ekstra makin tinggi
        return 0.85 + 0.1 * len(ta) / len(tb)
    return min(0.84, SequenceMatcher(None, a, b).ratio())


def url_pencarian(nama: str) -> str:
    kueri = f"{nama} {cfg.LOKASI}".strip()
    return f"{cfg.MAPS_BASE}/search/{quote_plus(kueri)}?hl={cfg.BAHASA}"


def id_tempat_dari_url(url: str) -> str:
    m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url or "")
    return m.group(1) if m else ""


def bersihkan_tanggal(t: str) -> str:
    return re.sub(r"^(diedit|edited)\s+", "", (t or "").strip(), flags=re.I)


def _tunggu(kondisi, timeout: float, jeda: float = 0.4) -> bool:
    mulai = time.time()
    while True:
        if kondisi():
            return True
        if time.time() - mulai >= timeout:
            return False
        time.sleep(jeda)


def _klik(pb: Peramban, el) -> bool:
    """Klik biasa; jika gagal (tertutup elemen lain / belum terlihat) pakai klik JavaScript."""
    try:
        el.click()
        return True
    except WebDriverException as e:
        if sesi_mati(e):
            raise
    try:
        pb.js("arguments[0].click();", el)
        return True
    except WebDriverException as e:
        if sesi_mati(e):
            raise
        return False


def _roda(pb: Peramban, info: dict | None, dy: int):
    """Event roda mouse sungguhan (CDP) di tengah kontainer ulasan; memicu lazy-load yang menunggu 'wheel'."""
    if not info:
        return
    try:
        pb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": info["x"], "y": info["y"], "deltaX": 0, "deltaY": dy, "pointerType": "mouse"})
    except WebDriverException as e:
        if sesi_mati(e):
            raise


# ─── Mencari & membaca tempat ─────────────────────────────────────────────────

def _tunggu_keadaan(pb: Peramban, timeout: float = 18):
    """Menunggu hasil pencarian: ('tempat'|'daftar'|'kosong'|'timeout', info)."""
    mulai, terakhir, putaran = time.time(), None, 0
    while time.time() - mulai < timeout:
        st = pb.js(JS_KEADAAN)
        terakhir = st
        if st["tempat"]:
            return "tempat", st
        if st["kosong"]:
            return "kosong", st
        if st["hasil"] > 0:
            time.sleep(1.0)                      # beri kesempatan redirect ke halaman tempat (direct match)
            st2 = pb.js(JS_KEADAAN)
            return ("tempat", st2) if st2["tempat"] else ("daftar", st2)
        putaran += 1
        if putaran % 6 == 0:
            pb.periksa_halangan()
        time.sleep(0.5)
    return "timeout", terakhir


def resolve_tempat(pb: Peramban, nama_input: str) -> dict:
    """
    Mencari tempat di Google Maps dan membaca detailnya.
    Returns dict: nama_resmi, alamat, kategori, url, id_tempat, rating, total_ulasan,
                  status_buka ('aktif'|'tutup_permanen'|'tutup_sementara'), kemiripan.
    Raises TempatTidakDitemukan.
    """
    pb.buka(url_pencarian(nama_input))
    keadaan, _ = _tunggu_keadaan(pb)
    kemiripan = None

    if keadaan == "daftar":
        kandidat = pb.js(JS_KANDIDAT_HASIL)
        skor = [(skor_kemiripan(nama_input, k["nama"]), -i, k) for i, k in enumerate(kandidat)]
        skor.sort(reverse=True, key=lambda x: (x[0], x[1]))
        kemiripan, _, pilih = skor[0]
        log.info(f"  Daftar hasil ({len(kandidat)}): memilih '{pilih['nama']}' (kemiripan {kemiripan:.2f})")
        if kemiripan < 0.5:
            log.warning(f"  PERHATIAN: nama hasil kurang mirip dengan '{nama_input}'. Periksa manual di status_tempat.json")
        pb.buka(pilih["href"])
        keadaan, _ = _tunggu_keadaan(pb)
    elif keadaan == "tempat":
        log.info("  Google Maps langsung membuka halaman tempat (direct match)")

    if keadaan == "kosong":
        raise TempatTidakDitemukan(f"Google Maps tidak menemukan '{nama_input}'")
    if keadaan != "tempat":
        pb.simpan_debug(f"resolve_{nama_input}")
        raise TempatTidakDitemukan(f"Halaman tempat '{nama_input}' tidak terbuka (keadaan: {keadaan})")

    # Panel detail kadang selesai dirender beberapa saat setelah <h1> muncul.
    detail = None
    for _ in range(10):
        detail = pb.js(JS_DETAIL_TEMPAT)
        if detail["nama"] and (detail["alamat"] or detail["total"]):
            break
        time.sleep(0.6)
    if not detail or not detail["nama"]:
        raise TempatTidakDitemukan(f"Nama tempat tidak terbaca untuk '{nama_input}'")

    alamat = re.sub(r"^(alamat|address):\s*", "", detail["alamat"] or "", flags=re.I).strip()
    try:
        rating = float(detail["rating"].replace(",", ".")) if detail["rating"] else None
    except ValueError:
        rating = None
    info = {
        "nama_resmi": detail["nama"], "alamat": alamat or cfg.LOKASI, "kategori": detail["kategori"],
        "url": detail["url"], "id_tempat": id_tempat_dari_url(detail["url"]),
        "rating": rating, "total_ulasan": angka_dari_teks(detail["total"]),
        "status_buka": deteksi_status_buka(detail["head"]), "kemiripan": kemiripan,
    }
    log.info(f"  Tempat: {info['nama_resmi']} | {info['kategori'] or '-'} | rating {rating} | "
             f"{info['total_ulasan']} ulasan | status: {info['status_buka']}")
    return info


# ─── Membuka tab Ulasan & memilih urutan ──────────────────────────────────────

def ada_dinding_login(pb: Peramban) -> bool:
    """True jika modal 'Login untuk menikmati fitur terbaik dari Google Maps' sedang tampil."""
    return bool(pb.js(JS_DINDING_LOGIN)["ada"])


def tutup_dinding_login(pb: Peramban) -> bool:
    """Menutup modal login (tombol 'Tutup'; cadangan: Escape). Returns True jika modal sudah hilang."""
    pb.js(JS_TUTUP_DINDING)
    time.sleep(0.8)
    if ada_dinding_login(pb):
        try:
            ActionChains(pb.driver).send_keys(Keys.ESCAPE).perform()
        except WebDriverException as e:
            if sesi_mati(e):
                raise
        time.sleep(0.8)
    return not ada_dinding_login(pb)


def status_login(pb: Peramban) -> str:
    """'masuk' | 'keluar' | 'tidak_jelas' (heuristik dari tombol 'Login' / tombol akun di pojok kanan atas)."""
    return pb.js(JS_STATUS_LOGIN)


def _dinding_login_terdeteksi(pb: Peramban, konteks: str):
    """
    Jika modal login tampil: coba tutup. Bila setelah ditutup tab Ulasan tetap tidak terbuka berarti
    ulasan lengkap memang terkunci di balik login -> LoginDiperlukanError (tidak ada gunanya mencoba lagi).
    """
    if not ada_dinding_login(pb):
        return
    log.warning(f"  Dinding login Google terdeteksi ({konteks})")
    pb.simpan_debug(f"dinding_login_{konteks}")
    tutup_dinding_login(pb)
    if not pb.js(JS_TAB_AKTIF)["aktif"]:
        raise LoginDiperlukanError(PESAN_LOGIN)


def _klik_tab_ulasan(pb: Peramban) -> bool:
    if pb.js(JS_TAB_AKTIF)["aktif"] and not ada_dinding_login(pb):
        return True
    for _ in range(3):
        for k in pb.js(JS_KANDIDAT_TAB):
            if not _klik(pb, k["el"]):
                continue
            log.info(f"  Klik tab Ulasan via: {k['s']}")
            if _tunggu(lambda: pb.js(JS_TAB_AKTIF)["aktif"] or ada_dinding_login(pb), 4.0):
                _dinding_login_terdeteksi(pb, "tab_ulasan")      # raise bila ulasan terkunci di balik login
                st = pb.js(JS_TAB_AKTIF)
                if st["aktif"]:
                    log.info(f"  Tab Ulasan terbuka (terverifikasi lewat: {st['alasan']})")
                    return True
        pb.tidur(1.5, 2.5)
    return False


def pilih_urutan(pb: Peramban, mode: str) -> bool:
    """Memilih urutan ulasan. 'relevan' = bawaan Maps (tidak perlu klik). Best-effort: gagal tidak fatal."""
    if mode == "relevan" or mode not in POLA_URUTAN:
        return True
    pola = re.compile(POLA_URUTAN[mode], re.I)
    tombol = pb.js(JS_TOMBOL_URUT)
    if not tombol:
        log.warning(f"  Tombol urutkan tidak ditemukan; memakai urutan bawaan Maps (diminta: {mode})")
        return False
    id_awal = pb.js(JS_IDS)["ids"][:1]
    _klik(pb, tombol)
    items: list = []

    def menu_terbuka() -> bool:
        items[:] = pb.js(JS_ITEM_MENU)
        return bool(items)

    _tunggu(menu_terbuka, 3.0, 0.3)
    pilih = next((i for i in items if pola.search(i["teks"])), None)
    if not pilih:
        log.warning(f"  Opsi urutan '{mode}' tidak ada di menu ({[i['teks'] for i in items]})")
        pb.simpan_debug("menu_urutan")
        return False
    _klik(pb, pilih["el"])
    _tunggu(lambda: pb.js(JS_IDS)["ids"][:1] != id_awal, 6.0)
    log.info(f"  Ulasan diurutkan: {mode}")
    time.sleep(0.8)
    return True


def buka_ulasan(pb: Peramban, info: dict, mode: str):
    """Membuka halaman tempat, tab Ulasan (terverifikasi), lalu memilih urutan."""
    pb.buka(info["url"])
    keadaan, _ = _tunggu_keadaan(pb)
    if keadaan != "tempat":
        pb.simpan_debug(f"buka_{info['nama_resmi']}")
        raise TabUlasanGagal(f"halaman tempat tidak terbuka (keadaan: {keadaan})")
    pb.tidur(*cfg.JEDA_BUKA_HALAMAN)
    if ada_dinding_login(pb):                       # modal yang muncul sendiri saat halaman dibuka tidak boleh menghalangi
        tutup_dinding_login(pb)
    if not _klik_tab_ulasan(pb):
        pb.simpan_debug(f"tab_ulasan_{info['nama_resmi']}")
        raise TabUlasanGagal("tab Ulasan tidak berhasil dibuka (lihat folder debug/)")
    _tunggu(lambda: len(pb.js(JS_IDS)["ids"]) > 0, 12.0)    # kartu pertama
    pilih_urutan(pb, mode)


# ─── Memanen ulasan ───────────────────────────────────────────────────────────

@dataclass
class HasilPanen:
    baru: int = 0           # ulasan bersyarat teks yang benar-benar baru tersimpan
    terlihat: int = 0       # jumlah kartu (termasuk tanpa teks) yang pernah terlihat
    habis: bool = False     # True: ujung daftar ulasan tercapai
    alasan: str = ""        # 'kuota' | 'habis' | 'macet' | 'waktu' | 'langkah' | 'kosong'


def _goyang(pb: Peramban):
    """Scroll naik lalu turun lagi: membangunkan pemuat ulasan yang 'tertidur'."""
    _roda(pb, pb.js(JS_SCROLL_NAIK, 900), -300)
    time.sleep(0.8)
    _roda(pb, pb.js(JS_SCROLL_BAWAH), 300)


def _tunggu_kartu_baru(pb: Peramban, ids_dom_sebelum: set, terlihat: set, timeout: float):
    """Menunggu kartu bertambah (jumlah di DOM naik ATAU muncul id yang belum pernah terlihat)."""
    mulai, ada_spinner = time.time(), False
    while True:
        st = pb.js(JS_IDS)
        ids = set(st["ids"])
        if (ids - terlihat) or len(ids) > len(ids_dom_sebelum):
            return True
        ada_spinner = ada_spinner or st["spinner"]
        if time.time() - mulai > timeout + (8 if ada_spinner else 0):
            return False
        time.sleep(0.4)


def panen_ulasan(pb: Peramban, store, info: dict, kuota: int, mode: str, deadline: float) -> HasilPanen:
    """
    Scroll daftar ulasan yang sudah terbuka sambil memanen kartu di setiap langkah.
    Berhenti jika: kuota ulasan baru terpenuhi, ujung daftar tercapai, macet, atau melewati batas waktu/langkah.
    """
    nama = info["nama_resmi"]
    total_maps = info.get("total_ulasan")
    terlihat: set = set()
    h = HasilPanen()
    langkah = macet = reload_n = 0
    goyang_dulu = False         # pemuat terbukti butuh digoyang -> lakukan langsung tanpa menunggu timeout

    while True:
        # 1) buka 'Selengkapnya', lalu panen kartu baru
        if pb.js(JS_EXPAND):
            time.sleep(0.3)
        data = pb.js(JS_PANEN)
        ids_dom = set(data["ids"])
        if ids_dom - terlihat:                   # ada kartu yang belum pernah terlihat = ada kemajuan, dari cara apa pun
            macet = 0
        terlihat |= ids_dom
        for k in data["kartu"]:
            if h.baru >= kuota:
                break                            # kuota tepat terpenuhi; sisa kartu batch ini diambil di ronde berikutnya
            if not k["teks"]:
                continue
            ok = store.tambah(nama, info["alamat"], k["nama"] or "Anonim", k["rating"],
                              bersihkan_tanggal(k["tanggal"]), k["teks"], id_ulasan=k["id"],
                              sumber="scrape", urutan=mode, id_tempat=info.get("id_tempat", ""))
            if ok:
                h.baru += 1
        h.terlihat = len(terlihat)

        # 2) kondisi berhenti
        if h.baru >= kuota:
            h.alasan = "kuota"
            break
        if time.time() > deadline:
            h.alasan = "waktu"
            log.warning(f"  Batas waktu per tempat ({cfg.MAKS_MENIT_PER_TEMPAT} menit) tercapai")
            break
        if langkah >= cfg.MAKS_LANGKAH_SCROLL:
            h.alasan = "langkah"
            break
        if not ids_dom and langkah == 0:
            h.alasan = "kosong"
            break

        # 3) scroll & tunggu kartu baru
        langkah += 1
        info_scroll = pb.js(JS_SCROLL_BAWAH)
        _roda(pb, info_scroll, 400)
        if goyang_dulu and info_scroll is not None:
            _goyang(pb)
        tumbuh = info_scroll is not None and _tunggu_kartu_baru(pb, ids_dom, terlihat, cfg.TIMEOUT_MUAT_BATCH)
        if tumbuh:
            if langkah % 5 == 0:
                log.info(f"  Scroll {langkah}: {len(terlihat)} kartu terlihat, +{h.baru} ulasan baru bersyarat teks")
            pb.tidur(*cfg.JEDA_SCROLL)
            if langkah % 12 == 0:
                pb.tidur(3, 6)                   # jeda 'membaca' sesekali agar pola tidak terlalu teratur
            continue

        # 4) tidak ada kartu baru -> tangga pemulihan
        macet += 1
        log.info(f"  Scroll {langkah}: tidak ada kartu baru ({macet}/{cfg.MAKS_MACET}), "
                 f"{len(terlihat)} kartu terlihat" + (f" dari {total_maps}" if total_maps else ""))
        pb.periksa_halangan()
        if ada_dinding_login(pb):                # Google meminta login di tengah pemuatan ulasan
            pb.simpan_debug(f"dinding_login_{nama}")
            tutup_dinding_login(pb)
            raise LoginDiperlukanError(PESAN_LOGIN)
        if total_maps and len(terlihat) >= total_maps and macet >= 2:     # dua kali macet = benar-benar ujung daftar
            h.habis, h.alasan = True, "habis"
            break
        if macet < cfg.MAKS_MACET:
            _goyang(pb)
            if _tunggu_kartu_baru(pb, ids_dom, terlihat, cfg.TIMEOUT_MUAT_BATCH):
                macet = 0                        # menggoyang berhasil membangunkan pemuat
                if not goyang_dulu:
                    goyang_dulu = True
                    log.info("  Pemuat ulasan butuh 'digoyang' (scroll naik-turun); selanjutnya dilakukan otomatis")
            continue
        if total_maps and len(terlihat) >= 0.85 * total_maps:
            h.habis, h.alasan = True, "habis"
            break
        if reload_n < cfg.MAKS_RELOAD_PER_TEMPAT:
            reload_n += 1
            pb.perlambat()
            log.warning(f"  Macet di {len(terlihat)} kartu. Memuat ulang halaman ({reload_n}/{cfg.MAKS_RELOAD_PER_TEMPAT})...")
            buka_ulasan(pb, info, mode)
            macet = 0
            continue
        pb.simpan_debug(f"macet_{nama}")
        h.alasan = "macet"
        break
    return h


def ambil_ulasan_tempat(pb: Peramban, store, info: dict, kuota_total: int, daftar_urutan: list) -> HasilPanen:
    """Mengambil sampai `kuota_total` ulasan baru untuk satu tempat; kuota dibagi ke tiap urutan."""
    modes = list(daftar_urutan) or ["terbaru"]
    sisa = kuota_total
    total = HasilPanen()
    for i, mode in enumerate(modes):
        jatah = math.ceil(sisa / (len(modes) - i))
        if i == 0:
            buka_ulasan(pb, info, mode)
        else:
            pilih_urutan(pb, mode)
            time.sleep(1.0)
        log.info(f"  Mengambil hingga {jatah} ulasan baru (urutan: {mode})")
        h = panen_ulasan(pb, store, info, jatah, mode, time.time() + cfg.MAKS_MENIT_PER_TEMPAT * 60)
        total.baru += h.baru
        total.terlihat = max(total.terlihat, h.terlihat)
        total.alasan = h.alasan
        sisa -= h.baru
        if h.habis:
            total.habis = True
            break                                # seluruh daftar sudah terbaca; urutan lain tak akan memberi hal baru
        if h.alasan in ("macet", "kosong"):
            break
        if sisa <= 0:
            break
    return total
