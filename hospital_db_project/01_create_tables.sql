-- ============================================================
-- 医院信息管理系统 数据库设计
-- 数据库：hospital_db
-- 说明：包含患者、医生、科室、排班、挂号、病历、药品、收费、发药、住院等核心业务表
-- 共 15 张表
-- 作者：陈嘉岩
-- ============================================================

DROP DATABASE IF EXISTS hospital_db;
CREATE DATABASE hospital_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE hospital_db;

-- ------------------------------------------------------------
-- 1. 科室表 department
-- ------------------------------------------------------------
CREATE TABLE department (
    dept_id       INT PRIMARY KEY AUTO_INCREMENT COMMENT '科室编号',
    dept_name     VARCHAR(50)  NOT NULL COMMENT '科室名称',
    dept_location VARCHAR(100) COMMENT '科室位置',
    dept_phone    VARCHAR(20)  COMMENT '科室电话',
    -- 停用的科室不再出现在"新增挂号"的科室下拉里，但它名下的历史挂号/病历照常显示。
    -- 基础数据一律用"标记停用"而不是物理删除，否则会连带毁掉历史数据。
    status        VARCHAR(4)   NOT NULL DEFAULT '启用' COMMENT '状态：启用/停用',
    create_time   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
) COMMENT '科室信息表';

-- ------------------------------------------------------------
-- 2. 医生表 doctor
-- ------------------------------------------------------------
CREATE TABLE doctor (
    doctor_id     INT PRIMARY KEY AUTO_INCREMENT COMMENT '医生编号',
    doctor_name   VARCHAR(30)  NOT NULL COMMENT '医生姓名',
    gender        CHAR(1)      NOT NULL COMMENT '性别：男/女',
    title         VARCHAR(20)  COMMENT '职称：主任医师/副主任医师/主治医师/住院医师',
    dept_id       INT          COMMENT '所属科室编号',
    specialty     VARCHAR(100) COMMENT '擅长领域',
    phone         VARCHAR(20)  COMMENT '联系电话',
    -- 医生离职不删记录：历史挂号/病历/排班都要留痕，只把状态改成"离职"，
    -- 之后的"新增挂号 / 新增排班"下拉里就不会再出现他。
    status        VARCHAR(4)   NOT NULL DEFAULT '在职' COMMENT '状态：在职/离职',
    create_time   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '入职/创建时间',
    CONSTRAINT fk_doctor_dept FOREIGN KEY (dept_id) REFERENCES department(dept_id)
) COMMENT '医生信息表';

-- ------------------------------------------------------------
-- 3. 患者表 patient
-- ------------------------------------------------------------
CREATE TABLE patient (
    patient_id      INT PRIMARY KEY AUTO_INCREMENT COMMENT '患者编号',
    patient_name    VARCHAR(30)  NOT NULL COMMENT '患者姓名',
    gender          CHAR(1)      NOT NULL COMMENT '性别：男/女',
    birth_date      DATE         COMMENT '出生日期',
    id_card         VARCHAR(18)  UNIQUE COMMENT '身份证号',
    phone           VARCHAR(20)  COMMENT '联系电话',
    address         VARCHAR(200) COMMENT '家庭住址',
    create_time     DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '建档时间'
) COMMENT '患者信息表';

-- ------------------------------------------------------------
-- 4. 医生排班表 doctor_schedule
--     解决"号源"问题：某位医生某天某个时段放多少个号，
--     挂满即止。挂号时按排班占用号源，取消挂号则释放。
--     唯一键 (doctor_id, work_date, time_period) 保证一个医生
--     一天一个时段只有一条排班，不会重复放号。
-- ------------------------------------------------------------
CREATE TABLE doctor_schedule (
    schedule_id  INT PRIMARY KEY AUTO_INCREMENT COMMENT '排班编号',
    doctor_id    INT          NOT NULL COMMENT '医生编号',
    work_date    DATE         NOT NULL COMMENT '出诊日期',
    time_period  VARCHAR(4)   NOT NULL COMMENT '时段：上午/下午',
    total_quota  INT          NOT NULL DEFAULT 30 COMMENT '放号总数（号源）',
    booked_count INT          NOT NULL DEFAULT 0  COMMENT '已挂出号数',
    status       VARCHAR(4)   NOT NULL DEFAULT '正常' COMMENT '状态：正常/停诊',
    create_time  DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    CONSTRAINT uk_sched_unique    UNIQUE (doctor_id, work_date, time_period),
    CONSTRAINT fk_sched_doctor    FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id)
) COMMENT '医生排班与号源表';

