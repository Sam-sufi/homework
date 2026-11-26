"""HTTP 接口层：请求校验、路由分发与响应。

一次请求的生命周期（由标准库 http.server 驱动）：
    浏览器请求 → HTTPServer 创建 Handler 实例 → do_GET / do_POST
    → user() 认身份 → 路由表找到 api_xxx / 交给 services.act()
    → send() 统一下发 JSON 或文件

分工约定：
    * 本文件只处理 HTTP 细节（读参数、鉴权、状态码、响应头），不写业务规则；
    * 业务规则一律调用 services.act()，数据库访问一律走 db.py；
    * 对外路径保持不变：
        GET  /、/app.js、/style.css                 静态页面
        GET  /api/me、/api/products、/api/product    公开接口
        GET  /api/mine、/api/orders、/api/messages、/api/review_history   登录后
        GET  /api/admin、/api/export                 管理员
        POST /api/<动作>                             业务动作交给 services.act
"""
import csv
import hmac
import io
import json
import mimetypes
import re
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from core import ROOT, BusinessError, need, now, password_hash, text, valid_password
from db import connect, deliver_notices, public_user, rows, stats
from services import act

# 允许直接访问的静态文件，避免把整个 static 目录暴露出去
STATIC_FILES = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}

# 商品列表：只展示在售与已预订、且未被冻结的商品；rating 为该卖家已完成订单的平均分
PRODUCTS_SQL = """
SELECT p.*, u.name seller_name,
       (SELECT avg(r.rating) FROM reviews r JOIN orders o ON o.id = r.order_id
         WHERE o.seller = p.seller AND r.author = o.buyer AND o.status = '已完成') rating
  FROM products p JOIN users u ON u.id = p.seller
 WHERE p.status IN ('在售', '已预订') AND p.frozen = 0
"""

# 商品详情：不限制状态，可见性由 api_product 按“是否冻结/是否本人或管理员”再判断
PRODUCT_SQL = """
SELECT p.*, u.name seller_name
  FROM products p JOIN users u ON u.id = p.seller
 WHERE p.id = ?
"""

# 商品下的公开评价：只显示已完成订单的评价，最新的排前面
PRODUCT_REVIEWS_SQL = """
SELECT r.*, u.name
  FROM reviews r
  JOIN users u ON u.id = r.author
  JOIN orders o ON o.id = r.order_id
 WHERE o.product = ? AND o.status = '已完成'
 ORDER BY r.updated DESC
"""

# 会话查询：拿到用户信息的同时带出该会话的 CSRF 令牌，供写操作校验
ME_SQL = """
SELECT users.*, sessions.csrf
  FROM users JOIN sessions ON sessions.user_id = users.id
 WHERE sessions.token = ? AND expires > ?
"""


