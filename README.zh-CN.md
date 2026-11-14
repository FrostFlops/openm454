# openm454

[English](README.md) | 简体中文

[![CI](https://github.com/FrostFlops/openm454/actions/workflows/ci.yml/badge.svg)](https://github.com/FrostFlops/openm454/actions/workflows/ci.yml)

这是 **DreamSourceLab DSCope M454** USB 混合信号示波器(4 路模拟通道最高 400 MSa/s,8 路数字通道,内置 0-3 V 信号发生器)的开源驱动、命令行界面和 MCP 服务器。它的目标是让 AI 智能体(Claude Code 或任意 MCP 客户端)无需厂商图形界面,就能自己采集并分析真实信号。

M454 使用比开源 DSView 所支持设备更新的("v3")USB 协议。该协议是通过对 DSWave 1.2.1 做静态分析、注入命令驱动 DSWave 自身的硬件层以及 USB 抓包还原出来的,详见 `docs/PROTOCOL.md`。

## 使用

环境要求:Windows、Python 3.10+、示波器已绑定 WinUSB(默认即是)、USB 3 接口和线缆(USB 2 连接下采集会以 `UNSUPPORTED` 拒绝),以及 DSWave 1.2.1(只需用一次,用来提取 FPGA 比特流,本项目绝不再分发它)。使用驱动前请关闭 DSWave:示波器只接受一个 USB 客户端。

```
pip install openm454            # or, from a clone: pip install -e .
```

openm454 的数据文件夹就是克隆下来的仓库本身,对于 pip 安装的副本则是 `%LOCALAPPDATA%\openm454`(其他系统为 `~/.local/share/openm454`;可用 `OPENM454_HOME` 覆盖)。其中存放缓存的比特流(`firmware/`)、状态(`state/`)和采集文件(`captures/`)。

### FPGA 比特流从哪里来

需要用到一次 DSWave 1.2.1(`DSWave.exe`)。openm454 只读取它的文件,按以下顺序查找(每一项可以是存放 `DSWave.exe` 的文件夹,也可以是 exe 本身),除此之外不会搜索任何磁盘:

1. 环境变量 `OPENM454_DSWAVE`(也接受 `DSCOPE_DSWAVE`);
2. 数据文件夹中 `state/dswave.json` 保存的路径(由下面的 `openm454 setup --dswave` 写入);
3. 数据文件夹中的 `DSWave/`(DSWave 文件夹的副本);
4. 注册表中 DSWave 的 Windows 卸载项:正常安装的 DSWave 无需任何设置即可找到;
5. `%ProgramFiles%`、`%ProgramFiles(x86)%` 和 `%LOCALAPPDATA%\Programs`,分别查找其中的 `DSWave\` 或 `DreamSourceLab\DSWave\`。

首次需要配置示波器的 FPGA 时(示波器上电后的第一条命令)或执行 `setup --dswave` 时,会提取比特流,用 SHA-256 校验,并缓存到数据文件夹的 `firmware/` 中;此后 DSWave 可以关闭、移动或卸载。不带 `--dswave` 的 `setup` 只做报告(`cached`、`dswave`、`source`、`ready`)。如果什么都没找到,错误为 `FIRMWARE_SOURCE`(退出码 6),并说明搜索了哪些位置以及该怎么做。若要查看状态,或一次性告诉 openm454 DSWave 在哪里(无需示波器):

```
python -m openm454 setup                          # cached? where is DSWave found, and how
python -m openm454 setup --dswave <folder>        # check DSWave 1.2.1 there, cache the bitstream, save the path
```

使用同一数据文件夹的每个 openm454 进程都会读取保存的路径,无需重启(包括 MCP 服务器:如果它是用 `-e OPENM454_HOME=<folder>` 注册的,运行 `setup` 时要使用相同的 `OPENM454_HOME`)。

```
python -m openm454 info
python -m openm454 capture -c A1,D0 --vdiv 1V --offset 1.5V --timebase 200us --trig A1:rise:1.4V
python -m openm454 watch -c A1 --every 0.5s --count 10 --fields A1.freq_hz,A1.vpp_v
python -m openm454 awg set --wave sine --freq 10k --low 0V --high 2V
python -m openm454 analyze captures/<file>.npz --window=-1ms:1ms
python -m openm454 capture -c D0,D1 --rate 20M --duration 2ms --trig D0:pulse>:200us --decode "i2c:scl=D0,sda=D1"
python -m openm454 selftest
```

## 记录模式与实测速度

| 命令 | 记录方式 | 长度 | 采样率 |
|---|---|---|---|
| `capture` | 写入 512 MiB 采样内存,之后再读出(250-265 MB/s) | 单条记录:每通道 400M 个采样点 / 512 MiB | 最高 400 MSa/s(单通道) |
| `stream` | 每条记录在采集的同时就发送出去,任何数据率均可(`--record-first`:数据率高于 102.4 MB/s 时先采集完再读出,与 DSWave 相同);超过一条记录的长度或 `--duration forever`:一条接一条地记录,每条刷新上一条 | 不限长度;读出能跟上时记录之间约 0.2 s,超过读出上限时再加上读出该记录剩余部分的时间(250 MB/s 时共 0.7 s;使用 `--record-first` 时 2.4 s);保留最后一条记录(`--save-each`:全部保留) | 与 `capture` 相同:模拟和数字各用各的采样率 |
| `log` | 循环模式:一条无间隙的无限数据流,保存在内存中(默认;结束时一次性写出;更长的运行由内存环形缓冲保留最新部分),或使用 `--storage disk`,运行时分块转储为文件 | 直到 `openm454 stop` 或达到指定时长;受内存预算 / 磁盘额度限制 | 所有通道同一采样率,100 MHz / N,N 为任意整数(1 kSa/s..100 MSa/s,例如 33.333 MSa/s);交错 200 MSa/s(<= 2 个模拟、<= 4 个数字通道)和 400 MSa/s(<= 1 个模拟、<= 2 个数字通道) |

在 M454 上通过 USB 3 实测,所有输入均接 1 kHz 方波,逐个周期检查是否有间隙(2026-11-01/02):

- 数据率为 采样率 x (模拟通道数 + 数字通道数 / 8) 字节/秒。
- `stream`:在 DSWave 的 100 MB/s 上限以及 200 MB/s 下均为实时。在 400 MB/s 时读出速度为 270-290 MB/s,其余数据留在采样内存中等待;记录完整到达。12 个通道、50 MSa/s(250 MB/s,约 2 s 的记录):边采集边发送(默认)时两条记录之间为 0.74 / 0.66 s,使用 `--record-first` 时为 2.35 / 2.45 s;记录完整且连续。
- `log`:150 MB/s(2 个模拟 + 8 个数字,50 MSa/s)运行 3 min(27 GB),以及写盘 30 s,主机延迟恒定;200 MB/s(2 个模拟,100 MSa/s)运行 20 s 无积压;5 MB/s 运行 10 min。在 250 MB/s(4 个模拟 + 8 个数字,50 MSa/s)时读出约 200 MB/s,积压以约 55 MB/s 增长。持续 3 s 的 300 MB/s 突发(2 个模拟 + 8 个数字,100 MSa/s)以 266 MB/s 读出,数据保持完整。
- `log` 在 100 MSa/s 以上时对转换器做交错,方式与 `capture` 完全相同(DSWave 在循环模式下从不这样做)。已在示波器上验证:1 个模拟通道 200 和 400 MSa/s、2 个模拟 200 MSa/s、2 个数字 400 MSa/s、4 个数字 200 MSa/s 以及混合通道组合。2 个数字通道 400 MSa/s 或 4 个数字通道 200 MSa/s 为 100 MB/s,可无时间限制地运行。1 个模拟通道 200 MSa/s(200 MB/s)运行 10 s 无积压。
- 循环模式下采样内存是一个 512 MiB 的环形缓冲:主机最多可落后约 512 MiB 而不丢数据(已吸收 50 MB/s 下 10 s 的主机停顿)。超过之后,示波器会在没有任何标志的情况下覆盖未读数据并继续输出数据流:此后的数据都是垃圾(模拟通道出现一次相位跳变,数字通道错位),直到重新启动运行。用驱动的积压估计值测得,在 508-514 MiB 处出现断裂(三次运行),因此 `log` 在 480 MiB 处停止。
- `log --plan`(无需设备)会输出针对某个采样率的结论、无间隙上限和建议(见下一节)、运行最长能持续多久(`max_duration_s`)以及数据去向:默认保存在内存(放不进预算时拒绝,除非加 `--keep-last`),`--storage disk` 写成约 256 MB 的分块文件(会警告 SSD 磨损:200 MB/s 相当于每小时 720 GB),`--storage none` 只做统计。
- `bench` 测量这台电脑(空闲内存、解码速度、磁盘写入速度、USB 读出速度),并记录用户允许录制和数据文件夹使用多少内存和磁盘(`bench --ram 8GB --disk 50GB --dir <folder>`,保存在 `state/host.json`);`log` 的默认值取自这里。本机实测:解码 468 MB/s,USB 读出 206 MB/s(4 个模拟 + 8 个数字)/ 267 MB/s(2 个模拟 + 8 个数字)。

### 超过读出上限时:`log` 如何判断

D = 数据率;R = 这台电脑在示波器采集时读取它的速度。R 取决于通道组合(本机实测:4 个模拟 + 8 个数字 50 MSa/s 为 202 MB/s,同样通道在 100 MSa/s 以及 2 个模拟 + 8 个数字 100 MSa/s 为 264 MB/s,4 个模拟 100 MSa/s 为 275 MB/s)。

1. R 是测出来的,而不是假定的:某个通道组合第一次在 150 MB/s 以上运行 `log` 时,会先做最长 2 s 的探测(输出一行 `probe`),并把 R 记入 `state/host.json`:实测速率,或者当读取速度 >= D 的 99 % 时记为"kept up"(R >= D,跟得上)。之后在 150 MB/s 以上运行时,若结果明确(停止时仍以稳定速率落后,或在结束时已追上)就会刷新它,`bench` 和 `--remeasure` 也一样。在此之前,`--plan` 假定为 190 MB/s,并把方案标为 `provisional`。
2. D <= R:主机跟得上(`sustained` / `near_limit`);运行时长只受存储空间限制。
3. D > R:示波器每秒领先 D - R(`backlog_growth_mb_s`),而且停止后它不再发送任何数据,所以未读部分会丢失。无间隙的记录最长为 `gapfree_max_s` = 480 MiB x R / (D x (D - R)),即示波器领先 480 MiB 时主机已读到的信号长度。更短的记录正常运行。
4. 更长的记录(或 `forever`)会与 `stream` 在相同通道和设置下的表现做比较(`stream_alternative`,来自 stream 自己的规划):每条记录最长 400M 个采样点每通道 / 512 MiB,边采集边发送,间隙为 记录时长 x (D - R) / R + 0.18 s,采用 stream 的采样率(1-2-5 步进 x 交错;某个域可能更慢,例如 2 个模拟 + 8 个数字 100 MSa/s 时数字通道按 50 MSa/s 采样):
   - `STREAM_BETTER`(退出码 2):stream 对每个通道的采样率都不低于 log,且它的记录至少和 log 那一段无间隙数据一样长(或者一条记录就能容纳整个时长)。错误信息中带有 stream 命令,垂直设置相同。
   - `NEEDS_FORCE`(退出码 2):log 的这一段是可能的最长无间隙记录,但比要求的短。`--force` 会录下这一段然后停止(`stop_reason` 为 `ring_full`,ok,带一条警告);请求时间的其余部分不会被记录。两个错误都在 `error.plan` 中带有具体数字。
5. 运行时,每条统计行都有 `backlog_mb`;积压增长时还有 `backlog_growth_mb_s`、`ring_full_in_s` 和 `gapfree_projected_s`。结果中有 `unread_at_stop_mb`(已采集但从未读取的部分)。

本机示例:12 个通道 50 MSa/s(250 MB/s,R = 204):无间隙 8.88 s,对比 stream 每条 2.03 s、间隙 0.64 s 的记录,所以是 `NEEDS_FORCE`;强制运行后在 8.84 s 停止(早先一次运行 R = 202 时为 8.30 s),直到最后一个采样点每个周期都在。12 个通道 100 MSa/s(500 MB/s,R = 264):1.13 s 对比 1.02 s 的记录、1.09 s 的间隙,仍是 `NEEDS_FORCE`;读出速度低于约 250 MB/s 时 `stream` 会更好。200 MB/s 以 190 MB/s 读出:积压增长 10 MB/s,约 50 s 后触发保护,得到 47.8 s 的无间隙信号。

积压的估计方法是 D x (距起点的时间) - 已接收字节数,在每次 USB 传输时检查,并对照一条跟随示波器 / 电脑时钟差的基线:基线每秒最多上升 200 ppm x D,而且只在主机已追上时(估计值低于 16 MiB)才上升,因此估计值在数小时内保持准确,真实的积压绝不会被吸收。保护停止时若完全没有数据到达,则报告为示波器停滞(`TIMEOUT_DATA`);计划恰好在保护点结束的强制 log 报告 `ring_full`,其他任何保护停止报告 `HOST_TOO_SLOW`。

### 用 `log` 还是 `stream`?

两者都是无触发记录,但在示波器上不是同一种模式。`log` 是一次无限运行(循环模式);`stream` 是一系列记录,每条都以固定长度编程,示波器在该长度处停止。

| | `log` | `stream` |
|---|---|---|
| 间隙 | 一次运行内没有 | 每条记录一个(刷新) |
| 两次运行之间的间隙,12 个通道 20 MSa/s(100 MB/s) | 0.18 s | 0.19 s |
| 两次运行之间的间隙,12 个通道 50 MSa/s(250 MB/s,超过读出上限) | 1.5 s 的运行之后 0.51 s:0.33 s 用来读出本次运行的积压(示波器此间采集的数据被丢弃),再加 0.18 s 重启 | 约 2 s 的记录之后 0.66-0.74 s:读出该记录的剩余部分(边采集边发送),然后重启;使用 `--record-first`(整条记录采集完后再整体读出)时为 2.33-2.45 s |
| 采样率 | 所有通道同一采样率:100 MHz / N,N 为任意整数,1-2 个通道可交错 200 / 400 MSa/s | 模拟和数字各用各的采样率(例如 1 个模拟 400 MSa/s 配 8 个数字 100 MSa/s),取自 DSWave 的 1-2-5 x 交错表(例如 80 MSa/s) |
| 超过读出上限(约 200-275 MB/s) | 一段最长 480 MiB x R / (D x (D - R)) 的无间隙数据(停止后示波器不再发送数据;见上文) | 每条最长 512 MiB / D(每通道 400M 个采样点)的记录,边采集边发送,有间隙 |
| 低于约 51 kB/s(例如 1 个通道 1-20 kSa/s) | 支持 | 采样率会被提高 |
| 协议解码器 | 对保存的文件离线解码(`analyze --decode`) | 对保留的记录使用 `--decode` |
| 寄存器程序 | DSWave 的 Data Acquisition 模式(交错:本驱动的扩展,已验证) | DSWave 针对该窗口自己的程序(已做 golden 测试) |

重启(约 0.18 s)对两者相同;区别在于重启之前必须读出的内容。需要连续记录时用 `log`;需要模拟和数字使用不同采样率、需要对记录解码,或者在超过读出上限且 `log` 提示 `STREAM_BETTER`、或者总时间比一段长的无间隙数据更重要时,用 `stream`。

```
python -m openm454 bench --ram 8GB --disk 50GB --dir logs
python -m openm454 log --plan -c A0,D0-D7 --rate 1M --duration 10min
python -m openm454 log -c A0,D0-D7 --rate 1M --duration 10min                   # kept in RAM, one .npz at the end
python -m openm454 log -c A0,D0-D7 --rate 1M --duration forever                 # RAM ring; end it with: python -m openm454 stop
python -m openm454 stream -c D0-D7 --rate 100M --duration 60s --every 1s
```

无论是否给出 `--json`,每条命令都输出一个 JSON 文档(`watch`、`stream` 和 `log`:每行一个,最后是结果):包括测量值、实际应用的设置、文件路径。采样点从不打印(除非用 `--dump N` 要求输出抽取后的序列);采集结果保存为 `.npz`(原始码值 + 比例系数)和 `.png`。图中模拟通道的图例位于绘图区上方的一行(每个通道一项,例如 `A1  1V/div  Vpp 2.86V  f 1kHz`),标题在图例之上,数字通道的泳道标签位于右侧边距,波形上只绘制触发标记(t = 0 处的虚线、模拟触发电平的点线);解码出的帧不会画在图上,它们在 JSON 和 `<capture>_decode.json` 中。`python -m openm454 <command> --help` 列出所有选项。Python API:`openm454.scope.Scope`、`openm454.capture.load_capture`、`Capture.decode()`、`openm454.plot.render(cap, window=None, title=None, summary=None, channels=None)`(返回图形对象,由调用方关闭)、`openm454.firmware.status()` / `save_dswave()`。

协议解码器(`--decode SPEC`,可重复,用于 `capture`、`watch`、`stream` 和 `analyze`):`uart:rx=D4,baud=115200|auto,bits=8,parity=none,stop=1,invert=0`、`spi:clk=..,mosi=..,miso=..,cs=..,mode=0..3`、`i2c:scl=..,sda=..`、`can:rx=..,bitrate=500k|auto,invert=0`。它们会报告每一帧(时间相对于触发点)及其错误(校验/帧错误、不完整的字、NACK、CRC/填充/格式错误),并去除慢边沿上的比较器抖动。输出格式:`docs/schemas/decode.json`。解码需要每位或每个时钟周期约 10 个采样点(I2C:每个 SCL 周期 9 个,无错误;每周期 5-6 个仍可解码,错误远低于 1 %)。它们已用微控制器产生的真实总线做过验证(SPI 最高 18 MHz、RS232、CAN 最高 1 Mbit/s、I2C 最高 400 kHz),包括深存储采集和解码后的数据流。

## CLI 参考

`pip install openm454` 会安装 `openm454` 命令(`python -m openm454 ...` 等价)和 `openm454-mcp` 服务器。全局选项写在子命令之前:`--max-wall T`(看门狗:在 T 或根据命令计算出的预算之后停止并释放设备,取较长者;它绝不会缩短计算出的预算)、`--verbose`(在 stderr 输出 USB 通信日志,即寄存器读写)、`--json`(接受该选项,输出始终是 JSON)。
`python -m openm454 <command> --help` 列出所有选项。

| 命令 | 用途 | 主要选项 |
|---|---|---|
| `info` | 设备、固件、校准、限制 | |
| `capture` | 一次触发(或无触发)的记录:测量值、`.png`、`.npz` | 见下文 |
| `watch` | 重复采集,每次输出一行 JSON(监控用) | `capture` 的选项,`--every 0.5s`(从一次开始到下一次开始)、`--count N`(未给出 `--count` 和 `--for` 时默认 20)、`--for T`、`--fields A1.freq_hz,...`、`--stop-when EXPR`、`--save-last`(见 [watch](#watch)) |
| `stream` | 边采集边发送的无触发记录,实时统计 | `capture` 的选项,`--every 1s`、`--save-each`、`--record-first` |
| `log` | 无间隙的无限记录(循环模式),所有通道同一采样率 | `-c`、`--rate 1M`、`--duration 10s\|forever`、`--every 1s`、`--storage ram\|disk\|none`(`--no-save` = none)、`--keep-last`、`--chunk`、`--ram`、`--plan`、`--force`、`--remeasure`、`--out`、`--tag`、`--awg`、`--hook FILE.py`(实时处理,见[实时处理钩子](https://github.com/FrostFlops/openm454/blob/main/README.zh-CN.md#实时处理钩子log---hook))、垂直选项 |
| `bench` | 这台电脑的记录极限(空闲内存、解码和磁盘速度、USB 读出);保存内存 / 磁盘额度 | `--ram 8GB`、`--disk 50GB`、`--dir`、`--disk-test-mb`、`--no-device` |
| `awg set` | MCX 输出上的连续波形(0..3 V) | `--wave sine\|square\|triangle\|noise`、`--freq 1k`、`--low/--high` 或 `--vpp/--dc`、`--duty 50%`、`--phase` |
| `awg sweep` | 频率扫描,重复进行 | `--start`、`--end`(必需)、`--time 100ms`、`--log`、`--wave`、`--high`、`--low`、`--duty` |
| `awg burst` | 每个周期输出 N 个载波周期 | `--cycles`、`--period 10ms`、`--freq`、`--wave`、`--high`、`--low`、`--duty` |
| `awg mod` | 载波的 AM / FM / FSK 调制 | `--kind am\|fm\|fsk`、`--mod-wave`、`--mod-freq`、`--depth`、`--deviation`、`--hop`、`--freq`、`--wave`、`--high`、`--low`、`--duty` |
| `awg dc` | 稳定的直流电平(0..3 V,步进 6.4 mV) | `--level`(必需) |
| `awg off`、`awg status` | 把输出停放在 0 V / 显示上次的设置 | |
| `plot` | 把保存的 `.npz` 绘制成 PNG(无需设备) | `file`、`--window`、`--channels`、`--out` |
| `analyze` | 测量保存的 `.npz`(无需设备) | `file`、`--window`、`--channels`、`--edges N`、`--dump N`、`--measure`、`--decode SPEC` |
| `selftest` | 验证示波器(见[自检](#自检)) | `--quick`、`--no-awg`、`--fixture file\|standard` |
| `setup` | FPGA 比特流从哪里来;`--dswave <folder>` 一次性记录 DSWave 的文件夹(不用 USB) | `--dswave` |
| `release` | 确保设备空闲并已释放 | |
| `recover` | 复位并重新加载挂起示波器的 FPGA,相当于重新上电(对寄存器读取无响应的示波器仍需重新插拔) | |
| `stop` | 请求正在运行的 `watch` / `stream` / `log`(例如在后台)干净地停止 | |
| `debug` | 维护用:`regs-read`、`regs-write`(同样的安全策略)、`plan`(打印某组 capture 选项对应的寄存器程序,不接触设备) | |

`capture`、`watch` 和 `stream` 共用这些选项:

- 通道 / 垂直:`-c A0-A3,D0-D7`(也可用 `analog`、`digital`)、`--vdiv 1V`、`--offset 0V`、`--coupling dc|ac`、`--bw20 A1,A2`、`--invert A2`、`--probe 10x|A2=10x`、`--threshold 1.4V`。按通道设置的值写作 `A0=500mV,A2=2V`。
- 时基:`--timebase`(窗口 = 10 格)、`--duration`、`--samples`、`--rate`。
- 触发:`--trig SRC:EDGE[:LEVEL]`(例如 `A1:rise:1.4V`、`D0:fall`、`D0:pulse>:200us`)、`--trig-mode auto|normal|force`(默认 `auto`)、`--trig-pos 50%`、`--auto-after 0.3s`、`--timeout 5s`(normal 模式的等待时间)、`--holdoff`、`--sensitivity 0..15`(默认 5)、`--trig-retries N`(默认 4;数字边沿触发,见[数字边沿触发](#数字边沿触发))。`stream` 接受触发选项但会忽略它们:它始终以无触发方式记录。
- 输出:`--measure basic|full|none`(见[测量](#测量--measure))、`--edges N`、`--dump N`、`--window t0:t1`、`--no-plot`、`--no-save`、`--out`、`--tag`、`--awg 'sine,10k,0V,2V[,duty%]'`(先设置信号发生器)、`--decode SPEC`(可重复)、`--decode-limit`(默认 50 帧)。

`log` 也接受垂直选项;它的时基是 `--rate` 和 `--duration`(见上面的记录章节)。以 `-` 开头的值在 Python 3.10-3.12 上需要用 `=` 形式(`--window=-1ms:1ms`、`--offset=-1.5V`;按通道的 `A1=-1.5V` 保持原样即可);MCP 服务器会自行处理。在模拟通道上,不带电平的 `--trig` 会把该通道的 `--offset` 作为触发电平。`--rate` 会向下取到示波器支持的采样率(1 个模拟通道最高 400M,2 个为 200M,3-4 个为 100M;仅数字通道时:最多 2 个通道为 400M,最多 4 个为 200M,5-8 个为 100M)。`capture` 在其输出文件夹(默认 `captures/`,或 `--out`)中,对带有自己 `--tag`(默认 `cap`)的记录只保留最新的 200 条:按 openm454 命名方式命名、带有该标签的较旧文件(`<yyyymmdd-hhmmss-mmm>_<tag>.npz`、`.png`、`_decode.json`)会被删除。带有其他标签的记录(`log`、`stream`、`watch_last`)以及该文件夹中的任何其他文件绝不会被触碰。长时间的记录请放到别的文件夹(`log --out`、`bench --dir`)。

退出码:0 正常,1 其他(自检失败、内部错误、被中断),2 参数错误,3 未找到示波器,4 忙,5 未触发,6 设备 / 协议 / 看门狗故障,7 被安全规则拒绝(见错误一节)。

### watch

`watch` 重复执行 `capture`,每次采集输出一行 JSON。

- `--every T`(默认 0.5s)是从一次采集开始到下一次采集开始的时间。如果某次采集耗时更长,下一次会立即开始,不会补发连续的突发采集。
- 每行的 `t_s` 是该次采集开始的时间,单位为秒,自 watch 开始起算(`--every 0.5s` 依次为 0、0.5、1.0、...)。最后一行带有 `elapsed_s`。
- `--count N` 和 `--for T` 用于结束 watch;两者都给出时,以先到者为准。在 `--for` 时刻及之后不会再开始新的采集:`--every 0.5s --for 2s` 得到 4 次采集。两者都没有时为 20 次采集。`openm454 stop` 会在当前采集结束后结束 watch,如果正在两次采集之间等待,则在 0.25 s 内结束。
- `--fields CH.KEY,...`:`KEY` 可以是任意测量名称;只测量被点名的通道。默认字段是每个通道的 `vpp_v`、`vmean_v`、`freq_hz`、`duty_pct` 和 `state`。
- `--stop-when EXPR`:一个 Python 表达式,每个通道对应一个字典,例如 `"A1['vpp_v'] < 1"`;抛出异常的表达式视为假。`--fields` 或 `--stop-when` 与 `--measure none` 同时使用会被拒绝(退出码 2)。
- `--window` 和 `--edges` 有效;`--dump`、`--no-plot`、`--no-save` 和 `--tag` 无效。`--out` 只用于 `--save-last`,它会写入 `<out>/<date>-<time>_watch_last.npz`。
- 当 `trigger_check` 不是 `ok` / `unchecked` 时,行中会增加 `trigger_check`;当 `trigger_misfires` 大于 0 时,会增加 `trigger_misfires`。

### 测量(`--measure`)

`--measure basic|full|none` 适用于 `capture`、`watch`、`stream` 的最终结果以及 `analyze`。

- `basic`(默认)。模拟通道:`samples`、`vmin_v`、`vmax_v`、`vpp_v`、`vmean_v`、`vrms_v`、`vac_rms_v`、`vtop_v`、`vbase_v`、`vamp_v`、`freq_hz`、`period_s`、`duty_pct`、`edges_rise`、`edges_fall`、`periods_in_window`、`pwidth_s`、`nwidth_s`、`rise_s`、`fall_s`、`overshoot_pct`、`undershoot_pct`、`clip_lo`、`clip_hi`,以及出现时才有的 `freq_method`、`period_irregular`、`noise_like`。数字通道:`samples`、`state`、`high_frac`、`edges`、`freq_hz`、`period_s`、`duty_pct`,以及出现时才有的 `glitch_edges` 和 `runts`。在模拟通道上,当窗口内至少有两个下降沿但上升沿少于两个时,频率和占空比由下降沿得出(数字通道需要两个上升沿)。
- `full`:在 `basic` 的基础上,按通道增加窗口内每个完整周期(上升沿到上升沿)的统计:`cycles`、`period_min_s`、`period_max_s`、`period_std_s`、`freq_min_hz`、`freq_max_hz`、`pwidth_min_s`、`pwidth_max_s`、`nwidth_min_s`、`nwidth_max_s`、`duty_min_pct`、`duty_max_pct`(数字通道还有平均宽度 `pwidth_s`、`nwidth_s`),以及达到 10 万个边沿上限时的 `cycles_capped: true`(此时统计只覆盖前 10 万个边沿)。上升沿少于 2 个的通道或噪声通道不给出这些值。模拟通道还会得到 `range_lo_v` 和 `range_hi_v`,即在当前 V/div 和偏移下屏幕能显示的电压;位于两端的读数会被截幅。边沿列表(`edges_rise_s`、`edges_fall_s`)只在使用 `--edges N` 时出现。深存储记录需要再做一遍测量。
- `none`:没有 `analog` / `digital` 块(仍会给出提示)。MCP 的 `detail` 参数(`summary` | `full`)是另一回事:它选择紧凑摘要还是 CLI 的完整文档。

### 数字边沿触发

数字输入没有迟滞。缓慢的边沿(例如校准方波约 8 us 的上升沿)会在阈值处抖动几十 ns,所以下降沿触发可能在上升沿内部触发,反之亦然,而硬件仍然报告触发有效。因此,在每次以上升或下降边沿对数字通道触发的 `capture` 或 `watch`(normal 或 auto 模式)之后,openm454 会检查触发线在 t = 0 前后已稳定的电平(一个电平持续至少 200 ns 且至少 3 个采样点,取较长者,才算稳定;更短的是抖动)。只有当两个稳定电平之间的未稳定一段短到与抖动相当(至多 200 ns;实测抖动最长约 120 ns)时,才会判定为误触发;更长的一段是真实数据,结果为 `inconclusive`。因此,恰好位于触发点的快速数据(快于约 10 Mbit/s,例如夹在长低电平与长高电平之间的短 1-0 位对)可能被当作抖动而重新待命,而边沿慢到其抖动持续 200 ns 或更久时,会以 `ok` 通过。在相反边沿上触发的采集会被丢弃,示波器重新待命,最多重试 `--trig-retries N` 次(默认 4;0 = 只检查;MCP 为 `trig_retries`)。重新待命共用原来的触发等待时间(normal 模式下为 `--timeout`,auto 模式下为 `--auto-after`,再加 `--holdoff`);只有等待触发所花的时间才计入。模拟触发、`any` 和脉冲触发、`--trig-mode force` 以及 auto 模式下的无触发回退不做检查。

采集结果的 `acq` 块会报告检查结果:

- `trigger_check`:`ok`(第一次采集就显示了要求的边沿);`misfire_retried`(误触发的采集已被丢弃,返回的采集是正确的);`mismatch`(返回的采集本身就是误触发:t = 0 不是要求的边沿,带有警告和提示);`inconclusive`(无法确认边沿,例如在 `--trig-pos 0` 时没有预触发采样点,或存在毛刺或快于 200 ns 的时钟;不会重新待命);`unchecked`(不是数字上升/下降沿触发,或无触发)。MCP 摘要中省略 `unchecked`。
- `trigger_misfires`:被丢弃并重新待命的误触发采集次数(为 0 时 MCP 摘要中省略)。
- `attempts` 列出每次运行:`triggered`、`forced`(无触发)、`misfire`(在错误的边沿上触发)、`no-trigger`。`acq.triggered` 的含义不变:返回的那次采集的硬件触发标志。
- 警告和提示:`mismatch` 会给出警告和提示;`misfire_retried` 还会附加一条带有被丢弃误触发次数的提示(例如 "D5 falling-edge trigger: N misfire(s) inside a rising edge were discarded and re-armed")。

在 M454 上的实测(1 kHz 校准方波,normal 模式):检查之前,在 100 MSa/s 时,D5 下降沿触发有 13/40 次采集在上升沿内部触发,D0 下降沿为 5/40,D5 上升沿从未出现。检查之后,每种情形 60 次采集:在 50、80 和 100 MSa/s 时都没有返回错误的边沿(仅 D5、D5 加 A1、全部 8 个数字通道;每组重新待命了 5-25 次误触发)。仅 D5 一个通道在 400 MSa/s 时,约一半的尝试会误触发,60 次采集中有 2 次在全部 5 次尝试中都误触发:这两次都以 `mismatch` 加警告返回,而不是当作成功。要获得可靠的数字触发,请使用信号上快速的那个边沿(缓慢的上升沿会让下降沿触发误触发),或者改用模拟通道触发,因为它的触发有噪声裕量。

### 垂直范围限制

- `--offset` 是屏幕中心的电压。允许范围约为 -4.32 .. +4.36 格(-127 .. +128 个偏移码),其中一格是硬件的 V/div(把要求的值向上取整到 1-2-5 步进,探头输入端 1 mV .. 5 V)。1x 时:

  | V/div | 允许的偏移 | V/div | 允许的偏移 |
  |---|---|---|---|
  | 1 mV | -4.323 .. +4.357 mV | 100 mV | -0.4323 .. +0.4357 V |
  | 2 mV | -8.647 .. +8.715 mV | 200 mV | -0.8647 .. +0.8715 V |
  | 5 mV | -21.62 .. +21.79 mV | 500 mV | -2.162 .. +2.179 V |
  | 10 mV | -43.23 .. +43.57 mV | 1 V | -4.323 .. +4.357 V |
  | 20 mV | -86.47 .. +87.15 mV | 2 V | -8.647 .. +8.715 V |
  | 50 mV | -216.2 .. +217.9 mV | 5 V | -21.62 .. +21.79 V |

  所有数值都随探头倍率缩放(10x 时:探头尖端处 10 mV .. 50 V/div,限值 x10)。
- 超出范围的偏移会被限幅并给出警告,例如 `A1: offset 2.5 V out of range at 0.5 V/div (allowed -2.162 .. 2.179 V, about +-4.3 div); clamped to 2.179 V`;实际使用的值在 `applied.offset_v` 中。`watch` 的各行不显示警告。
- 触发电平必须在屏幕上(屏幕 ADC 码值 10..245 以内,屏幕以偏移为中心),否则返回 `TRIGGER_OUT_OF_RANGE`(normal 和 auto 模式;使用 `--trig-mode force` 时,屏幕外的电平会被限幅且不报错)。不带电平时,模拟触发使用 `--offset`。
- 截幅会给出提示(`A1 clips at the top: increase --vdiv or lower --offset`);过驱之后前端需要约 0.2-0.3 s 才能恢复。

## 自检

`python -m openm454 selftest` 检查示波器本身。M454 有一个 1 kHz 校准信号输出(约 3 V 方波,即探头补偿信号)和信号发生器输出,但没有内部回环通路:在示波器内部,输入端并没有连到这两者中的任何一个。因此标准夹具就是把示波器自己的这两个输出用线缆接到它的输入端,不需要其他仪器。检查分为两组。

无需接线的检查在任何接线下都能运行。它们只读取输入,绝不触碰信号发生器(约 4-6 s):

- V01 设备信息
- V12 对全部 12 个通道(A0-A3、D0-D7)以 100 MSa/s 做一次强制采集
- V13 最高采样率和折叠模式:1 个模拟通道 400 MSa/s、2 个模拟 200 MSa/s、2 个数字 400 MSa/s、4 个数字 200 MSa/s
- V11 采集延迟

信号检查(V00、V02-V10:校准方波、V/div、采样率一致性、数字通道和阈值、触发位置、数字触发、无触发超时、信号发生器回环)需要下面的标准夹具,当接线没有声明为该夹具时,会标记为跳过并给出原因。它们的容差例如:1 kHz +-2 %,顶部 2.8 V +-12 %,底部 0 V +-0.15 V,占空比 50 +-3 %。

| 输入 | 标准夹具 |
|---|---|
| A0 | 示波器自己的信号发生器输出(MCX,0-3 V) |
| A1(1x 探头)、A2(10x 探头)、A3(1x 探头) | 示波器自己的 1 kHz 校准信号输出(约 2.8-3 V 方波,上升沿为约 8 us 的慢速 RC 边沿,下降沿快,约 0.3 us) |
| D0、D2、D5、D7 | 同一个校准信号输出 |
| D1、D3、D4、D6 | 悬空 |

只需在数据文件夹的 `state/fixture.json` 中声明一次接线,或者用 `--fixture standard` 只对单次运行声明:

```
python -c "from openm454 import fixture; fixture.save('standard', 'standard')"
python -m openm454 selftest                      # uses the recorded wiring
python -m openm454 selftest --fixture standard   # the fixture is wired right now (this run only)
```

`fixture.save(analog='standard', digital='standard', note=None)` 写入 `{"analog": ..., "digital": ..., "since": ...}`;某一组引线的值为 `"standard"` 表示标准夹具(任何其他值表示接的是别的东西,这些引线上的信号检查会被跳过)。文件不存在表示接线未知,所以全新安装只运行无需接线的检查;文件无法读取时也是如此(结果中的 `note` 会说明原因;UTF-8 字节顺序标记没有问题)。接线变化时请更新该文件。

信号发生器只在模拟引线已声明为标准夹具且未给出 `--no-awg` 时使用:V09 把它停放在 0 V 以测试无触发超时,V10 向 A0 输出 2 Vpp 正弦波和 0-3 V 方波,然后关闭(其中某次采集失败时也会关闭)。`--no-awg`(以及 MCP 的默认设置)不触碰信号发生器;此时结果中不含 V10,而当最近一次 `awg` 设置让信号发生器仍在输出时,V09 会被跳过(先运行 `awg off` 即可纳入)。`--quick` 减少重复次数(时间约减半)。结果列出每项检查的 `measured` 和 `expected`;当有影响结论的检查失败时,退出码为 1。

## 该用哪个命令

| 命令 | 适用场景 |
|---|---|
| `capture` | 围绕某个事件或触发的一条记录,例如捕捉掉电:`--trig A1:fall:3.0V --trig-mode normal --timeout 1h` |
| `watch` | 每隔 N 秒重复一次带测量值(频率、Vpp 等)的触发快照 |
| `stream` | 带实时统计的无触发记录,模拟和数字各用各的采样率,对记录解码 |
| `log` | 持续数分钟到数小时的连续无间隙记录或监控:每个时间间隔的最小 / 最大 / 平均值,各行之间不漏掉任何数据;使用 `--hook` 时,你自己的 Python 代码会在每个采样点到达时就看到它,并可以触发事件或停止运行(见[实时处理钩子](https://github.com/FrostFlops/openm454/blob/main/README.zh-CN.md#实时处理钩子log---hook)) |

## 用 `log` 监控

`log` 每经过 `--every` 的信号时间(默认 1 s)输出一行统计,因此长时间运行就是一张按间隔列出 `vmin_v` / `vmax_v` / `vmean_v` 的表:即使平均值掩盖了跌落和尖峰,它们也会体现在 `vmin_v` / `vmax_v` 中。监控 8 小时的 3.3 V 电源轨,每约 10 s 一行,不保存任何数据(`--storage none`):

```
python -m openm454 log -c A1 --vdiv 0.5V --offset 2.1V --rate 10k --duration 8h --every 10s --storage none
```

窗口约为 0.09..4.09 V,一个 ADC 步进约 17 mV(V/div 的选取方法见[限制与安全](#限制与安全))。不加 `--storage none` 时,每个采样点都保存在内存中(默认)并在结束时写出:10 kSa/s 下 2 h 为 72 MB,8 h 为 288 MB。测量电源轨上的纹波:使用交流耦合和较小的 V/div,这会丢失直流电平:

```
python -m openm454 log -c A1 --coupling ac --vdiv 20mV --rate 1M --duration 1h --every 1s --storage none
```

先用 `log --plan` 检查方案(选项相同,无需设备)。通过 MCP:先用 `log_plan`,再用带 `mode: "log"` 的 `record_start`(它也接受 `vdiv`、`offset` 和 `coupling`),用 `record_status`(最近几行)和 `record_stop`。通过 MCP 启动的记录会在 MCP 客户端退出时被停止,所以无人值守的数小时 log 需要保持 Claude 会话开启,或者在后台运行 CLI(`openm454 log ...`,用 `openm454 stop` 结束)。

### `log` 的刷新能有多快?

`--every` 最小可以是一个 64 采样点的块,各行按精确的信号时间边界截断(`--every` 取整为完整的 64 采样点块,见 `plan.every_s`),而不是按 USB 传输截断。在 M454 上以 150 MB/s 实测(A0-A1 + D0-D7,50 MSa/s,`--storage none`,每项 10 s):

| `--every` | 实时行 | 读取端的到达间隔 | 示波器积压 |
|---|---|---|---|
| 1 s | 1/s | ~0.94 s | 1.4 MB,持平 |
| 100 ms | 10/s | ~103 ms | 1.4 MB,持平 |
| 10 ms | 100/s | ~13 ms | 1.3 MB,持平 |
| 1 ms | 1001/s | 成批到达,间隔最多 8.6 ms | 1.2 MB,持平 |

因此,在以 150 MB/s 记录的同时每秒输出 1000 行并不是极限(这些行承载的是统计值,而不是采样点)。数据以约 1 MB 的 USB 传输到达电脑(150 MB/s 时约 7 ms),所以各行是成批到达的,最早的反应在信号发生后约 10 ms。真正的限制在读取端:读取 JSON 行或轮询 MCP `record_status` 的智能体,最快也只能在数秒内作出反应。要在毫秒内作出反应,请用[钩子](https://github.com/FrostFlops/openm454/blob/main/README.zh-CN.md#实时处理钩子log---hook)在数据旁边运行你自己的代码。

### `log` 和 `stream` 的实时行

`log`:当该通道组合的读出速度需要先测量时(在 150 MB/s 以上且尚未知晓,或使用 `--remeasure`),先输出一行 `probe`;然后是一行带有 `"started": true`、`plan` 和 `out_stem` 的行;之后每个 `--every` 一行,最后是结果。使用 `--hook` 时,事件行(`{"event": ...}`,没有 `i`)会在发生时穿插其中,见[实时处理钩子](https://github.com/FrostFlops/openm454/blob/main/README.zh-CN.md#实时处理钩子log---hook)。各行在完整的 64 采样点块处截断,因此一行覆盖的是取整为 64 / 采样率 的整数倍的 `--every`(10 kSa/s 时 `--every 10s` 得到 9.9968 s);`plan.every_s` 显示实际使用的值。实时行包含:

| 键 | 含义 |
|---|---|
| `i` | 行号,从 0 开始 |
| `t_signal_s` | 自记录开始起的信号时间,到本行结束为止(采样点数 / 采样率) |
| `t_wall_s` | 自运行开始起的电脑时钟秒数,即这些数据到达的时刻(`t_wall_s - t_signal_s` 就是延迟) |
| `backlog_mb` | 示波器已采集但电脑尚未读取的 MB 数(估计值);运行在接近 `plan.backlog_stop_mb`(约 503 MB)时停止:`HOST_TOO_SLOW`,或者对计划恰好在此结束的 `--force` log 为 `ring_full` |
| `backlog_growth_mb_s`、`ring_full_in_s`、`gapfree_projected_s` | 仅在积压增长时出现:最近约 2 s 的 MB/s、保护停止运行前剩余的秒数、该次运行能保持的无间隙信号秒数 |
| `A0`..`A3` | `vmin_v`、`vmax_v`(完整的 ADC 码值,LSB = V/div / 29.375)和 `vmean_v`:仅针对本行时间间隔的探头尖端电压,单位为伏 |
| `D0`..`D7` | `high_frac`(0..1,高电平采样点所占比例)、`edges`(本间隔内的跳变次数) |
| `chunk_saved` | 磁盘存储:本行期间完成的 `chunk_NNNNN.npz` 的序号 N |
| `ram_kept_s` | 内存环形缓冲(`--keep-last`、`forever`):当前保存的信号秒数 |
| `final` | 最后一行(可能不完整)为 `true` |
| `hook_ms` | 使用 `--hook` 时:自上一行以来钩子所花的毫秒数 |

`stream`:各行包含 `i`、`t_signal_s`、`t_wall_s`(自本条记录开始起的秒数;在链式或 `forever` 的 stream 中,每条记录开始时都会重新计时,而 `t_signal_s` 贯穿整个流)、带有 `vmin_v`、`vmax_v`、`vmean_v` 和 `n`(本行的采样点数)的 `A0`..`A3`,以及带有 `high_frac`、`edges` 的 `D0`..`D7`。信号时间越过下一个 `--every` 边界时输出一行,因此各行在 USB 事件边界处截断。单条记录以 `final: true` 结束;链式 stream 会在每行加上 `record: k`,并在一条记录结束时加上 `record_end: true`。没有积压相关字段。最终结果包含完整的测量值(`--measure`、`--window`、`--edges`、`--dump`)、文件、提示和警告;链式 stream 还会增加 `records`、`record_s`、`signal_s`、`seams_s`、`seam_max_s`、`stopped_by`。

## 实时处理钩子(`log --hook`)

钩子是你自己的一个 Python 文件,它会在无间隙的 `log` 记录中看到每一个到达的采样点,并能立即作出反应:阈值报警、边沿计数、触发其他设备、提前停止。智能体想比读取输出行更快地对实时数据采取行动,就用这个办法(见[用 log 监控](https://github.com/FrostFlops/openm454/blob/main/README.zh-CN.md#用-log-监控))。

```
python -m openm454 log -c A1,D0 --rate 1M --duration forever --storage none --hook myhook.py
```

文件会在打开示波器之前加载。加载就是导入该文件,所以它的顶层代码会运行。文件缺失、语法或导入错误、`sys.exit()`、或没有 `process()`,都会给出 `INVALID_ARG`(退出码 2),并且不记录任何数据。加上 `--plan` 时,文件同样会被加载和检查;不会调用任何钩子函数,也不会触碰示波器。该文件作为已注册的模块运行(dataclass 之类可以正常使用),它所在的文件夹会被加到导入路径的末尾(旁边的辅助模块可以导入),其中的 `print()` 输出到 stderr,所以 stdout 仍然是每行一个 JSON 文档。

### 文件

```python
def setup(info):      # 可选,在第一个采样点之前调用一次
    ...

def process(block):   # 必需:对记录的每一段按顺序调用
    return None       # 或返回一个 dict:一个事件

def finish(summary):  # 可选,结束时调用一次;返回的 dict 会进入最终结果
    ...
```

`info` 包含 `rate_hz`、`analog`、`digital`、`scale`(每个模拟通道:`volts_per_code`、`zero_code`、`probe`、`vdiv_v`)、`every_s`、`duration_s`、`storage`、`block_samples`、`piece_s`(计划的每段时长,单位为秒)。`finish()` 的 `summary` 包含 `samples`、`duration_s`、`stop_reason`、`calls`、`events`。

每个采样点恰好传递一次,按顺序、没有间隙,以约 10 ms 信号为一段(`--every` 更短时段也更短,因为一段不会跨越多于一行;一次 USB 传输包含更多数据时则更长,例如低采样率下约 20 ms 的 roll 数据包;`info['piece_s']` 是计划的大小)。各行、统计和存储不受钩子影响。

### block

| 成员 | 含义 |
|---|---|
| `t0_s`、`t1_s` | 这一段开始和结束时,自记录开始起的信号秒数 |
| `rate_hz`、`n` | 采样率,以及这一段中每个通道的采样点数 |
| `analog`、`digital` | 通道列表 |
| `codes(ch)` | 模拟通道的原始 ADC 码值(`uint8`) |
| `volts(ch)` | 探头尖端的电压(`float32`) |
| `bits(ch)` | 数字通道每个采样点一个 `uint8` 的 0/1 |
| `packed(ch)` | 数字位,每字节 8 位,小端 |
| `times()` | 每个采样点的信号时间(`float64`) |

这些数组属于记录本身,是只读的:原地修改(`c -= 128`)会抛出异常并以 `HOOK_ERROR` 结束运行,已存储的数据不变。请在副本上计算(`c.astype(np.int16) - 128`)。各段长度不一(`n`):需要的内容(最后一个采样点、运行中的状态)要跨调用携带,否则落在段边界上的边沿或穿越会被漏掉。

### 事件与停止

`process()` 返回 `None` 或一个 dict。dict 就是一个事件,会立即以 `{"event": {...}, "k": n, "t_signal_s": ..., "lag_s": ..., "t_wall_s": ...}` 的形式输出:`t_signal_s` 是这一段的结束时刻,`lag_s` 是钩子落后于实时多少(示波器已采集的更新信号的秒数),`k` 是从 0 开始的事件序号,`t_wall_s` 是自运行开始起的电脑秒数。在 dict 中加入 `"stop": true` 可干净地结束记录(`acq.stop_reason` 为 `hook`,`ok` 为 true):记录随这一段结束,其后的数据既不保留也不计数(在 RAM 环形缓冲中,事件之前的历史会保留)。返回任何其他类型都是 `HOOK_ERROR`。

dict 会被输出为 JSON:其中的 numpy 标量和数组会变成普通数字和列表,NaN 和无穷大变成 `null`;JSON 无法表示的其他内容(集合、对象)是 `HOOK_ERROR`。`finish()` 返回的 dict 也是如此。

实时行会带上 `hook_ms`(自上一行以来钩子所花的时间)。最终结果会带上 `hook` 块:`file`、`calls`、`events`、`seconds`、`load`(每信号秒钩子所用的秒数)、`lag_max_s`、`stopped_run`、`last_event` 和 `result`(`finish()` 返回的 dict,如果有)。`load` 高于 1 或 `lag_max_s` 高于 1 s 时会增加一条钩子已落后的警告。

### 钩子错误

钩子代码抛出的任何异常,包括 `sys.exit()` 和 `KeyboardInterrupt`,都是 `HOOK_ERROR`。`process()` 中的异常会以 `HOOK_ERROR` 结束运行(退出码 2;`error.trace` 给出回溯的末尾部分;`acq.stop_reason` 为 `hook_error`)。直到并包括该段为止已记录的数据会保留并保存,`finish()` 不会被调用。`finish()` 中的异常同样给出带 `error.trace` 的 `HOOK_ERROR`,但此时记录是完整的,`acq.stop_reason` 仍是结束它的原因。`setup()` 中的异常会在记录任何数据之前给出 `HOOK_ERROR`(信息中会写出异常名称,没有 `error.trace`)。当运行因设备错误或 Ctrl+C 结束时,`finish()` 仍会被调用,错误结果也带有 `hook` 块。

### 钩子的性能

钩子在解码数据的线程中运行,所以它必须跟得上。一旦落后,队列就会增长,主机速度保护会在排队达到 768 MiB(约 805 MB)时以 `HOST_TOO_SLOW` 停止运行(数据保留)。在 150 MB/s 下的实测:一个轻量钩子(用 `bits()` 统计 D0 边沿数、A1 码值的最小/最大值、每 100 ms 一个事件、2 s 时停止)在 2 s 内调用了 144 次,`load` 为 0.022,`lag_s` 中位数 0.2 ms、最大 7 ms,并在 2.0 s 自行停止了运行。一个重量钩子(对两个模拟通道调用 `volts()`,再对每段做一次 FFT)的 `load` 为 3.16,延迟最高 2.2 s,并带有警告。因此:对 `codes()` / `packed()` 使用 numpy,而不是对每个采样点使用 `volts()` / `bits()`;每段的工作量要小;用模块级状态跨段累积;必要时降低 `--rate` 或减少通道。

### 钩子的安全

钩子不得与示波器通信:示波器正忙于记录,所以不要在钩子中运行 `openm454` 命令。文件、套接字、串口和子进程都可以使用。钩子是以你的权限运行的你自己的代码:只运行你自己编写或信任的钩子文件。

### 通过 MCP 和 Python 使用钩子

`record_start` 在 `mode: "log"` 时接受 `hook`(文件的绝对路径;相对路径会被拒绝,因为记录在 MCP 服务器所在的文件夹中运行);`mode: "stream"` 时会得到 `INVALID_ARG`。钩子所导入的包必须安装在运行 MCP 服务器的那个 Python 中。此时 `record_status` 会增加 `events`(最后 `lines` 条事件行,默认 3,最多 20)和 `events_seen`(输出最后 256 KiB 中的事件行数;总数在 `record_stop` 结果的 `hook.events` 中)。从 Python 使用:`logger.run(..., emit=print_or_collect, hook=openm454.hook.Hook(path))`;事件行会交给 `emit`。

### 示例:电源轨跌落报警

A1 接在 3.3 V 电源轨上;钩子报告每一段中第一个低于 3.0 V 的采样点,它把原始码值与预先算好的整数限值比较,所以每段不必构建电压数组:

```python
import numpy as np
LIMIT_V = 3.0

def setup(info):
    s = info['scale']['A1']
    global LIMIT_CODE
    LIMIT_CODE = int(np.ceil(s['zero_code'] + LIMIT_V / (s['volts_per_code'] * s['probe'])))

def process(block):
    c = block.codes('A1')
    i = np.flatnonzero(c < LIMIT_CODE)
    if i.size:
        return {'dip_at_s': round(block.t0_s + i[0] / block.rate_hz, 6), 'min_v': round(float(block.volts('A1').min()), 4)}
```

```
python -m openm454 log -c A1 --vdiv 0.5V --offset 2.1V --rate 1M --duration forever --storage none --hook dip.py
```

在 M454 上运行,A1 接校准方波(它大部分时间低于 3.0 V):1 MSa/s 下每个约 20 ms 的段产生一个事件,`load` 为 0.005。[钩子的性能](https://github.com/FrostFlops/openm454/blob/main/README.zh-CN.md#钩子的性能)中的轻量和重量钩子也是在 M454 上实测的。

## 限制与安全

来自厂商数据手册:

| 输入 | 限制 |
|---|---|
| 模拟 BNC | -200 .. +200 V 峰值(DC + AC);1 MOhm // ~16 pF;8 位,1 mV/div .. 5 V/div |
| 数字引线 | 引线输入端 -30 .. +30 V;1 MOhm // ~16 pF;阈值 0..5 V(`--threshold`,默认 1.4 V) |
| 信号发生器(MCX) | 0 .. 3 V |
| 校准信号输出 | 约 3 V、约 1 kHz 方波 |

- 探头地线就是电脑的地(通过 USB,如果电脑有市电保护接地,也通过它)。只能把它接到电位相同的点,绝不能接到"带电"的地或任何以市电为参考的点。
- 屏幕显示范围不等于安全范围。1x 时屏幕可达约 +-41.7 V(5 V/div,偏移处于极限),10x 时约 +-417 V,但 BNC 的限制是 +-200 V 峰值,还要受探头自身额定值的约束。
- 超出屏幕范围会使 ADC 饱和;该部分的数据被截幅。

为直流耦合下的直流电源轨选择 V/div 和偏移:屏幕显示 8 格,最远可达的边缘是 +8.34 格,所以应使 V/div >= V_rail / 8.34,并设置偏移让电源轨落在屏幕上:在默认偏移 0 V 时,高于约 4 x V/div 的正电源轨会落到屏幕外(1 V/div 时窗口为 -4.02..+3.98 V)。

| 电源轨 | V/div(1x) | 说明 |
|---|---|---|
| 3.3 V | 0.5 V | 偏移 2.0 V:窗口 0..4.0 V(2.1 V:0.09..4.09 V;偏移 2.0 V 时,塌陷到 0 V 会读成 0 V);0.2 V/div 无法达到 3.3 V |
| 5 V | 1 V | 偏移 2.5 V:约 -1.5..6.5 V |
| 12 V | 2 V | 偏移 6 V:约 -2..14 V |
| 24 V | 5 V | 偏移 12 V:约 -8..32 V |
| 高于约 41 V | 使用 10x 探头 | `--probe 10x`,限值 x10;不要超过探头的额定值 |

## MCP 服务器

`openm454-mcp`(或 `python -m openm454.mcp_server`)通过 Model Context Protocol(stdio,无额外依赖;随 `pip install openm454` 一起安装)提供示波器服务:

```
claude mcp add -s user openm454 openm454-mcp
```

这种写法在所有 shell(PowerShell、cmd、bash)中都适用。`-s user` 让服务器在每个文件夹中都可用(不加则只在运行该命令的文件夹中可用)。如果 pip 的 `Scripts` 文件夹不在 `PATH` 中,请给出 `openm454-mcp` 的完整路径。DSWave 会被自动找到(搜索顺序见上文),也可以用 `python -m openm454 setup --dswave <folder>` 一次性记录。用 `claude mcp list` 检查。其他客户端:`{"mcpServers": {"openm454": {"command": "openm454-mcp"}}}`。

环境变量(`OPENM454_DSWAVE`、`OPENM454_HOME`)只能通过 Claude 启动服务器时所用的环境传给它:请在注册时给出(`-e`)。Claude 在后台启动服务器,所以之后在 shell 中设置的变量永远到不了那里。要把服务器名称放在 `-e` 之前(`-e` 接受多个值,会把它后面的名称吞掉),然后是 `--`,再是命令:

```
claude mcp add -s user openm454 -e "OPENM454_DSWAVE=<folder>" -- openm454-mcp
```

在 Windows PowerShell 中,通过 npm 安装的 `claude` 经由一个 `.ps1` 包装脚本运行,它会丢掉单独的 `--`,导致 "missing required argument"。请把分隔符写在引号里(`'--'`),调用 `claude.cmd`,使用不带分隔符的 `"--env=OPENM454_DSWAVE=<folder>"`,或者在 cmd 中运行该命令。之后请重启 Claude。每次工具调用在子进程中运行一条 CLI 命令,因此每次调用都会打开并释放示波器,挂起不会阻塞服务器,CLI 的所有规则都适用。

11 个工具:

| 工具 | 运行 | 主要参数(默认值) |
|---|---|---|
| `status` | `info` | 无。是否已连接?型号、USB 速度、固件 / FPGA 状态、上次的信号发生器设置 |
| `capture` | `capture` | `channels`(`A0-A3,D0-D7`)、`timebase` / `duration` / `rate` / `samples`、`trigger`(`SRC:EDGE[:LEVEL]`)、`trig_mode`(有触发时为 `normal`,无触发时为 `force`)、`timeout_s`(5)、`trig_pos`、`holdoff`、`trig_retries`(4)、`decode`、`edges`、`window`、`save`(true)、`plot`(true)、垂直选项(`vdiv`、`offset`、`probe`、`coupling`、`bw20`、`threshold`)。每通道的频率、Vpp、最小/最大/平均值、占空比、上升/下降时间(数字通道:状态、频率、占空比、边沿数)、触发结果、解码帧、文件路径。未触发时返回 `TIMEOUT_NO_TRIGGER` |
| `analyze` | `analyze` | `file`(必需)、`channels`、`window`、`edges`、`decode`;对保存的 `.npz` 做测量 |
| `plot` | `plot` | `file`(必需)、`channels`、`window`、`out`;返回 `.png` 路径 |
| `awg` | `awg set` / `dc` / `off` / `status` | `action`(必需:`set`、`dc`、`off`、`status`);`wave`、`freq`、`high`、`low`(或 `vpp`、`dc`)、`duty`、`phase`;`dc` 用 `level`。调用结束后输出仍保持运行 |
| `log_plan` | `log --plan` | `rate`(必需)、`channels`、`duration`、`every`、`storage`、`keep_last`、`force`、垂直选项;无需设备 |
| `record_start` | 在后台运行 `log` 或 `stream` | `mode`(必需:`log` 或 `stream`)、`channels`、`rate`、`duration`、`every`、`storage`(log:`ram` / `disk` / `none`;stream:仅 `none`,不保存)、`keep_last`、`force` 和 `hook`(仅 log;一个钩子文件,此时 `record_status` 会返回它的 `events`)、`decode`(仅 stream)、垂直选项(`vdiv`、`offset`、`probe`、`coupling`、`bw20`、`threshold`);返回任务 id。同一时间只能有一个记录,期间示波器处于忙状态 |
| `record_status` | 无(读取任务的输出) | `job`(最近一个)、`lines`(3);最近的状态行,完成后给出结果;带 `hook` 时还有 `events` 和 `events_seen` |
| `record_stop` | `stop` | `job`、`wait_s`(120);干净地停止并返回结果 |
| `release` | `release` | 无;释放被崩溃的程序遗留在运行状态的示波器 |
| `selftest` | `selftest` | `quick`(true)、`fixture`(`file`;或 `standard`)、`awg`(false)、`detail`、`max_wall_s`(1260) |

结果是摘要;对于 `capture`、`analyze` 和 `selftest`,`detail: "full"` 会返回 CLI 的完整文档。采样点保存在 `.npz` 中。每次设备调用都有硬性时限,超时后返回错误(`TIMEOUT_WATCHDOG`),并由 CLI 自己的看门狗停止示波器:`capture` 和 `selftest` 接受 `max_wall_s`(`capture` 的默认值:180 s 加触发超时和记录长度;`selftest`:1260 s,高于 CLI 自己的 1200 s 看门狗);`status`、`awg` 和 `release` 限制为 60 s。

`selftest` 运行 `selftest --quick`(当 `quick` 为 false 时去掉 `--quick`),除非 `awg` 为 true 否则带 `--no-awg`,当 `fixture` 为 `standard` 时带 `--fixture standard`,所以默认情况下它绝不触碰信号发生器,而且只有在接线已声明时才运行信号检查。只有在用户已接好并确认标准夹具之后才传 `fixture: "standard"`,只有在信号发生器输出接到 A0 且没有接其他任何东西时才传 `awg: true`。摘要包含 `passed_checks`、`failed_checks`(measured 对比 expected)、`skipped_checks`(按原因分组)以及与这些列表一致的计数(设备故障后未运行的检查计为跳过),另有 `fixture`、`note` 和 `seconds`。运行失败时返回错误 `SELFTEST_FAILED`,其提示会区分信号检查失败(通常是接线问题)和无需接线的检查失败(示波器或其 USB 连接有问题);设备故障时返回该设备错误(带 `stopped_at`)。

没有对应 MCP 工具的 CLI 命令:

| 命令 | 原因 |
|---|---|
| `watch` | 在多次采集中每次输出一行 JSON(默认 20 次,或直到 `--count` / `--for` / `--stop-when` / `openm454 stop`),不适合"一次请求一次响应"的模式;请使用 `mode: "stream"` 的 `record_start`,或重复调用 `capture` |
| `setup` | 一次性配置 DSWave 的位置;在终端中运行一次即可:之后每次工具调用都会读取保存的路径(`state/dswave.json`) |
| `bench` | 维护:测量这台电脑的 USB 读出速度,`log` 会用到它 |
| `recover` | 复位并重新加载挂起示波器的 FPGA;有意保留为 CLI 手动步骤 |
| `debug` | 原始寄存器访问,有意不向智能体开放 |
| `stop` | `record_stop` 内部使用它 |
| `awg sweep`、`burst`、`mod` | `awg` 工具只覆盖 `set`、`dc`、`off`、`status` |

有 shell 的智能体可以用 CLI 完成以上所有操作。

## 错误

每个错误都有代码、信息和提示,因此下一步很明确:

| 代码 | 退出码 | 含义 |
|---|---|---|
| `NOT_FOUND` | 3 | 示波器未连接(没有看到 USB 2A0E:0080) |
| `BUSY` | 4 | DSWave 或另一个 openm454 进程占用着示波器 |
| `TIMEOUT_NO_TRIGGER` | 5 | 在 `--timeout` 内没有触发(`--trig-mode normal`) |
| `TRIGGER_OUT_OF_RANGE` | 2 | 触发电平超出该通道的可见范围(信息中会给出范围);仅限 normal 和 auto 模式(使用 `--trig-mode force` 时不使用触发,电平会被限幅) |
| `FIRMWARE_SOURCE` | 6 | 未找到 DSWave 1.2.1(或文件不是 1.2.1),因此无法提取 FPGA 比特流;信息列出了搜索过的位置,提示中给出 `openm454 setup --dswave <folder>` |
| `UNSUPPORTED` | 6 | 示波器以 USB 2 速度连接;capture、watch、stream 和 log 需要 USB 3(SuperSpeed)连接 |
| `USB_NO_MEM` | 6 | 仅 Linux:一次 capture 排队的读取放不进内核 usbfs 缓冲(`usbfs_memory_mb`,默认 16 MiB,所有 USB 设备共用)。本次 capture 自己就超限时在 setup 程序之前抛出,被其他进程占去一部分时在 submit 时抛出;信息里是排队的字节数和当前上限,提示里给出改法。预算装不下几何给的队列时,按能装下的数量排队,前提是队列至少保留 4 个读取,或至少 2 个合计 4 MiB 的读取(capture 约 1 MiB 的读取 4 个,约 2 MB 的 3 个,实时流 4-10 MB 的 2-3 个);capture、stream、log 都用 `acq.transfers` 报告实际深度,深度被降低时 `acq.transfers_wanted` 给出期望深度(MCP 摘要只在此时显示这两项)。默认 16 MiB 时:快速 capture 排 15 x 1 MiB,约 2 MB 的读取 8 个,`stream -c A0 --rate 400M` 2 个,`-c A0,D0 --rate 200M` 3 个;400M 的 `-c A0,D0` 和 100M 的 12 通道会被拒绝(分别需要 `usbfs_memory_mb` >= 18 和 >= 20;stream 可用 `--record-first`,所需小得多)。只有连下限都装不下才报错(此时未向示波器写入任何内容)。运行中途的 NO_MEM 不报 `USB_NO_MEM`,而是 `DEVICE_ERROR`(退出码 6,示波器仍在运行,下次打开时会先停止它)。`log --plan` 里它请求的深度叫 `transfers_planned` |
| `INVALID_ARG` | 2 | 某个参数有误,或对该通道组合不可行,或 `log --hook` 的文件无法使用(缺失、语法或导入错误、没有 `process()`) |
| `STREAM_BETTER`、`NEEDS_FORCE` | 2 | `log` 超过了它的无间隙上限(见上文) |
| `HOOK_ERROR` | 2 | `log --hook`:钩子文件在 `setup()`、`process()` 或 `finish()` 中抛出异常,或 `process()` 返回了 `None` 和 dict 之外的东西;对 `process()` / `finish()`,`error.trace` 给出回溯的末尾部分,此前已记录的数据会保留;`setup()` 失败时不会记录任何数据 |
| `SELFTEST_FAILED` | 1 | 仅 MCP `selftest`(CLI 以 1 退出并列出各项检查):某项检查失败;`failed_checks` 给出 measured 对比 expected(信号检查失败通常意味着接线与标准夹具不同,无需接线的检查失败则指向示波器或其 USB 连接) |
| `UNSAFE_OPERATION` | 7 | 被安全规则拒绝 |
| `TIMEOUT_WATCHDOG`、`TIMEOUT_DATA`、`USB_ERROR`、`PROTOCOL_ERROR`、`HOST_TOO_SLOW` | 6 | 运行期间设备或主机出错;此前的数据会保留 |

## 目录结构

- `openm454/` 驱动:带安全白名单的 USB 传输层、设备启动(FPGA、安全握手、校准)、寄存器编译器(`mso.py`,精确复现 DSWave 的寄存器程序)、数据流解码器(`packet.py`)、采集引擎、循环模式记录器(`logger.py`)、`log --hook` 的实时处理钩子(`hook.py`)、测量、绘图、AWG、CLI、自检、协议解码器(`decode.py`)、测试用的合成总线流量(`protosim.py`)以及 MCP 服务器(`mcp_server.py`)。
- `tests/` 离线测试:golden 寄存器和 AWG 程序、数据流解码器、针对模拟设备的采集引擎、测量、基于合成流量的协议解码器、MCP 服务器、安全规则(`python -m pytest tests`)。
- `docs/PROTOCOL.md` 还原出的 USB 协议。
- `.github/workflows/`:CI(在 Windows 和 Linux、Python 3.10-3.14 上运行离线测试,使用 `OPENM454_NO_USB=1`,让驱动拒绝打开 USB;检查受版本控制的文件不含本地路径;构建包),在推送到 main 和提交 pull request 时触发;与 `openm454.__version__` 相同的标签 `vX.Y.Z` 会运行测试、构建并发布到 PyPI(trusted publishing),同时创建 GitHub release。

## 此处不包含的内容

厂商应用程序、其内嵌的 FPGA 比特流和固件镜像、反编译的厂商代码、原始 USB 抓包(其中含有设备的安全密钥)、逆向工程工作区以及厂商数据手册,均不放在本仓库中。

## 安全

驱动绝不发送固件升级请求(bRequest 0xB3),绝不写入设备 EEPROM,只有在设备报告 FPGA 未配置时,或者当 `openm454 recover` 复位挂起的示波器时,才会配置 FPGA(始终使用经过验证的比特流,并且是会话中的第一次写入)。

## 许可证

GPL-3.0(见 `LICENSE`)。DreamSourceLab 的 DSView(GPLv3)被用作协议参考。DSWave 和 DSCope 是 DreamSourceLab 的商标;本项目与 DreamSourceLab 无关联。
