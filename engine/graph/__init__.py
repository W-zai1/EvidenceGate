"""案件层：把 spine 的纯函数接到真实世界——存起来、读出来、跑一遍。

    cases.py       案件增删改查、材料入库、签署、跑闸门、导出证据包
    validation.py  8 项证据链自检，产出「黄金线索」报告

**这一层是唯一知道「数据库」和「当前时间」的地方。**

一个关键机制：案件维护两个单调递增计数器 revision / gate_revision。
求值闸门时 gate_revision = 当时的 revision；之后任何改动推高 revision，
结论自动判为过期，导出被拒。

为什么不用时间戳比较：now_iso() 只到秒。同一秒内「求值完又改材料」时
updated_at == evaluated_at，严格大于比不出来，过期结论会被当成有效的导出去。
"""
