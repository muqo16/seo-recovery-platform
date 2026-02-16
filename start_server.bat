@echo off
cd /d %~dp0

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Sanal ortam bulunamadi, olusturuluyor...
  python -m venv .venv
)

echo [INFO] Bagimliliklar kontrol ediliyor...
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

echo [INFO] Sunucu baslatiliyor: http://127.0.0.1:8000
echo [INFO] Kapatmak icin bu pencereyi kapat veya Ctrl+C yap.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
