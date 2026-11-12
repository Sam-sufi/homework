"""校园拾集：Python 标准库应用，不需要 pip 安装依赖。"""
import base64,csv,hashlib,hmac,io,json,mimetypes,os,re,secrets,sqlite3,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlsplit,parse_qs

ROOT=Path(__file__).resolve().parent
def now():return int(time.time())
def password_hash(value,salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256',value.encode(),salt.encode(),160000).hex()
def valid_password(raw,stored):return hmac.compare_digest(password_hash(raw,stored.split(':')[0]),stored)
class BusinessError(Exception):pass
def need(condition,message):
    if not condition:raise BusinessError(message)
def text(data,key,lo=1,hi=1000):
    value=str(data.get(key,'')).strip();need(lo<=len(value)<=hi,f'{key}长度应为{lo}—{hi}字');return value
class Database(sqlite3.Connection):
    def __exit__(self,*args):
        try:return super().__exit__(*args)
        finally:self.close()
def connect(path):
    db=sqlite3.connect(path,timeout=15,factory=Database);db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON');return db
def init_db(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with connect(path) as db:
        db.executescript((ROOT/'schema.sql').read_text(encoding='utf-8'))
        if db.execute('SELECT count(*) FROM users').fetchone()[0]:return
        for student,name,pw,role in [('admin','管理员','Admin123!','admin'),('20260001','林同学','Seller123!','student'),('20260002','陈同学','Buyer123!','student')]:
            db.execute('INSERT INTO users(student,name,password,role) VALUES(?,?,?,?)',(student,name,password_hash(pw),role))
        samples=[('高等数学教材 全套','教材保存完好，内页无缺页，适合大一学习使用，可在图书馆交接。','教材书籍',3500,'八成新'),('护眼台灯','毕业闲置台灯，三档色温均可正常使用，附电源线，支持当面检查。','数码电器',6800,'九成新'),('校园通勤自行车','骑行正常，车胎刚更换，适合校园内通勤，停车处当面交接。','运动出行',18000,'八成新'),('桌面收纳盒','宿舍整理好物，收纳文具和小物件，整套出售，已经清洁。','宿舍生活',1800,'九成新'),('无线键盘','键位正常，无明显磨损，附接收器，可在校内试用后交付。','数码电器',8900,'九成新'),('羽毛球拍','两支球拍及收纳袋，入门练习适用，握把完好，可查看实物。','运动出行',4500,'八成新')]
        for title,desc,cat,price,condition in samples:db.execute('INSERT INTO products(seller,title,description,category,price,condition,status,created) VALUES(2,?,?,?,?,?,\'在售\',?)',(title,desc,cat,price,condition,now()))
def rows(db,sql,args=()):return [dict(x) for x in db.execute(sql,args)]
def notify(db,uid,body):db.execute('INSERT INTO notices(user_id,body,created) VALUES(?,?,?)',(uid,body,now()))
def stats(db,start=0,end=4102444800):
    data=dict(db.execute("SELECT count(*) completed,coalesce(sum(price),0) amount FROM orders WHERE status='已完成' AND completed BETWEEN ? AND ?",(start,end)).fetchone())
    data.update(products=db.execute('SELECT count(*) FROM products WHERE created BETWEEN ? AND ?',(start,end)).fetchone()[0],reports=db.execute("SELECT count(*) FROM reports WHERE status='待处理'").fetchone()[0]);return data
def public_user(u):return {k:u[k] for k in ['id','student','name','role','banned_until']}
def act(db,user,action,d):
    uid=user['id'];admin=user['role']=='admin'
    new_actions={'publish','favorite','message','order','submit','edit'}
    need(action not in new_actions or user['banned_until']<=now(),'账号处于封禁期，不允许新增业务')
    def get(table,key='id'):
        obj=db.execute(f'SELECT * FROM {table} WHERE id=?',(int(d.get(key,0)),)).fetchone();need(obj is not None,'对象不存在');return obj
    def administrator():need(admin,'需要管理员权限')
    def own_order():
        obj=get('orders');need(uid in [obj['buyer'],obj['seller']],'不能操作他人订单');return obj
    if action in ['publish','edit']:
        title=text(d,'title',2,50);desc=text(d,'description',10,1000);price=d.get('price')
        try:price=round(float(price)*100)
        except (ValueError,TypeError):raise BusinessError('价格必须为数字')
        need(0<price<=100000000,'价格必须大于0且不超过100万元');image=str(d.get('image',''))
        need(len(image)<=7500000,'图片不超过5MB')
        if image:
            need(image.startswith(('data:image/png;base64,','data:image/jpeg;base64,')),'仅支持PNG或JPEG图片')
            try:raw=base64.b64decode(image.split(',',1)[1],validate=True)
            except Exception:raise BusinessError('图片格式无效')
            need(len(raw)<=5*1024*1024 and (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw.startswith(b'\xff\xd8\xff')),'图片内容无效或超过5MB')
        if action=='edit':
            obj=get('products');need(obj['seller']==uid and obj['status'] in ['已下架','已驳回'] and not obj['frozen'],'只能编辑本人未冻结的下架或驳回商品')
            db.execute("UPDATE products SET title=?,description=?,category=?,price=?,condition=?,status='待审核',image=?,reason='' WHERE id=?",(title,desc,text(d,'category',1,30),price,text(d,'condition',1,20),image or obj['image'],obj['id']))
        else:db.execute('INSERT INTO products(seller,title,description,category,price,condition,status,image,created) VALUES(?,?,?,?,?,?,?,?,?)',(uid,title,desc,text(d,'category',1,30),price,text(d,'condition',1,20),'待审核',image,now()))
    elif action=='favorite':
        product=get('products');need(product['status'] in ['在售','已预订'] and not product['frozen'],'商品当前不可收藏')
        if db.execute('SELECT 1 FROM favorites WHERE user_id=? AND product=?',(uid,product['id'])).fetchone():db.execute('DELETE FROM favorites WHERE user_id=? AND product=?',(uid,product['id']))
        else:db.execute('INSERT INTO favorites VALUES(?,?)',(uid,product['id']))
    elif action=='message':
        product=get('products');receiver=int(d.get('receiver',product['seller']));need(receiver!=uid,'不能向自己发消息')
        need(uid==product['seller'] or receiver==product['seller'],'咨询必须关联商品卖家')
        need(db.execute('SELECT 1 FROM users WHERE id=?',(receiver,)).fetchone(),'接收人不存在')
        if uid==product['seller']:need(db.execute('SELECT 1 FROM messages WHERE product=? AND sender=? AND receiver=?',(product['id'],receiver,uid)).fetchone(),'只能回复已有咨询')
        else:need(not product['frozen'] and product['status'] in ['在售','已预订'],'商品不接受新咨询')
        db.execute('INSERT INTO messages(product,sender,receiver,body,created) VALUES(?,?,?,?,?)',(product['id'],uid,receiver,text(d,'body',1,1000),now()));notify(db,receiver,'收到新的商品咨询')
    elif action=='order':
        product=get('products');need(product['seller']!=uid,'不能购买自己的商品')
        existing=db.execute("SELECT id FROM orders WHERE product=? AND buyer=? AND status IN ('待接单','待交付','争议处理中')",(product['id'],uid)).fetchone()
        if existing:return {'ok':True,'message':'订单已存在','order_id':existing['id']}
        need(not product['frozen'] and product['status']=='在售','商品已被占用或不可交易')
        need(db.execute('SELECT banned_until FROM users WHERE id=?',(product['seller'],)).fetchone()[0]<=now(),'卖家账号处于封禁期')
        db.execute("INSERT INTO orders(product,buyer,seller,title,price,status,created) VALUES(?,?,?,?,?,'待接单',?)",(product['id'],uid,product['seller'],product['title'],product['price'],now()));db.execute("UPDATE products SET status='已预订' WHERE id=?",(product['id'],));notify(db,product['seller'],'有新订单，请接单或拒绝')
    elif action in ['accept','cancel','confirm','withdraw','dispute','review']:
        order=own_order();oid=order['id']
        if action=='accept':
            if uid==order['seller'] and order['status']=='待交付':return {'ok':True,'message':'订单已经接单'}
            need(uid==order['seller'] and order['status']=='待接单','只有卖家可接待接单订单');place=text(d,'place',2,100);meeting=text(d,'meeting',1,50)
            try:stamp=int(time.mktime(time.strptime(meeting,'%Y-%m-%dT%H:%M')))
            except ValueError:raise BusinessError('交付时间格式无效')
            need(now()<=stamp<=order['created']+72*3600,'交付时间须在当前时间之后且下单后72小时内');db.execute("UPDATE orders SET status='待交付',place=?,meeting=? WHERE id=?",(place,meeting,oid))
        elif action=='cancel':
            if order['status']=='已关闭':return {'ok':True,'message':'订单已经关闭'}
            need(user['banned_until']<=now(),'封禁期不允许普通取消，请联系管理员')
            need(order['status'] in ['待接单','待交付'] and not order['buyer_confirm'] and not order['seller_confirm'],'已有确认或争议，不允许普通取消')
            db.execute("UPDATE orders SET status='已关闭' WHERE id=?",(oid,));db.execute("UPDATE products SET status='在售' WHERE id=?",(order['product'],))
        elif action=='confirm':
            need(order['status'] in ['待交付','已完成'],'订单不可确认交付');field='buyer_confirm' if uid==order['buyer'] else 'seller_confirm'
            if not order[field] and order['status']=='待交付':db.execute(f'UPDATE orders SET {field}=? WHERE id=?',(now(),oid))
            fresh=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if fresh['buyer_confirm'] and fresh['seller_confirm'] and fresh['status']!='已完成':db.execute("UPDATE orders SET status='已完成',completed=? WHERE id=?",(now(),oid));db.execute("UPDATE products SET status='已售出' WHERE id=?",(order['product'],))
        elif action=='withdraw':
            stamp=order['buyer_confirm'] if uid==order['buyer'] else order['seller_confirm']
            need(order['status'] in ['待交付','已完成'] and stamp and now()-stamp<=7200,'须在本人确认后2小时内申请')
            need(not db.execute("SELECT 1 FROM withdrawals WHERE order_id=? AND status='待审核'",(oid,)).fetchone(),'已有撤回申请正在审核');db.execute('INSERT INTO withdrawals(order_id,author,body,created) VALUES(?,?,?,?)',(oid,uid,text(d,'body',2,500),now()))
        elif action=='dispute':
            need(order['status']=='待交付','只有待交付订单可直接发起争议，完成订单请先申请撤回');db.execute("UPDATE orders SET status='争议处理中',note=? WHERE id=?",(text(d,'body',2,500),oid))
        else:
            need(order['status']=='已完成','仅已完成订单可评价');rating=int(d.get('rating',0));need(1<=rating<=5,'评分范围1至5');body=text(d,'body',1,200)
            existing=db.execute('SELECT * FROM reviews WHERE order_id=? AND author=?',(oid,uid)).fetchone()
            if existing:db.execute('INSERT INTO review_history(review_id,rating,body,created) VALUES(?,?,?,?)',(existing['id'],existing['rating'],existing['body'],existing['updated']))
            db.execute('INSERT INTO reviews(order_id,author,rating,body,updated) VALUES(?,?,?,?,?) ON CONFLICT(order_id,author) DO UPDATE SET rating=excluded.rating,body=excluded.body,updated=excluded.updated',(oid,uid,rating,body,now()))
        notify(db,order['seller'] if uid==order['buyer'] else order['buyer'],'订单 #'+str(oid)+' 已更新：'+action)
    elif action=='report':
        product=get('products');db.execute('INSERT INTO reports(author,product,body,created) VALUES(?,?,?,?)',(uid,product['id'],text(d,'body',2,500),now()))
    elif action in ['review_product','freeze','unfreeze','shelve']:
        product=get('products')
        if action=='shelve':need(uid==product['seller'] and product['status']=='在售','只能下架本人在售商品');db.execute("UPDATE products SET status='已下架' WHERE id=?",(product['id'],))
        else:
            administrator()
            if action=='review_product':
                need(product['status']=='待审核','商品不是待审核状态');status='在售' if d.get('approve') else '已驳回';reason=text(d,'reason',2,500);db.execute('UPDATE products SET status=?,reason=? WHERE id=?',(status,reason,product['id']))
            elif action=='freeze':db.execute('UPDATE products SET frozen=1,reason=? WHERE id=?',(text(d,'reason',2,500),product['id']))
            else:db.execute('UPDATE products SET frozen=0,reason=? WHERE id=?',(text(d,'reason',2,500),product['id']))
            notify(db,product['seller'],'商品处理结果已更新')
    elif action in ['ban','unban','handle_report','handle_withdrawal','resolve']:
        administrator();reason=text(d,'reason',2,500)
        if action in ['ban','unban']:
            obj=get('users');need(obj['role']!='admin','不能封禁管理员');days=int(d.get('days',7));need(days>=7,'封禁至少7天');db.execute('UPDATE users SET banned_until=? WHERE id=?',(now()+days*86400 if action=='ban' else 0,obj['id']));notify(db,obj['id'],reason)
        elif action=='handle_report':obj=get('reports');db.execute("UPDATE reports SET status='已处理',result=? WHERE id=?",(reason,obj['id']));notify(db,obj['author'],'举报结果：'+reason)
        elif action=='handle_withdrawal':
            obj=get('withdrawals');need(obj['status']=='待审核','申请已处理');order=db.execute('SELECT * FROM orders WHERE id=?',(obj['order_id'],)).fetchone();need(order['status'] in ['待交付','已完成'],'订单状态已改变')
            if d.get('approve'):
                field='buyer_confirm' if obj['author']==order['buyer'] else 'seller_confirm';db.execute('INSERT INTO confirmation_history(order_id,user_id,confirmed,revoked,reason) VALUES(?,?,?,?,?)',(order['id'],obj['author'],order[field],now(),reason));db.execute(f"UPDATE orders SET status='争议处理中',{field}=0,note=? WHERE id=?",('撤回获批：'+reason,order['id']));db.execute("UPDATE products SET status='已预订' WHERE id=?",(order['product'],))
            db.execute('UPDATE withdrawals SET status=?,result=? WHERE id=?',('已批准' if d.get('approve') else '已驳回',reason,obj['id']));notify(db,obj['author'],'撤回处理：'+reason)
        else:
            order=get('orders');need(order['status']=='争议处理中','订单不在争议中')
            if d.get('complete'):need((order['buyer_confirm'] or order['seller_confirm']) and len(reason)>=10,'认定完成需至少一方有效确认，并填写不少于10字的交付证据说明')
            db.execute('UPDATE orders SET status=?,note=?,completed=? WHERE id=?',('已完成' if d.get('complete') else '已关闭',reason,now() if d.get('complete') else 0,order['id']));db.execute('UPDATE products SET status=? WHERE id=?',('已售出' if d.get('complete') else '在售',order['product']))
            notify(db,order['buyer'],'争议裁定：'+reason);notify(db,order['seller'],'争议裁定：'+reason)
    elif action=='read_notices':db.execute('UPDATE notices SET seen=1 WHERE user_id=?',(uid,))
    else:raise BusinessError('未知操作')
    db.execute('INSERT INTO audit(actor,action,target,detail,created) VALUES(?,?,?,?,?)',(uid,action,int(d.get('id',0)),json.dumps({k:v for k,v in d.items() if k not in ['image','password']},ensure_ascii=False)[:2000],now()))
    return {'ok':True,'message':'操作成功'}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):pass
    def send(self,data,status=200,cookie=None,ctype='application/json; charset=utf-8'):
        body=json.dumps(data,ensure_ascii=False).encode() if isinstance(data,(dict,list)) else data
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(body)
    def user(self,db):
        try:cookie=SimpleCookie(self.headers.get('Cookie',''));token=cookie['sid'].value
        except (KeyError,ValueError):return None
        return db.execute('SELECT users.*,sessions.csrf FROM users JOIN sessions ON sessions.user_id=users.id WHERE sessions.token=? AND expires>?',(token,now())).fetchone()
    def do_GET(self):
        try:
            url=urlsplit(self.path);q={k:v[0] for k,v in parse_qs(url.query).items()}
            if not url.path.startswith('/api/'):
                names={'/':'index.html','/app.js':'app.js','/style.css':'style.css'};need(url.path in names,'页面不存在');path=ROOT/'static'/names[url.path];return self.send(path.read_bytes(),ctype=mimetypes.guess_type(path)[0]+'; charset=utf-8')
            with connect(self.server.db_path) as db:
                user=self.user(db)
                if url.path=='/api/me':return self.send({'user':public_user(user) if user else None,'csrf':user['csrf'] if user else ''})
                if url.path=='/api/products':
                    sql="SELECT p.*,u.name seller_name,(SELECT avg(r.rating) FROM reviews r JOIN orders o ON o.id=r.order_id WHERE o.seller=p.seller AND r.author=o.buyer AND o.status='已完成') rating FROM products p JOIN users u ON u.id=p.seller WHERE p.status IN ('在售','已预订') AND p.frozen=0";args=[]
                    if q.get('q'):sql+=' AND (p.title LIKE ? OR p.description LIKE ?)';args+=['%'+q['q']+'%']*2
                    if q.get('category'):sql+=' AND p.category=?';args.append(q['category'])
                    for key,op in [('min','>='),('max','<=')]:
                        if q.get(key):sql+=' AND p.price'+op+'?';args.append(round(float(q[key])*100))
                    sql+=' ORDER BY '+{'price':'p.price ASC','rating':'rating DESC'}.get(q.get('sort'),'p.created DESC,p.id DESC')
                    return self.send({'products':rows(db,sql,args)})
                if url.path=='/api/product':
                    pid=int(q.get('id',0));obj=db.execute('SELECT p.*,u.name seller_name FROM products p JOIN users u ON u.id=p.seller WHERE p.id=?',(pid,)).fetchone();need(obj is not None,'商品不存在');need((not obj['frozen'] and obj['status'] in ['在售','已预订','已售出']) or (user and (user['role']=='admin' or user['id']==obj['seller'])),'商品不可见')
                    return self.send({'product':dict(obj),'reviews':rows(db,"SELECT r.*,u.name FROM reviews r JOIN users u ON u.id=r.author JOIN orders o ON o.id=r.order_id WHERE o.product=? AND o.status='已完成' ORDER BY r.updated DESC",(pid,))})
                need(user is not None,'请先登录')
                if url.path=='/api/mine':return self.send({'products':rows(db,'SELECT * FROM products WHERE seller=? ORDER BY id DESC',(user['id'],)),'favorites':rows(db,'SELECT p.* FROM favorites f JOIN products p ON p.id=f.product WHERE f.user_id=?',(user['id'],)),'notices':rows(db,'SELECT * FROM notices WHERE user_id=? ORDER BY id DESC LIMIT 100',(user['id'],))})
                if url.path=='/api/orders':return self.send({'orders':rows(db,'SELECT o.*,b.name buyer_name,s.name seller_name,p.frozen FROM orders o JOIN users b ON b.id=o.buyer JOIN users s ON s.id=o.seller JOIN products p ON p.id=o.product WHERE o.buyer=? OR o.seller=? ORDER BY o.id DESC',(user['id'],user['id'])),'withdrawals':rows(db,'SELECT w.* FROM withdrawals w JOIN orders o ON o.id=w.order_id WHERE o.buyer=? OR o.seller=? ORDER BY w.id DESC',(user['id'],user['id']))})
                if url.path=='/api/messages':
                    pid=int(q.get('id',0));return self.send({'messages':rows(db,'SELECT m.*,s.name sender_name,r.name receiver_name FROM messages m JOIN users s ON s.id=m.sender JOIN users r ON r.id=m.receiver WHERE m.product=? AND (m.sender=? OR m.receiver=?) ORDER BY m.id',(pid,user['id'],user['id']))})
                if url.path=='/api/review_history':
                    oid=int(q.get('id',0));return self.send({'history':rows(db,'SELECT h.* FROM review_history h JOIN reviews r ON r.id=h.review_id WHERE r.order_id=? AND r.author=? ORDER BY h.id DESC',(oid,user['id'])),'confirmations':rows(db,'SELECT h.* FROM confirmation_history h JOIN orders o ON o.id=h.order_id WHERE o.id=? AND (o.buyer=? OR o.seller=?)',(oid,user['id'],user['id']))})
                need(user['role']=='admin','需要管理员权限')
                if url.path=='/api/admin':return self.send({'products':rows(db,'SELECT p.*,u.name seller_name FROM products p JOIN users u ON u.id=p.seller ORDER BY p.id DESC'),'users':rows(db,'SELECT id,student,name,role,banned_until FROM users'),'reports':rows(db,'SELECT * FROM reports ORDER BY id DESC'),'withdrawals':rows(db,'SELECT * FROM withdrawals ORDER BY id DESC'),'disputes':rows(db,"SELECT * FROM orders WHERE status='争议处理中'"),'audit':rows(db,'SELECT a.*,u.name FROM audit a JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT 100'),'stats':stats(db,int(q.get('start',0)),int(q.get('end',4102444800)))})
                if url.path=='/api/export':
                    buf=io.StringIO();writer=csv.writer(buf);writer.writerow(['订单编号','商品','约定金额（元）','完成时间'])
                    completed_orders=db.execute("SELECT * FROM orders WHERE status='已完成' AND completed BETWEEN ? AND ?",(int(q.get('start',0)),int(q.get('end',4102444800)))).fetchall()
                    for r in completed_orders:
                        safe_title=("'"+r['title']) if r['title'].lstrip().startswith(('=','+','-','@')) else r['title']
                        writer.writerow([r['id'],safe_title,r['price']/100,time.strftime('%Y-%m-%d %H:%M',time.localtime(r['completed']))])
                    return self.send(('\ufeff'+buf.getvalue()).encode(),ctype='text/csv; charset=utf-8')
                raise BusinessError('接口不存在')
        except (BusinessError,ValueError,TypeError) as e:self.send({'error':str(e)},400)
        except Exception as e:print('GET error:',repr(e));self.send({'error':'服务器处理失败'},500)
    def do_POST(self):
        try:
            need(self.headers.get('Content-Type','').split(';')[0]=='application/json','请求必须为JSON')
            origin=self.headers.get('Origin');need(not origin or urlsplit(origin).netloc==self.headers.get('Host'),'请求来源不匹配')
            size=int(self.headers.get('Content-Length','0'));need(0<size<=8*1024*1024,'请求内容过大');d=json.loads(self.rfile.read(size));need(isinstance(d,dict),'JSON对象格式无效')
            action=urlsplit(self.path).path.removeprefix('/api/');need(self.path.startswith('/api/'),'接口不存在')
            with connect(self.server.db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                if action=='register':
                    student=text(d,'student',8,12);need(student.isdigit(),'学号须为8至12位数字');pw=text(d,'password',8,100);need(re.search('[A-Z]',pw) and re.search('[a-z]',pw) and re.search('[0-9]',pw),'密码需包含大小写字母和数字')
                    db.execute('INSERT INTO users(student,name,password) VALUES(?,?,?)',(student,text(d,'name',2,20),password_hash(pw)));db.commit();return self.send({'ok':True,'message':'注册成功，请使用学号登录'})
                if action=='login':
                    student=text(d,'student',1,30);obj=db.execute('SELECT * FROM users WHERE student=?',(student,)).fetchone()
                    ipkey=self.client_address[0]+':'+student;attempt=self.server.login_attempts.get(ipkey,(0,0));need(attempt[1]<now(),'失败次数过多，请5分钟后重试')
                    if not(obj and valid_password(str(d.get('password','')),obj['password'])):
                        count=attempt[0]+1;self.server.login_attempts[ipkey]=(count,now()+300 if count>=5 else 0);raise BusinessError('账号或密码错误')
                    self.server.login_attempts.pop(ipkey,None);token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24);db.execute('DELETE FROM sessions WHERE expires<?',(now(),));db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(token,obj['id'],csrf,now()+86400));db.commit();return self.send({'ok':True,'user':public_user(obj),'csrf':csrf},cookie=f'sid={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400')
                user=self.user(db);need(user is not None,'请先登录');need(hmac.compare_digest(self.headers.get('X-CSRF-Token',''),user['csrf']),'会话校验失败，请刷新后重试')
                if action=='logout':
                    cookie=SimpleCookie(self.headers.get('Cookie',''));db.execute('DELETE FROM sessions WHERE token=?',(cookie['sid'].value,));db.commit();return self.send({'ok':True},cookie='sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                result=act(db,user,action,d);db.commit();return self.send(result)
        except sqlite3.IntegrityError:self.send({'error':'该操作已存在、已被占用或账号重复，请刷新后重试'},409)
        except (BusinessError,ValueError,TypeError,json.JSONDecodeError) as e:self.send({'error':str(e)},400)
        except Exception as e:print('POST error:',repr(e));self.send({'error':'服务器处理失败'},500)
def create_server(host='127.0.0.1',port=8000,db_path=None):
    path=str(db_path or ROOT/'data/campus.db');init_db(path);server=ThreadingHTTPServer((host,port),Handler);server.db_path=path;server.login_attempts={};return server
