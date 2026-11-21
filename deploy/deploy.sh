#!/usr/bin/env bash
# deploy.sh — run on the Linux server, from the checkout:
#   bash deploy/deploy.sh            # fetch → ff-only merge → uv sync → (re)install unit → restart
#   bash deploy/deploy.sh status     # service status + last log lines + today's usage
# Never uses `git reset --hard`: a dirty tree aborts the deploy so nothing is lost.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV="${HOME}/.local/bin/uv"
UNIT_SRC="${ROOT}/deploy/ltp.service"
UNIT_DST="${HOME}/.config/systemd/user/ltp.service"
BRANCH="${BRANCH:-main}"

status() {
  systemctl --user --no-pager status ltp.service | head -12 || true
  echo "--- last log lines"
  journalctl --user -u ltp.service -n 20 --no-pager || true
  echo "--- today"
  (cd "${ROOT}/server" && "${UV}" run --frozen --no-dev ltp status) || true
}

if [[ "${1:-}" == "status" ]]; then status; exit 0; fi

cd "${ROOT}"
git fetch origin "${BRANCH}"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "working tree has uncommitted changes; stash them first (git stash push -m 'desc')" >&2
  git status --short >&2
  exit 1
fi
git merge --ff-only "origin/${BRANCH}"

cd "${ROOT}/server"
"${UV}" sync --frozen --no-dev
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created server/.env from template — fill in LG_TV_IP / TELEGRAM_* then rerun"
fi
chmod 600 .env
if [[ ! -f config/settings.yaml ]]; then
  cp config/settings.example.yaml config/settings.yaml
  echo "created server/config/settings.yaml from template — set timezone / quota, then rerun"
fi

if [[ "$(loginctl show-user "${USER}" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo "WARNING: linger is off — the user service will not start at boot without a login." >&2
  echo "         run once: sudo loginctl enable-linger ${USER}" >&2
fi

mkdir -p "$(dirname "${UNIT_DST}")"
# The unit template is filled in with sed and systemd splits ExecStart on spaces,
# so refuse paths that would need escaping instead of writing a broken unit.
for p in "${ROOT}" "${UV}"; do
  if [[ "${p}" =~ [^A-Za-z0-9._/+-] ]]; then
    echo "ERROR: path '${p}' may only contain letters, digits and . _ / + -; move the checkout / uv to a plain path" >&2
    exit 1
  fi
done
sed -e "s#@SERVER_DIR@#${ROOT}/server#g" -e "s#@UV@#${UV}#g" "${UNIT_SRC}" > "${UNIT_DST}"
chmod 644 "${UNIT_DST}"
systemctl --user daemon-reload
systemctl --user enable ltp.service >/dev/null
systemctl --user restart ltp.service
restarts_before="$(systemctl --user show ltp.service -p NRestarts --value)"
sleep 8
restarts_after="$(systemctl --user show ltp.service -p NRestarts --value)"
if ! systemctl --user is-active --quiet ltp.service || [[ "${restarts_after}" != "${restarts_before}" ]]; then
  echo "ERROR: ltp.service is not staying up (restarts ${restarts_before} -> ${restarts_after})" >&2
  systemctl --user status ltp.service --no-pager || true
  journalctl --user -u ltp.service -n 40 --no-pager || true
  exit 1
fi
status
