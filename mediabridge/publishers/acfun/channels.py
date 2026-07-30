"""Rendering of AcFun's partition tree, for the ``mediabridge channels`` command.

A single unparameterised ``getChannelList`` call returns both trees mixed
together. Video partitions are ``channelType == 2`` nodes with a ``children``
list of leaf partitions; article realms hang off ``realms`` on nodes one level
down, so the tree has to be walked rather than read at a fixed depth.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .client import AcFunClient

Node = dict[str, Any]


def walk(nodes: list[Node], parent: Node | None = None) -> Iterator[tuple[Node, Node | None]]:
    for node in nodes:
        yield node, parent
        yield from walk(node.get("children") or [], node)


def _describe(node: Node, id_key: str, name_key: str) -> str:
    intro = (node.get("introduction") or "").strip()
    line = f"  {str(node.get(id_key, '')):>5}  {node.get(name_key, '?')}"
    return f"{line}    {intro}" if intro else line


def format_video_channels(tree: list[Node]) -> str:
    lines = ["Video partitions -- use the indented id as publish.channel_id", "=" * 64]
    for node, _parent in walk(tree):
        children = node.get("children") or []
        # Skip the article branch, whose children carry realms instead.
        if not children or any(child.get("realms") for child in children):
            continue
        lines.append(f"\n{node.get('name', '?')}")
        lines += [_describe(child, "channelId", "name") for child in children]
    return "\n".join(lines)


def format_article_realms(tree: list[Node]) -> str:
    lines = [
        "Article realms -- publish.channel_id is the header id, publish.realm_id the indented id",
        "=" * 64,
    ]
    found = False
    for node, _parent in walk(tree):
        realms = node.get("realms") or []
        if not realms:
            continue
        found = True
        lines.append(f"\n{node.get('name', '?')}  (channel_id={node.get('channelId')})")
        lines += [_describe(realm, "realmId", "realmName") for realm in realms]
    if not found:
        lines.append("\n(no realms returned -- AcFun may have changed the channel tree)")
    return "\n".join(lines)


def describe_channels(client: AcFunClient, articles: bool = False) -> str:
    tree = client.get_channel_list()
    return format_article_realms(tree) if articles else format_video_channels(tree)
