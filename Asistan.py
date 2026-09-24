"""Lokal Asistan: kısayolla ekran görüntüsü al, ekrana dair sohbet et."""
import queue
import threading
import tkinter as tk
from pathlib import Path

import keyboard
import mss
import mss.tools
import ollama

try:
    from PIL import Image
except ImportError:  # Pillow yoksa görüntü küçültülmeden gönderilir
    Image = None

# ---------- Ayarlar ----------
KLASOR = Path(__file__).parent
RESIM = KLASOR / "ekran.png"

MODEL = "qwen2.5vl:7b"
KISAYOL = "ctrl+alt+s"
GENISLIK = 440        # pencere genişliği (piksel)
ALT_BOSLUK = 60       # görev çubuğuna yer bırak
MAX_KENAR = 1400      # ekran görüntüsü en fazla bu kadar piksel olur

SISTEM = (
    "Sen kullanıcının ekran görüntüsünü görebilen bir yardımcı asistansın. "
    "Yalnızca Türkçe yaz; Çince, Japonca gibi başka dillerin karakterlerini "
    "asla kullanma. Teknik terimleri (float, string, print gibi) olduğu gibi "
    "İngilizce bırak. Soruyu ekrandaki içeriğe dayanarak, kısa ve adım adım "
    "cevapla. Ekranda görmediğin şeyi uydurma; emin değilsen söyle."
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
mesajlar = []                 # modele gönderilen sohbet geçmişi
durum = {"resim_bekliyor": False}


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
    durum_etiketi.config(text=metin)


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


# ---------- Ekran görüntüsü ----------
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
    mesajlar.clear()
    mesajlar.append({"role": "system", "content": SISTEM})
    durum["resim_bekliyor"] = True
    sohbeti_temizle()
    yaz("Ekran görüntüsü alındı. Ne öğrenmek istediğini yaz.\n\n", "not")
    durum_yaz("Ekran hazır")
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

    mesgul.set()
    durum_yaz("Düşünüyor...")
    yaz("Asistan\n", "rol_ai")
    threading.Thread(target=modele_sor, args=(list(mesajlar),), daemon=True).start()
    return "break"


def modele_sor(gecmis):
    """Ayrı thread'de çalışır; cevabı parça parça kuyruğa bırakır."""
    tam = ""
    try:
        for parca in ollama.chat(model=MODEL, messages=gecmis, stream=True, options={"temperature": 0.3}):
            metin = parca["message"]["content"]
            tam += metin
            kuyruk.put(("parca", metin))
        kuyruk.put(("bitti", tam))
    except Exception as e:
        kuyruk.put(("hata", str(e)))


def kuyruk_kontrol():
    try:
        while True:
            komut, veri = kuyruk.get_nowait()
            if komut == "yeni":
                yeni_ekran()
            elif komut == "parca":
                yaz(veri)
            elif komut == "bitti":
                mesajlar.append({"role": "assistant", "content": veri})
                yaz("\n\n")
                mesgul.clear()
                durum_yaz("Hazır")
            elif komut == "hata":
                yaz(f"\nHata: {veri}\n\n", "not")
                if mesajlar and mesajlar[-1]["role"] == "user":
                    son = mesajlar.pop()
                    if "images" in son:
                        durum["resim_bekliyor"] = True
                mesgul.clear()
                durum_yaz("Hata")
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
        relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
        font=(FONT, 9), **kw,
    )


# Üst bar
ust = tk.Frame(pencere, bg=BG)
ust.pack(fill="x", padx=12, pady=(12, 6))
tk.Label(ust, text="Lokal Asistan", bg=BG, fg=YAZI,
         font=(FONT, 13, "bold")).pack(side="left")
durum_etiketi = tk.Label(ust, text="Hazır", bg=BG, fg=SOLUK, font=(FONT, 9))
durum_etiketi.pack(side="left", padx=10)
dugme(ust, "Çıkış", cikis).pack(side="right")
dugme(ust, "Gizle", gizle).pack(side="right", padx=6)
dugme(ust, "Yeni ekran", yeni_ekran).pack(side="right")

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
    ipucu, text=f"Enter: gönder  •  Shift+Enter: yeni satır  •  {KISAYOL.upper()}: yeni ekran",
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
yaz(f"Hazır. Ekranı sormak için {KISAYOL.upper()} tuşlarına bas.\n\n", "not")
pencere.after(100, kuyruk_kontrol)
giris.focus_set()
pencere.mainloop()