#!/usr/bin/env bash
#
# setup-dashboard-https.sh
#
# Gives the Coding Agent's Mini App dashboard a public HTTPS address:
#   https://<your-ip-with-dashes>.sslip.io  ->  Caddy (Let's Encrypt)  ->  127.0.0.1:8787
#
# Prerequisite: the dashboard code is deployed on /opt/ai-coding-agent.
#
# Each step checks before it changes anything, and stops with an explanation
# instead of half-configuring. Safe to re-run.
#
# The dashboard gets its own Caddy site file (/etc/caddy/sites/); the main
# Caddyfile only gains an import line, so other sites in it are left alone.
#
# Usage:   sudo bash setup-dashboard-https.sh            # auto-detect public IP
#          sudo bash setup-dashboard-https.sh 1.2.3.4    # or pass it explicitly
#
set -euo pipefail

SERVICE="ai-coding-agent"
APP_DIR="/opt/ai-coding-agent"
VENV="/opt/ai_coding_venv"
ENV_FILE="/etc/ai-coding-agent/ai-coding-agent.env"
CADDYFILE="/etc/caddy/Caddyfile"
SITES_DIR="/etc/caddy/sites"
SITE_FILE="${SITES_DIR}/ai-coding-agent-dashboard.caddy"
IMPORT_LINE="import ${SITES_DIR}/*.caddy"

say()  { printf '\n=== %s ===\n' "$*"; }
ok()   { printf '    OK: %s\n' "$*"; }
stop() { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }
stamp() { date +%Y%m%d-%H%M%S; }

[ "$(id -u)" -eq 0 ] || stop "run as root: sudo bash $0"

# --------------------------------------------------------------------------
say "1. Dashboard code is deployed"
grep -q "WEBAPP_URL" "${APP_DIR}/ai_agent/config.py" 2>/dev/null \
  || stop "${APP_DIR} has no dashboard support yet. Deploy the dashboard code first."
[ -f "${APP_DIR}/ai_agent/bot/webapp.html" ] || stop "webapp.html missing in ${APP_DIR}; deploy is incomplete."
[ -f "${ENV_FILE}" ] || stop "${ENV_FILE} not found; the bot's env file is needed to set WEBAPP_URL."
ok "dashboard code and env file present"

PORT="$(grep -E '^WEBAPP_PORT=' "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
PORT="${PORT:-8787}"
[[ "${PORT}" =~ ^[0-9]+$ ]] || stop "WEBAPP_PORT in ${ENV_FILE} is not a number: '${PORT}'"
ok "local dashboard port: ${PORT}"

# --------------------------------------------------------------------------
say "2. Python dependencies (aiohttp)"
"${VENV}/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
"${VENV}/bin/python" -c "import aiohttp" || stop "aiohttp still not importable in ${VENV}"
ok "aiohttp installed in ${VENV}"

# --------------------------------------------------------------------------
say "3. Public IP and sslip.io name"
IP="${1:-}"
if [ -z "${IP}" ]; then
  # DigitalOcean metadata first (no external call), then a public echo service.
  # -f: an HTTP error page (e.g. a non-DO metadata 404) must count as "no answer".
  IP="$(curl -sf --max-time 2 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || true)"
  [ -n "${IP}" ] || IP="$(curl -sf --max-time 5 https://api.ipify.org || true)"
