"""Windows DPAPI 密钥保管：Web 工作台保存的 API Key 等敏感值加密落盘。

设计（2026-09-09 方案A升级）：
- 用 Windows Data Protection API（CryptProtectData/CryptUnprotectData）加密，
  密钥由操作系统托管并绑定当前 Windows 用户——服务启动免输密码，密文离开本机
  （换电脑/其他用户）即无法解密，需在 Web 端重填一次；
- 纯 ctypes 实现，零第三方依赖；
- 固定 entropy（应用盐）防止同机其他程序随意解出密文；
- 存储 state/secrets.json（已 gitignore）：{"llm_api_key": {"v": "<base64(dpapi密文)>"}}
"""
import base64
import json
import os
import sys

from . import config as cfgmod

# 应用熵：绑定 boss-apply 用途，其他程序用默认参数也解不开
_ENTROPY = b"boss-apply-secrets-v1"
SECRETS_PATH = os.path.join(cfgmod.STATE_DIR, "secrets.json")

_IS_WIN = sys.platform == "win32"


def _dpapi_call(data, protect):
    """ctypes 调 CryptProtectData/CryptUnprotectData。非 Windows（macOS/Linux）下基于应用盐混淆回退。"""
    if not _IS_WIN:
        key = _ENTROPY
        return bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])
    import ctypes
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    ent = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_ent = DATA_BLOB(len(_ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), None, ctypes.byref(blob_ent),
                                      None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, ctypes.byref(blob_ent),
                                        None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise RuntimeError("DPAPI call failed (err=%d)" % kernel32.GetLastError())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(plain):
    """明文 -> base64(DPAPI密文)。"""
    return base64.b64encode(_dpapi_call(plain.encode("utf-8"), True)).decode("ascii")


def unprotect(b64):
    """base64(DPAPI密文) -> 明文。"""
    return _dpapi_call(base64.b64decode(b64), False).decode("utf-8")


def _load_all():
    if not os.path.exists(SECRETS_PATH):
        return {}
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_all(data):
    os.makedirs(cfgmod.STATE_DIR, exist_ok=True)
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def set_secret(name, value):
    """加密保存（value 为空则删除该条）。"""
    data = _load_all()
    if value:
        data[name] = {"v": protect(value)}
    else:
        data.pop(name, None)
    _save_all(data)


def get_secret(name):
    """解密读取；不存在/解密失败返回 None。"""
    entry = _load_all().get(name)
    if not entry or not entry.get("v"):
        return None
    try:
        return unprotect(entry["v"])
    except Exception:
        return None


def has_secret(name):
    return bool(_load_all().get(name, {}).get("v"))


def masked(value):
    """脱敏显示：ak_3B5j8l02... -> ak_3B5...r33（仅暴露首尾各3字符）。"""
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:5] + "..." + value[-3:]
