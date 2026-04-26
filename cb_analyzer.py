#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cb_analyzer.py  —  可转债历史整合 + 报告生成 + 推送

每次运行自动输出两份报告：
    cb_report_YYYYMMDD.html   深色仪表盘（含净值折线图）
    cb_report_YYYYMMDD.md     Markdown（导入墨滴/Md2WeChat 发公众号）

20260401 手工修改部分：
六、格式化工具
单位换算部分，统一由原来的“万元”，改为“亿元”


"""

import re
import logging
import smtplib
import requests
import pandas as pd
import json
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── 目录 ─────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
DAILY_DIR    = BASE_DIR / "data" / "daily"
HISTORY_DIR  = BASE_DIR / "data" / "history"
REPORT_DIR   = BASE_DIR / "data" / "reports"
HISTORY_FILE = HISTORY_DIR / "cb_history.csv"

for _d in [DAILY_DIR, HISTORY_DIR, REPORT_DIR, BASE_DIR / "logs"]:
    _d.mkdir(parents=True, exist_ok=True)

# ── 日志 ─────────────────────────────────────────────────────
logger = logging.getLogger("cb_analyzer")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(BASE_DIR / "logs" / "analyzer.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

KEEP_COLUMNS = [
    "date", "bond_id", "bond_nm", "price", "increase_rt",
    "premium_rt", "dblow", "curr_iss_amt", "convert_value",
    "ytm_rt", "rating_cd", "sw_name", "market_cd",
    "maturity_dt", "year_left", "volume", "turnover_rt",
    "convert_price", "sprice", "sincrease_rt", "pb",
]

NUMERIC_COLS = [
    "price", "increase_rt", "premium_rt", "dblow",
    "curr_iss_amt", "convert_value", "ytm_rt",
    "volume", "turnover_rt", "year_left", "pb",
    "sprice", "sincrease_rt", "convert_price",
]


# ══════════════════════════════════════════════════════════════
#  一、历史数据整合
# ══════════════════════════════════════════════════════════════

def _parse_date(fp: Path):
    try:
        raw = fp.stem.replace("cb_list_", "")
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    except Exception:
        return None



def _clean_sw_name(val) -> str:
    # 清洗 sw_name 字段。
    # 集思录部分转债行业字段返回 JSON 字典（含逗号），写入 CSV 时会破坏字段对齐。
    # 此函数提取纯文本的行业名，兼容字典格式和普通字符串格式。
    import json
    s = str(val).strip()
    if not s.startswith("{"):
        return s
    # 尝试 JSON 解析（单引号先转双引号）
    try:
        obj = json.loads(s.replace("'", '"'))
        for key in ["\u4e00\u7ea7", "sw1", "industry"]:
            if key in obj:
                return str(obj[key])
        vals = list(obj.values())
        if vals:
            return str(vals[0])
    except Exception:
        pass
    # 兜底：找冒号后第一个被引号包裹的值
    colon_pos = s.find(":")
    if colon_pos != -1:
        rest = s[colon_pos + 1:].strip().strip("'\"").split("'")[0].split('"')[0]
        return rest.strip()
    return s


def _load_clean(fp: Path, date: str):
    try:
        df = pd.read_csv(fp, encoding="utf-8-sig")
        df["date"] = date
        existing = [c for c in KEEP_COLUMNS if c in df.columns]
        df = df[existing].copy()
        for col in NUMERIC_COLS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # sw_name 字段有时是 JSON 字典（含逗号），写入 CSV 前简化为纯文本，防止解析错误
        if "sw_name" in df.columns:
            df["sw_name"] = df["sw_name"].apply(_clean_sw_name)
        return df.dropna(subset=["bond_id", "price"])
    except Exception as e:
        logger.error("读取 %s 失败: %s", fp, e)
        return None


def _done_dates():
    if not HISTORY_FILE.exists():
        return set()
    try:
        return set(pd.read_csv(HISTORY_FILE, usecols=["date"])["date"].unique())
    except Exception:
        return set()


def append_to_history(csv_path: Path):
    date = _parse_date(csv_path)
    if not date:
        return False
    if date in _done_dates():
        logger.info("历史文件已包含 %s，跳过", date)
        return True
    df = _load_clean(csv_path, date)
    if df is None or df.empty:
        return False
    header = not HISTORY_FILE.exists()
    df.to_csv(HISTORY_FILE, mode="a", header=header, index=False, encoding="utf-8-sig")
    logger.info("已追加历史：%s（%d 条）", date, len(df))
    return True


def load_history():
    if not HISTORY_FILE.exists():
        return None
    # on_bad_lines='skip'：跳过字段数异常的行（通常是 sw_name 含逗号导致）
    df = pd.read_csv(HISTORY_FILE, encoding="utf-8-sig", on_bad_lines="skip")
    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ══════════════════════════════════════════════════════════════
#  二、策略选股函数构建
# ══════════════════════════════════════════════════════════════

def _exclude_pre_listed(df: pd.DataFrame) -> pd.DataFrame:
    """
    剔除预挂牌状态的转债（已公告但未正式上市交易）。
    集思录会在转债正式上市前就将其列入行情页，此时 price=100、increase_rt=0。
    这类债不应进入选股池，否则上市首日的暴涨会污染策略收益。
    过滤条件：price 恰好等于 100 且 increase_rt 为 0（或 NaN）。
    """
    if df.empty:
        return df
    mask_pre = (df["price"] == 100) & (df["increase_rt"].fillna(0) == 0)
    excluded = df[mask_pre]["bond_nm"].tolist()
    if excluded:
        logger.debug("剔除预挂牌转债：%s", excluded)
    return df[~mask_pre]


def _build_strategies(cfg: dict) -> dict:
    """
    根据 config.STRATEGY_CONFIG 动态构建选股函数。
    支持四种内置策略类型，通过 key 名称自动识别。
    """
    strategies = {}
    for name, c in cfg.items():
        n = c.get("top_n", 10)

        if "双低" in name:
            strategies[name] = {
                "desc":     c["desc"],
                "select":   lambda d, _n=n: (
                    d[d["year_left"].notna() & (d["year_left"] >= 0.5)]
                    .nsmallest(_n, "dblow")
                ),
                "sort_col": "dblow",
            }
        elif "低溢价率" in name:
            strategies[name] = {
                "desc":     c["desc"],
                "select":   lambda d, _n=n: (
                    d[(d["premium_rt"] >= 0) & (d["year_left"].notna()) & (d["year_left"] >= 0.5)]
                    .nsmallest(_n, "premium_rt")
                ),
                "sort_col": "premium_rt",
            }
        elif "小规模" in name:
            mp = c.get("max_premium_rt", 20)
            ms = c.get("max_size_yi", 5) * 10000
            strategies[name] = {
                "desc":     c["desc"],
                "select":   lambda d, _n=n, _mp=mp, _ms=ms: (
                    d[(d["premium_rt"] >= 0) & (d["premium_rt"] < _mp) & (d["curr_iss_amt"] < _ms)]
                    .nsmallest(_n, "premium_rt")
                ),
                "sort_col": "premium_rt",
            }
        elif "低价" in name:
            strategies[name] = {
                "desc":     c["desc"],
                "select":   lambda d, _n=n: (
                    d[(d["price"] > 0) & (d["year_left"].notna()) & (d["year_left"] >= 0.5)]
                    .nsmallest(_n, "price")
                ),
                "sort_col": "price",
            }
        else:
            logger.warning("未识别的策略类型：%s，已跳过", name)

    return strategies


# ══════════════════════════════════════════════════════════════
#  三、当日策略持仓表现
# ══════════════════════════════════════════════════════════════

def calc_strategy(df_today: pd.DataFrame, df_prev, strategies: dict) -> dict:
    """
    T+1 逻辑：用昨日数据选股，统计今日持仓涨跌幅。
    无昨日数据时退化为今日数据（快照模式，仅供参考）。
    注：用 df_prev 选股天然排除了今日新上市转债（昨日不存在即不入选股池），
    无需额外过滤新债首日。
    """
    results  = {}
    source   = df_prev if df_prev is not None else df_today
    source   = _exclude_pre_listed(source)   # 剔除预挂牌（price=100 涨跌=0）的转债
    disp_cols = ["bond_nm", "bond_id", "price", "increase_rt", "premium_rt", "dblow", "curr_iss_amt", "sw_name"]

    for name, cfg in strategies.items():
        sel = cfg["select"](source)
        if sel.empty:
            results[name] = {**cfg, "avg_return": None, "count": 0, "holdings": pd.DataFrame()}
            continue

        ids  = set(sel["bond_id"].astype(str))
        perf = df_today[df_today["bond_id"].astype(str).isin(ids)]
        h    = perf[[c for c in disp_cols if c in df_today.columns]].copy()
        sc   = cfg["sort_col"]
        if sc in h.columns:
            h = h.sort_values(sc)

        results[name] = {
            **cfg,
            "avg_return": round(perf["increase_rt"].mean(), 3) if not perf.empty else None,
            "max_return": round(perf["increase_rt"].max(), 3)  if not perf.empty else None,
            "min_return": round(perf["increase_rt"].min(), 3)  if not perf.empty else None,
            "count":      len(perf),
            "holdings":   h,
        }
    return results


# ══════════════════════════════════════════════════════════════
#  四、轮动净值回测
# ══════════════════════════════════════════════════════════════

def calc_rotation_nav(hist: pd.DataFrame, strategies: dict,
                      start_date: str) -> dict[str, list]:
    """
    从 start_date 起，按每日轮动（T日选股→T+1日持有）计算各策略净值曲线。

    返回：
        {
          "dates":  ["2026-04-02", "2026-04-03", ...],   # T+1日期（持有日）
          "策略名": [1.0, 1.008, 1.003, ...],             # 累计净值，起始为1.0
          ...
        }
    """
    if hist is None or hist.empty:
        return {}

    all_dates = sorted(hist["date"].unique())
    # 找出 >= start_date 的日期
    trade_dates = [d for d in all_dates if d >= start_date]

    if len(trade_dates) < 2:
        logger.warning("start_date=%s 之后的交易日不足2天，无法计算轮动净值", start_date)
        return {}

    result = {"dates": []}
    for name in strategies:
        result[name] = []

    nav = {name: 1.0 for name in strategies}  # 净值从1.0开始

    for i in range(len(trade_dates) - 1):
        t_date  = trade_dates[i]      # 选股日（T）
        t1_date = trade_dates[i + 1]  # 持有日（T+1）

        df_t  = hist[hist["date"] == t_date].copy()
        df_t1 = hist[hist["date"] == t1_date].copy()

        for col in NUMERIC_COLS:
            for df in [df_t, df_t1]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

        # 剔除预挂牌转债（price=100 且 increase_rt=0，尚未正式上市交易）
        df_t = _exclude_pre_listed(df_t)

        # 剔除新债首日：T日选股时，过滤掉在T前一个交易日不存在的转债
        # 防止新债上市首日的暴涨被纳入回测收益
        if i > 0:
            prev_date   = trade_dates[i - 1]
            ids_in_prev = set(hist[hist["date"] == prev_date]["bond_id"].astype(str))
            df_t = df_t[df_t["bond_id"].astype(str).isin(ids_in_prev)]
        # i==0（历史起始日）时不做过滤，无前一日数据可参考

        result["dates"].append(t1_date)

        for name, cfg in strategies.items():
            sel = cfg["select"](df_t)
            if sel.empty:
                # 无法选股，当日收益记0
                daily_r = 0.0
            else:
                ids      = set(sel["bond_id"].astype(str))
                held     = df_t1[df_t1["bond_id"].astype(str).isin(ids)]
                daily_r  = held["increase_rt"].mean() / 100 if not held.empty else 0.0

            nav[name] = round(nav[name] * (1 + daily_r), 6)
            result[name].append(nav[name])

    logger.info("轮动净值计算完成：%d 个交易日，%d 个策略", len(result["dates"]), len(strategies))
    return result


# ══════════════════════════════════════════════════════════════
#  五、自定义持仓收益追踪
# ══════════════════════════════════════════════════════════════

def calc_custom_portfolios(hist: pd.DataFrame, portfolios: list) -> list:
    """
    追踪用户自定义持仓的收益，支持换仓（多段持仓）。

    Config 格式：
      新格式（支持换仓）：
        {"name": "我的组合", "tranches": [
            {"start_date": "2026-04-01", "bonds": ["127113", ...]},   # 首段：建仓日，T+1起计收益
            {"start_date": "2026-04-10", "bonds": ["127114", ...]},   # 后续段：当日起计收益（含start_date当天）
        ]}
      旧格式（兼容，视为单段）：
        {"name": "我的组合", "start_date": "2026-04-01", "bonds": [...]}

    净值规则：
      - 各段净值链乘，反映组合从建仓起到今天的完整收益
      - 首段 start_date 为建仓日，该日不计收益，T+1 起开始累计
      - 后续段 start_date 当日已持新债，当日收益即计入新持仓

    返回：
        [
          {
            "name":         "我的自选组合A",
            "start_date":   "2026-04-01",   # 首段建仓日
            "bonds":        ["127114", ...], # 当前最新持仓
            "nav_dates":    ["2026-04-02", ...],
            "nav_values":   [1.0, 1.012, ...],
            "total_return": 0.023,
            "today_return": 0.005,
            "missing":      [],             # 当前持仓中未找到数据的代码
          },
          ...
        ]
    """
    if hist is None or hist.empty or not portfolios:
        return []

    all_dates = sorted(hist["date"].unique())
    output    = []

    for port in portfolios:
        name = port.get("name", "自定义组合")

        # 兼容旧格式（start_date + bonds）与新格式（tranches）
        if "tranches" in port:
            raw_tranches = port["tranches"]
        else:
            raw_tranches = [{"start_date": port.get("start_date", ""),
                             "bonds":      port.get("bonds", [])}]

        tranches = sorted(
            [{"start_date": t["start_date"],
              "bonds":      [str(b) for b in t.get("bonds", [])]}
             for t in raw_tranches if t.get("start_date") and t.get("bonds")],
            key=lambda x: x["start_date"]
        )
        if not tranches:
            continue

        first_start   = tranches[0]["start_date"]
        current_bonds = tranches[-1]["bonds"]   # 最新一段的持仓

        trade_dates = [d for d in all_dates if d >= first_start]
        if len(trade_dates) < 2:
            logger.warning("自定义持仓[%s] start_date=%s 之后数据不足", name, first_start)
            continue

        def _active_bonds(t1_date: str) -> list:
            """根据T+1日期返回应持有的债券列表。
            首段：T+1 > first_start，自然从第二个交易日起生效。
            后续段：t1_date >= tranche.start_date 时即切换（含换仓当日）。
            """
            active = tranches[0]["bonds"]
            for tranche in tranches:
                if tranche["start_date"] <= t1_date:
                    active = tranche["bonds"]
            return active

        nav        = 1.0
        nav_dates  = []
        nav_values = []

        for i in range(len(trade_dates) - 1):
            t1_date  = trade_dates[i + 1]
            bonds_t1 = _active_bonds(t1_date)

            df_t1 = hist[hist["date"] == t1_date].copy()
            if "increase_rt" in df_t1.columns:
                df_t1["increase_rt"] = pd.to_numeric(df_t1["increase_rt"], errors="coerce")

            held    = df_t1[df_t1["bond_id"].astype(str).isin(bonds_t1)]
            daily_r = held["increase_rt"].mean() / 100 if not held.empty else 0.0
            nav     = round(nav * (1 + daily_r), 6)
            nav_dates.append(t1_date)
            nav_values.append(nav)

        today_r = ((nav_values[-1] / nav_values[-2]) - 1) if len(nav_values) >= 2 else 0.0

        # 最新持仓明细 + missing（仅针对当前持仓段）
        holdings_df = pd.DataFrame()
        missing     = set()
        if nav_dates:
            latest_date = nav_dates[-1]
            df_latest   = hist[hist["date"] == latest_date].copy()
            for col in NUMERIC_COLS:
                if col in df_latest.columns:
                    df_latest[col] = pd.to_numeric(df_latest[col], errors="coerce")
            disp_cols   = ["bond_nm", "bond_id", "price", "increase_rt",
                           "premium_rt", "dblow", "curr_iss_amt", "sw_name"]
            held_latest = df_latest[df_latest["bond_id"].astype(str).isin(current_bonds)]
            holdings_df = held_latest[[c for c in disp_cols if c in held_latest.columns]].copy()
            found_ids   = set(held_latest["bond_id"].astype(str))
            for b in current_bonds:
                if b not in found_ids:
                    missing.add(b)

        output.append({
            "name":         name,
            "start_date":   first_start,
            "bonds":        current_bonds,
            "nav_dates":    nav_dates,
            "nav_values":   nav_values,
            "total_return": round(nav - 1, 6),
            "today_return": round(today_r, 6),
            "missing":      sorted(missing),
            "holdings":     holdings_df,
        })
        logger.info("自定义持仓[%s] 计算完成，总收益率 %.2f%%", name, (nav - 1) * 100)

    return output


# ══════════════════════════════════════════════════════════════
#  六、格式化工具
# ══════════════════════════════════════════════════════════════

def _f(val, suffix="%", dp=2, sign=True):
    try:
        v = float(val)
        s = "+" if (sign and v > 0) else ""
        return f"{s}{v:.{dp}f}{suffix}"
    except Exception:
        return "—"


def _cc(val):
    # ⚠️ 中国市场惯例：正数=up(红涨)，负数=down(绿跌)，禁止改为绿涨红跌
    try:
        v = float(val)
        return "up" if v > 0 else ("down" if v < 0 else "neutral")
    except Exception:
        return "neutral"


def _amt(val):
    try:
        v = float(val)
        return f"{v:.2f}亿"
    except Exception:
        return "—"


# ══════════════════════════════════════════════════════════════
#  七、HTML 仪表盘（含净值折线图）
# ══════════════════════════════════════════════════════════════

def _html_mover_rows(mdf: pd.DataFrame) -> str:
    out = ""
    for _, r in mdf.iterrows():
        out += (
            f"<tr>"
            f"<td><strong>{r.get('bond_nm','—')}</strong>"
            f"<span class='sub'> {r.get('bond_id','')}</span></td>"
            f"<td class='{_cc(r.get('increase_rt'))}'>{_f(r.get('increase_rt'))}</td>"
            f"<td>{_f(r.get('price',''), suffix='', sign=False)}</td>"
            f"<td>{_f(r.get('premium_rt',''))}</td>"
            f"<td>{_amt(r.get('curr_iss_amt'))}</td>"
            f"<td><span class='tag'>{r.get('sw_name','—')}</span></td>"
            f"</tr>"
        )
    return out


def _html_strategy_block(name: str, e: dict, has_prev: bool) -> str:
    avg, mx, mn = e.get("avg_return"), e.get("max_return"), e.get("min_return")
    cnt = e.get("count", 0)
    h   = e.get("holdings", pd.DataFrame())

    mx_h = (f'<div class="pi"><span class="label">最高</span>'
            f'<span class="value up">{_f(mx)}</span></div>') if mx is not None else ""
    mn_h = (f'<div class="pi"><span class="label">最低</span>'
            f'<span class="value down">{_f(mn)}</span></div>') if mn is not None else ""

    rows = ""
    for _, r in h.head(10).iterrows():
        db = r.get("dblow")
        try:   dbt = f"{float(db):.1f}"
        except: dbt = "—"
        rows += (
            f"<tr>"
            f"<td><strong>{r.get('bond_nm','—')}</strong></td>"
            f"<td>{_f(r.get('price',''), suffix='', sign=False)}</td>"
            f"<td class='{_cc(r.get('increase_rt'))}'>{_f(r.get('increase_rt'))}</td>"
            f"<td>{_f(r.get('premium_rt',''))}</td>"
            f"<td>{dbt}</td>"
            f"<td>{_amt(r.get('curr_iss_amt'))}</td>"
            f"<td><span class='tag'>{r.get('sw_name','—')}</span></td>"
            f"</tr>"
        )

    note = ("" if has_prev else
            '<p class="note">⚠️ 首次运行或无昨日数据，策略收益基于今日数据估算（非严格 T+1 回测）</p>')

    return (
        f'<div class="sc">'
        f'<div class="sh"><h3>{name}</h3><p class="sd">{e["desc"]}</p></div>'
        f'<div class="sp">'
        f'<div class="pi"><span class="label">今日均涨跌幅</span>'
        f'<span class="value {_cc(avg)}">{_f(avg) if avg is not None else "无数据"}</span></div>'
        f'<div class="pi"><span class="label">持仓数量</span><span class="value">{cnt} 只</span></div>'
        f'{mx_h}{mn_h}'
        f'</div>{note}'
        f'<div class="ht"><p class="table-title">持仓明细（前10只）</p>'
        f'<table><thead><tr>'
        f'<th>转债名称</th><th>现价</th><th>涨跌幅</th><th>溢价率</th><th>双低值</th><th>剩余规模</th><th>行业</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
        f'</div>'
    )


def _html_custom_port_block(port: dict) -> str:
    """生成自定义持仓的 HTML 展示块，风格与策略持仓块一致。"""
    name      = port.get("name", "自选持仓")
    total_ret = port.get("total_return", 0) * 100
    today_ret = port.get("today_return", 0) * 100
    nav_now   = port["nav_values"][-1] if port.get("nav_values") else None
    missing   = port.get("missing", [])
    h         = port.get("holdings", pd.DataFrame())

    nav_str = _f(nav_now, suffix="", dp=4, sign=False) if nav_now is not None else "—"

    rows = ""
    if not h.empty:
        for _, r in h.iterrows():
            db = r.get("dblow")
            try:   dbt = f"{float(db):.1f}"
            except: dbt = "—"
            rows += (
                f"<tr>"
                f"<td><strong>{r.get('bond_nm','—')}</strong>"
                f"<span class='sub'> {r.get('bond_id','')}</span></td>"
                f"<td>{_f(r.get('price',''), suffix='', sign=False)}</td>"
                f"<td class='{_cc(r.get('increase_rt'))}'>{_f(r.get('increase_rt'))}</td>"
                f"<td>{_f(r.get('premium_rt',''))}</td>"
                f"<td>{dbt}</td>"
                f"<td>{_amt(r.get('curr_iss_amt'))}</td>"
                f"<td><span class='tag'>{r.get('sw_name','—')}</span></td>"
                f"</tr>"
            )

    missing_note = ""
    if missing:
        missing_note = (f'<p class="note">⚠️ 以下代码在最新数据中未找到（可能已退市）：'
                        f'{", ".join(missing)}</p>')

    table_html = ""
    if rows:
        table_html = (
            f'<div class="ht"><p class="table-title">最新持仓明细（共 {len(h)} 只）</p>'
            f'<table><thead><tr>'
            f'<th>转债名称</th><th>现价</th><th>涨跌幅</th><th>溢价率</th>'
            f'<th>双低值</th><th>剩余规模</th><th>行业</th>'
            f'</tr></thead><tbody>{rows}</tbody></table></div>'
        )

    return (
        f'<div class="sc">'
        f'<div class="sh"><h3>【自选】{name}</h3>'
        f'<p class="sd">自定义持仓跟踪，固定持仓不换仓，等权计算净值</p></div>'
        f'<div class="sp">'
        f'<div class="pi"><span class="label">今日涨跌幅</span>'
        f'<span class="value {_cc(today_ret)}">{_f(today_ret)}</span></div>'
        f'<div class="pi"><span class="label">区间总收益</span>'
        f'<span class="value {_cc(total_ret)}">{_f(total_ret)}</span></div>'
        f'<div class="pi"><span class="label">当前净值</span>'
        f'<span class="value">{nav_str}</span></div>'
        f'<div class="pi"><span class="label">持仓数量</span>'
        f'<span class="value">{len(port.get("bonds", []))} 只</span></div>'
        f'</div>'
        f'{missing_note}'
        f'{table_html}'
        f'</div>'
    )


def _html_nav_chart(nav_data: dict, custom_ports: list) -> str:
    """生成净值折线图 HTML（用 Chart.js CDN，无服务器依赖）"""
    if not nav_data or not nav_data.get("dates"):
        return '<p style="color:var(--sub);padding:20px">净值数据不足，待积累更多历史数据后展示</p>'

    dates = nav_data["dates"]
    # 策略净值系列
    COLORS = ["#7b8cde", "#00c97a", "#f4c430", "#ff4d6d", "#c792ea", "#80deea"]
    datasets = []
    strategy_keys = [k for k in nav_data if k != "dates"]

    for i, name in enumerate(strategy_keys):
        vals  = nav_data[name]
        color = COLORS[i % len(COLORS)]
        datasets.append({
            "label":           name,
            "data":            vals,
            "borderColor":     color,
            "backgroundColor": color + "22",
            "borderWidth":     2,
            "pointRadius":     0,
            "tension":         0.3,
            "fill":            False,
        })

    # 自定义持仓系列
    for i, port in enumerate(custom_ports):
        if not port.get("nav_dates"):
            continue
        color = COLORS[(len(strategy_keys) + i) % len(COLORS)]
        # 对齐日期轴
        port_map = dict(zip(port["nav_dates"], port["nav_values"]))
        vals = [port_map.get(d) for d in dates]
        datasets.append({
            "label":           f"【自选】{port['name']}",
            "data":            vals,
            "borderColor":     color,
            "backgroundColor": color + "22",
            "borderWidth":     2,
            "borderDash":      [5, 3],
            "pointRadius":     0,
            "tension":         0.3,
            "fill":            False,
        })

    dates_json    = json.dumps(dates, ensure_ascii=False)
    datasets_json = json.dumps(datasets, ensure_ascii=False)

    # 最新净值摘要表
    summary_rows = ""
    for name in strategy_keys:
        vals = nav_data[name]
        if not vals:
            continue
        nav_now   = vals[-1]
        total_ret = (nav_now - 1) * 100
        today_ret = ((vals[-1] / vals[-2]) - 1) * 100 if len(vals) >= 2 else 0
        summary_rows += (
            f"<tr>"
            f"<td>{name}</td>"
            f"<td class='{_cc(today_ret)}'>{_f(today_ret)}</td>"
            f"<td class='{_cc(total_ret)}'>{_f(total_ret)}</td>"
            f"<td>{_f(nav_now, suffix='', dp=4, sign=False)}</td>"
            f"</tr>"
        )
    for port in custom_ports:
        if not port.get("nav_values"):
            continue
        total_ret = port["total_return"] * 100
        today_ret = port["today_return"] * 100
        nav_now   = port["nav_values"][-1]
        summary_rows += (
            f"<tr>"
            f"<td>【自选】{port['name']}</td>"
            f"<td class='{_cc(today_ret)}'>{_f(today_ret)}</td>"
            f"<td class='{_cc(total_ret)}'>{_f(total_ret)}</td>"
            f"<td>{_f(nav_now, suffix='', dp=4, sign=False)}</td>"
            f"</tr>"
        )

    return f"""
