"""Lokal Asistan: kısayolla ekran görüntüsü al, ekrana dair sohbet et.
Roadmap modunda bilgi/ klasöründeki kaynaklara (RAG) dayanarak yol haritası çıkarır."""
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import keyboard
import mss
import mss.tools
import ollama

import rag

try:
    from PIL import Image, ImageTk
except ImportError:  # Pillow yoksa görüntü küçültülmeden gönderilir, önizleme gösterilmez
    Image = ImageTk = None

# ---------- Ayarlar ----------
KLASOR = Path(__file__).parent
RESIM = KLASOR / "ekran.png"

MODEL = "qwen2.5vl:7b"
KISAYOL = "ctrl+alt+s"
PANEL_KISAYOL = "ctrl+alt+d"   # gizli token paneli
OZET_ESIK = 8          # bu sayıda mesajdan sonra eski geçmiş özetlenir (sistem hariç)
OZET_KORUNAN = 4       # özetlenmeden, ham haliyle bırakılan en yeni mesaj sayısı
GENISLIK = 440        # pencere genişliği (piksel)
ALT_BOSLUK = 60       # görev çubuğuna yer bırak
MAX_KENAR = 1400      # ekran görüntüsü en fazla bu kadar piksel olur

SISTEM = (
    "Sen kullanıcının ekran görüntüsünü görebilen bir yardımcı asistansın. "
    "Yalnızca Türkçe yaz; Çince, Japonca gibi başka dillerin karakterlerini "
    "asla kullanma. Teknik terimleri (float, string, print gibi) olduğu gibi "
    "İngilizce bırak. Soruyu ekrandaki içeriğe dayanarak, kısa ve adım adım "
    "cevapla. Ekranda görmediğin şeyi uydurma; emin değilsen söyle. "
    "Kaynak parçaları verilmişse cevabını onlara dayandır; kaynaklarda yoksa "
    "bunu söyle."
)

ROADMAP_SISTEM = (
    "Sen bir öğrenme yol haritası koçusun. Yalnızca Türkçe yaz; Çince, Japonca "
    "gibi başka dillerin karakterlerini asla kullanma; teknik terimleri "
    "İngilizce bırak. Kullanıcının hedefini, mevcut seviyesini ve haftalık "
    "ayırabileceği süreyi bilmiyorsan önce bunları kısa sorularla, birer birer "
    "öğren. Yeterli bilgi olunca, verilen kaynak parçalarına dayanarak haftalık, "
    "uygulanabilir bir yol haritası çıkar. Kaynaklarda olmayan kurs, kitap veya "
    "kaynak uydurma; kaynaklarda yoksa bunu belirt."
)

# ---------- Renkler ve yazı tipi ----------
BG = "#16171b"
PANEL = "#212329"
PANEL_HOVER = "#2b2e36"
KENAR = "#30333c"
YAZI = "#e8e9ed"
SOLUK = "#8b909a"
VURGU = "#5b6cf0"
VURGU_HOVER = "#7282f5"
YESIL = "#4ade80"
SARI = "#facc15"
KIRMIZI = "#f87171"
KOD_BG = "#0d0e11"
KOD_YAZI = "#f5c08b"
FONT = "Segoe UI"
KOD_FONT = "Consolas"

# ---------- Durum ----------
kuyruk = queue.Queue()        # thread'lerden arayüze mesaj taşır
mesgul = threading.Event()    # model cevap verirken set edilir
rag_mesgul = threading.Event()  # bilgi klasörü indekslenirken set edilir
mesajlar = []                 # modele gönderilen sohbet geçmişi
gecmis_token = []             # her isteğin token istatistikleri (en yeni en sonda)
ozet_mesgul = threading.Event()  # özetleme sürerken set edilir
ozet_sayisi = 0               # kaç kez özetleme yapıldı (panelde gösterilir)
durum = {"resim_bekliyor": False, "mod": "ekran", "rag": "Bilgi: yükleniyor"}


def ekran_yakala():
    """Ana monitörü yakalar, gerekirse küçültüp RESIM olarak kaydeder."""
    with mss.mss() as sct:
        ham = sct.grab(sct.monitors[1])
    if Image is None:
        mss.tools.to_png(ham.rgb, ham.size, output=str(RESIM))
        return
    img = Image.frombytes("RGB", ham.size, ham.bgra, "raw", "BGRX")
    img.thumbnail((MAX_KENAR, MAX_KENAR))
    img.save(RESIM)


# ---------- Sohbet bileşenleri ----------
aktif = None        # şu an yazılmakta olan asistan mesajı
takip = True        # kullanıcı en alttaysa yeni içerik geldikçe aşağı kaydır
sarilanlar = []     # (label, oran): pencere genişleyince satır kaydırması güncellenir


def sayi(t, *secenekler):
    """Text.count sonucunu tek bir int'e çevirir (Python sürümüne göre tuple dönebiliyor)."""
    r = t.count("1.0", "end", *secenekler)
    if isinstance(r, (tuple, list)):
        r = r[0]
    return int(r or 0)


def yuvarlak_dikdortgen(c, x1, y1, x2, y2, r, **kw):
    noktalar = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(noktalar, smooth=True, **kw)