-- ------------------------------------------------------------
-- 5. 挂号表 registration
-- ------------------------------------------------------------
CREATE TABLE registration (
    reg_id        INT PRIMARY KEY AUTO_INCREMENT COMMENT '挂号编号',
    patient_id    INT          NOT NULL COMMENT '患者编号',
    doctor_id     INT          NOT NULL COMMENT '医生编号',
    dept_id       INT          NOT NULL COMMENT '科室编号',
    schedule_id   INT          NULL COMMENT '关联排班编号（占用的号源）',
    reg_date      DATETIME     NOT NULL COMMENT '挂号时间',
    time_period   VARCHAR(4)   NULL COMMENT '就诊时段：上午/下午',
    queue_no      INT          NULL COMMENT '当日该医生的候诊序号',
    called_time   DATETIME     NULL COMMENT '最近一次叫号时间',
    reg_fee       DECIMAL(8,2) NOT NULL DEFAULT 0.00 COMMENT '挂号费',
    reg_status    VARCHAR(10)  NOT NULL DEFAULT '已挂号'
                  COMMENT '状态：已挂号/已就诊/已收费/已发药/已退费/已取消',
    visit_type    VARCHAR(10)  NOT NULL DEFAULT '初诊' COMMENT '就诊类型：初诊/复诊',
    parent_reg_id INT          NULL COMMENT '复诊时关联的原挂号编号（初诊为 NULL）',
    CONSTRAINT fk_reg_patient  FOREIGN KEY (patient_id) REFERENCES patient(patient_id),
    CONSTRAINT fk_reg_doctor   FOREIGN KEY (doctor_id)  REFERENCES doctor(doctor_id),
    CONSTRAINT fk_reg_dept     FOREIGN KEY (dept_id)    REFERENCES department(dept_id),
    CONSTRAINT fk_reg_schedule FOREIGN KEY (schedule_id) REFERENCES doctor_schedule(schedule_id),
    CONSTRAINT fk_reg_parent   FOREIGN KEY (parent_reg_id) REFERENCES registration(reg_id)
) COMMENT '挂号记录表';

-- ------------------------------------------------------------
-- 6. 病历表 medical_record
-- ------------------------------------------------------------
CREATE TABLE medical_record (
    record_id     INT PRIMARY KEY AUTO_INCREMENT COMMENT '病历编号',
    patient_id    INT          NOT NULL COMMENT '患者编号',
    doctor_id     INT          NOT NULL COMMENT '接诊医生编号',
    reg_id        INT          COMMENT '关联挂号编号',
    visit_date    DATETIME     NOT NULL COMMENT '就诊时间',
    diagnosis     VARCHAR(200) COMMENT '诊断结果',
    prescription  TEXT         COMMENT '处方内容',
    remark        VARCHAR(200) COMMENT '备注',
    CONSTRAINT fk_record_patient FOREIGN KEY (patient_id) REFERENCES patient(patient_id),
    CONSTRAINT fk_record_doctor  FOREIGN KEY (doctor_id)  REFERENCES doctor(doctor_id),
    CONSTRAINT fk_record_reg     FOREIGN KEY (reg_id)     REFERENCES registration(reg_id)
) COMMENT '病历记录表';

-- ------------------------------------------------------------
-- 7. 药品表 medicine
-- ------------------------------------------------------------
CREATE TABLE medicine (
    medicine_id   INT PRIMARY KEY AUTO_INCREMENT COMMENT '药品编号',
    medicine_name VARCHAR(100) NOT NULL COMMENT '药品名称',
    category      VARCHAR(30)  COMMENT '药品分类：抗感染/心血管/消化/呼吸/内分泌/解热镇痛/神经/维生素/中成药/注射输液/皮肤五官',
    spec          VARCHAR(50)  COMMENT '规格',
    unit          VARCHAR(10)  COMMENT '单位',
    price         DECIMAL(8,2) NOT NULL COMMENT '单价',
    stock         INT          NOT NULL DEFAULT 0 COMMENT '库存数量',
    manufacturer  VARCHAR(100) COMMENT '生产厂家',
    -- 停用（淘汰）的药品不再出现在"开处方"的药品下拉里，但历史处方与收费快照仍可查。
    status        VARCHAR(4)   NOT NULL DEFAULT '在用' COMMENT '状态：在用/停用'
) COMMENT '药品信息表';

