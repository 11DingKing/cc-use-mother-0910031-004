"""版本快照差异比较：按片段血缘（lineage）与引用来源对比两个冻结版本。"""
from __future__ import annotations


def _item_brief(item: dict) -> dict:
    return {
        "lineage_id": item["lineage_id"],
        "fragment_id": item["fragment_id"],
        "title": item["title"],
        "change_kind": item["change_kind"],
    }


def diff_snapshots(a: dict, b: dict) -> dict:
    """比较两个版本快照（各含 items 与 sources），返回结构化差异。"""
    a_items = {item["lineage_id"]: item for item in a["items"]}
    b_items = {item["lineage_id"]: item for item in b["items"]}

    added = [_item_brief(b_items[key]) for key in sorted(b_items.keys() - a_items.keys())]
    removed = [_item_brief(a_items[key]) for key in sorted(a_items.keys() - b_items.keys())]

    changed = []
    for key in sorted(a_items.keys() & b_items.keys()):
        old, new = a_items[key], b_items[key]
        if old["title"] != new["title"] or old["body"] != new["body"]:
            changed.append(
                {
                    "lineage_id": key,
                    "from": {**_item_brief(old), "body": old["body"]},
                    "to": {**_item_brief(new), "body": new["body"]},
                }
            )

    a_sources = {src["source_id"]: src for src in a["sources"]}
    b_sources = {src["source_id"]: src for src in b["sources"]}
    sources_added = [
        {"source_id": key, "title": b_sources[key]["title"]}
        for key in sorted(b_sources.keys() - a_sources.keys())
    ]
    sources_removed = [
        {"source_id": key, "title": a_sources[key]["title"]}
        for key in sorted(a_sources.keys() - b_sources.keys())
    ]

    return {
        "fragments": {"added": added, "removed": removed, "changed": changed},
        "sources": {"added": sources_added, "removed": sources_removed},
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "sources_added": len(sources_added),
            "sources_removed": len(sources_removed),
        },
    }
