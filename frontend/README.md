# Панель оператора (React + TypeScript)

Заплановано на **Milestone 3**. Стек: React 18 + TypeScript + Vite,
збірка віддається nginx як статика (`/usr/share/nginx/html`).

Сторінки за планом:

- список пристроїв з онлайн/офлайн статусом і пошуком;
- деталі пристрою: телеметрія, застосунки, файли, геолокація;
- enrollment-токени з QR-кодом;
- аудит-журнал із фільтрами;
- адміністративні дії (reboot, sync, kiosk) з підтвердженням;
- сеанси віддаленої підтримки.

Автентифікація: `POST /api/v1/auth/login` → access-токен у памʼяті,
refresh — у `httpOnly`-cookie або secure storage, автоматичне поновлення
через `/auth/refresh` (ротація описана в [docs/SECURITY.md](../docs/SECURITY.md)).

Контракт API — [docs/API.md](../docs/API.md).
