# 手机网关（tob gateway）

网关是给**手机**用的一个小接口。手机上的 App（比如快捷指令、Tasker、
自带的健康 App 导出工具）把两类东西发给它：

1. **数据流**：步数、睡眠、心率、体重这类「只看不改」的记录，发进来存好，
   之后你的 AI 可以用 MCP 工具（`list_data_streams` / `get_stream_data`）查。
2. **事件**：最重要的是「到家 / 离家」的地理围栏事件——手机一进家门范围
   就发一条，网关把它交给场景引擎，该开的空调、净化器自动就开了。

网关只负责「收」，不直接控制任何设备；真正执行动作的是场景引擎，
高风险动作照旧进确认队列等你本人点头，这一点和其它入口没有区别。

## 启动

先给网关设一个口令（自己随便定一个长一点的，只给你的手机用）：

```bash
export OMNIBUTLER_GATEWAY_TOKEN="<your-gateway-token>"
tob gateway
```

- 默认只在本机监听：`127.0.0.1:8767`。想让局域网里的手机连，加
  `--host 0.0.0.0`（**只在自己家网络里这么干**，并确保口令够长）。
- **没有口令拒绝启动**：`OMNIBUTLER_GATEWAY_TOKEN` 没设，`tob gateway`
  会直接报错退出，不会裸奔。
- 换端口：`tob gateway --port 9000`。用哪个驱动接设备：`--driver` 和
  其它命令一样（默认 mock 演示家，真设备按你的配置来）。

除了 `/health`，所有请求都要带口令，放在请求头里：

```
Authorization: Bearer <your-gateway-token>
```

## GET /health

探活，不需要口令。

```
GET /health
→ 200 {"status": "ok"}
```

## POST /ingest —— 上报数据

请求体是一个 JSON 对象，里面有一个 `points` 数组。两种写法可以混用。

**写法一：一批数据共用一个流描述**（同一类数据一次报多个点，最常用）：

```json
{
  "stream": {"id": "phone-steps", "kind": "health.steps",
             "source": "phone", "unit": "count"},
  "points": [{"ts": 1760000000.0, "value": 100},
             {"ts": 1760003600.0, "value": 260}]
}
```

**写法二：每个点自带说明**（一次报好几类数据时用）：

```json
{
  "points": [
    {"stream": {"id": "watch-sleep", "kind": "health.sleep",
                "source": "watch", "unit": "minutes"},
     "ts": 1760003600.0, "value": 432},
    {"stream_id": "phone-steps", "ts": 1760007200.0, "value": 400}
  ]
}
```

规矩就几条：

- `stream` 描述一条流：`id` 自己起名（同一条流一直用同一个 id）、
  `kind` 是点分的类型词（`health.steps`、`health.sleep`、
  `health.heart_rate`、`location`……）、`source` 写来源（`phone` /
  `watch`）、`unit` 写单位，可省。
- 每个点必须有 `value`；`ts` 是秒级时间戳，可省，省了就用网关收到
  的时间；`meta` 可省，是个自由的小对象（比如 `{"origin": "healthkit"}`）。
- 用 `stream_id` 引用一条流之前，这条流必须先被描述过（在同一批里
  先描述也算）——没描述过的流会被整批拒绝，不会写一半。
- 成功返回 `200 {"status": "ok", "accepted": 2}`，数字是这批收下的点数。

数据存在哪：追加写到状态目录的 `streams.jsonl`
（默认 `~/.omnibutler/streams.jsonl`，设了 `$OMNIBUTLER_STATE_DIR`
就跟它走）。一个点一行，重启不丢。

## POST /event —— 上报事件

地理围栏（到家/离家）的形状是固定的：

```json
{"type": "geofence", "zone": "home", "transition": "enter"}
```

- `zone`：地点名，和场景里写的一致（自带场景用 `home`，
  车库场景用 `garage_gate`）。
- `transition`：只能是 `enter`（进入）或 `exit`（离开）。
- 可选 `ts`：事件发生的秒级时间戳，省了用收到时的时间。

发对了返回 `200 {"status": "published", "type": "geofence"}`，然后：

- `zone=home, enter` → 触发「到家」场景（开空调、净化器等）；
- `zone=home, exit` → 触发「离家巡检」场景（该关的关掉）；
- 其它 zone（比如 `office`）也是合法事件，只是没有场景在听，
  什么都不会发生。

其它类型的事件（比如 `{"type": "presence", "person": "zhou",
"present": true}`）也能发，会原样进事件总线，`source` 记为 `phone`；
没有场景或工具在听的类型，发了也就发了。

## 错误码

- **401**：没带口令，或口令不对。检查 `Authorization: Bearer ...`
  里的口令与启动网关时的 `OMNIBUTLER_GATEWAY_TOKEN` 是否一致。
- **400**：请求体本身有问题——不是合法 JSON、不是 JSON 对象、
  `points` 不是数组、某个点没有 `value`、引用了从没描述过的流、
  地理围栏缺 `zone`/`transition` 或 `transition` 不是 `enter`/`exit`。
  返回体里有 `{"error": "..."}` 说明具体错在哪。整批校验，有一个
  点不合格，整批都不写。
- **404**：路径写错了（只有 `/health`、`/ingest`、`/event` 三个）。
- **413**：请求体太大（上限 1 MB）——分批报，别一次塞太多点。

## 一个重要注意事项：别和 tob run 同时各跑一份场景

`tob gateway` 是在**它自己的进程里**装配整套桥的（驱动、场景引擎
都在里面），所以地理围栏事件触发的场景，是在网关进程里执行的。

如果你同时又开着 `tob run`，那是**另一套**场景引擎。网关这边的
事件不会跑到 daemon 那边去，但两个进程都在轮询/控制同一批真设备
时，可能出现重复执行、状态互相覆盖这类混乱。所以日常用法二选一：

- 只开 `tob gateway`：手机上报 + 围栏场景都在它这儿跑，够用；
- 或者只开 `tob run` 做常驻管家，手机数据等需要时再单独开网关
  （注意这时围栏场景在网关进程执行，不在 daemon）。

MCP 那边查数据不受影响：`tob mcp` 的流工具每次都现读
`streams.jsonl`，网关在另一个进程里新写入的点，AI 下一次查就能看到。