class Kutu(tk.Canvas):
    """İçine widget konabilen, köşeleri yuvarlatılmış renkli kutu.
    genis=True ise bulunduğu satırı boydan boya kaplar, değilse içeriği kadar olur."""

    def __init__(self, ust, renk, yaricap=14, bosluk=(14, 9), genis=False):
        super().__init__(ust, bg=ust["bg"], highlightthickness=0, bd=0,
                         width=1, height=1)
        self.renk, self.r, self.genis = renk, yaricap, genis
        self.px, self.py = bosluk
        self.ic = tk.Frame(self, bg=renk)
        self.oge = self.create_window(self.px, self.py, window=self.ic, anchor="nw")
        self.ic.bind("<Configure>", lambda e: self.olc())
        if genis:
            self.bind("<Configure>", self._dis_degisti)
        # Kutu 1x1 başladığı için iç çerçeve görünmez ve <Configure> gelmeyebilir;
        # içerik eklendikten sonra ilk ölçümü elle yap.
        self.after_idle(self.olc)

    def olc(self):
        if not self.winfo_exists():
            return
        w = self.ic.winfo_reqwidth() + 2 * self.px
        h = self.ic.winfo_reqheight() + 2 * self.py
        if self.genis:
            self.config(height=h)
        else:
            self.config(width=w, height=h)
        self._ciz()

    def _dis_degisti(self, e):
        self.itemconfig(self.oge, width=max(e.width - 2 * self.px, 1))
        self._ciz()

    def _ciz(self):
        w = self.winfo_width() if self.genis else self.ic.winfo_reqwidth() + 2 * self.px
        h = self.ic.winfo_reqheight() + 2 * self.py
        self.delete("zemin")
        yuvarlak_dikdortgen(self, 0, 0, w - 1, h - 1, self.r, fill=self.renk,
                            outline=self.renk, tags="zemin")
        self.tag_lower("zemin")

    def renk_degistir(self, renk):
        self.renk = renk
        self.itemconfig("zemin", fill=renk, outline=renk)
        for w in [self.ic, *self.ic.winfo_children()]:
            w.config(bg=renk)


def tiklanabilir(kutu, komut, normal, hover):
    """Kutuyu ve içindeki her şeyi tıklanabilir yapar, üzerine gelince rengini değiştirir."""
    for w in [kutu, kutu.ic, *kutu.ic.winfo_children()]:
        w.bind("<Button-1>", lambda e: komut())
        w.bind("<Enter>", lambda e: kutu.renk_degistir(hover))
        w.bind("<Leave>", lambda e: kutu.renk_degistir(normal))
        w.config(cursor="hand2")


def sarilan(label, oran):
    sarilanlar.append((label, oran))
    label.config(wraplength=max(int(sohbet.winfo_width() * oran), 120))
    return label


def mesaj_satiri(ust_bosluk=8):
    satir = tk.Frame(ic, bg=BG)
    satir.pack(fill="x", padx=16, pady=(ust_bosluk, 2))
    return satir


def asagi_in():
    global takip
    takip = True
    sohbet.yview_moveto(1.0)


def not_ekle(metin, renk=SOLUK):
    """Ortalanmış küçük bilgi satırı (sistem notları, uyarılar)."""
    satir = mesaj_satiri(6)
    sarilan(tk.Label(satir, text=metin, bg=BG, fg=renk, font=(FONT, 9, "italic"),
                     justify="center"), 0.85).pack()


def kullanici_ekle(metin):
    satir = mesaj_satiri(14)
    balon = Kutu(satir, VURGU, yaricap=16, bosluk=(14, 9))
    balon.pack(side="right")
    sarilan(tk.Label(balon.ic, text=metin, bg=VURGU, fg="white", font=(FONT, 11),
                     justify="left"), 0.72).pack()


def ekran_karti():
    """Alınan ekran görüntüsünün küçük önizlemesini sohbete koyar."""
    satir = mesaj_satiri(14)
    kutu = Kutu(satir, PANEL, yaricap=14, bosluk=(10, 10))
    kutu.pack(anchor="w")
    if ImageTk is not None:
        try:
            with Image.open(RESIM) as img:
                kucuk = img.copy()
            kucuk.thumbnail((280, 170))
            foto = ImageTk.PhotoImage(kucuk)
            lbl = tk.Label(kutu.ic, image=foto, bg=PANEL, bd=0)
            lbl.image = foto   # referans tutulmazsa resim silinir
            lbl.pack(anchor="w")
        except Exception:
            pass
    tk.Label(kutu.ic, text="Ekran görüntüsü alındı", bg=PANEL, fg=YAZI,
             font=(FONT, 10, "bold")).pack(anchor="w", pady=(8, 0))
    tk.Label(kutu.ic, text="Ne öğrenmek istediğini aşağıya yaz.", bg=PANEL,
             fg=SOLUK, font=(FONT, 9)).pack(anchor="w")