-- ------------------------------------------------------------
-- 8. 处方明细表 prescription_detail
-- ------------------------------------------------------------
CREATE TABLE prescription_detail (
    detail_id     INT PRIMARY KEY AUTO_INCREMENT COMMENT '明细编号',
    record_id     INT           NOT NULL COMMENT '病历编号',
    medicine_id   INT           NOT NULL COMMENT '药品编号',
    quantity      INT           NOT NULL COMMENT '数量',
    dosage        VARCHAR(100)  COMMENT '用法用量',
    CONSTRAINT fk_detail_record   FOREIGN KEY (record_id)   REFERENCES medical_record(record_id),
    CONSTRAINT fk_detail_medicine FOREIGN KEY (medicine_id) REFERENCES medicine(medicine_id)
) COMMENT '处方明细表';

-- ------------------------------------------------------------
-- 9. 药品出入库流水表 medicine_stock_log
--    每一次库存变动都留一条记录，做到可追溯
-- ------------------------------------------------------------
CREATE TABLE medicine_stock_log (
    log_id        INT PRIMARY KEY AUTO_INCREMENT COMMENT '流水编号',
    medicine_id   INT          NOT NULL COMMENT '药品编号',
    change_type   VARCHAR(10)  NOT NULL COMMENT '变动类型：入库/出库',
    quantity      INT          NOT NULL COMMENT '变动数量（正数）',
    before_stock  INT          NOT NULL COMMENT '变动前库存',
    after_stock   INT          NOT NULL COMMENT '变动后库存',
    reason        VARCHAR(100) COMMENT '原因：采购入库/处方扣减/报损出库等',
    create_time   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '变动时间',
    CONSTRAINT fk_log_medicine FOREIGN KEY (medicine_id) REFERENCES medicine(medicine_id)
) COMMENT '药品出入库流水表';

-- ------------------------------------------------------------
-- 10. 收费单表 charge
--     一次挂号对应一张收费单：挂号费 + 药费 合并结算
-- ------------------------------------------------------------
CREATE TABLE charge (
    charge_id     INT PRIMARY KEY AUTO_INCREMENT COMMENT '收费单编号',
    charge_no     VARCHAR(24)  NOT NULL UNIQUE COMMENT '收费单号，形如 SF202610140001',
    reg_id        INT          NOT NULL UNIQUE COMMENT '关联挂号编号（一次挂号只收一次费）',
    patient_id    INT          NOT NULL COMMENT '患者编号',
    reg_fee       DECIMAL(8,2) NOT NULL DEFAULT 0.00 COMMENT '挂号费',
    drug_fee      DECIMAL(8,2) NOT NULL DEFAULT 0.00 COMMENT '药费合计',
    total_fee     DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '应收合计 = 挂号费 + 药费',
    pay_method    VARCHAR(10)  NOT NULL DEFAULT '现金' COMMENT '支付方式：现金/微信/支付宝/银行卡',
    charge_status VARCHAR(10)  NOT NULL DEFAULT '已收费' COMMENT '状态：已收费/已退费',
    charge_time   DATETIME     NOT NULL COMMENT '收费时间',
    operator      VARCHAR(30)  COMMENT '收费员',
    refund_time   DATETIME     NULL COMMENT '退费时间',
    refund_reason VARCHAR(100) NULL COMMENT '退费原因',
    refund_operator VARCHAR(30) NULL COMMENT '退费操作员',
    CONSTRAINT fk_charge_reg     FOREIGN KEY (reg_id)     REFERENCES registration(reg_id),
    CONSTRAINT fk_charge_patient FOREIGN KEY (patient_id) REFERENCES patient(patient_id)
) COMMENT '门诊收费单表';

