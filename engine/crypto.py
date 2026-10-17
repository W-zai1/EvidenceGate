"""
静态加密（PII at-rest encryption）—— 纯标准库。

为什么需要
----------
简历里是真实姓名、手机号、邮箱、身份证号、住址、前雇主、薪资期望。
把这些以明文写进 `candidates.json` / SQLite，等于把一份「未加密的个人信息库」
留在员工电脑上——磁盘被拷走、备份上传网盘、离职交接忘了删，都是实际的泄露路径。
《个人信息保护法》要求处理个人信息采取加密等安全措施。

为什么不引第三方库
------------------
项目自我约束是"引擎只用标准库"。`cryptography` 能带来 AES-GCM，
但它会破坏"双击 start.bat 就能跑"这个前提（用户得先 pip install）。

**取舍与边界（务必如实告知）**
- 这里实现的是 HMAC-SHA256 构造的流密码 + HMAC-SHA256 认证（encrypt-then-MAC），
  不是 AES-GCM。密码学上它是稳妥的（HMAC 作为 PRF 是标准做法），
  但**没有经过第三方审计**，且不提供 AAD。
- 它防的是：磁盘/H备份/误提交泄露时明文可见。它**防不了**：拿到
  `data/.secret_key` 的攻击者、以及正在运行的本进程。
- 如果想换成 AES-256-GCM：只需替换 `_encrypt_raw` / `_decrypt_raw` 两个函数，
  因为密文格式里已经带了 alg 标记位，可以做平滑迁移。

密钥管理
--------
密钥首次运行时生成，存在 `data/.secret_key`（权限收紧、不进 git）。
支持用环境变量 `TALENT_PII_KEY`（base64，32 字节）覆盖，便于部署时交给密钥管理系统。
**密钥丢了，历史 PII 就解不开了**——这是加密的必然代价，文档里必须写清楚。
"""
import base64
import hashlib
import hmac
import os
import secrets

# 密文格式: b"TS1" + alg(1B) + nonce(16B) + tag(32B) + ciphertext
_MAGIC = b"TS1"
_ALG_HMACCTR = 1
_NONCE_LEN = 16
_TAG_LEN = 32
_PREFIX_LEN = len(_MAGIC) + 1 + _NONCE_LEN + _TAG_LEN

KEY_ENV = "TALENT_PII_KEY"
_KEY_FILE_NAME = ".secret_key"

_cached_key = None


class PIICryptoError(RuntimeError):
    """加解密失败（密钥缺失 / 密文被篡改 / 格式不符）。"""


# ---------------------------------------------------------------------------
# 密钥
# ---------------------------------------------------------------------------
def _data_dir():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data")


def _key_path():
    return os.path.join(_data_dir(), _KEY_FILE_NAME)


def _derive_subkey(master: bytes, label: bytes) -> bytes:
    """HKDF-Expand 的简化版：用 HMAC 从主密钥派生子密钥。"""
    return hmac.new(master, b"talent-screen/v1/" + label, hashlib.sha256).digest()


def load_key(force_new=False) -> bytes:
    """读取（或首次生成）32 字节主密钥。"""
    global _cached_key
    if _cached_key is not None and not force_new:
        return _cached_key

    env = os.getenv(KEY_ENV)
    if env:
        try:
            raw = base64.b64decode(env.strip(), validate=True)
        except Exception:
            raise PIICryptoError("环境变量 %s 不是合法的 base64" % KEY_ENV)
        if len(raw) != 32:
            raise PIICryptoError("环境变量 %s 必须是 32 字节（base64 编码）" % KEY_ENV)
        _cached_key = hashlib.sha256(raw + b"|talent").digest()
        return _cached_key

    path = _key_path()
    if not force_new and os.path.isfile(path):
        with open(path, "rb") as f:
            raw = f.read().strip()
        try:
            key = base64.b64decode(raw, validate=True)
        except Exception:
            raise PIICryptoError("密钥文件损坏：%s" % path)
        if len(key) != 32:
            raise PIICryptoError("密钥文件长度不对：%s" % path)
        _cached_key = key
        return _cached_key

    os.makedirs(_data_dir(), exist_ok=True)
    key = secrets.token_bytes(32)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(base64.b64encode(key))
    os.replace(tmp, path)
    _restrict_permissions(path)
    _cached_key = key
    return key


