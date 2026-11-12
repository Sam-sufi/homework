import argparse
from app import create_server
if __name__=='__main__':
    parser=argparse.ArgumentParser(description='校园拾集启动器');parser.add_argument('--port',type=int,default=8000);args=parser.parse_args()
    try:
        server=create_server(port=args.port);print(f'校园拾集已启动：http://127.0.0.1:{server.server_port}  按Ctrl+C停止',flush=True);server.serve_forever()
    except KeyboardInterrupt:print('\n服务已停止')
    except OSError as e:print(f'启动失败：{e}。端口占用可运行 python run.py --port 8001');raise SystemExit(1)
