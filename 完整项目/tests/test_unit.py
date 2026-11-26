"""单元测试：直接调用函数，逐个验证关键函数自己的规则。

与 tests/test_app.py 的分工:
    集成测试  起真实 HTTP 服务，检查“请求 → 路由 → 业务 → 数据库”的整条链路；
    单元测试  直接调用函数，只检查这个函数本身的行为，失败时能定位到具体函数。

文件地图（建议按这个顺序读）:
    1. setUpModule / tearDownModule —— 整个模块只建一次演示库模板，各用例复制一份；
    2. UnitCase / DbCase          —— 公共断言与小工具（raises / value / call /
                                     rejected / last_notice）以及造数助手
                                     （meeting / new_order / accepted / completed / publish）；
    3. TestXxx 测试类             —— 按被测模块排列，顺序：
                                     密码散列 → 文本校验 → 布尔/断言/时间
                                     → 商品字段校验 → db 层 → 业务公共前置
                                     → act 分发 → 商品动作 → 交易动作
                                     → 撤回/争议 → 评价 → 收藏/咨询/举报 → 后台处置；
    4. 每条 test_ 上方的一行注释   —— “# 验证 函数名：这条用例在验什么”，
                                     用 # 而不是 docstring，是为了不改变
                                     unittest -v 的输出格式。

做法:
    * 纯函数（密码散列、文本校验、金额换算）不碰数据库，直接传参断言；
    * 需要数据库的函数用临时库：模块启动时建一次演示库，每个测试复制一份再改，
      跑完即删，互不影响，也不碰 data/campus.db；
    * 业务函数直接拿数据库里的用户行调用，不经过 act() 分发，也不发 HTTP 请求；
    * 与时间窗口有关的用例直接改库里的时间戳，不 sleep，结果稳定。

运行:
    python -m unittest discover -s tests -v                       # 集成 + 单元一起跑
    python -m unittest discover -s tests -p "test_unit.py" -v     # 只跑单元测试
"""
import base64
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services
from core import BusinessError, boolean, need, now, password_hash, text, valid_password
from db import connect, deliver_notices, init_db, notify, public_user, rows, stats
from services import ACTIONS, EarlyReturn, _product_fields, act

# 演示数据里的固定编号：admin=1、卖家（20260001）=2、买家（20260002）=3；
# 商品 1—6 在初始化时全部属于卖家 2、状态为“在售”。
ADMIN, SELLER, BUYER = 1, 2, 3

_TEMP = None
_TEMPLATE = None


def setUpModule():
    """整个模块只建一次演示库，各测试复制文件使用，省掉反复跑 PBKDF2 的时间。"""
    global _TEMP, _TEMPLATE
    _TEMP = tempfile.TemporaryDirectory()
    _TEMPLATE = Path(_TEMP.name) / 'seed.db'
    init_db(str(_TEMPLATE))


def tearDownModule():
    _TEMP.cleanup()


class UnitCase(unittest.TestCase):
    """所有单元测试的公共断言工具（不依赖数据库）。"""

    def raises(self, func, *args, message=''):
        """断言调用抛出 BusinessError；message 非空时再核对提示文字。"""
        with self.assertRaises(BusinessError) as ctx:
            func(*args)
        if message:
            self.assertIn(message, str(ctx.exception))
        return str(ctx.exception)


