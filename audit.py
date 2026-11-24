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


def audit_master(rekaman: list[dict], referensi: dict[str, dict]) -> dict:
    """
    Returns {'kelompok': [ {nama, lokasi, jumlah, cocok_dengan} ... ],
             'salah': jumlah ulasan tak cocok, 'benar': jumlah cocok,
             'belum_ada': [nama tempat di daftar yang belum punya ulasan sama sekali]}
    """
    kelompok: OrderedDict = OrderedDict()
    ada = set()
    for r in rekaman:
        cocok = cocokkan(r, referensi)
        kunci = (r["nama_tempat"], r.get("lokasi", ""), cocok)
        kelompok[kunci] = kelompok.get(kunci, 0) + 1
        if cocok:
            ada.add(cocok)
    daftar = [{"nama": n, "lokasi": l, "cocok_dengan": c, "jumlah": j} for (n, l, c), j in kelompok.items()]
    return {
        "kelompok": daftar,
        "salah": sum(k["jumlah"] for k in daftar if not k["cocok_dengan"]),
        "benar": sum(k["jumlah"] for k in daftar if k["cocok_dengan"]),
        "belum_ada": [r["nama"] for r in referensi.values() if r["nama"] not in ada],
    }


def teks_audit(hasil: dict) -> str:
    baris = ["", "AUDIT CABANG: data master vs daftar Wanderlog", "-" * 70]
    for k in sorted(hasil["kelompok"], key=lambda x: (bool(x["cocok_dengan"]), -x["jumlah"])):
        if k["cocok_dengan"]:
            baris.append(f"  OK     {k['jumlah']:>4}  {k['nama'][:34]:<34} -> {k['cocok_dengan'][:30]}")
        else:
            baris.append(f"  SALAH  {k['jumlah']:>4}  {k['nama'][:34]:<34} ({k['lokasi'][:46]})")
    baris.append("-" * 70)
    baris.append(f"  Ulasan sesuai daftar : {hasil['benar']}")
    baris.append(f"  Ulasan cabang salah / di luar daftar : {hasil['salah']}")
    if hasil["belum_ada"]:
        baris.append(f"  Tempat di daftar yang BELUM punya ulasan ({len(hasil['belum_ada'])}):")
        for n in hasil["belum_ada"]:
            baris.append(f"     - {n}")
    return "\n".join(baris)
