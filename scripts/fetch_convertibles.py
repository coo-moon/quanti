#!/usr/bin/env python3
"""拉全量可转债日线到 data/cb.db —— 为「低价/双低可转债策略」回测供数(幸存者正确)。

可转债是 A 股里结构性最稳、long-only 无需融券的方向:债底保底 + 转股期权上行的不对称收益。
akshare `bond_zh_hs_cov_daily` 给单券完整生命周期日线(含强赎/到期退出),故按当时在交易取池
即幸存者正确。全量清单来自 `bond_zh_cov`(含已退市)。

可续跑:已在 cb.db 的 code 跳过。per-code try/except,单券失败不炸全量。
用法:.venv/bin/python scripts/fetch_convertibles.py
"""
import sys
import sqlite3
import time

import akshare as ak

CB_DB = "data/cb.db"


def exchange_prefix(code: str) -> str | None:
    """债券代码 → akshare symbol 前缀。11x/110/113/118=沪;12x/123/127/128=深。"""
    c = str(code)
    if c.startswith(("11", "10", "118")):
        return "sh" + c
    if c.startswith(("12", "128", "123", "127")):
        return "sz" + c
    return None


def main():
    con = sqlite3.connect(CB_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS cb_daily(
        code TEXT, date TEXT, close REAL, volume REAL,
        PRIMARY KEY(code, date))""")
    con.execute("""CREATE TABLE IF NOT EXISTS cb_info(
        code TEXT PRIMARY KEY, name TEXT, stock_code TEXT,
        conv_price REAL, issue_size REAL, rating TEXT)""")
    con.commit()

    lst = ak.bond_zh_cov()
    lst = lst.rename(columns={"债券代码": "code", "债券简称": "name", "正股代码": "stock_code",
                              "转股价": "conv_price", "发行规模": "issue_size", "信用评级": "rating"})
    print(f"全量可转债清单 {len(lst)} 只", flush=True)
    for _, r in lst.iterrows():
        con.execute("INSERT OR REPLACE INTO cb_info VALUES(?,?,?,?,?,?)",
                    (str(r["code"]), r.get("name"), str(r.get("stock_code", "")),
                     float(r["conv_price"]) if str(r.get("conv_price", "")).replace(".", "").isdigit() else None,
                     None, str(r.get("rating", ""))))
    con.commit()

    done = {row[0] for row in con.execute("SELECT DISTINCT code FROM cb_daily")}
    codes = [str(c) for c in lst["code"].tolist()]
    todo = [c for c in codes if c not in done]
    print(f"已有 {len(done)} 只,待拉 {len(todo)} 只", flush=True)

    ok = fail = 0
    for i, code in enumerate(todo):
        sym = exchange_prefix(code)
        if not sym:
            fail += 1
            continue
        try:
            d = ak.bond_zh_hs_cov_daily(symbol=sym)
            rows = [(code, str(x["date"])[:10], float(x["close"]), float(x.get("volume", 0)))
                    for _, x in d.iterrows() if x["close"] and x["close"] > 0]
            con.executemany("INSERT OR REPLACE INTO cb_daily VALUES(?,?,?,?)", rows)
            con.commit()
            ok += 1
        except Exception as e:
            fail += 1
            if fail <= 8:
                print(f"  {code} FAIL: {repr(e)[:80]}", flush=True)
        if i % 50 == 0:
            print(f"  ..{i}/{len(todo)} ok={ok} fail={fail}", flush=True)
        time.sleep(0.15)  # 温和限速

    n = list(con.execute("SELECT COUNT(DISTINCT code), COUNT(*), MIN(date), MAX(date) FROM cb_daily"))[0]
    print(f"\ncb.db: {n[0]} 只券, {n[1]} 行, {n[2]} ~ {n[3]}  (ok={ok} fail={fail})", flush=True)
    print("DONE", flush=True)
    con.close()


if __name__ == "__main__":
    main()
