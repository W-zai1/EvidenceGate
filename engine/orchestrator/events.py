"""编排事件总线（orchestrator 层）。

把"采集 → 抽取 → 归属 → 闸门 → 留痕"各阶段之间的耦合，收敛成一个**只发事件、不互相
直接调用**的总线。其它模块订阅自己关心的事件，互不 import，互不阻塞。

事件就是普通 dict：{"type": "case.created", "case_id": ..., "at": ..., ...}。
总线保证：
- 订阅者抛异常不影响发布者与其它订阅者（异常被记录，不静默吞掉，也不上抛炸流程）；
- 订阅/取关是运行时的，便于测试注入桩。
"""


class EventBus:
    def __init__(self):
        self._subs = {}

    def on(self, event_type, handler):
        self._subs.setdefault(event_type, []).append(handler)
        return handler

    def off(self, event_type, handler):
        lst = self._subs.get(event_type)
        if lst and handler in lst:
            lst.remove(handler)

    def publish(self, event):
        et = event.get("type")
        handlers = list(self._subs.get(et, [])) + list(self._subs.get("*", []))
        errors = []
        for h in handlers:
            try:
                h(event)
            except Exception as e:  # 订阅者失败不得拖垮发布者/其它订阅者
                errors.append((h, e))
        if errors:
            names = ", ".join("%s:%s" % (type(h).__name__, e) for h, e in errors)
            raise OrchestrationError("事件 %r 处理失败: %s" % (et, names))
        return event


class OrchestrationError(RuntimeError):
    pass


# 进程级单例：全系统共用一条总线，模块借此解耦。
BUS = EventBus()
