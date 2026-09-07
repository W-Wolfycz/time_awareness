"""YAML 持久化助手：safe_load/safe_dump、原子替换；读用 CSafeLoader，写固定纯 Python SafeDumper（C emitter 转义 emoji）。"""

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
    """读取 YAML 映射；失败/根对象非 dict 记日志并返回 None。"""
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
    """原子写入 YAML；临时文件带 pid 后缀避免多进程互相覆盖 .tmp。"""
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
