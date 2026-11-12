"""使用SQLite在线备份API，无需停止服务。"""
import sqlite3,datetime
from pathlib import Path
base=Path(__file__).resolve().parent/'data'
if __name__=='__main__':
    source=base/'campus.db'
    if not source.exists():raise SystemExit('请先运行系统以创建数据库。')
    folder=base/'backups';folder.mkdir(exist_ok=True)
    target=folder/('campus_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.db')
    src=sqlite3.connect(source);dst=sqlite3.connect(target)
    try:src.backup(dst)
    finally:dst.close();src.close()
    print('备份已保存：'+str(target))
