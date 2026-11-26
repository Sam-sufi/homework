"""HTTP 集成测试：真起服务、真发请求、真查数据库。

做法:
* 每个测试用「临时数据库 + 随机端口」起一个独立服务，跑完即删，
不会动 data/campus.db，也不会受其他测试影响；
* Client 模拟浏览器：自动保存 sid Cookie 与 CSRF 令牌；
* 断言不只看状态码，还会直连数据库核对订单、商品、统计等副作用。

运行:
python -m unittest discover -s tests -v
"""
import concurrent.futures,csv,http.client,json,sys,tempfile,threading,time,unittest
from urllib.parse import quote
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_server,connect,now

def raw_get(port,path):
    """直接发 GET，返回 (状态码, 正文, 响应头)。

    静态页面与入口校验用例不需要登录态，用这个函数比 Client 更直观。
    """
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=15)
    conn.request('GET',path);res=conn.getresponse();body=res.read()
    status,headers=res.status,dict(res.getheaders());conn.close()
    return status,body,headers

def raw_post(port,path,body,headers):
    """直接发 POST，返回 (状态码, 解析后的正文)。

    请求头由调用方完全控制，用来构造 Content-Type、Origin、空正文等异常请求。
    """
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=15)
    conn.request('POST',path,body,headers);res=conn.getresponse();raw=res.read()
    ctype,status=res.getheader('Content-Type',''),res.status;conn.close()
    return status,json.loads(raw) if 'json' in ctype else raw

class Client:
    """极简 HTTP 客户端，模拟一个浏览器会话。

    保存两样东西：
        cookie  服务端下发的 sid，后续请求自动带上；
        csrf    登录后返回的令牌，写请求放在 X-CSRF-Token 头里。
    """
    def __init__(self, port):
        """创建客户端；port 是测试服务器端口。"""
        self.port = port
        self.cookie = ''
        self.csrf = ''
        self.headers = {}
    def req(self,path,data=None,csrf=True):
        """发一个请求并解包响应。

        参数:
            path  相对 /api/ 的路径，可带查询串，如 products?q=教材；
            data  为 None 时发 GET，否则发 POST + JSON 正文；
            csrf  是否携带 X-CSRF-Token（传 False 用于验证 CSRF 拦截）。
        返回:
            (状态码, 解析后的 JSON；非 JSON 响应则返回原始 bytes)
        副作用:
            自动保存响应里的 Set-Cookie 与 csrf 令牌，供后续请求使用；
            同时把本次响应的全部响应头存进 self.headers，便于检查安全头。
        """
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=15)
        headers={'Cookie':self.cookie}
        if data is not None:headers.update({'Content-Type':'application/json','X-CSRF-Token':self.csrf if csrf else ''})
        conn.request('POST' if data is not None else 'GET','/api/'+quote(path,safe='/?=&'),json.dumps(data) if data is not None else None,headers)
        res=conn.getresponse();raw=res.read();status=res.status
        self.headers=dict(res.getheaders())
        if res.getheader('Set-Cookie'):self.cookie=res.getheader('Set-Cookie').split(';')[0]
        ctype=res.getheader('Content-Type','');conn.close();out=json.loads(raw) if 'json' in ctype else raw
        if isinstance(out,dict) and 'csrf' in out:self.csrf=out['csrf']
        return status,out
    def login(self, user, pw):
        """登录并断言成功；失败会在这里直接抛 AssertionError。"""
        assert self.req('login', {'student': user, 'password': pw})[0] == 200

