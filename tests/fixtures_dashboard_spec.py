"""测试用的看板方案：和 M12 手工看板同口径的几张图，用来核对“方案 → 数字”这条路算得对不对。"""

RETAIL_SPEC = {
    "title": "电商运营看板",
    "goal": "给运营负责人看整体规模、趋势、市场和下单时间",
    "metrics": [
        {"id": "gmv", "label": "GMV", "agg": "sum", "column": "amount", "where": {"line_type": ["销售"]},
         "format": "money"},
        {"id": "orders", "label": "订单数", "agg": "count_distinct", "column": "invoice",
         "where": {"line_type": ["销售"]}},
        {"id": "cancelled", "label": "取消金额", "agg": "sum", "column": "amount", "where": {"line_type": ["取消"]},
         "format": "money"},
        {"id": "cancel_rate", "label": "取消率", "agg": "ratio", "left": "cancelled", "right": "gmv",
         "format": "percent"},
    ],
    "charts": [
        {"id": "gmv_total", "title": "GMV", "type": "kpi", "metrics": ["gmv"], "why": "整体规模"},
        {"id": "cancel_rate_kpi", "title": "取消率", "type": "kpi", "metrics": ["cancel_rate"], "why": "取消的严重程度"},
        {"id": "monthly_gmv", "title": "每月 GMV", "type": "column", "metrics": ["gmv"],
         "x": {"column": "invoice_time", "grain": "month"}, "why": "看趋势和季节性"},
        {"id": "market_gmv", "title": "英国和海外的 GMV", "type": "stacked_column", "metrics": ["gmv"],
         "x": {"column": "invoice_time", "grain": "month"},
         "series": {"column": "country", "keep": ["United Kingdom"], "other_label": "海外"}, "why": "市场构成"},
        {"id": "top_countries", "title": "GMV 最高的国家", "type": "bar", "metrics": ["gmv"],
         "x": {"column": "country"}, "top_n": 10, "why": "哪些市场最重要"},
        {"id": "order_hours", "title": "什么时候下单", "type": "heatmap", "metrics": ["orders"],
         "x": {"column": "invoice_time", "grain": "hour"}, "series": {"column": "invoice_time", "grain": "weekday"},
         "why": "客服和促销排期"},
    ],
    "filters": [{"column": "country", "keep": ["United Kingdom"], "other_label": "海外", "label": "市场"}],
    "year_filter": True,
}
