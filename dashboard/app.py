import sys
import os
import sqlite3
import subprocess
import time
import re
from pathlib import Path
from flask import Flask, jsonify, request, Response, render_template
import pandas as pd
import yaml

# Add project root to sys.path
ROOT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT_DIR))

from stockdb.config import Config
from stockdb.db import MetaDB
from stockdb.market import detect_market, detect_board, INDEX_MAP, normalize_code
from stockdb import StockDB
from dashboard.chip_calc import calculate_daily_chip

app = Flask(__name__)

# Compile standard stock codes checker (exclude bonds, funds, etc.)
A_SHARE_RE = re.compile(r'^(000|001|002|003|300|301|600|601|603|605|688)\d{3}$')

def get_db_and_meta():
    cfg = Config()
    meta = MetaDB(cfg.db_path)
    return cfg, meta

def get_directory_size(p: Path):
    if not p.exists():
        return 0, 0
    files = list(p.rglob("*.parquet"))
    count = len(files)
    total_size = sum(f.stat().st_size for f in files)
    return count, total_size

def get_expected_trade_days(meta):
    """
    Returns (expected_daily_day, expected_minutes_day) as YYYYMMDD strings.
    - Daily data is only expected after 15:30 on trade days.
    - Minutes data is only expected after 15:10 on trade days.
    - Before these times, or on weekends/holidays, the expected latest day is the previous trading day.
    """
    import datetime
    now = datetime.datetime.now()
    today_str = now.strftime("%Y%m%d")
    
    is_today_trade = meta.is_trade_day(today_str)
    
    if is_today_trade:
        # Find the last trading day strictly before today
        with sqlite3.connect(str(meta.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT date FROM trade_calendar WHERE is_open=1 AND date < ? ORDER BY date DESC LIMIT 1",
                (today_str,)
            )
            row = cursor.fetchone()
            prev_trade_day = row["date"] if row else None
            
        if now.time() >= datetime.time(15, 30):
            expected_daily = today_str
        else:
            expected_daily = prev_trade_day or today_str
            
        if now.time() >= datetime.time(15, 10):
            expected_minutes = today_str
        else:
            expected_minutes = prev_trade_day or today_str
    else:
        last_trade = meta.last_trade_day()
        expected_daily = last_trade
        expected_minutes = last_trade
        
    return expected_daily, expected_minutes

def read_config_raw():
    config_path = ROOT_DIR / "config.yaml"
    if not config_path.exists():
        return ""
    return config_path.read_text(encoding="utf-8")

def write_config_watchlist(watchlist):
    config_path = ROOT_DIR / "config.yaml"
    if not config_path.exists():
        # Fallback: create a basic config
        cfg = {"watchlist": watchlist}
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        return
        
    content = config_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    new_lines = []
    in_watchlist = False
    watchlist_idx = -1
    
    for i, line in enumerate(lines):
        if line.strip().startswith("watchlist:"):
            watchlist_idx = i
            in_watchlist = True
            new_lines.append(line)
            for c in sorted(watchlist):
                new_lines.append(f'  - "{c}"')
            continue
        
        if in_watchlist:
            if line.strip().startswith("-") or not line.strip():
                continue
            else:
                in_watchlist = False
        
        new_lines.append(line)
        
    if watchlist_idx == -1:
        new_lines.append("\n# 重点分钟线监控/更新的股票代码列表")
        new_lines.append("watchlist:")
        for c in sorted(watchlist):
            new_lines.append(f'  - "{c}"')
            
    config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

@app.route('/api/overview')
def api_overview():
    try:
        cfg, meta = get_db_and_meta()
        
        # 1. 统计股票总数
        total_stocks = 0
        with sqlite3.connect(str(cfg.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM stocks")
            total_stocks = cursor.fetchone()[0]
            
        # 2. 统计各类 Parquet 文件的数量和大小
        daily_count, daily_size = get_directory_size(cfg.data_dir / "daily")
        minutes_count, minutes_size = get_directory_size(cfg.data_dir / "minutes")
        index_count, index_size = get_directory_size(cfg.data_dir / "index")
        tick_count, tick_size = get_directory_size(cfg.data_dir / "tick")
        
        total_size = daily_size + minutes_size + index_size + tick_size
        
        # 3. 期望的最新的交易日
        expected_daily, expected_minutes = get_expected_trade_days(meta)
        
        # 4. 实际最后更新日期
        last_update_date = "N/A"
        with sqlite3.connect(str(cfg.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT date FROM update_log ORDER BY date DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                last_update_date = row[0]
                
        # 5. 数据是否滞后
        data_lagging = False
        if last_update_date != "N/A" and expected_daily:
            data_lagging = last_update_date < expected_daily
            
        return jsonify({
            "success": True,
            "data": {
                "total_stocks": total_stocks,
                "daily": {"count": daily_count, "size": daily_size},
                "minutes": {"count": minutes_count, "size": minutes_size},
                "index": {"count": index_count, "size": index_size},
                "tick": {"count": tick_count, "size": tick_size},
                "total_size": total_size,
                "expected_daily": expected_daily,
                "expected_minutes": expected_minutes,
                "last_update_date": last_update_date,
                "data_lagging": data_lagging
            }
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/run_update')
def api_run_update():
    def generate():
        py_executable = sys.executable
        script_path = str(ROOT_DIR / "scripts" / "daily_update.py")
        
        cmd = [py_executable, "-u", script_path]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(ROOT_DIR)
        )
        
        yield "data: 🚀 开始运行每日增量同步脚本...\n\n"
        
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        
        while True:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            clean_line = ansi_escape.sub('', line)
            yield f"data: {clean_line.rstrip()}\n\n"
            
        rc = process.poll()
        if rc == 0:
            yield "data: ✅ 每日增量数据同步成功完成！\n\n"
        else:
            yield f"data: ❌ 每日同步脚本运行出错，退出码: {rc}\n\n"
            
    return Response(generate(), mimetype='text/event-stream')

@app.route('/')
def index():
    return render_template("index.html")

# 全局筹码数据缓存，结构：{code: {"mtime": float, "stats": dict}}
CHIP_CACHE = {}

def get_watchlist_chip_stats(db, cfg, code):
    """
    高效获取个股最新一天的筹码特征，带有 mtime 文件修改时间缓存
    """
    global CHIP_CACHE
    try:
        daily_path = cfg.daily_path(code)
        if not daily_path.exists():
            return {
                "current_close": "N/A",
                "profit_ratio": "N/A",
                "concentration_90": "N/A",
                "concentration_70": "N/A",
                "avg_cost": "N/A",
                "peak_price": "N/A"
            }
        
        mtime = daily_path.stat().st_mtime
        if code in CHIP_CACHE and CHIP_CACHE[code]["mtime"] == mtime:
            return CHIP_CACHE[code]["stats"]
            
        # 缓存未命中，调用筹码引擎重新计算
        res = calculate_daily_chip(db, code, calc_days=500, show_days=150)
        if res.get("success", False) and "stats" in res:
            stats = res["stats"]
            CHIP_CACHE[code] = {
                "mtime": mtime,
                "stats": stats
            }
            return stats
        else:
            return {
                "current_close": "N/A",
                "profit_ratio": "N/A",
                "concentration_90": "N/A",
                "concentration_70": "N/A",
                "avg_cost": "N/A",
                "peak_price": "N/A"
            }
    except Exception as e:
        print(f"Error calculating stats for {code}: {e}")
        return {
            "current_close": "N/A",
            "profit_ratio": "N/A",
            "concentration_90": "N/A",
            "concentration_70": "N/A",
            "avg_cost": "N/A",
            "peak_price": "N/A"
        }

@app.route('/api/watchlist')
def api_watchlist():
    try:
        cfg, meta = get_db_and_meta()
        db = StockDB()
        
        # 合并配置中的 watchlist 与 minutes 目录下已存在的股票代码
        watchlist_codes = set(cfg.watchlist or [])
        minutes_dir = cfg.data_dir / "minutes"
        if minutes_dir.exists():
            for m in ("sh", "sz", "bj"):
                m_dir = minutes_dir / m
                if m_dir.exists():
                    for f in m_dir.iterdir():
                        if f.is_dir() and A_SHARE_RE.match(f.name):
                            watchlist_codes.add(f.name)
                            
        # 查询 SQLite 获取股票详细信息
        stocks_info = {}
        with sqlite3.connect(str(cfg.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if watchlist_codes:
                placeholders = ",".join("?" for _ in watchlist_codes)
                cursor.execute(f"SELECT * FROM stocks WHERE code IN ({placeholders})", list(watchlist_codes))
                for row in cursor.fetchall():
                    stocks_info[row["code"]] = {
                        "name": row["name"],
                        "market": row["market"],
                        "board": row["board"]
                    }
                    
        # 期望的最新的交易日
        expected_daily, expected_minutes = get_expected_trade_days(meta)
        
        result = []
        for code in sorted(watchlist_codes):
            info = stocks_info.get(code, {
                "name": "未知股票",
                "market": detect_market(code),
                "board": detect_board(code)
            })
            
            # 高效获取该股最新筹码统计数据
            chip_stats = get_watchlist_chip_stats(db, cfg, code)
            
            # 1. 计算日线状态
            daily_path = cfg.daily_path(code)
            daily_min, daily_max, daily_rows = "N/A", "N/A", 0
            if daily_path.exists():
                try:
                    df_d = pd.read_parquet(daily_path, columns=["date"])
                    if not df_d.empty:
                        dates_d = df_d["date"].dropna().astype(str).tolist()
                        if dates_d:
                            dates_clean = [d.replace("-", "")[:8] for d in dates_d]
                            daily_min = min(dates_clean)
                            daily_max = max(dates_clean)
                            daily_rows = len(df_d)
                except Exception:
                    pass
            
            # 2. 计算分钟线状态
            m_market = detect_market(code)
            minutes_dir_stock = cfg.data_dir / "minutes" / m_market / code
            min1_min, min1_max, min1_days = "N/A", "N/A", 0
            if minutes_dir_stock.exists():
                m_files = list(minutes_dir_stock.glob("*.parquet"))
                m_dates = [f.stem for f in m_files if f.stem.isdigit() and len(f.stem) == 8]
                if m_dates:
                    m_dates.sort()
                    min1_min = m_dates[0]
                    min1_max = m_dates[-1]
                    min1_days = len(m_dates)
            
            # 3. 数据缺失状态判定
            daily_missing = False
            if daily_max == "N/A" or (expected_daily and daily_max < expected_daily):
                daily_missing = True
                
            minutes_missing = False
            if min1_max == "N/A" or (expected_minutes and min1_max < expected_minutes):
                minutes_missing = True
                
            # 4. 断档风险判定（分钟线超过90天不更新）
            risk_alert = False
            gap_days = 0
            if min1_max != "N/A" and expected_minutes:
                try:
                    import datetime
                    d_max = datetime.datetime.strptime(min1_max, "%Y%m%d")
                    d_exp = datetime.datetime.strptime(expected_minutes, "%Y%m%d")
                    gap_days = (d_exp - d_max).days
                    if gap_days >= 90:
                        risk_alert = True
                except Exception:
                    pass
            
            result.append({
                "code": code,
                "name": info["name"],
                "market": info["market"].upper(),
                "board": info["board"],
                "in_config": code in (cfg.watchlist or []),
                "current_close": chip_stats.get("current_close", "N/A"),
                "profit_ratio": chip_stats.get("profit_ratio", "N/A"),
                "concentration_90": chip_stats.get("concentration_90", "N/A"),
                "concentration_70": chip_stats.get("concentration_70", "N/A"),
                "avg_cost": chip_stats.get("avg_cost", "N/A"),
                "peak_price": chip_stats.get("peak_price", "N/A"),
                "daily_min": daily_min,
                "daily_max": daily_max,
                "daily_rows": daily_rows,
                "min1_min": min1_min,
                "min1_max": min1_max,
                "min1_days": min1_days,
                "daily_missing": daily_missing,
                "minutes_missing": minutes_missing,
                "risk_alert": risk_alert,
                "gap_days": gap_days
            })
            
        return jsonify({
            "success": True,
            "data": result
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/watchlist/add', methods=['POST'])
def api_watchlist_add():
    try:
        req_data = request.get_json() or {}
        code = req_data.get("code", "").strip()
        if not code or len(code) != 6 or not code.isdigit():
            return jsonify({"success": False, "error": "股票代码格式不正确，必须为6位数字"}), 400
            
        cfg, meta = get_db_and_meta()
        code = normalize_code(code)
        
        # Verify if code exists in stocks table
        with sqlite3.connect(str(cfg.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM stocks WHERE code=?", (code,))
            row = cursor.fetchone()
            if not row:
                return jsonify({"success": False, "error": f"股票代码 {code} 不在本地数据库的股票列表中"}), 404
                
        watchlist = list(cfg.watchlist or [])
        if code in watchlist:
            return jsonify({"success": True, "message": "股票已在自选股列表中"})
            
        watchlist.append(code)
        write_config_watchlist(watchlist)
        
        return jsonify({"success": True, "message": f"成功将 {code} ({row[0]}) 添加到自选股"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/watchlist/delete', methods=['POST'])
def api_watchlist_delete():
    try:
        req_data = request.get_json() or {}
        code = req_data.get("code", "").strip()
        if not code:
            return jsonify({"success": False, "error": "股票代码不能为空"}), 400
            
        cfg, _ = get_db_and_meta()
        code = normalize_code(code)
        
        watchlist = list(cfg.watchlist or [])
        if code not in watchlist:
            return jsonify({"success": False, "error": "股票不在自选股配置列表中"}), 404
            
        watchlist.remove(code)
        write_config_watchlist(watchlist)
        
        return jsonify({"success": True, "message": f"成功将 {code} 从自选股配置中移除"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/search')
def api_search():
    try:
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"success": True, "data": []})
            
        cfg, _ = get_db_and_meta()
        
        with sqlite3.connect(str(cfg.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Search by code or name
            sql = "SELECT code, name, market, board FROM stocks WHERE code LIKE ? OR name LIKE ? LIMIT 10"
            cursor.execute(sql, (f"{query}%", f"%{query}%"))
            rows = cursor.fetchall()
            
            result = []
            for r in rows:
                result.append({
                    "code": r["code"],
                    "name": r["name"],
                    "market": r["market"].upper(),
                    "board": r["board"]
                })
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.route('/api/chip/analyze')
def api_chip_analyze():
    try:
        code = request.args.get("code", "").strip()
        if not code or len(code) != 6 or not code.isdigit():
            return jsonify({"success": False, "error": "股票代码格式不正确，必须为6位数字"}), 400
            
        code = normalize_code(code)
        db = StockDB()
        cfg = db.cfg
        
        # 计算高精度筹码分布
        res = calculate_daily_chip(db, code, calc_days=500, show_days=150)
        if not res.get("success", False):
            return jsonify(res)
            
        # 检查该股票是否已经在 watchlist 中
        res["watchlist_synced"] = code in (cfg.watchlist or [])
        return jsonify(res)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/chip/sync_watchlist', methods=['POST'])
def api_chip_sync_watchlist():
    try:
        req_data = request.get_json() or {}
        code = req_data.get("code", "").strip()
        if not code:
            return jsonify({"success": False, "error": "股票代码不能为空"}), 400
            
        cfg, _ = get_db_and_meta()
        code = normalize_code(code)
        
        watchlist = list(cfg.watchlist or [])
        if code in watchlist:
            return jsonify({"success": True, "message": "股票已在自选股列表中"})
            
        watchlist.append(code)
        write_config_watchlist(watchlist)
        return jsonify({"success": True, "message": f"成功将 {code} 同步到自选股监控列表，每日将自动同步更新分钟线。"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    # Default Flask port
    app.run(host='127.0.0.1', port=5000, debug=True)
