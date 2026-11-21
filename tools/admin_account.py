"""Provision the single web account in this installation."""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from foundation import admin_access


def main():
    if admin_access.account_path().exists():
        admin_access.read_account()
        print("ADMIN 已初始化，已保留原密码。")
        return
    password = os.environ.get("STOCK_INITIAL_ADMIN_PASSWORD")
    if password is None:
        password = getpass.getpass("设置 ADMIN 密码（12 至 160 字符）：")
        if password != getpass.getpass("再次输入："):
            raise SystemExit("两次输入不一致。")
    created = admin_access.initialize(password)
    print("已设置 ADMIN 的本机登录密码。" if created else "已保留原 ADMIN 密码。")


if __name__ == "__main__":
    os.environ.pop("STOCK_MYSQL_DATABASE", None)
    main()
