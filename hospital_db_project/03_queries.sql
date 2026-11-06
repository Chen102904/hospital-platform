-- ============================================================
-- 常用查询语句集（体现 SQL 能力）
-- 依赖：先执行 01_create_tables.sql 和 02_insert_data.sql
-- 注意：部分版本 Workbench 默认开启 safe update mode，
--       会拦截不带主键条件的 UPDATE/DELETE，故此处先关闭。
-- ============================================================
USE hospital_db;

-- 关闭安全更新模式（仅本次会话有效，避免 UPDATE/DELETE 被拦截）
SET SQL_SAFE_UPDATES = 0;

-- ------------------------------------------------------------
-- 一、基础查询（增删改查）
-- ------------------------------------------------------------

-- 1. 查询所有科室
SELECT * FROM department;

-- 2. 查询所有医生的姓名、职称、所属科室（多表联查）
SELECT d.doctor_id, d.doctor_name, d.title, dept.dept_name
FROM doctor d
LEFT JOIN department dept ON d.dept_id = dept.dept_id
ORDER BY dept.dept_name, d.doctor_id;

-- 3. 查询某位患者的挂号记录（按患者姓名）
SELECT p.patient_name, dept.dept_name, d.doctor_name,
       r.reg_date, r.reg_fee, r.reg_status
FROM registration r
JOIN patient p    ON r.patient_id = p.patient_id
JOIN doctor d     ON r.doctor_id  = d.doctor_id
JOIN department dept ON r.dept_id = dept.dept_id
WHERE p.patient_name = '陈小明';

-- 4. 新增一位患者
INSERT INTO patient (patient_name, gender, birth_date, phone, address)
VALUES ('周雨薇', '女', '1995-06-18', '13900000006', '杭州市余杭区XX路6号');

-- 5. 修改医生职称
UPDATE doctor SET title = '主任医师' WHERE doctor_name = '赵伟';

-- 6. 取消一条挂号记录
UPDATE registration SET reg_status = '已取消' WHERE reg_id = 6;

-- ------------------------------------------------------------
-- 二、统计查询（体现数据分析能力）
-- ------------------------------------------------------------

-- 7. 统计每个科室的医生人数
SELECT dept.dept_name AS 科室, COUNT(d.doctor_id) AS 医生人数
FROM department dept
LEFT JOIN doctor d ON dept.dept_id = d.dept_id
GROUP BY dept.dept_id, dept.dept_name
ORDER BY 医生人数 DESC;

-- 8. 统计各科室的挂号总量与挂号收入
SELECT dept.dept_name AS 科室,
       COUNT(r.reg_id) AS 挂号量,
       SUM(r.reg_fee)  AS 挂号收入
FROM department dept
LEFT JOIN registration r ON dept.dept_id = r.dept_id
GROUP BY dept.dept_id, dept.dept_name
ORDER BY 挂号收入 DESC;

-- 9. 查询挂号量最多的前 3 位医生
SELECT d.doctor_name AS 医生, dept.dept_name AS 科室,
       COUNT(r.reg_id) AS 接诊量
FROM doctor d
JOIN department dept ON d.dept_id = dept.dept_id
LEFT JOIN registration r ON d.doctor_id = r.doctor_id
GROUP BY d.doctor_id, d.doctor_name, dept.dept_name
ORDER BY 接诊量 DESC
LIMIT 3;

-- 10. 统计各职称的医生人数
SELECT title AS 职称, COUNT(*) AS 人数
FROM doctor
GROUP BY title;

-- 11. 查询库存低于 500 的药品（库存预警）
SELECT medicine_name AS 药品, spec AS 规格, stock AS 库存
FROM medicine
WHERE stock < 500
ORDER BY stock ASC;

-- 12. 查询某时间段内的挂号记录（日期范围查询）
SELECT r.reg_id, p.patient_name, r.reg_date
FROM registration r
JOIN patient p ON r.patient_id = p.patient_id
WHERE r.reg_date BETWEEN '2026-09-01 00:00:00' AND '2026-09-02 23:59:59';

-- ------------------------------------------------------------
-- 三、进阶查询（子查询 / 聚合 / 分页）
-- ------------------------------------------------------------

