#!/usr/bin/env bash
# Rebuild src/musicbot/mediactl.dex from MediaCtl.java.
# Needs only a JDK: android.jar (API 33) and D8 are fetched into a cache dir on
# first run, so there's no Android SDK to install.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/musicbot-mediactl"
r8_version=9.4.17

mkdir -p "$cache"
if [[ ! -f $cache/android.jar ]]; then
    curl -fL -o "$cache/platform.zip" https://dl.google.com/android/repository/platform-33_r02.zip
    unzip -qjo "$cache/platform.zip" '*/android.jar' -d "$cache"
    rm "$cache/platform.zip"
fi
if [[ ! -f $cache/r8-$r8_version.jar ]]; then
    curl -fL -o "$cache/r8-$r8_version.jar" \
        "https://dl.google.com/android/maven2/com/android/tools/r8/$r8_version/r8-$r8_version.jar"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
javac --release 11 -nowarn -cp "$cache/android.jar" -d "$tmp/classes" "$here/MediaCtl.java"
java -cp "$cache/r8-$r8_version.jar" com.android.tools.r8.D8 \
    --release --min-api 33 --lib "$cache/android.jar" --output "$tmp" "$tmp"/classes/*.class
cp "$tmp/classes.dex" "$repo/src/musicbot/mediactl.dex"
echo "wrote $repo/src/musicbot/mediactl.dex"
