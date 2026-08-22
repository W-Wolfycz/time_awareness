"""
数据文件读写助手（YAML 持久化）

集中插件持久化文件的通用读写逻辑，供 ``calendar_manager`` 复用，避免
「多编码读取 + 原子写入 + 临时文件清理」逻辑分散在多处实现而漂移。

设计要点：
- 仅使用 ``yaml.safe_load`` / ``yaml.safe_dump``，杜绝任意对象构造带来的安全风险。
- 写入采用 ``allow_unicode=True``（中文不转义）、``sort_keys=False``（保持插入顺序，
  diff 友好）、``default_flow_style=False``（块状缩进，最直观）。
- 写入使用「``.tmp`` + ``os.replace``」原子替换；替换前 flush + fsync，减少进程
  崩溃时留下空文件或半文件的概率。
- 读取优先使用 libyaml C 扩展（``CSafeLoader``）提速；写出固定使用纯 Python
  ``SafeDumper``：libyaml 的 C emitter 会忽略按节点设置的块样式（``|``）并转义
  星平面 Unicode（如 emoji），使长消息可读性变差。
"""

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
        except FileNotFoundError:
            # 首次启动或新装插件时文件尚未创建——视为空数据，静默返回 None
            return None
        except PermissionError:
            logger.error(f"{tag()} ❌ 文件读取权限不足: {path}")
            return None
        except UnicodeDecodeError:
            continue
        except yaml.YAMLError:
            # 保留损坏文件供人工排查，避免后续 save 用空数据覆盖
            try:
                os.replace(path, path + ".corrupt")
            except OSError:
                pass
            logger.error(
                f"{tag()} ❌ YAML 解析失败，文件可能已损坏: {path}"
                "（原文件已保留为 .corrupt，请人工检查后删除）"
            )
            return None
    else:
        logger.error(f"{tag()} ❌ 无法以任何编码读取文件: {path}")
        return None

    if not isinstance(data, dict):
        logger.error(f"{tag()} ❌ 文件格式错误：根对象不是字典: {path}")
        return None
    return data


def atomic_write_yaml(path: str, data: dict, header: str | None = None) -> bool:
    """原子性地将映射写入 YAML 文件。

    临时文件带 pid 后缀：多进程共享数据目录时避免互相覆盖对方的 .tmp。
    """
    temp_file = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(dump_yaml_str(data, header=header))
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_file, path)
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
