"""1.x Fernet 字段解密器 —— 迁移工具(tools/migrate_from_v1.py)专用输入侧解密。

★ R3 红线:Fernet 只允许在 app/infrastructure/secretstore/ import。
  tools/ 不在 ruff per-file-ignores 豁免名单里,所以 1.x 旧密文的 Fernet
  解密**不能写在 tools/**,必须放本目录(Fernet 的唯一合法栖身处)。
  迁移工具通过本模块的 V1FernetDecryptor 拿明文,再交给 2.0 SecretStore
  用新 master key 重加密 —— 全程 tools/ 不碰 Fernet。

1.x 加密细节(见 docs/legacy/V1_AS_IS.md §3.6 secret_crypto.py):
- 加密前缀 "fernet:"(老明文无前缀,原样返回)
- 算法 cryptography.fernet.Fernet(AES-128-CBC + HMAC-SHA256)
- key 来源优先级:
    env DATAOPS_CONFIG_SECRET → env DATAOPS_SECRET_KEY → config/.dataops_secret.key
- env 形态:任意字符串,内部 base64.urlsafe_b64encode(sha256(secret).digest())
- 本地文件:Fernet.generate_key() 写入的原始 urlsafe-b64 key(权限 0600)

本模块**只做解密(迁移输入)**,不参与 2.0 侧加密(那走 SecretStore)。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken  # noqa: TID251 - R3 contract seam

# 1.x secret_crypto.PREFIX
_V1_FERNET_PREFIX = "fernet:"


class V1SecretDecryptError(RuntimeError):
    """1.x 密文无法用提供的 v1 secret key 解密。错误信息不含明文。"""


def _derive_fernet_key(raw: bytes) -> bytes:
    """把 1.x secret key 物料归一化成合法 Fernet key。

    1.x 两种形态都要支持(secret_crypto.py 的 key 派生逻辑):
    - 直接是 Fernet.generate_key() 产物(urlsafe-b64,解码后 32 字节)→ 原样用
    - 任意字符串(env 形态)→ base64.urlsafe_b64encode(sha256(s).digest())
    """
    stripped = raw.strip()
    # 形态 1:已经是合法 Fernet key(urlsafe-b64 编码的 32 字节)。
    try:
        decoded = base64.urlsafe_b64decode(stripped)
        if len(decoded) == 32:
            return stripped
    except (ValueError, binascii.Error):
        pass
    # 形态 2:任意字符串 → sha256 派生(1.x env key 路径)。
    digest = hashlib.sha256(stripped).digest()
    return base64.urlsafe_b64encode(digest)


@dataclass(frozen=True)
class V1FernetDecryptor:
    """用 1.x `.dataops_secret.key`(或 env 形态字符串)解密旧 Fernet 字段。"""

    _fernet: Fernet

    @classmethod
    def from_key_file(cls, key_path: str | Path) -> V1FernetDecryptor:
        path = Path(key_path)
        if not path.exists():
            raise V1SecretDecryptError(f"v1 secret key file does not exist: {path}")
        if not path.is_file():
            raise V1SecretDecryptError(f"v1 secret key path is not a file: {path}")
        return cls.from_key_material(path.read_bytes())

    @classmethod
    def from_key_material(cls, raw: bytes | str) -> V1FernetDecryptor:
        material = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not material.strip():
            raise V1SecretDecryptError("v1 secret key material is empty")
        try:
            fernet = Fernet(_derive_fernet_key(material))
        except (ValueError, TypeError) as exc:
            raise V1SecretDecryptError("v1 secret key material is invalid") from exc
        return cls(fernet)

    def decrypt(self, stored: str) -> str:
        """解密 1.x 加密字段。

        - 空字符串 → 返回空字符串(1.x 未设置该字段的形态)。
        - 无 "fernet:" 前缀的老明文 → 原样返回(1.x 兼容老明文行为)。
        - 有前缀 → Fernet 解密;失败抛 V1SecretDecryptError(不含明文)。
        """
        if not stored:
            return ""
        if not stored.startswith(_V1_FERNET_PREFIX):
            # 1.x 行为:老明文(无前缀)原样返回。
            return stored
        ciphertext = stored[len(_V1_FERNET_PREFIX) :]
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise V1SecretDecryptError("v1 ciphertext could not be decrypted") from exc


__all__ = ["V1FernetDecryptor", "V1SecretDecryptError"]
