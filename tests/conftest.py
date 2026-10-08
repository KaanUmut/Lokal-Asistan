"""pytest, tests/ klasöründen çalıştırıldığında rag.py'yi bulabilsin diye
proje kök klasörünü (bu dosyanın bir üstünü) arama yoluna ekler."""
import pathlib
import sys

KOK = pathlib.Path(__file__).resolve().parent.parent
if str(KOK) not in sys.path:
    sys.path.insert(0, str(KOK))
