#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:-local}"
cd "$project_dir"
case "$mode" in
  public)
    # Parse only the three relevant dotenv values; never source an executable file.
    python3 - <<'PY'
import os,re,sys
from pathlib import Path
values={}
path=Path('.env')
if path.exists():
    for line in path.read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):continue
        key,value=line.split('=',1);values[key.strip()]=value.strip().strip("'\"")
for key in ('LAB_DOMAIN','LAB_USERNAME','LAB_PASSWORD_HASH'):
    if key in os.environ:values[key]=os.environ[key]
domain=values.get('LAB_DOMAIN','')
hashed=values.get('LAB_PASSWORD_HASH','')
username=values.get('LAB_USERNAME','ahmed')
if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}',domain):
    sys.exit('LAB_DOMAIN must be a DNS hostname before public launch')
if not re.fullmatch(r'\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}',hashed):
    sys.exit('LAB_PASSWORD_HASH must be a bcrypt hash before public launch')
if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',username):
    sys.exit('LAB_USERNAME must use letters, digits, underscore or hyphen')
PY
    files=(-f compose.yaml -f compose.public.yaml)
    ;;
  local) files=(-f compose.yaml) ;;
  *) printf 'Usage: bash deploy/setup.sh [local|public]\n' >&2; exit 1 ;;
esac
command -v docker >/dev/null || { printf 'Install Docker Engine and its Compose plugin first.\n' >&2; exit 1; }
docker compose version >/dev/null
docker compose "${files[@]}" config --quiet
docker compose "${files[@]}" up -d --build
docker compose "${files[@]}" ps