-- ------------------------------------------------------------
-- 11. 收费明细表 charge_detail
--     收费那一刻把每一笔费用"拍个照"存下来（快照），
--     以后药品调价也不会把历史账目改掉
-- ------------------------------------------------------------
CREATE TABLE charge_detail (
    cd_id       INT PRIMARY KEY AUTO_INCREMENT COMMENT '明细编号',
    charge_id   INT           NOT NULL COMMENT '收费单编号',
    item_type   VARCHAR(10)   NOT NULL COMMENT '费用类型：挂号费/药品费',
    medicine_id INT           NULL COMMENT '药品编号（挂号费为 NULL）',
    item_name   VARCHAR(100)  NOT NULL COMMENT '项目名称快照',
    unit_price  DECIMAL(8,2)  NOT NULL COMMENT '单价快照',
    quantity    INT           NOT NULL DEFAULT 1 COMMENT '数量',
    amount      DECIMAL(10,2) NOT NULL COMMENT '金额 = 单价 × 数量',
    CONSTRAINT fk_cd_charge FOREIGN KEY (charge_id) REFERENCES charge(charge_id)
) COMMENT '收费明细表（费用快照）';

-- ------------------------------------------------------------
-- 12. 员工表 staff
--     登录账号 + 角色。密码只存哈希，不存明文
-- ------------------------------------------------------------
CREATE TABLE staff (
    staff_id      INT PRIMARY KEY AUTO_INCREMENT COMMENT '员工编号',
    username      VARCHAR(30)  NOT NULL UNIQUE COMMENT '登录账号',
    password_hash VARCHAR(255) NOT NULL COMMENT '密码哈希（scrypt，不存明文）',
    real_name     VARCHAR(30)  NOT NULL COMMENT '姓名',
    role          VARCHAR(20)  NOT NULL COMMENT '角色：管理员/挂号员/医生/药房/收费员',
    phone         VARCHAR(20)  COMMENT '联系电话',
    status        TINYINT      NOT NULL DEFAULT 1 COMMENT '1 启用 / 0 停用',
    last_login    DATETIME     NULL COMMENT '最近登录时间',
    create_time   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '建档时间'
) COMMENT '员工与登录账号表';

-- ------------------------------------------------------------
-- 13. 操作日志表 operation_log
--     谁、什么时候、对哪条数据、做了什么
-- ------------------------------------------------------------
CREATE TABLE operation_log (
    log_id      INT PRIMARY KEY AUTO_INCREMENT COMMENT '日志编号',
    staff_id    INT          NULL COMMENT '操作人编号（未登录操作为 NULL）',
    staff_name  VARCHAR(30)  COMMENT '操作人姓名',
    role        VARCHAR(20)  COMMENT '操作人角色',
    action      VARCHAR(30)  NOT NULL COMMENT '动作：登录/收费/退费/发药/入库/出库/接诊开方/新增挂号/状态变更',
    target      VARCHAR(50)  COMMENT '操作对象，如 挂号 #12、收费单 SF202610140001',
    detail      VARCHAR(200) COMMENT '补充说明',
    ip          VARCHAR(45)  COMMENT '来源 IP',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间'
) COMMENT '操作日志表';

-- ------------------------------------------------------------
-- 14. 药房发药记录表 dispense
--     收费之后、患者取药之前，由药师核对处方并发药。
--     退费时如果药已经发出，要把这条记录置为「已退回」，
--     药品同时退回库存，形成完整的反向流程。
-- ------------------------------------------------------------
CREATE TABLE dispense (
    dispense_id     INT PRIMARY KEY AUTO_INCREMENT COMMENT '发药编号',
    reg_id          INT          NOT NULL UNIQUE COMMENT '关联挂号编号（一次挂号只发一次）',
    record_id       INT          NULL COMMENT '关联病历编号',
    dispense_status VARCHAR(8)   NOT NULL DEFAULT '已发药' COMMENT '状态：已发药/已退回',
    pharmacist      VARCHAR(30)  COMMENT '发药药师',
    dispense_time   DATETIME     NOT NULL COMMENT '发药时间',
    return_time     DATETIME     NULL COMMENT '退回时间（退费退药时填写）',
    remark          VARCHAR(100) COMMENT '备注',
    CONSTRAINT fk_disp_reg    FOREIGN KEY (reg_id)    REFERENCES registration(reg_id),
    CONSTRAINT fk_disp_record FOREIGN KEY (record_id) REFERENCES medical_record(record_id)
) COMMENT '药房发药记录表';