fi
[[ "${IP}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || stop "could not determine a public IPv4 (got '${IP}'). Pass it: sudo bash $0 <ip>"
DOMAIN="${IP//./-}.sslip.io"
RESOLVED="$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
[ "${RESOLVED}" = "${IP}" ] || stop "${DOMAIN} resolves to '${RESOLVED}', expected ${IP}. Check DNS / sslip.io reachability."
ok "${DOMAIN} -> ${IP}"

# --------------------------------------------------------------------------
say "4. Ports 80 and 443 are free (or already Caddy's)"
BUSY="$(ss -ltnpH '( sport = :80 or sport = :443 )' | grep -v caddy || true)"
[ -z "${BUSY}" ] || stop "something else listens on 80/443:
${BUSY}
Stop it, or move its site into Caddy (${CADDYFILE}), then re-run."
ok "80/443 available"

# --------------------------------------------------------------------------
say "5. Caddy"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi
ok "$(caddy version | head -1)"

CADDY_BACKUP=""
SITE_BACKUP=""
if [ -f "${CADDYFILE}" ]; then
  CADDY_BACKUP="${CADDYFILE}.bak.$(stamp)"
  cp "${CADDYFILE}" "${CADDY_BACKUP}"
  ok "previous Caddyfile saved to ${CADDY_BACKUP}"
fi
if [ -f "${SITE_FILE}" ]; then
  SITE_BACKUP="${SITE_FILE}.bak.$(stamp)"
  mv "${SITE_FILE}" "${SITE_BACKUP}"
fi

restore_caddy() {
  rm -f "${SITE_FILE}"
  [ -z "${SITE_BACKUP}" ] || mv "${SITE_BACKUP}" "${SITE_FILE}"
  if [ -n "${CADDY_BACKUP}" ]; then cp "${CADDY_BACKUP}" "${CADDYFILE}"; else rm -f "${CADDYFILE}"; fi
}

mkdir -p "${SITES_DIR}"
printf '# Managed by setup-dashboard-https.sh: Coding Agent Mini App\n%s {\n\tencode gzip\n\treverse_proxy 127.0.0.1:%s\n}\n' \
  "${DOMAIN}" "${PORT}" > "${SITE_FILE}"

if [ -f "${CADDYFILE}" ] && grep -q "The Caddyfile is an easy way to configure your Caddy web server" "${CADDYFILE}"; then
  # Stock placeholder from the Debian package: its ":80 { file_server }" site
  # is just the welcome page, so replace it (the backup above keeps it).
  printf '%s\n' "${IMPORT_LINE}" > "${CADDYFILE}"
  ok "replaced the stock placeholder Caddyfile with an import of ${SITES_DIR}"
elif ! grep -qxF "${IMPORT_LINE}" "${CADDYFILE}" 2>/dev/null; then
  printf '\n%s\n' "${IMPORT_LINE}" >> "${CADDYFILE}"
  ok "added '${IMPORT_LINE}' to ${CADDYFILE}; its other sites are unchanged"
fi

if ! caddy validate --config "${CADDYFILE}" --adapter caddyfile >/dev/null 2>&1; then
  restore_caddy
  stop "resulting Caddy config is invalid; previous files restored. Check with:
  caddy validate --config ${CADDYFILE} --adapter caddyfile"
fi
[ -z "${SITE_BACKUP}" ] || rm -f "${SITE_BACKUP}"
ok "${SITE_FILE}: ${DOMAIN} -> 127.0.0.1:${PORT}"

# --------------------------------------------------------------------------
say "6. Firewall"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ok "ufw: 80 and 443 allowed"
else
  ok "ufw not active on this host"
fi
echo "    NOTE: if the droplet has a DigitalOcean *cloud* firewall, allow inbound 80 and 443 there too."

systemctl enable --now caddy >/dev/null
systemctl reload caddy || systemctl restart caddy
ok "caddy running"

# --------------------------------------------------------------------------
say "7. Point the bot at the dashboard"
cp "${ENV_FILE}" "${ENV_FILE}.bak.$(stamp)"
URL="https://${DOMAIN}"
if grep -q '^WEBAPP_URL=' "${ENV_FILE}"; then
  sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=${URL}|" "${ENV_FILE}"
else
  printf '\nWEBAPP_URL=%s\n' "${URL}" >> "${ENV_FILE}"
fi
ok "WEBAPP_URL=${URL} (env file backed up)"

systemctl restart "${SERVICE}"
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:${PORT}/healthz" >/dev/null \
  || stop "the bot restarted but the dashboard is not answering on 127.0.0.1:${PORT}.
Check: journalctl -u ${SERVICE} -n 50 --no-pager | grep -i dashboard"
ok "dashboard answering locally"

# --------------------------------------------------------------------------
say "8. Public HTTPS (first certificate can take up to a minute)"
for _ in $(seq 1 30); do
  curl -sf --max-time 5 "${URL}/healthz" >/dev/null && break
  sleep 3
done
if ! curl -sf --max-time 5 "${URL}/healthz" >/dev/null; then
  journalctl -u caddy -n 25 --no-pager || true
  stop "${URL} is not reachable over HTTPS yet. Usual causes: inbound 80/443 blocked by a cloud
firewall (Let's Encrypt must reach port 80), or a certificate rate limit. See Caddy's log above."
fi
ok "${URL}/healthz answers over HTTPS"

say "DONE"
echo "Dashboard: ${URL}"
echo "In Telegram, open the Coding Agent chat and tap the 'Dashboard' button next to the"
echo "message field (reopen the chat if it still shows 'Menu'). Typing / still lists commands."
echo
echo "Opening ${URL} in a normal browser only shows 'Open this dashboard from the bot' by design:"
echo "the data endpoint accepts only Telegram-signed requests from your account."
