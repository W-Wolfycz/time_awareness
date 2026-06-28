"""
数据文件读写助手（YAML 持久化）

集中插件持久化文件的通用读写逻辑，供 ``calendar_manager`` 复用，避免
「多编码读取 + 原子写入 + 临时文件清理」逻辑分散在多处实现而漂移。

设计要点：
- 仅使用 ``yaml.safe_load`` / ``yaml.safe_dump``，杜绝任意对象构造带来的安全风险。
- 写入采用 ``allow_unicode=True``（中文不转义）、``sort_keys=False``（保持插入顺序，
  diff 友好）、``default_flow_style=False``（块状缩进，最直观）。
- 写入沿用「``.tmp`` + ``os.rename``」原子替换，并在 Windows 下先删旧文件。
- 读取优先使用 libyaml C 扩展（``CSafeLoader``）提速；写出固定使用纯 Python
  ``SafeDumper``：libyaml 的 C emitter 会忽略按节点设置的块样式（``|``）并转义
  星平面 Unicode（如 emoji），使长消息可读性变差。
"""

import json
import os

import yaml
from ..log import logger, tag


try:  # pragma: no cover - 取决于运行环境是否带 libyaml
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _SafeLoader

_READ_ENCODINGS = ("utf-8-sig", "utf-8")


def _represent_str(dumper, data):
    """多行字符串用字面量块样式（``|``）输出，单行保持默认。"""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _SafeDumper(yaml.SafeDumper):
    """纯 Python SafeDumper 子类，挂载多行字符串块样式 representer。"""


_SafeDumper.add_representer(str, _represent_str)


def dump_yaml_str(data: dict, header: str | None = None) -> str:
    """将映射序列化为 YAML 文本。"""
    body = yaml.dump(
        data,
        Dumper=_SafeDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    if header:
        return f"# {header}\n{body}"
    return body


def load_mapping(path: str):
    """从 YAML 文件读取一个映射（dict）。

    失败/根对象不是 dict 时记录日志并返回 None。
    """
    data = None
    for encoding in _READ_ENCODINGS:
        try:
            with open(path, "r", encoding=encoding) as f:
                data = yaml.load(f, Loader=_SafeLoader)
            break
        except PermissionError:
            logger.error(f"{tag()} ❌ 文件读取权限不足: {path}")
            return None
        except UnicodeDecodeError:
            continue
        except yaml.YAMLError:
            logger.error(f"{tag()} ❌ YAML 解析失败，文件可能已损坏: {path}")
            return None
    else:
        logger.error(f"{tag()} ❌ 无法以任何编码读取文件: {path}")
        return None

    if not isinstance(data, dict):
        logger.error(f"{tag()} ❌ 文件格式错误：根对象不是字典: {path}")
        return None
    return data


def atomic_write_yaml(path: str, data: dict, header: str | None = None) -> bool:
    """原子性地将映射写入 YAML 文件。"""
    temp_file = path + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(dump_yaml_str(data, header=header))

        if os.name == "nt" and os.path.exists(path):
            os.remove(path)
        os.rename(temp_file, path)
        return True
    except Exception as e:
        logger.error(f"{tag()} ❌ 写入文件失败: {path}: {e}")
        return False
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def _load_json_mapping(path: str):
    """读取旧的 JSON 文件（多编码），返回 dict；失败返回 None"""
    for encoding in _READ_ENCODINGS:
        try:
            with open(path, "r", encoding=encoding) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.error(f"{tag()} ❌ 旧 JSON 根对象不是字典: {path}")
            return None
        except PermissionError:
            logger.error(f"{tag()} ❌ 旧 JSON 读取权限不足: {path}")
            return None
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    logger.error(f"{tag()} ❌ 无法读取旧 JSON 文件: {path}")
    return None


def migrate_json_to_yaml(json_path: str, yaml_path: str):
    """将旧的 JSON 持久化文件一次性迁移为 YAML。

    仅当目标 YAML 不存在、且旧 JSON 存在时执行迁移：读取 JSON → 写出 YAML →
    将旧 JSON 重命名为 ``<json_path>.bak`` 保留以便回滚。
    """
    if os.path.exists(yaml_path) or not os.path.exists(json_path):
        return None

    data = _load_json_mapping(json_path)
    if data is None:
        return None

    if not atomic_write_yaml(yaml_path, data):
        logger.error(f"{tag()} ❌ JSON→YAML 迁移写入失败: {json_path} -> {yaml_path}")
        return None

    try:
        backup_path = json_path + ".bak"
        if os.path.exists(backup_path):
            os.remove(backup_path)
        os.rename(json_path, backup_path)
        logger.info(
            f"{tag()} ✅ 已迁移 {os.path.basename(json_path)} → "
            f"{os.path.basename(yaml_path)}（旧文件备份为 .bak）"
        )
    except OSError as e:
        logger.warning(f"{tag()} ⚠️ 旧 JSON 备份失败（数据已迁移）: {e}")

    return data