-- ------------------------------------------------------------
-- 15. 住院登记表 admission
--     住院是一次"事件"，不是患者身上的一个开关：同一个患者可以
--     住院多次，每住一次就留一条记录（入院 → 出院）。
--
--     难点是"一个患者同一时间只能在院一次"这条约束：
--     MySQL 没有部分唯一索引（只能约束"满足条件的那些行"），
--     这里用一个生成列来绕过去 —— current_patient_id 在院时等于
--     patient_id，出院时是 NULL；而唯一索引不约束 NULL，于是
--     "在院最多一条、已出院可以有任意多条"由数据库自己保证，
--     不依赖应用层先查再插（那种做法在并发下会漏）。
-- ------------------------------------------------------------
CREATE TABLE admission (
    admission_id      INT PRIMARY KEY AUTO_INCREMENT COMMENT '住院登记编号',
    admission_no      VARCHAR(24)  NOT NULL UNIQUE COMMENT '住院号，形如 ZY202610140001',
    patient_id        INT          NOT NULL COMMENT '患者编号',
    dept_id           INT          NOT NULL COMMENT '住院科室',
    doctor_id         INT          NOT NULL COMMENT '主治医生',
    bed_no            VARCHAR(20)  COMMENT '床位号，如 心血管内科-08',
    admit_diagnosis   VARCHAR(200) COMMENT '入院诊断',
    admit_time        DATETIME     NOT NULL COMMENT '入院时间',
    discharge_time    DATETIME     NULL COMMENT '出院时间（在院为 NULL）',
    status            VARCHAR(6)   NOT NULL DEFAULT '在院' COMMENT '状态：在院/已出院',
    discharge_summary VARCHAR(200) NULL COMMENT '出院小结',
    admit_operator    VARCHAR(30)  COMMENT '办理入院的员工',
    discharge_operator VARCHAR(30) NULL COMMENT '办理出院的员工',
    remark            VARCHAR(200) COMMENT '备注',
    create_time       DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    current_patient_id INT GENERATED ALWAYS AS (IF(status = '在院', patient_id, NULL)) STORED
                       COMMENT '在院去重键：在院=患者编号，出院=NULL',
    CONSTRAINT uk_adm_in_ward  UNIQUE (current_patient_id),
    CONSTRAINT ck_adm_time     CHECK (discharge_time IS NULL OR discharge_time >= admit_time),
    CONSTRAINT ck_adm_discharge CHECK ((status = '在院'   AND discharge_time IS NULL)
                                    OR (status = '已出院' AND discharge_time IS NOT NULL)),
    CONSTRAINT fk_adm_patient  FOREIGN KEY (patient_id) REFERENCES patient(patient_id),
    CONSTRAINT fk_adm_dept     FOREIGN KEY (dept_id)    REFERENCES department(dept_id),
    CONSTRAINT fk_adm_doctor   FOREIGN KEY (doctor_id)  REFERENCES doctor(doctor_id)
) COMMENT '住院登记表';

-- ------------------------------------------------------------
-- 索引（提升常用查询性能）
-- ------------------------------------------------------------
CREATE INDEX idx_reg_date      ON registration(reg_date);
CREATE INDEX idx_reg_status    ON registration(reg_status);
CREATE INDEX idx_reg_schedule  ON registration(schedule_id);
CREATE INDEX idx_record_date   ON medical_record(visit_date);
CREATE INDEX idx_patient_phone ON patient(phone);
CREATE INDEX idx_doctor_dept   ON doctor(dept_id);
CREATE INDEX idx_log_medicine  ON medicine_stock_log(medicine_id);
CREATE INDEX idx_log_time      ON medicine_stock_log(create_time);
CREATE INDEX idx_charge_time   ON charge(charge_time);
CREATE INDEX idx_charge_status ON charge(charge_status);
CREATE INDEX idx_cd_charge     ON charge_detail(charge_id);
CREATE INDEX idx_oplog_time    ON operation_log(create_time);
CREATE INDEX idx_oplog_staff   ON operation_log(staff_id);
CREATE INDEX idx_sched_date    ON doctor_schedule(work_date);
CREATE INDEX idx_sched_doctor  ON doctor_schedule(doctor_id);
CREATE INDEX idx_disp_time     ON dispense(dispense_time);
CREATE INDEX idx_adm_patient   ON admission(patient_id);
CREATE INDEX idx_adm_status    ON admission(status);
CREATE INDEX idx_adm_time      ON admission(admit_time);
CREATE INDEX idx_adm_dept      ON admission(dept_id);
