"""数据库备份脚本：python backup.py。

使用 SQLite 的在线备份 API，服务运行中也能安全备份，不用先停服务。
备份文件输出到 data/backups/，文件名带微秒时间戳，不会互相覆盖。
"""
import sqlite3, datetime
from pathlib import Path

base = Path(__file__).resolve().parent / 'data'

if __name__ == '__main__':
    source = base / 'campus.db'
    if not source.exists():
        # 还没有数据库说明系统没启动过，先提示再退出
        raise SystemExit('请先运行系统以创建数据库。')
    folder = base / 'backups'
    folder.mkdir(exist_ok=True)
    # 例：campus_20261126_140930_123456.db（年月日_时分秒_微秒）
    target = folder / ('campus_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.db')
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        # 无论成功失败都关闭连接，避免文件被占用
        dst.close()
        src.close()
    print('备份已保存：' + str(target))
