"""数字归属核对的评测（M18）：它指得出多少“数字安错对象”，在没改动的答案上会不会乱报。全程不调用模型。

材料分两份，互不重叠：
- 开发集：调规则时用的答案。规则是看着它们的结果改出来的，所以它们的成绩不算数。
- 留出集：规则定下来之后只跑一次。A 是复核 Agent 评测（M15）用过的 16 份答案；B 是多模型对比（M16）里三个模型新写的答案。

植入错误沿用复核 Agent 评测的两种方式（review_eval）：对象替换、数字对调。改完后数字核查的结果不变。
"""

import json
from pathlib import Path
from typing import Any

import yaml

from .attribution import check_attribution
from .paths import ROOT
from .review_eval import MUTATORS, _lines, invisible_to_number_check
from .review_eval import load_plan as load_review_plan

PLAN_PATH = ROOT / "eval" / "attribution_cases.yaml"
SEEDED_KINDS = ("entity", "swap")
KIND_LABELS = {"entity": "对象替换", "swap": "数字对调"}


def load_plan(path: Path = PLAN_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _from_runs(spec: dict[str, Any], name: str, root: Path) -> list[dict[str, Any]]:
    """从评测目录里按规则机械地取答案：指定的模型、套题、对比组里正常结束、有答案的运行。"""
    out = []
    for model in spec["models"]:
        for suite in spec["suites"]:
            folder = root / "eval" / "results" / f"{spec['prefix']}-{model}-{suite}" / "runs"
            for case in sorted(p.name for p in folder.iterdir()):
                for run_name in spec["runs"]:
                    run_dir = folder / case / run_name
                    if not (run_dir / "run.json").exists():
                        continue
                    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
                    if run["status"] == "completed" and run.get("answer"):
                        suffix = "" if len(spec["runs"]) == 1 else f"-{run_name}"
                        out.append({"id": f"{model}-{suite}-{case}{suffix}", "set": name,
                                    "run": run_dir.relative_to(root).as_posix(), "seed": model in spec["seeded_models"]})
    return out


def bases(plan: dict[str, Any], split: str, root: Path = ROOT) -> list[dict[str, Any]]:
    """这一份材料里的答案：{id, set, run 目录, 要不要植入错误}。

    dev：第一轮的开发集。test：第一轮的留出集（A、B）。
    dev2：第二轮的开发材料 = dev + A + B（第一轮跑完后，A、B 的结果已经看过，不能再当留出集）。
    test2：第二轮的留出集 C，两轮调规则时都没有跑过。
    """
    dev = [{"id": b["id"], "set": "dev", "run": b["run"], "seed": True} for b in plan["dev"]]
    first = [{"id": b["id"], "set": "A", "run": b["run"], "seed": True} for b in load_review_plan(root / plan["test_a"])["bases"]]
    first += _from_runs(plan["test_b"], "B", root)
    return {"dev": dev, "test": first, "dev2": dev + first, "test2": _from_runs(plan["test_c"], "C", root)}[split]


def build_materials(plan: dict[str, Any], split: str, root: Path = ROOT) -> list[dict[str, Any]]:
    """每份答案原样一条；要植入的再各加“对象替换”“数字对调”两条（植入不了的记为 skipped）。"""
    materials = []
    for base in bases(plan, split, root):
        run = json.loads((root / base["run"] / "run.json").read_text(encoding="utf-8"))
        materials.append({"id": f"{base['id']}-clean", "base": base["id"], "set": base["set"], "kind": "clean", "run": run})
        for kind in SEEDED_KINDS if base["seed"] else ():
            mutation = MUTATORS[kind](run["answer"], run)
            item = {"id": f"{base['id']}-{kind}", "base": base["id"], "set": base["set"], "kind": kind}
            if mutation is None or not invisible_to_number_check(run, mutation.answer):
                materials.append(item | {"skipped": True})
            else:
                materials.append(item | {"run": run | {"answer": mutation.answer},
                                         "mutation": {"spans": mutation.spans, "lines": mutation.lines,
                                                      "before": mutation.before, "after": mutation.after}})
    return materials


def _hits(flags: list[dict[str, Any]], spans: list[tuple[int, int]]) -> list[dict[str, Any]]:
    return [f for f in flags if f["kind"] == "对象" and any(f["start"] < end and start < f["end"] for start, end in spans)]


def evaluate(materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐条核对。植入的错误：有没有一条“对象”标记落在被改的那一句。

    原样答案里这一句本来就有“对象”标记的，分不清指的是哪个，记为 ambiguous，不计入检出率。
    """
    clean: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    rows = []
    for item in materials:
        if item.get("skipped"):
            rows.append({k: item[k] for k in ("id", "base", "set", "kind")} | {"outcome": "skipped"})
            continue
        result = check_attribution(item["run"]["answer"], item["run"]["messages"])
        row = {k: item[k] for k in ("id", "base", "set", "kind")} | {"checked": result["checked"], "flags": result["flags"]}
        if item["kind"] == "clean":
            clean[item["base"]] = (item["run"]["answer"], result["flags"])
        else:
            answer, flags = clean[item["base"]]
            sentences = _lines(answer)
            already = _hits(flags, [sentences[i][:2] for i in item["mutation"]["lines"]])
            hits = _hits(result["flags"], [tuple(span) for span in item["mutation"]["spans"]])
            row |= {"outcome": "ambiguous" if hits and already else "detected" if hits else "missed",
                    "before": item["mutation"]["before"], "after": item["mutation"]["after"]}
        rows.append(row)
    return rows


def clean_flags(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """原样答案上的全部标记，按出现顺序编号，供人工逐条判断。"""
    out = []
    for row in rows:
        if row["kind"] == "clean":
            out += [{"material": row["id"], "set": row["set"], **flag} for flag in row["flags"]]
    return [{"no": i, **flag} for i, flag in enumerate(out, 1)]


def summarize(rows: list[dict[str, Any]], judgements: dict[str, str] | None = None) -> dict[str, Any]:
    """judgements：人工判断，{标记编号: 成立 / 有争议 / 不成立}；没有就只给数量。"""
    sets = sorted({r["set"] for r in rows})
    seeded = {}
    for kind in SEEDED_KINDS:
        for name in [*sets, "合计"]:
            picked = [r for r in rows if r["kind"] == kind and (name == "合计" or r["set"] == name)]
            counted = [r for r in picked if r["outcome"] in ("detected", "missed")]
            seeded[f"{kind}/{name}"] = {"planned": len(picked), "skipped": sum(r["outcome"] == "skipped" for r in picked),
                                        "ambiguous": sum(r["outcome"] == "ambiguous" for r in picked), "counted": len(counted),
                                        "detected": sum(r["outcome"] == "detected" for r in counted)}
    flags = clean_flags(rows)
    clean = {}
    for name in [*sets, "合计"]:
        answers = [r for r in rows if r["kind"] == "clean" and (name == "合计" or r["set"] == name)]
        mine = [f for f in flags if name == "合计" or f["set"] == name]
        clean[name] = {"answers": len(answers), "checked": sum(r["checked"] for r in answers)}
        for kind in ("对象", "含义"):
            of_kind = [f for f in mine if f["kind"] == kind]
            clean[name][kind] = {"flags": len(of_kind), "answers": len({f["material"] for f in of_kind})}
            if judgements is not None:
                for verdict in ("成立", "有争议", "不成立"):
                    clean[name][kind][verdict] = sum(judgements.get(str(f["no"])) == verdict for f in of_kind)
    return {"seeded": seeded, "clean": clean, "judged": judgements is not None}


def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f"（{part / whole:.1%}）" if whole else "")  # 一位小数：25/42 是 59.5%，不能四舍五入成“60%”


def render_summary(summary: dict[str, Any], meta: dict[str, Any]) -> str:
    lines = [f"# 数字归属核对的评测：{meta['name']}", "",
             f"材料：{meta['split_label']}；{meta['started_at']}。不调用模型。评分标准：[attribution_rubric.md](../../attribution_rubric.md)。", "",
             "## 植入的错误", "", "| 植入方式 | 材料 | 计划 | 没法植入 | 分不清 | 计入 | 指到了被改的那一句 |", "|---|---|---:|---:|---:|---:|---:|"]
    for key, s in summary["seeded"].items():
        kind, name = key.split("/")
        lines.append(f"| {KIND_LABELS[kind]} | {name} | {s['planned']} | {s['skipped']} | {s['ambiguous']} | {s['counted']} | "
                     f"{_rate(s['detected'], s['counted'])} |")
    judged = summary["judged"]
    lines += ["", "## 原样答案（没有改动）上的标记", "",
              "| 材料 | 答案 | 核对的数字 | 标记 | 类型 | 条数 | 涉及答案 |" + (" 成立 | 有争议 | 不成立 |" if judged else ""),
              "|---|---:|---:|---|---|---:|---:|" + ("---:|---:|---:|" if judged else "")]
    for name, c in summary["clean"].items():
        for kind, label in (("对象", "可能安错了对象"), ("含义", "占比可能被读成变化率")):
            k = c[kind]
            lines.append(f"| {name} | {c['answers']} | {c['checked']:,} | {kind} | {label} | {k['flags']} | {k['answers']} |"
                         + (f" {k['成立']} | {k['有争议']} | {k['不成立']} |" if judged else ""))
    if not judged:
        lines += ["", "每条标记是否成立需要人工逐条判断（flags.json），判断后重新汇总。"]
    return "\n".join(lines) + "\n"
