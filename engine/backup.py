"""
数据库备份与恢复。

为什么这个模块不能省
--------------------
这套系统的核心承诺是"可追溯"：审计链、决策理由、评审历史、对齐标准答案，
全都只存在**一个 SQLite 文件**里。原来 grep `backup` 是零结果——
也就是说磁盘坏一次，所有"可追溯"的承诺同时归零，而且没有任何预告。

一条链的可信度等于它最弱的一环。审计链按哈希串起来防篡改，
却架不住文件消失，那这条链防的是最不重要的那种威胁。

做法
----
· 用 `sqlite3.Connection.backup()`（**在线备份**，不是 cp 文件）：
  WAL 模式下直接复制文件会拿到不一致的快照，这是经典陷阱。
· 备份文件做完整性校验（`PRAGMA integrity_check`），不完整的备份
  比没有备份更危险——它会让"我们有备份"变成一句假话。
· 自动轮转：只保留最近 N 份。
· 备份不是加密的：数据库里的简历字段本身是 enc:v1 密文，
  但**姓名、决策、审计记录是明文**。所以备份目录必须按敏感数据处理，
  不能随手扔到共享盘。这一点写在返回值里，不靠文档记性。

不做的事
--------
· 不做远程/异地备份（需要对象存储凭证，超出零依赖范围）。
  本模块只保证"本机有过一份可用的快照"，异地需要用户自己接。
· 不做自动恢复：恢复是破坏性操作，必须由人显式调用。
"""
import os
import shutil
import sqlite3
import time

import audit
import db

BACKUP_DIR_NAME = "backups"
DEFAULT_KEEP = 7
# 距上次备份超过这个小时数就在启动时补一份。
# 24 小时的理由：这是一台人工使用的工具，一天里总会开一两次；
# 更短会频繁产出几乎相同的快照，更长则"昨天做的决策今天机器坏了"就没得救。
AUTO_HOURS = 24

# 手动拷库的警告。SQLite 开着 WAL，未 checkpoint 时数据主要在 -wal 里，
# 主文件可能只有几 KB。只拷 talent.db 会得到一个**看起来正常但几乎是空**的库，
# 而这是"换台机器 demo"最常见的操作。所以这句话要在所有能拿到备份信息的地方出现。
MANUAL_COPY_WARNING = (
    "⚠ 不要直接拷 data/talent.db：WAL 模式下数据可能还在 talent.db-wal 里，"
    "单独拷主文件会得到空库。要么整个 data/ 目录一起拷（并先停服务），"
    "要么用「数据备份」功能（POST /api/backups/create）。"
)


