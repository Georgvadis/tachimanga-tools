# CLAUDE.md — Tachimanga backup internals

Заметки о структуре `.tmb`-бэкапов и логике облачной синхронизации Tachimanga.
Накоплены опытным путём, сервер не документирован публично.

## Формат `.tmb`

```
backup.tmb            (outer ZIP)
├── meta.json         (JSON с метаданными бэкапа)
└── contents.zip      (inner ZIP)
    ├── tachimanga.db    (SQLite, основная БД)
    ├── prefs/*.plist    (настройки источников)
    ├── extensions/*.jar (расширения-источники)
    ├── pref.json
    └── pref-all.json
```

`meta.json` содержит `size` (длина `contents.zip` в байтах) и `checksum` (SHA-1 от `contents.zip`). При любой модификации `contents.zip` оба поля **обязаны** пересчитываться, иначе приложение скажет "некорректный файл резервной копии".

## Ключевые таблицы и поля

| Таблица | Важные поля |
|---|---|
| `Category` | `is_delete`, `dirty`, `commit_id`, `name`, `order` |
| `CategoryManga` | `category`, `manga` (нет soft-delete полей) |
| `Manga` | `in_library`, `default_category`, `in_library_at`, `dirty`, `commit_id`, `source`, `url` |
| `Chapter` | `dirty`, `commit_id` |
| `SyncState` | `enable`, `max_commit_id`, `last_sync_time`, `email` |
| `SyncCommit` | история коммитов |
| `ChapterSync` | per-chapter sync state (read progress, last position) |

### Семантика sync-полей

- `dirty=1` — "запушить эту запись в облако при следующем синке"
- `dirty=0` — "локальное состояние совпадает с облаком (или не сихронизировалось ещё)"
- `commit_id > 0` — "облако знает эту запись по этому id, любое обновление прилетает как UPDATE"
- `commit_id = 0` — "новая запись, облако присвоит id при первом push (CREATE)"
- `is_delete=1, dirty=1, commit_id>0` — "стандартный маркер удаления, который ставит само приложение"
- `default_category=1` (на `Manga`) — "показывать в разделе 'По умолчанию'". Если манга добавлена в реальную категорию, нужно `=0`, иначе она дублируется и в букве, и в дефолтном разделе.

### Семантика `SyncState`

- `max_commit_id` — последний известный локально облачный commit_id. Pull тянет коммиты `> max_commit_id`.
- `last_sync_time` — таймштамп последней успешной синхронизации.
- `email` — аккаунт. Если удалить всю строку `SyncState`, приложение возьмёт текущий залогиненный аккаунт.

## Что облако делает при синке

1. **Pull**: возвращает коммиты с `id > max_commit_id`. Применяются локально, перетирая записи по `commit_id`.
2. **Push**: отправляет наверх все строки с `dirty=1`. Если `commit_id=0` — создаётся новая запись в облаке. Если `commit_id>0` — обновляется существующая по этому id.
3. **Конфликт-резолюция**: при конкуренции "облачный коммит новее" → побеждает облако. То есть push с `commit_id=0` для записи, которую облако уже знает, может быть проигнорирован или дать дубликат.

## Опытные наблюдения (важно)

- **Облако НЕ всегда чистится** при "удалить всё → синхронизировать" в приложении. Сервер часто хранит soft-deleted записи и при следующем синке тянет их назад с `commit_id` выше нашего `max_commit_id`.
- **`in_library` авторитарно у облака**. Если облако хранит мангу как `in_library=0` с `commit_id>наш`, то наш push `in_library=1, commit_id=0, dirty=1` будет перетёрт после ближайшего pull. Чтобы навязать своё, нужно пушить с **текущим cloud commit_id** + `dirty=1` — тогда оно идёт как UPDATE.
- **Системные категории (Запланировано/Прочитано/...) могут уехать в `is_delete=1`** в облаке. После рестора с такими маркерами приложение видит "нет системных категорий" → создаёт английские дефолтные → дубликаты.
- **Нет надёжного "локальное побеждает облако"**. Без доступа к серверу облако всегда может вернуть удалённое. Единственно надёжный путь — свежий аккаунт.
- **`max_commit_id=99999999`** не помог — pull всё равно тянул много данных. Похоже сервер не уважает фильтр на стороне клиента или возвращает всё state по другой логике.

