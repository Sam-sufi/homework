"""命令行启动入口：python run.py [--port 端口]。

页面启动脚本（启动系统.bat）双击后执行的也是这个文件。
真正的服务组装在 app.create_server()，这里只负责解析参数、打印地址、捕获退出。
"""
import argparse

from app import create_server

if __name__ == '__main__':
    # --port 默认 8000；端口被占用时可传 --port 8001
    parser = argparse.ArgumentParser(description='校园拾集启动器')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    try:
        server = create_server(port=args.port)
        # flush=True 保证重定向到日志文件时也能立刻看到启动地址
        print(f'校园拾集已启动：http://127.0.0.1:{server.server_port}  按Ctrl+C停止', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        # 用户按 Ctrl+C：正常退出，不打印异常
        print('\n服务已停止')
    except OSError as e:
        # 常见原因是端口被占用；提示换端口，并以失败状态退出
        print(f'启动失败：{e}。端口占用可运行 python run.py --port 8001')
        raise SystemExit(1)