def _restrict_permissions(path):
    """尽量把文件权限收到"仅本人可读写"。

    Windows 上 os.chmod 只影响只读位，这里的调用是给跨平台留的接口；
    真正的权限收紧在部署文档里说明（NTFS ACL / 目录访问控制）。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def key_fingerprint(key: bytes = None) -> str:
    """密钥指纹（用于文档/排障，不泄露密钥本身）。

    可传入指定密钥（轮换后尚未写盘时用它算新指纹）。
    """
    return hashlib.sha256(key if key is not None else load_key()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 核心：HMAC-CTR 流加密 + HMAC 认证
# ---------------------------------------------------------------------------
def _keystream_xor(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """HMAC-SHA256 作为 PRF 生成密钥流，与数据异或。"""
    enc_key = _derive_subkey(key, b"enc")
    out = bytearray(len(data))
    bs = 32
    for i in range(0, len(data), bs):
        ctr = (i // bs).to_bytes(8, "big")
        block = hmac.new(enc_key, nonce + ctr, hashlib.sha256).digest()
        chunk = data[i:i + bs]
        for j, byte in enumerate(chunk):
            out[i + j] = byte ^ block[j]
    return bytes(out)


def _encrypt_raw(key: bytes, plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(_NONCE_LEN)
    ct = _keystream_xor(key, nonce, plaintext)
    mac_key = _derive_subkey(key, b"mac")
    tag = hmac.new(mac_key, _MAGIC + bytes([_ALG_HMACCTR]) + nonce + ct,
                   hashlib.sha256).digest()
    return _MAGIC + bytes([_ALG_HMACCTR]) + nonce + tag + ct


def _decrypt_raw(key: bytes, blob: bytes) -> bytes:
    if len(blob) < _PREFIX_LEN or blob[:len(_MAGIC)] != _MAGIC:
        raise PIICryptoError("不是本系统的密文格式")
    alg = blob[len(_MAGIC)]
    if alg != _ALG_HMACCTR:
        raise PIICryptoError("不支持的加密算法标记：%s" % alg)
    p = len(_MAGIC) + 1
    nonce = blob[p:p + _NONCE_LEN]
    p += _NONCE_LEN
    tag = blob[p:p + _TAG_LEN]
    p += _TAG_LEN
    ct = blob[p:]

    mac_key = _derive_subkey(key, b"mac")
    expect = hmac.new(mac_key, _MAGIC + bytes([_ALG_HMACCTR]) + nonce + ct,
                      hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expect):
        # 认证失败就是认证失败：绝不返回残缺明文（那等于篡改后照样被信任）
        raise PIICryptoError("密文认证失败（可能被篡改，或密钥已更换）")
    return _keystream_xor(key, nonce, ct)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def is_encrypted(value) -> bool:
    if not isinstance(value, str):
        return False
    if not value.startswith("enc:v1:"):
        return False
    return True


def encrypt_text(plaintext, key=None) -> str:
    """把明文加密成 `enc:v1:<base64>`；空值原样返回。"""
    if plaintext is None:
        return ""
    s = str(plaintext)
    if not s:
        return ""
    if is_encrypted(s):          # 已加密，避免二次加密（会变成乱码）
        return s
    k = key or load_key()
    blob = _encrypt_raw(k, s.encode("utf-8"))
    return "enc:v1:" + base64.b64encode(blob).decode("ascii")


def decrypt_text(value, key=None) -> str:
    """解密 `enc:v1:...`；明文（未加密的历史数据）原样返回。"""
    if value is None:
        return ""
    s = str(value)
    if not is_encrypted(s):
        return s
    try:
        blob = base64.b64decode(s[len("enc:v1:"):], validate=True)
    except Exception:
        raise PIICryptoError("密文不是合法 base64")
    k = key or load_key()
    return _decrypt_raw(k, blob).decode("utf-8", "replace")


def install_new_key(new_key=None):
    """生成（并落盘）新的主密钥，返回新密钥。

    ⚠ **内部函数，不要在业务流程里直接调用。**
    它只换钥匙、不重加密数据：调用后所有既有密文立刻解不开。
    正确的换钥入口是 `privacy.rotate_key()`——那里在同一个事务里
    先解密、换钥匙、再加密，失败可回滚。

    函数名带 _ 前缀是刻意的：这个 API 一旦被误用就是不可逆的数据丢失。
    """
    global _cached_key
    if os.getenv(KEY_ENV):
        raise PIICryptoError(
            "密钥由环境变量 %s 托管，无法在程序内轮换。"
            "请更换环境变量值后重新用旧密钥解密、再用新密钥加密（见 README 密钥轮换）。"
            % KEY_ENV
        )
    key = new_key or secrets.token_bytes(32)
    if len(key) != 32:
        raise PIICryptoError("新密钥必须是 32 字节")
    path = _key_path()
    os.makedirs(_data_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(base64.b64encode(key))
    os.replace(tmp, path)
    _restrict_permissions(path)
    _cached_key = key
    return key


def set_cached_key(key):
    """仅供测试/迁移：直接设置内存中的密钥（不写盘）。"""
    global _cached_key
    _cached_key = key
    return _cached_key
