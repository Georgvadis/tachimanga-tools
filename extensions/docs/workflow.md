# Workflow И Релизы

## Обычный цикл работы

1. правите код расширения
2. повышаете `extVersionCode`
3. запускаете релиз
4. обновляете расширение в `Tachimanga`

## Что менять перед релизом

Для `E-Hentai`:

- `src/all/ehentai/build.gradle`

Для `Hitomi`:

- `src/all/hitomi/build.gradle`

Пример:

```groovy
extVersionCode = 28
```

Без увеличения `extVersionCode` приложение не увидит новую версию.

## Основные команды

Полный релиз:

```bash
cd /srv/data/projects/nsfw-ext
source ~/.bashrc
source ~/.nsfw-ext.env
./scripts/release_repo.sh
```

Только сборка:

```bash
./scripts/build_repo.sh
```

Только публикация уже собранного `repo/`:

```bash
./scripts/publish_pages.sh
```

## Что делает каждый скрипт

`scripts/build_repo.sh`

- собирает все расширения из `src/*/*`
- копирует APK в `repo/apk`
- запускает Inspector
- генерирует `index.json`, `index.min.json`, `index.html`, `repo.json`

`scripts/publish_pages.sh`

- синхронизирует содержимое `repo/` в отдельный worktree
- коммитит изменения в ветку `gh-pages`
- пушит `gh-pages` в `origin`

`scripts/release_repo.sh`

- вызывает `build_repo.sh`
- вызывает `publish_pages.sh`

## Проверка результата после релиза

Проверьте, что открываются:

```text
https://georgvadis.github.io/nsfw-ext/index.min.json
https://georgvadis.github.io/nsfw-ext/index.html
```

Либо fallback:

```text
https://raw.githubusercontent.com/Georgvadis/nsfw-ext/gh-pages/index.min.json
```

## Частые проблемы

Нет новой версии в приложении:

- не увеличен `extVersionCode`
- Pages ещё не обновились
- установлен APK с другой подписью

Сборка не стартует:

- не загружены `~/.bashrc` и `~/.nsfw-ext.env`
- отсутствует `ANDROID_HOME`
- отсутствует `signingkey.jks`

`Tachimanga` пишет, что репозиторий не найден:

- используется не `index.min.json`
- GitHub Pages ещё не включены
- лучше временно использовать raw fallback

## Минимальный чеклист перед каждым релизом

```bash
cd /srv/data/projects/nsfw-ext
source ~/.bashrc
source ~/.nsfw-ext.env
./scripts/release_repo.sh
```

После этого:

1. откройте `index.min.json` в браузере или через `curl`
2. проверьте обновление в `Tachimanga`
