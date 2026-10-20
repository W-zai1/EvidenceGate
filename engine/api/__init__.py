"""接入层：HTTP 路由、鉴权、静态资源。

**这一层不含业务判断。** 路由函数只做「取参数 → 调 graph → 返回 JSON」。
如果某个路由里出现了 `if 金额 > ...` 这样的判断，那它放错层了。

    server.py       HTTP 服务器（零依赖）：静态资源 + 路由分发
    api.py          路由表 + 各接口实现（证据链路由在文件末尾）
    auth.py         账号、口令、会话、角色、CSRF；权限点如 case.sign / case.export
    http_base.py    请求解析、响应输出、鉴权闸门

权限矩阵要点：
    admin   全部
    hr      案件读写 + 签署 + 导出 + profile/校验读取（业务主要使用者）
    viewer  只读（case.read / profile.read / validation.read）
"""
