# -*- coding: utf-8 -*-
"""ops_agent 三个脚本共用的数据库连接配置。

真实密码不进仓库。取值优先级：环境变量 → 同目录 db_local.py → 空串。

自己跑之前二选一：

  1) 复制 db_local.py.example 为 db_local.py，填上 password
  2) 设环境变量（PowerShell）：$env:OPS_DB_PASSWORD="你的密码"

为什么不在脚本里直接写密码：这个仓库是公开的，写一个就等于公开一个，
而且以后想换环境（本机 / 服务器 / 别人的机器）都得改三个文件。
"""
import os

DB = dict(
    host=os.environ.get("OPS_DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("OPS_DB_PORT", "3306")),
    user=os.environ.get("OPS_DB_USER", "root"),
    password=os.environ.get("OPS_DB_PASSWORD", ""),
    charset="utf8mb4",
    database=os.environ.get("OPS_DB_NAME", "hospital_ops"),
)

# 同目录存在 db_local.py 时，用它的配置覆盖（专门放本机密码，不进仓库）
try:
    from db_local import DB as _LOCAL
except ImportError:
    pass
else:
    DB.update(_LOCAL)
