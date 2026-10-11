# Настройка Ubuntu Без GUI

Эта инструкция покрывает полный путь: подготовка окружения, первая сборка, первая публикация и подключение репозитория в `Tachimanga`.

## 1. Установить системные пакеты

```bash
sudo apt update
sudo apt install -y openjdk-17-jdk unzip rsync git curl gh
```

Проверка:

```bash
java -version
javac -version
keytool -help >/dev/null && echo keytool-ok
gh --version
```

## 2. Установить Android SDK Command-Line Tools

```bash
cd ~/Downloads
curl -L -o commandlinetools-linux-14742923_latest.zip \
  "https://dl.google.com/android/repository/commandlinetools-linux-14742923_latest.zip"

mkdir -p "$HOME/Android/Sdk/cmdline-tools"
rm -rf /tmp/android-cmdline-tools
mkdir -p /tmp/android-cmdline-tools

unzip -q commandlinetools-linux-14742923_latest.zip -d /tmp/android-cmdline-tools
rm -rf "$HOME/Android/Sdk/cmdline-tools/latest"
mv /tmp/android-cmdline-tools/cmdline-tools "$HOME/Android/Sdk/cmdline-tools/latest"
```

## 3. Прописать переменные окружения

```bash
cat >> ~/.bashrc <<'EOF'
export ANDROID_HOME="$HOME/Android/Sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")"
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$JAVA_HOME/bin:$PATH"
EOF

source ~/.bashrc
```

Проверка:

```bash
echo "$ANDROID_HOME"
echo "$JAVA_HOME"
sdkmanager --version
```

## 4. Установить Android SDK пакеты под проект

Проект использует `compileSdk = 34`, поэтому нужен именно `android-34`.

```bash
sdkmanager --sdk_root="$ANDROID_HOME" \
  "cmdline-tools;latest" \
  "platform-tools" \
  "platforms;android-34" \
  "build-tools;34.0.0"

yes | sdkmanager --sdk_root="$ANDROID_HOME" --licenses
```

Проверка:

```bash
ls "$ANDROID_HOME/build-tools/34.0.0/aapt"
ls "$ANDROID_HOME/build-tools/34.0.0/apksigner"
```

Если после этого Gradle пишет про `latest-2`, уберите дублирующий каталог:

```bash
rm -rf "$HOME/Android/Sdk/cmdline-tools/latest-2"
```

## 5. Создать ключ подписи

Ключ должен лежать в корне проекта и оставаться одним и тем же для всех будущих обновлений.

```bash
cd /srv/data/projects/nsfw-ext
keytool -genkeypair -v \
  -keystore signingkey.jks \
  -alias nsfw-ext \
  -keyalg RSA \
  -keysize 2048 \
  -validity 3650
```

Рекомендуемый вариант:

- `alias`: `nsfw-ext`
- пароль ключа и пароль keystore сделать одинаковыми

## 6. Сохранить переменные подписи

```bash
cat > ~/.nsfw-ext.env <<'EOF'
export ALIAS="nsfw-ext"
export KEY_STORE_PASSWORD="ЗАМЕНИ_НА_СВОЙ_ПАРОЛЬ"
export KEY_PASSWORD="ЗАМЕНИ_НА_СВОЙ_ПАРОЛЬ"
EOF

chmod 600 ~/.nsfw-ext.env
grep -qxF 'source ~/.nsfw-ext.env' ~/.bashrc || echo 'source ~/.nsfw-ext.env' >> ~/.bashrc
source ~/.nsfw-ext.env
```

## 7. Первая локальная сборка

```bash
cd /srv/data/projects/nsfw-ext
source ~/.bashrc
source ~/.nsfw-ext.env
./scripts/build_repo.sh
```

После успешной сборки должны появиться:

- `repo/index.json`
- `repo/index.min.json`
- `repo/index.html`
- `repo/repo.json`
- `repo/apk/*.apk`

Локальная проверка:

```bash
python3 -m http.server 8000 --directory repo
```

## 8. Отправить код в GitHub

Если remote уже настроен:

```bash
cd /srv/data/projects/nsfw-ext
git push origin main
```

Если ещё не настроен:

```bash
cd /srv/data/projects/nsfw-ext
gh auth login
gh repo create Georgvadis/nsfw-ext --public --source=. --remote=origin --push
```

## 9. Первый релиз в gh-pages

```bash
cd /srv/data/projects/nsfw-ext
source ~/.bashrc
source ~/.nsfw-ext.env
./scripts/release_repo.sh
```

Скрипт:

1. собирает APK
2. генерирует `repo/`
3. создаёт или обновляет ветку `gh-pages`
4. пушит её в GitHub

## 10. Включить GitHub Pages

На GitHub:

1. откройте репозиторий `Georgvadis/nsfw-ext`
2. перейдите в `Settings -> Pages`
3. выберите `Deploy from a branch`
4. выберите ветку `gh-pages`
5. выберите папку `/(root)`
6. нажмите `Save`

После публикации репозиторий расширений будет доступен по адресу:

```text
https://georgvadis.github.io/nsfw-ext/index.min.json
```

## 11. Подключить репозиторий в Tachimanga

В `Tachimanga`:

1. откройте `More -> Extensions -> Extension repositories`
2. добавьте URL:

```text
https://georgvadis.github.io/nsfw-ext/index.min.json
```

Если Pages ещё не поднялись или приложение ругается, временный fallback:

```text
https://raw.githubusercontent.com/Georgvadis/nsfw-ext/gh-pages/index.min.json
```

## 12. Что важно не сломать

- не коммитьте `signingkey.jks`
- не теряйте `signingkey.jks`
- не меняйте ключ подписи между релизами
- не забывайте повышать `extVersionCode`
- если в `Tachimanga` уже стоят старые версии этих же расширений из другого репозитория, лучше удалить их перед первой установкой ваших сборок
