"""Lokal Asistan: kısayolla ekran görüntüsü al, ekrana dair sohbet et.
Roadmap modunda bilgi/ klasöründeki kaynaklara (RAG) dayanarak yol haritası çıkarır."""
import queue
import threading
import tkinter as tk
from pathlib import Path

import keyboard
import mss
import mss.tools
import ollama

import rag

try:
    from PIL import Image
except ImportError:  # Pillow yoksa görüntü küçültülmeden gönderilir
    Image = None

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
BG = "#1e1f22"
PANEL = "#2b2d31"
YAZI = "#e6e6e6"
SOLUK = "#9aa0a6"
VURGU = "#5c7cfa"
YESIL = "#51cf66"
FONT = "Segoe UI"

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


# ---------- Arayüz yardımcıları ----------
def yaz(metin, etiket="metin"):
    sohbet.config(state="normal")
    sohbet.insert("end", metin, etiket)
    sohbet.config(state="disabled")
    sohbet.see("end")


def sohbeti_temizle():
    sohbet.config(state="normal")
    sohbet.delete("1.0", "end")
    sohbet.config(state="disabled")


def durum_yaz(metin):
    durum_etiketi.config(text=f"{metin}  •  {durum['rag']}")


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
        yaz(f"Ekran alınamadı: {e}\n\n", "not")
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
    yaz("Ekran görüntüsü alındı. Ne öğrenmek istediğini yaz.\n\n", "not")
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
    yaz(
        "Roadmap modu. Hedefini (örneğin AI stajı), şu anki seviyeni ve "
        "haftalık ayırabileceğin süreyi yaz. Yol haritasını bilgi klasöründeki "
        "kaynaklara dayanarak çıkaracağım.\n\n",
        "not",
    )
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

    yaz("Sen\n", "rol_sen")
    yaz(soru + "\n\n", "metin")

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
    yaz("Asistan\n", "rol_ai")
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
    try:
        while True:
            komut, veri = kuyruk.get_nowait()
            if komut == "yeni":
                yeni_ekran()
            elif komut == "parca":
                yaz(veri)
            elif komut == "kaynak":
                yaz("\n\n" + veri, "not")
            elif komut == "token":
                gecmis_token.append(veri)
                panel_guncelle()
            elif komut == "bitti":
                mesajlar.append({"role": "assistant", "content": veri})
                yaz("\n\n")
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
                yaz(f"[Sohbet geçmişi özetlendi: {n} mesaj → 1 özet]\n\n", "not")
                panel_guncelle()
            elif komut == "hata":
                yaz(f"\nHata: {veri}\n\n", "not")
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
                yaz(veri + "\n\n", "not")
            elif komut == "panel":
                panel_ac_kapa()
    except queue.Empty:
        pass
    pencere.after(100, kuyruk_kontrol)


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


def dugme(ust, metin, komut, **kw):
    return tk.Button(
        ust, text=metin, command=komut, bg=PANEL, fg=YAZI,
        activebackground=VURGU, activeforeground="white",
        relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
        font=(FONT, 9), **kw,
    )


# Üst bar
ust = tk.Frame(pencere, bg=BG)
ust.pack(fill="x", padx=12, pady=(12, 2))
tk.Label(ust, text="Lokal Asistan", bg=BG, fg=YAZI,
         font=(FONT, 13, "bold")).pack(side="left")
dugme(ust, "Çıkış", cikis).pack(side="right")
dugme(ust, "Gizle", gizle).pack(side="right", padx=4)
dugme(ust, "Roadmap", roadmap_modu).pack(side="right")
dugme(ust, "Ekran", yeni_ekran).pack(side="right", padx=4)

# Durum satırı
durum_satiri = tk.Frame(pencere, bg=BG)
durum_satiri.pack(fill="x", padx=12, pady=(0, 6))
durum_etiketi = tk.Label(durum_satiri, text="", bg=BG, fg=SOLUK,
                         font=(FONT, 9), anchor="w")
durum_etiketi.pack(side="left")
dugme(durum_satiri, "Bilgiyi yenile", rag_yenile).pack(side="right")

