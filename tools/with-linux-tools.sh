#!/bin/bash
# Expose portable tools at Gear's fixed /usr/bin PATH inside a temporary mount namespace.
# The host /usr/bin and original Gear files remain unchanged.
set -euo pipefail
cd "$(dirname "$0")/.."
portable="$PWD/.runtime/linux-tools"
merged="$portable/merged-bin"
mkdir -p "$merged/.lib"
for path in /usr/bin/*; do
  name="${path##*/}"
  if [ ! -e "$merged/$name" ] && [ ! -L "$merged/$name" ]; then
    ln -s "/usr/local/lib/evotrace-system-bin/$name" "$merged/$name"
  fi
done
for pair in 'bwrap:bwrap' 'rg:rg' 'socat:.socat-real'; do
  src="${pair%%:*}"; dest="${pair#*:}"
  if ! cmp -s "$portable/usr/bin/$src" "$merged/$dest"; then
    cp "$portable/usr/bin/$src" "$merged/$dest"
  fi
done
if ! cmp -s "$portable/usr/lib/x86_64-linux-gnu/libwrap.so.0" "$merged/.lib/libwrap.so.0"; then
  cp -L "$portable/usr/lib/x86_64-linux-gnu/libwrap.so.0" "$merged/.lib/libwrap.so.0"
fi
if [ -L "$merged/socat" ]; then unlink "$merged/socat"; fi
if [ ! -f "$merged/socat" ]; then
cat > "$merged/socat" <<'EOF'
#!/bin/bash
export LD_LIBRARY_PATH="/usr/bin/.lib:${LD_LIBRARY_PATH:-}"
exec /usr/bin/.socat-real "$@"
EOF
chmod +x "$merged/socat"
fi
exec "$portable/usr/bin/bwrap" --dev-bind / / --tmpfs /usr/local/lib \
  --ro-bind /usr/bin /usr/local/lib/evotrace-system-bin \
  --ro-bind "$merged" /usr/bin -- "$@"
