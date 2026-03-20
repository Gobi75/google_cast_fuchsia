# Google Cast (Fuchsia & Nest Hub 2 Optimized)

To jest **testowa wersja (fork)** integracji bazująca na projekcie `continuously_casting_dashboards`. 

### 🎯 Cel projektu
Głównym celem tej modyfikacji jest rozwiązanie problemów ze stabilnością wyświetlania Dashboardów na urządzeniach **Nest Hub 2** z systemem **Fuchsia OS**, które często ignorują standardowe polecenia `cast_site` lub "zasypiają".

### ✨ Kluczowe poprawki (v2.0.9):
* **Strategia Double-Tap:** Automatyczne podwójne wysyłanie komendy startowej. Pierwszy strzał "budzi" urządzenie, drugi (po krótkiej przerwie) faktycznie ładuje URL.
* **Konfigurowalne opóźnienia (UI):** Możliwość ustawienia czasu między próbami (`retry_delay`) oraz limitu czasu operacji (`casting_timeout`) bezpośrednio w opcjach integracji w Home Assistant.
* **Pancerne zarządzanie procesami:** Każdy proces `catt` jest teraz rejestrowany i bezpiecznie zamykany (timeout), co zapobiega "wyciekom" procesów w systemie.
* **Poprawiona obsługa głośności:** Logika, która najpierw wycisza urządzenie, a po udanym załadowaniu Dashboardu przywraca zapamiętany poziom głośności.

### 🛠 Instalacja przez HACS
1. W Home Assistant przejdź do **HACS** -> **Integracje**.
2. Kliknij trzy kropki w prawym górnym rogu i wybierz **Niestandardowe repozytoria**.
3. Wklej link do tego repozytorium: `https://github.com/Gobi75/google_cast_fuchsia`
4. Wybierz kategorię **Integracja** i kliknij **Dodaj**.
5. Pobierz integrację i zrestartuj Home Assistant.

### 👥 Autorzy i Podziękowania
* Oryginalny kod: `continuously_casting_dashboards` (Autor: **@b0mbays**).
* Modyfikacje pod Nest Hub 2: **Gobi75**.

---
*Uwaga: Jest to wersja eksperymentalna. Używasz jej na własną odpowiedzialność.*
