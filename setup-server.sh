#!/bin/bash
# One-command setup for a Linux server (Ubuntu/Debian). Run inside the scout-app folder:  bash setup-server.sh
set -e
cd "$(dirname "$0")"
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."; curl -fsSL https://get.docker.com | sh
fi
if [ ! -f .env ]; then
  read -s -p "Choose a dashboard password: " PW; echo
  [ -z "$PW" ] && { echo "A password is required."; exit 1; }
  echo "SCOUT_PASSWORD=$PW" > .env
  echo "WA_SECRET=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env
  echo "SCOUT_PORT=8000" >> .env
  read -p "Your domain for HTTPS (leave blank to use the IP address): " DOM
  [ -n "$DOM" ] && echo "DOMAIN=$DOM" >> .env
fi
cp -n searxng/settings.yml.example searxng/settings.yml
mkdir -p data
if grep -q CHANGE_ME_SECRET searxng/settings.yml; then sed -i "s/CHANGE_ME_SECRET/$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')/" searxng/settings.yml; fi
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then ufw allow 8000/tcp || true; ufw allow 80/tcp || true; ufw allow 443/tcp || true; fi
if grep -q '^DOMAIN=' .env; then docker compose --profile https up -d --build; else docker compose up -d --build; fi
IP=$(curl -s https://api.ipify.org || hostname -I | awk '{print $1}')
echo
echo "Scout is running and will restart by itself after reboots or crashes."
grep -q '^DOMAIN=' .env && echo "Open: https://$(grep '^DOMAIN=' .env | cut -d= -f2)" || echo "Open: http://$IP:8000"
echo "Logs: docker compose logs -f     Update: docker compose up -d --build     Stop: docker compose down"
