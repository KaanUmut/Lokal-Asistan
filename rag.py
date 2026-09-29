"""Basit yerel RAG.

bilgi/ klasöründeki .pdf, .md ve .txt dosyalarını okur, parçalara böler,
Ollama ile embedding çıkarır ve bir soruya en yakın parçaları bulur.
Vektör veritabanı yok, sadece numpy ile kosinüs benzerliği kullanılır.
"""
import json
from pathlib import Path

import numpy as np
import ollama

try:
    from pypdf import PdfReader
except ImportError:  # pypdf yoksa PDF'ler atlanır
    PdfReader = None

# ---------- Ayarlar ----------
KLASOR = Path(__file__).parent
BILGI = KLASOR / "bilgi"          # kaynak dosyaların klasörü
INDEKS = KLASOR / ".rag_indeks"   # hesaplanan embedding'ler burada saklanır
UZANTILAR = (".md", ".txt", ".pdf")

EMBED_MODEL = "bge-m3"   # çok dilli (Türkçe için iyi): ollama pull bge-m3
PARCA_BOYU = 800         # bir parçanın yaklaşık karakter sayısı
ORTUSME = 150            # ardışık parçaların örtüşme miktarı
K = 4                    # en fazla kaç parça getirilsin
ESIK = 0.35              # bundan düşük benzerlikteki parçalar kullanılmaz

_parcalar = []           # [{"kaynak", "sayfa", "metin"}, ...]
_vektorler = None        # (parça sayısı, boyut) numpy dizisi, normalize edilmiş


def _dosyalar():
    if not BILGI.exists():
        return []
    return [
        y for y in sorted(BILGI.rglob("*"))
        if y.is_file() and y.suffix.lower() in UZANTILAR
    ]


def _imza():
    """Dosyalar veya ayarlar değişince indeksin yeniden kurulması için parmak izi."""
    dosyalar = []
    for y in _dosyalar():
        s = y.stat()
        dosyalar.append([y.relative_to(BILGI).as_posix(), s.st_size, int(s.st_mtime)])
    return {"model": EMBED_MODEL, "parca_boyu": PARCA_BOYU, "dosyalar": dosyalar}


def parcala(metin):
    """Metni yaklaşık PARCA_BOYU uzunluğunda, örtüşen parçalara böler."""
    metin = " ".join(metin.split())   # fazla boşluk ve satır sonlarını sadeleştir
    parcalar, basla = [], 0
    while basla < len(metin):
        son = min(basla + PARCA_BOYU, len(metin))
        if son < len(metin):
            bosluk = metin.rfind(" ", basla, son)   # kelimeyi ortadan bölme
            if bosluk > basla + PARCA_BOYU // 2:
                son = bosluk
        parca = metin[basla:son].strip()
        if len(parca) > 40:
            parcalar.append(parca)
        if son >= len(metin):
            break
        basla = max(son - ORTUSME, basla + 1)
    return parcalar


def _parcalari_cikar():
    parcalar, uyarilar = [], []
    for yol in _dosyalar():
        ad = yol.relative_to(BILGI).as_posix()
        if yol.suffix.lower() == ".pdf":
            if PdfReader is None:
                uyarilar.append(f"{ad} atlandı: pypdf kurulu değil (pip install pypdf).")
                continue
            try:
                okuyucu = PdfReader(str(yol))
                sayfalar = [
                    (no, s.extract_text() or "")
                    for no, s in enumerate(okuyucu.pages, start=1)
                ]
            except Exception as e:
                uyarilar.append(f"{ad} okunamadı: {e}")
                continue
        else:
            sayfalar = [(None, yol.read_text(encoding="utf-8", errors="ignore"))]

        eklenen = 0
        for no, metin in sayfalar:
            for parca in parcala(metin):
                parcalar.append({"kaynak": ad, "sayfa": no, "metin": parca})
                eklenen += 1
        if eklenen == 0:
            uyarilar.append(
                f"{ad} içinden metin çıkmadı (taranmış, yani görüntü halinde bir PDF olabilir)."
            )
    return parcalar, uyarilar


def _embed(metinler):
    """Metinleri embedding'e çevirir ve birim uzunluğa normalize eder."""
    sonuc = []
    for i in range(0, len(metinler), 16):
        r = ollama.embed(model=EMBED_MODEL, input=metinler[i:i + 16])
        sonuc.extend(r["embeddings"])
    v = np.array(sonuc, dtype=np.float32)
    normlar = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.clip(normlar, 1e-9, None)


def hazirla():
    """İndeksi yükler ya da kurar. (parça sayısı, uyarı listesi) döndürür."""
    global _parcalar, _vektorler
    BILGI.mkdir(exist_ok=True)
    imza = _imza()
    meta_yol = INDEKS / "meta.json"
    vek_yol = INDEKS / "vektorler.npy"

    # Dosyalar değişmediyse kayıtlı indeksi kullan
    if meta_yol.exists() and vek_yol.exists():
        try:
            kayit = json.loads(meta_yol.read_text(encoding="utf-8"))
            if kayit.get("imza") == imza:
                _parcalar = kayit["parcalar"]
                _vektorler = np.load(vek_yol)
                return len(_parcalar), []
        except Exception:
            pass  # bozuk kayıt, yeniden kur

    parcalar, uyarilar = _parcalari_cikar()
    if not parcalar:
        _parcalar, _vektorler = [], None
        return 0, uyarilar

    vektorler = _embed([p["metin"] for p in parcalar])
    INDEKS.mkdir(exist_ok=True)
    np.save(vek_yol, vektorler)
    meta_yol.write_text(
        json.dumps({"imza": imza, "parcalar": parcalar}, ensure_ascii=False),
        encoding="utf-8",
    )
    _parcalar, _vektorler = parcalar, vektorler
    return len(parcalar), uyarilar


def ara(soru, k=K, esik=ESIK):
    """Soruya en yakın parçaları döndürür (her biri 'skor' alanıyla)."""
    if _vektorler is None or not _parcalar:
        return []
    q = _embed([soru])[0]
    skorlar = _vektorler @ q
    sirali = np.argsort(skorlar)[::-1][:k]
    return [
        dict(_parcalar[i], skor=float(skorlar[i]))
        for i in sirali if skorlar[i] >= esik
    ]


def baglam_metni(sonuclar):
    """Bulunan parçaları modele verilecek metne çevirir."""
    bloklar = []
    for i, s in enumerate(sonuclar, start=1):
        yer = s["kaynak"] + (f", sayfa {s['sayfa']}" if s["sayfa"] else "")
        bloklar.append(f"[Kaynak {i}: {yer}]\n{s['metin']}")
    return (
        "Aşağıdaki kaynak parçaları kullanıcının bilgi klasöründen alındı:\n\n"
        + "\n\n".join(bloklar)
    )


def kaynak_ozeti(sonuclar):
    """Arayüzde gösterilecek kısa kaynak listesi (benzerlik skoruyla)."""
    gorulen, satirlar = set(), []
    for s in sonuclar:
        yer = s["kaynak"] + (f" s.{s['sayfa']}" if s["sayfa"] else "")
        if yer not in gorulen:
            gorulen.add(yer)
            satirlar.append(f"{yer} ({s['skor']:.2f})")
    return "Kaynaklar: " + ", ".join(satirlar)
