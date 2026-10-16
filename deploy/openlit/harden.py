#!/usr/bin/env python3
"""Replace OpenLIT's seeded default account and verify the stack is not left with a known login.

OpenLIT seeds `user@openlit.io` with the published password `openlituser` on every start, and allows open
self-registration. This script:
  1. creates (or reuses) an admin email and a strong password from .env.local (generated if absent),
  2. rewrites the seeded account to those credentials (only the bcrypt hash leaves this machine's memory; it is passed to
     the container on stdin, never on a command line),
  3. verifies through the UI: the default login is rejected, the new login works, registration is blocked (403).
Registration blocking itself is done by the ui-proxy (nginx) in docker-compose.yml.

usage:  uv run --with bcrypt python deploy/openlit/harden.py [--env .env.local] [--remove-other-users]
Docker is used as configured in your environment (set DOCKER_CONTEXT to choose one).
"""
import argparse
import http.cookiejar
import json
import secrets
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import bcrypt

HERE = Path(__file__).resolve().parent
DEFAULT_EMAIL, DEFAULT_PASSWORD = "user@openlit.io", "openlituser"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def compose(env_file: Path, *args: str, stdin: str | None = None) -> str:
    cmd = ["docker", "compose", "-f", str(HERE / "docker-compose.yml"), "--env-file", str(env_file), *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=True).stdout


NODE = r"""
const {PrismaClient} = require("@prisma/client");
const p = new PrismaClient();
(async () => {
  const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
  const users = await p.user.findMany({select: {id: true, email: true}});
  const target = users.find(u => u.email === input.email) || users.find(u => u.email === input.defaultEmail);
  if (!target) throw new Error("no seeded account found");
  await p.user.update({where: {id: target.id}, data: {email: input.email, password: input.hash, name: "Administrator"}});
  const others = users.filter(u => u.id !== target.id);
  if (input.removeOthers) {
    for (const u of others) {
      await p.organisationUser.deleteMany({where: {userId: u.id}});
      await p.user.delete({where: {id: u.id}});
    }
  }
  console.log(JSON.stringify({updated: input.email, otherUsers: others.map(u => u.email), removed: input.removeOthers}));
  await p.$disconnect();
})().catch(e => { console.error(e.message); process.exit(1); });
"""


def ui_request(base: str, path: str, data: dict[str, str] | None = None, jar=None) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body, method="POST" if body is not None else "GET")
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def login(base: str, provider: str, email: str, password: str) -> bool:
    jar = http.cookiejar.CookieJar()
    csrf = json.loads(ui_request(base, "/api/auth/csrf", jar=jar)[1])["csrfToken"]
    ui_request(base, f"/api/auth/callback/{provider}",
               {"csrfToken": csrf, "email": email, "password": password, "json": "true"}, jar)
    session = json.loads(ui_request(base, "/api/auth/session", jar=jar)[1] or "{}")
    return bool(session.get("user", {}).get("email") == email)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=str(HERE / ".env.local"))
    ap.add_argument("--remove-other-users", action="store_true", help="delete every account except the admin")
    args = ap.parse_args()
    env_file = Path(args.env)
    env = read_env(env_file)

    email = env.get("OPENLIT_ADMIN_EMAIL") or ""
    password = env.get("OPENLIT_ADMIN_PASSWORD") or ""
    generated = {}
    if not email:
        sys.exit("Set OPENLIT_ADMIN_EMAIL in the env file (a real address you control, not user@openlit.io).")
    if not password:
        password = generated["OPENLIT_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
    if email == DEFAULT_EMAIL or password == DEFAULT_PASSWORD or len(password) < 16:
        sys.exit("Refusing: the admin email must not be the default and the password needs at least 16 characters.")
    if generated:
        write_env(env_file, generated)
        print(f"Generated an admin password and stored it in {env_file} (mode 600).")

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(10)).decode()
    payload = json.dumps({"email": email, "defaultEmail": DEFAULT_EMAIL, "hash": hashed,
                          "removeOthers": args.remove_other_users})
    out = compose(env_file, "exec", "-T", "openlit", "sh", "-c", f"cd /app/client && node -e '{NODE}'", stdin=payload)
    result = json.loads(out.strip().splitlines()[-1])
    print(f"Account set: {result['updated']}")
    if result["otherUsers"]:
        verb = "removed" if result["removed"] else "present (use --remove-other-users to delete)"
        print(f"Other accounts {verb}: {', '.join(result['otherUsers'])}")

    base = f"http://127.0.0.1:{env.get('OPENLIT_UI_PORT', '3300')}"
    checks = {
        "default login rejected": not login(base, "login", DEFAULT_EMAIL, DEFAULT_PASSWORD),
        "admin login works": login(base, "login", email, password),
        "registration blocked (403)": ui_request(base, "/api/auth/callback/register", {"email": "x@example.test"})[0] == 403,
    }
    for name, ok in checks.items():
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
