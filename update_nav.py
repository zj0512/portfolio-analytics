# -*- coding: utf-8 -*-
"""抓取13只基金的最新净值(增量, 直到2026-09-08)。"""
import urllib.request, json, sqlite3, time

DB = r"D:\zj\portfolio_app\portfolio.db"
FUNDS = {
 "159682":"创业五零","159593":"A50指数","515180":"100红利","563020":"低波红利",
 "511090":"30年国债","511130":"国债30年","159649":"国开债","511360":"短融ETF",
 "159937":"黄金9999","518880":"黄金ETF","513100":"纳指ETF","513650":"标普ETF",
 "159516":"半导设备"
}

def fetch(code, page, size=20):
    url = "http://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=%s&pageSize=%s" % (code, page, size)
    req = urllib.request.Request(url, headers={"Referer":"http://fundf10.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))

conn = sqlite3.connect(DB)
cur = conn.cursor()
# 只取最近几页(前3页=60条, 足够覆盖到今天+补最近)
for code, name in FUNDS.items():
    n = 0
    try:
        # 取最近3页
        for page in [1,2,3]:
            d = fetch(code, page)
            lst = d.get("Data",{}).get("LSJZList",[])
            if not lst: break
            for r in lst:
                cur.execute("INSERT OR REPLACE INTO fund_nav (fund_code, fsrq, dwjz) VALUES (?,?,?)",
                            (code, r["FSRQ"], float(r["DWJZ"])))
                n += 1
            time.sleep(0.2)
        conn.commit()
        # 查最新日期
        latest = cur.execute("SELECT MAX(fsrq) FROM fund_nav WHERE fund_code=?", (code,)).fetchone()[0]
        newv = cur.execute("SELECT dwjz FROM fund_nav WHERE fund_code=? ORDER BY fsrq DESC LIMIT 1", (code,)).fetchone()[0]
        print("%s %s : 更新%d条, 最新净值 %s=%.4f" % (code, name, n, latest, newv))
    except Exception as e:
        print("%s %s : 失败 %s" % (code, name, str(e)[:40]))
conn.close()
print("DONE")
