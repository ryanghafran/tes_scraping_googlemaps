"""
audit.py
========
Memeriksa data master terhadap daftar Wanderlog: ulasan mana yang berasal dari cabang yang SALAH
(atau tempat yang tidak ada di daftar), lalu - bila diminta - membuangnya.

Pencocokan satu ulasan dengan satu tempat di daftar:
  1. id_tempat ulasan (ID internal Maps) == fid tempat di daftar, atau
  2. (untuk ulasan lama tanpa id_tempat) jalan + nomor di kolom 'lokasi' == alamat tempat di daftar.
Ulasan yang tidak cocok dengan tempat mana pun dianggap SALAH CABANG / di luar daftar.
"""

from __future__ import annotations

from collections import OrderedDict

from wanderlog import kunci_alamat


def cocokkan(rekaman: dict, referensi: dict[str, dict]) -> str:
    """Nama tempat di daftar yang cocok dengan ulasan ini ('' bila tidak ada)."""
    idt = rekaman.get("id_tempat") or ""
    if idt:
        for r in referensi.values():
            if r.get("fid") and r["fid"] == idt:
                return r["nama"]
        return ""
    k = kunci_alamat(rekaman.get("lokasi", ""))
    if not k[0]:
        return ""
    for r in referensi.values():
        if r.get("alamat") and kunci_alamat(r["alamat"]) == k:
            return r["nama"]
    return ""


def tempat_tutup(status_data: dict, baris_tutup: list[dict] | None = None) -> list[dict]:
    """
    Tempat yang diketahui TUTUP (permanen/sementara) dari status_tempat.json dan daftar_coffeeshop_tutup.csv.
    Tiap item: {'nama': nama resmi di Maps, 'fid': ID internal, 'kunci': kunci alamat, 'status': ...}.
    """
    hasil = []
    for s in status_data.values():
        if s.get("status_buka") in ("tutup_permanen", "tutup_sementara"):
            hasil.append({"nama": s.get("nama_resmi") or s.get("nama_input", ""), "fid": s.get("id_tempat", ""),
                          "kunci": kunci_alamat(s.get("alamat", "")), "status": s["status_buka"]})
    for b in baris_tutup or []:
        hasil.append({"nama": b.get("nama_resmi") or b.get("nama_input", ""), "fid": "", "kunci": ("", ""),
                      "status": b.get("status", "")})
    return hasil


def tutup_cocok(rekaman: dict, tutup: list[dict]) -> str:
    """Status tutup ('tutup_permanen'|'tutup_sementara') bila ulasan ini milik tempat yang tutup, else ''."""
    idt, k, nama = rekaman.get("id_tempat") or "", kunci_alamat(rekaman.get("lokasi", "")), (rekaman.get("nama_tempat") or "").casefold()
    for t in tutup:
        if idt and t["fid"] and idt == t["fid"]:
            return t["status"]
        if t["kunci"][0] and k == t["kunci"]:
            return t["status"]
        if nama and t["nama"] and nama == t["nama"].casefold():
            return t["status"]
    return ""


def audit_master(rekaman: list[dict], referensi: dict[str, dict], tutup: list[dict] | None = None) -> dict:
    """
    Returns {'kelompok': [ {nama, lokasi, jumlah, cocok_dengan, tutup} ... ],
             'salah': jumlah ulasan tak cocok, 'benar': jumlah cocok,
             'belum_ada': [nama tempat di daftar yang belum punya ulasan sama sekali]}
    """
    kelompok: OrderedDict = OrderedDict()
    ada = set()
    for r in rekaman:
        cocok = cocokkan(r, referensi)
        st_tutup = tutup_cocok(r, tutup or [])
        kunci = (r["nama_tempat"], r.get("lokasi", ""), cocok, st_tutup)
        kelompok[kunci] = kelompok.get(kunci, 0) + 1
        if cocok and not st_tutup:
            ada.add(cocok)
    daftar = [{"nama": n, "lokasi": l, "cocok_dengan": c, "tutup": t, "jumlah": j} for (n, l, c, t), j in kelompok.items()]
    return {
        "kelompok": daftar,
        "salah": sum(k["jumlah"] for k in daftar if not k["cocok_dengan"] and not k["tutup"]),
        "tutup": sum(k["jumlah"] for k in daftar if k["tutup"]),
        "benar": sum(k["jumlah"] for k in daftar if k["cocok_dengan"] and not k["tutup"]),
        "belum_ada": [r["nama"] for r in referensi.values() if r["nama"] not in ada],
    }


def teks_audit(hasil: dict) -> str:
    baris = ["", "AUDIT CABANG: data master vs daftar Wanderlog", "-" * 70]
    for k in sorted(hasil["kelompok"], key=lambda x: (bool(x["cocok_dengan"]) and not x["tutup"], -x["jumlah"])):
        if k["tutup"]:
            baris.append(f"  TUTUP  {k['jumlah']:>4}  {k['nama'][:34]:<34} [{k['tutup'].replace('_', ' ')}]")
        elif k["cocok_dengan"]:
            baris.append(f"  OK     {k['jumlah']:>4}  {k['nama'][:34]:<34} -> {k['cocok_dengan'][:30]}")
        else:
            baris.append(f"  SALAH  {k['jumlah']:>4}  {k['nama'][:34]:<34} ({k['lokasi'][:46]})")
    baris.append("-" * 70)
    baris.append(f"  Ulasan sesuai daftar : {hasil['benar']}")
    baris.append(f"  Ulasan cabang salah / di luar daftar : {hasil['salah']}")
    baris.append(f"  Ulasan tempat TUTUP (permanen/sementara) : {hasil['tutup']}")
    if hasil["belum_ada"]:
        baris.append(f"  Tempat di daftar yang BELUM punya ulasan ({len(hasil['belum_ada'])}):")
        for n in hasil["belum_ada"]:
            baris.append(f"     - {n}")
    return "\n".join(baris)
