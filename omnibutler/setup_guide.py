"""Plain-language setup guides + local secret storage.

Getting a device key (a Xiaomi miIO token, a Tuya local_key, an HA
long-lived token) is the one step a new user cannot skip, and it always
happens **on the user's own accounts and devices**. OmniButler has no
server and receives nothing: the guides below walk the user through
fetching each key themselves, and :func:`store_secret` writes the key
they hand over into their local config file - mode 0600, existing values
never echoed back, nothing printed, nothing uploaded.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from omnibutler.config import ConfigError

_MIIO_GUIDE = """\
小米设备（miIO token）—— 自己动手拿钥匙，全程在你自己的设备上

每台小米设备都有一把自己的钥匙，叫 token，一串 32 位的十六进制字符。
桥控制设备靠它，不靠你的小米账号密码。拿 token 要过小米的云一次
（这是小米的设计，绕不开），但 token 一旦拿到手，之后的控制全是
本地局域网直连，不再经过小米的云，token 也只存在你这台电脑里。

步骤：
1. 准备一台电脑（就是跑 OmniButler 的这台），在它上面运行一个开源的
   token 提取小工具——搜 “Xiaomi cloud tokens extractor” 就能找到，
   只用你自己信任来源的版本，别用要你把账号填进网页的那种在线工具。
2. 运行工具时登录你自己的小米账号（账号密码只输进工具的登录框，
   不要发给任何人，也不要写进聊天里）。
3. 工具会列出你名下所有设备，每台后面跟着它的 token。找到你要接的
   那台（对一下设备名和型号），把那串 32 位字符复制下来。
4. 回到 OmniButler 的设置，把 token 存进本机配置（存的时候用
   store_secret，或手动写 config.json 的 miio.devices 里对应那台的
   token 字段）。存好之后就可以退出小米账号了。
5. 验证：跑一次健康检查（doctor），它会真的跟设备握一次手，
   token 对不对当场就知道。注意：设备如果在米家里被删除又重新
   配对，token 会变，需要按上面步骤重新拿一次。

记住：token 只存这台电脑，配置文件权限是 0600（只有你能读），
OmniButler 没有服务器，不会把 token 上传到任何地方。
"""

_TUYA_GUIDE = """\
涂鸦设备（local_key）—— 自己动手拿钥匙，全程在你自己的账号里

涂鸦设备局域网直连的钥匙叫 local_key。拿它需要借涂鸦官方的 IoT
平台查一次（用你自己的账号），查到之后控制全走家里局域网，
不再经过涂鸦的云发指令，local_key 也只存在你这台电脑里。

步骤：
1. 打开涂鸦 IoT 平台（iot.tuya.com），用你自己的邮箱注册/登录
   （这是涂鸦官方平台，不是第三方）。
2. 在平台里创建一个「云项目」（开发方式选自定义/智能家居均可，
   按页面默认走），创建时记下项目给的 Access ID 和 Access Secret——
   这是查钥匙用的，也只存你本地。
3. 在项目里找到「设备」→「关联涂鸦 App 账号」，用你平时用的
   智能生活 / Tuya Smart App 扫码关联。关联完，你 App 里的设备
   会出现在项目的设备列表里。
4. 点开要接的那台设备，详情里有 Device ID 和 Local Key（本地密钥）。
   把这两个都复制下来。
5. 回到 OmniButler 的设置：Device ID 填 device_id、家里路由器给
   这台设备分配的 IP 填 ip、Local Key 用 store_secret 存进本机
   配置的 tuya.devices。不知道 IP 就去路由器后台的已连接设备里查，
   最好顺手给它绑定一个固定 IP，省得日后变来变去。
6. 验证：跑一次健康检查（doctor），确认每台设备都填齐了。
   注意：设备在 App 里删除重加之后 local_key 会变，要重查一次。

记住：local_key 只存这台电脑，配置文件权限 0600，
OmniButler 没有服务器，不会把钥匙上传到任何地方。
"""

_HA_GUIDE = """\
Home Assistant（长期访问令牌）—— 两分钟，在你自己的 HA 里

如果你已经在用 Home Assistant，桥不需要你的 HA 密码，只需要一枚
你亲手生成的长期令牌，而且随时可以在 HA 里吊销。