<div class="nav-wrap">
  <div class="nav-summary">
    <table>
      <thead><tr><th>策略</th><th>今日收益</th><th>区间总收益</th><th>当前净值</th></tr></thead>
      <tbody>{summary_rows}</tbody>
    </table>
  </div>
  <div class="nav-chart-box">
    <canvas id="navChart"></canvas>
  </div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
(function(){{
  var ctx = document.getElementById('navChart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: {dates_json},
      datasets: {datasets_json}
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{
          labels: {{ color: '#e2e6f0', font: {{ size: 12 }} }}
        }},
        tooltip: {{
          backgroundColor: '#22263a',
          titleColor: '#e2e6f0',
          bodyColor: '#7c85a2',
          callbacks: {{
            label: function(ctx) {{
              var v = ctx.parsed.y;
              if (v == null) return null;
              var ret = ((v - 1) * 100).toFixed(2);
              return ctx.dataset.label + ': ' + v.toFixed(4) + ' (' + (ret > 0 ? '+' : '') + ret + '%)';
            }}
          }}
        }}
      }},
      scales: {{
        x: {{
          ticks: {{ color: '#7c85a2', maxTicksLimit: 10, maxRotation: 0 }},
          grid:  {{ color: '#2e3348' }}
        }},
        y: {{
          ticks: {{
            color: '#7c85a2',
            callback: function(v) {{ return v.toFixed(3); }}
          }},
          grid: {{ color: '#2e3348' }}
        }}
      }}
    }}
  }});
}})();
</script>"""


def build_html_report(date_str: str, df: pd.DataFrame,
                      gainers: pd.DataFrame, losers: pd.DataFrame,
                      results: dict, has_prev: bool,
                      nav_data: dict, custom_ports: list,
                      start_date: str) -> str:

    avg  = df["increase_rt"].mean()
    up   = int((df["increase_rt"] > 0).sum())
    down = int((df["increase_rt"] < 0).sum())
    flat = len(df) - up - down
    sbody = "\n".join(_html_strategy_block(n, e, has_prev) for n, e in results.items())
    custom_body = "\n".join(_html_custom_port_block(p) for p in custom_ports if p.get("nav_values"))
    nav_html = _html_nav_chart(nav_data, custom_ports)

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>可转债日报 · {date_str}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ⚠️ 中国市场惯例：红涨绿跌，--up=红 --dn=绿，与国际默认相反，禁止修改颜色 */
:root{{--bg:#0f1117;--sf:#1a1d27;--sf2:#22263a;--bd:#2e3348;--tx:#e2e6f0;--sub:#7c85a2;--up:#f03e3e;--dn:#20c997;--ac:#7b8cde;--gd:#f4c430;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--tx);font-family:'Noto Serif SC',serif;line-height:1.7;padding-bottom:60px;}}
.hd{{background:linear-gradient(135deg,#1a1d27,#0f1117);border-bottom:1px solid var(--bd);padding:32px 40px 28px;position:relative;overflow:hidden;}}
.hd::before{{content:'';position:absolute;top:-60px;right:-60px;width:220px;height:220px;background:radial-gradient(circle,rgba(123,140,222,.15),transparent 70%);border-radius:50%;}}
.hd-date{{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--ac);letter-spacing:2px;margin-bottom:6px;}}
.hd h1{{font-size:28px;font-weight:700;letter-spacing:-.5px;}}
.hd h1 span{{color:var(--ac);}}
.mb{{display:flex;gap:28px;margin-top:20px;flex-wrap:wrap;}}
.mi{{display:flex;flex-direction:column;gap:2px;}}
.mi .label{{font-size:11px;color:var(--sub);}}
.mi .val{{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:500;}}
.ct{{max-width:980px;margin:0 auto;padding:0 24px;}}
.sec{{margin-top:36px;}}
.sec-title{{font-size:12px;font-weight:700;color:var(--sub);letter-spacing:2.5px;text-transform:uppercase;border-left:3px solid var(--ac);padding-left:10px;margin-bottom:16px;}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:16px;}}
.card{{background:var(--sf);border:1px solid var(--bd);border-radius:10px;overflow:hidden;}}
.card-t{{background:var(--sf2);padding:10px 16px;font-size:13px;font-weight:600;border-bottom:1px solid var(--bd);}}
table{{width:100%;border-collapse:collapse;font-size:13px;}}
th{{background:var(--sf2);color:var(--sub);font-size:11px;font-weight:600;letter-spacing:.5px;padding:8px 12px;text-align:left;border-bottom:1px solid var(--bd);}}
td{{padding:10px 12px;border-bottom:1px solid var(--bd);vertical-align:middle;}}
tr:last-child td{{border-bottom:none;}}
tr:hover td{{background:rgba(123,140,222,.05);}}
.sub{{font-size:11px;color:var(--sub);font-family:'JetBrains Mono',monospace;}}
.tag{{background:var(--sf2);border:1px solid var(--bd);border-radius:4px;padding:2px 6px;font-size:11px;color:var(--sub);white-space:nowrap;}}
.up{{color:var(--up);font-family:'JetBrains Mono',monospace;font-weight:500;}}
.down{{color:var(--dn);font-family:'JetBrains Mono',monospace;font-weight:500;}}
.neutral{{color:var(--sub);font-family:'JetBrains Mono',monospace;}}
.sc{{background:var(--sf);border:1px solid var(--bd);border-radius:10px;overflow:hidden;margin-bottom:20px;}}
.sh{{background:linear-gradient(90deg,var(--sf2),var(--sf));padding:14px 18px;border-bottom:1px solid var(--bd);}}
.sh h3{{font-size:15px;font-weight:700;color:var(--ac);}}
.sd{{font-size:12px;color:var(--sub);margin-top:4px;}}
.sp{{display:flex;gap:28px;padding:14px 18px;border-bottom:1px solid var(--bd);flex-wrap:wrap;}}
.pi{{display:flex;flex-direction:column;gap:2px;}}
.pi .label{{font-size:11px;color:var(--sub);}}
.pi .value{{font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:600;}}
.ht{{padding:14px 18px;}}
.table-title{{font-size:12px;color:var(--sub);margin-bottom:10px;}}
.note{{font-size:12px;color:var(--gd);background:rgba(244,196,48,.08);border-left:3px solid var(--gd);padding:8px 14px;}}
.nav-wrap{{background:var(--sf);border:1px solid var(--bd);border-radius:10px;overflow:hidden;padding:16px 18px;}}
.nav-summary{{margin-bottom:16px;}}
.nav-chart-box{{height:320px;position:relative;}}
.footer{{text-align:center;margin-top:48px;font-size:11px;color:var(--sub);}}
@media(max-width:640px){{.two{{grid-template-columns:1fr;}}.hd{{padding:20px;}}}}
</style>
</head>
<body>
<div class="hd">
  <div class="hd-date">📅 {date_str} · 可转债市场日报</div>
  <h1>可转债 <span>策略报告</span></h1>
  <div class="mb">
    <div class="mi"><span class="label">全市场均涨跌幅</span><span class="val {_cc(avg)}">{_f(avg)}</span></div>
    <div class="mi"><span class="label">上涨 / 下跌 / 持平</span>
      <span class="val" style="font-size:16px">
        <span class="up">{up}</span> / <span class="down">{down}</span> / <span class="neutral">{flat}</span>
      </span></div>
    <div class="mi"><span class="label">参与统计</span><span class="val">{len(df)} 只</span></div>
  </div>
</div>
<div class="ct">
  <div class="sec">
    <div class="sec-title">今日涨跌榜</div>
    <div class="two">
      <div class="card">
        <div class="card-t">🚀 涨幅前五</div>
        <table><thead><tr><th>转债</th><th>涨跌幅</th><th>现价</th><th>溢价率</th><th>规模</th><th>行业</th></tr></thead>
        <tbody>{_html_mover_rows(gainers)}</tbody></table>
      </div>
      <div class="card">
        <div class="card-t">📉 跌幅前五</div>
        <table><thead><tr><th>转债</th><th>涨跌幅</th><th>现价</th><th>溢价率</th><th>规模</th><th>行业</th></tr></thead>
        <tbody>{_html_mover_rows(losers)}</tbody></table>
      </div>
    </div>
  </div>
  <div class="sec"><div class="sec-title">今日策略持仓表现</div>{sbody}</div>
  {'<div class="sec"><div class="sec-title">自选持仓追踪</div>' + custom_body + '</div>' if custom_body else ''}
  <div class="sec">
    <div class="sec-title">策略净值曲线（{start_date} 起）</div>
    {nav_html}
  </div>
</div>
<div class="footer">数据来源：集思录 · 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════
#  七b、Excel 验证报告
# ══════════════════════════════════════════════════════════════

def build_excel_report(date_str: str, results: dict, nav_data: dict,
                       custom_ports: list, strategies: dict,
                       df_prev) -> Path:
    """
    生成 Excel 验证表，供手工回测核查：
      - Sheet "策略汇总"：所有策略+自选持仓的今日/区间数据汇总
      - Sheet "{策略名}"：每策略一张，昨日选股依据与今日实际涨跌幅对照，末行为均值
      - Sheet "净值历史"：每日日收益%与累计净值，含自选持仓，可直接用公式验证链乘
      - Sheet "[自选]{组合名}"：自选持仓今日持仓明细，末行为均值
    """
    date_raw = date_str.replace("-", "")
    xlsx_p = REPORT_DIR / f"cb_report_{date_raw}.xlsx"

    # 准备昨日数据（与 calc_strategy 保持一致：先剔除预挂牌）
    src_prev = _exclude_pre_listed(df_prev.copy()) if df_prev is not None else None

    with pd.ExcelWriter(xlsx_p, engine="openpyxl") as writer:

        # ── Sheet 1: 策略汇总 ────────────────────────────────
        summary_rows = []
        for name, e in results.items():
            nav_now   = None
            total_ret = None
            if nav_data and name in nav_data and nav_data[name]:
                nav_now   = nav_data[name][-1]
                total_ret = round((nav_now - 1) * 100, 4)
            summary_rows.append({
                "策略名称":     name,
                "描述":         e.get("desc", ""),
                "今日均涨跌幅%": e.get("avg_return"),
                "今日最高%":    e.get("max_return"),
                "今日最低%":    e.get("min_return"),
                "持仓数量":     e.get("count", 0),
                "区间总收益%":  total_ret,
                "当前净值":     nav_now,
            })
        for port in custom_ports:
            if not port.get("nav_values"):
                continue
            summary_rows.append({
                "策略名称":     f"【自选】{port['name']}",
                "描述":         "自定义持仓，固定不换仓",
                "今日均涨跌幅%": round(port["today_return"] * 100, 4),
                "今日最高%":    None,
                "今日最低%":    None,
                "持仓数量":     len(port.get("bonds", [])),
                "区间总收益%":  round(port["total_return"] * 100, 4),
                "当前净值":     port["nav_values"][-1],
            })
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="策略汇总", index=False)

        # ── Sheet per strategy: 昨日选股依据 + 今日实际收益 ──
        for name, e in results.items():
            cfg     = strategies.get(name)
            h_today = e.get("holdings", pd.DataFrame()).copy()

            if src_prev is not None and cfg is not None and not h_today.empty:
                sel_prev  = cfg["select"](src_prev).copy()
                sort_col  = cfg.get("sort_col", "")

                # 昨日选股数据：保留关键列并重命名为"昨日_"
                prev_keep = ["bond_id", "bond_nm", "sw_name"]
                for c in [sort_col, "dblow", "premium_rt", "price", "curr_iss_amt", "year_left"]:
                    if c in sel_prev.columns and c not in prev_keep:
                        prev_keep.append(c)
                sel_prev = sel_prev[[c for c in prev_keep if c in sel_prev.columns]].copy()
                sel_prev = sel_prev.rename(columns={
                    c: f"昨日_{c}"
                    for c in sel_prev.columns
                    if c not in ("bond_id", "bond_nm", "sw_name")
                })

                # 今日数据：只取数值列 + bond_id，避免与昨日列重名
                today_num = ["bond_id", "increase_rt", "price", "premium_rt"]
                h_sub = h_today[[c for c in today_num if c in h_today.columns]].copy()
                h_sub = h_sub.rename(columns={
                    c: f"今日_{c}" for c in h_sub.columns if c != "bond_id"
                })

                merged = sel_prev.merge(h_sub, on="bond_id", how="left")
            else:
                # 无昨日数据时直接展示今日持仓
                merged = h_today.copy()
                merged = merged.rename(columns={
                    c: f"今日_{c}"
                    for c in merged.columns
                    if c not in ("bond_id", "bond_nm", "sw_name")
                })

            # 末行：均值（仅填今日涨跌幅，即报告中的 avg_return）
            avg_rt = e.get("avg_return")
            rt_col = "今日_increase_rt" if "今日_increase_rt" in merged.columns else None
            avg_row = {"bond_nm": "【均值】"}
            if rt_col and avg_rt is not None:
                avg_row[rt_col] = avg_rt
            avg_df = pd.DataFrame([avg_row], columns=merged.columns)
            merged = pd.concat([merged, avg_df], ignore_index=True)

            merged.to_excel(writer, sheet_name=name[:31], index=False)

        # ── Sheet: 净值历史 ──────────────────────────────────
        if nav_data and nav_data.get("dates"):
            dates         = nav_data["dates"]
            strategy_keys = [k for k in nav_data if k != "dates"]
            nav_rows      = []

            for i, d in enumerate(dates):
                row = {"日期": d}
                for sname in strategy_keys:
                    vals    = nav_data[sname]
                    nav_now = vals[i] if i < len(vals) else None
                    if i == 0:
                        daily_ret = round((vals[0] - 1.0) * 100, 4) if vals else None
                    else:
                        daily_ret = round((vals[i] / vals[i - 1] - 1) * 100, 4) if vals and vals[i - 1] else None
                    row[f"{sname}_日收益%"]  = daily_ret
                    row[f"{sname}_累计净值"] = nav_now

                for port in custom_ports:
                    if not port.get("nav_dates"):
                        continue
                    port_map = dict(zip(port["nav_dates"], port["nav_values"]))
                    nav_v    = port_map.get(d)
                    prev_nav = port_map.get(dates[i - 1]) if i > 0 else None
                    if nav_v is not None and prev_nav is not None:
                        port_daily = round((nav_v / prev_nav - 1) * 100, 4)
                    elif nav_v is not None and i == 0:
                        port_daily = round((nav_v - 1.0) * 100, 4)
                    else:
                        port_daily = None
                    pname = f"【自选】{port['name']}"
                    row[f"{pname}_日收益%"]  = port_daily
                    row[f"{pname}_累计净值"] = nav_v

                nav_rows.append(row)

            pd.DataFrame(nav_rows).to_excel(writer, sheet_name="净值历史", index=False)

        # ── Sheet per custom portfolio: 今日持仓明细 ─────────
        for port in custom_ports:
            if not port.get("nav_values"):
                continue
            h = port.get("holdings", pd.DataFrame())
            if h.empty:
                continue
            h_out = h.copy()
            h_out = h_out.rename(columns={
                c: f"今日_{c}"
                for c in h_out.columns
                if c not in ("bond_nm", "bond_id", "sw_name")
            })
            # 末行：今日均收益
            today_rt = round(port["today_return"] * 100, 4)
            avg_row  = {"bond_nm": "【均值】"}
            if "今日_increase_rt" in h_out.columns:
                avg_row["今日_increase_rt"] = today_rt
            avg_df = pd.DataFrame([avg_row], columns=h_out.columns)
            h_out  = pd.concat([h_out, avg_df], ignore_index=True)

            sheet_name = f"自选_{port['name']}"[:31]
            h_out.to_excel(writer, sheet_name=sheet_name, index=False)

    return xlsx_p


# ══════════════════════════════════════════════════════════════
#  八、自动结论生成
# ══════════════════════════════════════════════════════════════

def _auto_conclusion(df: pd.DataFrame, results: dict,
                     gainers: pd.DataFrame, has_prev: bool) -> str:
    avg   = df["increase_rt"].mean()
    up    = int((df["increase_rt"] > 0).sum())
    down  = int((df["increase_rt"] < 0).sum())
    total = len(df)
    ratio = up / total if total else 0

    mood = ("普涨格局" if ratio >= 0.75 else
            "偏多格局" if ratio >= 0.6 else
            "震荡分化" if ratio >= 0.45 else "偏弱格局")
    verb = ("市场今日全面走强" if ratio >= 0.75 else
            "市场今日整体偏强" if ratio >= 0.6 else
            "市场今日涨跌互现，分化明显" if ratio >= 0.45 else "市场今日整体承压")
    avg_word = "上涨" if avg > 0 else "下跌"

    market_para = (
        f"今日可转债市场呈{mood}，{up} 只上涨、{down} 只下跌，"
        f"全市场均{avg_word} **{_f(abs(avg), sign=False)}**。"
    )

    valid = {n: e for n, e in results.items() if e.get("avg_return") is not None}
    strategy_para = ""
    if valid:
        ranked  = sorted(valid.items(), key=lambda x: x[1]["avg_return"], reverse=True)
        best_n, best_e   = ranked[0]
        worst_n, worst_e = ranked[-1]
        spread = best_e["avg_return"] - worst_e["avg_return"]

        if spread < 0.3:
            strategy_para = (
                f"策略层面，各策略今日表现趋同，分化不明显，"
                f"**{best_n}** 略占优（{_f(best_e['avg_return'])}）。"
            )
        else:
            strategy_para = (
                f"策略层面，**{best_n}** 今日表现最佳（{_f(best_e['avg_return'])}），"
                f"**{worst_n}** 相对偏弱（{_f(worst_e['avg_return'])}），"
                f"两者相差 {_f(spread)} 个百分点。"
            )
        best_diff = best_e["avg_return"] - avg
        if best_diff > 0.5:
            strategy_para += f" **{best_n}** 跑赢大市均值 {_f(best_diff)}，今日占优明显。"
        elif best_e["avg_return"] < avg - 0.5:
            strategy_para += " 各策略整体跑输大市，今日高弹性品种表现偏弱。"

    top_para = ""
    if not gainers.empty:
        r  = gainers.iloc[0]
        nm = r.get("bond_nm", "—")
        rt = r.get("increase_rt")
        try:
            prem = float(r.get("premium_rt"))
            size = float(r.get("curr_iss_amt"))
            sw   = r.get("sw_name", "")
            size_desc = ("超小盘（规模极小）" if size < 5000 else
                         f"小盘（规模 {size/10000:.1f} 亿）" if size < 20000 else
                         f"规模 {size/10000:.1f} 亿")
            prem_desc = (f"负溢价（{_f(prem)}）" if prem < 0 else
                         f"低溢价（{_f(prem)}）" if prem < 10 else
                         f"中等溢价（{_f(prem)}）" if prem < 30 else
                         f"高溢价（{_f(prem)}）")
            hint = ""
            if size < 50000 and 0 <= prem < 20:
                hint = "，与小规模低溢价策略逻辑吻合"
            elif prem < 10:
                hint = "，与低溢价率策略选股方向一致"
            top_para = (
                f"个券方面，**{nm}** 以 {_f(rt)} 领涨，"
                f"属于{size_desc}、{prem_desc}{hint}。"
                + (f"所属行业：{sw}。" if sw else "")
            )
        except Exception:
            top_para = f"个券方面，**{nm}** 以 {_f(rt)} 领涨今日榜单。"

    note = ("" if has_prev else
            "\n\n> ⚠️ 注：当前为首次运行，策略收益基于今日数据计算，非严格 T+1 回测，仅供参考。")

    paras = [p for p in [market_para, strategy_para, top_para] if p]
    return "\n\n".join(paras) + note


# ══════════════════════════════════════════════════════════════
#  九、Markdown 报告
# ══════════════════════════════════════════════════════════════

def build_markdown_report(date_str: str, df: pd.DataFrame,
                          gainers: pd.DataFrame, losers: pd.DataFrame,
                          results: dict, has_prev: bool,
                          nav_data: dict, custom_ports: list,
                          start_date: str) -> str:
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    avg  = df["increase_rt"].mean()
    up   = int((df["increase_rt"] > 0).sum())
    down = int((df["increase_rt"] < 0).sum())
    flat = len(df) - up - down

    def mover_tbl(mdf):
        rows = ["| 转债名称 | 涨跌幅 | 现价 | 溢价率 | 剩余规模 | 行业 |",
                "|---------|--------|------|--------|---------|------|"]
        for _, r in mdf.iterrows():
            rows.append(
                f"| {r.get('bond_nm','—')} "
                f"| {_f(r.get('increase_rt'))} "
                f"| {_f(r.get('price',''), suffix='', sign=False)} "
                f"| {_f(r.get('premium_rt',''))} "
                f"| {_amt(r.get('curr_iss_amt'))} "
                f"| {r.get('sw_name','—')} |"
            )
        return "\n".join(rows)

    def strategy_sec(name, e):
        avg_r = e.get("avg_return")
        mx    = e.get("max_return")
        mn    = e.get("min_return")
        cnt   = e.get("count", 0)
        h     = e.get("holdings", pd.DataFrame())
        perf  = (f"今日均涨跌幅 **{_f(avg_r) if avg_r is not None else '无数据'}**，"
                 f"持仓 {cnt} 只，最高 {_f(mx) if mx is not None else '—'}，"
                 f"最低 {_f(mn) if mn is not None else '—'}")
        htbl  = ""
        if not h.empty:
            rows = ["| 转债名称 | 现价 | 涨跌幅 | 溢价率 | 双低值 | 剩余规模 | 行业 |",
                    "|---------|------|--------|--------|--------|---------|------|"]
            for _, r in h.head(10).iterrows():
                db = r.get("dblow")
                try:   dbt = f"{float(db):.1f}"
                except: dbt = "—"
                rows.append(
                    f"| {r.get('bond_nm','—')} "
                    f"| {_f(r.get('price',''), suffix='', sign=False)} "
                    f"| {_f(r.get('increase_rt'))} "
                    f"| {_f(r.get('premium_rt',''))} "
                    f"| {dbt} "
                    f"| {_amt(r.get('curr_iss_amt'))} "
                    f"| {r.get('sw_name','—')} |"
                )
            htbl = "\n\n**持仓明细（前10只）**\n\n" + "\n".join(rows)
        return f"### {name}\n\n{e['desc']}\n\n{perf}{htbl}"

    # 净值汇总表（文字版）
    def nav_summary_md():
        if not nav_data or not nav_data.get("dates"):
            return "> 净值数据不足，待积累更多历史数据后展示"
        rows = [f"区间：**{start_date}** 至 **{nav_data['dates'][-1]}**，共 {len(nav_data['dates'])} 个交易日\n",
                "| 策略 | 今日收益 | 区间总收益 | 当前净值 |",
                "|------|---------|-----------|---------|"]
        for name in [k for k in nav_data if k != "dates"]:
            vals = nav_data[name]
            if not vals:
                continue
            nav_now   = vals[-1]
            total_ret = (nav_now - 1) * 100
            today_ret = ((vals[-1] / vals[-2]) - 1) * 100 if len(vals) >= 2 else 0
            rows.append(f"| {name} | {_f(today_ret)} | {_f(total_ret)} | {_f(nav_now, suffix='', dp=4, sign=False)} |")
        for port in custom_ports:
            if not port.get("nav_values"):
                continue
            total_ret = port["total_return"] * 100
            today_ret = port["today_return"] * 100
            nav_now   = port["nav_values"][-1]
            rows.append(f"| 【自选】{port['name']} | {_f(today_ret)} | {_f(total_ret)} | {_f(nav_now, suffix='', dp=4, sign=False)} |")
        return "\n".join(rows)

    parts = [
        f"# 可转债日报 · {date_str}",
        "",
        f"> 数据来源：集思录 | 生成时间：{now}",
        "",
        "## 📌 今日速览",
        "",
        _auto_conclusion(df, results, gainers, has_prev),
        "",
        "## 📊 市场概况",
        "",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 参与统计数量 | {len(df)} 只 |",
        f"| 全市场均涨跌幅 | {_f(avg)} |",
        f"| 上涨 / 下跌 / 持平 | {up} / {down} / {flat} |",
        "",
        "## 🚀 今日涨幅前五",
        "",
        mover_tbl(gainers),
        "",
        "## 📉 今日跌幅前五",
        "",
        mover_tbl(losers),
        "",
        "## 📈 今日策略持仓表现",
        "",
    ]
    for name, e in results.items():
        parts.append(strategy_sec(name, e))
        parts.append("")

    # 自定义持仓区段（含持仓明细表格）
    active_ports = [p for p in custom_ports if p.get("nav_values")]
    if active_ports:
        parts += ["## 📋 自选持仓追踪", ""]
        for port in active_ports:
            total_ret = port["total_return"] * 100
            today_ret = port["today_return"] * 100
            nav_now   = port["nav_values"][-1]
            h         = port.get("holdings", pd.DataFrame())
            missing   = port.get("missing", [])

            perf = (f"今日涨跌幅 **{_f(today_ret)}**，"
                    f"区间总收益 **{_f(total_ret)}**，"
                    f"当前净值 **{_f(nav_now, suffix='', dp=4, sign=False)}**，"
                    f"持仓 {len(port.get('bonds', []))} 只")

            parts.append(f"### 【自选】{port['name']}")
            parts.append("")
            parts.append(perf)

            if missing:
                parts.append(f"\n> ⚠️ 以下代码在最新数据中未找到（可能已退市）：{', '.join(missing)}")

            if not h.empty:
                rows = ["| 转债名称 | 代码 | 现价 | 涨跌幅 | 溢价率 | 双低值 | 剩余规模 | 行业 |",
                        "|---------|------|------|--------|--------|--------|---------|------|"]
                for _, r in h.iterrows():
                    db = r.get("dblow")
                    try:   dbt = f"{float(db):.1f}"
                    except: dbt = "—"
                    rows.append(
                        f"| {r.get('bond_nm','—')} "
                        f"| {r.get('bond_id','')} "
                        f"| {_f(r.get('price',''), suffix='', sign=False)} "
                        f"| {_f(r.get('increase_rt'))} "
                        f"| {_f(r.get('premium_rt',''))} "
                        f"| {dbt} "
                        f"| {_amt(r.get('curr_iss_amt'))} "
                        f"| {r.get('sw_name','—')} |"
                    )
                parts.append("\n**持仓明细**\n")
                parts.append("\n".join(rows))
            parts.append("")

    parts += [
        f"## 💹 策略净值回顾（{start_date} 起）",
        "",
        nav_summary_md(),
        "",
        "---",
        "",
        "*本报告由程序自动生成，数据来源集思录，仅供参考，不构成投资建议。*",
        "",
        f"*发布时间：{now}*",
    ]

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════
#  十、推送
# ══════════════════════════════════════════════════════════════

def send_email(html: str, date_str: str, cfg: dict):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"【可转债日报】{date_str}"
    msg["From"]    = cfg["sender"]
    msg["To"]      = ", ".join(cfg["receivers"])
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.login(cfg["sender"], cfg["password"])
            s.sendmail(cfg["sender"], cfg["receivers"], msg.as_string())
        logger.info("邮件推送成功")
    except Exception as e:
        logger.error("邮件推送失败: %s", e)


def send_wechat(results: dict, gainers: pd.DataFrame, losers: pd.DataFrame,
                date_str: str, market_avg: float, nav_data: dict,
                custom_ports: list, key: str):
    if not key or key == "your_serverchan_key_here":
        logger.warning("未配置 SERVERCHAN_KEY，跳过")
        return
    lines = [f"## 📊 可转债日报 · {date_str}",
             f"**全市场均涨跌幅：{_f(market_avg)}**", ""]
    lines += ["### 🚀 涨幅前五"]
    for _, r in gainers.head(5).iterrows():
        lines.append(f"- {r['bond_nm']}  **{_f(r['increase_rt'])}**  溢价率{r.get('premium_rt','—')}%")
    lines += ["", "### 📉 跌幅前五"]
    for _, r in losers.head(5).iterrows():
        lines.append(f"- {r['bond_nm']}  **{_f(r['increase_rt'])}**  溢价率{r.get('premium_rt','—')}%")
    lines += ["", "### 📈 策略表现"]
    for name, e in results.items():
        avg = e.get("avg_return")
        lines.append(f"- **{name}**：{_f(avg) if avg is not None else '无数据'}（{e.get('count',0)}只）")
    if nav_data and nav_data.get("dates"):
        lines += ["", "### 💹 区间净值"]
        for name in [k for k in nav_data if k != "dates"]:
            vals = nav_data[name]
            if vals:
                lines.append(f"- {name}：{_f((vals[-1]-1)*100)} 累计")
        for port in custom_ports:
            if port.get("nav_values"):
                lines.append(f"- 【自选】{port['name']}：{_f(port['total_return']*100)} 累计")
    try:
        resp = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": f"可转债日报·{date_str}", "desp": "\n".join(lines)},
            timeout=10,
        )
        if resp.json().get("code") == 0:
            logger.info("微信推送成功")
        else:
            logger.error("微信推送失败: %s", resp.text)
    except Exception as e:
        logger.error("微信推送异常: %s", e)


# ══════════════════════════════════════════════════════════════
#  十一、主流程入口
# ══════════════════════════════════════════════════════════════

def run_after_spider(csv_path):
    csv_path = Path(csv_path)
    logger.info("=== cb_analyzer 启动 ===")

    # ① 追加历史
    append_to_history(csv_path)

    # ② 载入今日数据
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        for col in NUMERIC_COLS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["bond_id", "price", "increase_rt"])
    except Exception as e:
        logger.error("读取 CSV 失败: %s", e)
        return None

    date_str = _parse_date(csv_path) or datetime.now().strftime("%Y-%m-%d")
    date_raw = date_str.replace("-", "")

    # ③ 载入昨日数据
    df_prev = None
    hist    = load_history()
    if hist is not None:
        prev_dates = sorted(d for d in hist["date"].unique() if d < date_str)
        if prev_dates:
            df_prev = hist[hist["date"] == prev_dates[-1]].copy()
            for col in NUMERIC_COLS:
                if col in df_prev.columns:
                    df_prev[col] = pd.to_numeric(df_prev[col], errors="coerce")

    has_prev = df_prev is not None
    logger.info("昨日数据：%s", "有" if has_prev else "无")

    # ④ 读取配置
    try:
        from config import STRATEGY_CONFIG, ROTATION_START_DATE, CUSTOM_PORTFOLIOS
    except ImportError:
        STRATEGY_CONFIG = {
            "双低策略":         {"desc": "双低值最低前10只", "top_n": 10},
            "低溢价率策略":     {"desc": "溢价率最低前10只", "top_n": 10},
            "小规模低溢价策略": {"desc": "溢价率<20%且规模<5亿", "max_premium_rt": 20, "max_size_yi": 5, "top_n": 10},
            "低价策略":         {"desc": "现价最低前10只", "top_n": 10},
        }
        ROTATION_START_DATE = "2026-04-01"
        CUSTOM_PORTFOLIOS   = []

    strategies = _build_strategies(STRATEGY_CONFIG)
    pcols      = ["bond_nm", "bond_id", "price", "increase_rt", "premium_rt", "curr_iss_amt", "sw_name"]
    gainers    = df.nlargest(5,  "increase_rt")[[c for c in pcols if c in df.columns]]
    losers     = df.nsmallest(5, "increase_rt")[[c for c in pcols if c in df.columns]]
    results    = calc_strategy(df, df_prev, strategies)

    # ⑤ 轮动净值计算
    nav_data     = calc_rotation_nav(hist, strategies, ROTATION_START_DATE) if hist is not None else {}
    custom_ports = calc_custom_portfolios(hist, CUSTOM_PORTFOLIOS)         if hist is not None else []

    # ⑥ 生成报告
    html   = build_html_report(date_str, df, gainers, losers, results, has_prev,
                               nav_data, custom_ports, ROTATION_START_DATE)
    html_p = REPORT_DIR / f"cb_report_{date_raw}.html"
    html_p.write_text(html, encoding="utf-8")
    logger.info("HTML 仪表盘: %s", html_p)

    md_text = build_markdown_report(date_str, df, gainers, losers, results, has_prev,
                                    nav_data, custom_ports, ROTATION_START_DATE)
    md_p    = REPORT_DIR / f"cb_report_{date_raw}.md"
    md_p.write_text(md_text, encoding="utf-8")
    logger.info("Markdown:    %s", md_p)

    xlsx_p = build_excel_report(date_str, results, nav_data, custom_ports,
                                strategies, df_prev)
    logger.info("Excel 验证表: %s", xlsx_p)

    # ⑦ 推送
    try:
        from config import SEND_EMAIL, SEND_WECHAT, EMAIL_CONFIG, SERVERCHAN_KEY
    except ImportError:
        SEND_EMAIL = SEND_WECHAT = False
        EMAIL_CONFIG = {}
        SERVERCHAN_KEY = ""

    if SEND_EMAIL:
        send_email(html, date_str, EMAIL_CONFIG)
    if SEND_WECHAT:
        send_wechat(results, gainers, losers, date_str,
                    df["increase_rt"].mean(), nav_data, custom_ports, SERVERCHAN_KEY)

    logger.info("=== cb_analyzer 完成 ===")
    return html_p


def main():
    files = sorted(DAILY_DIR.glob("cb_list_*.csv"))
    if not files:
        print(f"未找到 CSV 文件，请将数据放入: {DAILY_DIR}")
        return
    logger.info("独立运行，分析最新文件: %s", files[-1])
    run_after_spider(files[-1])


if __name__ == "__main__":
    main()
