#!/usr/bin/env bash
# jobright installer for macOS and Linux.
#   curl -fsSL https://raw.githubusercontent.com/ChandraChaan/jobright/main/install.sh | bash
# Installs into ~/.jobright/app (its own Python environment), adds a `jobright`
# command, and opens the setup wizard in your browser. Re-run to update.
set -euo pipefail

REPO="${JOBRIGHT_REPO:-https://github.com/ChandraChaan/jobright}"
REF="${JOBRIGHT_REF:-main}"
HOME_DIR="${JOBRIGHT_HOME:-$HOME/.jobright}"
APP="$HOME_DIR/app"
BIN="$HOME/.local/bin"

say() { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
die() { printf "\033[1;31mError:\033[0m %s\n" "$*" >&2; exit 1; }

# 1. Python 3.11+
PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  case "$(uname -s)" in
    Darwin) die "Python 3.11+ is needed. Install it from https://www.python.org/downloads/ (or: brew install python), then run this again." ;;
    *)      die "Python 3.11+ is needed. Install it with your package manager (e.g. sudo apt install python3.12 python3.12-venv), then run this again." ;;
  esac
fi
say "Using $("$PY" --version)"

# 2. Its own environment, so nothing else on your computer is touched
mkdir -p "$APP" "$BIN"
if [ ! -x "$APP/venv/bin/python" ]; then
  say "Creating environment in $APP"
  "$PY" -m venv "$APP/venv" || die "Could not create a virtual environment (on Debian/Ubuntu: sudo apt install python3-venv)."
fi
"$APP/venv/bin/python" -m pip install --quiet --upgrade pip
say "Installing jobright ($REF)"
"$APP/venv/bin/python" -m pip install --quiet --upgrade "jobright @ git+$REPO@$REF"

# 3. The browser engine the bot drives (Chrome is used instead when installed)
say "Installing the browser engine (one-time, ~150 MB)"
"$APP/venv/bin/python" -m playwright install chromium >/dev/null

# 4. A `jobright` command
cat > "$BIN/jobright" <<EOF
#!/usr/bin/env bash
export JOBRIGHT_HOME="$HOME_DIR"
exec "$APP/venv/bin/python" -m jobright "\$@"
EOF
chmod +x "$BIN/jobright"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
       [ -f "$rc" ] && ! grep -q 'jobright' "$rc" && printf '\n# jobright\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
     done
     export PATH="$BIN:$PATH" ;;
esac

say "Installed. Opening setup…"
"$BIN/jobright" start || true
cat <<EOF

  jobright is ready.
    Open it any time:   jobright start
    Check the install:  jobright doctor
    Your data:          $HOME_DIR (stays on this computer)

EOF
