"""通用工具层：时间、密码散列、业务异常与输入校验。

这一层被所有模块引用，自己不依赖任何业务模块，也不碰数据库和 HTTP。
"""
import hashlib
import hmac
import secrets
import time
from pathlib import Path

# 项目根目录（本文件所在目录）。schema.sql、static/ 都相对它定位，
# 这样从任何工作目录启动都能找到资源。
ROOT = Path(__file__).resolve().parent


def now():
    """返回当前时间的「秒级整数时间戳」。

    为什么用整数：存库、比较 72 小时交付窗口、2 小时撤回窗口时，
    整数直接加减最直观，也不受时区与格式影响。
    """
    return int(time.time())


def password_hash(value, salt=None):
    """把明文密码变成「盐:哈希」字符串，数据库只保存这个结果。

    算法: PBKDF2-HMAC-SHA256，迭代 160000 次，盐为 16 字节随机值。

    参数:
        value 明文密码；
        salt  已有盐（校验时传入），不传则自动生成。
    返回:
        形如 "3f2a…:5d8e…" 的字符串，冒号前是盐、冒号后是哈希。
    """
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.pbkdf2_hmac('sha256', value.encode(), salt.encode(), 160000).hex()


def valid_password(raw, stored):
    """校验明文密码与数据库里的记录是否一致。

    用 hmac.compare_digest 做恒定时间比较，避免攻击者根据响应时间猜密码。

    参数:
        raw    用户提交的明文密码；
        stored 数据库里的「盐:哈希」字符串。
    返回:
        True / False。
    """
    return hmac.compare_digest(password_hash(raw, stored.split(':')[0]), stored)


class BusinessError(Exception):
    """业务规则不满足时抛出的异常，例如「商品已被占用」「已有撤回申请」。

    接口层捕获后统一返回 400，并把 message 原样交给前端弹提示。
    它表示“操作不合法”，不是程序缺陷，正常使用就能避免。
    """


def need(condition, message):
    """轻量断言：条件为假时立即抛出 BusinessError(message)。

    替代写法:
        need(0 < price, '价格必须大于0')
        # 等价于
        if not 0 < price:
            raise BusinessError('价格必须大于0')

    全项目用它做参数与业务校验，错误信息会直接显示给用户。
    """
    if not condition:
        raise BusinessError(message)


def text(data, key, lo=1, hi=1000):
    """读取并规整一个文本字段。

    步骤: 取字段 → 校验字符串类型 → 去掉首尾空白 → 校验长度。

    参数:
        data  前端传来的 JSON 对象；
        key   字段名，同时用于拼错误提示；
        lo/hi 允许的长度范围（按字符计），默认 1—1000。
    返回:
        处理后的字符串。
    异常:
        长度不符时抛 BusinessError，例如「title长度应为2—50字」。
    """
    value = data.get(key)
    need(isinstance(value, str), f'{key}必须是文本')
    value = value.strip()
    need(lo <= len(value) <= hi, f'{key}长度应为{lo}—{hi}字')
    return value


def boolean(data, key):
    value = data.get(key)
    need(isinstance(value, bool), f'{key}必须是布尔值')
    return value
