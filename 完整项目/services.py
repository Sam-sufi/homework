"""业务规则层：一个动作一个函数，act() 统一做封禁校验、分发和审计。

设计约定:
    * 每个函数只处理“一个业务动作”，函数名与前端调用的动作名一致；
    * 输入校验用 core.need / core.text，失败抛 BusinessError（接口层转 400）；
    * 本层可以调用 db.py 读写数据库，但不碰 HTTP（不知道状态码、Cookie）；
    * 返回 None 表示“正常完成”，act() 会补写审计并返回 {'ok': True}；
      返回 EarlyReturn 表示“提前结束”，act() 原样返回且不写审计。

动作分组:
    商品   publish_product / edit_product / shelve_product /
           review_product / freeze_product / unfreeze_product
    互动   favorite / send_message / submit_report
    交易   place_order / accept_order / cancel_order /
           confirm_delivery / apply_withdrawal / raise_dispute
    评价   submit_review
    后台   ban_user / unban_user / handle_report /
           handle_withdrawal / resolve_dispute / read_notices
"""
import base64
import json
import time

from core import BusinessError, need, text, now
from db import notify

# 封禁用户不允许执行的“新增业务”动作
NEW_ACTIONS = {'publish', 'favorite', 'message', 'order', 'submit', 'edit'}


class EarlyReturn:
    """表示“动作已完成但无需继续处理”的返回值。

    对应旧实现里的提前 return（例如订单已存在、已经接单、订单已关闭）：
    这类分支直接返回结果，不写审计日志，保证重构前后行为一致。
    """

    def __init__(self, payload):
        self.payload = payload


# ---------- 通用小工具 ----------

def get(db, payload, table, key='id'):
    """按 id 取一行记录，取不到就抛「对象不存在」。

    参数:
        db      数据库连接；
        payload 请求数据，从中读取 id（默认键名 'id'）；
        table   表名，只传代码里写死的表名，不要传用户输入；
        key     payload 里表示 id 的字段名。
    返回:
        sqlite3.Row。
    异常:
        BusinessError('对象不存在')。
    """
    obj = db.execute(f'SELECT * FROM {table} WHERE id=?', (int(payload.get(key, 0)),)).fetchone()
    need(obj is not None, '对象不存在')
    return obj


def administrator(user):
    """管理员权限检查：不是管理员就抛「需要管理员权限」。

    所有后台动作（审核、冻结、封禁、处理举报/撤回、争议裁定）开头都会调用。
    """
    need(user['role'] == 'admin', '需要管理员权限')


def own_order(db, user, payload):
    """取出当前用户参与的订单，并校验这单确实与我有关。

    返回:
        orders 表的一行。
    异常:
        订单不存在，或我既不是买家也不是卖家（「不能操作他人订单」）。
    """
    obj = get(db, payload, 'orders')
    need(user['id'] in [obj['buyer'], obj['seller']], '不能操作他人订单')
    return obj


def _notify_trade(db, user, order, action):
    """给交易对方发一条「订单 #N 已更新：动作名」通知。

    参数:
        order  订单行（从中取 id、buyer、seller）；
        action 动作名：accept / cancel / confirm / withdraw / dispute / review。
    说明:
        只通知对方，不通知操作者本人；通知文案与旧版保持一致。
    """
    other = order['seller'] if user['id'] == order['buyer'] else order['buyer']
    notify(db, other, '订单 #' + str(order['id']) + ' 已更新：' + action)