步骤：
1. 打开你自己的 Home Assistant，点左下角你的头像进个人资料页。
2. 拉到「安全」部分，找到「长期访问令牌」，点「创建令牌」，
   名字随便起，比如就叫 OmniButler。
3. 生成的令牌只显示这一次，复制下来。
4. 回到 OmniButler 的设置，把令牌存进本机配置。推荐用引用的
   写法：config.json 的 ha 一节写 "token": "env:HA_TOKEN"，再把
   令牌放进环境变量 HA_TOKEN——这样配置文件里根本不出现令牌
   本身，发给别人看都没关系。图省事也可以直接存进 ha.token
   字段，配置文件权限是 0600，只有你能读。
5. ha.url 填你 HA 的地址（比如 http://192.168.1.10:8123）。
6. 验证：跑一次健康检查（doctor），它会真的调一次 HA 接口，
   令牌过期或被吊销它会直接告诉你。

记住：令牌只存在你这台电脑上，桥调 HA 走的是你家局域网；
哪天不想用了，在 HA 里把这枚令牌吊销，桥立刻就进不去了。
"""

_GUIDES = {
    "miio": _MIIO_GUIDE,
    "xiaomi": _MIIO_GUIDE,
    "tuya": _TUYA_GUIDE,
    "ha": _HA_GUIDE,
    "homeassistant": _HA_GUIDE,
    "home-assistant": _HA_GUIDE,
}


def guide_text(brand: str) -> str:
    """Return the plain-language key-fetching guide for one brand.

    ``brand`` is one of ``"miio"`` (alias ``"xiaomi"``), ``"tuya"`` or
    ``"ha"`` (alias ``"homeassistant"``), case-insensitive. The text
    explains every step the user performs on their own accounts and
    devices; it contains no real keys and no placeholders for any.
    """
    try:
        return _GUIDES[brand.strip().lower()]
    except (KeyError, AttributeError):
        raise ValueError(
            f"no setup guide for {brand!r}; available: miio, tuya, ha"
        ) from None


def _descend(container: Any, segment: str, next_segment: str | None) -> Any:
    """One step into a nested config structure, creating as needed."""
    want_list = next_segment is not None and next_segment.isdigit()
    if isinstance(container, list):
        index = int(segment)
        while len(container) <= index:
            container.append([] if want_list else {})
        if not isinstance(container[index], (dict, list)):
            container[index] = [] if want_list else {}
        return container[index]
    if not isinstance(container, dict):
        raise ConfigError(f"cannot store a secret through non-object segment {segment!r}")
    child = container.get(segment)
    if not isinstance(child, (dict, list)):
        child = [] if want_list else {}
        container[segment] = child
    return child


def store_secret(config_path: str | Path, key_path: str, value: str) -> None:
    """Store one secret in the local config file, quietly and safely.

    ``key_path`` is dotted - ``"ha.token"``,
    ``"miio.devices.0.token"``, ``"tuya.devices.1.local_key"`` (numeric
    segments index into lists, created as needed). The file is created
    when missing and rewritten atomically with mode 0600 (owner-only);
    an existing file's other settings are preserved. A config file that
    exists but is not valid JSON raises :class:`ConfigError` instead of
    being clobbered. The value is never printed, logged or returned -
    and neither is the value it replaces.
    """
    path = Path(config_path)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"config file {path} is not valid JSON ({exc.msg} at line "
                f"{exc.lineno}); refusing to overwrite it - fix it by hand first"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"config file {path} must contain a JSON object at the top level"
            )
        data = parsed

    segments = [s for s in key_path.split(".") if s != ""]
    if not segments:
        raise ValueError("key_path must name a field, e.g. 'ha.token'")
    container: Any = data
    for position, segment in enumerate(segments[:-1]):
        container = _descend(container, segment, segments[position + 1])
    last = segments[-1]
    if isinstance(container, list):
        index = int(last)
        while len(container) <= index:
            container.append({})
        container[index] = value
    elif isinstance(container, dict):
        container[last] = value
    else:  # pragma: no cover - _descend guarantees a container
        raise ConfigError(f"cannot store a secret at {key_path!r}")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
