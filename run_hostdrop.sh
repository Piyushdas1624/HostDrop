#!/usr/bin/env sh
# ==============================================================================
# HostDrop — Universal POSIX Shell Launcher
# Supports: Linux, macOS, Android (Termux)
# ==============================================================================

# Determine script directory portably
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# Termux environment detection
IS_TERMUX=0
if [ -n "$TERMUX_VERSION" ] || [ -d "/data/data/com.termux" ] || [ -n "$PREFIX" ]; then
    IS_TERMUX=1
fi

# Detect operating system
OS_NAME="$(uname -s 2>/dev/null)"

# Discover Python 3.8+
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "===================================================================="
    echo " [ERROR] Python 3.8+ is required to run HostDrop but was not found!"
    echo "===================================================================="
    echo ""
    if [ "$IS_TERMUX" -eq 1 ]; then
        echo " Android Termux detected."
        echo " Please install Python by running:"
        echo "   pkg install python"
    elif [ "$OS_NAME" = "Darwin" ]; then
        echo " macOS detected."
        echo " Please install Python 3.8+ by running:"
        echo "   brew install python"
    else
        echo " Linux detected."
        echo " Please install Python 3.8+ by running:"
        echo "   sudo apt install python3"
        echo " or use your distribution package manager."
    fi
    echo ""
    exit 1
fi

# Determine default receive folder
if [ "$IS_TERMUX" -eq 1 ]; then
    if [ -d "/sdcard" ]; then
        DEFAULT_DIR="/sdcard/HostDrop"
    elif [ -d "$HOME/storage/shared" ]; then
        DEFAULT_DIR="$HOME/storage/shared/HostDrop"
    else
        DEFAULT_DIR="$HOME/HostDrop"
    fi
else
    DEFAULT_DIR="$HOME/HostDrop"
fi

# Interactive prompt or command-line argument
if [ -n "$1" ]; then
    RECV_DIR="$1"
else
    echo "===================================================================="
    echo "   HostDrop — 2-Way Cross-Device File Transfer Hub"
    echo "===================================================================="
    echo ""
    echo "Where do you want to save INCOMING files (Inbox)?"
    printf "[Default: %s]: " "$DEFAULT_DIR"
    read RECV_DIR
    RECV_DIR="$(printf '%s' "$RECV_DIR" | tr -d '\r')"
    if [ -z "$RECV_DIR" ]; then
        RECV_DIR="$DEFAULT_DIR"
    fi
fi

# Clean any trailing carriage returns (e.g. from Windows / CRLF pipes)
RECV_DIR="$(printf '%s' "$RECV_DIR" | tr -d '\r')"

# Expand leading tilde if user manually typed e.g. ~/folder
case "$RECV_DIR" in
    "~"/*) RECV_DIR="$HOME/${RECV_DIR#"~/"}" ;;
    "~") RECV_DIR="$HOME" ;;
esac

# Ensure receive directory exists (only if not a CLI flag like --help or -h)
case "$RECV_DIR" in
    -*) ;;
    *) mkdir -p "$RECV_DIR" ;;
esac

# If user asked for help, skip browser opening and print help directly
case "$RECV_DIR" in
    -h|--help)
        exec "$PYTHON_BIN" "$SCRIPT_DIR/hostdrop.py" "$RECV_DIR"
        ;;
esac

echo ""
echo "===================================================================="
echo "   Starting HostDrop Hub on http://127.0.0.1:8080 ..."
echo "   Opening dashboard in your default browser..."
echo "===================================================================="
echo ""

# Launch browser in background
if [ "$IS_TERMUX" -eq 1 ] && command -v termux-open-url >/dev/null 2>&1; then
    termux-open-url "http://127.0.0.1:8080" >/dev/null 2>&1 &
elif [ "$OS_NAME" = "Darwin" ] && command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:8080" >/dev/null 2>&1 &
elif [ "$OS_NAME" = "Linux" ] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:8080" >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:8080" >/dev/null 2>&1 &
fi

# Launch HostDrop server
exec "$PYTHON_BIN" "$SCRIPT_DIR/hostdrop.py" "$RECV_DIR"