-- 13. 查询挂号费高于平均挂号费的记录（子查询）
SELECT r.reg_id, p.patient_name, r.reg_fee
FROM registration r
JOIN patient p ON r.patient_id = p.patient_id
WHERE r.reg_fee > (SELECT AVG(reg_fee) FROM registration);

-- 14. 查询每位患者的挂号次数
SELECT p.patient_name AS 患者, COUNT(r.reg_id) AS 挂号次数
FROM patient p
LEFT JOIN registration r ON p.patient_id = r.patient_id
GROUP BY p.patient_id, p.patient_name
ORDER BY 挂号次数 DESC;

-- 15. 查询有就诊记录的患者的病历详情（多表联查）
SELECT p.patient_name AS 患者, d.doctor_name AS 医生,
       m.visit_date AS 就诊时间, m.diagnosis AS 诊断
FROM medical_record m
JOIN patient p ON m.patient_id = p.patient_id
JOIN doctor d  ON m.doctor_id  = d.doctor_id
ORDER BY m.visit_date;

-- 16. 查询某份处方的药品明细与总金额
SELECT mr.record_id AS 病历号, p.patient_name AS 患者,
       med.medicine_name AS 药品, pd.quantity AS 数量,
       med.price AS 单价,
       pd.quantity * med.price AS 小计
FROM prescription_detail pd
JOIN medical_record mr ON pd.record_id = mr.record_id
JOIN patient p         ON mr.patient_id = p.patient_id
JOIN medicine med      ON pd.medicine_id = med.medicine_id
WHERE mr.record_id = 1;

-- 17. 分页查询：每页 3 条，查询第 2 页的挂号记录
SELECT r.reg_id, p.patient_name, r.reg_date
FROM registration r
JOIN patient p ON r.patient_id = p.patient_id
ORDER BY r.reg_id
LIMIT 3 OFFSET 3;

-- ------------------------------------------------------------
-- 六、收费与收入统计（收费单 / 收费明细 / 员工 / 操作日志）
-- ------------------------------------------------------------

-- 18. 收费单明细：一次挂号收了哪几笔钱，各多少钱
SELECT c.charge_no, c.charge_status, c.total_fee,
       cd.item_type, cd.item_name, cd.unit_price, cd.quantity, cd.amount
FROM charge c
JOIN charge_detail cd ON c.charge_id = cd.charge_id
WHERE c.reg_id = 1
ORDER BY cd.cd_id;

-- 19. 今日收入 + 累计收入 + 退费金额（一次查询算完，避免多次往返数据库）
SELECT
    COUNT(*) AS 收费单数,
    SUM(CASE WHEN charge_status = '已收费' AND DATE(charge_time) = CURDATE()
             THEN total_fee END) AS 今日收入,
    SUM(CASE WHEN charge_status = '已收费' THEN total_fee END) AS 累计收入,
    SUM(CASE WHEN charge_status = '已退费' THEN total_fee END) AS 退费金额,
    SUM(charge_status = '已退费') AS 退费笔数
FROM charge;

-- 20. 收入构成：挂号费与药费各占多少
SELECT
    SUM(reg_fee)  AS 挂号费收入,
    SUM(drug_fee) AS 药品费收入,
    SUM(reg_fee) + SUM(drug_fee) AS 合计
FROM charge
WHERE charge_status = '已收费';

-- 21. 各科目（科室）收入排行：按科室汇总已收费金额
SELECT d.dept_name AS 科室, COUNT(c.charge_id) AS 收费笔数,
       SUM(c.total_fee) AS 收入
FROM charge c
JOIN registration r ON c.reg_id = r.reg_id
JOIN department d   ON r.dept_id = d.dept_id
WHERE c.charge_status = '已收费'
GROUP BY d.dept_id, d.dept_name
ORDER BY 收入 DESC;

-- 22. 支付方式分布（按金额）
SELECT pay_method AS 支付方式, COUNT(*) AS 笔数, SUM(total_fee) AS 金额
FROM charge
WHERE charge_status = '已收费'
GROUP BY pay_method
ORDER BY 金额 DESC;

