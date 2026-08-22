"""AI 每日日程快照的隐私化持久化。

快照以 ``Persona identity HMAC + 本地日期`` 为键（Person 维度隔离：同一
Persona 的多个 Bot 共享同一份快照），不保存原始平台/Bot ID、UMO、
Persona ID/prompt 或对话历史。写入复用插件统一的 YAML 原子替换实现。
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import hmac
import os
import secrets
from typing import Any

from ._datafile import atomic_write_yaml, load_mapping
from ..log import logger, tag


# 2.0.0 新协议：从 1 重新计数；读取不匹配版本时整体忽略（不迁移旧快照）
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_FILE_NAME = "daily_schedule_snapshots.yaml"
PERSONA_SECRET_FILE_NAME = "daily_schedule_persona_secret.yaml"


class DailyScheduleSnapshotStore:
    """管理 AI 每日日程的 active snapshot 与最近失败状态。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._snapshot_path = os.path.join(data_dir, SNAPSHOT_FILE_NAME)
        self._secret_path = os.path.join(data_dir, PERSONA_SECRET_FILE_NAME)
        self._secret: bytes | None = None
        self._snapshots: dict[str, dict] = {}
        self._failures: dict[str, dict] = {}

    def load(self) -> bool:
        """加载或创建 persona secret，并读取已有快照。

        快照 schema 版本不匹配时整体忽略，等待按当前 Persona、日期和时区
        重新生成。
        """
        os.makedirs(self.data_dir, exist_ok=True)
        if not self._load_or_create_secret():
            return False

        data = load_mapping(self._snapshot_path)
        if data is None:
            self._snapshots = {}
            self._failures = {}
            return True

        schema_version = data.get("schema_version")
        if schema_version != SNAPSHOT_SCHEMA_VERSION:
            logger.info(
                f"{tag()} 🗑️ 快照文件 schema_version={schema_version} "
                f"与当前 {SNAPSHOT_SCHEMA_VERSION} 不一致，旧格式快照已忽略（等待重新生成）"
            )
            self._snapshots = {}
            self._failures = {}
            return True

        snapshots = data.get("snapshots", {})
        failures = data.get("failures", {})
        self._snapshots = (
            {str(key): value for key, value in snapshots.items() if isinstance(value, dict)}
            if isinstance(snapshots, dict)
            else {}
        )
        self._failures = (
            {str(key): value for key, value in failures.items() if isinstance(value, dict)}
            if isinstance(failures, dict)
            else {}
        )
        logger.info(f"{tag()} ✅ 已加载 {len(self._snapshots)} 份 AI 每日日程快照")
        return True

    def load_readonly(self) -> bool:
        """只读加载快照文件：**不创建 secret、不写任何文件**。

        供人格日程查看页使用——AI 日程关闭时也允许查看已有快照，
        但不能因为查看页面而创建 secret 或新数据文件。无 secret 时
        快照仍可枚举（persona_hash 是 key 的一部分，枚举不依赖 secret）。
        """
        if self._secret is None:
            data = load_mapping(self._secret_path)
            raw = str((data or {}).get("secret", "")).strip()
            if raw:
                try:
                    secret = bytes.fromhex(raw)
                    if len(secret) >= 32:
                        self._secret = secret
                except ValueError:
                    pass

        data = load_mapping(self._snapshot_path)
        if data is None:
            self._snapshots = {}
            self._failures = {}
            return True
        if data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            self._snapshots = {}
            self._failures = {}
            return True
        snapshots = data.get("snapshots", {})
        failures = data.get("failures", {})
        self._snapshots = (
            {str(key): value for key, value in snapshots.items() if isinstance(value, dict)}
            if isinstance(snapshots, dict)
            else {}
        )
        self._failures = (
            {str(key): value for key, value in failures.items() if isinstance(value, dict)}
            if isinstance(failures, dict)
            else {}
        )
        return True

    # ==================== 只读枚举（人格日程查看页） ====================

    def list_snapshots(self) -> list[dict]:
        """返回全部 ready 快照的深拷贝（不含失败记录）。"""
        return [
            copy.deepcopy(snapshot)
            for snapshot in self._snapshots.values()
            if isinstance(snapshot, dict) and snapshot.get("status") == "ready"
        ]

    def snapshots_by_persona(self) -> dict[str, list[dict]]:
        """按 persona_hash 分组的 ready 快照（日期/时区去重后的元信息）。"""
        grouped: dict[str, list[dict]] = {}
        for snapshot in self.list_snapshots():
            persona = str(snapshot.get("persona_hash", "")).strip()
            if not persona:
                continue
            grouped.setdefault(persona, []).append(
                {
                    "local_date": str(snapshot.get("local_date", "")),
                    "timezone": str(snapshot.get("timezone", "")),
                    "snapshot_id": str(snapshot.get("snapshot_id", "")),
                    "generated_at": str(snapshot.get("generated_at", "")),
                }
            )
        return grouped

    def _load_or_create_secret(self) -> bool:
        data = load_mapping(self._secret_path)
        raw = str((data or {}).get("secret", "")).strip()
        if raw:
            try:
                secret = bytes.fromhex(raw)
                if len(secret) >= 32:
                    self._secret = secret
                    return True
            except ValueError:
                pass
            logger.warning(f"{tag()} ⚠️ AI 日程 persona secret 无效，将重新生成")

        secret = secrets.token_bytes(32)
        if not atomic_write_yaml(
            self._secret_path,
            {"version": 1, "secret": secret.hex()},
            header="time_awareness AI 日程 Persona HMAC secret（自动生成，请勿公开）",
        ):
            logger.error(f"{tag()} ❌ 无法持久化 AI 日程 persona secret")
            return False
        try:
            os.chmod(self._secret_path, 0o600)
        except OSError:
            # Windows/部分挂载不支持 POSIX mode；文件仍位于插件私有数据目录。
            pass
        self._secret = secret
        return True

    def _require_secret(self) -> bytes:
        if self._secret is None:
            raise RuntimeError("AI 日程 persona secret 尚未就绪")
        return self._secret

    def persona_hash(self, persona_id: str) -> str:
        """将 effective Persona ID 转成不可逆的 Person 维度身份键。

        同一 Persona 的多个 Bot 共享同一身份键（Person 维度隔离）。
        """
        value = str(persona_id or "").strip()
        if not value:
            raise ValueError("persona_id 不能为空")
        digest = hmac.new(
            self._require_secret(),
            value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"persona_{digest[:24]}"

    @staticmethod
    def snapshot_key(
        persona_hash: str,
        local_date: str | datetime.date,
        timezone: str = "",
    ) -> str:
        """主键 = Persona + 本地日期 + 时区（v4：timezone 进入持久化 key）。

        时区进入 key 后，不同时区访问同一 Persona/日期各自生成独立快照，
        互不覆盖（此前时区只是校验字段，会在跨时区访问时反复互相覆盖）。
        """
        date_text = local_date.isoformat() if isinstance(local_date, datetime.date) else str(local_date)
        tz = str(timezone or "").strip() or "system-local"
        return f"{persona_hash}:{date_text}:{tz}"

    def get(
        self,
        persona_hash: str,
        local_date: str | datetime.date,
        timezone: str = "",
    ) -> dict | None:
        snapshot = self._snapshots.get(self.snapshot_key(persona_hash, local_date, timezone))
        return copy.deepcopy(snapshot) if isinstance(snapshot, dict) else None

    def get_failure(
        self,
        persona_hash: str,
        local_date: str | datetime.date,
        timezone: str = "",
    ) -> dict | None:
        failure = self._failures.get(self.snapshot_key(persona_hash, local_date, timezone))
        return copy.deepcopy(failure) if isinstance(failure, dict) else None

    def save_ready(
        self,
        snapshot: dict,
        *,
        retention_days: int,
        today: datetime.date,
    ) -> bool:
        """原子切换 active snapshot；写盘失败时恢复旧内存状态。"""
        persona_hash = str(snapshot.get("persona_hash", "")).strip()
        local_date = str(snapshot.get("local_date", "")).strip()
        timezone = str(snapshot.get("timezone", "")).strip()
        if not persona_hash or not local_date:
            raise ValueError("snapshot 缺少 persona_hash/local_date")
        key = self.snapshot_key(persona_hash, local_date, timezone)
        old_snapshots = copy.deepcopy(self._snapshots)
        old_failures = copy.deepcopy(self._failures)
        self._snapshots[key] = copy.deepcopy(snapshot)
        self._failures.pop(key, None)
        self._cleanup_in_memory(retention_days=retention_days, today=today)
        if self._save():
            return True

        self._snapshots = old_snapshots
        self._failures = old_failures
        return False

    def record_failure(
        self,
        *,
        persona_hash: str,
        local_date: str | datetime.date,
        failed_at: str,
        error_type: str,
        retention_days: int,
        today: datetime.date,
        timezone: str = "",
    ) -> bool:
        """记录去详情化失败状态，不覆盖同日仍可用的旧快照。"""
        key = self.snapshot_key(persona_hash, local_date, timezone)
        old_snapshots = copy.deepcopy(self._snapshots)
        old_failures = copy.deepcopy(self._failures)
        self._failures[key] = {
            "persona_hash": persona_hash,
            "local_date": local_date.isoformat() if isinstance(local_date, datetime.date) else str(local_date),
            "timezone": str(timezone or "").strip(),
            "failed_at": failed_at,
            "error_type": str(error_type or "unknown")[:80],
        }
        self._cleanup_in_memory(retention_days=retention_days, today=today)
        if self._save():
            return True
        self._snapshots = old_snapshots
        self._failures = old_failures
        return False

    def cleanup(self, *, retention_days: int, today: datetime.date) -> bool:
        old_snapshots = copy.deepcopy(self._snapshots)
        old_failures = copy.deepcopy(self._failures)
        before = (len(self._snapshots), len(self._failures))
        self._cleanup_in_memory(retention_days=retention_days, today=today)
        after = (len(self._snapshots), len(self._failures))
        if before == after or self._save():
            return True
        self._snapshots = old_snapshots
        self._failures = old_failures
        return False

    def _cleanup_in_memory(self, *, retention_days: int, today: datetime.date) -> None:
        retention_days = max(1, min(365, int(retention_days)))
        # retention_days 是包含今天在内的总天数：30 表示今天 + 前 29 天。
        cutoff = today - datetime.timedelta(days=retention_days - 1)

        def keep(record: Any) -> bool:
            if not isinstance(record, dict):
                return False
            try:
                return datetime.date.fromisoformat(str(record.get("local_date", ""))) >= cutoff
            except ValueError:
                return False

        self._snapshots = {key: value for key, value in self._snapshots.items() if keep(value)}
        self._failures = {key: value for key, value in self._failures.items() if keep(value)}

    def _save(self) -> bool:
        saved = atomic_write_yaml(
            self._snapshot_path,
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "snapshots": self._snapshots,
                "failures": self._failures,
            },
            header="time_awareness AI 每日日程快照（自动生成）",
        )
        if saved:
            try:
                os.chmod(self._snapshot_path, 0o600)
            except OSError as exc:
                logger.warning(f"{tag()} ⚠️ 无法收紧日程快照文件权限: {exc}")
        return saved
