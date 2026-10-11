# nsfw-ext

Мини-репозиторий расширений `E-Hentai` и `Hitomi` с локальной сборкой и публикацией в `gh-pages`.

## Быстрый старт

Если окружение уже настроено:

```bash
source ~/.bashrc
source ~/.nsfw-ext.env
./scripts/release_repo.sh
```

После публикации URL репозитория:

```text
https://georgvadis.github.io/nsfw-ext/index.min.json
```

## Основные команды

Локальная сборка:

```bash
./scripts/build_repo.sh
```

Публикация уже собранного `repo/`:

```bash
./scripts/publish_pages.sh
```

Полный релиз одной командой:

```bash
./scripts/release_repo.sh
```

## Документация

- [Полная настройка Ubuntu без GUI](docs/setup-ubuntu.md)
- [Ежедневный workflow и релизы](docs/workflow.md)

## Важно

- `signingkey.jks` должен лежать в корне проекта и не должен попадать в git
- для обновлений нужно всегда использовать один и тот же ключ подписи
- перед каждым релизом повышайте `extVersionCode` в нужном расширении
