# 关键科目表（上市公司财报）

`key_facts.csv` 由 `da-agent fetch-financials` 生成（2026-10-07），代码见 [financials.py](../../src/da_agent/financials.py)。

- **来源**：SEC EDGAR 的 XBRL companyfacts 接口（美国证监会公开数据，属于公共领域）。只用年报（10-K、20-F），只含标准分类（us-gaap）里的科目。
- **公司**：阿里巴巴（BABA）、京东（JD）、拼多多（PDD Holdings）、亚马逊（AMZN，作参照），2017 财年起。
- **币种**：各公司的编报币种（前三家人民币，亚马逊美元），原始数字，未做汇率换算。
- **财年**：按期间结束日期所在年份命名。阿里财年截至 3 月 31 日，例如 FY2025 = 2024-04-01 至 2025-03-31。

| 列 | 含义 |
|---|---|
| company、fiscal_year、item | 公司、财年、科目（科目定义见 financials.py 的 `ITEMS`） |
| value、currency | 数值、币种 |
| period_start、period_end | 期间：发生额有起止日期，余额只有期末日期 |
| concept | 用了标准分类里的哪种写法（同一科目有几种写法时按优先顺序取） |
| form、accn、filed | 出处：报表类型、SEC 文件编号、提交日期。同一个数字出现在多份年报里时，取最新提交的那份 |

**没有的行不等于 0**：公司用自定义科目标注的数字不在标准分类里，例如阿里、拼多多近年的购建固定资产支出，亚马逊的研发费用。分析工具会把它们标为“未披露”。

重新生成需要在 `.env` 里配置 `SEC_USER_AGENT`（SEC 要求请求头写明联系方式），原始 JSON 下载到 `data/raw/sec/`，不提交。
