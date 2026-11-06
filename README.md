# 医院信息化平台

一套围绕**同一份业务模型**（科室 / 医生 / 排班 / 挂号 / 收费 / 库存）搭起来的完整项目：
从底层表结构和约束，到 Web 系统，再到用 AI 做运营归因和数据问答。

不是三个互不相干的 demo——它们接的是同一套表、同一套字段、同一套业务规则。

## 四个模块

| 模块 | 是什么 | 关键数字 |
|---|---|---|
| [`hospital_db_project/`](hospital_db_project/) | 数据库设计与 SQL：表结构、约束、索引、39 条统计查询 | 15 张表 · 20 个外键 · 40 条索引 · 1872 行演示数据 |
| [`hospital_web/`](hospital_web/) | Flask Web 系统：门诊闭环、住院、药房、收费、权限 | 53 个路由 · 5 类角色 · 25 项单元测试 · Docker 一键部署 |
| [`ops_agent/`](ops_agent/) | 运营异常归因 Agent：问「这个月为什么掉量」，自己决定查几步 | 84,424 条挂号 · 3/3 检出 · 0 误报 |
| [`ai_lab/`](ai_lab/) | 数据问答 Agent 的十步实验：工具调用、RAG、评测体系 | 4 个工具 · 63 块知识库 · 38 题评测 · 87 项单测 |

## 一条业务线贯穿四个模块

```
科室 / 医生 / 排班 / 挂号 / 收费 / 库存
        │
        ├── hospital_db_project  把它们建成 15 张表，约束下沉到数据库
        │      · 住院「同时只能在院一次」用生成列 + 唯一索引兜底，不靠代码判断
        │      · 库存用流水账（事件溯源），当前库存是累加结果，能倒查每一笔
        │      · 基础数据只停用不删除，删科室不会毁掉三年前的收费单
        │
        ├── hospital_web         给这套表做界面和流程，53 个路由按 5 类角色分权
        │      · 门诊闭环：排班放号 → 挂号 → 叫号 → 接诊开方 → 收费 → 发药 → 退费退药
        │      · 收费存费用快照，药品调价不影响已开单据
        │
        ├── ops_agent            用同一套表回答「这个月为什么掉量」
        │      · 单轮查数只能发现「谁的量掉了」，发现不了「量正常但退号率暴涨」
        │      · 多步归因 3/3 检出，单轮 1/3
        │
        └── ai_lab               用同一套表做自然语言问答
               · 自建编排（Python 直调 API，工具循环自己写）
               · 38 题评测集，程序判分，能拒答才算可用
```

## 快速开始

### 只跑 Web 系统（最快）

```bash
cd hospital_web
pip install -r requirements.txt
# 复制 config_local.py.example 为 config_local.py，填入 MySQL 连接信息
python app.py          # → http://127.0.0.1:5000
```

账号：`admin / admin123`

或用 Docker（含 MySQL + 自动灌演示数据）：

```bash
cd hospital_web
docker compose up
```

容器里的数据库密码走环境变量，默认 `CHANGE_ME`（仅供本地开发）。要用自己的密码就复制 `.env.example` 为 `.env` 改一行。

### 跑运营归因 Agent

```bash
cd ops_agent
cp db_local.py.example db_local.py   # 填上本机 MySQL 密码（该文件已被 .gitignore 排除）
python gen_ops_data.py        # 造 12 个月数据（重建 hospital_ops 库）
python ops_agent.py 2026-07   # 查单月，规则决策
python eval_agent.py          # 12 个月评测 + 单轮对照
```

### 跑数据库设计

```bash
cd hospital_db_project
mysql -u root -p < 01_create_tables.sql
mysql -u root -p < 04_seed_demo_data.sql
mysql -u root -p < 03_queries.sql      # 39 条查询
```

## 技术栈

| 层 | 用了什么 |
|---|---|
| 数据库 | MySQL 8.0（生成列、唯一索引、行级锁、事件溯源、外键约束） |
| 后端 | Python 3.12 · Flask 3.1 · PyMySQL |
| 前端 | Jinja2 · ECharts · 原生 JS |
| 数据分析 | pandas · matplotlib · SciPy · statsmodels（Cox / PHReg） |
| AI | 大模型 API 直调（不用框架）· Function Calling · RAG · 自建 Agent 循环 |
| 工程 | Docker / docker-compose · pytest · Git |

## 四份 README 各自说什么

- [数据库设计](hospital_db_project/数据库设计文档.md)：表结构、索引口径、约束怎么下沉
- [Web 系统](hospital_web/README.md)：路由、权限模型、门诊闭环
- [归因 Agent](ops_agent/README.md)：为什么必须是多步、两套决策层对比、评测结果
- [AI 实验](ai_lab/README.md)：十步路线、评测体系、已知限制

## 已知限制

1. **演示数据**。数据库里的患者、医生姓名都是生成的，不含任何真实个人信息。
2. `ops_agent` 的 12 个月运营数据是按业务模型模拟的（固定随机种子），价值在于「答案已知」——能算出检出率，而不是只能看「像不像」。
3. `ai_lab` 的问答入口是自建编排，没用 LangChain / 向量数据库，检索是手写余弦相似度。规模上去了要换 ANN 索引。
4. 本仓库不含任何密钥。`config_local.py`、`db_local.py`、`.env`、`mykey.py`、`db_ro.py` 全部在 `.gitignore` 里，每个都有对应的 `.example` 模板，复制改名填密码即可。
