# tachimanga-tools

Инструменты для TachiManga: работа с библиотекой и резервными копиями, сборка и публикация расширений E-Hentai и Hitomi.

## Структура

- `backup-tools/` — извлечение ссылок, категоризация и восстановление библиотеки; Python, без сторонних пакетов.
- `extensions/` — расширения, Gradle и скрипты релиза.
- `docs/` — общие инструкции и описание миграции.
- `tests/` — проверки публикации на локальном Git-репозитории.

## Работа с резервными копиями

Положите `.tmb` в `backup-tools/import/`. Если файл один, путь можно не указывать.

```bash
python3 backup-tools/extract.py
python3 backup-tools/categorize.py --dry-run --no-api
```

Результаты сохраняются в `backup-tools/output/`. Полная инструкция: [работа с бэкапами](backup-tools/README.md).

## Расширения

Для сборки нужны Java 17+, Android SDK и прежний ключ подписи `extensions/signingkey.jks`.

```bash
cd extensions
source ~/.nsfw-ext.env
./scripts/release_repo.sh
```

Android SDK и Java должны быть доступны в окружении. [Настройка](extensions/docs/setup-ubuntu.md), [релизы](extensions/docs/workflow.md).

Адрес репозитория расширений в приложении:

```text
https://georgvadis.github.io/tachimanga-tools/index.min.json
```

Исходники находятся здесь; готовые расширения публикуются в ветку `gh-pages` этого же репозитория. Сборка и публикация остаются независимыми от Python-утилит. При переносе добавьте новый адрес в TachiManga, проверьте список расширений, затем удалите старый адрес.

## Проверки

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q backup-tools extensions/scripts
bash -n extensions/scripts/build_repo.sh extensions/scripts/publish_pages.sh extensions/scripts/release_repo.sh
```

## Локальные данные

Бэкапы, результаты, cookies, ключ подписи и кэши не входят в Git. После клонирования их нужно добавить локально. [Миграция и сохранение истории](docs/migration.md).
