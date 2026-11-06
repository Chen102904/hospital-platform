# MySQL 连接配置
#
# 【重要】真实密码不要写在这个文件里 —— 这个文件会提交到 Git 仓库，公开后密码就泄了。
#
# 本地跑项目时，把密码填在同目录的 config_local.py 里。
# 那个文件已经被 .gitignore 排除，不会进仓库（模板见本文件底部注释）。
#
#     # hospital_web/config_local.py
#     DB_CONFIG = {"password": "你的root密码"}
#
# 也可以用环境变量，但 config_local.py 的优先级更高：
#
#     set DB_PASSWORD=你的root密码        (Windows CMD)
#     $env:DB_PASSWORD="你的root密码"     (Windows PowerShell)
#     export DB_PASSWORD=你的root密码     (macOS / Linux)

import os

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),   # 数据库地址，本机就是 localhost
    "port": int(os.environ.get("DB_PORT", "3306")),   # MySQL 默认端口
    "user": os.environ.get("DB_USER", "root"),        # 用户名
    "password": os.environ.get("DB_PASSWORD", ""),    # 密码：见上面说明，默认空串
    "database": os.environ.get("DB_NAME", "hospital_db"),  # 数据库名，和项目里一致
}

# 同目录下存在 config_local.py 时，用它的配置覆盖（专门放本机密码，不进仓库）
try:
    from config_local import DB_CONFIG as _LOCAL
except ImportError:
    pass
else:
    DB_CONFIG.update(_LOCAL)