## План действий, если нужно "перезалить" облако

### Сценарий A: тот же аккаунт, нужно обновить состояние

1. Запустить `./categorize.py` на свежем бэкапе. Скрипт уже:
   - Создаёт буквы A–Z + `#`
   - Soft-deletes старые per-artist категории (`is_delete=1, dirty=1`, сохраняя `commit_id`)
   - Помечает `dirty=1` для всего, что должно запушиться
   - Оставляет `enable=0` чтобы пользователь сам включил синк
2. Восстановить полученный `_by_artist.tmb`
3. Убедиться что залогинен в **тот же** аккаунт что и до бэкапа
4. Включить синк → push должен донести изменения и удаления

Риск: облако может перетереть `in_library` или системные категории, если у них в облаке более свежий `commit_id`.

### Сценарий B: облако уже перетёрло наше → восстановление состояния

Использовать `./restore_library.py`:

```bash
./restore_library.py <post_sync_backup>.tmb <good_source_backup>.tmb
```

Скрипт:
- Берёт из `<good_source>` список манг, которые должны быть в библиотеке, и в какой букве
- В `<post_sync>` ставит `in_library=1, default_category=0, dirty=1` для этих манг (сохраняя cloud `commit_id`)
- Пересоздаёт `CategoryManga` привязки
- Помечает буквенные категории `dirty=1`
- НЕ трогает системные русские категории (нужно отдельно — см. ниже)

Если системные русские категории в `post_sync` помечены `is_delete=1`, нужно вручную восстановить:

```python
UPDATE Category SET is_delete=0, dirty=1, update_at=<now>
WHERE name IN ('Запланировано','Прочитано','Брошенно','Читаю','Отложено')
```

### Сценарий C: переезд на свежий аккаунт (в облаке пусто гарантированно)

1. Создать новый аккаунт у Google/Apple, войти в него
2. Сделать "fresh upload" вариант бэкапа:
   - `DELETE FROM Category WHERE is_delete=1` (маркеры удаления не нужны — облаку нечего удалять)
   - Все таблицы: `commit_id=0, dirty=1`
   - `SyncState`: `enable=0, max_commit_id=0, last_sync_time=0`
   - `DELETE FROM SyncCommit; DELETE FROM ChapterSync`
   - `DELETE FROM SyncState` (опционально — чтобы приложение взяло новый email)
3. Восстановить, включить синк → cloud получит чистое состояние

## Полезные SQL-запросы для диагностики

```sql
-- Видимые категории и сколько в них манги
SELECT id, name,
       (SELECT COUNT(*) FROM CategoryManga WHERE category=Category.id) cnt,
       dirty, commit_id
FROM Category WHERE is_delete=0 ORDER BY "order";

-- Манги в библиотеке без категорий
SELECT id, title FROM Manga
WHERE in_library=1 AND id NOT IN (SELECT manga FROM CategoryManga);

-- Дубль "по умолчанию" + категория (default_category=1 при наличии CategoryManga)
SELECT m.id, m.title FROM Manga m
JOIN CategoryManga cm ON cm.manga=m.id
WHERE m.in_library=1 AND m.default_category=1;

-- Общее состояние синка
SELECT enable, max_commit_id, last_sync_time, email FROM SyncState;
```

## Что НЕ делать

- Не сбрасывать `commit_id` на 0 для записей, которые облако уже знает — push улетит как CREATE и появятся дубликаты, либо будет проигнорирован.
- Не делать жёсткий `DELETE FROM Category` для категорий с `commit_id>0` — облако при следующем pull их вернёт активными. Нужно `is_delete=1, dirty=1`.
- Не модифицировать `contents.zip` без обновления `meta.json:size/checksum` — приложение откажется грузить.
- Не пересохранять inner ZIP без сохранения структуры подпапок (`prefs/`, `extensions/`) — приложение скажет "некорректный файл".
