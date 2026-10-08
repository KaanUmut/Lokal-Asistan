"""rag.py içindeki, Ollama'ya bağlanmayan (hızlı, deterministik) fonksiyonların testleri.

Çalıştırmak için (proje klasöründe):
    pip install pytest numpy
    pytest

Bu testler Ollama'yı başlatmaz, internete çıkmaz; saniyeler içinde biter.
_embed() ve hazirla() gibi gerçek modele bağlanan fonksiyonlar burada
test edilmiyor, onlar için test_rag_entegrasyon.py'ye bakın.
"""
import numpy as np
import pytest

import rag


# ---------- parcala() ----------

def test_parcala_bos_metin_bos_liste_doner():
    assert rag.parcala("") == []


def test_parcala_cok_kisa_metin_atlanir():
    # parcala(), 40 karakterden kısa parçaları ("çok küçük, anlamsız")
    # sonuca eklemiyor; bu davranışı kilitliyoruz.
    assert rag.parcala("kısa bir cümle") == []


def test_parcala_tum_metni_kaybetmeden_boluyor():
    metin = "Python öğrenmek istiyorum. " * 200  # PARCA_BOYU'nu (800) kat kat aşan bir metin
    parcalar = rag.parcala(metin)

    assert len(parcalar) > 1, "Uzun metin birden fazla parçaya bölünmeli"
    for p in parcalar:
        # Her parça, yapılandırılan hedef boyutun biraz üstüne kadar olmalı
        # (kelime ortasından bölmemek için küçük bir pay bırakılıyor).
        assert len(p) <= rag.PARCA_BOYU + 50


def test_parcala_ardisik_parcalar_ortusuyor():
    # Örtüşme olmazsa cümle sınırındaki bir bilgi iki parça arasında
    # kaybolabilir; art arda gelen parçaların bir miktar ortak metni
    # olduğunu doğruluyoruz.
    metin = " ".join(f"cümle{n} bilgi{n} veri{n}" for n in range(300))
    parcalar = rag.parcala(metin)

    assert len(parcalar) >= 2
    ilk_kelimeler = set(parcalar[0].split())
    ikinci_kelimeler = set(parcalar[1].split())
    assert ilk_kelimeler & ikinci_kelimeler, "Ardışık parçalar arasında örtüşme bekleniyor"


def test_parcala_kelime_ortasindan_bolmuyor():
    # rfind(" ", ...) mantığı sayesinde bir kelimenin tam ortasından
    # kesilmemesi lazım.
    metin = "kelime " * 500
    for p in rag.parcala(metin):
        assert not p.startswith(" ")
        # Parça gövdesinde yarım kalmış, boşluksuz devasa bir "kelime" olmamalı
        assert all(len(k) < 30 for k in p.split())


# ---------- ara() ----------

def test_ara_indeks_bossa_bos_liste_doner(monkeypatch):
    monkeypatch.setattr(rag, "_parcalar", [])
    monkeypatch.setattr(rag, "_vektorler", None)
    assert rag.ara("herhangi bir soru") == []


def test_ara_esigin_altindaki_sonuclari_eler(monkeypatch):
    # _embed'i gerçek Ollama çağrısı yapmadan, sabit vektörler döndürecek
    # şekilde değiştiriyoruz (monkeypatch). Böylece ara()'nın skor/eşik
    # mantığını Ollama olmadan test edebiliyoruz.
    parcalar = [
        {"kaynak": "a.pdf", "sayfa": 1, "metin": "alakalı parça"},
        {"kaynak": "b.pdf", "sayfa": 2, "metin": "alakasız parça"},
    ]
    # a.pdf sorguyla birebir aynı yönde (skor ~1.0), b.pdf dik (skor ~0.0)
    vektorler = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    monkeypatch.setattr(rag, "_parcalar", parcalar)
    monkeypatch.setattr(rag, "_vektorler", vektorler)
    monkeypatch.setattr(rag, "_embed", lambda metinler: np.array([[1.0, 0.0]], dtype=np.float32))

    sonuclar = rag.ara("soru", k=4, esik=0.5)

    assert len(sonuclar) == 1
    assert sonuclar[0]["kaynak"] == "a.pdf"
    assert sonuclar[0]["skor"] == pytest.approx(1.0)


def test_ara_k_parametresi_sonuc_sayisini_sinirlar(monkeypatch):
    parcalar = [{"kaynak": f"{i}.pdf", "sayfa": None, "metin": "x"} for i in range(5)]
    vektorler = np.tile(np.array([1.0, 0.0], dtype=np.float32), (5, 1))
    monkeypatch.setattr(rag, "_parcalar", parcalar)
    monkeypatch.setattr(rag, "_vektorler", vektorler)
    monkeypatch.setattr(rag, "_embed", lambda metinler: np.array([[1.0, 0.0]], dtype=np.float32))

    sonuclar = rag.ara("soru", k=2, esik=0.0)

    assert len(sonuclar) == 2


# ---------- baglam_metni() / kaynak_ozeti() ----------

def test_baglam_metni_kaynaklari_ve_sayfalari_iceriyor():
    sonuclar = [
        {"kaynak": "ai-engineer.pdf", "sayfa": 3, "metin": "örnek içerik", "skor": 0.8},
    ]
    metin = rag.baglam_metni(sonuclar)
    assert "ai-engineer.pdf" in metin
    assert "sayfa 3" in metin
    assert "örnek içerik" in metin


def test_kaynak_ozeti_tekrar_eden_kaynaklari_birlestirir():
    sonuclar = [
        {"kaynak": "a.pdf", "sayfa": 1, "metin": "x", "skor": 0.9},
        {"kaynak": "a.pdf", "sayfa": 1, "metin": "y", "skor": 0.7},
        {"kaynak": "b.pdf", "sayfa": None, "metin": "z", "skor": 0.6},
    ]
    ozet = rag.kaynak_ozeti(sonuclar)
    # a.pdf s.1 sadece bir kez geçmeli, en yüksek (ilk gelen) skorla
    assert ozet.count("a.pdf s.1") == 1
    assert "(0.90)" in ozet
    assert "b.pdf" in ozet
