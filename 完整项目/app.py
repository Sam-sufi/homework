"""校园拾集：启动入口（建库 + 启动 HTTP 服务）。

模块分工：
    core.py      通用工具（时间、密码、校验、异常）
    db.py        数据库连接与建表
    services.py  业务规则（各动作函数 + act 分发）
    api.py       HTTP 接口（路由、鉴权、响应）
    app.py       就是你看到的这里：只负责组装和启动
"""
from http.server import ThreadingHTTPServer

from api import Handler
from core import ROOT, now  # re-export：测试从 app 导入 now
from db import connect, init_db  # re-export：测试从 app 导入 connect/init_db


def create_server(host='127.0.0.1', port=8000, db_path=None):
    """创建（但不启动）HTTP 服务。

    参数:
        host     监听地址，默认 127.0.0.1，只允许本机访问；
        port     端口，传 0 表示由系统随机分配（测试用）；
        db_path  数据库文件路径，默认 data/campus.db。

    步骤:
        1. init_db(path)：建目录、建表，首次启动时写入演示数据；
        2. ThreadingHTTPServer：每个请求一个线程，处理类是 api.Handler；
        3. server.db_path：挂到服务器上，所有 Handler 通过 self.server.db_path 读取；
        4. server.login_attempts：登录失败计数，进程内共享。

    返回:
        未启动的服务器对象。run.py 里再调用 server.serve_forever() 才开始接收请求。

    示例:
        server = create_server(port=8001)
        print(server.server_port)
        server.serve_forever()
    """
    path = str(db_path or ROOT / 'data/campus.db')
    init_db(path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = path
    server.login_attempts = {}
    return server