class PlatformTests(unittest.TestCase):
    """平台核心流程的集成测试集合。

    setUp 会准备四个客户端：
        admin   管理员
        seller  20260001 卖家
        buyer   20260002 买家
        other   独立客户端，用于注册、并发、越权等场景
    """
    def setUp(self):
        """每个测试开始前：建临时数据库、起服务、登录三个固定角色。
        """
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/'test.db';self.server=create_server(port=0,db_path=self.db);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.admin=Client(self.server.server_port);self.seller=Client(self.server.server_port);self.buyer=Client(self.server.server_port);self.other=Client(self.server.server_port)
        self.admin.login('admin','Admin123!');self.seller.login('20260001','Seller123!');self.buyer.login('20260002','Buyer123!')
    def tearDown(self):
        """测试结束：关闭服务、停线程、删除临时数据库。"""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()
    def ok(self,c,path,d):
        """断言请求返回 200 并返回响应体，减少重复代码。
        """
        code,out=c.req(path,d);self.assertEqual(code,200,out);return out
    def order(self,pid=1):
        """用买家对指定商品下单，返回订单 id。
        """
        self.ok(self.buyer,'order',{'id':pid});return self.buyer.req('orders')[1]['orders'][0]['id']
    def accept(self, oid):
        """卖家接单，交付时间设为当前时间 +1 小时（满足 72 小时内）。"""
        self.ok(self.seller, 'accept', {'id': oid, 'place': '图书馆门口',
                                        'meeting': time.strftime('%Y-%m-%dT%H:%M', time.localtime(now() + 3600))})
    def complete(self,oid):
        """走完交付流程：接单 → 买家确认 → 卖家确认，使订单变为已完成。
        """
        self.accept(oid);self.ok(self.buyer,'confirm',{'id':oid});self.assertEqual(self.buyer.req('orders')[1]['orders'][0]['status'],'待交付');self.ok(self.seller,'confirm',{'id':oid})
    def test_name_requires_text(self):
        for name in (None, [], 1234):
            status, _ = self.other.req('register', {'student':'20265555','name':name,'password':'Test123!'})
            self.assertEqual(status, 400)
        with connect(self.db) as db:
            self.assertIsNone(db.execute("SELECT id FROM users WHERE student='20265555'").fetchone())

    def test_price_requires_finite_value(self):
        product = {'title':'课程教材','description':'课程教材整套出售可以线下交付','price':'Infinity'}
        self.assertEqual(self.seller.req('publish', product)[0], 400)
        with connect(self.db) as db:
            self.assertIsNone(db.execute("SELECT id FROM products WHERE title='课程教材'").fetchone())

    def test_approval_requires_boolean(self):
        product = {'title':'课程教材','description':'课程教材整套出售可以线下交付','price':10,'category':'教材','condition':'九成新'}
        self.ok(self.seller, 'publish', product)
        pid = self.seller.req('mine')[1]['products'][0]['id']
        self.assertEqual(self.admin.req('review_product', {'id':pid,'approve':'false','reason':'资料不符'})[0], 400)
        self.assertEqual(self.seller.req('product?id='+str(pid))[1]['product']['status'], '待审核')

    def test_repeated_favorite_intent(self):
        for desired in (True, True, False, False):
            self.ok(self.buyer, 'favorite', {'id':1,'desired':desired})
            self.assertEqual(self.buyer.req('product?id=1')[1]['favorite'], desired)
            self.assertEqual(len(self.buyer.req('mine')[1]['favorites']), int(desired))

    def test_notice_failure_retries_once(self):
        with connect(self.db) as db:
            db.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON notices BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.order()
        self.assertEqual(self.seller.req('mine')[1]['notices'], [])
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT status FROM products WHERE id=1').fetchone()[0], '已预订')
            self.assertEqual(db.execute('SELECT count(*) FROM notice_queue').fetchone()[0], 1)
            db.execute('DROP TRIGGER fail_notice')
        for _ in range(2):
            self.assertEqual(len(self.seller.req('mine')[1]['notices']), 1)

    def test_reject_withdrawal_during_dispute(self):
        oid = self.order()
        self.accept(oid)
        self.ok(self.buyer, 'confirm', {'id':oid})
        self.ok(self.buyer, 'withdraw', {'id':oid,'body':'申请重新检查'})
        wid = self.admin.req('admin')[1]['withdrawals'][0]['id']
        self.ok(self.seller, 'dispute', {'id':oid,'body':'交付存在争议'})
        before = self.buyer.req('orders')[1]['orders'][0]
        self.ok(self.admin, 'handle_withdrawal', {'id':wid,'approve':False,'reason':'转入争议流程'})
        after = self.buyer.req('orders')[1]['orders'][0]
        self.assertEqual(after['status'], '争议处理中')
        self.assertEqual(after['buyer_confirm'], before['buyer_confirm'])
        self.assertEqual(self.admin.req('admin')[1]['withdrawals'][0]['status'], '已驳回')

    def test_registration_authentication_and_csrf(self):
        """注册、登录、CSRF 与登出。

        覆盖:
            * 新学号注册并登录；
            * 不带 CSRF 令牌的写请求被拒（400）；
            * 普通用户调用管理员接口被拒（400）；
            * 登出后再访问需要登录的接口被拒（400）。
        """
        self.ok(self.other,'register',{'student':'20269999','name':'测试用户','password':'Test123!'})
        self.other.login('20269999','Test123!');self.assertEqual(self.other.req('order',{'id':1},csrf=False)[0],400)
        self.assertEqual(self.other.req('ban',{'id':2,'reason':'越权测试'})[0],400)
        self.ok(self.other,'logout',{});self.assertEqual(self.other.req('orders')[0],400)
    def test_publish_review_search_edit(self):
        """发布 → 审核 → 搜索 → 下架 → 编辑的完整链路。

        覆盖:
            * 新发布的商品处于待审核，不会出现在集市；
            * 管理员审核通过后才可被搜索到；
            * 价格按“分”存储（12.50 元 → 1250）；
            * 在售商品必须先下架才能编辑，编辑后重新回到待审核。
        """
        self.ok(self.seller,'publish',{'title':'测试教材','description':'这是一份完整的课程测试教材描述','category':'教材书籍','condition':'九成新','price':'12.50'})
        pid=self.seller.req('mine')[1]['products'][0]['id'];self.assertNotIn(pid,[p['id'] for p in self.buyer.req('products')[1]['products']])
        self.ok(self.admin,'review_product',{'id':pid,'approve':True,'reason':'内容符合发布要求'})
        found=self.buyer.req('products?q=测试教材&min=12&max=13')[1]['products'];self.assertEqual(found[0]['price'],1250)
        self.ok(self.seller,'shelve',{'id':pid});self.ok(self.seller,'edit',{'id':pid,'title':'修订教材','description':'修改之后的一份完整的教材描述','category':'教材书籍','condition':'八成新','price':'13'})
        self.assertEqual(self.seller.req('product?id='+str(pid))[1]['product']['status'],'待审核')
    def test_favorites_and_private_messages(self):
        """收藏与私有咨询。

        覆盖:
            * 收藏后个人中心能看到，重复操作切换为取消收藏；
            * 买家发起咨询、卖家回复；
            * 第三方看不到别人商品的会话内容。
        """
        self.ok(self.buyer,'favorite',{'id':1});self.assertEqual(len(self.buyer.req('mine')[1]['favorites']),1)
        self.ok(self.buyer,'message',{'id':1,'receiver':2,'body':'请问可以校内交接吗？'});self.ok(self.seller,'message',{'id':1,'receiver':3,'body':'可以在图书馆交接'})
        self.ok(self.other,'register',{'student':'20268888','name':'其他同学','password':'Test123!'});self.other.login('20268888','Test123!')
        self.assertEqual(len(self.other.req('messages?id=1')[1]['messages']),0);self.assertEqual(len(self.buyer.req('messages?id=1')[1]['messages']),2)
    def test_concurrent_order_only_one_wins(self):
        """并发下单：两人同时抢同一商品，只允许一单成功。

        覆盖:
            * 两个请求中恰好一个返回 200；
            * 数据库中该商品只产生一条订单（唯一索引兜底）。
        """
        self.ok(self.other,'register',{'student':'20267777','name':'并发买家','password':'Test123!'});self.other.login('20267777','Test123!')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda c:c.req('order',{'id':1})[0],[self.buyer,self.other]))
        self.assertEqual(results.count(200),1)
        with connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM orders WHERE product=1').fetchone()[0],1)
    def test_cancel_releases_product_and_single_confirmation_blocks(self):
        """取消释放占用；单方确认后禁止普通取消。

        覆盖:
            * 买家取消待交付订单后，商品回到“在售”；
            * 买家已确认后再取消会被拒（400），应走争议或撤回流程。
        """
        oid=self.order();self.accept(oid);self.ok(self.buyer,'cancel',{'id':oid});self.assertEqual(self.buyer.req('product?id=1')[1]['product']['status'],'在售')
        oid=self.order();self.accept(oid);self.ok(self.buyer,'confirm',{'id':oid});self.assertEqual(self.buyer.req('cancel',{'id':oid})[0],400)
    def test_confirm_idempotent_review_history(self):
        """确认幂等、评价可改并留历史、统计口径。

        覆盖:
            * 重复调用 confirm 不会重复变更订单；
            * 第二次评价会把旧评分写入 review_history（保留 4 分那条）；
            * 后台统计只计入已完成订单（completed=1）。
        """
        oid=self.order();self.complete(oid);self.ok(self.seller,'confirm',{'id':oid})
        self.ok(self.buyer,'review',{'id':oid,'rating':4,'body':'商品很好'});self.ok(self.buyer,'review',{'id':oid,'rating':5,'body':'追加好评'})
        self.assertEqual(self.buyer.req('review_history?id='+str(oid))[1]['history'][0]['rating'],4)
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
    def test_withdraw_approval_statistics_and_resolution(self):
        """撤回批准的影响，以及争议裁定后的恢复。

        覆盖:
            * 批准撤回后订单转争议处理中：完成数归零、评价不再公开展示；
            * confirmation_history 留下被撤销的确认记录；
            * 管理员认定完成后：完成数与评价恢复。
        """
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':5,'body':'完好'})
        self.ok(self.buyer,'withdraw',{'id':oid,'body':'申请核查交付情况'});self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
        wid=self.admin.req('admin')[1]['withdrawals'][0]['id'];self.ok(self.admin,'handle_withdrawal',{'id':wid,'approve':True,'reason':'证据需要进一步核查'})
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],0);self.assertEqual(self.buyer.req('product?id=1')[1]['reviews'],[])
        self.assertEqual(len(self.buyer.req('review_history?id='+str(oid))[1]['confirmations']),1)
        self.ok(self.admin,'resolve',{'id':oid,'complete':True,'reason':'核对卖方有效确认以及线下交接照片，认定已经交付'})
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1);self.assertEqual(len(self.buyer.req('product?id=1')[1]['reviews']),1)
    def test_withdraw_timeout_and_rejection(self):
        """撤回的两个边界：超时与驳回。

        覆盖:
            * 把确认时间改到 7300 秒前，申请撤回被拒（超过 2 小时）；
            * 管理员驳回撤回申请后，订单保持已完成，统计不变。
        """
        oid=self.order();self.complete(oid)
        with connect(self.db) as db:db.execute('UPDATE orders SET buyer_confirm=? WHERE id=?',(now()-7300,oid))
        self.assertEqual(self.buyer.req('withdraw',{'id':oid,'body':'超时申请'})[0],400)
        self.ok(self.seller,'withdraw',{'id':oid,'body':'申请重新核查'});wid=self.admin.req('admin')[1]['withdrawals'][0]['id'];self.ok(self.admin,'handle_withdrawal',{'id':wid,'approve':False,'reason':'交付证据充分，驳回申请'});self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
    def test_freeze_and_ban_keep_existing_fulfillment(self):
        """冻结商品、封禁账号不影响已有订单履约。

        覆盖:
            * 商品冻结后不能再下单（400）；
            * 卖家被封禁后，已有订单仍可继续确认交付；
            * 解冻不会把已售出的商品改回在售。
        """
        oid=self.order();self.ok(self.admin,'freeze',{'id':1,'reason':'内容存在违规问题'});self.ok(self.admin,'ban',{'id':3,'reason':'违规处置','days':7});self.accept(oid)
        self.assertEqual(self.buyer.req('order',{'id':2})[0],400);self.ok(self.buyer,'confirm',{'id':oid});self.ok(self.seller,'confirm',{'id':oid})
        self.ok(self.admin,'unfreeze',{'id':1,'reason':'整改内容审核通过'});self.assertEqual(self.buyer.req('product?id=1')[1]['product']['status'],'已售出')
    def test_report_and_admin_audit_csv(self):
        """举报去重、后台处理、审计与导出权限。

        覆盖:
            * 同一用户重复举报同一商品返回 409；
            * 管理员处理举报后审计有记录；
            * 只有管理员能导出 CSV，普通用户返回 400。
        """
        self.ok(self.buyer,'report',{'id':1,'body':'物品内容需要核查'});self.assertEqual(self.buyer.req('report',{'id':1,'body':'重复举报'})[0],409)
        rid=self.admin.req('admin')[1]['reports'][0]['id'];self.ok(self.admin,'handle_report',{'id':rid,'reason':'内容已经核对并通知发布者'})
        self.assertTrue(self.admin.req('admin')[1]['audit']);self.assertEqual(self.admin.req('export')[0],200);self.assertEqual(self.buyer.req('export')[0],400)
    def test_transaction_rollback_on_invalid_and_own_order(self):
        """非法请求不留下半成品（事务回滚）。

        覆盖:
            * 卖家不能购买自己的商品；
            * 参数不合法的发布被拒；
            * 以上失败后 orders 表仍为空。
        """
        self.assertEqual(self.seller.req('order',{'id':1})[0],400)
        self.assertEqual(self.buyer.req('publish',{'title':'坏','description':'短','price':-1})[0],400)
        with connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0],0)
    def test_dispute_close_and_image_rejection(self):
        """争议关闭后的商品状态，以及图片校验。

        覆盖:
            * 待交付订单发起争议，管理员认定关闭后商品回到“在售”；
            * 伪造内容（非 PNG/JPEG 文件头）的商品图片被拒（400）。
        """
        oid=self.order();self.accept(oid);self.ok(self.buyer,'dispute',{'id':oid,'body':'商品未实际交接'})
        self.ok(self.admin,'resolve',{'id':oid,'complete':False,'reason':'核查双方记录，确认商品没有完成交接'})
        self.assertEqual(self.buyer.req('product?id=1')[1]['product']['status'],'在售')
        self.assertEqual(self.seller.req('publish',{'title':'测试图片','description':'这是一份含有错误图片的商品描述','category':'其他闲置','condition':'全新','price':10,'image':'data:image/png;base64,aGVsbG8='})[0],400)
    def test_export_csv_bom_header_and_formula_escape(self):
        """导出 CSV 的内容：BOM、表头、金额换算与公式注入转义。

        覆盖:
            * 以 = 开头的商品名被加单引号，Excel 不会当公式执行；
            * 金额由“分”换算成“元”（12.50 元 → 12.5）；
            * 文件以 UTF-8 BOM 开头，Excel 打开中文不乱码；
            * start/end 区间内没有已完成订单时只返回表头。
        """
        self.ok(self.seller,'publish',{'title':'=1+1 恶意标题','description':'用于验证导出转义的商品描述文字','category':'教材书籍','condition':'九成新','price':'12.50'})
        pid=self.seller.req('mine')[1]['products'][0]['id']
        self.ok(self.admin,'review_product',{'id':pid,'approve':True,'reason':'内容符合发布要求'})
        oid=self.order(pid);self.complete(oid)
        status,raw=self.admin.req('export')
        self.assertEqual(status,200);self.assertTrue(raw.startswith('\ufeff'.encode()))
        lines=raw.decode('utf-8-sig').splitlines()
        self.assertEqual(lines[0],'订单编号,商品,约定金额（元）,完成时间')
        row=list(csv.reader([lines[1]]))[0]
        self.assertEqual(int(row[0]),oid);self.assertEqual(row[1],"'=1+1 恶意标题")
        self.assertEqual(float(row[2]),12.5);self.assertTrue(row[3])
        empty=self.admin.req('export?start=0&end=1')[1]
        self.assertEqual(len(empty.decode('utf-8-sig').strip().splitlines()),1)
    def test_login_lockout_after_five_failures(self):
        """同一 IP + 学号连续 5 次失败后锁定，其他学号不受影响。

        覆盖:
            * 前 4 次失败提示“账号或密码错误”；
            * 第 5 次失败后进入锁定，即使密码正确也提示“失败次数过多”；
            * 换一个学号仍能正常登录。
        """
        client=Client(self.server.server_port)
        for _ in range(4):
            code,out=client.req('login',{'student':'20260002','password':'Wrong123!'})
            self.assertEqual(code,400);self.assertIn('账号或密码错误',out['error'])
        self.assertEqual(client.req('login',{'student':'20260002','password':'Wrong123!'})[0],400)
        code,out=client.req('login',{'student':'20260002','password':'Buyer123!'})
        self.assertEqual(code,400);self.assertIn('失败次数过多',out['error'])
        self.assertEqual(client.req('login',{'student':'20260001','password':'Seller123!'})[0],200)
    def test_security_headers_and_session_cookie(self):
        """登录 Cookie 与会话响应的安全属性。

        覆盖: HttpOnly、SameSite=Strict、Path、Max-Age 四个 Cookie 属性，
        以及 X-Content-Type-Options、Cache-Control、CSP 三个响应头。
        """
        client=Client(self.server.server_port)
        client.login('20260002','Buyer123!')
        cookie=client.headers.get('Set-Cookie','')
        for flag in ('HttpOnly','SameSite=Strict','Path=/','Max-Age=86400'):
            self.assertIn(flag,cookie)
        self.assertEqual(client.req('me')[0],200)
        self.assertEqual(client.headers.get('X-Content-Type-Options'),'nosniff')
        self.assertEqual(client.headers.get('Cache-Control'),'no-store')
        self.assertIn("default-src 'self'",client.headers.get('Content-Security-Policy',''))
    def test_static_pages_and_path_whitelist(self):
        """静态页面可访问；白名单之外的路径一律 400，避免读到项目其他文件。"""
        port=self.server.server_port
        for path,ctype in [('/','text/html'),('/app.js','javascript'),('/style.css','text/css')]:
            status,body,headers=raw_get(port,path)
            self.assertEqual(status,200,path);self.assertIn(ctype,headers.get('Content-Type',''));self.assertTrue(body)
        for path in ('/favicon.ico','/schema.sql','/api.py','/../api.py'):
            status,body,_=raw_get(port,path)
            self.assertEqual(status,400,path);self.assertIn('页面不存在',json.loads(body)['error'])
    def test_request_guards(self):
        """写请求入口校验：Content-Type、来源、体积、JSON 结构与未知动作。"""
        port=self.server.server_port;json_head={'Content-Type':'application/json'}
        status,out=raw_post(port,'/api/login',b'{"student":"admin"}',{'Content-Type':'text/plain'})
        self.assertEqual((status,out['error']),(400,'请求必须为JSON'))
        status,out=raw_post(port,'/api/login',b'{"student":"admin"}',dict(json_head,Origin='http://evil.example'))
        self.assertEqual((status,out['error']),(400,'请求来源不匹配'))
        status,out=raw_post(port,'/api/login',b'',json_head)
        self.assertEqual((status,out['error']),(400,'请求内容过大'))
        self.assertEqual(raw_post(port,'/api/login',b'{not json',json_head)[0],400)
        status,out=raw_post(port,'/api/login',b'[]',json_head)
        self.assertEqual((status,out['error']),(400,'JSON对象格式无效'))
        status,out=raw_post(port,'/login',b'{}',json_head)
        self.assertEqual((status,out['error']),(400,'接口不存在'))
        status,out=self.buyer.req('no_such_action',{})
        self.assertEqual((status,out['error']),(400,'未知操作'))
    def test_products_filters_sort_and_seller_rating(self):
        """集市列表：关键词、分类、价格区间、价格排序与卖家评分。"""
        listed=self.buyer.req('products')[1]['products']
        self.assertEqual(len(listed),6);self.assertTrue(all(p['rating'] is None for p in listed))
        prices=[p['price'] for p in self.buyer.req('products?sort=price')[1]['products']]
        self.assertEqual(prices,sorted(prices))
        self.assertTrue(all(p['category']=='数码电器' for p in self.buyer.req('products?category=数码电器')[1]['products']))
        ranged=self.buyer.req('products?min=18&max=90')[1]['products']
        self.assertTrue(ranged and all(1800<=p['price']<=9000 for p in ranged))
        found=self.buyer.req('products?q=交接')[1]['products']
        self.assertTrue(found and all('交接' in (p['title']+p['description']) for p in found))
        self.assertEqual(self.buyer.req('products?min=abc')[0],400)
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':5,'body':'完好'})
        self.assertEqual(self.buyer.req('products')[1]['products'][0]['rating'],5.0)
    def test_frozen_and_sold_products_visibility(self):
        """商品可见性：已售出仍可看详情，冻结与下架只有管理员和卖家可见。"""
        oid=self.order();self.complete(oid)
        self.assertEqual(self.buyer.req('product?id=1')[1]['product']['status'],'已售出')
        self.assertNotIn(1,[p['id'] for p in self.buyer.req('products')[1]['products']])
        self.ok(self.admin,'freeze',{'id':2,'reason':'内容存在违规问题'})
        self.assertEqual(self.buyer.req('product?id=2')[0],400)
        self.assertEqual(self.seller.req('product?id=2')[0],200)
        self.assertEqual(self.admin.req('product?id=2')[0],200)
        self.ok(self.seller,'shelve',{'id':3})
        self.assertEqual(self.buyer.req('product?id=3')[0],400)
        self.assertEqual(self.buyer.req('product?id=999')[0],400)
    def test_concurrent_duplicate_order_stays_single(self):
        """同一买家并发重复下单：只生成一单，其余返回“订单已存在”而不是 409。

        覆盖: BEGIN IMMEDIATE 把并发请求串行化，幂等分支先于“商品已占用”判断，
        因此重复下单是成功响应而不是错误。
        """
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results=list(pool.map(lambda _:self.buyer.req('order',{'id':1}),range(4)))
        self.assertTrue(all(code==200 for code,_ in results),[code for code,_ in results])
        self.assertEqual([out.get('message') for _,out in results].count('订单已存在'),3)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orders WHERE product=1').fetchone()[0],1)
    def test_route_permission_order_for_unknown_paths(self):
        """路由顺序：公开 → 登录 → 管理员，最后才是“接口不存在”。"""
        guest=Client(self.server.server_port)
        self.assertEqual(guest.req('products')[0],200)
        self.assertEqual(guest.req('mine')[0],400)
        code,out=guest.req('no_such_path')
        self.assertEqual((code,out['error']),(400,'请先登录'))
        code,out=self.buyer.req('no_such_path')
        self.assertEqual((code,out['error']),(400,'需要管理员权限'))
        code,out=self.admin.req('no_such_path')
        self.assertEqual((code,out['error']),(400,'接口不存在'))
        self.assertEqual(self.buyer.req('admin')[0],400)
        self.assertEqual(self.admin.req('admin')[0],200)
if __name__=='__main__':unittest.main(verbosity=2)
