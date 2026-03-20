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