def karsilama():
    """Boş sohbette gösterilen başlangıç ekranı."""
    satir = mesaj_satiri(40)
    tk.Label(satir, text="Merhaba!", bg=BG, fg=YAZI,
             font=(FONT, 20, "bold")).pack(anchor="w")
    sarilan(tk.Label(satir, text="Ekranındaki her şeyi sorabilir ya da bir öğrenme "
                     "yol haritası çıkarabilirsin. Her şey bilgisayarında kalır.",
                     bg=BG, fg=SOLUK, font=(FONT, 10), justify="left"),
            0.85).pack(anchor="w", pady=(2, 18))
    secenekler = [
        ("Ekranı sor", f"{KISAYOL.upper()} ya da buraya tıkla", yeni_ekran),
        ("Yol haritası çıkar", "Bilgi klasöründeki kaynaklara dayanır", roadmap_modu),
    ]
    for baslik, alt_yazi, komut in secenekler:
        kart = Kutu(satir, PANEL, yaricap=12, bosluk=(14, 11), genis=True)
        kart.pack(fill="x", pady=4)
        tk.Label(kart.ic, text=baslik, bg=PANEL, fg=YAZI,
                 font=(FONT, 11, "bold")).pack(anchor="w")
        tk.Label(kart.ic, text=alt_yazi, bg=PANEL, fg=SOLUK,
                 font=(FONT, 9)).pack(anchor="w")
        tiklanabilir(kart, komut, PANEL, PANEL_HOVER)


def md_satir_ici(t, metin, taban):
    """**kalın** ve `kod` işaretlerini etiketlere çevirerek ekler."""
    for parca in re.split(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)", metin):
        if not parca:
            continue
        if parca.startswith("**") and parca.endswith("**") and len(parca) > 4:
            t.insert("end", parca[2:-2], (taban, "kalin"))
        elif parca.startswith("`") and parca.endswith("`") and len(parca) > 2:
            t.insert("end", parca[1:-1], (taban, "kod"))
        else:
            t.insert("end", parca, taban)


def md_ciz(t, metin):
    """Modelin markdown cevabını basitçe biçimlendirir: başlık, madde, kod bloğu."""
    t.config(state="normal")
    t.delete("1.0", "end")
    satirlar = metin.strip("\n").split("\n")
    kod = False
    for i, satir in enumerate(satirlar):
        son = "" if i == len(satirlar) - 1 else "\n"
        if satir.strip().startswith("```"):
            kod = not kod
            continue
        if kod:
            t.insert("end", satir + son, "kod_blok")
            continue
        if not satir.strip():
            t.insert("end", son, "bosluk")   # paragraf arası: tam satır yerine kısa boşluk
        elif m :=re.match(r"#{1,4}\s+(.*)", satir):
            md_satir_ici(t, m.group(1), "baslik")
            t.insert("end", son, "baslik")
        elif m := re.match(r"\s*[-*•]\s+(.*)", satir):
            t.insert("end", "•  ", ("madde", "isaret"))
            md_satir_ici(t, m.group(1), "madde")
            t.insert("end", son, "madde")
        elif m := re.match(r"\s*(\d+)[.)]\s+(.*)", satir):
            t.insert("end", f"{m.group(1)}.  ", ("madde", "isaret"))
            md_satir_ici(t, m.group(2), "madde")
            t.insert("end", son, "madde")
        else:
            md_satir_ici(t, satir, "metin")
            t.insert("end", son, "metin")
    t.config(state="disabled")


class AsistanMesaji:
    """Akarak yazılan, markdown biçimli asistan cevabı."""

    def __init__(self):
        self.satir = mesaj_satiri(14)
        bas = tk.Frame(self.satir, bg=BG)
        bas.pack(fill="x")
        avatar = tk.Canvas(bas, width=22, height=22, bg=BG, highlightthickness=0)
        avatar.create_oval(1, 1, 21, 21, fill=YESIL, outline=YESIL)
        avatar.create_text(11, 11, text="A", fill=BG, font=(FONT, 9, "bold"))
        avatar.pack(side="left")
        tk.Label(bas, text="Asistan", bg=BG, fg=YAZI,
                 font=(FONT, 10, "bold")).pack(side="left", padx=(8, 0))

        self.kutu = tk.Frame(self.satir, bg=BG, height=24)
        self.kutu.pack(fill="x", padx=(30, 0), pady=(4, 0))
        self.kutu.pack_propagate(False)
        self.t = tk.Text(
            self.kutu, wrap="word", bg=BG, fg=YAZI, bd=0, relief="flat",
            padx=0, pady=0, font=(FONT, 11), highlightthickness=0,
            selectbackground=VURGU, inactiveselectbackground=KENAR,
            cursor="xterm", state="disabled",
        )
        self.t.pack(fill="both", expand=True)
        metin_etiketleri(self.t)
        self.t.bind("<Configure>", lambda e: self.boy_ayarla())
        self.t.bind("<MouseWheel>", lambda e: (tekerlek(e), "break")[1])

        self.alt = tk.Frame(self.satir, bg=BG)
        self.alt.pack(fill="x", padx=(30, 0))
        self.ham = ""
        self.bitti = False
        self.planli = False
        self.nokta = 0
        self._dusunuyor()

    def _dusunuyor(self):
        if self.ham or self.bitti:
            return
        self.t.config(state="normal")
        self.t.delete("1.0", "end")
        self.t.insert("end", "Düşünüyor" + "." * (self.nokta % 4), "dusunuyor")
        self.t.config(state="disabled")
        self.boy_ayarla()
        self.nokta += 1
        self.t.after(350, self._dusunuyor)

    def ekle(self, metin):
        self.ham += metin
        if not self.planli:     # her parçada değil, ~60 ms'de bir yeniden çiz
            self.planli = True
            self.t.after(60, self._ciz)

    def _ciz(self):
        self.planli = False
        md_ciz(self.t, self.ham)
        self.boy_ayarla()

    def boy_ayarla(self):
        if self.t.winfo_width() < 50:   # henüz yerleşmedi; <Configure> tekrar çağırır
            return
        yukseklik = sayi(self.t, "update", "ypixels") + 4
        if yukseklik != self.kutu.winfo_reqheight():
            self.kutu.config(height=yukseklik)

    def kaynak(self, metin):
        chip = Kutu(self.alt, PANEL, yaricap=10, bosluk=(10, 5))
        chip.pack(anchor="w", pady=(8, 0))
        sarilan(tk.Label(chip.ic, text=metin, bg=PANEL, fg=SOLUK, font=(FONT, 9),
                         justify="left"), 0.75).pack()

    def bitir(self, kopyala=True):
        self.bitti = True
        self._ciz()
        if not kopyala:
            return
        dugme_ = tk.Label(self.alt, text="Kopyala", bg=BG, fg=SOLUK,
                          font=(FONT, 9), cursor="hand2")
        dugme_.pack(anchor="w", pady=(6, 0))

        def kopya(e=None):
            pencere.clipboard_clear()
            pencere.clipboard_append(self.ham)
            dugme_.config(text="Kopyalandı ✓", fg=YESIL)
            dugme_.after(1500, lambda: dugme_.config(text="Kopyala", fg=SOLUK))

        dugme_.bind("<Button-1>", kopya)
        dugme_.bind("<Enter>", lambda e: dugme_.config(fg=YAZI))
        dugme_.bind("<Leave>", lambda e: dugme_.config(fg=SOLUK))

    def hata(self, metin):
        self.bitti = True
        if self.ham:
            self._ciz()
        else:
            self.t.config(state="normal")
            self.t.delete("1.0", "end")
            self.t.config(state="disabled")
            self.boy_ayarla()
        sarilan(tk.Label(self.alt, text=f"Hata: {metin}", bg=BG, fg=KIRMIZI,
                         font=(FONT, 9), justify="left"), 0.8).pack(anchor="w")


