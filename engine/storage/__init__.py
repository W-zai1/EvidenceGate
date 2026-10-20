"""数据层：SQLite 连接、schema、迁移。

    db.py      连接、schema、迁移（SCHEMA_VERSION = 10）、now_iso()、tx()
    store.py   岗位与候选人仓储（人才筛选场景）
    talent.py  人才库聚合（按 people 表，不按姓名）

**加字段的唯一正确方式**：新增一个 @_migration(N) 函数，里面用
PRAGMA table_info 判断列是否存在再 ALTER TABLE，并推高 SCHEMA_VERSION。
不要在业务层写裸 SQL，也不要直接 ALTER 线上库。

**已知坑**：now_iso() 只到秒。任何「先后顺序」判断都不要用时间戳比较，
用单调计数器（见 graph/cases.py 的 revision）。
"""