def backup_dir():
    d = os.getenv("REFINE_BACKUP_DIR") or os.path.join(db.DATA_DIR, BACKUP_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _verify(path):
    """校验备份文件可读且完整。返回 (ok, 详情)。"""
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            status = (row[0] if row else "").lower()
            if status != "ok":
                return False, "integrity_check=%s" % (row[0] if row else "空")
            n_cand = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            n_audit = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            ver = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            return True, {"candidates": n_cand, "audit": n_audit,
                          "schema_version": int(ver or 0)}
        finally:
            conn.close()
    except Exception as e:
        return False, str(e)


def create(keep=DEFAULT_KEEP, actor="system", note=""):
    """创建一份在线备份并校验。

    返回 {ok, path, bytes, verified, stats, kept, pruned, warning}。
    失败时 ok=False 且 error 说明原因——**不抛异常**，因为备份失败
    不该让主流程崩掉，但必须让调用方看见。
    """
    db.init()
    out = {"ok": False, "path": "", "bytes": 0, "verified": False,
           "stats": {}, "kept": [], "pruned": [], "error": "", "warning": ""}
    d = backup_dir()
    # 文件名必须**唯一**，用微秒而不是秒级时间戳。
    #
    # 踩过的坑：原来只在"同秒且同名已存在"时才加一个
    # `int(time.time()*1000) % 1000` 的后缀。问题在于：
    # 那份同名旧文件可能刚被轮转删掉，于是 `os.path.exists` 为 False，
    # 新备份就**复用了旧文件名**。此时一个记住了旧文件名的人去恢复，
    # 拿到的是一份完全不同的、更新的快照——而界面上看不出任何异常。
    # 备份系统里"名字指向的内容会变"是最不该有的性质。
    stamp = time.strftime("%Y%m%d-%H%M%S")
    frac = int((time.time() % 1) * 1e6)
    path = os.path.join(d, "talent-%s-%06d.db" % (stamp, frac))
    n = 0
    while os.path.exists(path):     # 同一微秒内（理论可能）也保证不重名
        n += 1
        path = os.path.join(d, "talent-%s-%06d-%d.db" % (stamp, frac, n))

    try:
        src = db.connect()
        dst = sqlite3.connect(path)
        try:
            # 在线备份：WAL 模式下必须走 backup API，
            # 直接 copy 文件会拿到"主库 + 半个 WAL"的不一致快照。
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    except Exception as e:
        out["error"] = "备份写入失败：%s" % e
        return out

    ok, detail = _verify(path)
    out["path"] = path
    out["bytes"] = os.path.getsize(path) if os.path.exists(path) else 0
    out["verified"] = bool(ok)
    if ok:
        out["stats"] = detail
        out["ok"] = True
    else:
        out["error"] = "备份校验未通过：%s" % detail
        # 校验失败的文件留着会让人误以为有可用备份
        try:
            os.remove(path)
            out["path"] = ""
            out["bytes"] = 0
        except OSError:
            pass
        return out

    out["warning"] = ("备份中包含明文姓名、决策理由与审计记录；"
                      "简历正文是 enc:v1 密文但姓名不是。请按敏感数据保管。"
                      + MANUAL_COPY_WARNING)

    pruned = _rotate(d, int(keep))
    out["pruned"] = pruned
    out["kept"] = list_files()

    try:
        audit.record(db.connect(), "backup.create",
                     actor=actor or "system", target_type="system",
                     target_id="backup",
                     detail={"file": os.path.basename(path),
                             "bytes": out["bytes"],
                             "verified": out["verified"],
                             "pruned": len(pruned), "note": note[:200]})
    except Exception as e:
        out["warning"] += "（审计写入失败：%s）" % e
    return out


def _rotate(d, keep):
    """只保留最近 keep 份，其余删除。"""
    files = sorted(
        [os.path.join(d, f) for f in os.listdir(d)
         if f.startswith("talent-") and f.endswith(".db")],
        key=lambda p: os.path.getmtime(p), reverse=True)
    keep = max(1, int(keep))
    pruned = []
    for p in files[keep:]:
        try:
            os.remove(p)
            pruned.append(os.path.basename(p))
        except OSError:
            pass
    return pruned


def list_files():
    """列出所有备份（新的在前），带大小与时间。"""
    d = backup_dir()
    out = []
    for f in os.listdir(d):
        if not (f.startswith("talent-") and f.endswith(".db")):
            continue
        p = os.path.join(d, f)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"file": f, "bytes": st.st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                           time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def restore(src_path, actor="system", confirm=False):
    """从备份恢复。

    破坏性操作，必须 confirm=True。恢复前会**先给当前库再做一次备份**——
    否则"恢复了一个错的备份"就没有退路了。
    """
    if not confirm:
        raise ValueError("恢复是破坏性操作，需要显式确认。")
    if not os.path.exists(src_path):
        raise ValueError("备份文件不存在：%s" % src_path)
    ok, detail = _verify(src_path)
    if not ok:
        raise ValueError("备份文件校验未通过，拒绝恢复：%s" % detail)

    # 先备份现状，留退路
    safety = create(keep=DEFAULT_KEEP, actor=actor, note="恢复前自动备份")

    # 用 backup API 反向写回，而不是 copy 文件。
    #
    # 踩过的坑：`shutil.copy2(src, DB_PATH)` 会留下旧的 `-wal` 文件，
    # 而 SQLite 下次打开时会把 WAL 里的**旧提交**重放到新主库上——
    # 于是"恢复"看起来成功了，数据却还是恢复前的样子（实测 count 不变）。
    # 走连接级别的 backup 由 SQLite 自己处理 WAL 与页一致性，
    # 同时也避免手工删 -wal/-shm 与并发连接抢文件。
    src_conn = sqlite3.connect(src_path)
    try:
        # 不能在 db.tx() 里做：backup API 自己管事务，外层 BEGIN IMMEDIATE
        # 会导致 "cannot start a transaction within a transaction"。
        src_conn.backup(db.connect())
    finally:
        src_conn.close()
    try:
        audit.record(db.connect(), "backup.restore", actor=actor or "system",
                     target_type="system", target_id="restore",
                     detail={"source": os.path.basename(src_path),
                             "safety_backup": safety.get("path") or ""})
    except Exception:
        pass
    return {"ok": True, "restored_from": os.path.basename(src_path),
            "safety_backup": safety.get("path") or "",
            "safety_ok": safety.get("ok")}


def auto_backup_if_stale(hours=24, keep=DEFAULT_KEEP):
    """启动时调用：超过 N 小时没有备份就补一份。

    自动备份的意义是"不需要某人记得"。没有它，备份功能迟早会因为
    "今天太忙"而停摆，而那正是最需要它的时候。
    """
    files = list_files()
    if files:
        try:
            newest = os.path.join(backup_dir(), files[0]["file"])
            age_h = (time.time() - os.path.getmtime(newest)) / 3600.0
            if age_h < float(hours):
                return {"ok": True, "skipped": True,
                        "reason": "最近一次备份在 %.1f 小时前" % age_h}
        except OSError:
            pass
    out = create(keep=keep, actor="system", note="启动时自动备份")
    out["skipped"] = False
    return out