def metin_etiketleri(t):
    # Sonra tanımlanan etiketin yazı tipi öncelikli olur: kalin ve kod en sonda.
    t.tag_config("metin", spacing2=3, spacing3=4)
    t.tag_config("madde", lmargin1=6, lmargin2=24, spacing2=3, spacing3=3)
    t.tag_config("baslik", font=(FONT, 12, "bold"), spacing1=8, spacing3=4)
    t.tag_config("isaret", foreground=VURGU_HOVER)
    t.tag_config("kod_blok", font=(KOD_FONT, 10), background=KOD_BG,
                 foreground="#d4d4d4", lmargin1=12, lmargin2=12, rmargin=8,
                 lmargincolor=KOD_BG, rmargincolor=KOD_BG, spacing1=1, spacing3=1)
    t.tag_config("kalin", font=(FONT, 11, "bold"), foreground="white")
    t.tag_config("kod", font=(KOD_FONT, 10), background=KOD_BG, foreground=KOD_YAZI)
    t.tag_config("dusunuyor", foreground=SOLUK, font=(FONT, 10, "italic"))
    t.tag_config("bosluk", font=(FONT, 4))


def asistan_mesaji(metin):
    """Modele gitmeyen, hazır yazılmış asistan mesajı (örneğin mod açıklaması)."""
    m = AsistanMesaji()
    m.ekle(metin)
    m.bitir(kopyala=False)


def sohbeti_temizle():
    global aktif
    aktif = None
    sarilanlar.clear()
    for w in ic.winfo_children():
        w.destroy()
    asagi_in()


def durum_yaz(metin):
    renk = SARI if "Düşünüyor" in metin else KIRMIZI if "Hata" in metin else YESIL
    durum_nokta.itemconfig("nokta", fill=renk, outline=renk)
    durum_etiketi.config(text=metin)
    rag_etiketi.config(text=durum["rag"])


def mod_guncelle():
    for ad, d in mod_dugmeleri.items():
        secili = durum["mod"] == ad
        d.renk_degistir(VURGU if secili else PANEL)
        d.secili = secili


def goster():
    pencere.deiconify()
    pencere.lift()
    pencere.attributes("-topmost", ustte.get())
    pencere.focus_force()
    giris.focus_set()


def gizle(event=None):
    pencere.withdraw()


def cikis():
    keyboard.unhook_all()
    pencere.destroy()


def ustte_degisti():
    pencere.attributes("-topmost", ustte.get())


# ---------- Gizli token paneli ----------
def panel_guncelle():
    if not gecmis_token:
        panel_metin.config(text="Henüz istek yok.")
        return
    son = gecmis_token[-1]
    toplam_giris = sum(t["giris"] for t in gecmis_token)
    toplam_cikis = sum(t["cikis"] for t in gecmis_token)
    satirlar = [
        f"İstek sayısı: {len(gecmis_token)}   •   Özetleme sayısı: {ozet_sayisi}",
        "",
        f"Son istek — giriş: {son['giris']}  çıkış: {son['cikis']}  "
        f"(kaynak parça: {son['kaynak_sayisi']})",
        f"Son istek toplam: {son['giris'] + son['cikis']} token",
        "",
        f"Oturum toplamı — giriş: {toplam_giris}  çıkış: {toplam_cikis}  "
        f"toplam: {toplam_giris + toplam_cikis}",
    ]
    panel_metin.config(text="\n".join(satirlar))


