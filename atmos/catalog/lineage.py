"""血缘图谱服务。

血缘数据以有向边形式存于 ``lineage_edges``。本模块提供：

* ``build_graph``     —— 产出前端可直接渲染的节点/边集合，并计算层级深度；
* ``trace``           —— 从任一资产出发，向上游追溯或向下游做影响分析（BFS，支持深度限制）；
* ``lineage_summary`` —— 血缘健康度指标（孤立节点、叶子节点、变更影响面）。

由于边数量很小（十余条量级），在内存中做图遍历比递归 SQL 更清晰也更快。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.logging_setup import get_logger
from atmos.models import DataAsset, LineageEdge
from atmos.models.enums import AssetDomain, AssetLayer, TransformType
from atmos.utils import iso_z, relative_time

logger = get_logger("catalog.lineage")

Direction = Literal["upstream", "downstream", "both"]


def _node_payload(asset: DataAsset) -> dict[str, Any]:
    layer = AssetLayer(asset.layer) if asset.layer in AssetLayer.values() else None
    domain = AssetDomain(asset.domain) if asset.domain in AssetDomain.values() else None
    return {
        "id": asset.asset_key,
        "key": asset.asset_key,
        "name": asset.name,
        "domain": asset.domain,
        "domain_label": domain.label if domain else asset.domain,
        "layer": asset.layer,
        "layer_label": layer.label if layer else asset.layer,
        "owner": asset.owner,
        "steward": asset.steward,
        "status": asset.status,
        "granularity": asset.granularity,
        "update_frequency": asset.update_frequency,
        "row_count": asset.row_count,
        "city_count": asset.city_count,
        "byte_size": asset.byte_size,
        "latest_data_time": iso_z(asset.latest_data_time),
        "latest_data_ago": relative_time(asset.latest_data_time),
        "latest_score": asset.latest_score,
        "latest_grade": asset.latest_grade,
        "tags": asset.tags or [],
    }


def _edge_payload(edge: LineageEdge, key_by_id: dict[int, str]) -> dict[str, Any]:
    transform = TransformType(edge.transform_type) if edge.transform_type in TransformType.values() else None
    return {
        "id": edge.id,
        "source": key_by_id.get(edge.upstream_asset_id),
        "target": key_by_id.get(edge.downstream_asset_id),
        "transform_type": edge.transform_type,
        "transform_label": transform.label if transform else edge.transform_type,
        "transform_ref": edge.transform_ref,
        "description": edge.description,
    }


def build_graph(
    session: Session, registry: AssetRegistry | None = None
) -> dict[str, Any]:
    """构建完整血缘图。"""
    registry = registry or load_registry()
    assets = list(session.scalars(select(DataAsset)).all())
    edges = list(session.scalars(select(LineageEdge)).all())
    key_by_id = {asset.id: asset.asset_key for asset in assets}

    nodes = [_node_payload(asset) for asset in assets]
    edge_payloads = [
        payload
        for payload in (_edge_payload(edge, key_by_id) for edge in edges)
        if payload["source"] and payload["target"]
    ]

    # 计算层级：无上游者为 0，其余为上游最大层级 + 1（数据湖分层视角）
    depth = _compute_depths({node["key"] for node in nodes}, edge_payloads)
    for node in nodes:
        node["depth"] = depth.get(node["key"], 0)
        node["upstream_count"] = sum(1 for edge in edge_payloads if edge["target"] == node["key"])
        node["downstream_count"] = sum(1 for edge in edge_payloads if edge["source"] == node["key"])

    return {
        "nodes": sorted(nodes, key=lambda item: (item["depth"], item["key"])),
        "edges": edge_payloads,
        "stats": lineage_summary(nodes, edge_payloads),
        "layer_order": [layer.value for layer in AssetLayer],
        "layer_labels": {layer.value: layer.label for layer in AssetLayer},
        "domain_labels": {domain.value: domain.label for domain in AssetDomain},
        "transform_labels": {
            transform.value: transform.label for transform in TransformType
        },
    }


def _compute_depths(node_keys: set[str], edges: list[dict[str, Any]]) -> dict[str, int]:
    """拓扑层级：无上游为 0。存在环时退化为按已知层级，不抛错。"""
    incoming: dict[str, list[str]] = {key: [] for key in node_keys}
    for edge in edges:
        incoming.setdefault(edge["target"], []).append(edge["source"])

    depth: dict[str, int] = {}
    queue: deque[str] = deque(key for key in node_keys if not incoming.get(key))
    for key in queue:
        depth[key] = 0

    remaining = {key: len(incoming.get(key, [])) for key in node_keys}
    while queue:
        current = queue.popleft()
        for edge in edges:
            if edge["source"] != current:
                continue
            target = edge["target"]
            candidate = depth.get(current, 0) + 1
            depth[target] = max(depth.get(target, 0), candidate)
            remaining[target] = remaining.get(target, 1) - 1
            if remaining[target] <= 0:
                queue.append(target)

    for key in node_keys:
        depth.setdefault(key, 0)
    return depth


def lineage_summary(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    """血缘健康度指标。"""
    isolated = [
        node["key"] for node in nodes if node["upstream_count"] == 0 and node["downstream_count"] == 0
    ]
    sources = [node["key"] for node in nodes if node["upstream_count"] == 0 and node["downstream_count"] > 0]
    sinks = [node["key"] for node in nodes if node["downstream_count"] == 0 and node["upstream_count"] > 0]
    transforms: dict[str, int] = {}
    for edge in edges:
        transforms[edge["transform_type"]] = transforms.get(edge["transform_type"], 0) + 1

    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "source_count": len(sources),
        "sink_count": len(sinks),
        "isolated_count": len(isolated),
        "isolated_assets": isolated,
        "max_depth": max((node["depth"] for node in nodes), default=0),
        "transform_distribution": transforms,
        "avg_upstream": round(
            sum(node["upstream_count"] for node in nodes) / len(nodes), 2
        )
        if nodes
        else 0.0,
    }


def trace(
    session: Session,
    asset_key: str,
    *,
    direction: Direction = "upstream",
    max_depth: int = 10,
) -> dict[str, Any]:
    """从某资产出发追溯上下游影响面。

    返回按层组织的节点与经过的边，便于前端逐层展示"这个资产坏了会影响谁"。
    """
    assets = {asset.asset_key: asset for asset in session.scalars(select(DataAsset)).all()}
    if asset_key not in assets:
        return {"root": asset_key, "found": False, "levels": [], "edges": [], "summary": {}}

    edges = list(session.scalars(select(LineageEdge)).all())
    key_by_id = {asset.id: asset.asset_key for asset in assets.values()}

    adjacency: dict[str, list[tuple[str, LineageEdge]]] = {}
    for edge in edges:
        source = key_by_id.get(edge.upstream_asset_id)
        target = key_by_id.get(edge.downstream_asset_id)
        if not source or not target:
            continue
        if direction in ("downstream", "both"):
            adjacency.setdefault(source, []).append((target, edge))
        if direction in ("upstream", "both"):
            adjacency.setdefault(target, []).append((source, edge))

    visited: set[str] = {asset_key}
    levels: list[dict[str, Any]] = [
        {"depth": 0, "nodes": [_node_payload(assets[asset_key])]}
    ]
    traversed: list[dict[str, Any]] = []
    frontier = [asset_key]

    for depth in range(1, max_depth + 1):
        next_frontier: list[str] = []
        for node_key in frontier:
            for neighbour, edge in adjacency.get(node_key, []):
                traversed.append(_edge_payload(edge, key_by_id))
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                next_frontier.append(neighbour)
        if not next_frontier:
            break
        levels.append(
            {
                "depth": depth,
                "nodes": [_node_payload(assets[key]) for key in sorted(set(next_frontier))],
            }
        )
        frontier = sorted(set(next_frontier))

    unique_edges = {edge["id"]: edge for edge in traversed}
    impacted = sorted(visited - {asset_key})
    return {
        "root": asset_key,
        "root_name": assets[asset_key].name,
        "direction": direction,
        "found": True,
        "levels": levels,
        "edges": list(unique_edges.values()),
        "summary": {
            "depth_reached": len(levels) - 1,
            "related_count": len(impacted),
            "related_assets": impacted,
            "impacted_city_count": None,
            "max_depth": max_depth,
        },
    }


def orphan_report(session: Session) -> dict[str, Any]:
    """治理巡检：找出无归属说明或孤立于血缘之外的资产。"""
    graph = build_graph(session)
    orphan_nodes = [node for node in graph["nodes"] if node["upstream_count"] == 0 and node["downstream_count"] == 0]
    undocumented_edges = [edge for edge in graph["edges"] if not edge["description"]]
    return {
        "isolated_assets": [
            {"key": node["key"], "name": node["name"], "layer": node["layer"], "owner": node["owner"]}
            for node in orphan_nodes
        ],
        "edges_without_documentation": undocumented_edges,
        "healthy": not orphan_nodes and not undocumented_edges,
    }


__all__ = ["build_graph", "trace", "lineage_summary", "orphan_report", "Direction"]
