/* 无框架前端；所有写操作通过服务端权限、事务及CSRF验证。 */
let me=null,csrf='',timer=null,editing=0,currentProduct=null,currentReviews=[],currentTagCounts=[],reviewTag='',sellerData=null,sellerTab='active',sellerReviewTag='',adminTab='products',adminStart='',adminEnd='',adminReviewStatus='all',adminReviewTag='';
const main=document.querySelector('#main');
const REVIEW_TAGS=['物美价廉','外观好看','描述相符','成色很新','描述不符','成色一般','沟通顺畅','交付准时','沟通不畅','交付迟到'];
const NEGATIVE_TAGS=new Set(['描述不符','成色一般','沟通不畅','交付迟到']);
const categories=['教材书籍','数码电器','宿舍生活','运动出行','其他闲置'];
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=x=>(Number(x)/100).toFixed(2);
const date=x=>x?new Date(x*1000).toLocaleString('zh-CN'):'—';
function toast(s){const el=document.querySelector('#toast');el.textContent=s;el.className='show';setTimeout(()=>el.className='',4500)}
async function api(path,data){const r=await fetch('/api/'+path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-CSRF-Token':csrf}:{},body:data?JSON.stringify(data):undefined});const j=await r.json();if(!r.ok)throw new Error(j.error||'请求失败');return j}
async function session(){const d=await api('me');me=d.user;csrf=d.csrf;document.querySelector('#account').innerHTML=me?`<span>${esc(me.name)}</span><button class="secondary" data-action="logout">退出</button>`:'<a class="button secondary" href="#login">登录 / 注册</a>';document.querySelector('#adminlink').hidden=me?.role!=='admin'}
function auth(){if(!me){location.hash='login';return false}return true}
const icon=cat=>`<svg aria-hidden="true" viewBox="0 0 120 120" fill="none" stroke="currentColor" stroke-width="3">${cat==='教材书籍'?'<path fill="#fff9e9" d="M27 19h55v83H27z"/><path d="M35 19v83M46 40h25M46 50h22M46 79h18"/>':cat==='运动出行'?'<circle cx="30" cy="83" r="21"/><circle cx="92" cy="83" r="21"/><path d="m30 83 25-37 17 37H30l51-38 11 38M47 44h18M77 30h14"/>':cat==='数码电器'?'<path fill="#f9fbf8" d="M27 31h66v49H27z"/><path d="M60 80v17M38 98h44M39 43h43M39 52h26"/>':'<path fill="#faf9f1" d="m22 40 38-15 38 15v52l-38 14-38-14z"/><path d="m22 40 38 14 38-14M60 54v52M42 33l38 15"/>'}</svg>`;
function art(p){return `<div class="thumb t${p.id%4}">${p.image?`<img src="${esc(p.image)}" alt="${esc(p.title)}" loading="lazy">`:icon(p.category)}</div>`}
function ratingLine(rating,count,compact=false){
    if(rating===null||rating===undefined||Number.isNaN(Number(rating)))return '<span class="muted">暂无评分</span>';
    const n=Number(rating);
    return `<span class="ratingline"><progress class="ratingbar" max="5" value="${n}" aria-label="${n.toFixed(1)} 分"></progress><strong>${n.toFixed(1)}</strong>${count!==undefined&&count!==null?`<small>${compact?'('+count+')':'（'+count+' 条评价）'}</small>`:''}</span>`;
}
function tagBadge(tag){
    const negative=NEGATIVE_TAGS.has(tag);
    return `<span class="tag ${negative?'neg':''}">${esc(tag)}</span>`;
}
function reviewTagChips(counts,selected,action='filter_tag'){
    if(!counts||!counts.length)return '';
    return `<div class="tagfilter"><button type="button" class="tag ${selected?'':'active'}" data-action="${action}" data-tag="">全部</button>${counts.map(x=>`<button type="button" class="tag ${NEGATIVE_TAGS.has(x.tag)?'neg':''} ${selected===x.tag?'active':''}" data-action="${action}" data-tag="${esc(x.tag)}">${esc(x.tag)} <small>${x.count}</small></button>`).join('')}</div>`;
}
function reviewImages(images){
    if(!images||!images.length)return '';
    return `<div class="review-images">${images.map(img=>`<a href="/api/review_image?id=${img.id}" target="_blank" rel="noopener"><img src="/api/review_image?id=${img.id}" alt="评价图片" loading="lazy"></a>`).join('')}</div>`;
}
function reviewCard(r,product,showActions=true){
    const seller=product?Number(product.seller):0;
    const canAppend=showActions&&me&&me.id===r.author;
    const canReply=showActions&&me&&me.id===seller;
    const updated=r.updated&&r.created&&r.updated>r.created?`<small>最后更新 ${date(r.updated)}</small>`:'';
    const reply=r.reply?`<div class="reply"><div class="reply-head"><strong>卖家回复</strong><small>${date(r.reply.updated||r.reply.created)}</small></div><p>${esc(r.reply.body)}</p></div>`:'';
    const actions=canAppend||canReply?`<div class="review-actions">${canAppend?`<a class="button secondary small" href="#review/${r.order_id}">补充评价</a>`:''}${canReply?`<a class="button secondary small" href="#review/${r.order_id}">${r.reply?'修改回复':'回复评价'}</a>`:''}</div>`:'';
    const context=r.product_title?`<div class="review-context">${r.product_public?`<a href="#detail/${r.product_id}">${esc(r.product_title)}</a>`:`<span>${esc(r.product_title)}</span>`}<small>成交 ${date(r.completed)}</small></div>`:'';
    return `<article class="review">${context}<div class="review-head"><div><strong>${esc(r.author_name||'买家')}</strong><span class="badge">买家</span></div><div class="review-meta">${date(r.created)} ${updated}</div></div><div class="review-rating">${ratingLine(r.rating)}</div><p class="review-body">${esc(r.body)}</p>${reviewImages(r.images)}${r.tags&&r.tags.length?`<div class="taglist">${r.tags.map(tagBadge).join('')}</div>`:''}${reply}${actions}</article>`;
}
function cards(items){
    const list=items||[];
    return list.length?`<div class="grid">${list.map(p=>`<a class="card" href="#detail/${p.id}">${art(p)}<div class="cardbody"><span class="badge ${p.status!=='在售'?'warn':''}">${esc(p.frozen?'违规冻结':p.status)} · ${esc(p.condition)}</span><h3>${esc(p.title)}</h3><div class="price"><small>¥</small> ${money(p.price)}</div><div class="seller"><span><span class="avatar">${esc((p.seller_name||'校')[0])}</span>${esc(p.seller_name||'校园同学')}</span><span class="card-rating">${ratingLine(p.rating,p.rating_count,true)}</span></div></div></a>`).join('')}</div>`:'<div class="empty">这里还没有物品，换个条件看看。</div>';
}
function button(action,label,id,cls='secondary',extra=''){return `<button class="${cls}" data-action="${action}" data-id="${id}" ${extra}>${label}</button>`}

async function market(){
    main.innerHTML=`<section class="hero"><div class="hero-copy"><span class="eyebrow">LESS WASTE, MORE STORIES</span><h1>让闲置，<br>在校园里继续发光。</h1><p>课本、台灯、骑行装备。把用不上的，交给需要的人。</p><a class="button" href="#publish">发布一件闲置 ↗</a></div><div class="hero-art" aria-hidden="true"><div class="book-art">拾</div></div></section><div class="section-title"><h2>逛逛校园闲置</h2><small>当面看货 · 校内交接</small></div><form id="search" class="filter"><label>搜索物品<input name="q" placeholder="搜教材、台灯、自行车…"></label><label>分类<select name="category"><option value="">全部分类</option>${categories.map(c=>`<option>${c}</option>`).join('')}</select></label><label>最低价格<input name="min" type="number" min="0" placeholder="¥ 0"></label><label>最高价格<input name="max" type="number" min="0" placeholder="不限"></label><label>排序<select name="sort"><option value="time">最新发布</option><option value="price">价格从低到高</option><option value="rating">商家评分</option></select></label><button>查找物品</button></form><div id="results"></div>`;
    await search();
}
async function search(){const q=new URLSearchParams(new FormData(document.querySelector('#search')));const d=await api('products?'+q);document.querySelector('#results').innerHTML=cards(d.products)}

async function detail(id){
    currentProduct=null;currentReviews=[];currentTagCounts=[];reviewTag='';
    const d=await api('product?id='+id),p=d.product;currentProduct=p;currentReviews=d.reviews||[];currentTagCounts=d.tag_counts||[];
    const orderButton=me?.id!==p.seller?(p.status==='在售'&&!p.frozen?button('order','下单并占用',id,''):''):p.status==='在售'?button('shelve','下架后编辑',id):['已下架','已驳回'].includes(p.status)?button('edit_form','编辑并重新提交',id):'';
    const favoriteButton=['在售','已预订'].includes(p.status)&&!p.frozen?button('favorite','收藏 / 取消收藏',id):'';
    main.innerHTML=`<a href="#market" class="muted">← 返回闲置集市</a><div class="split detail"><div class="panel detail-art">${art(p)}</div><div class="panel"><span class="badge">${esc(p.category)} · ${esc(p.condition)}</span><h1>${esc(p.title)}</h1><div class="price">¥ ${money(p.price)}</div><p class="muted"><a href="#seller/${p.seller}" class="seller-link">${esc(p.seller_name)}</a> · ${esc(p.frozen?'违规冻结':p.status)}</p><div class="rating-summary"><span><small>商品评分</small> ${ratingLine(p.product_rating,p.product_rating_count)}</span><a href="#seller/${p.seller}" class="rating-summary-link"><small>商家评分</small> ${ratingLine(p.seller_rating,p.seller_rating_count)}</a></div><p class="details">${esc(p.description)}</p>${p.reason?`<p>处理说明：${esc(p.reason)}</p>`:''}<div class="actions">${orderButton}${favoriteButton}${button('report','举报',id)}</div><p class="muted">线下结算，约定校内地点交接；下单后卖家接单。</p></div></div><div class="panel"><h2>物品咨询</h2>${me?`<div id="messages"></div><form id="message" class="row" data-id="${id}"><label>接收人ID<input name="receiver" value="${p.seller}" type="number" required></label><label>咨询内容<input name="body" maxlength="1000" required placeholder="咨询成色、交接时间…"></label><button>发送消息</button></form><p class="muted">卖家回复时填写来信者的用户ID。聊天仅参与双方可见，每5秒更新。</p>`:'<a href="#login">登录后与卖家交流</a>'}</div><div class="panel review-panel"><div class="section-title"><h2>交易评价</h2><small>仅买家可评价卖家 · <a href="#seller/${p.seller}">查看卖家全部评价（${p.seller_rating_count||0}）</a></small></div><div id="tagfilter"></div><div id="reviewlist"></div></div>`;
    renderReviews();
    if(me){await messages(id);timer=setInterval(()=>messages(id).catch(()=>{}),5000)}
}
function renderReviews(){
    const filter=document.querySelector('#tagfilter'),list=document.querySelector('#reviewlist');if(!filter||!list)return;
    filter.innerHTML=reviewTagChips(currentTagCounts,reviewTag);
    const filtered=reviewTag?currentReviews.filter(r=>(r.tags||[]).includes(reviewTag)):currentReviews;
    list.innerHTML=filtered.length?filtered.map(r=>reviewCard(r,currentProduct)).join(''):`<div class="empty">${currentReviews.length?'该标签下暂无评价。':'暂时没有已完成交易的评价。'}</div>`;
}
async function messages(id){const d=await api('messages?id='+id);const el=document.querySelector('#messages');if(el)el.innerHTML=d.messages.map(m=>`<div class="msg"><strong>${esc(m.sender_name)}（ID ${m.sender}）→ ${esc(m.receiver_name)}</strong><small>　${date(m.created)}</small><br>${esc(m.body)}</div>`).join('')||'<p class="muted">还没有咨询，打个招呼吧。</p>'}
function sellerItemRow(p,sold){
    const when=sold&&p.completed?` · 售出 ${date(p.completed)}`:'';
    return `<a class="item-row" href="#detail/${p.id}"><div class="item-copy"><span class="badge ${p.status==='已售出'?'warn':''}">${esc(p.status)}</span><strong>${esc(p.title)}</strong><p class="muted">${esc(p.category)} · ${esc(p.condition)}${when}</p></div><span class="item-price">¥ ${money(p.price)}</span></a>`;
}
function sellerItems(items,sold){
    return items&&items.length?`<div class="seller-items">${items.map(p=>sellerItemRow(p,sold)).join('')}</div>`:`<div class="empty">${sold?'还没有已售出的物品。':'当前没有在售物品。'}</div>`;
}
function sellerReviews(){
    if(!sellerData)return '';
    const all=sellerData.reviews||[],filtered=sellerReviewTag?all.filter(r=>(r.tags||[]).includes(sellerReviewTag)):all;
    return `${reviewTagChips(sellerData.tag_counts||[],sellerReviewTag,'seller_filter_tag')}${filtered.length?filtered.map(r=>reviewCard(r,{seller:sellerData.seller.id},false)).join(''):`<div class="empty">${all.length?'该标签下暂无评价。':'卖家还没有收到评价。'}</div>`}`;
}
function renderSellerPage(){
    if(!sellerData)return;
    const s=sellerData.seller,st=sellerData.stats,tabs=[['active','在售物品',st.active_count],['sold','已售出',st.sold_count],['reviews','全部评价',st.review_count]];
    const content=sellerTab==='active'?sellerItems(sellerData.items.active,false):sellerTab==='sold'?sellerItems(sellerData.items.sold,true):sellerReviews();
    main.innerHTML=`<a href="#market" class="muted">← 返回闲置集市</a><section class="panel seller-head"><div class="seller-identity"><div class="seller-avatar">${esc((s.name||'校')[0])}</div><div><span class="eyebrow">CAMPUS SELLER</span><h1>${esc(s.name)}</h1><p class="muted">校园卖家 · ID ${s.id}</p></div></div><div class="seller-stats"><div class="seller-stat"><small>卖家评分</small>${ratingLine(st.rating,st.review_count)}</div><div class="seller-stat"><strong>${st.completed_count}</strong><small>完成订单</small></div><div class="seller-stat"><strong>${st.active_count}</strong><small>在售物品</small></div><div class="seller-stat"><strong>${st.sold_count}</strong><small>已售物品</small></div></div></section><div class="tabs seller-tabs">${tabs.map(([k,label,count])=>`<button class="${sellerTab===k?'active':'secondary'}" data-seller-tab="${k}">${label} <small>${count}</small></button>`).join('')}</div><div id="seller-content">${content}</div>`;
}
async function sellerPage(id){
    sellerData=await api('seller?id='+id);sellerTab='active';sellerReviewTag='';renderSellerPage();
}

function login(){main.innerHTML=`<div class="panel narrow"><span class="eyebrow">WELCOME BACK</span><h1>欢迎来到校园拾集</h1><p class="muted">使用校园学号登录。注册采用本地校园身份演示适配。</p><form id="login" class="form"><label>学号 / 管理账号<input name="student" required autocomplete="username"></label><label>密码<input name="password" type="password" required autocomplete="current-password"></label><button>登录</button></form><div class="tabs"><button class="secondary" data-demo="seller">卖家演示账号</button><button class="secondary" data-demo="buyer">买家演示账号</button><button class="secondary" data-demo="admin">管理员演示账号</button></div><details><summary>没有账号？注册校园用户</summary><form id="register" class="form"><label>学号（8—12位数字）<input name="student" pattern="[0-9]{8,12}" required></label><label>昵称<input name="name" minlength="2" maxlength="20" required></label><label>密码（至少8位，含大小写字母及数字）<input name="password" type="password" minlength="8" required></label><button>注册</button></form></details></div>`}
function publish(){if(!auth())return;editing=0;main.innerHTML=`<div class="panel narrow"><span class="eyebrow">GIVE IT A NEW STORY</span><h1>发布你的闲置</h1><p class="muted">信息越清楚，越容易遇到需要它的人。提交后由管理员审核。</p><form id="publish" class="form"><label>物品名称<input name="title" minlength="2" maxlength="50" required></label><label>分类<select name="category">${categories.map(c=>`<option>${c}</option>`).join('')}</select></label><label>成色<select name="condition"><option>九成新</option><option>八成新</option><option>七成新</option><option>全新未使用</option></select></label><label>价格（元）<input name="price" type="number" min="0.01" step="0.01" max="1000000" required></label><label>物品描述<textarea name="description" minlength="10" maxlength="1000" required placeholder="介绍使用情况、配件与交接区域，至少10字"></textarea></label><label>物品照片（可选，PNG / JPEG，5MB以内）<input id="photo" type="file" accept="image/png,image/jpeg"></label><p class="muted">禁止发布违法违规物品、危险品及学校禁止交易物品。</p><button>提交审核</button></form></div>`}
async function mine(){if(!auth())return;const d=await api('mine');main.innerHTML=`<div class="section-title"><h1>${esc(me.name)} 的校园空间</h1><span class="badge">校园用户 ID ${me.id}</span></div><div class="panel"><h2>站内通知</h2>${button('read_notices','全部已读',0)}${d.notices.map(n=>`<p>${n.seen?'已读':'未读'} · ${esc(n.body)} <small>${date(n.created)}</small></p>`).join('')||'<p class="muted">暂无通知</p>'}</div><div class="section-title"><h2>我发布的物品</h2><a class="button" href="#publish">发布闲置</a></div>${cards(d.products)}<h2>我的收藏</h2>${cards(d.favorites)}`}

async function reviewPage(oid){
    if(!auth())return;const d=await api('orders');const o=d.orders.find(x=>x.id===Number(oid));if(!o)throw new Error('订单不存在');
    const review=o.review;
    let content='';
    if(o.status!=='已完成')content='<div class="empty">订单完成后才能评价。</div>';
    else if(review){
        content=reviewCard(review,{seller:o.seller},false);
        if(me.id===o.buyer)content+=`<form id="appendform" class="form review-append" data-id="${o.id}"><label>补充文字（1—100字，只能追加；评分、图片、标签不可修改）<textarea name="body" minlength="1" maxlength="100" required placeholder="补充使用感受或交接情况"></textarea></label><button>追加文字</button></form>`;
        else if(me.id===o.seller)content+=`<form id="replyform" class="form review-reply" data-id="${o.id}"><label>卖家回复（1—200字）<textarea name="body" minlength="1" maxlength="200" required placeholder="回应买家的评价">${esc(review.reply?.body||'')}</textarea></label><p class="muted">修改会覆盖原回复，不保留历史。</p><div class="actions"><button>${review.reply?'保存回复':'提交回复'}</button>${review.reply?`<button type="button" class="danger" data-action="delete_reply" data-id="${o.id}">删除回复</button>`:''}</div></form>`;
    }else if(o.review_closed)content='<div class="empty">该订单评价已关闭。</div>';
    else if(o.reviewed)content='<div class="empty">评价暂不可用。</div>';
    else if(me.id===o.buyer)content=reviewForm(o);
    else if(me.id===o.seller)content='<div class="empty">买家还没有评价。</div>';
    main.innerHTML=`<a href="#orders" class="muted">← 返回我的交易</a><div class="section-title"><h1>评价订单 #${o.id}</h1><span class="badge">${esc(o.status)}</span></div><p class="muted">${esc(o.title)} · 买家 ${esc(o.buyer_name)} · 卖家 <a href="#seller/${o.seller}" class="seller-link">${esc(o.seller_name)}</a> · ¥ ${money(o.price)}</p><div class="panel">${content}</div>`;
}
function reviewForm(o){
    return `<form id="reviewform" class="form review-form" data-id="${o.id}"><label class="rating-label">评分（1.0—5.0，0.1 分步进）<span class="rating-control"><input id="rating" name="rating" type="range" min="1" max="5" step="0.1" value="5"><output id="ratingval">5.0</output></span></label><label>评价内容（1—200字）<textarea name="body" minlength="1" maxlength="200" required placeholder="描述商品成色、沟通和交接体验"></textarea></label><fieldset class="tag-picker"><legend>标签（最多选 3 个）</legend>${REVIEW_TAGS.map(t=>`<label class="check ${NEGATIVE_TAGS.has(t)?'neg':''}"><input type="checkbox" name="tags" value="${esc(t)}">${esc(t)}</label>`).join('')}</fieldset><label>图片（可选，最多 3 张，每张 5MB，PNG / JPEG）<input name="images" type="file" accept="image/png,image/jpeg" multiple></label><button>提交评价</button></form>`;
}

async function orders(){
    if(!auth())return;const d=await api('orders');
    main.innerHTML=`<div class="section-title"><h1>我的交易</h1><small>下单 → 接单 → 线下交付 → 双方确认 → 买家评价</small></div>${d.orders.length?d.orders.map(o=>{
        const review=o.review,reviewState=review?`<span class="badge">已评价 ${Number(review.rating).toFixed(1)}</span>`:o.review_closed?'<span class="badge warn">评价已关闭</span>':o.reviewed?'<span class="badge warn">评价暂不可用</span>':'';
        const reviewLink=o.status==='已完成'&&me.id===o.buyer&&!review&&!o.reviewed&&!o.review_closed?`<a class="button secondary" href="#review/${o.id}">评价</a>`:'';
        const appendLink=review&&me.id===o.buyer?`<a class="button secondary" href="#review/${o.id}">补充评价</a>`:'';
        const replyLink=review&&me.id===o.seller?`<a class="button secondary" href="#review/${o.id}">${review.reply?'修改回复':'回复评价'}</a>`:'';
        return `<article class="panel order"><div class="section-title"><h3>#${o.id} ${esc(o.title)}</h3><span class="badge">${esc(o.status)}${o.frozen?' · 商品冻结':''}</span></div><p>买家 ${esc(o.buyer_name)} · 卖家 <a href="#seller/${o.seller}" class="seller-link">${esc(o.seller_name)}</a> · ¥ ${money(o.price)} ${reviewState}</p><p class="muted">约定：${esc(o.meeting||'接单时约定')} ${esc(o.place)}<br>买家确认：${date(o.buyer_confirm)}　卖家确认：${date(o.seller_confirm)}</p>${o.note?`<p class="details">处理说明：${esc(o.note)}</p>`:''}<div class="actions">${o.status==='待接单'&&me.id===o.seller?button('accept','接受订单并约定交付',o.id):''}${['待接单','待交付'].includes(o.status)&&!o.buyer_confirm&&!o.seller_confirm?button('cancel',me.id===o.seller?'拒绝 / 取消订单':'取消订单',o.id):''}${o.status==='待交付'?button('confirm',me.id===o.buyer?'确认已收到':'确认已交付',o.id,''):''}${o.status==='待交付'?button('dispute','发起争议',o.id):''}${['待交付','已完成'].includes(o.status)?button('withdraw','申请撤回本人确认',o.id):''}${reviewLink}${appendLink}${replyLink}${button('history','撤回历史',o.id)}</div>${d.withdrawals.filter(w=>w.order_id===o.id).map(w=>`<p>撤回申请：${esc(w.status)} · ${esc(w.result||w.body)}</p>`).join('')}</article>`;
    }).join(''):'<div class="empty">还没有订单，去集市发现一件心仪物品。</div>'}`;
}

async function admin(){
    if(!auth())return;const query=new URLSearchParams();if(adminStart)query.set('start',Math.floor(new Date(adminStart+'T00:00:00').getTime()/1000));if(adminEnd)query.set('end',Math.floor(new Date(adminEnd+'T23:59:59').getTime()/1000));const d=await api('admin?'+query);
    main.innerHTML=`<div class="section-title"><h1>校园运营后台</h1><small>审核有依据，处置可追溯</small></div><div class="stats">${[['发布物品',d.stats.products],['有效完成订单',d.stats.completed],['成交约定金额','¥ '+money(d.stats.amount)],['待处理举报',d.stats.reports],['评价总数',d.stats.reviews],['已隐藏评价',d.stats.hidden_reviews]].map(([k,v])=>`<div class="stat"><span class="muted">${k}</span><strong>${v}</strong></div>`).join('')}</div><form id="stats" class="filter"><label>开始日期<input name="start" type="date" value="${esc(adminStart)}"></label><label>结束日期<input name="end" type="date" value="${esc(adminEnd)}"></label><button>查询统计</button><a class="button secondary" href="/api/export?${query}">导出成交CSV</a></form><div class="tabs">${[['products','商品审核'],['users','用户治理'],['reports','举报处理'],['withdrawals','撤回审核'],['disputes','争议裁定'],['reviews','评价管理'],['audit','操作日志']].map(([k,v])=>`<button class="${k===adminTab?'active':'secondary'}" data-tab="${k}">${v}</button>`).join('')}</div><div id="admin-content"></div>`;
    renderAdmin(d);
}
function renderAdmin(d){
    const el=document.querySelector('#admin-content');if(!el)return;
    if(adminTab==='reviews'){
        let reviews=d.reviews||[];
        if(adminReviewStatus==='visible')reviews=reviews.filter(r=>!r.hidden);
        if(adminReviewStatus==='hidden')reviews=reviews.filter(r=>!!r.hidden);
        if(adminReviewTag)reviews=reviews.filter(r=>(r.tags||[]).includes(adminReviewTag));
        const filters=`<div class="filter reviewfilter"><label>状态<select id="reviewstatus"><option value="all" ${adminReviewStatus==='all'?'selected':''}>全部</option><option value="visible" ${adminReviewStatus==='visible'?'selected':''}>显示中</option><option value="hidden" ${adminReviewStatus==='hidden'?'selected':''}>已隐藏</option></select></label><label>标签<select id="reviewtag"><option value="">全部标签</option>${REVIEW_TAGS.map(t=>`<option value="${esc(t)}" ${adminReviewTag===t?'selected':''}>${esc(t)}</option>`).join('')}</select></label></div>`;
        el.innerHTML=filters+(reviews.length?reviews.map(r=>{
            const images=(r.images||[]).map(img=>`<a href="/api/review_image?id=${img.id}" target="_blank" rel="noopener"><img class="admin-thumb" src="/api/review_image?id=${img.id}" alt="评价图片" loading="lazy"></a>`).join('');
            const reply=r.reply?`<div class="reply"><div class="reply-head"><strong>卖家回复</strong><small>${date(r.reply.updated||r.reply.created)}</small></div><p>${esc(r.reply.body)}</p></div>`:'';
            return `<article class="panel admin-review"><div class="section-title"><h3>评价 #${r.id} · 订单 #${r.order_id}</h3><span class="badge ${r.hidden?'warn':''}">${r.hidden?'已隐藏':'显示中'}</span></div><p>${esc(r.product_title)} · 买家 ${esc(r.buyer_name)} · 卖家 ${esc(r.seller_name)} · ${ratingLine(r.rating)}</p><p class="muted">${esc(r.order_status)} · 评价人 ${esc(r.author_name)} · ${date(r.created)}</p><p class="review-body">${esc(r.body)}</p>${images?`<div class="review-images">${images}</div>`:''}${r.tags&&r.tags.length?`<div class="taglist">${r.tags.map(tagBadge).join('')}</div>`:''}${reply}${r.hidden?`<p class="warn-text">隐藏理由：${esc(r.hidden_reason)}</p>`:''}<div class="actions">${r.hidden?button('unhide_review','恢复显示',r.id):button('hide_review','隐藏',r.id)}${button('delete_review','硬删除',r.id,'danger')}</div></article>`;
        }).join(''):'<div class="empty">当前筛选条件下没有评价。</div>');
        return;
    }
    const items=d[adminTab]||[];
    el.innerHTML=items.length?items.map(o=>{
        let title='',body='',actions='';
        if(adminTab==='products'){
            title=`#${o.id} ${esc(o.title)}`;body=`${esc(o.seller_name)} · ${esc(o.status)}${o.frozen?' · 违规冻结':''} · ¥${money(o.price)}`;actions=(o.status==='待审核'?button('review_product','通过',o.id,'','data-approve="1"')+button('review_product','驳回',o.id,'secondary','data-approve="0"'):'')+button(o.frozen?'unfreeze':'freeze',o.frozen?'整改审核并解冻':'冻结',o.id);body+=`<p class="details">${esc(o.description)}</p>`;
        }else if(adminTab==='users'){
            title=`${esc(o.name)}（${esc(o.student)}）`;body=`${esc(o.role)} · ${o.banned_until>Date.now()/1000?'封禁至 '+date(o.banned_until):'正常'}`;actions=o.role==='admin'?'':button('ban','封禁7天',o.id)+button('unban','解封',o.id);
        }else if(adminTab==='reports'){
            title=`举报 #${o.id} · 商品 #${o.product}`;body=`${esc(o.status)} · ${esc(o.body)} · ${esc(o.result)}`;actions=o.status==='待处理'?button('handle_report','填写处理结论',o.id):'';
        }else if(adminTab==='withdrawals'){
            title=`撤回申请 #${o.id} · 订单 #${o.order_id}`;body=`${esc(o.status)} · ${esc(o.body)} · ${esc(o.result)}`;actions=o.status==='待审核'?button('handle_withdrawal','批准并转争议',o.id,'','data-approve="1"')+button('handle_withdrawal','驳回',o.id,'secondary','data-approve="0"'):'';
        }else if(adminTab==='disputes'){
            title=`争议订单 #${o.id} · ${esc(o.title)}`;body=esc(o.note);actions=button('resolve','认定完成',o.id,'','data-complete="1"')+button('resolve','关闭并释放占用',o.id,'secondary','data-complete="0"');
        }else{
            title=`${esc(o.name)} · ${esc(o.action)} · #${o.target}`;body=`${date(o.created)}<p class="details">${esc(o.detail)}</p>`;
        }
        return `<article class="panel"><h3>${title}</h3><div>${body}</div><div class="actions">${actions}</div></article>`;
    }).join(''):'<div class="empty">当前没有需要处理的记录。</div>';
}