def panel_ac_kapa():
    if panel.winfo_ismapped():
        panel.pack_forget()
    else:
        panel_guncelle()
        panel.pack(fill="x", padx=12, pady=(0, 8), before=orta)


def panel_kisayol_basildi():
    kuyruk.put(("panel", None))


# ---------- Bilgi klasörü (RAG) ----------
def rag_yukle():
    """Ayrı thread'de bilgi/ klasörünü okur ve indeksler."""
    if rag_mesgul.is_set():
        return
    rag_mesgul.set()
    try:
        kuyruk.put(("rag_durum", "Bilgi: indeksleniyor..."))
        sayi, uyarilar = rag.hazirla()
        if sayi:
            kuyruk.put(("rag_durum", f"Bilgi: {sayi} parça"))
        else:
            kuyruk.put(("rag_durum", "Bilgi: boş"))
        for u in uyarilar:
            kuyruk.put(("uyari", u))
    except Exception as e:
        kuyruk.put(("rag_durum", "Bilgi: hata"))
        kuyruk.put(("uyari", f"Bilgi klasörü yüklenemedi: {e}"))
    finally:
        rag_mesgul.clear()


def rag_yenile():
    threading.Thread(target=rag_yukle, daemon=True).start()


# ---------- Modlar ----------
def yeni_ekran():
    if mesgul.is_set():
        return
    pencere.withdraw()               # pencere görüntüye girmesin
    pencere.after(350, ekran_al)


def ekran_al():
    try:
        ekran_yakala()
    except Exception as e:
        goster()
        not_ekle(f"Ekran alınamadı: {e}", KIRMIZI)
        return
    durum["mod"] = "ekran"
    mesajlar.clear()
    mesajlar.append({"role": "system", "content": SISTEM})
    gecmis_token.clear()
    global ozet_sayisi
    ozet_sayisi = 0
    panel_guncelle()
    durum["resim_bekliyor"] = True
    sohbeti_temizle()
    ekran_karti()
    mod_guncelle()
    durum_yaz("Ekran hazır")
    goster()


def roadmap_modu():
    if mesgul.is_set():
        return
    durum["mod"] = "roadmap"
    durum["resim_bekliyor"] = False
    mesajlar.clear()
    mesajlar.append({"role": "system", "content": ROADMAP_SISTEM})
    gecmis_token.clear()
    global ozet_sayisi
    ozet_sayisi = 0
    panel_guncelle()
    sohbeti_temizle()
    asistan_mesaji(
        "**Roadmap modu.** Bana şunları yaz:\n"
        "- Hedefin (örneğin AI stajı)\n"
        "- Şu anki seviyen\n"
        "- Haftalık ayırabileceğin süre\n\n"
        "Yol haritasını bilgi klasöründeki kaynaklara dayanarak çıkaracağım."
    )
    mod_guncelle()
    durum_yaz("Roadmap modu")
    goster()


# ---------- Modele soru ----------
def gonder(event=None):
    if mesgul.is_set():
        return "break"
    soru = giris.get("1.0", "end").strip()
    if not soru:
        return "break"
    giris.delete("1.0", "end")
    giris_degisti()

    kullanici_ekle(soru)

    mesaj = {"role": "user", "content": soru}
    if durum["resim_bekliyor"]:
        mesaj["images"] = [str(RESIM)]   # görüntü sadece ilk soruyla gider
        durum["resim_bekliyor"] = False
    mesajlar.append(mesaj)

    # Roadmap modunda kısa cevaplar ("evet", "günde 3 saat") tek başına anlamsız
    # olduğu için son birkaç kullanıcı mesajıyla arama yapılır.
    kullanici = [m["content"] for m in mesajlar if m["role"] == "user"]
    sorgu = " ".join(kullanici[-3:]) if durum["mod"] == "roadmap" else soru

    mesgul.set()
    durum_yaz("Düşünüyor...")
    global aktif
    aktif = AsistanMesaji()
    asagi_in()
    threading.Thread(
        target=modele_sor, args=(list(mesajlar), sorgu, durum["mod"]), daemon=True
    ).start()
    return "break"


def modele_sor(gecmis, sorgu, mod):
    """Ayrı thread'de çalışır; kaynakları bulur, cevabı parça parça kuyruğa bırakır."""
    tam = ""
    try:
        sonuclar = []
        if mod == "roadmap":
            # RAG sadece roadmap modunda çalışır. Ekran modunda amaç ekranı
            # yorumlamak; PDF'le zayıf/rastgele bir benzerlik çıkıp alakasız
            # bir "kaynak" etiketi görünmesini istemiyoruz.
            try:
                sonuclar = rag.ara(sorgu)
            except Exception:
                sonuclar = []   # bilgi araması başarısızsa kaynaksız devam et

        if sonuclar:
            # Kaynaklar sadece bu istek için eklenir, sohbet geçmişi temiz kalır
            son = dict(gecmis[-1])
            son["content"] = (
                rag.baglam_metni(sonuclar)
                + "\n\nKullanıcının mesajı: "
                + son["content"]
            )
            gecmis = gecmis[:-1] + [son]

        for parca in ollama.chat(
            model=MODEL, messages=gecmis, stream=True,
            options={"temperature": 0.3},
        ):
            metin = parca["message"]["content"]
            tam += metin
            kuyruk.put(("parca", metin))
            if parca.get("done"):
                # Ollama'nın verdiği gerçek token sayıları (tahmin değil)
                kuyruk.put(("token", {
                    "giris": parca.get("prompt_eval_count", 0),
                    "cikis": parca.get("eval_count", 0),
                    "kaynak_sayisi": len(sonuclar),
                }))

        if sonuclar:
            kuyruk.put(("kaynak", rag.kaynak_ozeti(sonuclar)))
        kuyruk.put(("bitti", tam))
    except Exception as e:
        kuyruk.put(("hata", str(e)))


