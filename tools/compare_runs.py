#!/usr/bin/env python
"""两轮检测结果的逐图差异对比（单图基线 vs 双图对比）。

全量跑批有上百张图，肉眼比对两份结果 Excel 不现实。本工具直接从 `row_results`
表把两轮结论按 (行, 列) 对齐后读回来，输出：

  * 两轮各自的缺陷张数、按语种聚合的缺陷分布；
  * 逐图差异明细，按方向分组，并附截图路径供直接打开判读。

⚠️ 差异的**方向**不等于结论好坏：
   双图判出更多缺陷 —— 可能是修复了单图的漏报，也可能是新引入的误报；
   双图判出更少缺陷 —— 可能是消除了误报，也可能是丢掉了真实缺陷。
   本工具只负责把差异精确、完整地摆出来，**真假仍需人工打开截图判读**。

⚠️ 英文列在两轮中都是单图（它没有基准图），因此英文列的差异与双图无关，
   只反映模型自身的随机性；若该数量明显不为 0，说明结果本身不够稳定。

用法：
    python tools/compare_runs.py <基线task_id> <双图task_id>
    python tools/compare_runs.py <基线task_id> <双图task_id> --limit 100
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，编不出下文要打印的 ⚠ → 等符号；一旦输出被重定向到管道或文件，
# Python 会按系统编码构造 stdout，print 直接抛 UnicodeEncodeError，把一次正常对比变成 traceback。
# 只放宽错误处理、不改编码，这样 GBK 控制台不会出现乱码。
for _s in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_s, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(errors="replace")

from backend.app.config import settings  # noqa: E402
from backend.app.database import get_row_results, get_task  # noqa: E402
from backend.app.services.excel_parser import load_manifest  # noqa: E402

# 缺陷种类按固定顺序排列，保证两轮的判定结果可以直接比较（元组有序、可哈希）
KINDS = (
    ("has_truncation", "截断"),
    ("has_overlap", "重叠"),
    ("has_missing_glyph", "缺字"),
)


def flags_of(rec: dict | None) -> tuple[str, ...] | None:
    """该图判出的缺陷种类（按 KINDS 顺序）。

    无缺陷返回空元组，**没有有效结论返回 None**——两者必须区分开：
    把「跑失败、没结论」当成「无缺陷」，正是最危险的漏报来源。
    """
    if rec is None or rec.get("status") != "SUCCESS" or not rec.get("result"):
        return None
    result = rec["result"]
    if not result.get("is_ui_screenshot", True):
        return ()
    return tuple(label for key, label in KINDS if result.get(key))


def describe(flags: tuple[str, ...] | None) -> str:
    if flags is None:
        return "无有效结论"
    return "无缺陷" if not flags else " + ".join(flags)


def reason_of(rec: dict | None) -> str:
    if rec is None:
        return "（该轮没有这个单元格的记录）"
    if rec.get("error"):
        return f"[{rec.get('status')}] {rec['error']}"
    return (rec.get("result") or {}).get("reason", "")


def short(text: str, width: int = 92) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def image_paths(task_id: str) -> dict[tuple[int, int], str]:
    """从任务的 manifest 取 (行, 列) -> 截图磁盘路径，找不到时返回空表。"""
    parsed = load_manifest(settings.uploads_dir / task_id)
    if parsed is None:
        return {}
    return {k: v.path for k, v in parsed.image_by_cell.items()}


async def collect(task_id: str) -> tuple[dict, dict[tuple[int, int], dict]]:
    task = await get_task(task_id)
    if task is None:
        sys.exit(f"找不到任务 {task_id}。可用 `sqlite3 data/app.db 'select id,filename from tasks'` 查看。")
    rows = {(r["row_index"], r["col_index"]): r for r in await get_row_results(task_id)}
    if not rows:
        print(f"[!] 任务 {task_id} 在 row_results 中没有记录——它可能还没开始跑，或已清库。")
    return task, rows


def report_run(title: str, task: dict, rows: dict, flags: dict) -> None:
    valid = [k for k, f in flags.items() if f is not None]
    invalid = [k for k, f in flags.items() if f is None]
    defective = [k for k in valid if flags[k]]

    print("\n" + "=" * 76)
    print(f"  {title}")
    print("=" * 76)
    print(f"  task_id  : {task['id']}")
    print(f"  文件     : {task['filename']}")
    print(f"  状态     : {task['status']}   完成度 {task['processed_images']}/{task['total_images']} 张")
    if valid:
        pct = len(defective) / len(valid) * 100
        print(f"  有效结论 : {len(valid)} 张   缺陷 {len(defective)} 张 ({pct:.1f}%)   无有效结论 {len(invalid)} 张")
    else:
        print(f"  有效结论 : 0 张   无有效结论 {len(invalid)} 张")

    by_lang: dict[str, list[int]] = {}
    for key in rows:
        stat = by_lang.setdefault(rows[key]["language"], [0, 0])
        stat[0] += 1
        if flags.get(key):
            stat[1] += 1
    if by_lang:
        print("-" * 76)
        print("  按语种（按缺陷数降序）:")
        for lang, (total, bad) in sorted(by_lang.items(), key=lambda kv: (-kv[1][1], kv[0])):
            mark = " ←" if lang == "英语" else ""
            print(f"    {lang:<14} 共 {total:>4} 张    缺陷 {bad:>3} 张{mark}")


def report_diff(
    flags_a: dict,
    flags_b: dict,
    rows_a: dict,
    rows_b: dict,
    paths_a: dict,
    paths_b: dict,
    limit: int,
) -> None:
    groups: dict[str, list[tuple[int, int]]] = {"A": [], "B": [], "C": [], "D": []}
    same = 0
    both_invalid = 0
    for key in sorted(set(flags_a) | set(flags_b)):
        fa, fb = flags_a.get(key), flags_b.get(key)
        if fa is None and fb is None:
            # 两侧都没跑出结论，这不是「双图带来的差异」，只是这两张图都失败了。
            # 若把它算进差异，每个长期失败的单元格都会永远占据差异清单。
            both_invalid += 1
        elif fa is None or fb is None:
            groups["D"].append(key)
        elif fa == fb:
            same += 1
        elif not fa and fb:
            groups["A"].append(key)
        elif fa and not fb:
            groups["B"].append(key)
        else:
            groups["C"].append(key)

    total = same + both_invalid + sum(len(v) for v in groups.values())
    changed = total - same - both_invalid
    print("\n" + "=" * 76)
    print("  差异对比")
    print("=" * 76)
    print(f"  覆盖单元格  : {total} 个")
    print(f"  结论一致    : {same} 个")
    print(f"  两侧均无结论: {both_invalid} 个（都是检测失败，不计入差异）")
    print(f"  结论不一致  : {changed} 个  ← 需要人工抽查的部分")

    specs = (
        ("A", groups["A"], "双图判出更多缺陷 → 候选「修复漏报」，也可能是「新增误报」"),
        ("B", groups["B"], "双图判出更少缺陷 → 候选「消除误报」，也可能是「新增漏报」"),
        ("C", groups["C"], "两侧都判有缺陷，但缺陷种类不同"),
        ("D", groups["D"], "只有一侧有有效结论（另一侧跑失败 / 无结果）"),
    )
    for tag, keys, desc in specs:
        if keys:
            print(f"    [{tag}] {desc}   —— {len(keys)} 个")

    # 英文列两轮都是单图，它的差异与双图无关，只能反映模型随机性
    english_changed = [
        k for _, keys, _ in specs for k in keys
        if (rows_b.get(k) or rows_a.get(k) or {}).get("is_english")
    ]
    if english_changed:
        print(
            f"\n  ⚠ 其中 {len(english_changed)} 个差异落在**英文列**——英文列两轮都是单图，"
            "差异与双图无关，\n     只反映模型自身的不稳定性；数量偏大时两轮结果的其余差异也需谨慎采信。"
        )

    if changed == 0:
        print("\n  两轮结论完全一致，没有需要抽查的差异。")
        return

    print("\n" + "-" * 76)
    print("  差异明细（请按截图路径逐一打开判读）")
    print("-" * 76)
    for tag, keys, _ in specs:
        if not keys:
            continue
        shown = keys[:limit]
        print(f"\n### [{tag}] 共 {len(keys)} 个，展示 {len(shown)} 个")
        for row_index, col_index in shown:
            rec_a, rec_b = rows_a.get((row_index, col_index)), rows_b.get((row_index, col_index))
            meta = rec_b or rec_a or {}
            english = "  ← 英文列（与双图无关）" if meta.get("is_english") else ""
            print(f"\n  行 {row_index} / 列 {col_index}  {meta.get('language', '?')}{english}")
            print(f"    基线(单图): {describe(flags_a.get((row_index, col_index)))}")
            print(f"    双图      : {describe(flags_b.get((row_index, col_index)))}")
            print(f"    双图理由  : {short(reason_of(rec_b))}")
            print(f"    基线理由  : {short(reason_of(rec_a))}")
            path = paths_b.get((row_index, col_index)) or paths_a.get((row_index, col_index))
            print(f"    截图      : {path or '（manifest 中找不到该单元格的截图路径）'}")
        if len(keys) > limit:
            print(f"\n  … [{tag}] 还有 {len(keys) - limit} 个未展示，用 --limit 调大")


async def main() -> int:
    ap = argparse.ArgumentParser(description="两轮检测结果差异对比")
    ap.add_argument("baseline", help="基线轮 task_id（单图，LLM_DUAL_IMAGE=false）")
    ap.add_argument("dual", help="双图轮 task_id（LLM_DUAL_IMAGE=true）")
    ap.add_argument("--limit", type=int, default=40, help="每类差异最多展示多少条（默认 40）")
    args = ap.parse_args()

    task_a, rows_a = await collect(args.baseline)
    task_b, rows_b = await collect(args.dual)
    paths_a = image_paths(args.baseline)
    paths_b = image_paths(args.dual)

    print(f"当前 .env 中 LLM_DUAL_IMAGE={settings.llm_dual_image}"
          "（仅供参考——两轮实际用的模式由跑批时的设置决定，库里不记录）")

    flags_a = {k: flags_of(v) for k, v in rows_a.items()}
    flags_b = {k: flags_of(v) for k, v in rows_b.items()}

    report_run("基线轮（单图）", task_a, rows_a, flags_a)
    report_run("双图轮", task_b, rows_b, flags_b)
    report_diff(flags_a, flags_b, rows_a, rows_b, paths_a, paths_b, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