class DbCase(UnitCase):
    """需要数据库的用例的基类：每个测试都拿到一份全新的演示库副本。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'case.db'
        shutil.copyfile(_TEMPLATE, self.path)
        self.db = connect(str(self.path))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        self.tmp.cleanup()

    # ---------- 通用小工具 ----------

    def user(self, uid):
        """取数据库里的用户行；业务函数要用其中的 id、role、banned_until。"""
        return self.db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    def value(self, sql, args=()):
        """只取第一行第一列，断言单个字段时最省事。"""
        return self.db.execute(sql, args).fetchone()[0]

    def last_notice(self, uid):
        """取某用户最近一条待投递通知；同一用户可能已有更早的通知。"""
        return self.value('SELECT body FROM notice_queue WHERE user_id=? ORDER BY id DESC LIMIT 1',
                          (uid,))

    def call(self, action, uid, payload):
        """直接调用业务函数并提交，等价于一次成功的接口请求。"""
        result = ACTIONS[action](self.db, self.user(uid), payload)
        self.db.commit()
        return result

    def rejected(self, action, uid, payload, message=''):
        """调用一个应被业务规则拒绝的动作；无论成败都回滚，不留半成品。"""
        try:
            return self.raises(ACTIONS[action], self.db, self.user(uid), payload, message=message)
        finally:
            self.db.rollback()

    # ---------- 造数 ----------

    def meeting(self, offset=3600):
        """生成合法的交付时间文本（默认 1 小时后，格式 %Y-%m-%dT%H:%M）。"""
        return time.strftime('%Y-%m-%dT%H:%M', time.localtime(now() + offset))

    def add_user(self, student='20260003', name='王同学'):
        """直接插一个普通用户，用于“第二买家”“第三人”这类场景。"""
        self.db.execute("INSERT INTO users(student,name,password,role) VALUES(?,?,?,'student')",
                        (student, name, password_hash('Test123!')))
        self.db.commit()
        return self.value('SELECT max(id) FROM users')

    def publish(self, uid=SELLER, **kw):
        """发布一件商品，返回商品 id（默认停在“待审核”）。"""
        data = {'title': '无线键盘', 'description': '键位正常无明显磨损，附接收器',
                'category': '数码电器', 'condition': '九成新', 'price': 89}
        data.update(kw)
        self.call('publish', uid, data)
        return self.value('SELECT max(id) FROM products')

    def new_order(self, pid=1):
        """买家下单，返回订单 id（状态：待接单）。"""
        self.call('order', BUYER, {'id': pid})
        return self.value('SELECT max(id) FROM orders')

    def accepted(self, pid=1):
        """下单并让卖家接单，返回订单 id（状态：待交付）。"""
        oid = self.new_order(pid)
        self.call('accept', SELLER, {'id': oid, 'place': '图书馆门口', 'meeting': self.meeting()})
        return oid

    def completed(self, pid=1):
        """下单、接单、双方确认，返回订单 id（状态：已完成）。"""
        oid = self.accepted(pid)
        self.call('confirm', BUYER, {'id': oid})
        self.call('confirm', SELLER, {'id': oid})
        return oid


# ---------- 一、core.py 的纯函数 ----------

class TestPasswordHash(unittest.TestCase):
    """core.password_hash / valid_password：库里只存“盐:散列”，校验用恒定时间比较。"""

    # 验证 password_hash：返回「盐:散列」，盐 32 位、散列 64 位十六进制，明文不落库。
    def test_hash_is_salt_and_digest_without_plain_text(self):
        stored = password_hash('Seller123!')
        salt, digest = stored.split(':')
        self.assertEqual(len(salt), 32)          # 16 字节随机盐的十六进制
        self.assertEqual(len(digest), 64)        # SHA-256 的十六进制
        self.assertNotIn('Seller123!', stored)

    # 验证 password_hash：同一密码两次散列不同（每次换随机盐，防彩虹表）。
    def test_same_password_gets_a_new_salt_each_time(self):
        self.assertNotEqual(password_hash('abc12345'), password_hash('abc12345'))

    # 验证 valid_password：只有完全一致才通过；大小写不同、少一位、空串都返回 False。
    def test_verify_accepts_only_the_right_password(self):
        stored = password_hash('Buyer123!')
        self.assertTrue(valid_password('Buyer123!', stored))
        self.assertFalse(valid_password('buyer123!', stored))
        self.assertFalse(valid_password('Buyer123', stored))
        self.assertFalse(valid_password('', stored))

    # 验证 password_hash(salt=…)：固定盐时结果可复现，说明散列是确定性的。
    def test_verify_with_fixed_salt_is_reproducible(self):
        first = password_hash('pw123456', salt='a1b2c3')
        self.assertEqual(first, password_hash('pw123456', salt='a1b2c3'))
        self.assertEqual(first.split(':')[0], 'a1b2c3')

    # 验证 valid_password：中文密码能往返校验（按 UTF-8 编码）。
    def test_unicode_password_round_trip(self):
        stored = password_hash('中文密码abc1')
        self.assertTrue(valid_password('中文密码abc1', stored))
        self.assertFalse(valid_password('中文密码abc2', stored))


class TestTextRules(unittest.TestCase):
    """core.text：先查类型，再去首尾空白，最后按字符数查长度。"""

    # 验证 text：首尾空白被去掉后返回（'  高等数学  ' → '高等数学'）。
    def test_returns_stripped_text(self):
        self.assertEqual(text({'title': '  高等数学  '}, 'title', 2, 50), '高等数学')

    # 验证 text：None、数字、列表、字典、布尔都不是文本，一律抛 BusinessError。
    def test_rejects_missing_or_non_text(self):
        for value in (None, 1234, [], {}, True):
            with self.assertRaises(BusinessError):
                text({'title': value}, 'title')

    # 验证 text：长度按去空白后的字符数算，'  短  ' 只剩 1 字，按 2—50 字规则被拒。
    def test_length_is_measured_after_strip(self):
        with self.assertRaises(BusinessError) as ctx:
            text({'title': '  短  '}, 'title', 2, 50)
        self.assertIn('title长度应为2—50字', str(ctx.exception))

    # 验证 text：长度上下界本身合法（2 字、50 字通过；1 字、51 字被拒）。
    def test_bounds_are_inclusive(self):
        self.assertEqual(text({'t': 'ab'}, 't', 2, 50), 'ab')
        self.assertEqual(len(text({'t': 'a' * 50}, 't', 2, 50)), 50)
        with self.assertRaises(BusinessError):
            text({'t': 'a'}, 't', 2, 50)
        with self.assertRaises(BusinessError):
            text({'t': 'a' * 51}, 't', 2, 50)


class TestBooleanAndNeed(unittest.TestCase):
    """core.boolean 只接受 JSON 布尔值；core.need 是业务校验的统一出口。"""

    # 验证 boolean：只接受 JSON true/false；字符串 "true"、1、0、None 都被拒。
    def test_boolean_accepts_only_json_booleans(self):
        self.assertIs(boolean({'desired': True}, 'desired'), True)
        self.assertIs(boolean({'desired': False}, 'desired'), False)
        for value in ('true', 'false', 1, 0, None):
            with self.assertRaises(BusinessError):
                boolean({'desired': value}, 'desired')

    # 验证 need：条件为真返回 None，为假抛 BusinessError 且提示原样保留。
    def test_need_passes_and_raises_business_error(self):
        self.assertIsNone(need(True, '不该触发'))
        with self.assertRaises(BusinessError) as ctx:
            need(False, '库存不足')
        self.assertEqual(str(ctx.exception), '库存不足')

    # 验证 now：返回秒级整数，与 time.time() 相差不超过 3 秒。
    def test_now_returns_integer_seconds(self):
        stamp = now()
        self.assertIsInstance(stamp, int)
        self.assertLess(abs(stamp - int(time.time())), 3)


class TestProductFields(UnitCase):
    """services._product_fields：发布与编辑共用的字段校验。

    重点覆盖两个容易出错的点：金额“元 → 分”的换算，以及图片的
    data URL 前缀 + Base64 解码 + 文件头魔数三重校验。
    """

    def payload(self, price=10, **kw):
        data = {'title': '无线键盘', 'description': '键位正常无明显磨损，附接收器',
                'category': '数码电器', 'condition': '九成新', 'price': price}
        data.update(kw)
        return data

    # 验证 _product_fields：金额由「元」换成「分」，'12.50'→1250、35→3500、'0.01'→1、1000000→100000000。
    def test_price_is_converted_to_cents(self):
        cases = [('12.50', 1250), (35, 3500), ('0.01', 1), (1000000, 100000000)]
        for raw, expected in cases:
            self.assertEqual(_product_fields(self.payload(price=raw))[2], expected)

    # 验证 _product_fields：Infinity/NaN/非数字/None/列表/字典统一报「价格必须为有限数字」，不再溢出成 500。
    def test_price_must_be_finite(self):
        for raw in ('Infinity', '-Infinity', 'NaN', 'abc', None, [], {}):
            self.assertEqual(self.raises(_product_fields, self.payload(price=raw),
                                         message='价格必须为有限数字'), '价格必须为有限数字')

    # 验证 _product_fields：0、负数、超过 100 万元（含 1e12）都超出允许范围。
    def test_price_must_be_positive_and_within_limit(self):
        for raw in (0, -1, '-0.01', 1000000.01, 1e12):
            self.raises(_product_fields, self.payload(price=raw), message='价格必须大于0且不超过100万元')

    # 验证 _product_fields：标题 2—50 字、描述 10—1000 字，越界时提示带字段名。
    def test_title_and_description_bounds(self):
        self.raises(_product_fields, self.payload(title='键'), message='title长度应为2—50字')
        self.raises(_product_fields, self.payload(description='太短了'), message='description长度应为10—1000字')

    # 验证 _product_fields：不传图片时返回空串（图片是可选项）。
    def test_empty_image_is_allowed(self):
        self.assertEqual(_product_fields(self.payload())[3], '')

    # 验证 _product_fields：合法 PNG / JPEG 的 data URL 原样返回（Base64 与文件头都正确）。
    def test_png_and_jpeg_images_are_accepted(self):
        png = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * 16).decode()
        jpeg = 'data:image/jpeg;base64,' + base64.b64encode(b'\xff\xd8\xff\xe0' + b'x' * 16).decode()
        self.assertEqual(_product_fields(self.payload(image=png))[3], png)
        self.assertEqual(_product_fields(self.payload(image=jpeg))[3], jpeg)

    # 验证 _product_fields：GIF 前缀、普通 http 链接都不是允许的 data URL，一律拒绝。
    def test_image_prefix_must_be_png_or_jpeg(self):
        gif = 'data:image/gif;base64,' + base64.b64encode(b'GIF89a').decode()
        self.raises(_product_fields, self.payload(image=gif), message='仅支持PNG或JPEG图片')
        self.raises(_product_fields, self.payload(image='http://example.com/a.png'),
                    message='仅支持PNG或JPEG图片')

    # 验证 _product_fields：前缀对但 Base64 含非法字符（@@@@）时报「图片格式无效」。
    def test_image_base64_must_be_valid(self):
        self.raises(_product_fields, self.payload(image='data:image/png;base64,@@@@'),
                    message='图片格式无效')

    # 验证 _product_fields：Base64 能解但内容不是 PNG/JPEG 文件头（b'hello world'）被拒。
    def test_image_content_must_match_magic_bytes(self):
        fake = 'data:image/png;base64,' + base64.b64encode(b'hello world').decode()
        self.raises(_product_fields, self.payload(image=fake), message='图片内容无效或超过5MB')

    # 验证 _product_fields：真实 5MB+ 的图片载荷超过上限被拒（按解码后的字节数判断）。
    def test_image_larger_than_five_megabytes_is_rejected(self):
        big = 'data:image/png;base64,' + base64.b64encode(
            b'\x89PNG\r\n\x1a\n' + b'x' * (5 * 1024 * 1024)).decode()
        self.raises(_product_fields, self.payload(image=big), message='图片内容无效或超过5MB')


# ---------- 二、db.py ----------

class TestInitDb(unittest.TestCase):
    """db.init_db：建目录、建表、写演示数据，且重复执行不覆盖已有数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'nested' / 'campus.db'   # 故意多一层不存在的目录
        self.addCleanup(self.tmp.cleanup)

    # 验证 init_db：自动建多级目录与表，写入 3 个演示账号和 6 件在售商品；二次启动不重复插入。
    def test_creates_schema_and_demo_data_once(self):
        init_db(str(self.path))
        self.assertTrue(self.path.exists())
        with connect(str(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM products').fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT count(*) FROM products WHERE status='在售'").fetchone()[0], 6)
        init_db(str(self.path))                      # 模拟第二次启动
        with connect(str(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM products').fetchone()[0], 6)

    # 验证 init_db：第二次启动保留用户改过的数据，不被演示数据覆盖。
    def test_keeps_data_created_after_the_first_start(self):
        init_db(str(self.path))
        with connect(str(self.path)) as db:
            db.execute("UPDATE products SET title='用户改过的标题' WHERE id=1")
        init_db(str(self.path))
        with connect(str(self.path)) as db:
            self.assertEqual(db.execute('SELECT title FROM products WHERE id=1').fetchone()[0], '用户改过的标题')


class TestStatisticsAndRows(DbCase):
    """db.stats 的统计口径、db.rows 的返回形态、db.public_user 的字段裁剪。"""

    # 验证 stats：只统计「已完成」订单；区间外为 0，未完成不计入完成数与成交额。
    def test_stats_counts_only_completed_orders_in_range(self):
        base = stats(self.db)
        self.assertEqual((base['completed'], base['amount'], base['products'], base['reports']), (0, 0, 6, 0))
        oid = self.completed()
        price = self.value('SELECT price FROM orders WHERE id=?', (oid,))
        self.assertEqual(stats(self.db)['completed'], 1)
        self.assertEqual(stats(self.db)['amount'], price)
        self.assertEqual(stats(self.db, 0, now() - 3600)['completed'], 0)

    # 验证 stats：成交额是区间内已完成订单价格之和（3500 + 6800 分）。
    def test_stats_amount_sums_completed_orders(self):
        self.completed(1)                            # 商品 1：3500 分
        self.completed(2)                            # 商品 2：6800 分
        self.assertEqual(stats(self.db)['amount'], 3500 + 6800)

    # 验证 stats：待处理举报数不受时间区间影响，处理完变 0。
    def test_pending_reports_ignore_the_date_range(self):
        self.call('report', BUYER, {'id': 1, 'body': '物品内容需要核查'})
        self.assertEqual(stats(self.db, 0, now() - 3600)['reports'], 1)
        rid = self.value('SELECT max(id) FROM reports')
        self.call('handle_report', ADMIN, {'id': rid, 'reason': '内容已经核对并通知发布者'})
        self.assertEqual(stats(self.db)['reports'], 0)

    # 验证 rows：返回普通 dict 列表（可直接 JSON 序列化），列名齐全。
    def test_rows_returns_plain_dicts(self):
        data = rows(self.db, 'SELECT id,title FROM products ORDER BY id')
        self.assertEqual(len(data), 6)
        self.assertIsInstance(data[0], dict)
        self.assertEqual(set(data[0]), {'id', 'title'})

    # 验证 public_user：只保留 5 个对外字段，password 不出现。
    def test_public_user_hides_password(self):
        exposed = public_user(self.user(BUYER))
        self.assertEqual(set(exposed), {'id', 'student', 'name', 'role', 'banned_until'})
        self.assertNotIn('password', exposed)


class TestNotificationDelivery(DbCase):
    """db.notify / db.deliver_notices：通知先入队，打开收件箱时才投递，失败可重试。"""

    # 验证 notify/deliver_notices：先入队，打开收件箱时才投递并清空；只投给本人，重复投递不重复。
    def test_queue_is_delivered_once_and_only_to_its_owner(self):
        notify(self.db, BUYER, '订单 #1 已更新：confirm')
        self.db.commit()
        self.assertEqual(self.value('SELECT count(*) FROM notice_queue'), 1)
        deliver_notices(self.db, BUYER)
        self.assertEqual(self.value('SELECT count(*) FROM notices'), 1)
        self.assertEqual(self.value('SELECT count(*) FROM notice_queue'), 0)
        self.assertEqual(self.value('SELECT count(*) FROM notices WHERE user_id=?', (SELLER,)), 0)
        deliver_notices(self.db, BUYER)          # 再打开一次不应重复投递
        self.assertEqual(self.value('SELECT count(*) FROM notices'), 1)

    # 验证 deliver_notices：用触发器让插入失败，投递回滚、队列保留；解除故障后重投成功。
    def test_failure_keeps_the_queue_and_next_delivery_succeeds(self):
        notify(self.db, BUYER, '待投递')
        self.db.commit()
        self.db.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON notices "
                        "BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.db.commit()
        deliver_notices(self.db, BUYER)          # 内部捕获并回滚，不向上抛异常
        self.assertEqual(self.value('SELECT count(*) FROM notice_queue'), 1)
        self.assertEqual(self.value('SELECT count(*) FROM notices'), 0)
        self.db.execute('DROP TRIGGER fail_notice')
        self.db.commit()
        deliver_notices(self.db, BUYER)
        self.assertEqual(self.value('SELECT count(*) FROM notices'), 1)
        self.assertEqual(self.value('SELECT count(*) FROM notice_queue'), 0)


# ---------- 三、services.py 的公共前置 ----------

class TestLookupAndRoles(DbCase):
    """services.get / administrator / own_order：取对象与权限判断的公共前置。"""

    # 验证 get：按 id 取对象，也支持自定义键名（如 order_id）。
    def test_get_reads_by_id_and_by_custom_key(self):
        self.assertEqual(services.get(self.db, {'id': 1}, 'products')['title'], '高等数学教材 全套')
        oid = self.accepted()
        self.assertEqual(services.get(self.db, {'order_id': oid}, 'orders', key='order_id')['id'], oid)

    # 验证 get：查不到对象时抛「对象不存在」，不让 None 漏到业务层。
    def test_get_rejects_missing_object(self):
        self.assertEqual(self.raises(services.get, self.db, {'id': 9999}, 'products',
                                     message='对象不存在'), '对象不存在')

    # 验证 administrator：只有 role=admin 通过；卖家、买家都抛「需要管理员权限」。
    def test_administrator_only_allows_admin_role(self):
        self.assertIsNone(services.administrator(self.user(ADMIN)))
        self.raises(services.administrator, self.user(SELLER), message='需要管理员权限')
        self.raises(services.administrator, self.user(BUYER), message='需要管理员权限')

    # 验证 own_order：买卖双方都能取订单；第三方（含管理员）抛「不能操作他人订单」。
    def test_own_order_accepts_participants_and_rejects_outsiders(self):
        oid = self.new_order()
        self.assertEqual(services.own_order(self.db, self.user(BUYER), {'id': oid})['id'], oid)
        self.assertEqual(services.own_order(self.db, self.user(SELLER), {'id': oid})['id'], oid)
        self.raises(services.own_order, self.db, self.user(ADMIN), {'id': oid},
                    message='不能操作他人订单')


class TestActDispatch(DbCase):
    """services.act：封禁拦截、未知动作、EarlyReturn 跳过审计、审计字段裁剪。"""

    # 验证 act：动作表里没有的动作名抛「未知操作」。
    def test_unknown_action_is_rejected(self):
        self.raises(act, self.db, self.user(BUYER), 'no_such_action', {}, message='未知操作')

    # 验证 act：封禁期用户下单被拒，但 read_notices 仍可执行（不打断在途履约）。
    def test_banned_user_blocked_from_new_business_only(self):
        self.db.execute('UPDATE users SET banned_until=? WHERE id=?', (now() + 3600, BUYER))
        self.db.commit()
        self.raises(act, self.db, self.user(BUYER), 'order', {'id': 1}, message='账号处于封禁期')
        self.assertTrue(act(self.db, self.user(BUYER), 'read_notices', {})['ok'])

    # 验证 act：同一买家重复下单走 EarlyReturn，返回「订单已存在」且不写审计（审计仍 1 条）。
    def test_early_return_skips_audit(self):
        self.assertTrue(act(self.db, self.user(BUYER), 'order', {'id': 1})['ok'])
        again = act(self.db, self.user(BUYER), 'order', {'id': 1})
        self.assertEqual(again['message'], '订单已存在')
        self.assertIn('order_id', again)
        self.assertEqual(self.value('SELECT count(*) FROM orders'), 1)
        self.assertEqual(self.value("SELECT count(*) FROM audit WHERE action='order'"), 1)

    # 验证 act：审计剔除 image/password、超长内容截断到 2000 字，target 取 payload 里的 id。
    def test_audit_strips_sensitive_fields_and_truncates_detail(self):
        image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * 32).decode()
        act(self.db, self.user(SELLER), 'publish',
            {'title': '台灯', 'description': '毕业闲置台灯，三档色温均可正常使用',
             'category': '数码电器', 'condition': '九成新', 'price': 68,
             'image': image, 'password': 'Secret123', 'extra': 'x' * 5000})
        detail = self.value("SELECT detail FROM audit WHERE action='publish'")
        self.assertNotIn('image', detail)
        self.assertNotIn('Secret123', detail)
        self.assertNotIn('base64', detail)
        self.assertLessEqual(len(detail), 2000)
        self.assertEqual(self.value("SELECT target FROM audit WHERE action='publish'"), 0)

    # 验证 act：审计同时记录操作者、动作、目标与时间。
    def test_audit_records_actor_action_target_and_time(self):
        act(self.db, self.user(BUYER), 'order', {'id': 1})
        self.db.commit()
        self.assertEqual(self.value("SELECT actor FROM audit WHERE action='order'"), BUYER)
        self.assertEqual(self.value("SELECT target FROM audit WHERE action='order'"), 1)
        self.assertGreater(self.value("SELECT created FROM audit WHERE action='order'"), 0)


# ---------- 四、services.py 商品相关 ----------

class TestProductRules(DbCase):
    """商品动作：发布、编辑、下架、审核、冻结。"""

    # 验证 publish：入库状态为「待审核」，价格按分存（12.50→1250），卖家是本人。
    def test_publish_enters_pending_with_price_in_cents(self):
        pid = self.publish(price='12.50')
        product = self.db.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        self.assertEqual((product['status'], product['price'], product['seller']), ('待审核', 1250, SELLER))

    # 验证 edit：待审核不能改、非本人不能改；被驳回后可改，并回到待审核、清空驳回原因。
    def test_edit_requires_own_unfrozen_shelved_or_rejected_product(self):
        pid = self.publish()
        payload = {'id': pid, 'title': '修订键盘', 'description': '修改之后的一份完整键盘描述',
                   'category': '数码电器', 'condition': '八成新', 'price': 75}
        self.rejected('edit', SELLER, payload, '只能编辑本人未冻结的下架或驳回商品')   # 仍在待审核
        self.rejected('edit', BUYER, payload, '只能编辑本人未冻结的下架或驳回商品')    # 不是本人
        self.call('review_product', ADMIN, {'id': pid, 'approve': False, 'reason': '描述信息不足'})
        self.call('edit', SELLER, payload)
        product = self.db.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        self.assertEqual((product['status'], product['title'], product['reason']),
                         ('待审核', '修订键盘', ''))

    # 验证 edit：没传新图片时沿用旧图片。
    def test_edit_keeps_the_old_image_when_new_one_is_empty(self):
        image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * 16).decode()
        pid = self.publish(image=image)
        self.call('review_product', ADMIN, {'id': pid, 'approve': True, 'reason': '内容符合发布要求'})
        self.call('shelve', SELLER, {'id': pid})
        self.call('edit', SELLER, {'id': pid, 'title': '无线键盘', 'description': '键位正常，附接收器，可在校内试用',
                                   'category': '数码电器', 'condition': '九成新', 'price': 89})
        self.assertEqual(self.value('SELECT image FROM products WHERE id=?', (pid,)), image)

    # 验证 edit：被冻结的商品即使是本人也不能编辑。
    def test_frozen_product_cannot_be_edited(self):
        pid = self.publish()
        self.call('review_product', ADMIN, {'id': pid, 'approve': False, 'reason': '描述信息不足'})
        self.call('freeze', ADMIN, {'id': pid, 'reason': '内容存在违规问题'})
        self.rejected('edit', SELLER, {'id': pid, 'title': '修订键盘', 'description': '修改之后的一份完整键盘描述',
                                       'category': '数码电器', 'condition': '八成新', 'price': 75},
                      '只能编辑本人未冻结的下架或驳回商品')

    # 验证 shelve：只能下架本人在售商品；已被下单（已预订）的不能下架。
    def test_shelve_requires_own_on_sale_product(self):
        self.rejected('shelve', BUYER, {'id': 1}, '只能下架本人在售商品')
        self.new_order()                             # 商品 1 变成“已预订”
        self.rejected('shelve', SELLER, {'id': 1}, '只能下架本人在售商品')
        self.call('shelve', SELLER, {'id': 2})
        self.assertEqual(self.value('SELECT status FROM products WHERE id=2'), '已下架')

    # 验证 review_product：需管理员、待审核、approve 为 JSON 布尔；通过后转在售，重复审核被拒。
    def test_review_product_requires_admin_pending_state_and_boolean(self):
        pid = self.publish()
        self.rejected('review_product', BUYER, {'id': pid, 'approve': True, 'reason': '内容符合要求'},
                      '需要管理员权限')
        self.rejected('review_product', ADMIN, {'id': pid, 'approve': 'true', 'reason': '内容符合要求'},
                      'approve必须是布尔值')
        self.call('review_product', ADMIN, {'id': pid, 'approve': True, 'reason': '内容符合发布要求'})
        self.assertEqual(self.value('SELECT status FROM products WHERE id=?', (pid,)), '在售')
        self.rejected('review_product', ADMIN, {'id': pid, 'approve': True, 'reason': '重复审核'},
                      '商品不是待审核状态')

    # 验证 review_product：审核结果通过通知队列告知卖家。
    def test_review_result_notifies_seller_through_the_queue(self):
        pid = self.publish()
        self.call('review_product', ADMIN, {'id': pid, 'approve': False, 'reason': '图片与描述不一致'})
        self.assertEqual(self.value('SELECT status FROM products WHERE id=?', (pid,)), '已驳回')
        self.assertEqual(self.last_notice(SELLER),
                         '商品处理结果已更新')

    # 验证 freeze/unfreeze：冻结只置标记、不改底层状态；解冻后不会自动复售。
    def test_freeze_and_unfreeze_keep_status(self):
        self.new_order()                             # 商品 1 → 已预订
        self.call('freeze', ADMIN, {'id': 1, 'reason': '内容存在违规问题'})
        product = self.db.execute('SELECT * FROM products WHERE id=1').fetchone()
        self.assertEqual((product['frozen'], product['status']), (1, '已预订'))
        self.rejected('favorite', BUYER, {'id': 1}, '商品当前不可收藏')
        self.call('unfreeze', ADMIN, {'id': 1, 'reason': '整改内容审核通过'})
        self.assertEqual(self.value('SELECT frozen FROM products WHERE id=1'), 0)
        self.assertEqual(self.value('SELECT status FROM products WHERE id=1'), '已预订')   # 不会自动复售


# ---------- 五、services.py 交易相关 ----------

class TestOrderRules(DbCase):
    """下单与接单：独占、幂等、自购拦截、交付时间窗口。"""

    # 验证 order：生成「待接单」订单、商品转「已预订」、卖家收到通知。
    def test_order_occupies_product_and_notifies_seller(self):
        oid = self.new_order()
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual((order['status'], order['buyer'], order['seller']), ('待接单', BUYER, SELLER))
        self.assertEqual(self.value('SELECT status FROM products WHERE id=1'), '已预订')
        self.assertEqual(self.last_notice(SELLER),
                         '有新订单，请接单或拒绝')

    # 验证 order：第二个买手下单被拒（商品已被占用）。
    def test_second_buyer_cannot_occupy_the_same_product(self):
        self.new_order()
        other = self.add_user()
        self.rejected('order', other, {'id': 1}, '商品已被占用或不可交易')

    # 验证 order：自购、冻结商品、封禁期卖家三种情况都拒绝下单。
    def test_own_product_frozen_product_and_banned_seller_are_rejected(self):
        self.rejected('order', SELLER, {'id': 1}, '不能购买自己的商品')
        self.call('freeze', ADMIN, {'id': 2, 'reason': '内容存在违规问题'})
        self.rejected('order', BUYER, {'id': 2}, '商品已被占用或不可交易')
        self.db.execute('UPDATE users SET banned_until=? WHERE id=?', (now() + 3600, SELLER))
        self.db.commit()
        self.rejected('order', BUYER, {'id': 3}, '卖家账号处于封禁期')

    # 验证 order：同一买家重复下单返回原订单（EarlyReturn），库里仍只有一单。
    def test_same_buyer_gets_the_existing_order_instead_of_a_second_one(self):
        oid = self.new_order()
        again = self.call('order', BUYER, {'id': 1})
        self.assertIsInstance(again, EarlyReturn)
        self.assertEqual(again.payload['order_id'], oid)
        self.assertEqual(self.value('SELECT count(*) FROM orders'), 1)

    # 验证 accept：交付时间在过去、超过下单后 72 小时、格式不对都被拒；合法时间才接单成功。
    def test_accept_requires_a_time_inside_the_72_hour_window(self):
        oid = self.new_order()
        for meeting, message in [
            (self.meeting(-3600), '交付时间须在当前时间之后且下单后72小时内'),      # 已经过去
            (self.meeting(73 * 3600), '交付时间须在当前时间之后且下单后72小时内'),  # 超过 72 小时
            ('2026/11/20 18:00', '交付时间格式无效'),                              # 格式不对
        ]:
            self.rejected('accept', SELLER, {'id': oid, 'place': '图书馆门口', 'meeting': meeting}, message)
        self.call('accept', SELLER, {'id': oid, 'place': '图书馆门口', 'meeting': self.meeting()})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual((order['status'], order['place']), ('待交付', '图书馆门口'))
        self.assertEqual(self.last_notice(BUYER),
                         '订单 #' + str(oid) + ' 已更新：accept')

    # 验证 accept：卖家重复接单返回「订单已经接单」；买家接单被拒。
    def test_accept_is_idempotent_and_seller_only(self):
        oid = self.accepted()
        again = self.call('accept', SELLER, {'id': oid, 'place': '', 'meeting': ''})
        self.assertIsInstance(again, EarlyReturn)
        self.assertEqual(again.payload['message'], '订单已经接单')
        self.rejected('accept', BUYER, {'id': oid, 'place': '图书馆门口', 'meeting': self.meeting()},
                      '只有卖家可接待接单订单')

    # 验证 confirm：单方确认后仍「待交付」；双方确认后「已完成」、商品「已售出」、completed 有值。
    def test_single_confirmation_keeps_waiting_and_both_complete(self):
        oid = self.accepted()
        self.call('confirm', BUYER, {'id': oid})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual((order['status'], order['completed']), ('待交付', 0))
        self.assertGreater(order['buyer_confirm'], 0)
        self.call('confirm', SELLER, {'id': oid})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual(order['status'], '已完成')
        self.assertGreater(order['completed'], 0)
        self.assertEqual(self.value('SELECT status FROM products WHERE id=1'), '已售出')

    # 验证 confirm：重复确认不会改写完成时间（幂等）。
    def test_repeated_confirmation_does_not_change_completion_time(self):
        oid = self.completed()
        stamp = self.value('SELECT completed FROM orders WHERE id=?', (oid,))
        self.call('confirm', SELLER, {'id': oid})
        self.call('confirm', BUYER, {'id': oid})
        self.assertEqual(self.value('SELECT completed FROM orders WHERE id=?', (oid,)), stamp)

    # 验证 confirm：「待接单」的订单不能确认交付。
    def test_confirm_requires_waiting_delivery_state(self):
        oid = self.new_order()
        self.rejected('confirm', BUYER, {'id': oid}, '订单不可确认交付')

    # 验证 cancel：订单转「已关闭」、商品回到「在售」；重复取消返回「订单已经关闭」。
    def test_cancel_closes_order_and_releases_product(self):
        oid = self.accepted()
        self.call('cancel', BUYER, {'id': oid})
        self.assertEqual(self.value('SELECT status FROM orders WHERE id=?', (oid,)), '已关闭')
        self.assertEqual(self.value('SELECT status FROM products WHERE id=1'), '在售')
        self.assertEqual(self.call('cancel', BUYER, {'id': oid}).payload['message'], '订单已经关闭')

    # 验证 cancel：已有一方确认禁止普通取消；封禁期用户也不能普通取消。
    def test_cancel_blocked_after_confirmation_and_for_banned_user(self):
        oid = self.accepted()
        self.call('confirm', BUYER, {'id': oid})
        self.rejected('cancel', BUYER, {'id': oid}, '已有确认或争议，不允许普通取消')
        oid2 = self.accepted(2)
        self.db.execute('UPDATE users SET banned_until=? WHERE id=?', (now() + 3600, BUYER))
        self.db.commit()
        self.rejected('cancel', BUYER, {'id': oid2}, '封禁期不允许普通取消，请联系管理员')


class TestWithdrawalAndDispute(DbCase):
    """撤回申请（2 小时窗口、唯一待审）与争议发起。"""

    # 验证 withdraw：确认后 2 小时内可申请；同一订单第二次申请被拒（唯一待审）。
    def test_withdrawal_within_two_hours_and_duplicate_blocked(self):
        oid = self.completed()
        self.call('withdraw', BUYER, {'id': oid, 'body': '申请核查交付情况'})
        self.assertEqual(self.value('SELECT status FROM withdrawals WHERE order_id=?', (oid,)), '待审核')
        self.assertEqual(self.last_notice(SELLER),
                         '订单 #' + str(oid) + ' 已更新：withdraw')
        self.rejected('withdraw', BUYER, {'id': oid, 'body': '重复申请'}, '已有撤回申请正在审核')
        self.rejected('withdraw', SELLER, {'id': oid, 'body': '卖家也申请'}, '已有撤回申请正在审核')

    # 验证 withdraw：把确认时间改成 7201 秒前再申请，超 2 小时被拒。
    def test_withdrawal_after_two_hours_is_rejected(self):
        oid = self.completed()
        self.db.execute('UPDATE orders SET buyer_confirm=? WHERE id=?', (now() - 7201, oid))
        self.db.commit()
        self.rejected('withdraw', BUYER, {'id': oid, 'body': '超时申请'}, '须在本人确认后2小时内申请')

    # 验证 withdraw：自己还没确认就申请撤回，被拒。
    def test_withdrawal_requires_own_confirmation(self):
        oid = self.accepted()
        self.rejected('withdraw', BUYER, {'id': oid, 'body': '还没确认'}, '须在本人确认后2小时内申请')

    # 验证 dispute：只有「待交付」可直接发起（转争议处理中并记说明）；已完成、重复发起都拒绝。
    def test_dispute_only_from_waiting_delivery_state(self):
        oid = self.accepted()
        self.call('dispute', BUYER, {'id': oid, 'body': '商品未实际交接'})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual((order['status'], order['note']), ('争议处理中', '商品未实际交接'))
        self.rejected('dispute', BUYER, {'id': oid, 'body': '重复发起'}, '只有待交付订单可直接发起争议')
        oid2 = self.completed(2)
        self.rejected('dispute', BUYER, {'id': oid2, 'body': '完成订单不能直接争议'},
                      '只有待交付订单可直接发起争议')


class TestReviewRules(DbCase):
    """评价：仅已完成订单、评分 1—5、修改时保留历史。"""

    # 验证 review：首次入库；改评价后旧评分进 review_history，reviews 只留一条新值。
    def test_review_is_saved_and_modified_with_history(self):
        oid = self.completed()
        self.call('review', BUYER, {'id': oid, 'rating': 4, 'body': '商品很好'})
        self.call('review', BUYER, {'id': oid, 'rating': 5, 'body': '追加好评'})
        self.assertEqual(self.value('SELECT rating FROM reviews WHERE order_id=?', (oid,)), 5)
        self.assertEqual(self.value('SELECT rating FROM review_history'), 4)
        self.assertEqual(self.value('SELECT count(*) FROM reviews'), 1)

    # 验证 review：评分 1—5 之外被拒；未完成订单、第三人评价都被拒。
    def test_rating_bounds_and_order_state(self):
        oid = self.completed()
        for rating in (0, 6, -1):
            self.rejected('review', BUYER, {'id': oid, 'rating': rating, 'body': '评分越界'}, '评分范围1至5')
        waiting = self.new_order(2)
        self.rejected('review', BUYER, {'id': waiting, 'rating': 5, 'body': '还没完成'}, '仅已完成订单可评价')
        self.rejected('review', ADMIN, {'id': oid, 'rating': 5, 'body': '第三人评价'}, '不能操作他人订单')

    # 验证 review：评分传 'abc' 抛 ValueError（接口层统一映射 400），库里不产生评价。
    def test_rating_must_be_an_integer(self):
        oid = self.completed()
        with self.assertRaises(ValueError):          # 接口层把 ValueError 也映射成 400
            ACTIONS['review'](self.db, self.user(BUYER), {'id': oid, 'rating': 'abc', 'body': '非法评分'})
        self.db.rollback()
        self.assertEqual(self.value('SELECT count(*) FROM reviews'), 0)


# ---------- 六、services.py 互动与后台 ----------

class TestInteractionRules(DbCase):
    """收藏、咨询、举报各自的边界。"""

    # 验证 favorite：按目标状态写入；连按两次收藏仍 1 条，连按两次取消为 0 条。
    def test_favorite_is_idempotent_per_target_state(self):
        for desired, expected in [(None, 1), (None, 1), (False, 0), (False, 0)]:
            payload = {'id': 1} if desired is None else {'id': 1, 'desired': desired}
            self.call('favorite', BUYER, payload)
            self.assertEqual(self.value('SELECT count(*) FROM favorites'), expected)

    # 验证 message：买家发起咨询、卖家回复都入库，卖家收到「收到新的商品咨询」。
    def test_message_between_buyer_and_seller(self):
        self.call('message', BUYER, {'id': 1, 'receiver': SELLER, 'body': '请问可以校内交接吗？'})
        self.assertEqual(self.value('SELECT count(*) FROM messages'), 1)
        self.assertEqual(self.last_notice(SELLER),
                         '收到新的商品咨询')
        self.call('message', SELLER, {'id': 1, 'receiver': BUYER, 'body': '可以在图书馆交接'})
        self.assertEqual(self.value('SELECT count(*) FROM messages'), 2)

    # 验证 message：不能主动群发、不能发给自己、必须关联卖家、接收人须存在、冻结商品拒绝新咨询。
    def test_message_rules(self):
        self.rejected('message', SELLER, {'id': 1, 'receiver': BUYER, 'body': '卖家主动群发'},
                      '只能回复已有咨询')
        self.rejected('message', SELLER, {'id': 1, 'receiver': 999, 'body': '接收人不存在'},
                      '接收人不存在')
        self.rejected('message', BUYER, {'id': 1, 'receiver': BUYER, 'body': '发给自己'},
                      '不能向自己发消息')
        self.rejected('message', BUYER, {'id': 1, 'receiver': ADMIN, 'body': '咨询无关人员'},
                      '咨询必须关联商品卖家')
        self.call('freeze', ADMIN, {'id': 1, 'reason': '内容存在违规问题'})
        self.rejected('message', BUYER, {'id': 1, 'receiver': SELLER, 'body': '冻结后咨询'},
                      '商品不接受新咨询')

    # 验证 report：同一用户重复举报触发唯一索引 IntegrityError（接口层映射 409）。
    def test_report_is_unique_per_user_and_product(self):
        self.call('report', BUYER, {'id': 1, 'body': '物品内容需要核查'})
        self.assertEqual(self.value('SELECT status FROM reports'), '待处理')
        with self.assertRaises(sqlite3.IntegrityError):
            ACTIONS['report'](self.db, self.user(BUYER), {'id': 1, 'body': '重复举报'})
        self.db.rollback()


class TestAdminDisposition(DbCase):
    """管理员处置：封禁/解封、举报处理、撤回审批、争议裁定。"""

    # 验证 ban：需管理员、至少 7 天、不能封管理员；封禁时间落在 now+7 天，理由进通知。
    def test_ban_requires_admin_reason_and_minimum_days(self):
        self.rejected('ban', SELLER, {'id': BUYER, 'reason': '违规处置', 'days': 7}, '需要管理员权限')
        self.rejected('ban', ADMIN, {'id': BUYER, 'reason': '违规处置', 'days': 6}, '封禁至少7天')
        self.rejected('ban', ADMIN, {'id': ADMIN, 'reason': '违规处置', 'days': 7}, '不能封禁管理员')
        before = now()
        self.call('ban', ADMIN, {'id': BUYER, 'reason': '发布违规内容', 'days': 7})
        until = self.value('SELECT banned_until FROM users WHERE id=?', (BUYER,))
        self.assertGreaterEqual(until, before + 7 * 86400)
        self.assertLessEqual(until, now() + 7 * 86400)
        self.assertEqual(self.last_notice(BUYER),
                         '发布违规内容')

    # 验证 unban：解封把 banned_until 归零。
    def test_unban_clears_the_ban_time(self):
        self.call('ban', ADMIN, {'id': BUYER, 'reason': '发布违规内容', 'days': 7})
        self.call('unban', ADMIN, {'id': BUYER, 'reason': '整改完成提前解封', 'days': 7})
        self.assertEqual(self.value('SELECT banned_until FROM users WHERE id=?', (BUYER,)), 0)

    # 验证 handle_report：写结论与状态并通知举报人；非管理员被拒。
    def test_report_handling_records_result_and_notifies_author(self):
        self.call('report', BUYER, {'id': 1, 'body': '物品内容需要核查'})
        rid = self.value('SELECT max(id) FROM reports')
        self.rejected('handle_report', SELLER, {'id': rid, 'reason': '内容已经核对'}, '需要管理员权限')
        self.call('handle_report', ADMIN, {'id': rid, 'reason': '内容已经核对并通知发布者'})
        report = self.db.execute('SELECT * FROM reports WHERE id=?', (rid,)).fetchone()
        self.assertEqual((report['status'], report['result']), ('已处理', '内容已经核对并通知发布者'))
        self.assertEqual(self.last_notice(BUYER),
                         '举报结果：内容已经核对并通知发布者')

    # 验证 handle_withdrawal（批准）：订单转争议、该方确认清零、商品保持占用、原确认时间进历史表。
    def test_withdrawal_approval_moves_order_to_dispute(self):
        oid = self.completed()
        stamp = self.value('SELECT buyer_confirm FROM orders WHERE id=?', (oid,))
        self.call('withdraw', BUYER, {'id': oid, 'body': '申请核查交付情况'})
        wid = self.value('SELECT max(id) FROM withdrawals')
        self.call('handle_withdrawal', ADMIN, {'id': wid, 'approve': True, 'reason': '证据需要进一步核查'})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual(order['status'], '争议处理中')
        self.assertEqual(order['buyer_confirm'], 0)               # 被撤销的确认清零
        self.assertEqual(order['note'], '撤回获批：证据需要进一步核查')
        self.assertEqual(self.value('SELECT status FROM products WHERE id=1'), '已预订')
        self.assertEqual(self.value('SELECT confirmed FROM confirmation_history'), stamp)  # 原确认时间留证
        self.assertEqual(self.value('SELECT status FROM withdrawals WHERE id=?', (wid,)), '已批准')

    # 验证 handle_withdrawal（驳回）：只改申请单，订单仍「已完成」、确认时间不变；重复处理被拒。
    def test_withdrawal_rejection_keeps_order_untouched(self):
        oid = self.completed()
        stamp = self.value('SELECT buyer_confirm FROM orders WHERE id=?', (oid,))
        self.call('withdraw', BUYER, {'id': oid, 'body': '申请核查交付情况'})
        wid = self.value('SELECT max(id) FROM withdrawals')
        self.rejected('handle_withdrawal', SELLER, {'id': wid, 'approve': False, 'reason': '交付证据充分'},
                      '需要管理员权限')
        self.call('handle_withdrawal', ADMIN, {'id': wid, 'approve': False, 'reason': '交付证据充分，驳回申请'})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual((order['status'], order['buyer_confirm']), ('已完成', stamp))
        self.assertEqual(self.value('SELECT status FROM withdrawals WHERE id=?', (wid,)), '已驳回')
        self.rejected('handle_withdrawal', ADMIN, {'id': wid, 'approve': False, 'reason': '重复处理'},
                      '申请已处理')

    # 验证 resolve：认定完成需「至少一方确认 + ≥10 字说明」；满足后订单已完成、商品已售出。
    def test_dispute_resolution_needs_confirmation_and_evidence(self):
        oid = self.accepted()                                     # 双方都还没确认
        self.call('dispute', SELLER, {'id': oid, 'body': '交付存在争议'})
        self.rejected('resolve', ADMIN, {'id': oid, 'complete': True,
                                         'reason': '核对记录后认定已经交付完成'},
                      '认定完成需至少一方有效确认')
        oid2 = self.accepted(2)                                   # 卖家已确认，但说明不足 10 字
        self.call('confirm', SELLER, {'id': oid2})
        self.call('dispute', SELLER, {'id': oid2, 'body': '交付存在争议'})
        self.rejected('resolve', ADMIN, {'id': oid2, 'complete': True, 'reason': '证据不足'},
                      '认定完成需至少一方有效确认')
        self.call('resolve', ADMIN, {'id': oid2, 'complete': True,
                                     'reason': '核对卖方有效确认以及线下交接记录，认定已经交付'})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid2,)).fetchone()
        self.assertEqual(order['status'], '已完成')
        self.assertGreater(order['completed'], 0)
        self.assertEqual(self.value('SELECT status FROM products WHERE id=2'), '已售出')

    # 验证 resolve（关闭）：订单已关闭、completed 归零、商品回到「在售」。
    def test_dispute_close_releases_the_product(self):
        oid = self.accepted()
        self.call('dispute', BUYER, {'id': oid, 'body': '商品未实际交接'})
        self.call('resolve', ADMIN, {'id': oid, 'complete': False,
                                    'reason': '核查双方记录，确认商品没有完成交接'})
        order = self.db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual((order['status'], order['completed']), ('已关闭', 0))
        self.assertEqual(self.value('SELECT status FROM products WHERE id=1'), '在售')
        self.rejected('resolve', ADMIN, {'id': oid, 'complete': False, 'reason': '重复裁定'},
                      '订单不在争议中')

    # 验证 read_notices：只把自己的通知标为已读，不影响别人的。
    def test_read_notices_marks_only_own_notices_seen(self):
        notify(self.db, BUYER, '订单 #1 已更新')
        notify(self.db, SELLER, '商品处理结果已更新')
        self.db.commit()
        deliver_notices(self.db, BUYER)
        self.assertEqual(self.value('SELECT seen FROM notices WHERE user_id=?', (BUYER,)), 0)
        self.call('read_notices', BUYER, {})
        self.assertEqual(self.value('SELECT seen FROM notices WHERE user_id=?', (BUYER,)), 1)
        deliver_notices(self.db, SELLER)
        self.assertEqual(self.value('SELECT seen FROM notices WHERE user_id=?', (SELLER,)), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