def ozetle(snapshot):
    """Ayrı thread'de çalışır. Eski mesajları tek bir özet mesajına indirir.

    snapshot: özetleme başladığı andaki mesajlar listesinin kopyası.
    Sadece snapshot'ın ilk kısmını özetler; sonradan eklenen yeni mesajlara
    dokunmaz, bu yüzden özetleme sürerken kullanıcı yazmaya devam edebilir.
    """
    if ozet_mesgul.is_set():
        return
    ozet_mesgul.set()
    try:
        ozetlenecek = snapshot[1:len(snapshot) - OZET_KORUNAN]
        if len(ozetlenecek) < 2:
            return  # özetlenecek yeterli mesaj yok

        # Görüntüleri değil, sadece metinleri özetliyoruz; görüntüden çıkan
        # bilgi zaten o turun cevabında metne dökülmüş durumda.
        metin = "\n".join(
            f"{m['role']}: {m['content']}" for m in ozetlenecek
        )
        istem = [
            {
                "role": "system",
                "content": (
                    "Aşağıdaki konuşmayı Türkçe, en fazla 4 cümlede özetle. "
                    "Önemli kararları, sayıları ve sonuçları kaybetme. "
                    "Sadece özeti yaz, başka bir şey ekleme."
                ),
            },
            {"role": "user", "content": metin},
        ]
        r = ollama.chat(model=MODEL, messages=istem, options={"temperature": 0.2})
        ozet = r["message"]["content"]
        kuyruk.put(("ozet_hazir", {"ozet": ozet, "sayi": len(ozetlenecek)}))
    except Exception as e:
        kuyruk.put(("uyari", f"Özetleme başarısız oldu, geçmiş olduğu gibi kaldı: {e}"))
    finally:
        ozet_mesgul.clear()


def kuyruk_kontrol():
    global aktif
    try:
        while True:
            komut, veri = kuyruk.get_nowait()
            if komut == "yeni":
                yeni_ekran()
            elif komut == "parca":
                if aktif:
                    aktif.ekle(veri)
            elif komut == "kaynak":
                if aktif:
                    aktif.kaynak(veri)
            elif komut == "token":
                gecmis_token.append(veri)
                panel_guncelle()
            elif komut == "bitti":
                mesajlar.append({"role": "assistant", "content": veri})
                if aktif:
                    aktif.bitir()
                    aktif = None
                mesgul.clear()
                durum_yaz("Hazır")
                if (
                    len(mesajlar) - 1 > OZET_ESIK
                    and not ozet_mesgul.is_set()
                ):
                    threading.Thread(
                        target=ozetle, args=(list(mesajlar),), daemon=True
                    ).start()
            elif komut == "ozet_hazir":
                global ozet_sayisi
                n = veri["sayi"]
                ozet_mesaji = {
                    "role": "system",
                    "content": f"Önceki konuşmanın özeti: {veri['ozet']}",
                }
                mesajlar[1:1 + n] = [ozet_mesaji]
                ozet_sayisi += 1
                not_ekle(f"Sohbet geçmişi özetlendi: {n} mesaj → 1 özet")
                panel_guncelle()
            elif komut == "hata":
                if aktif:
                    aktif.hata(veri)
                    aktif = None
                else:
                    not_ekle(f"Hata: {veri}", KIRMIZI)
                if mesajlar and mesajlar[-1]["role"] == "user":
                    son = mesajlar.pop()
                    if "images" in son:
                        durum["resim_bekliyor"] = True
                mesgul.clear()
                durum_yaz("Hata")
            elif komut == "rag_durum":
                durum["rag"] = veri
                durum_yaz("Düşünüyor..." if mesgul.is_set() else "Hazır")
            elif komut == "uyari":
                not_ekle(veri, SARI)
            elif komut == "panel":
                panel_ac_kapa()
    except queue.Empty:
        pass
    gonder_dugmesi_guncelle()
    bolge_guncelle()
    pencere.after(50, kuyruk_kontrol)


def kisayol_basildi():
    # Klavye thread'inde çalışır; arayüze dokunmadan kuyruğa haber verir
    kuyruk.put(("yeni", None))


# ---------- Pencere ----------
pencere = tk.Tk()
pencere.title("Lokal Asistan")
pencere.configure(bg=BG)
yukseklik = pencere.winfo_screenheight() - ALT_BOSLUK
x = pencere.winfo_screenwidth() - GENISLIK
pencere.geometry(f"{GENISLIK}x{yukseklik}+{x}+0")
pencere.minsize(340, 400)
ustte = tk.BooleanVar(value=True)


def metin_dugme(ust, metin, komut, renk=SOLUK, hover=YAZI, boyut=9):
    """Çerçevesiz, üzerine gelince parlayan yazı düğmesi."""
    d = tk.Label(ust, text=metin, bg=ust["bg"], fg=renk, font=(FONT, boyut),
                 cursor="hand2", padx=6, pady=2)
    d.bind("<Button-1>", lambda e: komut())
    d.bind("<Enter>", lambda e: d.config(fg=hover))
    d.bind("<Leave>", lambda e: d.config(fg=renk))
    return d


