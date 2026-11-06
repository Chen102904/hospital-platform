# -*- coding: utf-8 -*-
"""
医院信息管理系统 单元测试（pytest）

只测「纯业务逻辑」，不连数据库、不起 Flask 服务，跑得快、可复现。
覆盖最容易出错、最该有回归保护的部分：
  1. rmb_upper —— 金额转中文大写（收据核心，边界最多）
  2. stock_level —— 库存分级（阈值边界）
  3. has_perm —— 角色权限判断（默认拒绝 + 通配）
  4. reg_fee_items —— 挂号费/药费明细计算（mock 游标）

运行：cd hospital_web && pytest tests/ -v
"""
import pytest

from app import (
    rmb_upper,
    stock_level,
    has_perm,
    reg_fee_items,
    ROLE_PERMS,
    LOW_STOCK,
    DANGER_STOCK,
)


# ============ 1. 金额转中文大写 ============
class TestRmbUpper:
    def test_zero(self):
        assert rmb_upper(0) == "零元整"
        assert rmb_upper(0.0) == "零元整"

    def test_round_integer(self):
        assert rmb_upper(128) == "壹佰贰拾捌元整"

    def test_with_jiao(self):
        assert rmb_upper(128.5) == "壹佰贰拾捌元伍角"

    def test_with_fen(self):
        assert rmb_upper(128.55) == "壹佰贰拾捌元伍角伍分"

    def test_jiao_zero_fen_nonzero(self):
        # 只有分没有角：0.05 -> 零伍分（收据场景，不加"元"）
        assert rmb_upper(0.05) == "零伍分"

    def test_small_amount(self):
        # 纯角：0.1 -> 壹角
        assert rmb_upper(0.1) == "壹角"

    def test_ten_and_units(self):
        assert rmb_upper(10) == "壹拾元整"
        assert rmb_upper(15) == "壹拾伍元整"

    def test_middle_zero(self):
        # 中间有零要补"零"：105 -> 壹佰零伍元整
        assert rmb_upper(105) == "壹佰零伍元整"

    def test_ten_thousand(self):
        assert rmb_upper(10000) == "壹万元整"

    def test_million_with_gap(self):
        # 1000005 -> 壹佰万零伍元整（低位组不足四位补零）
        assert rmb_upper(1000005) == "壹佰万零伍元整"

    def test_rounding(self):
        # 浮点精度：0.999 应该进位成 1 元而不是 0.99
        assert rmb_upper(0.999) == "壹元整"


# ============ 2. 库存分级 ============
class TestStockLevel:
    def test_danger_boundary(self):
        assert stock_level(DANGER_STOCK - 1) == "danger"
        assert stock_level(DANGER_STOCK) == "warn"  # 等于 50 不算 danger

    def test_warn_boundary(self):
        assert stock_level(DANGER_STOCK + 1) == "warn"
        assert stock_level(LOW_STOCK - 1) == "warn"
        assert stock_level(LOW_STOCK) == "ok"  # 等于 100 不算 warn

    def test_ok(self):
        assert stock_level(LOW_STOCK + 1) == "ok"
        assert stock_level(9999) == "ok"


# ============ 3. 角色权限判断 ============
import app as app_module


@pytest.fixture
def req_ctx():
    """提供一个 Flask 请求上下文，让 session 可用"""
    with app_module.app.test_request_context():
        yield


class TestHasPerm:
    def test_no_role_denied(self, req_ctx, monkeypatch):
        # 未登录（无 role）→ 拒绝
        from flask import session
        session["role"] = None
        assert has_perm("reg.view") is False

    def test_admin_wildcard(self, req_ctx, monkeypatch):
        from flask import session
        session["role"] = "管理员"
        # 管理员有 '*'，任何权限都放行
        assert has_perm("anything.here") is True

    def test_doctor_has_reg_visit(self, req_ctx, monkeypatch):
        from flask import session
        session["role"] = "医生"
        assert has_perm("reg.visit") is True

    def test_doctor_denied_charge(self, req_ctx, monkeypatch):
        from flask import session
        session["role"] = "医生"
        # 医生没有收费权限 → 拒绝（默认拒绝，不是放行）
        assert has_perm("charge.do") is False

    def test_empty_perm_always_allowed(self, req_ctx):
        # perm 为 None 表示"只要求登录"，任何人登录后都可访问
        assert has_perm(None) is True

    def test_unknown_role_denied(self, req_ctx, monkeypatch):
        from flask import session
        session["role"] = "不存在的角色"
        assert has_perm("reg.view") is False

    def test_pharmacist_has_dispense(self, req_ctx, monkeypatch):
        from flask import session
        session["role"] = "药房"
        assert has_perm("dispense.do") is True
        assert has_perm("charge.do") is False  # 药房不能收费


# ============ 4. 挂号费/药费明细计算 ============
class FakeCursor:
    """模拟数据库游标：按 SQL 返回预设结果"""
    def __init__(self, reg_row, drug_rows):
        self._reg = reg_row
        self._drugs = drug_rows
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append(sql)

    def fetchone(self):
        return self._reg

    def fetchall(self):
        return self._drugs


class TestRegFeeItems:
    def test_only_reg_fee(self):
        cur = FakeCursor(
            {"reg_fee": 10},
            [],  # 没有处方药
        )
        items, drug_fee = reg_fee_items(1, cur=cur)
        assert drug_fee == 0.0
        assert len(items) == 1
        assert items[0]["item_type"] == "挂号费"
        assert items[0]["amount"] == 10.0

    def test_reg_plus_drugs(self):
        cur = FakeCursor(
            {"reg_fee": 10},
            [
                {"medicine_id": 1, "quantity": 2, "medicine_name": "布洛芬",
                 "spec": "0.2g", "unit": "盒", "price": 15.5},
            ],
        )
        items, drug_fee = reg_fee_items(1, cur=cur)
        assert drug_fee == 31.0  # 15.5 * 2
        assert len(items) == 2
        assert items[1]["item_type"] == "药品费"
        assert items[1]["amount"] == 31.0
        # 规格要拼进名称
        assert "0.2g" in items[1]["item_name"]

    def test_drug_without_spec(self):
        cur = FakeCursor(
            {"reg_fee": 10},
            [{"medicine_id": 1, "quantity": 1, "medicine_name": "维C",
              "spec": None, "unit": "瓶", "price": 3.0}],
        )
        items, _ = reg_fee_items(1, cur=cur)
        # 没有规格时不要拼括号
        assert items[1]["item_name"] == "维C"

    def test_float_rounding(self):
        # 价格 * 数量可能有浮点误差，要 round 到 2 位
        cur = FakeCursor(
            {"reg_fee": 10},
            [{"medicine_id": 1, "quantity": 3, "medicine_name": "某药",
              "spec": None, "unit": "盒", "price": 0.1}],
        )
        items, drug_fee = reg_fee_items(1, cur=cur)
        assert drug_fee == 0.3  # 0.1 * 3 = 0.30000000000000004，必须 round
