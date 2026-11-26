"""数据层：数据库连接、建表、演示数据与常用查询。

本层只关心“怎么读写 SQLite”，不判断业务对错；业务规则在 services.py。
连接使用 sqlite3 标准库，数据库文件默认在 data/campus.db。
"""
import sqlite3
from pathlib import Path

from core import ROOT, now, password_hash


class Database(sqlite3.Connection):
    """sqlite3.Connection 的子类，只改了一件事：with 结束时不只提交/回滚，还关闭连接。

    用法:
        with connect(path) as db:
            db.execute(...)
        # 正常结束 → commit + close；中途抛异常 → rollback + close
    """

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path):
    """打开数据库连接，并做好三项设置。

    参数:
        path  数据库文件路径（文件不存在会自动创建）。

    设置:
        timeout=15          写锁被占用时最多等待 15 秒；
        row_factory=Row     查询结果可按列名取值：row['title']；
        PRAGMA foreign_keys 打开外键约束（SQLite 默认是关闭的）。

    返回:
        Database 连接对象，建议配合 with 使用，自动提交/回滚/关闭。
    """
    db = sqlite3.connect(path, timeout=15, factory=Database)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def init_db(path):
    """首次启动的初始化：建表 + 写演示数据。

    流程:
        1. 确保数据库所在目录存在；
        2. 执行 schema.sql（语句全是 IF NOT EXISTS，重复执行安全）；
        3. users 表已有数据就直接返回，不覆盖你演示时产生的数据；
        4. 否则插入 3 个演示账号 + 6 件在售商品（卖家固定为 2 号林同学）。

    演示账号:
        admin / 管理员（管理员）
        20260001 / 林同学（卖家）
        20260002 / 陈同学（买家）
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        if db.execute('SELECT count(*) FROM users').fetchone()[0]:
            return
        for student, name, pw, role in [
            ('admin', '管理员', 'Admin123!', 'admin'),
            ('20260001', '林同学', 'Seller123!', 'student'),
            ('20260002', '陈同学', 'Buyer123!', 'student'),
        ]:
            db.execute('INSERT INTO users(student,name,password,role) VALUES(?,?,?,?)',
                       (student, name, password_hash(pw), role))
        samples = [
            ('高等数学教材 全套', '教材保存完好，内页无缺页，适合大一学习使用，可在图书馆交接。', '教材书籍', 3500, '八成新'),
            ('护眼台灯', '毕业闲置台灯，三档色温均可正常使用，附电源线，支持当面检查。', '数码电器', 6800, '九成新'),
            ('校园通勤自行车', '骑行正常，车胎刚更换，适合校园内通勤，停车处当面交接。', '运动出行', 18000, '八成新'),
            ('桌面收纳盒', '宿舍整理好物，收纳文具和小物件，整套出售，已经清洁。', '宿舍生活', 1800, '九成新'),
            ('无线键盘', '键位正常，无明显磨损，附接收器，可在校内试用后交付。', '数码电器', 8900, '九成新'),
            ('羽毛球拍', '两支球拍及收纳袋，入门练习适用，握把完好，可查看实物。', '运动出行', 4500, '八成新'),
        ]
        for title, desc, cat, price, condition in samples:
            db.execute('INSERT INTO products(seller,title,description,category,price,condition,status,created) '
                       "VALUES(2,?,?,?,?,?,'在售',?)",
                       (title, desc, cat, price, condition, now()))


def rows(db, sql, args=()):
    """执行查询并返回“字典列表”，方便直接 json 序列化。

    参数:
        db   数据库连接；
        sql  带 ? 占位符的 SQL（参数一律用占位符，不拼字符串）；
        args 占位符对应的值。
    返回:
        [{'id': 1, 'title': '…'}, …]；没有结果时是空列表。
    """
    return [dict(x) for x in db.execute(sql, args)]


def notify(db, uid, body):
    db.execute('INSERT INTO notice_queue(user_id,body,created) VALUES(?,?,?)', (uid, body, now()))

def deliver_notices(db, uid):
    # Serialize concurrent inbox reads. Delivery and removal commit together.
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('INSERT INTO notices(user_id,body,created) SELECT user_id,body,created FROM notice_queue WHERE user_id=? ORDER BY id', (uid,))
        db.execute('DELETE FROM notice_queue WHERE user_id=?', (uid,))
        db.commit()
    except (sqlite3.IntegrityError, sqlite3.OperationalError) as error:
        db.rollback()
        print('站内通知将在下次打开收件箱时重试', type(error).__name__)

def stats(db, start=0, end=4102444800):
    """统计后台仪表盘需要的数字。

    参数:
        start/end  时间戳区间（左闭右闭）；end 默认 2100 年，相当于不限结束时间。

    返回:
        completed  区间内已完成订单数；
        amount     区间内成交约定金额（单位：分）；
        products   区间内发布的商品数；
        reports    当前待处理举报数（不受时间区间影响）。
    """
    data = dict(db.execute(
        "SELECT count(*) completed,coalesce(sum(price),0) amount FROM orders "
        "WHERE status='已完成' AND completed BETWEEN ? AND ?", (start, end)).fetchone())
    data.update(
        products=db.execute('SELECT count(*) FROM products WHERE created BETWEEN ? AND ?', (start, end)).fetchone()[0],
        reports=db.execute("SELECT count(*) FROM reports WHERE status='待处理'").fetchone()[0],
    )
    return data


def public_user(u):
    """把用户记录裁剪成可以公开返回的字段。

    去掉 password 等敏感列，只保留:
        id / student / name / role / banned_until
    用于登录返回、/api/me、商品页卖家信息等场景。
    """
    return {k: u[k] for k in ['id', 'student', 'name', 'role', 'banned_until']}
