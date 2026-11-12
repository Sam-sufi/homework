import concurrent.futures,http.client,json,sqlite3,sys,tempfile,threading,time,unittest
from urllib.parse import quote
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_server,connect,init_db,now

PNG='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII='
PNG_URI='data:image/png;base64,'+PNG

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
    def order(self,pid=1,buyer=None):
        buyer=buyer or self.buyer;self.ok(buyer,'order',{'id':pid});return buyer.req('orders')[1]['orders'][0]['id']
    def accept(self,oid):self.ok(self.seller,'accept',{'id':oid,'place':'图书馆门口','meeting':time.strftime('%Y-%m-%dT%H:%M',time.localtime(now()+3600))})
    def complete(self,oid):
        self.accept(oid);self.ok(self.buyer,'confirm',{'id':oid});self.assertEqual(self.buyer.req('orders')[1]['orders'][0]['status'],'待交付');self.ok(self.seller,'confirm',{'id':oid})
    def review(self,oid,rating='4.6',body='商品与描述一致，交接顺利',tags=None,images=None):
        data={'id':oid,'rating':rating,'body':body}
        if tags is not None:data['tags']=tags
        if images is not None:data['images']=images
        return self.buyer.req('review',data)
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
    def test_review_once_rating_and_averages(self):
        oid=self.order();self.complete(oid)
        self.assertEqual(self.seller.req('review',{'id':oid,'rating':'4.6','body':'卖家不能评价'})[0],400)
        self.assertEqual(self.other.req('review',{'id':oid,'rating':'4.6','body':'非当事人不能评价'})[0],400)
        for bad in ['4.55','0.9','5.1']:
            self.assertEqual(self.review(oid,rating=bad)[0],400,bad)
        self.ok(self.buyer,'review',{'id':oid,'rating':'4.6','body':'商品与描述一致，交接顺利'})
        self.assertEqual(self.review(oid)[0],400)
        detail=self.buyer.req('product?id=1')[1];self.assertEqual(detail['product']['product_rating'],4.6);self.assertEqual(detail['product']['product_rating_count'],1);self.assertEqual(detail['product']['seller_rating'],4.6);self.assertEqual(detail['product']['seller_rating_count'],1)
        self.assertEqual(detail['reviews'][0]['rating'],4.6);self.assertEqual(detail['reviews'][0]['body'],'商品与描述一致，交接顺利')
        listed=self.buyer.req('products')[1]['products'];item=[p for p in listed if p['id']==2][0];self.assertEqual(item['rating'],4.6);self.assertEqual(item['rating_count'],1)
        order=self.buyer.req('orders')[1]['orders'][0];self.assertTrue(order['reviewed']);self.assertFalse(order['review_closed']);self.assertEqual(order['review']['rating'],4.6)
        with connect(self.db) as db:self.assertEqual(db.execute('SELECT rating FROM reviews WHERE order_id=?',(oid,)).fetchone()[0],46)
    def test_review_tags_images_and_tag_filter(self):
        oid=self.order();self.complete(oid)
        self.assertEqual(self.review(oid,tags=['不存在'])[0],400)
        self.assertEqual(self.review(oid,tags=['物美价廉','外观好看','描述相符','沟通顺畅'])[0],400)
        self.assertEqual(self.review(oid,images=[PNG_URI]*4)[0],400)
        self.assertEqual(self.review(oid,images=['data:image/png;base64,aGVsbG8='])[0],400)
        self.ok(self.buyer,'review',{'id':oid,'rating':'4.6','body':'带图带标签的评价','tags':['物美价廉','外观好看','沟通顺畅'],'images':[PNG_URI]})
        detail=self.buyer.req('product?id=1')[1];review=detail['reviews'][0];self.assertEqual(set(review['tags']),{'物美价廉','外观好看','沟通顺畅'});self.assertEqual(len(review['images']),1)
        counts={x['tag']:x['count'] for x in detail['tag_counts']};self.assertEqual(counts['物美价廉'],1);self.assertEqual(counts['沟通顺畅'],1)
        image_id=review['images'][0]['id'];status,raw=self.other.req('review_image?id='+str(image_id));self.assertEqual(status,200);self.assertTrue(raw.startswith(b'\x89PNG'))
    def test_review_append_only_text(self):
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':'4.0','body':'初始评价内容','tags':['物美价廉']})
        self.ok(self.buyer,'append_review',{'id':oid,'body':'补'*100})
        self.assertEqual(self.buyer.req('append_review',{'id':oid,'body':'补'*101})[0],400)
        self.assertEqual(self.seller.req('append_review',{'id':oid,'body':'卖家不能追加'})[0],400)
        self.ok(self.buyer,'append_review',{'id':oid,'body':'第二次补充'})
        detail=self.buyer.req('product?id=1')[1];review=detail['reviews'][0];self.assertTrue(review['body'].startswith('初始评价内容\n'));self.assertIn('补'*100,review['body']);self.assertIn('第二次补充',review['body']);self.assertEqual(review['rating'],4.0);self.assertEqual(review['tags'],['物美价廉'])
    def test_seller_reply_edit_delete(self):
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':'4.6','body':'不错的交易'})
        self.assertEqual(self.buyer.req('reply_review',{'id':oid,'body':'买家不能回复'})[0],400)
        self.ok(self.seller,'reply_review',{'id':oid,'body':'感谢支持，欢迎下次再来'})
        detail=self.buyer.req('product?id=1')[1];self.assertEqual(detail['reviews'][0]['reply']['body'],'感谢支持，欢迎下次再来')
        self.ok(self.seller,'reply_review',{'id':oid,'body':'感谢支持，已修改回复'})
        self.assertEqual(self.buyer.req('product?id=1')[1]['reviews'][0]['reply']['body'],'感谢支持，已修改回复')
        self.ok(self.seller,'delete_reply',{'id':oid});self.assertIsNone(self.buyer.req('product?id=1')[1]['reviews'][0]['reply'])
        with connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM review_replies').fetchone()[0],0)
    def test_seller_page_aggregates_reviews_and_sold_items(self):
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':'4.6','body':'卖家主页可见的评价','tags':['物美价廉','沟通顺畅']})
        data=self.other.req('seller?id=2')[1];self.assertEqual(data['seller']['name'],'林同学');self.assertNotIn('student',data['seller'])
        self.assertEqual(data['stats']['rating'],4.6);self.assertEqual(data['stats']['review_count'],1);self.assertEqual(data['stats']['completed_count'],1);self.assertEqual(data['stats']['sold_count'],1)
        self.assertEqual(data['items']['sold'][0]['id'],1);self.assertTrue(any(p['id']==2 for p in data['items']['active']))
        review=data['reviews'][0];self.assertEqual(review['product_id'],1);self.assertEqual(review['product_title'],'高等数学教材 全套');self.assertEqual(review['product_public'],1);self.assertEqual(set(review['tags']),{'物美价廉','沟通顺畅'})
        self.assertEqual({x['tag']:x['count'] for x in data['tag_counts']},{'物美价廉':1,'沟通顺畅':1})
        sold=self.other.req('product?id=1')[1];self.assertEqual(sold['product']['status'],'已售出');self.assertEqual(len(sold['reviews']),1)
        review_id=self.admin.req('admin')[1]['reviews'][0]['id'];self.ok(self.admin,'hide_review',{'id':review_id,'reason':'卖家主页隐藏测试'})
        hidden=self.other.req('seller?id=2')[1];self.assertEqual(hidden['reviews'],[]);self.assertIsNone(hidden['stats']['rating']);self.assertEqual(hidden['stats']['review_count'],0);self.assertEqual(hidden['stats']['completed_count'],1);self.assertEqual(hidden['stats']['sold_count'],1)
        self.ok(self.admin,'unhide_review',{'id':review_id,'reason':'恢复卖家主页评价'})
        self.assertEqual(len(self.other.req('seller?id=2')[1]['reviews']),1)
    def test_admin_hide_and_unhide_review(self):
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':'4.6','body':'包含图片的评价','images':[PNG_URI]})
        detail=self.buyer.req('product?id=1')[1];image_id=detail['reviews'][0]['images'][0]['id'];review_id=detail['reviews'][0]['id']
        with connect(self.db) as db:before=db.execute('SELECT count(*) FROM notices WHERE user_id=3').fetchone()[0]
        self.ok(self.admin,'hide_review',{'id':review_id,'reason':'内容需要进一步核查'})
        detail=self.buyer.req('product?id=1')[1];self.assertEqual(detail['reviews'],[]);self.assertIsNone(detail['product']['product_rating']);self.assertIsNone(detail['product']['seller_rating'])
        order=self.buyer.req('orders')[1]['orders'][0];self.assertIsNone(order['review']);self.assertTrue(order['reviewed'])
        self.assertEqual(self.other.req('review_image?id='+str(image_id))[0],400);self.assertEqual(self.admin.req('review_image?id='+str(image_id))[0],200)
        with connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM notices WHERE user_id=3').fetchone()[0],before)
        admin_review=self.admin.req('admin')[1]['reviews'][0];self.assertEqual(admin_review['hidden'],1);self.assertEqual(admin_review['hidden_reason'],'内容需要进一步核查')
        self.ok(self.admin,'unhide_review',{'id':review_id,'reason':'复核后恢复显示'})
        detail=self.buyer.req('product?id=1')[1];self.assertEqual(detail['product']['product_rating'],4.6);self.assertEqual(len(detail['reviews']),1)
    def test_admin_hard_delete_review_and_tombstone(self):
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':'4.6','body':'将被删除的评价','tags':['物美价廉'],'images':[PNG_URI]})
        self.ok(self.buyer,'append_review',{'id':oid,'body':'补充内容'});self.ok(self.seller,'reply_review',{'id':oid,'body':'卖家回复'})
        review_id=self.admin.req('admin')[1]['reviews'][0]['id'];self.ok(self.admin,'delete_review',{'id':review_id,'reason':'严重违规，硬删除'})
        with connect(self.db) as db:
            for table in ['reviews','review_tags','review_images','review_replies']:
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0],0,table)
            self.assertEqual(db.execute('SELECT count(*) FROM review_deleted WHERE order_id=?',(oid,)).fetchone()[0],1)
        self.assertEqual(self.review(oid)[0],400)
        detail=self.buyer.req('product?id=1')[1];self.assertEqual(detail['reviews'],[]);self.assertIsNone(detail['product']['product_rating'])
        self.assertEqual(self.admin.req('admin')[1]['audit'][0]['action'],'delete_review')
    def test_withdraw_approval_statistics_and_review_visibility(self):
        oid=self.order();self.complete(oid);self.ok(self.buyer,'review',{'id':oid,'rating':'5.0','body':'完好'})
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
        self.ok(self.buyer,'withdraw',{'id':oid,'body':'申请核查交付情况'});self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],1)
        wid=self.admin.req('admin')[1]['withdrawals'][0]['id'];self.ok(self.admin,'handle_withdrawal',{'id':wid,'approve':True,'reason':'证据需要进一步核查'})
        self.assertEqual(self.admin.req('admin')[1]['stats']['completed'],0);self.assertEqual(self.buyer.req('product?id=1')[1]['reviews'],[])
        self.assertEqual(len(self.buyer.req('order_history?id='+str(oid))[1]['confirmations']),1)
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
    def test_legacy_database_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'legacy.db';conn=sqlite3.connect(path)
            conn.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, student TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'student',banned_until INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE products(id INTEGER PRIMARY KEY, seller INTEGER, title TEXT, description TEXT, category TEXT, price INTEGER, condition TEXT, status TEXT, frozen INTEGER DEFAULT 0, image TEXT DEFAULT '', reason TEXT DEFAULT '', created INTEGER);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, product INTEGER, buyer INTEGER, seller INTEGER, title TEXT, price INTEGER, status TEXT, buyer_confirm INTEGER DEFAULT 0, seller_confirm INTEGER DEFAULT 0, place TEXT DEFAULT '', meeting TEXT DEFAULT '', note TEXT DEFAULT '', created INTEGER, completed INTEGER DEFAULT 0);
            CREATE TABLE reviews(id INTEGER PRIMARY KEY, order_id INTEGER, author INTEGER, rating INTEGER CHECK(rating BETWEEN 1 AND 5), body TEXT NOT NULL, updated INTEGER NOT NULL, UNIQUE(order_id,author));
            CREATE TABLE review_history(id INTEGER PRIMARY KEY, review_id INTEGER, rating INTEGER, body TEXT, created INTEGER);
            INSERT INTO users VALUES(1,'20260001','旧卖家','x','student',0);
            INSERT INTO users VALUES(2,'20260002','旧买家','x','student',0);
            INSERT INTO products VALUES(1,1,'旧商品','旧商品描述内容','其他闲置',100,'八成新','在售',0,'','',1);
            INSERT INTO orders VALUES(1,1,2,1,'旧商品',100,'已完成',1,1,'图书馆','2026-11-06T18:00','',1,2);
            INSERT INTO reviews VALUES(1,1,2,5,'旧评价',2);
            INSERT INTO review_history VALUES(1,1,4,'更早版本',1);
            """);conn.commit();conn.close()
            init_db(str(path));init_db(str(path))
            with connect(path) as db:
                review=db.execute('SELECT * FROM reviews').fetchone();self.assertEqual(review['rating'],50);self.assertEqual(review['created'],2);self.assertEqual(review['hidden'],0)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='review_history'").fetchone())
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='review_tags'").fetchone())
if __name__=='__main__':unittest.main(verbosity=2)
