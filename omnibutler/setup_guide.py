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
from pathlib import Path
from typing import Any

from omnibutler.config import ConfigError, save_config

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

_MATTER_GUIDE = """\
Matter 设备 —— 桥不自己配网，它去接一台你已经在跑的 Matter 控制器

这条路和其他驱动不一样：桥本身不当 Matter 控制器。真正管 Matter
网络的是另一个服务，比如 matterjs-server（Home Assistant 的
Matter 集成背后也是这一类服务）。桥做的是连上那台控制器，把它管
的设备翻译成桥的统一模型。所以这条路没有「钥匙」要拿，要准备的
是：一台已经在跑、设备已经配好对的控制器，和它的地址。

步骤：
1. 先把 Matter 控制器跑起来（比如 matterjs-server，按它自己的
   文档安装和启动），确认你的 Matter 设备在控制器那边已经配好
   对、能正常开关。这一步在控制器那边完成，桥帮不上，也不假装
   能配网。
2. 装桥的 Matter 组件（一个 WebSocket 小库），在跑桥的终端里运行：
   pip install "tiybai-omnibutler[matter]"
3. 告诉桥控制器在哪：把环境变量 MATTER_SERVER_URL 设成控制器的
   WebSocket 地址。控制器跑在同一台机器、用默认端口时默认就是
   ws://127.0.0.1:5580/ws，这一步可以省；控制器在别的机器上，
   就把地址里的主机和端口换成它的。
4. （可选）想给设备起顺口的名字和房间，用环境变量
   MATTER_NODES_JSON 写一份 node 编号到名字/房间的对照清单。这
   只是标签：设备有什么本事，桥是从控制器现学的，不用你编。
5. 以后配新设备：配对码是控制器那边生成的（或者看设备/包装上
   印的配对码），在桥里对一台叫 matter-controller 的「设备」
   执行 commission 动作，把码递过去——真正执行配对的是
   控制器，桥只是转发。配好后设备会出现在桥的设备列表里，
   名字长得像 matter-<编号>-<端点>。
6. 验证：跑一次健康检查（doctor），或直接列一下设备，能看到
   Matter 设备就是通了。

记住：控制器连不上时，桥会明确报错，不会装作设备在那儿；
桥和控制器之间走你的局域网/本机，不经过任何厂商的云。
"""

_Z2M_GUIDE = """\
Zigbee 设备 —— 经 Zigbee2MQTT 接入，配对先在那边配好

桥不自己讲 Zigbee 协议。你在家里跑一个 Zigbee2MQTT（插一个
Zigbee USB 协调器），它把 Zigbee 设备翻译成 MQTT 消息，桥订
这些消息来认设备、下指令。所以这条路也没有要去哪个平台拿的
「钥匙」：设备先在 Zigbee2MQTT 里配好对，桥这边只要知道 MQTT
中转站（broker）的地址就行。

步骤：
1. 先确认 Zigbee2MQTT 已在跑（按它自己的文档装好、配好协调
   器），家里的 Zigbee 设备已经在它里面配完对、在它的页面里
   能开关。
2. 装桥的 Zigbee 组件，在跑桥的终端里运行：
   pip install "tiybai-omnibutler[zigbee]"
3. 告诉桥 broker 在哪：把环境变量 Z2M_MQTT_URL 设成 broker 的
   地址，形状像 mqtt://192.168.1.10:1883；broker 要账号密码的，
   写成 mqtt://用户名:密码@192.168.1.10:1883。
   注意：这个地址里可能带着 broker 的密码，按钥匙对待——只放
   环境变量，别写进会发给别人看的配置文件，也别贴进聊天里。
4. （可选）设备可以不预先登记：Zigbee2MQTT 报告过的设备，桥
   会自动认下来。想固定桥这边的设备 id、名字和房间，在本地
   配置的 zigbee2mqtt.devices 里按 friendly_name（设备在
   Zigbee2MQTT 里的名字）登记，或者用环境变量 Z2M_DEVICES_JSON
   给一份同样的清单；Zigbee2MQTT 的主题前缀改过的话，再设
   Z2M_BASE_TOPIC。
5. 验证：跑一次健康检查（doctor），或列一下设备，能看到
   z2m- 开头的设备；实际开关一次，状态对得上就是通了。

记住：控制链是 桥 → MQTT broker → Zigbee2MQTT → 设备，
全程在你家局域网里，不走任何厂商的云；哪一环没在跑，桥都会
明确报连不上，不会装作控制成功了。
"""

