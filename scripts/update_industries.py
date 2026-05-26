import os
import sys
import logging
import sqlite3
from pathlib import Path

# 强制绕过代理以防 ConnectionError
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

orig_request = requests.Session.request
def patched_request(self, method, url, *args, **kwargs):
    kwargs['proxies'] = {'http': None, 'https': None}
    kwargs['verify'] = False
    return orig_request(self, method, url, *args, **kwargs)
requests.Session.request = patched_request

# 将上级目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from stockdb import StockDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("update_industries")

def update_industries():
    try:
        import akshare as ak
    except ImportError:
        logger.error("未找到 akshare 模块，请先安装。")
        return

    logger.info("获取新浪行业分类板块...")
    try:
        sectors = ak.stock_sector_spot()
    except Exception as e:
        logger.error("拉取行业分类板块失败: %s", e)
        return

    logger.info("共发现 %d 个板块，开始拉取成分股...", len(sectors))
    
    code_to_industry = {}
    for idx, row in sectors.iterrows():
        label = row["label"]
        name = row["板块"]
        logger.info("正在拉取板块: %s (%s)...", name, label)
        try:
            detail = ak.stock_sector_detail(sector=label)
            for _, s_row in detail.iterrows():
                code = str(s_row["code"]).strip().zfill(6)
                if code not in code_to_industry:
                    code_to_industry[code] = name
        except Exception as e:
            logger.warning("拉取板块 %s 失败: %s", name, e)

    logger.info("成功建立 %d 只股票的行业分类映射。", len(code_to_industry))

    db = StockDB()
    db_path = db.cfg.db_path
    logger.info("正在将映射写入元数据库 %s ...", db_path)

    if not db_path.exists():
        logger.error("数据库文件 %s 不存在，请先初始化数据库（运行 init_full.py）。", db_path)
        return

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # 1. 将所有当前 stocks 表中的 industry 清空（或者设置为 NULL）以便更新
        cursor.execute("UPDATE stocks SET industry = NULL")
        
        # 2. 批量更新
        update_data = [(industry, code) for code, industry in code_to_industry.items()]
        cursor.executemany("UPDATE stocks SET industry = ? WHERE code = ?", update_data)
        
        # 3. 将所有依然为空 of industry 更新为 '未分类'
        cursor.execute("UPDATE stocks SET industry = '未分类' WHERE industry IS NULL OR industry = ''")
        
        conn.commit()
        
        # 统计
        cursor.execute("SELECT industry, count(*) FROM stocks GROUP BY industry")
        logger.info("数据库更新成功！各行业分布如下：")
        for row in cursor.fetchall():
            logger.info("  %s: %d 只", row[0], row[1])
            
        conn.close()
    except Exception as e:
        logger.error("写入数据库失败: %s", e)

if __name__ == "__main__":
    update_industries()