-- 23. 近 7 天每日收入趋势
SELECT DATE(charge_time) AS 日期, COUNT(*) AS 笔数, SUM(total_fee) AS 收入
FROM charge
WHERE charge_status = '已收费'
  AND charge_time >= CURDATE() - INTERVAL 6 DAY
GROUP BY DATE(charge_time)
ORDER BY 日期;

-- 24. 退费明细（谁退的、为什么退、退了多少钱）
SELECT c.charge_no, p.patient_name, c.total_fee,
       c.refund_time, c.refund_reason, c.refund_operator
FROM charge c
JOIN patient p ON c.patient_id = p.patient_id
WHERE c.charge_status = '已退费'
ORDER BY c.refund_time DESC;

-- 25. 退费后药品退回库存的流水（退药入库）
SELECT l.log_id, m.medicine_name, l.quantity, l.before_stock, l.after_stock,
       l.reason, l.create_time
FROM medicine_stock_log l
JOIN medicine m ON l.medicine_id = m.medicine_id
WHERE l.reason LIKE '退药入库%'
ORDER BY l.create_time DESC;

-- 26. 员工列表与各自的操作次数（左连接，没操作过的也显示）
SELECT s.username, s.real_name, s.role,
       COUNT(o.log_id) AS 操作次数, MAX(o.create_time) AS 最近操作时间
FROM staff s
LEFT JOIN operation_log o ON s.staff_id = o.staff_id
GROUP BY s.staff_id, s.username, s.real_name, s.role
ORDER BY 操作次数 DESC;

-- 27. 某个员工做过什么（按时间倒序）
SELECT action, target, detail, create_time
FROM operation_log
WHERE staff_id = 1
ORDER BY create_time DESC;

-- 28. 找出"已就诊但还没收费"的挂号（收费处的待办清单）
SELECT r.reg_id, p.patient_name, doc.doctor_name, r.reg_date, r.reg_fee
FROM registration r
JOIN patient p   ON r.patient_id = p.patient_id
JOIN doctor doc  ON r.doctor_id  = doc.doctor_id
WHERE r.reg_status = '已就诊'
ORDER BY r.reg_date;

-- ------------------------------------------------------------
-- 七、号源排班 / 候诊 / 发药
-- ------------------------------------------------------------

-- 29. 某天各医生的号源使用情况（放号 / 已挂 / 剩余 / 占用率）
SELECT doc.doctor_name, d.dept_name, s.time_period, s.total_quota, s.booked_count,
       s.total_quota - s.booked_count AS 剩余,
       CONCAT(ROUND(s.booked_count * 100 / s.total_quota), '%') AS 占用率, s.status
FROM doctor_schedule s
JOIN doctor doc      ON s.doctor_id = doc.doctor_id
LEFT JOIN department d ON doc.dept_id = d.dept_id
WHERE s.work_date = CURDATE()
ORDER BY s.status, 占用率 DESC;

-- 30. 某天某时段的候诊队列（按候诊号排序，叫过号的排后面）
SELECT r.queue_no AS 候诊号, p.patient_name, p.gender,
       doc.doctor_name, r.reg_status, r.called_time, r.visit_type
FROM registration r
JOIN doctor_schedule s ON r.schedule_id = s.schedule_id
JOIN patient p   ON r.patient_id = p.patient_id
JOIN doctor  doc ON r.doctor_id  = doc.doctor_id
WHERE s.work_date = CURDATE() AND r.time_period = '上午'
  AND r.reg_status = '已挂号'
ORDER BY (r.called_time IS NOT NULL), r.queue_no;

-- 31. 某天某时段候诊人数排行（哪些医生候诊压力大）
SELECT doc.doctor_name, d.dept_name, COUNT(*) AS 候诊人数
FROM registration r
JOIN doctor_schedule s ON r.schedule_id = s.schedule_id
JOIN doctor doc ON r.doctor_id = doc.doctor_id
JOIN department d ON r.dept_id = d.dept_id
WHERE s.work_date = CURDATE() AND r.reg_status = '已挂号'
GROUP BY doc.doctor_id, doc.doctor_name, d.dept_name
ORDER BY 候诊人数 DESC;

