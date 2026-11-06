# 数据库工具模块：把"连接数据库"和"执行 SQL"封装成几个函数
# 这样 app.py 里就不用每次都写连接代码

from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor
from config import DB_CONFIG


def get_conn():
    """建立一条到 MySQL 的连接"""
    return pymysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset="utf8mb4",
        cursorclass=DictCursor,   # 让查询结果变成字典，能按字段名取值
        autocommit=False,
    )


def query_all(sql, args=None):
    """查询多行，返回列表"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.fetchall()
    finally:
        conn.close()


def query_one(sql, args=None):
    """查询一行（比如统计数量），返回单个字典"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.fetchone()
    finally:
        conn.close()


def execute(sql, args=None):
    """执行插入/更新/删除，返回影响行数"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            rows = cur.execute(sql, args or ())
        conn.commit()   # 必须提交，否则数据不会真正写进库
        return rows
    finally:
        conn.close()


def insert(sql, args=None):
    """执行插入，返回新记录的自增主键（lastrowid）

    新增挂号后要跳转到挂号列表并高亮那一条，
    就必须知道刚插进去的 reg_id，所以单独做一个函数。
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            new_id = cur.lastrowid
        conn.commit()
        return new_id
    finally:
        conn.close()


@contextmanager
def transaction():
    """事务上下文：把多步操作打包成"要么全成、要么全不成"

    开处方就是典型场景——写病历、扣库存、改挂号状态必须一起成功。
    如果扣库存时发现药不够，前面写进去的病历也要一起撤销，
    否则就会出现"病历有了但药没扣"的脏数据。

    用法：
        with transaction() as cur:
            cur.execute(...)
            cur.execute(...)
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()   # 任何一步出错，整批操作全部撤销
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    # 直接运行本文件时，测试一下连接是否正常
    row = query_one("SELECT COUNT(*) AS total FROM patient")
    print("连接成功！患者表共有", row["total"], "条记录")