# Gizli token paneli (Ctrl+Alt+D ile açılır/kapanır, varsayılan gizli)
panel = tk.Frame(pencere, bg="#16171a", highlightbackground=VURGU, highlightthickness=1)
panel_metin = tk.Label(
    panel, text="", bg="#16171a", fg=YAZI, font=("Consolas", 9),
    justify="left", anchor="w", padx=10, pady=8,
)
panel_metin.pack(fill="x")

# Sohbet alanı
orta = tk.Frame(pencere, bg=BG)
orta.pack(fill="both", expand=True, padx=12)
kaydirma = tk.Scrollbar(orta, troughcolor=BG, bd=0)
kaydirma.pack(side="right", fill="y")
sohbet = tk.Text(
    orta, wrap="word", bg=PANEL, fg=YAZI, bd=0, relief="flat",
    padx=12, pady=10, font=(FONT, 11), state="disabled",
    yscrollcommand=kaydirma.set, highlightthickness=0, cursor="arrow",
)
sohbet.pack(side="left", fill="both", expand=True)
kaydirma.config(command=sohbet.yview)
sohbet.tag_config("rol_sen", foreground=VURGU, font=(FONT, 10, "bold"), spacing1=4)
sohbet.tag_config("rol_ai", foreground=YESIL, font=(FONT, 10, "bold"), spacing1=4)
sohbet.tag_config("metin", foreground=YAZI, lmargin1=4, lmargin2=4, spacing3=2)
sohbet.tag_config("not", foreground=SOLUK, font=(FONT, 10, "italic"))

# Giriş alanı
alt = tk.Frame(pencere, bg=BG)
alt.pack(fill="x", padx=12, pady=(8, 4))
giris = tk.Text(
    alt, height=3, wrap="word", bg=PANEL, fg=YAZI, insertbackground=YAZI,
    bd=0, relief="flat", padx=10, pady=8, font=(FONT, 11),
    highlightthickness=1, highlightbackground=PANEL, highlightcolor=VURGU,
)
giris.pack(side="left", fill="x", expand=True)
gonder_dugmesi = tk.Button(
    alt, text="Gönder", command=gonder, bg=VURGU, fg="white",
    activebackground="#748ffc", activeforeground="white", relief="flat",
    bd=0, padx=14, pady=8, cursor="hand2", font=(FONT, 10, "bold"),
)
gonder_dugmesi.pack(side="right", padx=(8, 0), fill="y")

ipucu = tk.Frame(pencere, bg=BG)
ipucu.pack(fill="x", padx=12, pady=(0, 10))
tk.Label(
    ipucu, text=f"Enter: gönder  •  Shift+Enter: yeni satır  •  {KISAYOL.upper()}: yeni ekran  •  {PANEL_KISAYOL.upper()}: token paneli",
    bg=BG, fg=SOLUK, font=(FONT, 8),
).pack(side="left")
tk.Checkbutton(
    ipucu, text="Hep üstte", variable=ustte, command=ustte_degisti,
    bg=BG, fg=SOLUK, selectcolor=PANEL, activebackground=BG,
    activeforeground=YAZI, bd=0, highlightthickness=0, font=(FONT, 8),
).pack(side="right")

giris.bind("<Return>", gonder)
giris.bind("<Shift-Return>", lambda e: None)   # Shift+Enter: yeni satır
pencere.bind("<Escape>", gizle)
pencere.protocol("WM_DELETE_WINDOW", gizle)

# ---------- Başlat ----------
keyboard.add_hotkey(KISAYOL, kisayol_basildi)
keyboard.add_hotkey(PANEL_KISAYOL, panel_kisayol_basildi)
mesajlar.append({"role": "system", "content": SISTEM})
yaz(
    f"Hazır. Ekranı sormak için {KISAYOL.upper()}, yol haritası için "
    "Roadmap düğmesine bas.\n\n",
    "not",
)
durum_yaz("Hazır")
pencere.after(100, kuyruk_kontrol)
rag_yenile()
giris.focus_set()
pencere.mainloop()
