"""数字归属核对（M18）：数字前面最近的名字是不是它自己的。用手写的小工具结果，每条规则一个例子。"""

import json

from da_agent.attribution import build_index, check_attribution, nearest_name

SUMMARY = {"tool": "metric_summary", "metrics": [
    {"metric": "gmv", "label": "GMV", "current": 120064.77, "base": 144932.97, "change_pct": -0.1716},
    {"metric": "net_sales", "label": "净销售额", "current": 118850.04, "base": 142980.77, "change_pct": -0.1688},
    {"metric": "orders", "label": "订单数", "current": 288, "base": 325, "change_pct": -0.1138},
    {"metric": "aov", "label": "客单价", "current": 416.89, "base": 445.95, "change_pct": -0.0652},
    {"metric": "orders_per_customer", "label": "人均订单数", "current": 1.2857, "base": 1.2745, "change_pct": 0.0088},
    {"metric": "gmv_per_day", "label": "日均 GMV", "current": 20010.8, "base": 24155.5, "change_pct": -0.1716}],
    "warnings": ["部分分组订单数少于 30，变化可能是偶然波动"]}
DRILL = {"tool": "drilldown", "metric": "gmv", "metric_label": "GMV", "dimension": "country",
         "total": {"base": 144932.97, "current": 120064.77, "change": -24868.2},
         "segments": [
             {"segment": "United Kingdom", "base": 110861.51, "current": 98233.4, "change": -12628.11, "base_orders": 291,
              "current_orders": 263, "share_of_total_change": 0.5078},
             {"segment": "Netherlands", "base": 18570.88, "current": 1542.3, "change": -17028.58, "base_orders": 11,
              "current_orders": 14, "share_of_total_change": 0.6848}],
         "warnings": ["Netherlands 基期只有 11 单"]}
DECOMPOSE = {"tool": "decompose_gmv", "contributions": [
    {"factor": "active_customers", "label": "活跃客户数", "shapley": -13310.62, "share_of_total_change": 0.5352},
    {"factor": "identified_aov", "label": "可识别客户客单价", "shapley": -7748.52, "share_of_total_change": 0.3116}]}
QUALITY = {"tool": "data_quality_overview", "clean_totals": {"gmv": 19643861.64, "gmv_missing_customer_share": 0.1311},
           "extreme_lines": [{"invoice": "541431", "description": "MEDIUM CERAMIC TOP STORAGE JAR", "amount": 77183.6,
                              "invoice_date": "2011-01-18 10:01:00"}]}
PEERS = {"tool": "peer_compare", "rows": [
    {"company": "pdd", "name": "拼多多（PDD Holdings）", "net_margin": 0.2266, "asset_turnover": 0.7609},
    {"company": "jd", "name": "京东", "net_margin": 0.015, "asset_turnover": 1.8789}]}
RATIOS = {"tool": "financial_summary", "company": "pdd", "name": "拼多多（PDD Holdings）", "ratios": [
    {"ratio": "net_margin", "label": "归母净利率", "current": 0.2266}, {"ratio": "asset_turnover", "label": "总资产周转率", "current": 0.7609}],
    "items": [{"item": "revenue", "label": "营业收入", "current": 431845713000, "in_yi": {"current": "4,318.46 亿元"}}]}
CALC = {"tool": "calculate", "expression": "120064.77 - 98233.4", "purpose": "英国以外的 GMV", "result": 21831.37}