class Handler(BaseHTTPRequestHandler):
    """HTTP 请求处理类：每个请求新建一个实例，处理完即销毁，不要在这里存状态。

    一次请求的流程:
        请求 → do_GET / do_POST → user() 识别会话 → 路由表命中 api_xxx
        →（写操作）services.act() → send() 统一下发 JSON 或文件

    新增一个接口只需三步:
        1. 写方法  def api_xxx(self, db, user, query): return self.send(...)
        2. 在 PUBLIC_ROUTES / USER_ROUTES / ADMIN_ROUTES 里登记路径；
        3. 写操作则在 services.py 加动作函数，并登记到 ACTIONS。

    职责边界:
        * 本类只处理 HTTP 细节（读参数、鉴权、状态码、响应头），不写业务规则；
        * 业务判断在 services.py，SQL 访问在 db.py；
        * self.server 是所有请求共享的服务器对象（db_path、login_attempts 挂在上面）。
    """

    def log_message(self, fmt, *args):
        """覆盖基类方法：关闭标准库默认的访问日志，避免刷屏。"""
        pass

    # ---------- 响应 ----------

    def send(self, data, status=200, cookie=None, ctype='application/json; charset=utf-8'):
        """统一响应出口：序列化数据 → 写响应头 → 写正文。

        参数:
            data   dict / list → 自动转 JSON（ensure_ascii=False，中文不转义）；
                   bytes → 原样输出，用于静态页面与 CSV。
            status HTTP 状态码，默认 200，出错时传 400 / 409 / 500；
            cookie 需要下发或清除的 Set-Cookie 字符串；
            ctype  Content-Type，静态文件与 CSV 会覆盖默认值。

        示例:
            self.send({'ok': True})                        # 200 + JSON
            self.send({'error': '商品不可见'}, 400)         # 400 + JSON
            self.send(html, ctype='text/html; charset=utf-8')
        """
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        # 基础安全头：禁止嗅探类型、禁止缓存、限制只加载本站资源与 data: 图片
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                         "script-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def user(self, db):
        """从请求头 Cookie 的 sid 令牌解析当前登录用户。

        返回:
            sqlite3.Row，包含 users.* 与 sessions.csrf；
            未登录、Cookie 缺失或会话过期时返回 None。

        为什么连 csrf 一起查出来：写操作要拿请求头里的 X-CSRF-Token 与它比对，
        一次查询省掉后面再查一遍数据库。
        """
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookie['sid'].value
        except (KeyError, ValueError):
            return None
        return db.execute(ME_SQL, (token, now())).fetchone()

    # ---------- GET：静态文件与路由 ----------

    def do_GET(self):
        """处理所有查询类请求，按权限从宽到严依次尝试：

            0. 不是 /api/ 开头   → 静态文件（/、/app.js、/style.css）
            1. PUBLIC_ROUTES    → 任何人可访问（/api/me、/api/products、/api/product）
            2. 其余接口          → 必须登录，否则 400「请先登录」
            3. USER_ROUTES      → 登录用户（/api/mine、/api/orders、/api/messages、/api/review_history）
            4. 其余接口          → 必须是管理员，否则 400「需要管理员权限」
            5. ADMIN_ROUTES     → 管理员（/api/admin、/api/export）
            6. 都不匹配          → 400「接口不存在」

        错误约定:
            BusinessError / ValueError / TypeError → 400 + 具体原因；
            其他异常 → 控制台打印摘要，返回 500「服务器处理失败」。
        """
        try:
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if not url.path.startswith('/api/'):
                return self.serve_static(url.path)
            with connect(self.server.db_path) as db:
                user = self.user(db)
                route = PUBLIC_ROUTES.get(url.path)
                if route:
                    return route(self, db, user, query)
                need(user is not None, '请先登录')
                route = USER_ROUTES.get(url.path)
                if route:
                    return route(self, db, user, query)
                need(user['role'] == 'admin', '需要管理员权限')
                route = ADMIN_ROUTES.get(url.path)
                if route:
                    return route(self, db, user, query)
                raise BusinessError('接口不存在')
        except (BusinessError, ValueError, TypeError) as e:
            self.send({'error': str(e)}, 400)
        except Exception as e:
            print('GET error:', repr(e))
            self.send({'error': '服务器处理失败'}, 500)

    def serve_static(self, path):
        """返回前端页面与资源。

        只允许 STATIC_FILES 白名单里的三个路径，避免通过 ../ 之类的路径
        读到项目中的其他文件；其他路径（如 /favicon.ico）返回 400「页面不存在」。
        """
        need(path in STATIC_FILES, '页面不存在')
        file = ROOT / 'static' / STATIC_FILES[path]
        ctype = mimetypes.guess_type(file)[0] + '; charset=utf-8'
        return self.send(file.read_bytes(), ctype=ctype)

    def api_me(self, db, user, query):
        """GET /api/me —— 查询当前登录状态。

        返回:
            {"user": {"id","student","name","role","banned_until"} | null,
             "csrf": "…"}

        前端用它判断是否登录；拿到 csrf 后，写请求要放在 X-CSRF-Token 头里。
        """
        return self.send({'user': public_user(user) if user else None,
                          'csrf': user['csrf'] if user else ''})

    def api_products(self, db, user, query):
        """GET /api/products —— 集市列表：搜索、筛选、排序。

        查询参数（全部可选）:
            q=教材                关键词，同时匹配标题与描述；
            category=数码电器     分类精确匹配；
            min=10&max=50         价格区间（单位：元，后端换算成“分”比较）；
            sort=price|rating     price 按价格升序；rating 按卖家评分降序；
                                  都不传则按发布时间倒序。

        返回:
            {"products": [{"id","title","description","category","price","condition",
                           "status","image","seller_name","rating","created"}, …]}
            price 单位是“分”（3500 表示 35 元），rating 是卖家历史平均分，可能为 null。

        权限: 无需登录；只返回在售/已预订且未冻结的商品。
        """
        sql = PRODUCTS_SQL
        args = []
        if query.get('q'):
            sql += ' AND (p.title LIKE ? OR p.description LIKE ?)'
            args += ['%' + query['q'] + '%'] * 2
        if query.get('category'):
            sql += ' AND p.category = ?'
            args.append(query['category'])
        for key, op in [('min', '>='), ('max', '<=')]:
            if query.get(key):
                sql += ' AND p.price ' + op + ' ?'
                args.append(round(float(query[key]) * 100))
        # ORDER BY 只能来自固定映射，避免把用户输入拼接进 SQL
        order_by = {'price': 'p.price ASC', 'rating': 'rating DESC'}.get(
            query.get('sort'), 'p.created DESC, p.id DESC')
        sql += ' ORDER BY ' + order_by
        return self.send({'products': rows(db, sql, args)})

    def api_product(self, db, user, query):
        """GET /api/product?id=1 —— 商品详情 + 公开评价。

        可见性规则:
            * 未冻结、状态为在售/已预订/已售出：所有人可见；
            * 冻结或下架的商品：只有管理员和卖家本人可见。

        返回:
            {"product": {…}, "reviews": [{"rating","body","updated","name"}, …]}
            评价只包含该商品已完成订单的评价，按更新时间倒序。
        """
        pid = int(query.get('id', 0))
        obj = db.execute(PRODUCT_SQL, (pid,)).fetchone()
        need(obj is not None, '商品不存在')
        visible = (not obj['frozen'] and obj['status'] in ['在售', '已预订', '已售出'])
        need(visible or (user and (user['role'] == 'admin' or user['id'] == obj['seller'])), '商品不可见')
        favorite = bool(user and db.execute('SELECT 1 FROM favorites WHERE user_id=? AND product=?', (user['id'], pid)).fetchone())
        return self.send({'favorite': favorite, 'product': dict(obj), 'reviews': rows(db, PRODUCT_REVIEWS_SQL, (pid,))})

    def api_mine(self, db, user, query):
        """GET /api/mine —— 个人中心数据。

        返回:
            products  我发布的商品（含待审核、已下架、已售出）；
            favorites 我收藏的商品；
            notices   我的站内通知，最近 100 条，按 id 倒序。
        """
        deliver_notices(db, user['id'])
        return self.send({
            'products': rows(db, 'SELECT * FROM products WHERE seller=? ORDER BY id DESC', (user['id'],)),
            'favorites': rows(db, 'SELECT p.* FROM favorites f JOIN products p ON p.id=f.product '
                                 'WHERE f.user_id=?', (user['id'],)),
            'notices': rows(db, 'SELECT * FROM notices WHERE user_id=? ORDER BY id DESC LIMIT 100',
                            (user['id'],)),
        })

    def api_orders(self, db, user, query):
        """GET /api/orders —— 我参与的订单（买或卖）与相关撤回申请。

        返回:
            orders       [{id, product, buyer, seller, title, price, status,
                           buyer_confirm, seller_confirm, place, meeting, note,
                           created, completed, buyer_name, seller_name, frozen}]
            withdrawals  [{id, order_id, author, body, status, result, created}]

        只返回本人作为买家或卖家的记录，按 id 倒序。
        """
        return self.send({
            'orders': rows(db, 'SELECT o.*,b.name buyer_name,s.name seller_name,p.frozen '
                               'FROM orders o JOIN users b ON b.id=o.buyer JOIN users s ON s.id=o.seller '
                               'JOIN products p ON p.id=o.product WHERE o.buyer=? OR o.seller=? '
                               'ORDER BY o.id DESC', (user['id'], user['id'])),
            'withdrawals': rows(db, 'SELECT w.* FROM withdrawals w JOIN orders o ON o.id=w.order_id '
                                    'WHERE o.buyer=? OR o.seller=? ORDER BY w.id DESC',
                                (user['id'], user['id'])),
        })

    def api_messages(self, db, user, query):
        """GET /api/messages?id=5 —— 某件商品的咨询记录（私有会话）。

        返回:
            {"messages": [{"id","product","sender","receiver","body","created",
                           "sender_name","receiver_name"}, …]}

        权限: 只返回本人是发送者或接收者的消息，
              同一商品下不同买家之间的对话彼此不可见。
        """
        pid = int(query.get('id', 0))
        return self.send({'messages': rows(db, 'SELECT m.*,s.name sender_name,r.name receiver_name '
                                               'FROM messages m JOIN users s ON s.id=m.sender '
                                               'JOIN users r ON r.id=m.receiver '
                                               'WHERE m.product=? AND (m.sender=? OR m.receiver=?) '
                                               'ORDER BY m.id', (pid, user['id'], user['id']))})

    def api_review_history(self, db, user, query):
        """GET /api/review_history?id=3 —— 评价修改历史与确认撤回历史。

        参数:
            id 订单编号（只会返回本人参与的订单）。

        返回:
            history       我的评价在历次修改前的内容（评分/文字/时间）；
            confirmations 该订单的确认与撤回记录，管理员批准撤回时写入，
                          其中 revoked 是被撤销掉的那次确认时间。
        """
        oid = int(query.get('id', 0))
        return self.send({
            'history': rows(db, 'SELECT h.* FROM review_history h JOIN reviews r ON r.id=h.review_id '
                                'WHERE r.order_id=? AND r.author=? ORDER BY h.id DESC', (oid, user['id'])),
            'confirmations': rows(db, 'SELECT h.* FROM confirmation_history h JOIN orders o ON o.id=h.order_id '
                                      'WHERE o.id=? AND (o.buyer=? OR o.seller=?)',
                                  (oid, user['id'], user['id'])),
        })

    def api_admin(self, db, user, query):
        """GET /api/admin —— 后台首页数据，一次请求返回全部面板内容。

        返回:
            products     全部商品（含待审核、已下架、冻结）；
            users        用户列表（不含密码）；
            reports      举报列表；
            withdrawals  撤回申请列表；
            disputes     争议中的订单；
            audit        最近 100 条操作审计；
            stats        按 start/end 时间戳统计的完成数、成交额、发布数与待处理举报。

        权限: 仅管理员。
        """
        return self.send({
            'products': rows(db, 'SELECT p.*,u.name seller_name FROM products p JOIN users u ON u.id=p.seller '
                                 'ORDER BY p.id DESC'),
            'users': rows(db, 'SELECT id,student,name,role,banned_until FROM users'),
            'reports': rows(db, 'SELECT * FROM reports ORDER BY id DESC'),
            'withdrawals': rows(db, 'SELECT * FROM withdrawals ORDER BY id DESC'),
            'disputes': rows(db, "SELECT * FROM orders WHERE status='争议处理中'"),
            'audit': rows(db, 'SELECT a.*,u.name FROM audit a JOIN users u ON u.id=a.actor '
                              'ORDER BY a.id DESC LIMIT 100'),
            'stats': stats(db, int(query.get('start', 0)), int(query.get('end', 4102444800))),
        })

    def api_export(self, db, user, query):
        """GET /api/export?start=0&end=4102444800 —— 导出已完成订单为 CSV。

        细节:
            * 表头: 订单编号 / 商品 / 约定金额（元） / 完成时间；
            * 金额由“分”换算成“元”；
            * 以 = + - @ 开头的标题会被 Excel 当公式执行，因此前面补一个单引号；
            * 输出前加 BOM（\ufeff），用 Excel 打开中文不乱码。
        """
        start, end = int(query.get('start', 0)), int(query.get('end', 4102444800))
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['订单编号', '商品', '约定金额（元）', '完成时间'])
        completed = db.execute("SELECT * FROM orders WHERE status='已完成' AND completed BETWEEN ? AND ?",
                               (start, end)).fetchall()
        for row in completed:
            # 以 = + - @ 开头的单元格会被 Excel 当公式执行，加单引号转义
            safe_title = ("'" + row['title']) if row['title'].lstrip().startswith(('=', '+', '-', '@')) \
                else row['title']
            writer.writerow([row['id'], safe_title, row['price'] / 100,
                             time.strftime('%Y-%m-%d %H:%M', time.localtime(row['completed']))])
        return self.send(('\ufeff' + buf.getvalue()).encode(), ctype='text/csv; charset=utf-8')

    # ---------- POST ----------

    def do_POST(self):
        """处理所有写操作，流程固定：

            1. 请求头：Content-Type 必须是 application/json；
            2. 来源：带 Origin 时必须与本站 Host 一致（防跨站提交）；
            3. 体积：0 < Content-Length <= 8MB；
            4. 解析：json.loads 后必须是对象；
            5. 动作：/api/order → order；
            6. 事务：BEGIN IMMEDIATE 拿写锁，后续读写都在同一事务里；
            7. 路由：register / login 在未登录时处理；
            8. 鉴权：其余动作先查会话，再比对 X-CSRF-Token；
            9. 分发：logout 单独处理，其他动作交给 services.act()；
           10. 提交事务并返回。

        错误映射:
            IntegrityError（唯一约束冲突）→ 409；
            BusinessError / ValueError / TypeError / JSON 解析失败 → 400；
            其他 → 500，具体异常只打印在服务端控制台。
        """
        try:
            need(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', '请求必须为JSON')
            origin = self.headers.get('Origin')
            need(not origin or urlsplit(origin).netloc == self.headers.get('Host'), '请求来源不匹配')
            size = int(self.headers.get('Content-Length', '0'))
            need(0 < size <= 8 * 1024 * 1024, '请求内容过大')
            payload = json.loads(self.rfile.read(size))
            need(isinstance(payload, dict), 'JSON对象格式无效')
            # /api/order → order；保留完整路径用于判断是否真的在 /api/ 下
            action = urlsplit(self.path).path.removeprefix('/api/')
            need(self.path.startswith('/api/'), '接口不存在')
            with connect(self.server.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                if action == 'register':
                    return self.register(db, payload)
                if action == 'login':
                    return self.login(db, payload)
                user = self.user(db)
                need(user is not None, '请先登录')
                # 双提交校验：请求头里的令牌必须与会话中保存的一致，防止跨站伪造
                need(hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), user['csrf']),
                     '会话校验失败，请刷新后重试')
                if action == 'logout':
                    return self.logout(db)
                result = act(db, user, action, payload)
                db.commit()
                return self.send(result)
        except sqlite3.IntegrityError:
            # 唯一约束冲突：重复学号、同一商品重复下单、重复举报等
            self.send({'error': '该操作已存在、已被占用或账号重复，请刷新后重试'}, 409)
        except (BusinessError, ValueError, TypeError, json.JSONDecodeError) as e:
            self.send({'error': str(e)}, 400)
        except Exception as e:
            print('POST error:', repr(e))
            self.send({'error': '服务器处理失败'}, 500)

    def register(self, db, payload):
        """POST /api/register —— 注册新账号（不需要登录）。

        请求体:
            {"student": "20260003", "name": "王同学", "password": "Test1234"}

        校验:
            student 8—12 位纯数字；password 8 位以上且同时含大写、小写、数字；
            name 2—20 字。

        返回:
            {"ok": true, "message": "注册成功，请使用学号登录"}

        学号重复由数据库唯一约束拦截，最终返回 409。
        """
        student = text(payload, 'student', 8, 12)
        need(student.isdigit(), '学号须为8至12位数字')
        pw = text(payload, 'password', 8, 100)
        need(re.search('[A-Z]', pw) and re.search('[a-z]', pw) and re.search('[0-9]', pw),
             '密码需包含大小写字母和数字')
        db.execute('INSERT INTO users(student,name,password) VALUES(?,?,?)',
                   (student, text(payload, 'name', 2, 20), password_hash(pw)))
        db.commit()
        return self.send({'ok': True, 'message': '注册成功，请使用学号登录'})

    def login(self, db, payload):
        """POST /api/login —— 登录并创建会话。

        请求体:
            {"student": "admin", "password": "Admin123!"}

        返回:
            {"ok": true, "user": {…}, "csrf": "…"}，同时下发 Cookie: sid=…

        安全策略:
            * 同一 IP + 学号连续失败 5 次，锁定 5 分钟（计数存内存，重启清零）；
            * 会话令牌 32 字节随机，有效期 24 小时；
            * Cookie 带 HttpOnly（JS 读不到）与 SameSite=Strict（防跨站携带）；
            * 登录成功才返回 csrf，之后的写请求必须带 X-CSRF-Token。
        """
        student = text(payload, 'student', 1, 30)
        obj = db.execute('SELECT * FROM users WHERE student=?', (student,)).fetchone()
        ipkey = self.client_address[0] + ':' + student
        attempt = self.server.login_attempts.get(ipkey, (0, 0))
        need(attempt[1] < now(), '失败次数过多，请5分钟后重试')
        if not (obj and valid_password(str(payload.get('password', '')), obj['password'])):
            count = attempt[0] + 1
            self.server.login_attempts[ipkey] = (count, now() + 300 if count >= 5 else 0)
            raise BusinessError('账号或密码错误')
        self.server.login_attempts.pop(ipkey, None)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        db.execute('DELETE FROM sessions WHERE expires<?', (now(),))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (token, obj['id'], csrf, now() + 86400))
        db.commit()
        cookie = f'sid={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400'
        return self.send({'ok': True, 'user': public_user(obj), 'csrf': csrf}, cookie=cookie)

    def logout(self, db):
        """POST /api/logout —— 退出登录（需要已登录 + CSRF 令牌）。

        行为:
            删除 sessions 表里的当前会话，并下发一个立即过期的 sid Cookie。

        返回:
            {"ok": true}
        """
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        db.execute('DELETE FROM sessions WHERE token=?', (cookie['sid'].value,))
        db.commit()
        return self.send({'ok': True},
                         cookie='sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')


# 路由表：路径 → 处理函数。函数签名统一为 (self, db, user, query)。
# 分组本身表达了权限要求：不需要登录 / 需要登录 / 需要管理员。
PUBLIC_ROUTES = {
    '/api/me': Handler.api_me,
    '/api/products': Handler.api_products,
    '/api/product': Handler.api_product,
}

USER_ROUTES = {
    '/api/mine': Handler.api_mine,
    '/api/orders': Handler.api_orders,
    '/api/messages': Handler.api_messages,
    '/api/review_history': Handler.api_review_history,
}

ADMIN_ROUTES = {
    '/api/admin': Handler.api_admin,
    '/api/export': Handler.api_export,
}
