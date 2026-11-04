#!/usr/bin/env bash
# build the app and package it as a dmg
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
architecture="${1:-$(uname -m)}"
case "$architecture" in
  arm64|x86_64) ;;
  *)
    echo "Unsupported macOS architecture: $architecture" >&2
    exit 2
    ;;
esac

app_name="NUP Packer"
bundle_id="me.xdan.nuppacker"
build_dir="$root/build/macos-$architecture"
dist_dir="$root/dist"
app_path="$build_dir/dist/$app_name.app"
dmg_root="$build_dir/dmg-root"
dmg_path="$dist_dir/NUP-Packer-macos-$architecture.dmg"
iconset="$build_dir/NUP-Packer.iconset"
icon_path="$build_dir/NUP-Packer.icns"

rm -rf "$build_dir"
rm -f "$dmg_path"
mkdir -p "$build_dir" "$dist_dir" "$iconset"

# resize the icon for finder
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$root/packaging/nuppacker.png" \
    --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double_size=$((size * 2))
  sips -z "$double_size" "$double_size" "$root/packaging/nuppacker.png" \
    --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil --convert icns "$iconset" --output "$icon_path"

uv run pyinstaller "$root/packaging/launcher.py" \
  --name "$app_name" \
  --windowed \
  --onedir \
  --noconfirm \
  --clean \
  --paths "$root/src" \
  --collect-submodules nuppacker \
  --icon "$icon_path" \
  --osx-bundle-identifier "$bundle_id" \
  --target-architecture "$architecture" \
  --distpath "$build_dir/dist" \
  --workpath "$build_dir/work" \
  --specpath "$build_dir/spec"

# sign the app so macos can verify it
codesign --force --deep --sign - "$app_path"
codesign --verify --deep --strict "$app_path"

# release signing and notarization need apple credentials
mkdir -p "$dmg_root"
cp -R "$app_path" "$dmg_root/$app_name.app"
ln -s /Applications "$dmg_root/Applications"
hdiutil create \
  -volname "$app_name" \
  -srcfolder "$dmg_root" \
  -ov \
  -format UDZO \
  "$dmg_path"
hdiutil verify "$dmg_path"

echo "Created $dmg_path"