_GATEWAY_GUIDE = """\
手机网关 —— 不是拿设备钥匙，是给手机开一个往桥里送数据的口

有些数据不在设备上，在你手机里：步数、睡眠、位置。手机网关是
桥在本机开的一个小接口，手机（或手机上的自动化工具）把数据
POST 进来：健康这类数据存成一条条数据流，到家/离家这种地理
围栏事件能直接触发场景。它只收数据，不控制任何设备。

步骤：
1. 先给网关想一把令牌：自己用密码管理器之类的工具生成一串
   够长的随机字符串，这就是网关令牌。它和 MCP 的令牌、批准
   页的口令都是分开的——手机上只存这一把，就算漏了，也只能
   往桥里灌数据，干不了别的。
2. 在跑桥的机器上把这把令牌放进环境变量
   OMNIBUTLER_GATEWAY_TOKEN（只放环境变量，别写进代码、配置
   文件或聊天里），然后启动：终端里运行 tob gateway，默认
   只监听本机 127.0.0.1:8767；或者用 tob run --gateway，把
   网关和常驻的桥跑在同一个进程里（推荐，场景只会执行
   一遍）。没设令牌时它会拒绝启动，这是故意的。
3. 手机往哪儿发：两个入口都要在请求头带上
   Authorization: Bearer <网关令牌>。
   - 送数据点：POST 到 /ingest，形状是
     {"stream": {"id": "phone-steps", "kind": "health.steps",
     "source": "phone", "unit": "count"},
     "points": [{"ts": 时间戳, "value": 数值}]}
   - 送事件：POST 到 /event，地理围栏长这样：
     {"type": "geofence", "zone": "home", "transition": "enter"}
     （enter 是到家、exit 是离家，会触发对应的场景。）
   字段细节以网关代码（omnibutler/gateway.py）开头的说明为准。
4. 手机不在同一局域网时，别把 8767 端口直接暴露到公网：
   按本项目的规矩，套 Cloudflare Access 或 WireGuard 进来。

记住：令牌只用来比对，桥不会把令牌写进日志。想换令牌就改
环境变量、重启网关，手机侧同步换掉即可。
"""

_TUYA_CLOUD_GUIDE = """\
涂鸦云（两件事：一次性取钥 + 云兜底控制）

先分清涂鸦云在桥里出现的两个地方，别混：

【一】取钥（tob fetch-keys tuya）——云只用这一次
和 `tob setup tuya` 那篇本地指南是同一件事的两种做法，目标都是
拿到每台设备的 local_key。区别只在查钥匙的动作：本地指南是你
在 IoT 平台网页上一台台点开抄；这条是桥拿着你 IoT 项目凭据，
自己去涂鸦云把所有设备的 local_key 一次查回来。查完之后，
日常控制照样走家里局域网本地直连，不靠云。

步骤：
1. 在涂鸦 IoT 平台（iot.tuya.com）用你自己的账号创建一个
   云项目、关联你的智能生活 / Tuya Smart App 账号。记下项目
   给的 Access ID 和 Access Secret；再在 IoT 平台项目里找到
   你账号的 UID（在关联账号/成员相关的页面里，位置写不准，
   就在项目里找标着 UID 的那串）。
2. 运行 tob fetch-keys tuya。它会一样样问你（Access Secret
   输入时不回显）；想免交互，先设好三个环境变量：
   TUYA_ACCESS_ID、TUYA_ACCESS_SECRET、TUYA_UID。
3. 命令加上 --store，查回来的 local_key 会按设备合并存进
   本机配置（权限 0600）。注意涂鸦云不给设备的局域网 IP：
   存完后照本地指南，把每台设备的 ip 在路由器后台查出来补
   上，最好顺手绑固定 IP。

【二】云兜底控制（--driver tuya_cloud）——本地实在走不通时才用
有些设备拿不到 local_key、或者本地协议对不上，还有一条路：
让桥直接经涂鸦云控制它们（状态读取和开关/亮度/色温控制都
走云端 API）。先把丑话说前面：这条路依赖厂商云和外网，
断网、云接口变动都会让它失效；速度和可靠性都不如本地直连。
所以它只当兜底：桥的 all 组合里故意不包含它，必须你自己
显式选 --driver tuya_cloud 才会走云，不会不知不觉用上。

配置（二选一）：
- 环境变量：TUYA_CLOUD_ACCESS_ID、TUYA_CLOUD_ACCESS_SECRET
  （可选 TUYA_CLOUD_UID——不给时桥会从登录返回里取；
  可选 TUYA_CLOUD_BASE_URL 换数据中心，默认中国区
  openapi.tuyacn.com）。用的是同一个 IoT 项目的凭据。
- 或者写进本机 config.json 的 tuya_cloud 一节：access_id、
  access_secret 两个字段，值一律用 env: 引用（比如
  "access_secret": "env:TUYA_CLOUD_ACCESS_SECRET"），
  配置文件里不落明文。Access Secret 是钥匙级别的东西：
  只放环境变量或当场输入，别写进聊天，别提交进代码仓库。
配好后 tob --driver tuya_cloud devices 就能列出云端设备。

两件事共同的提醒：
- 取钥那一步撞上验证码或两步验证，桥会直接停下，报
  needs_human_verification——它不会、也不应该替你过验证。
  这种情况就回本地指南，网页上一台台抄。
- 云接口失败时桥会把原因分类说清（凭据不对 / 网络问题 /
  返回异常），不会假装控制成功。
"""