def mod_dugmesi(ust, metin, komut):
    d = Kutu(ust, PANEL, yaricap=12, bosluk=(12, 4))
    d.secili = False
    tk.Label(d.ic, text=metin, bg=PANEL, fg=YAZI, font=(FONT, 9, "bold")).pack()
    for w in [d, d.ic, *d.ic.winfo_children()]:
        w.bind("<Button-1>", lambda e: komut())
        w.bind("<Enter>", lambda e: d.secili or d.renk_degistir(PANEL_HOVER))
        w.bind("<Leave>", lambda e: d.secili or d.renk_degistir(PANEL))
        w.config(cursor="hand2")
    return d


# Üst bar
ust = tk.Frame(pencere, bg=BG)
ust.pack(fill="x", padx=16, pady=(14, 0))
logo = tk.Canvas(ust, width=30, height=30, bg=BG, highlightthickness=0)
yuvarlak_dikdortgen(logo, 0, 0, 29, 29, 9, fill=VURGU, outline=VURGU)
logo.create_text(15, 15, text="LA", fill="white", font=(FONT, 9, "bold"))
logo.pack(side="left")
baslik = tk.Frame(ust, bg=BG)
baslik.pack(side="left", padx=(10, 0))
tk.Label(baslik, text="Lokal Asistan", bg=BG, fg=YAZI,
         font=(FONT, 12, "bold")).pack(anchor="w")
durum_kutusu = tk.Frame(baslik, bg=BG)
durum_kutusu.pack(anchor="w")
durum_nokta = tk.Canvas(durum_kutusu, width=8, height=8, bg=BG, highlightthickness=0)
durum_nokta.create_oval(0, 0, 7, 7, fill=YESIL, outline=YESIL, tags="nokta")
durum_nokta.pack(side="left", pady=(1, 0))
durum_etiketi = tk.Label(durum_kutusu, text="", bg=BG, fg=SOLUK, font=(FONT, 8))
durum_etiketi.pack(side="left", padx=(5, 0))
metin_dugme(ust, "Çıkış", cikis, hover=KIRMIZI).pack(side="right")
metin_dugme(ust, "Gizle", gizle).pack(side="right")

# Mod seçimi ve bilgi klasörü durumu
araclar = tk.Frame(pencere, bg=BG)
araclar.pack(fill="x", padx=16, pady=(12, 8))
mod_dugmeleri = {
    "ekran": mod_dugmesi(araclar, "Ekran", yeni_ekran),
    "roadmap": mod_dugmesi(araclar, "Roadmap", roadmap_modu),
}
mod_dugmeleri["ekran"].pack(side="left")
mod_dugmeleri["roadmap"].pack(side="left", padx=(6, 0))
metin_dugme(araclar, "↻", rag_yenile, boyut=10).pack(side="right")
rag_etiketi = tk.Label(araclar, text="", bg=BG, fg=SOLUK, font=(FONT, 8))
rag_etiketi.pack(side="right")

tk.Frame(pencere, bg=KENAR, height=1).pack(fill="x")

# Gizli token paneli (Ctrl+Alt+D ile açılır/kapanır, varsayılan gizli)
panel = tk.Frame(pencere, bg=KOD_BG, highlightbackground=VURGU, highlightthickness=1)
panel_metin = tk.Label(
    panel, text="", bg=KOD_BG, fg=YAZI, font=(KOD_FONT, 9),
    justify="left", anchor="w", padx=10, pady=8,
)
panel_metin.pack(fill="x")

# Sohbet alanı: kaydırılabilir canvas içinde her mesaj ayrı bir satır
stil = ttk.Style()
stil.theme_use("clam")
stil.layout("Koyu.Vertical.TScrollbar", [(
    "Vertical.Scrollbar.trough",
    {"children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})],
     "sticky": "ns"},
)])
stil.configure("Koyu.Vertical.TScrollbar", troughcolor=BG, background=KENAR,
               bordercolor=BG, lightcolor=KENAR, darkcolor=KENAR, width=8)
stil.map("Koyu.Vertical.TScrollbar", background=[("active", PANEL_HOVER)])

orta = tk.Frame(pencere, bg=BG)
orta.pack(fill="both", expand=True)
kaydirma = ttk.Scrollbar(orta, orient="vertical", style="Koyu.Vertical.TScrollbar")
kaydirma.pack(side="right", fill="y", padx=(0, 3), pady=4)
sohbet = tk.Canvas(orta, bg=BG, highlightthickness=0, bd=0,
                   yscrollcommand=kaydirma.set)
sohbet.pack(side="left", fill="both", expand=True)
ic = tk.Frame(sohbet, bg=BG)
ic_oge = sohbet.create_window(0, 0, window=ic, anchor="nw")


def kaydir_cubuk(*args):
    global takip
    sohbet.yview(*args)
    takip = sohbet.yview()[1] >= 0.999


def tekerlek(e):
    global takip
    if sohbet.yview() != (0.0, 1.0):
        sohbet.yview_scroll(int(-e.delta / 120) * 3, "units")
    takip = sohbet.yview()[1] >= 0.999


son_bolge = [None]