def _product_fields(payload):
    """发布与编辑共用的商品字段校验。

    校验:
        标题 2—50 字、描述 10—1000 字、价格为正数且不超过 100 万元、
        图片不超过 5MB，且必须是 PNG/JPEG 的 data URL（同时校验文件头魔数）。
    返回:
        (标题, 描述, 价格, 图片)：价格已转成“分”的整数；图片是原始 data URL 或空串。
    """
    title = text(payload, 'title', 2, 50)
    desc = text(payload, 'description', 10, 1000)
    price = payload.get('price')
    try:
        price = round(float(price) * 100)
    except (ValueError, TypeError):
        raise BusinessError('价格必须为数字')
    need(0 < price <= 100000000, '价格必须大于0且不超过100万元')
    image = str(payload.get('image', ''))
    need(len(image) <= 7500000, '图片不超过5MB')
    if image:
        need(image.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,')), '仅支持PNG或JPEG图片')
        try:
            raw = base64.b64decode(image.split(',', 1)[1], validate=True)
        except Exception:
            raise BusinessError('图片格式无效')
        need(len(raw) <= 5 * 1024 * 1024 and
             (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff')),
             '图片内容无效或超过5MB')
    return title, desc, price, image


# ---------- 商品 ----------

def publish_product(db, user, payload):
    """发布商品：校验字段后写入商品表，状态为“待审核”。

    谁能调: 已登录、未被封禁的用户（封禁拦截在 act() 里统一做）。
    写入:   products 新增一行：status='待审核'、created=当前时间。
    后续:   管理员 review_product 通过后才变成“在售”并出现在集市里。
    """
    title, desc, price, image = _product_fields(payload)
    db.execute('INSERT INTO products(seller,title,description,category,price,condition,status,image,created) '
               'VALUES(?,?,?,?,?,?,?,?,?)',
               (user['id'], title, desc, text(payload, 'category', 1, 30), price,
                text(payload, 'condition', 1, 20), '待审核', image, now()))


def edit_product(db, user, payload):
    """编辑自己已下架或已驳回的商品，修改后重新进入“待审核”。

    前置:
        商品必须是我发布的、状态为 已下架/已驳回、且未被冻结。
    写入:
        更新标题/描述/分类/价格/成色/图片，status 重置为“待审核”，清空驳回原因。
    说明:
        在售商品不能直接编辑，必须先下架（对应需求 FR-09 的状态约束）。
    """
    title, desc, price, image = _product_fields(payload)
    obj = get(db, payload, 'products')
    need(obj['seller'] == user['id'] and obj['status'] in ['已下架', '已驳回'] and not obj['frozen'],
         '只能编辑本人未冻结的下架或驳回商品')
    db.execute("UPDATE products SET title=?,description=?,category=?,price=?,condition=?,"
               "status='待审核',image=?,reason='' WHERE id=?",
               (title, desc, text(payload, 'category', 1, 30), price,
                text(payload, 'condition', 1, 20), image or obj['image'], obj['id']))


def shelve_product(db, user, payload):
    """卖家下架自己的在售商品。

    前置: 本人商品且状态为“在售”；已预订的商品不能直接下架，需先取消订单。
    写入: products.status → 已下架。
    """
    product = get(db, payload, 'products')
    need(user['id'] == product['seller'] and product['status'] == '在售', '只能下架本人在售商品')
    db.execute("UPDATE products SET status='已下架' WHERE id=?", (product['id'],))


def review_product(db, user, payload):
    """管理员审核商品：通过 → 在售；驳回 → 已驳回并记录原因。

    前置: 调用者是管理员；商品状态为“待审核”；必须填写 2—500 字理由。
    写入: products.status、products.reason；通知卖家“商品处理结果已更新”。
    """
    product = get(db, payload, 'products')
    administrator(user)
    need(product['status'] == '待审核', '商品不是待审核状态')
    status = '在售' if payload.get('approve') else '已驳回'
    db.execute('UPDATE products SET status=?,reason=? WHERE id=?',
               (status, text(payload, 'reason', 2, 500), product['id']))
    notify(db, product['seller'], '商品处理结果已更新')


def freeze_product(db, user, payload):
    """管理员冻结商品：加冻结标记，商品不再公开、不可交易。

    前置: 管理员；填写 2—500 字依据。
    写入: products.frozen=1、reason；不改变 status，已有订单仍可继续履约。
    """
    product = get(db, payload, 'products')
    administrator(user)
    db.execute('UPDATE products SET frozen=1,reason=? WHERE id=?',
               (text(payload, 'reason', 2, 500), product['id']))
    notify(db, product['seller'], '商品处理结果已更新')


def unfreeze_product(db, user, payload):
    """管理员解除冻结（通常是卖家整改后申请）。

    前置: 管理员；填写 2—500 字依据。
    写入: products.frozen=0、reason；不会自动恢复冻结前的状态，需重新审核。
    """
    product = get(db, payload, 'products')
    administrator(user)
    db.execute('UPDATE products SET frozen=0,reason=? WHERE id=?',
               (text(payload, 'reason', 2, 500), product['id']))
    notify(db, product['seller'], '商品处理结果已更新')


# ---------- 收藏与咨询 ----------

def favorite(db, user, payload):
    """收藏 / 取消收藏（同一个动作，按当前是否已收藏切换）。

    前置: 商品在售或已预订，且未被冻结。
    写入: favorites 表插入或删除一行。
    """
    product = get(db, payload, 'products')
    need(product['status'] in ['在售', '已预订'] and not product['frozen'], '商品当前不可收藏')
    if db.execute('SELECT 1 FROM favorites WHERE user_id=? AND product=?',
                  (user['id'], product['id'])).fetchone():
        db.execute('DELETE FROM favorites WHERE user_id=? AND product=?', (user['id'], product['id']))
    else:
        db.execute('INSERT INTO favorites VALUES(?,?)', (user['id'], product['id']))


def send_message(db, user, payload):
    """商品咨询：买家发起会话，卖家回复已有咨询。

    规则:
        * 不能给自己发消息；
        * 会话必须关联商品卖家（买家发给卖家，卖家回复曾来信的买家）；
        * 卖家只能回复已有咨询，不能主动群发；
        * 买家只能对在售/已预订且未冻结的商品发起新咨询。
    写入: messages 新增一行；通知接收人“收到新的商品咨询”。
    """
    uid = user['id']
    product = get(db, payload, 'products')
    receiver = int(payload.get('receiver', product['seller']))
    need(receiver != uid, '不能向自己发消息')
    need(uid == product['seller'] or receiver == product['seller'], '咨询必须关联商品卖家')
    need(db.execute('SELECT 1 FROM users WHERE id=?', (receiver,)).fetchone(), '接收人不存在')
    if uid == product['seller']:
        need(db.execute('SELECT 1 FROM messages WHERE product=? AND sender=? AND receiver=?',
                        (product['id'], receiver, uid)).fetchone(), '只能回复已有咨询')
    else:
        need(not product['frozen'] and product['status'] in ['在售', '已预订'], '商品不接受新咨询')
    db.execute('INSERT INTO messages(product,sender,receiver,body,created) VALUES(?,?,?,?,?)',
               (product['id'], uid, receiver, text(payload, 'body', 1, 1000), now()))
    notify(db, receiver, '收到新的商品咨询')


# ---------- 交易 ----------

def place_order(db, user, payload):
    """买家下单：占用商品并生成订单（先到先得）。

    前置: 已登录且未封禁；不能买自己的商品；商品在售且未冻结；卖家不在封禁期。
    幂等: 同一买家对同一商品已有有效订单时直接返回原订单，不重复建单。
    写入: orders 新增（status='待接单'）+ products.status → 已预订；通知卖家。
    并发: 依靠 act() 所在事务的 BEGIN IMMEDIATE 与唯一索引 one_active_order 保证
          同一商品只有一条有效订单。
    """
    uid = user['id']
    product = get(db, payload, 'products')
    need(product['seller'] != uid, '不能购买自己的商品')
    existing = db.execute(
        "SELECT id FROM orders WHERE product=? AND buyer=? AND status IN ('待接单','待交付','争议处理中')",
        (product['id'], uid)).fetchone()
    if existing:
        return EarlyReturn({'ok': True, 'message': '订单已存在', 'order_id': existing['id']})
    need(not product['frozen'] and product['status'] == '在售', '商品已被占用或不可交易')
    need(db.execute('SELECT banned_until FROM users WHERE id=?', (product['seller'],)).fetchone()[0] <= now(),
         '卖家账号处于封禁期')
    db.execute("INSERT INTO orders(product,buyer,seller,title,price,status,created) "
               "VALUES(?,?,?,?,?,'待接单',?)",
               (product['id'], uid, product['seller'], product['title'], product['price'], now()))
    db.execute("UPDATE products SET status='已预订' WHERE id=?", (product['id'],))
    notify(db, product['seller'], '有新订单，请接单或拒绝')


def accept_order(db, user, payload):
    """卖家接单：填写交付地点与时间，订单进入“待交付”。

    前置:
        本人是卖家；订单为“待接单”；交付时间格式 %Y-%m-%dT%H:%M，
        且在当前时间之后、下单后 72 小时之内。
    幂等: 已经接单的订单再次接单，直接返回成功。
    写入: orders.status=待交付、place、meeting；通知买家。
    """
    uid = user['id']
    order = own_order(db, user, payload)
    oid = order['id']
    if uid == order['seller'] and order['status'] == '待交付':
        return EarlyReturn({'ok': True, 'message': '订单已经接单'})
    need(uid == order['seller'] and order['status'] == '待接单', '只有卖家可接待接单订单')
    place = text(payload, 'place', 2, 100)
    meeting = text(payload, 'meeting', 1, 50)
    try:
        stamp = int(time.mktime(time.strptime(meeting, '%Y-%m-%dT%H:%M')))
    except ValueError:
        raise BusinessError('交付时间格式无效')
    need(now() <= stamp <= order['created'] + 72 * 3600, '交付时间须在当前时间之后且下单后72小时内')
    db.execute("UPDATE orders SET status='待交付',place=?,meeting=? WHERE id=?", (place, meeting, oid))
    _notify_trade(db, user, order, 'accept')


def cancel_order(db, user, payload):
    """正常取消订单：关闭订单并释放商品占用。

    前置: 订单为 待接单/待交付，且买卖双方都未确认；封禁用户不能普通取消。
    幂等: 已关闭的订单再次取消直接返回成功。
    写入: orders.status=已关闭、products.status=在售；通知对方。
    """
    order = own_order(db, user, payload)
    oid = order['id']
    if order['status'] == '已关闭':
        return EarlyReturn({'ok': True, 'message': '订单已经关闭'})
    need(user['banned_until'] <= now(), '封禁期不允许普通取消，请联系管理员')
    need(order['status'] in ['待接单', '待交付'] and not order['buyer_confirm'] and not order['seller_confirm'],
         '已有确认或争议，不允许普通取消')
    db.execute("UPDATE orders SET status='已关闭' WHERE id=?", (oid,))
    db.execute("UPDATE products SET status='在售' WHERE id=?", (order['product'],))
    _notify_trade(db, user, order, 'cancel')


def confirm_delivery(db, user, payload):
    """确认交付（买家点“已收到”、卖家点“已交付”）。

    前置: 订单为 待交付 或 已完成。
    逻辑:
        记录本人确认时间；当双方都确认且订单尚未完成时，
        订单转“已完成”、商品转“已售出”。
    写入: orders.buyer_confirm / seller_confirm，必要时 status、completed；
          products.status；通知对方。
    幂等: 重复确认不会重复变更。
    """
    uid = user['id']
    order = own_order(db, user, payload)
    oid = order['id']
    need(order['status'] in ['待交付', '已完成'], '订单不可确认交付')
    field = 'buyer_confirm' if uid == order['buyer'] else 'seller_confirm'
    if not order[field] and order['status'] == '待交付':
        db.execute(f'UPDATE orders SET {field}=? WHERE id=?', (now(), oid))
    fresh = db.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if fresh['buyer_confirm'] and fresh['seller_confirm'] and fresh['status'] != '已完成':
        db.execute("UPDATE orders SET status='已完成',completed=? WHERE id=?", (now(), oid))
        db.execute("UPDATE products SET status='已售出' WHERE id=?", (order['product'],))
    _notify_trade(db, user, order, 'confirm')


def apply_withdrawal(db, user, payload):
    """申请撤回本人在 2 小时内做出的确认。

    前置:
        订单为 待交付/已完成；本人已确认且距今不超过 7200 秒；
        同一订单没有处于待审核的撤回申请。
    写入: withdrawals 新增一行（status='待审核'）；通知对方。
    后续: 由管理员 handle_withdrawal 批准或驳回。
    """
    uid = user['id']
    order = own_order(db, user, payload)
    oid = order['id']
    stamp = order['buyer_confirm'] if uid == order['buyer'] else order['seller_confirm']
    need(order['status'] in ['待交付', '已完成'] and stamp and now() - stamp <= 7200,
         '须在本人确认后2小时内申请')
    need(not db.execute("SELECT 1 FROM withdrawals WHERE order_id=? AND status='待审核'", (oid,)).fetchone(),
         '已有撤回申请正在审核')
    db.execute('INSERT INTO withdrawals(order_id,author,body,created) VALUES(?,?,?,?)',
               (oid, uid, text(payload, 'body', 2, 500), now()))
    _notify_trade(db, user, order, 'withdraw')


def raise_dispute(db, user, payload):
    """直接发起争议（仅限“待交付”订单）。

    前置: 本人参与该订单，且状态为“待交付”；填写 2—500 字说明。
    写入: orders.status=争议处理中、note 记录说明；商品保持占用；通知对方。
    说明: 已完成的订单必须先申请撤回，不能直接发起争议。
    """
    order = own_order(db, user, payload)
    need(order['status'] == '待交付', '只有待交付订单可直接发起争议，完成订单请先申请撤回')
    db.execute("UPDATE orders SET status='争议处理中',note=? WHERE id=?",
               (text(payload, 'body', 2, 500), order['id']))
    _notify_trade(db, user, order, 'dispute')


def submit_review(db, user, payload):
    """提交评价：完成订单可评一次，可修改并保留历史。

    前置: 订单为“已完成”；评分为 1—5 的整数；正文 1—200 字。
    逻辑:
        首次提交写入 reviews；再次提交时，先把旧内容写入 review_history，
        再覆盖 reviews 里的评分与正文。
    写入: reviews / review_history；通知对方。
    """
    uid = user['id']
    order = own_order(db, user, payload)
    oid = order['id']
    need(order['status'] == '已完成', '仅已完成订单可评价')
    rating = int(payload.get('rating', 0))
    need(1 <= rating <= 5, '评分范围1至5')
    body = text(payload, 'body', 1, 200)
    existing = db.execute('SELECT * FROM reviews WHERE order_id=? AND author=?', (oid, uid)).fetchone()
    if existing:
        db.execute('INSERT INTO review_history(review_id,rating,body,created) VALUES(?,?,?,?)',
                   (existing['id'], existing['rating'], existing['body'], existing['updated']))
    db.execute('INSERT INTO reviews(order_id,author,rating,body,updated) VALUES(?,?,?,?,?) '
               'ON CONFLICT(order_id,author) DO UPDATE SET '
               'rating=excluded.rating,body=excluded.body,updated=excluded.updated',
               (oid, uid, rating, body, now()))
    _notify_trade(db, user, order, 'review')


def submit_report(db, user, payload):
    """举报商品（同一用户对同一商品只能举报一次，由唯一索引兜底）。

    写入: reports 新增一行（status='待处理'）。
    说明: 不通知卖家，由管理员在后台处理并通知举报人。
    """
    product = get(db, payload, 'products')
    db.execute('INSERT INTO reports(author,product,body,created) VALUES(?,?,?,?)',
               (user['id'], product['id'], text(payload, 'body', 2, 500), now()))


# ---------- 后台处置 ----------

def ban_user(db, user, payload):
    """封禁账号（管理员）。

    前置: 调用者是管理员；不能封禁管理员；封禁天数 >= 7；填写理由。
    写入: users.banned_until = 当前时间 + 天数×86400；通知被处理人。
    说明: 封禁不打断在途订单——封禁用户仍可查看订单、确认交付、申请撤回，
          但不能发布、下单、收藏、咨询等新增业务（由 act() 的 NEW_ACTIONS 拦截）。
    """
    administrator(user)
    reason = text(payload, 'reason', 2, 500)
    obj = get(db, payload, 'users')
    need(obj['role'] != 'admin', '不能封禁管理员')
    days = int(payload.get('days', 7))
    need(days >= 7, '封禁至少7天')
    db.execute('UPDATE users SET banned_until=? WHERE id=?', (now() + days * 86400, obj['id']))
    notify(db, obj['id'], reason)


def unban_user(db, user, payload):
    """提前解封账号（管理员）。

    写入: users.banned_until = 0；通知被处理人。
    注意: 与旧版行为保持一致，解封同样要求 days >= 7（参数校验未放宽）。
    """
    administrator(user)
    reason = text(payload, 'reason', 2, 500)
    obj = get(db, payload, 'users')
    need(obj['role'] != 'admin', '不能封禁管理员')
    days = int(payload.get('days', 7))
    need(days >= 7, '封禁至少7天')
    db.execute('UPDATE users SET banned_until=0 WHERE id=?', (obj['id'],))
    notify(db, obj['id'], reason)


def handle_report(db, user, payload):
    """处理举报：记录结论并通知举报人。

    前置: 管理员；填写 2—500 字处理结论。
    写入: reports.status=已处理、result=结论；通知举报人。
    """
    administrator(user)
    reason = text(payload, 'reason', 2, 500)
    obj = get(db, payload, 'reports')
    db.execute("UPDATE reports SET status='已处理',result=? WHERE id=?", (reason, obj['id']))
    notify(db, obj['author'], '举报结果：' + reason)


def handle_withdrawal(db, user, payload):
    """审批撤回申请（管理员）。

    前置: 申请单状态为“待审核”；对应订单为 待交付 或 已完成。
    批准时:
        * 把被撤销的那次确认写入 confirmation_history 作为证据；
        * 订单转“争议处理中”，该方 confirm 清零，note 记「撤回获批：理由」；
        * 商品状态改为“已预订”（保持占用）。
    驳回时:
        只更新申请单状态与结论，订单与商品都不动。
    写入: confirmation_history / orders / products / withdrawals；通知申请人。
    """
    administrator(user)
    reason = text(payload, 'reason', 2, 500)
    obj = get(db, payload, 'withdrawals')
    need(obj['status'] == '待审核', '申请已处理')
    order = db.execute('SELECT * FROM orders WHERE id=?', (obj['order_id'],)).fetchone()
    need(order['status'] in ['待交付', '已完成'], '订单状态已改变')
    if payload.get('approve'):
        field = 'buyer_confirm' if obj['author'] == order['buyer'] else 'seller_confirm'
        db.execute('INSERT INTO confirmation_history(order_id,user_id,confirmed,revoked,reason) '
                   'VALUES(?,?,?,?,?)',
                   (order['id'], obj['author'], order[field], now(), reason))
        db.execute(f"UPDATE orders SET status='争议处理中',{field}=0,note=? WHERE id=?",
                   ('撤回获批：' + reason, order['id']))
        db.execute("UPDATE products SET status='已预订' WHERE id=?", (order['product'],))
    db.execute('UPDATE withdrawals SET status=?,result=? WHERE id=?',
               ('已批准' if payload.get('approve') else '已驳回', reason, obj['id']))
    notify(db, obj['author'], '撤回处理：' + reason)


def resolve_dispute(db, user, payload):
    """争议裁定（管理员）：认定完成 → 已完成；认定关闭 → 已关闭。

    前置: 订单状态为“争议处理中”。
    认定完成:
        至少要有一方有效确认，且理由不少于 10 字（作为交付证据说明）。
    写入:
        orders.status/note/completed；products.status（已售出或恢复在售）；
        通知买卖双方。
    说明: 管理员只能认定事实，不代签、不生成任何一方的确认记录。
    """
    administrator(user)
    reason = text(payload, 'reason', 2, 500)
    order = get(db, payload, 'orders')
    need(order['status'] == '争议处理中', '订单不在争议中')
    if payload.get('complete'):
        need((order['buyer_confirm'] or order['seller_confirm']) and len(reason) >= 10,
             '认定完成需至少一方有效确认，并填写不少于10字的交付证据说明')
    db.execute('UPDATE orders SET status=?,note=?,completed=? WHERE id=?',
               ('已完成' if payload.get('complete') else '已关闭', reason,
                now() if payload.get('complete') else 0, order['id']))
    db.execute('UPDATE products SET status=? WHERE id=?',
               ('已售出' if payload.get('complete') else '在售', order['product']))
    notify(db, order['buyer'], '争议裁定：' + reason)
    notify(db, order['seller'], '争议裁定：' + reason)


def read_notices(db, user, payload):
    """把当前用户的站内通知全部标记为已读。

    写入: notices.seen = 1（只影响本人通知）。
    """
    db.execute('UPDATE notices SET seen=1 WHERE user_id=?', (user['id'],))


# ---------- 动作表与统一入口 ----------

# 动作表：动作名 → 处理函数，act() 查这张表分发。
# 新增一个动作只需两步：写处理函数，再在这里登记一行；
# 前端调用 POST /api/<动作名> 即可，不用改 api.py。
ACTIONS = {
    'publish': publish_product,
    'edit': edit_product,
    'favorite': favorite,
    'message': send_message,
    'order': place_order,
    'accept': accept_order,
    'cancel': cancel_order,
    'confirm': confirm_delivery,
    'withdraw': apply_withdrawal,
    'dispute': raise_dispute,
    'review': submit_review,
    'report': submit_report,
    'shelve': shelve_product,
    'review_product': review_product,
    'freeze': freeze_product,
    'unfreeze': unfreeze_product,
    'ban': ban_user,
    'unban': unban_user,
    'handle_report': handle_report,
    'handle_withdrawal': handle_withdrawal,
    'resolve': resolve_dispute,
    'read_notices': read_notices,
}


def act(db, user, action, payload):
    """业务统一入口：校验 → 分发 → 审计 → 返回。

    参数:
        db      数据库连接（调用方已开启事务）；
        user    当前登录用户行（含 id、role、banned_until）；
        action  动作名，例如 order / accept / handle_withdrawal；
        payload 前端传来的 JSON（含业务字段与 id）。

    流程:
        1. 封禁拦截：NEW_ACTIONS 里的动作在封禁期内直接拒绝；
        2. 查动作表：找不到就抛「未知操作」；
        3. 调用对应处理函数；
        4. 若返回 EarlyReturn（订单已存在等）：原样返回，不写审计；
        5. 正常完成：写一条 audit（剔除 image/password 字段），返回 {'ok': True}。

    新增动作的步骤:
        写一个 def xxx_action(db, user, payload) 函数，再在 ACTIONS 里登记一行即可，
        接口层会自动通过 /api/xxx_action 访问到它。
    """
    need(action not in NEW_ACTIONS or user['banned_until'] <= now(), '账号处于封禁期，不允许新增业务')
    handler = ACTIONS.get(action)
    if handler is None:
        raise BusinessError('未知操作')
    result = handler(db, user, payload)
    if isinstance(result, EarlyReturn):
        return result.payload
    detail = json.dumps({k: v for k, v in payload.items() if k not in ['image', 'password']},
                        ensure_ascii=False)[:2000]
    db.execute('INSERT INTO audit(actor,action,target,detail,created) VALUES(?,?,?,?,?)',
               (user['id'], action, int(payload.get('id', 0)), detail, now()))
    return {'ok': True, 'message': '操作成功'}
