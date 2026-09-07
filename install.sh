#!/usr/bin/env bash
# Put clinic-triage on PATH and its man page on the manpath. Symlinks rather
# than copies, so a git pull updates both.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/man/man1"
ln -sf "$HERE/bin/clinic-triage"      "$HOME/.local/bin/clinic-triage"
ln -sf "$HERE/man/clinic-triage.1"    "$HOME/.local/share/man/man1/clinic-triage.1"
echo "installed:"
echo "  $HOME/.local/bin/clinic-triage        -> $HERE/bin/clinic-triage"
echo "  $HOME/.local/share/man/man1/clinic-triage.1 -> $HERE/man/clinic-triage.1"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  echo; echo "note: $HOME/.local/bin is not on your PATH." ;;
esac
echo
echo "Try:  clinic-triage doctor   and   man clinic-triage"
