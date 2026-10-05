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
- **统一 Agent 接口** — 一个零依赖的 MCP Server（stdio，外加可选的、带
  令牌鉴权的 HTTP 传输），12 个工具，任何 MCP 客户端都能接。特意说明：
  工具里**没有**"批准"这一项——确认只能由人在宿主机终端完成。
- **场景由本地引擎执行** — 场景是 YAML 规则（触发 + 条件 + 动作），由确定性
  引擎在本地运行，断网照跑、结果可预期。AI 负责听懂你的话、编写和调整
  场景，不进入实时控制回路。
- **安全护栏写在代码里** — 高风险动作（车库门、门锁、燃气）不会被直接
  执行，而是进入待确认队列等人工批准；每一次控制调用都写入本地审计日志。

## 架构

```
 AI Agent (MCP) → mcp_server → 场景引擎 → core（能力模型/注册表/事件/审计/路由）
                                              │
                       驱动：mock | homeassistant | miio | tuya | 美的 |
                             broadlink | matter | zigbee2mqtt | 终端（眼镜演示）
                                              │
                       device-data（按型号的事实数据）
```

五层结构与五种设备接入模式见 [docs/architecture.md](docs/architecture.md)。

## 5 分钟快速开始（不需要任何硬件）

需要 Python 3.11+。

```bash
git clone https://github.com/Tiybai/tiybai-omnibutler.git
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
`get_pending_confirmations`。就这些——**没有批准用的工具**。批准是
人在宿主机上的事：`tob pending` 看队列，`tob confirm <id>` 批准执行，
`tob reject <id>` 拒绝。

不在宿主机上跑的 Agent 用 `tob mcp --http`：同样的工具走 HTTP
（默认 `127.0.0.1:8765`），必须带 `$OMNIBUTLER_HTTP_TOKEN` 的令牌；
远程访问请套 Cloudflare Access 或 WireGuard，不要裸奔。

**不用终端也能批准：** `tob approvals` 起一个本地小网页（独立口令
`$OMNIBUTLER_APPROVALS_TOKEN`，默认只在本机打开），排队的动作列在
页面上，点「批准执行」或「拒绝」就行；或者在 Mac 上 `tob run --notify`，
有新待确认时弹原生对话框。两种都是人在 MCP 之外的点击，AI 仍然批
不了任何东西。Mac 弹窗的默认按钮是「拒绝」，不回答永远不等于同意。

常驻运行用 `tob run`（定时触发 + 设备状态轮询）；接真设备前先跑
`tob doctor` 体检，取钥匙的方法看 `tob setup miio` / `tob setup tuya`。
v0.4 新增：`tob onboard` 一次扫遍所有驱动、用大白话说清每台设备还缺
什么；`tob fetch-keys xiaomi|tuya` 用你自己的账号过云一次把本地钥匙
取回来（撞验证码/两步验证会明确停下，不硬闯）；`tob gateway` 接收
手机上报的数据流和到家/离家地理围栏事件。

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

v0.5（本版）：补齐轮次——不再留半接线的东西。MCP 补上数据流与
终端会话工具（共 12 个）；手机网关可以跑进 daemon 里
（`tob run --gateway`，一个进程、场景只执行一遍）；`tob doctor`
开始查 Matter 控制器与 Zigbee2MQTT 的可达性、以及配置文件版本
健康；配置文件正式版本化（没写版本视为 v1、版本比程序新会明确
报错、首次回写留一次性 .bak 备份）；新增涂鸦云兜底驱动
（`--driver tuya_cloud`，本地实在走不通时经厂商云控制——故意
不进 all 组合，走云必须显式选）；`tob setup` 补齐 matter /
zigbee2mqtt / gateway / tuya_cloud 四份指南。全部仍然只经假
设备测试——**实机验证还是项目最大的缺口**（见 issue #2 / #4）。

- [x] v0.2 — 按净室规格实现第一批真实本地驱动；开放 device-data 贡献
- [x] v0.3 — 确认带外化强制执行；daemon；MCP over HTTP；doctor 与
      setup 指南；美的与 Broadlink 驱动
- [x] v0.4 — Matter（控制器客户端）+ Zigbee2MQTT 驱动；手机网关与
      数据流；终端会话；发现纳管与云取钥；Docker/常驻服务打包
- [x] v0.5 — MCP 数据流/会话工具；网关嵌入 daemon；doctor 覆盖
      新驱动；配置版本化落地；涂鸦云兜底驱动；全部驱动的
      setup 指南
- [ ] v0.6 — 实机验证轮次（需要有真设备的朋友，见 issue #2 / #4）
- [ ] 之后 — PyPI 正式发布（需要维护者本人的 PyPI 账号）；更多
      无本地通道品牌的厂商云兜底

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
