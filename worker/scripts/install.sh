#!/bin/sh
# boost installer.
#
#   curl -fsSL https://kojesko.dev/boost | sh
#
# Fetches the CLI from the same Worker that serves this script and drops it on
# PATH. Deliberately POSIX sh with no dependency beyond curl: the whole point of
# a `curl | sh` install is that it works on a machine you have not set up yet.
#
# Honoured environment variables:
#   BOOST_API      base URL to install from (default https://kojesko.dev/boost)
#   BOOST_BIN_DIR  where to place the binary (default ~/.local/bin)

set -eu

BOOST_API="${BOOST_API:-https://kojesko.dev/boost}"
BIN_DIR="${BOOST_BIN_DIR:-$HOME/.local/bin}"
TARGET="$BIN_DIR/boost"

# When this script is itself piped into sh, stdin is the script -- so anything
# interactive would read the remaining bytes of the installer. Never prompt.
say()  { printf '%s\n' "$*"; }
die()  { printf 'install: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required but was not found."

mkdir -p "$BIN_DIR" || die "could not create $BIN_DIR"

TMP=$(mktemp "${TMPDIR:-/tmp}/boost.XXXXXX") || die "could not create a temp file"
# Clean up on every exit path, including a failed download.
trap 'rm -f "$TMP"' EXIT INT TERM

say "Fetching boost from $BOOST_API/bin ..."
curl -fsSL "$BOOST_API/bin" -o "$TMP" || die "download failed from $BOOST_API/bin"

# Guard against a proxy or captive portal handing back an HTML error page: a
# truncated or wrong payload would otherwise be installed and fail confusingly
# later. Checking the shebang is cheap and catches essentially all of it.
[ -s "$TMP" ] || die "downloaded an empty file"
head -n 1 "$TMP" | grep -q '^#!' || die "downloaded file is not a script (is $BOOST_API correct?)"

chmod 0755 "$TMP"
mv "$TMP" "$TARGET" || die "could not write $TARGET"
trap - EXIT INT TERM

VERSION=$("$TARGET" --version 2>/dev/null || echo "unknown")
say ""
say "  Installed boost $VERSION to $TARGET"

# `case` over PATH rather than `grep`, so a directory name that happens to be a
# regex metacharacter cannot produce a false positive.
case ":$PATH:" in
  *":$BIN_DIR:"*)
    say ""
    say "  Get started:  boost add"
    ;;
  *)
    say ""
    say "  $BIN_DIR is not on your PATH. Add it:"
    say ""
    say "    echo 'export PATH=\"\$PATH:$BIN_DIR\"' >> ~/.profile"
    say "    export PATH=\"\$PATH:$BIN_DIR\""
    say ""
    say "  Then:  boost add"
    ;;
esac
say ""
