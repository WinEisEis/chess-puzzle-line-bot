#!/usr/bin/env bash
# One-time setup: run from this folder with  bash setup.sh
set -e
cd "$(dirname "$0")"

# 1) .env from the template, with a random ADMIN_KEY (won't overwrite an existing .env)
if [ ! -f .env ]; then
  KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
  sed "s/^ADMIN_KEY=.*/ADMIN_KEY=$KEY/" .env.example > .env
  sed -i.bak "s/X-Admin-Key: .*/X-Admin-Key: $KEY/" requests.http && rm -f requests.http.bak
  echo "✔ Created .env  (fill in LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, BASE_URL)"
else
  echo "• .env already exists, left as is"
fi

# 2) VS Code run/debug configs
mkdir -p .vscode
cp -n vscode/*.json .vscode/ 2>/dev/null || true
echo "✔ VS Code configs in .vscode/"

# 3) Virtual env + packages
if [ ! -d .venv ]; then python3 -m venv .venv; fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
echo "✔ Installed packages in .venv"

./.venv/bin/python -m pytest -q
echo
echo "Next: open .env and paste your LINE keys, then press F5 in VS Code → 'Run bot (uvicorn)'."
