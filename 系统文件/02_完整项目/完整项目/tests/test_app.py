import concurrent.futures,http.client,json,sys,tempfile,threading,time,unittest
from urllib.parse import quote
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_server,connect,now

class Client:
    def __init__(self,port):self.port=port;self.cookie='';self.csrf=''
    def req(self,path,data=None,csrf=True):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=15)
        headers={'Cookie':self.cookie}
        if data is not None:headers.update({'Content-Type':'application/json','X-CSRF-Token':self.csrf if csrf else ''})
        conn.request('POST' if data is not None else 'GET','/api/'+quote(path,safe='/?=&'),json.dumps(data) if data is not None else None,headers)
        res=conn.getresponse();raw=res.read();status=res.status
        if res.getheader('Set-Cookie'):self.cookie=res.getheader('Set-Cookie').split(';')[0]
        ctype=res.getheader('Content-Type','');conn.close();out=json.loads(raw) if 'json' in ctype else raw
        if isinstance(out,dict) and 'csrf' in out:self.csrf=out['csrf']
        return status,out
    def login(self,user,pw):assert self.req('login',{'student':user,'password':pw})[0]==200

class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/'test.db';self.server=create_server(port=0,db_path=self.db);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.admin=Client(self.server.server_port);self.seller=Client(self.server.server_port);self.buyer=Client(self.server.server_port);self.other=Client(self.server.server_port)
        self.admin.login('admin','Admin123!');self.seller.login('20260001','Seller123!');self.buyer.login('20260002','Buyer123!')
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def ok(self,c,path,d):
        code,out=c.req(path,d);self.assertEqual(code,200,out);return out
    def order(self,pid=1):
        self.ok(self.buyer,'order',{'id':pid});return self.buyer.req('orders')[1]['orders'][0]['id']
    def accept(self,oid):self.ok(self.seller,'accept',{'id':oid,'place':'图书馆门口','meeting':time.strftime('%Y-%m-%dT%H:%M',time.localtime(now()+3600))})
    def complete(self,oid):
        self.accept(oid);self.ok(self.buyer,'confirm',{'id':oid});self.assertEqual(self.buyer.req('orders')[1]['orders'][0]['status'],'待交付');self.ok(self.seller,'confirm',{'id':oid})
    def test_registration_authentication_and_csrf(self):
        self.ok(self.other,'register',{'student':'20269999','name':'测试用户','password':'Test123!'})
        self.other.login('20269999','Test123!');self.assertEqual(self.other.req('order',{'id':1},csrf=False)[0],400)
        self.assertEqual(self.other.req('ban',{'id':2,'reason':'越权测试'})[0],400)
        self.ok(self.other,'logout',{});self.assertEqual(self.other.req('orders')[0],400)
    def test_publish_review_search_edit(self):
        self.ok(self.seller,'publish',{'title':'测试教材','description':'这是一份完整的课程测试教材描述','category':'教材书籍','condition':'九成新','price':'12.50'})
        pid=self.seller.req('mine')[1]['products'][0]['id'];self.assertNotIn(pid,[p['id'] for p in self.buyer.req('products')[1]['products']])
        self.ok(self.admin,'review_product',{'id':pid,'approve':True,'reason':'内容符合发布要求'})
        found=self.buyer.req('products?q=测试教材&min=12&max=13')[1]['products'];self.assertEqual(found[0]['price'],1250)
        self.ok(self.seller,'shelve',{'id':pid});self.ok(self.seller,'edit',{'id':pid,'title':'修订教材','description':'修改之后的一份完整的教材描述','category':'教材书籍','condition':'八成新','price':'13'})
        self.assertEqual(self.seller.req('product?id='+str(pid))[1]['product']['status'],'待审核')
    def test_favorites_and_private_messages(self):
        self.ok(self.buyer,'favorite',{'id':1});self.assertEqual(len(self.buyer.req('mine')[1]['favorites']),1)
        self.ok(self.buyer,'message',{'id':1,'receiver':2,'body':'请问可以校内交接吗？'});self.ok(self.seller,'message',{'id':1,'receiver':3,'body':'可以在图书馆交接'})
        self.ok(self.other,'register',{'student':'20268888','name':'其他同学','password':'Test123!'});self.other.login('20268888','Test123!')
        self.assertEqual(len(self.other.req('messages?id=1')[1]['messages']),0);self.assertEqual(len(self.buyer.req('messages?id=1')[1]['messages']),2)
    def test_concurrent_order_only_one_wins(self):
        self.ok(self.other,'register',{'student':'20267777','name':'并发买家','password':'Test123!'});self.other.login('20267777','Test123!')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda c:c.req('order',{'id':1})[0],[self.buyer,self.other]))
        self.assertEqual(results.count(200),1)
        with connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM orders WHERE product=1').fetchone()[0],1)
    def test_cancel_releases_product_and_single_confirmation_blocks(self):
        oid=self.order();self.accept(oid);self.ok(self.buyer,'cancel',{'id':oid});self.assertEqual(self.buyer.req('product?id=1')[1]['product']['status'],'在售')
        oid=self.order();self.accept(oid);self.ok(self.buyer,'confirm',{'id':oid});self.assertEqual(self.buyer.req('cancel',{'id':oid})[0],400)
    def test_confirm_idempotent_review_history(self):
        oid=self.order();self.complete(oid);self.ok(self.seller,'confirm',{'id':oid})
        self.ok(self.buyer,'review',{'id':oid,'rating':4,'body':'商品很好'});self.ok(self.buyer,'review',{'id':oid,'rating':5,'body':'追加好评'})
        self.assertEqual(self.buyer.req('review_history?id='+str(oid))[1]['history'][0]['rating'],4)
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
    def test_withdraw_approval_statistics_and_resolution(self):
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':5,'body':'完好'})
        self.ok(self.buyer,'withdraw',{'id':oid,'body':'申请核查交付情况'});self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
        wid=self.admin.req('admin')[1]['withdrawals'][0]['id'];self.ok(self.admin,'handle_withdrawal',{'id':wid,'approve':True,'reason':'证据需要进一步核查'})
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],0);self.assertEqual(self.buyer.req('product?id=1')[1]['reviews'],[])
        self.assertEqual(len(self.buyer.req('review_history?id='+str(oid))[1]['confirmations']),1)
        self.ok(self.admin,'resolve',{'id':oid,'complete':True,'reason':'核对卖方有效确认以及线下交接照片，认定已经交付'})
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1);self.assertEqual(len(self.buyer.req('product?id=1')[1]['reviews']),1)
    def test_withdraw_timeout_and_rejection(self):
        oid=self.order();self.complete(oid)
        with connect(self.db) as db:db.execute('UPDATE orders SET buyer_confirm=? WHERE id=?',(now()-7300,oid))
        self.assertEqual(self.buyer.req('withdraw',{'id':oid,'body':'超时申请'})[0],400)
        self.ok(self.seller,'withdraw',{'id':oid,'body':'申请重新核查'});wid=self.admin.req('admin')[1]['withdrawals'][0]['id'];self.ok(self.admin,'handle_withdrawal',{'id':wid,'approve':False,'reason':'交付证据充分，驳回申请'});self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
    def test_freeze_and_ban_keep_existing_fulfillment(self):
        oid=self.order();self.ok(self.admin,'freeze',{'id':1,'reason':'内容存在违规问题'});self.ok(self.admin,'ban',{'id':3,'reason':'违规处置','days':7});self.accept(oid)
        self.assertEqual(self.buyer.req('order',{'id':2})[0],400);self.ok(self.buyer,'confirm',{'id':oid});self.ok(self.seller,'confirm',{'id':oid})
        self.ok(self.admin,'unfreeze',{'id':1,'reason':'整改内容审核通过'});self.assertEqual(self.buyer.req('product?id=1')[1]['product']['status'],'已售出')
    def test_report_and_admin_audit_csv(self):
        self.ok(self.buyer,'report',{'id':1,'body':'物品内容需要核查'});self.assertEqual(self.buyer.req('report',{'id':1,'body':'重复举报'})[0],409)
        rid=self.admin.req('admin')[1]['reports'][0]['id'];self.ok(self.admin,'handle_report',{'id':rid,'reason':'内容已经核对并通知发布者'})
        self.assertTrue(self.admin.req('admin')[1]['audit']);self.assertEqual(self.admin.req('export')[0],200);self.assertEqual(self.buyer.req('export')[0],400)
    def test_transaction_rollback_on_invalid_and_own_order(self):
        self.assertEqual(self.seller.req('order',{'id':1})[0],400)
        self.assertEqual(self.buyer.req('publish',{'title':'坏','description':'短','price':-1})[0],400)
        with connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0],0)
    def test_dispute_close_and_image_rejection(self):
        oid=self.order();self.accept(oid);self.ok(self.buyer,'dispute',{'id':oid,'body':'商品未实际交接'})
        self.ok(self.admin,'resolve',{'id':oid,'complete':False,'reason':'核查双方记录，确认商品没有完成交接'})
        self.assertEqual(self.buyer.req('product?id=1')[1]['product']['status'],'在售')
        self.assertEqual(self.seller.req('publish',{'title':'测试图片','description':'这是一份含有错误图片的商品描述','category':'其他闲置','condition':'全新','price':10,'image':'data:image/png;base64,aGVsbG8='})[0],400)
if __name__=='__main__':unittest.main(verbosity=2)
