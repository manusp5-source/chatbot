#!/usr/bin/env bash
# Smoke test E2E del despliegue. Asume docker compose corriendo.
# Uso: bash scripts/smoke.sh

set -e

API=${API:-http://localhost:8000}
# Sin valores por defecto a propósito: cada instalación tiene su propio admin.
EMAIL=${EMAIL:?define EMAIL con el admin de tu instalación}
PASS=${PASS:?define PASS con la contraseña de ese admin}

echo "== 1. Healthcheck =="
curl -sf "$API/health" | tee /dev/null
echo

echo "== 2. Login =="
TOKEN=$(curl -sf -X POST "$API/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")
echo "Token OK ($(echo "$TOKEN" | cut -c1-20)…)"

AUTH="Authorization: Bearer $TOKEN"

echo "== 3. /auth/me =="
curl -sf "$API/api/v1/auth/me" -H "$AUTH"
echo

echo "== 4. Admin health =="
curl -sf "$API/api/v1/admin/health" -H "$AUTH"
echo

echo "== 5. Tags =="
curl -sf "$API/api/v1/tags" -H "$AUTH"
echo

echo "== 6. Conversations (vacío al inicio) =="
curl -sf "$API/api/v1/conversations?page=1" -H "$AUTH"
echo

echo "== 7. Agent config =="
curl -sf "$API/api/v1/admin/agent/config" -H "$AUTH" | head -c 300
echo
echo

echo "== 8. Webhook YCloud (sin secret en dev) =="
curl -sf -X POST "$API/api/v1/webhooks/ycloud" \
  -H "Content-Type: application/json" \
  -d '{"type":"whatsapp.inbound_message.received","whatsappInboundMessage":{"id":"smoke-1","from":"+34600111222","to":"+34900000000","type":"text","text":{"body":"Hola, ¿cuánto cuesta un corte?"}}}'
echo

echo
echo "Smoke OK ✓"