_XIAOMI_CLOUD_GUIDE = """\
小米云兜底控制（--driver xiaomi_cloud）——本地走不通时才用

先说清这条路是干什么的。桥控制小米设备的正道是本地直连
（--driver miio）：靠每台设备自己的 token，在家里局域网里
控制，断网照样跑、不经过小米的服务器（拿 token 的办法看
`tob setup miio`）。但有些情况本地这条路走不通：某台设备
的 token 拿不到、设备在桥够不着的网络里、或者本地协议对
不上。这时还有一条兜底路：桥拿你的小米账号登录小米云，
经云端控制这些设备。丑话说前面：这条路依赖小米云和外网，
断网就用不了，速度和可靠性都不如本地直连，而且小米的服务
器会看到每条指令。所以它只当兜底：桥的 all 组合里故意不
包含它，必须你自己显式选 --driver xiaomi_cloud 才会走云。

什么时候值得用：
- 某台小米设备折腾半天还是拿不到 token，先用云兜底顶上，
  别让一台设备卡住全屋；
- 设备不在家里这个局域网（比如另一处住所），本地够不着。

要填什么（小米账号 + 密码，二选一）：
- 环境变量：XIAOMI_CLOUD_USERNAME（你的小米账号：邮箱、
  手机号或小米 ID）、XIAOMI_CLOUD_PASSWORD（账号密码）；
  账号不是中国区的再加 XIAOMI_CLOUD_COUNTRY（如 de、us，
  默认 cn）。
- 或者写进本机 config.json 的 xiaomi_cloud 一节：
  username、password、country 三个字段；password 用 env:
  引用（如 "password": "env:XIAOMI_CLOUD_PASSWORD"），
  配置文件里不落明文密码。

关于密码，放心在这几点上：
- 密码只在跑桥的这台机器上、登录那一刻在内存里用一次
  （按小米自己的登录方式算个哈希发出去），不写进日志、
  不出现在报错里、更不会上传到小米以外的任何地方；
- 桥没有服务器，账号密码不出这台机器（除了登录小米云
  本身）；
- 介意密码长期放环境变量，就只在启动桥时临时给，用完清掉。

会明确停下来的情况：
- 小米在登录时弹验证码或要求两步验证，桥会直接停下，报
  needs_human_verification——它不会、也不应该替你过验证。
  遇到这种情况，先在浏览器或米家 App 里正常登录一次把
  验证过掉，再回来重试；实在过不去，就回本地路线
  （tob setup miio）一台台拿 token。
- 云端登录态过期时桥会自动重新登录一次再重试；账号密码
  真错了，它会直说凭据不对，不会假装控制成功。

配好后：tob --driver xiaomi_cloud devices 就能列出账号
名下、桥有控制映射的设备（空调、空气净化器这些已有映射
的品类）。没有映射的型号会被跳过——云端能看见它，也不
等于桥会控制它，这一点桥不会糊弄你。
"""

_GUIDES = {
    "miio": _MIIO_GUIDE,
    "xiaomi": _MIIO_GUIDE,
    "tuya": _TUYA_GUIDE,
    "tuya_cloud": _TUYA_CLOUD_GUIDE,
    "tuya-cloud": _TUYA_CLOUD_GUIDE,
    "xiaomi_cloud": _XIAOMI_CLOUD_GUIDE,
    "xiaomi-cloud": _XIAOMI_CLOUD_GUIDE,
    "mi-cloud": _XIAOMI_CLOUD_GUIDE,
    "ha": _HA_GUIDE,
    "homeassistant": _HA_GUIDE,
    "home-assistant": _HA_GUIDE,
    "matter": _MATTER_GUIDE,
    "zigbee2mqtt": _Z2M_GUIDE,
    "zigbee": _Z2M_GUIDE,
    "z2m": _Z2M_GUIDE,
    "gateway": _GATEWAY_GUIDE,
}


def guide_text(brand: str) -> str:
    """Return the plain-language key-fetching guide for one brand.

    ``brand`` is one of ``"miio"`` (alias ``"xiaomi"``), ``"tuya"``,
    ``"tuya_cloud"`` (alias ``"tuya-cloud"``), ``"xiaomi_cloud"``
    (aliases ``"xiaomi-cloud"`` / ``"mi-cloud"``), ``"ha"`` (aliases
    ``"homeassistant"`` / ``"home-assistant"``), ``"matter"``,
    ``"zigbee2mqtt"`` (aliases ``"zigbee"`` / ``"z2m"``) or
    ``"gateway"``, case-insensitive. The text explains every step the
    user performs on their own accounts and devices; it contains no
    real keys, only placeholders the user replaces with their own.
    """
    try:
        return _GUIDES[brand.strip().lower()]
    except (KeyError, AttributeError):
        raise ValueError(
            f"no setup guide for {brand!r}; available: miio, tuya, "
            f"tuya_cloud, xiaomi_cloud, ha, matter, zigbee2mqtt, gateway"
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

    # Write through the canonical config writer: it stamps the format
    # version, keeps the one-time .bak of a pre-versioning file, and
    # writes atomically with mode 0600.
    save_config(path, data)