def bolge_guncelle(e=None):
    # İçerik görünür alanın dışına kayınca <Configure> gelmeyebiliyor; bu yüzden
    # kuyruk_kontrol her turda da çağırır, istenen yüksekliğe göre günceller.
    bolge = (0, 0, sohbet.winfo_width(), max(ic.winfo_reqheight(), sohbet.winfo_height()))
    if bolge != son_bolge[0]:
        son_bolge[0] = bolge
        sohbet.configure(scrollregion=bolge)
        if takip:
            sohbet.yview_moveto(1.0)


def sohbet_degisti(e):
    sohbet.itemconfig(ic_oge, width=e.width)
    for lbl, oran in sarilanlar:
        if lbl.winfo_exists():
            lbl.config(wraplength=max(int(e.width * oran), 120))


kaydirma.config(command=kaydir_cubuk)
sohbet.config(yscrollincrement=12)
ic.bind("<Configure>", bolge_guncelle)
sohbet.bind("<Configure>", sohbet_degisti)


def tekerlek_yonlendir(e):
    if e.widget is not giris:   # giriş kutusu kendi kaydırmasını yapar
        tekerlek(e)


pencere.bind_all("<MouseWheel>", tekerlek_yonlendir)

# Giriş alanı: yuvarlak kutu, yazdıkça büyüyen metin alanı ve gönder düğmesi
alt = tk.Frame(pencere, bg=BG)
alt.pack(fill="x", padx=14, pady=(8, 4))
giris_kutu = Kutu(alt, PANEL, yaricap=18, bosluk=(14, 8), genis=True)
giris_kutu.pack(fill="x")
giris = tk.Text(
    giris_kutu.ic, height=1, wrap="word", bg=PANEL, fg=YAZI,
    insertbackground=YAZI, bd=0, relief="flat", padx=0, pady=6,
    font=(FONT, 11), highlightthickness=0, selectbackground=VURGU,
)
gonder_dugmesi = tk.Canvas(giris_kutu.ic, width=34, height=34, bg=PANEL,
                           highlightthickness=0, cursor="hand2")
gonder_dugmesi.create_oval(1, 1, 33, 33, fill=VURGU, outline=VURGU, tags="daire")
gonder_dugmesi.create_text(18, 17, text="➤", fill="white", font=(FONT, 12), tags="ok")
gonder_dugmesi.pack(side="right", anchor="s", padx=(8, 0))
giris.pack(side="left", fill="x", expand=True)
yer_tutucu = tk.Label(giris, text="Bir şey sor…", bg=PANEL, fg=SOLUK, font=(FONT, 11))
yer_tutucu.bind("<Button-1>", lambda e: giris.focus_set())


def giris_degisti(e=None):
    """Yazı uzadıkça kutuyu büyütür (en fazla 6 satır), boşken yer tutucu gösterir."""
    satir_sayisi = sayi(giris, "update", "displaylines")
    giris.config(height=min(max(satir_sayisi, 1), 6))
    if giris.get("1.0", "end-1c"):
        yer_tutucu.place_forget()
    else:
        yer_tutucu.place(x=0, y=6)


gonder_durumu = {"mesgul": None}


def gonder_dugmesi_guncelle():
    m = mesgul.is_set()
    if gonder_durumu["mesgul"] == m:
        return
    gonder_durumu["mesgul"] = m
    renk = KENAR if m else VURGU
    gonder_dugmesi.itemconfig("daire", fill=renk, outline=renk)
    gonder_dugmesi.config(cursor="arrow" if m else "hand2")


gonder_dugmesi.bind("<Button-1>", gonder)
gonder_dugmesi.bind("<Enter>", lambda e: mesgul.is_set() or gonder_dugmesi.itemconfig(
    "daire", fill=VURGU_HOVER, outline=VURGU_HOVER))
gonder_dugmesi.bind("<Leave>", lambda e: mesgul.is_set() or gonder_dugmesi.itemconfig(
    "daire", fill=VURGU, outline=VURGU))

ipucu = tk.Frame(pencere, bg=BG)
ipucu.pack(fill="x", padx=18, pady=(2, 10))
tk.Label(
    ipucu, text=f"Enter gönder  ·  Shift+Enter yeni satır  ·  {KISAYOL.upper()} yeni ekran",
    bg=BG, fg=SOLUK, font=(FONT, 8),
).pack(side="left")
tk.Checkbutton(
    ipucu, text="Hep üstte", variable=ustte, command=ustte_degisti,
    bg=BG, fg=SOLUK, selectcolor=PANEL, activebackground=BG,
    activeforeground=YAZI, bd=0, highlightthickness=0, font=(FONT, 8),
    cursor="hand2",
).pack(side="right")

giris.bind("<Return>", gonder)
giris.bind("<Shift-Return>", lambda e: None)   # Shift+Enter: yeni satır
giris.bind("<KeyRelease>", giris_degisti)
giris.bind("<Configure>", giris_degisti)
pencere.bind("<Escape>", gizle)
pencere.protocol("WM_DELETE_WINDOW", gizle)

# ---------- Başlat ----------
keyboard.add_hotkey(KISAYOL, kisayol_basildi)
keyboard.add_hotkey(PANEL_KISAYOL, panel_kisayol_basildi)
mesajlar.append({"role": "system", "content": SISTEM})
karsilama()
mod_guncelle()
durum_yaz("Hazır")
giris_degisti()
pencere.after(100, kuyruk_kontrol)
rag_yenile()
giris.focus_set()
pencere.mainloop()
