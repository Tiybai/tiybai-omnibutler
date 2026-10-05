# Tiybai OmniButler（万能管家）

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**一个开源智能设备桥：让 Muse、OpenClaw、Hermes 等 AI Agent 通过 MCP 统一控制
你生活中的智能设备，并用本地确定性场景引擎替你安排贴近生活的自动化。**

English version: [README.md](README.md)

> 本项目不是小米、涂鸦、美的、Home Assistant、Anthropic 等任何厂商的官方产品，
> 文中商标归各自所有者所有。

## 它解决什么问题

家里的设备各说各话：小米走 MiOT、涂鸦是 DP 点、美的有自己的局域网协议，
还有 Zigbee、Matter、蓝牙……而 AI Agent 说的是 MCP。OmniButler 站在中间：

- **统一能力模型** — 每个驱动把自家模型翻译成同一套词：`onoff`（开关）、
  `target_temperature`（目标温度）、`pm25`、`position`（窗帘/门位置）等，
  场景和 AI 不需要懂任何厂商。
- **统一 Agent 接口** — 一个零依赖的 MCP Server（stdio，JSON-RPC，2024-11-05
  规范），8 个工具，任何 MCP 客户端都能接。
- **场景由本地引擎执行** — 场景是 YAML 规则（触发 + 条件 + 动作），由确定性
  引擎在本地运行，断网照跑、结果可预期。AI 负责听懂你的话、编写和调整
  场景，不进入实时控制回路。
- **安全护栏写在代码里** — 高风险动作（车库门、门锁、燃气）不会被直接
  执行，而是进入待确认队列等人工批准；每一次控制调用都写入本地审计日志。

## 架构

```
 AI Agent (MCP) → mcp_server → 场景引擎 → core（能力模型/注册表/事件/审计/路由）
                                              │
                       驱动：mock | homeassistant |（miio、tuya 规划中）
                                              │
                       device-data（按型号的事实数据）
```

五层结构与五种设备接入模式见 [docs/architecture.md](docs/architecture.md)。

## 5 分钟快速开始（不需要任何硬件）

需要 Python 3.11+。

```bash
git clone https://github.com/zr9959/tiybai-omnibutler.git
cd tiybai-omnibutler
pip install -e .

tob devices                 # 一个虚拟的家：2 台空调、净化器、灯、窗帘、体脂秤、车库门
tob devices --state         # 带实时状态
tob set living_ac onoff true
tob set living_ac target_temperature 24
tob scenes                  # 列出并校验自带场景包
tob simulate                # 模拟"到家"：看场景链如何执行
tob simulate --event leave  # 模拟"离家"：全部关闭巡检
tob simulate --event pm25   # 模拟 PM2.5 飙升：净化器自动强档
tob simulate --event garage # 模拟到达车库门口：开门动作进入待确认队列（护栏演示）
```

接真实家庭（通过你已有的 Home Assistant）：

```bash
export HA_URL=http://192.168.1.10:8123
export HA_TOKEN=<你的 Home Assistant 长期访问令牌>
tob --driver homeassistant devices
```

## 让 AI 来控制（MCP）

```bash
tob mcp        # 在 stdio 上提供 MCP 服务
```

把任何 MCP 客户端指向这条命令即可，例如：

```json
{
  "mcpServers": {
    "omnibutler": { "command": "tob", "args": ["mcp"] }
  }
}
```

工具：`list_devices`、`get_device_state`、`set_device_property`、
`call_device_action`、`list_scenes`、`enable_scene`、
`get_pending_confirmations`、`confirm_action`。

高风险设备（演示家里的车库门）按设计拒绝工具直控。可以试一下：让 Agent
开车库门会被拒绝；再用场景请求开门（`examples/scenes/garage-arrival.yaml`），
动作会进入 `get_pending_confirmations` 等你确认，而不是直接执行。

## 自带场景（examples/scenes/）

| 场景 | 触发 | 动作 |
|---|---|---|
| `arrive-home` | 进入家地理围栏 | 客厅空调开 26°C、净化器自动档 |
| `leave-home-check` | 离开家地理围栏 | 全关巡检，防忘关 |
| `sleep-mode` | 22:30 | 主卧睡眠温度、关窗帘、净化器静音 |
| `air-quality-guard` | PM2.5 状态变化 | PM2.5 > 75 时净化器强档 |
| `garage-arrival` | 到达车库门口围栏 | 开门动作**进入人工确认队列**（高风险）；开灯正常执行 |

## 状态与路线图

v0.1（本版）：能力模型、Mock 驱动、Home Assistant 驱动、带高风险护栏的
场景引擎、MCP Server、CLI、完整测试。小米 miIO 与涂鸦本地驱动目前是带
接入点说明的骨架（`omnibutler/drivers/miio.py`、`tuya.py`），**尚不能用**，
请勿误解。

- [ ] v0.2 — 按净室规格实现第一批真实本地驱动；开放 device-data 贡献
- [ ] v0.3 — 手机网关模式（穿戴 / 健康只读管道）
- [ ] v0.4 — Matter 控制器；经 MQTT 对接外部 Zigbee2MQTT
- [ ] 之后 — 开放智能眼镜的终端模式；厂商云兜底通道

## 贡献设备

设备支持靠一台台实机验证攒出来：

1. 在 `device-data/` 加数据文件（只收事实，必须写 `source` 与
   `provenance`，见 `device-data/README.md`）；
2. 需要改驱动时按 [CONTRIBUTING.md](CONTRIBUTING.md)：必须附实机验证；
   参考过他人实现的，必须走[净室流程](docs/clean-room.md)；
3. 只收原创实现。我们向生态学习协议和事实，不移植他人的代码。

## 安全

风险分级、钥匙管理、白名单、审计与远程访问规则见
[docs/security.md](docs/security.md)。一句话：钥匙只存你本机；不对公网
暴露任何端口（远程只走 Cloudflare Access 或 WireGuard）；高风险设备
必须人工确认。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。全部依赖与参考
项目的许可证清单在 [docs/license-audit.md](docs/license-audit.md)。