-- 32. 待发药清单（收费处已收钱、药房还没发药的挂号）
SELECT r.reg_id, p.patient_name, doc.doctor_name, c.charge_no, c.total_fee,
       c.charge_time,
       TIMESTAMPDIFF(MINUTE, c.charge_time, NOW()) AS 已等待分钟
FROM registration r
JOIN charge c    ON c.reg_id  = r.reg_id
JOIN patient p   ON r.patient_id = p.patient_id
JOIN doctor doc  ON r.doctor_id = doc.doctor_id
WHERE r.reg_status = '已收费' AND c.charge_status = '已收费'
ORDER BY c.charge_time;

-- 33. 发药记录（含药师与发药 / 退回时间）
SELECT dp.dispense_id, r.reg_id, p.patient_name, doc.doctor_name,
       dp.dispense_status, dp.pharmacist, dp.dispense_time, dp.return_time
FROM dispense dp
JOIN registration r ON dp.reg_id = r.reg_id
JOIN patient p      ON r.patient_id = p.patient_id
JOIN doctor doc     ON r.doctor_id = doc.doctor_id
ORDER BY dp.dispense_time DESC;

-- 34. 退费退药统计（发了药又退回来的）
SELECT dp.pharmacist AS 药师, COUNT(*) AS 发药笔数,
       SUM(dp.dispense_status = '已退回') AS 退回笔数
FROM dispense dp
GROUP BY dp.pharmacist;

-- 35. 号源对账：排班里记的已挂数，必须等于该班次"未取消"的挂号条数
--     查出来是空集才算对得上
SELECT s.schedule_id, s.booked_count AS 表里记的,
       (SELECT COUNT(*) FROM registration r
         WHERE r.schedule_id = s.schedule_id AND r.reg_status <> '已取消') AS 实际挂号数
FROM doctor_schedule s
WHERE s.booked_count <> (SELECT COUNT(*) FROM registration r
                          WHERE r.schedule_id = s.schedule_id AND r.reg_status <> '已取消');

-- 36. 一位医生未来几天的出诊安排
SELECT s.work_date AS 出诊日期, s.time_period AS 时段,
       s.total_quota AS 号源, s.booked_count AS 已挂,
       s.total_quota - s.booked_count AS 剩余, s.status
FROM doctor_schedule s
WHERE s.doctor_id = 1 AND s.work_date >= CURDATE()
ORDER BY s.work_date, s.time_period;

-- 37. 当前在院患者清单（住院天数含入院当天，所以 +1）
SELECT a.admission_no AS 住院号, p.patient_name AS 患者, d.dept_name AS 科室,
       doc.doctor_name AS 主治医生, a.bed_no AS 床位, a.admit_time AS 入院时间,
       TIMESTAMPDIFF(DAY, a.admit_time, NOW()) + 1 AS 已住院天数,
       a.admit_diagnosis AS 入院诊断
FROM admission a
JOIN patient p    ON a.patient_id = p.patient_id
JOIN department d ON a.dept_id    = d.dept_id
JOIN doctor doc ON a.doctor_id    = doc.doctor_id
WHERE a.status = '在院'
ORDER BY a.admit_time;

-- 38. 各科室住院人次与平均住院天数
--     平均住院天数只统计"已出院"的（在院的还没结束，算平均会偏低）
SELECT d.dept_name AS 科室, COUNT(*) AS 住院人次,
       SUM(a.status = '在院') AS 在院人数,
       SUM(a.status = '已出院') AS 已出院人次,
       ROUND(AVG(CASE WHEN a.status = '已出院'
                      THEN DATEDIFF(a.discharge_time, a.admit_time) + 1 END), 1) AS 平均住院天数
FROM admission a
JOIN department d ON a.dept_id = d.dept_id
GROUP BY d.dept_id, d.dept_name
ORDER BY 住院人次 DESC;

-- 39. 在院唯一性对账：一个患者同一时间最多只能有一条"在院"记录
--     查出来是空集才算对得上（这条不变式由表上的生成列 + 唯一索引兜底，不只是靠代码判断）
SELECT a.patient_id, p.patient_name, COUNT(*) AS 在院记录数
FROM admission a
JOIN patient p ON a.patient_id = p.patient_id
WHERE a.status = '在院'
GROUP BY a.patient_id, p.patient_name
HAVING COUNT(*) > 1;