async function route(){
    clearInterval(timer);timer=null;
    try{
        const [page,id]=location.hash.slice(1).split('/');
        const routes={market,detail:()=>detail(Number(id)),login,publish,mine,orders,admin,seller:()=>sellerPage(Number(id)),review:()=>reviewPage(Number(id))};
        await (routes[page||'market']||market)();
    }catch(e){
        main.innerHTML=`<div class="panel"><h1>暂时无法打开</h1><p>${esc(e.message)}</p><a class="button" href="#market">返回集市</a></div>`;
    }
}
async function readImages(files){
    const list=files?[...files]:[];
    if(list.length>3)throw new Error('最多上传3张图片');const out=[];
    for(const file of list){
        if(!['image/png','image/jpeg'].includes(file.type))throw new Error('仅支持PNG或JPEG图片');
        if(file.size>5*1024*1024)throw new Error('单张图片不得超过5MB');
        out.push(await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('图片读取失败'));reader.readAsDataURL(file)}));
    }
    return out;
}

document.addEventListener('submit',async e=>{
    e.preventDefault();const f=e.target,submit=f.querySelector('button');if(submit)submit.disabled=true;
    try{
        if(f.id==='search')await search();
        else if(f.id==='stats'){
            const d=Object.fromEntries(new FormData(f));if(d.start&&d.end&&d.start>d.end)throw new Error('开始日期不能晚于结束日期');adminStart=d.start;adminEnd=d.end;await admin();
        }else if(f.id==='login'){
            await api('login',Object.fromEntries(new FormData(f)));await session();location.hash='market';await route();
        }else if(f.id==='register'){
            toast((await api('register',Object.fromEntries(new FormData(f)))).message);
        }else if(f.id==='publish'){
            const d=Object.fromEntries(new FormData(f)),file=document.querySelector('#photo').files[0];
            if(file){if(file.size>5*1024*1024)throw new Error('图片不得超过5MB');d.image=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=()=>reject(new Error('图片读取失败'));reader.readAsDataURL(file)})}
            if(editing)d.id=editing;await api(editing?'edit':'publish',d);editing=0;toast('提交成功，管理员审核后上架');location.hash='mine';
        }else if(f.id==='message'){
            const d=Object.fromEntries(new FormData(f));await api('message',{...d,id:Number(f.dataset.id),receiver:Number(d.receiver)});f.elements.body.value='';await messages(Number(f.dataset.id));
        }else if(f.id==='reviewform'){
            const fd=new FormData(f),data={id:Number(f.dataset.id),rating:Number(f.elements.rating.value),body:fd.get('body'),tags:fd.getAll('tags')};data.images=await readImages(f.elements.images.files);await api('review',data);toast('评价已提交');location.hash='orders';await route();
        }else if(f.id==='appendform'){
            await api('append_review',{id:Number(f.dataset.id),body:f.elements.body.value});toast('补充内容已追加');await route();
        }else if(f.id==='replyform'){
            await api('reply_review',{id:Number(f.dataset.id),body:f.elements.body.value});toast('回复已保存');await route();
        }
    }catch(err){toast(err.message)}finally{if(submit)submit.disabled=false}
});
document.addEventListener('click',async e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.demo){const pair={seller:['20260001','Seller123!'],buyer:['20260002','Buyer123!'],admin:['admin','Admin123!']}[b.dataset.demo];const f=document.querySelector('#login');f.elements.student.value=pair[0];f.elements.password.value=pair[1];return}
    if(b.dataset.tab){adminTab=b.dataset.tab;await admin().catch(err=>toast(err.message));return}
    if(b.dataset.sellerTab){sellerTab=b.dataset.sellerTab;renderSellerPage();return}
    if(b.dataset.action==='filter_tag'){reviewTag=b.dataset.tag||'';renderReviews();return}
    if(b.dataset.action==='seller_filter_tag'){sellerReviewTag=b.dataset.tag||'';renderSellerPage();return}
    const action=b.dataset.action;if(!action)return;if(!auth())return;const d={id:Number(b.dataset.id||0)};
    try{
        if(action==='logout'){await api(action,{});await session();location.hash='market';await route();return}
        if(action==='history'){const r=await api('order_history?id='+d.id);alert('撤回历史：\n'+(r.confirmations.map(x=>'用户 '+x.user_id+' 原确认 '+date(x.confirmed)+'；撤回 '+date(x.revoked)+'；'+x.reason).join('\n')||'暂无撤回记录'));return}
        if(action==='edit_form'){const result=await api('product?id='+d.id);publish();editing=d.id;const form=document.querySelector('#publish');for(const k of ['title','description','category','condition'])form.elements[k].value=result.product[k];form.elements.price.value=result.product.price/100;window.scrollTo(0,0);return}
        if(action==='delete_reply'){if(!confirm('删除后回复会彻底消失，确认删除？'))return;await api(action,d);toast('回复已删除');await route();return}
        if(action==='accept'){d.place=prompt('校内交付地点','图书馆门口');if(!d.place)return;d.meeting=prompt('交付时间（格式：2026-11-06T18:00；须在下单后72小时内）',new Date(Date.now()+3600000-new Date().getTimezoneOffset()*60000).toISOString().slice(0,16));if(!d.meeting)return}
        else if(['report','dispute','withdraw'].includes(action)){d.body=prompt('请填写具体原因（至少2字）');if(!d.body)return}
        else if(['hide_review','unhide_review','delete_review'].includes(action)){if(action==='delete_review'&&!confirm('硬删除后评价和图片会彻底移除，确认？'))return;d.reason=prompt(action==='hide_review'?'填写隐藏理由':'填写处理理由',action==='unhide_review'?'复核后恢复显示':'');if(!d.reason)return}
        else if(['review_product','freeze','unfreeze','ban','unban','handle_report','handle_withdrawal','resolve'].includes(action)){d.reason=prompt(action==='resolve'?'请填写核实的交付证据与裁定依据（认定完成至少10字）':'填写处理 / 审核理由',action==='review_product'&&b.dataset.approve==='1'?'内容审核符合发布要求':'');if(!d.reason)return;d.approve=b.dataset.approve==='1';d.complete=b.dataset.complete==='1';d.days=7}
        else if(['order','cancel','confirm'].includes(action)&&!confirm({order:'确认下单并占用这件商品？',cancel:'确认关闭本订单并释放占用？',confirm:'确认已实际完成线下交付？'}[action]))return;
        b.disabled=true;await api(action,d);toast('操作成功');await route();
    }catch(err){toast(err.message)}finally{b.disabled=false}
});
document.addEventListener('change',e=>{
    if(e.target.id==='reviewstatus'){adminReviewStatus=e.target.value;admin().catch(err=>toast(err.message));return}
    if(e.target.id==='reviewtag'){adminReviewTag=e.target.value;admin().catch(err=>toast(err.message));return}
    if(e.target.name==='tags'){const checked=[...document.querySelectorAll('input[name="tags"]:checked')];if(checked.length>3){e.target.checked=false;toast('最多选择3个标签')}}
});
document.addEventListener('input',e=>{if(e.target.id==='rating'){const out=document.querySelector('#ratingval');if(out)out.textContent=Number(e.target.value).toFixed(1)}});
window.addEventListener('hashchange',route);
session().then(route).catch(e=>toast(e.message));
