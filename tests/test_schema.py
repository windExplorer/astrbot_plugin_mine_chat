"""_conf_schema.json 的装载期自检（复刻 AstrBot 的解析规则）。

背景（v1.0.4 / v1.0.5 的真实事故）：分组节点漏写 `"type": "object"`，
AstrBot 装载插件时 `_config_schema_to_default_config` 对每个节点取 `v["type"]`
抛 KeyError: 'type'，整个插件直接装不上——而当时的纯函数自检全绿，
因为 config.py 的读取是普通字典操作，不经过 schema。

本测试按 AstrBot 侧的遍历规则在本地把这类问题拦在发布之前。

AstrBot 侧规则（astrbot/core/config/astrbot_config.py: _config_schema_to_default_config）：
  - 每个节点必须有 "type"，且属于 DEFAULT_VALUE_MAP；
  - "object" 节点递归解析其 "items"；
  - 其余节点的 "items"（如 list 的子项描述）不递归。
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "_conf_schema.json")

# 与 AstrBot default.py 的 DEFAULT_VALUE_MAP 保持一致（对照 4.28.1）
SUPPORTED_TYPES = {
    "int",
    "float",
    "bool",
    "string",
    "text",
    "list",
    "file",
    "object",
    "template_list",
    "dict",
}

failures: list[str] = []


def walk(node: object, path: str) -> None:
    if not isinstance(node, dict):
        failures.append(f"{path}: 节点不是对象（{type(node).__name__}）")
        return
    if "type" not in node:
        failures.append(f"{path}: 缺少 type（现有键 {sorted(node.keys())}）")
        return
    node_type = node["type"]
    if node_type not in SUPPORTED_TYPES:
        failures.append(f"{path}: 不受支持的类型 {node_type!r}")
        return
    if node_type == "object":
        items = node.get("items")
        if not isinstance(items, dict) or not items:
            failures.append(f"{path}: object 节点缺少非空 items")
            return
        for key, child in items.items():
            walk(child, f"{path}.{key}" if path else key)
    if node_type == "list" and isinstance(node.get("items"), dict):
        child = node["items"]
        if "type" not in child:
            failures.append(f"{path}[]: list 子项描述缺少 type")


def check_widgets(node: object, path: str) -> None:
    """widget=model 的节点必须是 string（后端 _cast 按 string 处理）。"""
    if not isinstance(node, dict):
        return
    if node.get("widget") == "model" and node.get("type") != "string":
        failures.append(f"{path}: widget=model 的 type 必须是 string")
    items = node.get("items")
    if isinstance(items, dict):
        for key, child in items.items():
            check_widgets(child, f"{path}.{key}" if path else key)


def main() -> int:
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)

    count = 0
    for key, value in schema.items():
        walk(value, key)
        check_widgets(value, key)
        count += 1
    print(f"已检查 {count} 个顶层配置节点")

    if failures:
        print(f"\n发现 {len(failures)} 处 schema 问题：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("schema 结构合法，可被 AstrBot 装载")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
