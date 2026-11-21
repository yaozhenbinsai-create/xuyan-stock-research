"""Use the install's private Codex CLI profile for an explicit official sign-in."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.codex_native import official_command, official_environment


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="登录或查看本安装的官方账号")
    parser.add_argument("action", choices=["login", "status"])
    args = parser.parse_args()
    os.environ.pop("STOCK_MYSQL_DATABASE", None)
    executable = shutil.which("codex.cmd") or shutil.which("codex")
    if not executable:
        parser.exit(1, "请先安装官方 Codex CLI。\n")
    command = ["login"] if args.action == "login" else ["login", "status"]
    environment = official_environment()
    result = subprocess.run(official_command(executable, *command), env=environment,
                            cwd=environment["CODEX_HOME"])
    raise SystemExit(result.returncode)
