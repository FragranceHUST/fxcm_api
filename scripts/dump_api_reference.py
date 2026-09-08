#!/usr/bin/env python
"""全量导出已安装 forexconnect 包的 API 表面，生成 docs/forexconnect_api_dump.md。

用法：
    .venv/bin/python scripts/dump_api_reference.py

输出为内省快照（类/方法/常量/枚举），升级包版本后重跑即可刷新文档附录。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forexconnect
from forexconnect import ForexConnect, fxcorepy

OUT = Path(__file__).resolve().parent.parent / "docs" / "forexconnect_api_dump.md"

SKIP_ATTRS = {"__class__", "__delattr__", "__dir__", "__doc__", "__eq__", "__format__",
              "__ge__", "__getattribute__", "__gt__", "__hash__", "__init__",
              "__init_subclass__", "__le__", "__lt__", "__module__", "__ne__",
              "__new__", "__reduce__", "__reduce_ex__", "__repr__", "__setattr__",
              "__sizeof__", "__str__", "__subclasshook__"}


def doc_first_lines(obj, limit=6):
    doc = (getattr(obj, "__doc__", None) or "").strip()
    lines = [l.rstrip() for l in doc.splitlines() if l.strip()]
    return "\n".join(f"    {l}" for l in lines[:limit])


def main() -> int:
    lines: list[str] = []
    add = lines.append

    add(f"# forexconnect API 内省快照")
    add(f"")
    add(f"- 包路径: `{forexconnect.__file__}`")
    add(f"- 生成方式: `python scripts/dump_api_reference.py`（升级包后重跑刷新）")
    add(f"")

    add("## ForexConnect 封装类方法")
    add("")
    for name in sorted(a for a in dir(ForexConnect) if not a.startswith("_")):
        member = getattr(ForexConnect, name)
        add(f"### ForexConnect.{name}")
        add("")
        add("```")
        add(doc_first_lines(member))
        add("```")
        add("")

    add("## fxcorepy 类清单")
    add("")
    classes = sorted(a for a in dir(fxcorepy)
                     if not a.startswith("_") and a[0].isupper())
    add("，".join(f"`{c}`" for c in classes))
    add("")

    add("## 关键类详情")
    add("")
    focus = [
        "O2GSession", "O2GLoginRules", "O2GTradingSettingsProvider", "O2GTableManager",
        "O2GTable", "O2GTradeTableRow", "O2GOfferTableRow", "O2GAccountTableRow",
        "O2GOrderTableRow", "O2GClosedTradeTableRow", "O2GSummaryTableRow",
        "O2GMessageTableRow", "O2GRequest", "O2GRequestFactory", "O2GValueMap",
        "O2GTableColumn", "PriceHistoryCommunicator", "O2GSessionDescriptor",
    ]
    for cls_name in focus:
        cls = getattr(fxcorepy, cls_name, None)
        add(f"### {cls_name}")
        add("")
        if cls is None:
            add("(不存在)")
            add("")
            continue
        members = [a for a in dir(cls) if a not in SKIP_ATTRS]
        add(f"成员: {', '.join(f'`{m}`' for m in members)}")
        add("")
        doc = doc_first_lines(cls, 3)
        if doc:
            add(doc)
            add("")

    add("## Constants 枚举值")
    add("")
    C = fxcorepy.Constants
    for group in sorted(a for a in dir(C) if not a.startswith("_")):
        g = getattr(C, group)
        if group in ("BUY", "SELL"):
            add(f"- **{group}** = `{g!r}`")
            continue
        values = [a for a in dir(g) if not a.startswith("_")]
        if not values:
            continue
        add(f"### Constants.{group}")
        add("")
        for v in values:
            add(f"- `{group}.{v}` = `{getattr(g, v)!r}`")
        add("")

    add("## O2GRequestParamsEnum（create_order_request 的 kwargs 全集）")
    add("")
    enum_cls = fxcorepy.O2GRequestParamsEnum
    names = getattr(enum_cls, "names", None)
    if names:
        for n in sorted(names):
            add(f"- `{n}`")
    else:
        add(", ".join(a for a in dir(enum_cls) if not a.startswith("_")))
    add("")

    add("## O2GTableType")
    add("")
    for t in sorted(a for a in dir(fxcorepy.O2GTableType) if not a.startswith("_")):
        add(f"- `{t}` = `{getattr(fxcorepy.O2GTableType, t)!r}`")
    add("")

    out = Path(__file__).resolve().parent.parent / "docs" / "forexconnect_api_dump.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {out}（{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