def messages(*tools: dict) -> list[dict]:
    out = [{"role": "system", "content": "数据范围到 2011-12-09。"}, {"role": "user", "content": "2010-W05 怎么样？样本 5000 人。"}]
    return out + [{"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps(t, ensure_ascii=False)} for i, t in enumerate(tools)]


def flags(answer: str, *tools: dict) -> list[dict]:
    return check_attribution(answer, messages(*tools))["flags"]


def test_a_number_next_to_its_own_name_is_fine() -> None:
    answer = ("GMV 为 120,064.77，环比 -17.16%；净销售额 118,850.04。\n"
              "订单 288 单（上周 325 单），orders_per_customer 基本持平（1.2857）。\n"  # 名字只写一部分、写代号都认
              "英国 GMV 从 110,861.51 降到 98,233.40，订单 291 → 263。\n"  # 国家写中文也认；表的名字（GMV）也是它自己的
              "可识别客户客单价贡献 -7,748.52，客单价 416.89。")
    result = check_attribution(answer, messages(SUMMARY, DRILL, DECOMPOSE))
    assert result["flags"] == [] and result["checked"] >= 10


def test_a_number_next_to_another_name_is_flagged() -> None:
    found = flags("本周净销售额为 120,064.77，订单数 288。", SUMMARY)
    assert [(f["kind"], f["text"], f["belongs_to"], f["sentence_says"]) for f in found] == [
        ("对象", "120,064.77", ["GMV"], "净销售额")]
    assert found[0]["sources"] == [{"call": 1, "tool": "metric_summary", "field": "current"}]
    country = flags("荷兰 GMV 从 110,861.51 降到 98,233.40。", SUMMARY, DRILL)  # 这是英国的数
    assert [(f["text"], f["sentence_says"]) for f in country] == [("110,861.51", "Netherlands")]
    assert country[0]["belongs_to"] == ["GMV", "United Kingdom"]


def test_the_table_subject_counts_as_a_name() -> None:
    """下钻的是 GMV：把英国那一行的数说成“英国的净销售额”是错的；合计也带着这张表的名字。"""
    assert [f["sentence_says"] for f in flags("英国的净销售额 98,233.40。", SUMMARY, DRILL)] == ["净销售额"]
    assert flags("英国的 GMV 98,233.40，全站 GMV 变化 -24,868.20。", SUMMARY, DRILL) == []


def test_column_names_come_from_codes_seen_elsewhere() -> None:
    """同行对比表里字段就是指标：net_margin 的名字从财报摘要的 {ratio, label} 学到。"""
    assert flags("拼多多的归母净利率 22.66%，京东只有 1.50%。", RATIOS, PEERS) == []
    wrong = flags("拼多多的总资产周转率 22.66%。", RATIOS, PEERS)
    assert [(f["text"], f["sentence_says"]) for f in wrong] == [("22.66%", "总资产周转率")]
    assert "归母净利率" in wrong[0]["belongs_to"] and "拼多多（PDD Holdings）" in wrong[0]["belongs_to"]
    assert flags("PDD 营业收入 4,318.46 亿元。", RATIOS) == []  # 括号里的英文名、行内文字里的数都认


def test_longer_name_wins_and_ties_go_to_the_numbers_own_name() -> None:
    assert [f["sentence_says"] for f in flags("日均 GMV 120,064.77。", SUMMARY)] == ["日均 GMV"]  # 周 GMV 被说成了日均
    assert flags("客单价贡献 -7,748.52。", SUMMARY, DECOMPOSE) == []  # “客单价”是别的指标的全名，也是“可识别客户客单价”的一部分
    index = build_index(messages(SUMMARY, DRILL))
    gmv_uk = frozenset({("metric", "GMV"), ("segment", "United Kingdom")})
    assert nearest_name("本周的 GMV 和净销售额 ", gmv_uk, index, "metric") == ("净销售额", False)
    assert nearest_name("本周的净销售额和 GMV ", gmv_uk, index, "metric") == ("GMV", True)
    assert nearest_name("本周合计 ", gmv_uk, index, "metric") is None
    # 每一类各看各的：指标名对了，分组名错了，也要指出来
    assert nearest_name("荷兰 GMV 从 ", gmv_uk, index, "metric") == ("GMV", True)
    assert nearest_name("荷兰 GMV 从 ", gmv_uk, index, "segment") == ("Netherlands", False)


def test_it_stays_silent_when_it_cannot_be_sure() -> None:
    # 名字隔了一个分句（逗号、括号）或者离得太远：不标记
    assert flags("净销售额下降明显，120,064.77 这个数要结合取消看。", SUMMARY) == []
    assert flags("净销售额（120,064.77）。", SUMMARY) == []
    assert flags("净销售额这一项在本周的全部商品销售记录里经过清洗和汇总之后得到的结果是 120,064.77。", SUMMARY) == []
    # 有一处出处没有名字：警告里的 30、calculate 算出来的数、用户问题里的数
    assert flags("净销售额 30 以下，净销售额 21,831.37，净销售额 5000。", SUMMARY, CALC) == []
    # 表格里的数不查对象
    assert flags("| 指标 | 本周 |\n|---|---|\n| 净销售额 | 120,064.77 |", SUMMARY) == []
    # 找不到出处的数归数字核查管
    assert flags("净销售额 99,999.99。", SUMMARY) == []


def test_dates_inside_tool_text_are_not_numbers() -> None:
    """发票日期 2011-01-18 里的 18 不是一个数：不然“其余 18 个国家”会被当成那张发票的数。"""
    index = build_index(messages(QUALITY))
    assert not [o for o in index.occurrences if o.value in (18.0, 2011.0, 10.0)]
    assert flags("剔除 Netherlands 后其余 18 个国家。", DRILL, QUALITY) == []


def test_field_names_only_carry_meaning_when_the_rest_is_generic() -> None:
    index = build_index(messages(SUMMARY, DRILL, QUALITY))
    by_field = {o.field: o.names for o in index.occurrences}
    assert ("metric", "订单数") in by_field["base_orders"] and ("metric", "GMV") in by_field["gmv"]  # clean_totals.gmv 是 GMV
    assert by_field["gmv_missing_customer_share"] == frozenset()  # 不是 GMV：不给名字，也就不查
    assert flags("英国订单数 291。缺客户 ID 的占比 13.11%。", SUMMARY, DRILL, QUALITY) == []


def test_a_bare_share_in_brackets_gets_a_hint() -> None:
    found = flags("- 活跃客户数 -13,310.62（53.52%）", DECOMPOSE)
    assert [(f["kind"], f["text"]) for f in found] == [("含义", "53.52%")] and "占总变化的比重" in found[0]["meaning"]
    assert flags("- 活跃客户数 -13,310.62（占总变化 53.52%）", DECOMPOSE) == []
    assert flags("- 53.52% 的降幅来自活跃客户数", DECOMPOSE) == []
    table = "| 因子 | 金额 | 占总变化 |\n|---|---|---|\n| 活跃客户数 | -13,310.62 | 53.52% |"
    assert flags(table, DECOMPOSE) == []  # 表头说了是占比
    assert [f["kind"] for f in flags(table.replace("占总变化", "变化"), DECOMPOSE)] == ["含义"]


def test_the_name_search_stops_at_the_previous_number() -> None:
    """“GMV 120,064.77 高于 118,850.04”：GMV 说的是前一个数，后一个数前面没有名字。"""
    assert flags("GMV 120,064.77 高于 118,850.04。", SUMMARY) == []
    assert [f["text"] for f in flags("GMV 高于 118,850.04。", SUMMARY)] == ["118,850.04"]


def test_its_own_name_right_after_the_number_is_enough() -> None:
    assert flags("GMV 对应 118,850.04 的净销售额。", SUMMARY) == []
    assert [f["sentence_says"] for f in flags("GMV 对应 118,850.04。", SUMMARY)] == ["GMV"]
    assert [f["sentence_says"] for f in flags("GMV 对应 118,850.04，另外再看取消和退货之后的净销售额。", SUMMARY)] == ["GMV"]  # 离得远不算


def test_shares_of_the_total_change_are_not_checked_for_the_object() -> None:
    """占比同时关系到部分和整体：“占净销售额降幅的 50.78%”里的名字可能是整体，不标记。"""
    assert flags("占净销售额降幅的 50.78%。", SUMMARY, DRILL) == []
    other = {"tool": "metric_summary", "metrics": [{"metric": "new_customers", "label": "新客数", "change_pct": 0.5078}]}
    assert flags("占净销售额降幅的 50.78%。", SUMMARY, DRILL, other) == []  # 碰巧也是别的数：只要有一处出处是占比就不查


def test_short_numbers_are_not_checked_for_the_object() -> None:
    """两位数太容易碰巧对上别的数：不查。三位有效数字起才查。"""
    assert flags("英国订单 14 单。", SUMMARY, DRILL) == []  # 14 是荷兰的订单数
    assert [f["sentence_says"] for f in flags("荷兰订单 263 单。", SUMMARY, DRILL)] == ["Netherlands"]


def test_text_in_backticks_is_not_a_name() -> None:
    assert flags("`decompose_gmv` 算出 -13,310.62。", SUMMARY, DECOMPOSE) == []  # 工具名里的 gmv 不是在说 GMV
    assert [f["sentence_says"] for f in flags("decompose_gmv 算出 -13,310.62。", SUMMARY, DECOMPOSE)] == ["GMV"]


def test_names_do_not_reach_across_a_neighbouring_number() -> None:
    """名字属于紧挨着它的那个数：往前不越过上一个数字，往后不越过下一个数字。"""
    assert flags("订单数 288 单而另一项是 416.89。", SUMMARY) == []  # “订单数”说的是 288，不是后面客单价的 416.89
    found = flags("GMV 高于 118,850.04 和 288 单的净销售额。", SUMMARY)  # “净销售额”在 288 后面，不能替 118,850.04 作证
    assert [(f["text"], f["sentence_says"]) for f in found] == [("118,850.04", "GMV")]


def test_more_ways_of_saying_it_is_a_share() -> None:
    for wording in ("抵消了 53.52%", "抵掉 53.52%", "总变化的 53.52%", "share 53.52%"):
        assert flags(f"- 活跃客户数 -13,310.62（{wording}）", DECOMPOSE) == [], wording
