# 多产品云沙箱启动并发速度测试

本项目是在 Windows 本地运行的压测客户端：阿里云使用 E2B Python SDK，字节/
火山引擎使用 veFaaS 原生 Python SDK。它不会在本机部署云沙箱服务端。

本文说明三类启动并发速度测试：

1. 无挂载；
2. 对象存储 OSS；
3. 文件存储 NAS。

测试档位为：

```text
1 次/秒、10 次/秒、50 次/秒、100 次/秒、200 次/秒
```

每个档位按配置的沙箱数量执行；调度频率由 TPS 档位决定。
每次运行都会生成独立的 CSV、JSON 和失败日志。

程序执行过程如下：

```text
读取 benchmark.ini
→ 根据 [run].product 选择阿里云或字节/火山
→ 按 rates 和 sandbox_counts 生成定量定时启动任务
→ 并发创建 Sandbox 并等待功能就绪
→ 清理本轮创建的 Sandbox
→ 写入本次原始明细、汇总、失败日志
→ 追加全局历史 CSV，并更新跨厂商结果矩阵
```

阿里云驱动以创建返回、第一条命令完成、第二条命令完成作为三个累计里程碑；
火山原生驱动以创建返回、首次 Ready、二次 Ready 确认作为三个累计里程碑。
三项延迟都从本次创建开始计时。默认情况下，每个TPS档位的第一个沙箱不参与
`mean`，但仍参与 `min`、`max`、P50、P90、P95、P99和成功率统计。

---

## 下载后快速运行

以下命令适合第一次从 GitHub 下载本项目的用户。所有命令都在 PowerShell 中
执行，不需要把项目放到固定盘符或固定目录。

### 1. 下载并进入项目

```powershell
git clone https://github.com/Lejiawe/test.git
Set-Location test
```

如果已经通过浏览器下载并解压 ZIP，则在解压后的项目根目录打开 PowerShell，
不需要再次执行 `git clone`。

### 2. 安装依赖

可以直接安装到当前 Python 环境：

```powershell
python -m pip install -r requirements.txt
python -m pip check
```

也可以按第3节说明选择 Conda 或 venv 隔离环境。

### 3. 创建本地配置文件

```powershell
Copy-Item benchmark.template.ini benchmark.ini
notepad benchmark.ini
```

在 `benchmark.ini` 中完成以下配置：

1. 在 `[run]` 的 `product` 中选择 `aliyun` 或 `vol`；
2. 在 `rates` 中填写需要测试的档位，例如 `1,10,50`；
3. 阿里云在 `[product.aliyun]` 填写 `api_key`，并检查 API URL、Domain 和模板；
4. 字节在 `[product.vol]` 填写火山 AK、SK、Region 和 veFaaS Function ID。

真实密钥只填写在本地 `benchmark.ini` 中。该文件已被 `.gitignore` 忽略，
不要强制提交到 GitHub。

### 4. 预览测试计划

```powershell
python benchmark.py run-config --config benchmark.ini
```

这条命令只显示产品、档位和各档位预计创建数量，不会创建沙箱，也不会
产生云资源费用。

### 5. 正式运行

确认计划和费用风险后执行：

```powershell
python benchmark.py run-config --config benchmark.ini --confirm
```

测试结束后，结果位于相对路径 `results`。每次正式运行都会创建一个包含时间和
产品名的新目录，例如：

```text
results/20260804_120000_123_阿里云_启动并发速度_无挂载_1-10-50tps_各10-100-500个/
```

重复执行正式运行命令不会覆盖之前的测试目录：

- `results/全局测试历史.csv` 追加本次结果，保留全部历史；
- `results/全局测试结果.csv` 保留各厂商、档位的最新结果矩阵，同一测试项以
  最新一次结果更新；
- 每次运行的原始明细、汇总和失败日志仍保存在各自的时间戳目录中。

---

## 0. 推荐方式：通过一个配置文件选择产品和规模

首次使用时复制模板：

```powershell
Copy-Item benchmark.template.ini benchmark.ini
```

`benchmark.ini` 是本地运行配置，已经被 `.gitignore` 忽略，可以在里面切换产品、
选择TPS档位并填写本机密钥。主要配置如下：

```ini
[run]
product = aliyun
rates = 1,10,50
sandbox_counts = 10,100,500
storages = none
max_workers = 1000
output = results
exclude_first_from_mean = true
```

`sandbox_counts` 可选；填写时必须与 `rates` 的数量完全一致。若未填写，则每个
档位均使用 `default_sandbox_count`，例如：

```ini
default_sandbox_count = 100
# sandbox_counts =
```

产品选择：

```ini
product = aliyun
```

或：

```ini
product = vol
```

只测一个档位可以填写：

```ini
rates = 50
```

测试多个档位可以填写：

```ini
rates = 1,10,50,100,200
```

配置完成后，先检查计划而不创建沙箱：

```powershell
python benchmark.py run-config --config benchmark.ini
```

确认数量和产品无误后正式运行：

```powershell
python benchmark.py run-config --config benchmark.ini --confirm
```

`benchmark.template.ini` 已包含两种驱动的配置模板。阿里云使用 E2B API Key；
字节使用火山原生 SDK 的 AK、SK、Region 和 veFaaS Function ID。真实凭证只能
填写在本地 `benchmark.ini` 或对应环境变量中，不能提交到 GitHub。

### 0.1 公平对比阿里云和字节

`[run]` 是两家产品共用的测试条件。切换厂商时只修改 `product`，下面这些字段
必须保持完全相同：

```ini
[run]
rates = 1,10,50,100,200
sandbox_counts = 10,100,500,1000,2000
storages = none
max_workers = 1000
exclude_first_from_mean = true
```

先测试阿里云：

```ini
product = aliyun
```

```powershell
python benchmark.py run-config --config benchmark.ini
python benchmark.py run-config --config benchmark.ini --confirm
```

阿里云测试完成并确认资源已经释放后，只把产品改为字节：

```ini
product = vol
```

再次运行相同命令：

```powershell
python benchmark.py run-config --config benchmark.ini
python benchmark.py run-config --config benchmark.ini --confirm
```

两次运行使用完全相同的 TPS、沙箱数量、线程上限和均值口径。阿里云通过 E2B
创建并执行两条命令，字节通过原生 `CreateSandbox` 创建并轮询
`DescribeSandbox` 到 Ready。两者的发压方式和统计方法相同，但“功能就绪”的
判定接口不同，报告中应同时保留 `driver` 字段和第11节的里程碑定义。

`max_workers` 不是某个厂商独有的配置。脚本对阿里云和字节使用同一个值，并按
当前档位计算：

```text
effective_max_workers = min(max_workers, sandbox_count)
```

例如 `max_workers = 1000`、持续60秒时，1/10/50/100/200 TPS 的实际线程上限
分别是60、600、1000、1000、1000。判断线程是否足够，应查看结果中的
`schedule_delay_p95_ms`；如果它只有几毫秒，说明本地基本按计划发出了请求。
此时云端创建慢、HTTP 429、实例/副本配额不足或服务端错误不能通过增加本地
线程解决，应向对应厂商申请更高配额或排查服务端错误。

---

## 1. 测试前需要准备什么

### 1.1 本机软件

需要安装：

- Windows PowerShell；
- Python 3.10 或更高版本；
- 能访问所测厂商云沙箱 Endpoint 的网络。

Conda 不是必需的。可以直接使用已有 Python 环境，也可以选择 Conda 或
Python 自带的 venv 隔离依赖。

检查 Python：

```powershell
python --version
```

### 1.2 阿里云配置

需要准备：

1. 已开通函数计算和云沙箱功能；
2. 云沙箱 API Key；
3. 云沙箱所在地域；
4. 可用的 `code-interpreter-v1` 模板；
5. 足够的 Sandbox 实例配额、创建限流额度和测试费用预算。

第一部分“无挂载”测试不需要 OSS、NAS、VPC、AccessKey ID 或 AccessKey
Secret。OSS/NAS 测试需要额外的阿里云存储、角色和网络资源，见后文。

### 1.3 字节/火山引擎原生配置

字节无挂载测试需要准备：

1. 火山引擎 Access Key ID（AK）；
2. 火山引擎 Secret Access Key（SK）；
3. veFaaS 沙箱所在地域，例如 `cn-beijing`；
4. 已创建并发布、允许创建预热实例的 veFaaS 沙箱应用 Function ID；
5. 足够的实例/副本配额、OpenAPI 流控额度和费用预算。

配置示例：

```ini
[product.vol]
name = 字节
driver = volcengine_native
access_key = 这里填AK
secret_key = 这里填SK
region = cn-beijing
function_id = 这里填veFaaS沙箱应用ID
sandbox_lifetime = 6
sandbox_lifetime_unit = minute
ready_timeout_seconds = 120
poll_interval_seconds = 0.1
sdk_auto_retry = false
endpoint =
```

其中 `endpoint` 通常留空，让 SDK 根据地域选择官方 Endpoint。只有火山项目方
明确提供自定义 Endpoint 时才填写。`sdk_auto_retry = false` 表示压测中的一个
trial 对应一次 CreateSandbox 调用，避免 SDK 隐式重试改变请求数口径。

#### 1.3.1 火山原生单档连通性测试

正式压测前，先设置每秒创建1个、持续2秒：

```ini
[run]
product = vol
rates = 1
sandbox_counts = 2
storages = none
max_workers = 1000
output = results
exclude_first_from_mean = true
```

先预览，确认总共创建2个 Sandbox：

```powershell
python benchmark.py run-config --config benchmark.ini
```

确认凭证、配额和费用后正式运行：

```powershell
python benchmark.py run-config --config benchmark.ini --confirm
```

成功时应看到：

```text
api_success_rate_pct = 100
ready_success_rate_pct = 100
cleanup_success_rate_pct = 100
```

如果延迟指标全部为 `null`，表示没有成功创建任何 Sandbox。此时应直接查看本次
结果目录中的 `失败日志.txt`：

```text
InvalidAccessKey       → AK 无效、停用或抄写错误
SignatureDoesNotMatch  → SK 错误、AK/SK 不配对或字符大小写错误
AccessDenied           → 当前用户缺少 veFaaS 权限
资源不存在类错误       → Region 或 Function ID 不正确
```

AK/SK 区分大小写，必须从火山控制台复制同一对原始值，不要通过截图或 OCR
转录。如果使用临时凭证，还需要额外配置 Session Token；当前模板默认用于长期
AK/SK。

#### 1.3.2 火山原生正式压测

单档测试成功后，再设置正式档位：

```ini
[run]
product = vol
rates = 1,10,50,100,200
sandbox_counts = 60,600,3000,6000,12000
storages = none
max_workers = 1000
output = results
exclude_first_from_mean = true
```

然后执行：

```powershell
python benchmark.py run-config --config benchmark.ini
python benchmark.py run-config --config benchmark.ini --confirm
```

每个 trial 的原生生命周期为：

```text
CreateSandbox
→ DescribeSandbox 轮询到第一次 Ready
→ DescribeSandbox 第二次确认 Ready
→ KillSandbox 清理实例
```

程序使用和其他产品相同的 TPS 调度、线程上限、持续时间、成功率、百分位、CSV、
失败日志和全局结果矩阵。火山原生测试当前只支持 `storages = none`。

---

## 2. 进入项目目录

从 GitHub 克隆仓库后，在仓库根目录打开 PowerShell。后续所有相对路径和命令
都以仓库根目录为起点，不依赖某台机器的用户名、盘符或安装位置。

如果 PowerShell 当前位于仓库的上一级目录，可以使用仓库目录名进入：

```powershell
Set-Location 你的仓库目录名
```

确认项目文件存在：

```powershell
Get-ChildItem
```

至少应看到：

```text
benchmark.py
benchmark.template.ini
environment.yml
requirements.txt
env.template.txt
```

---

## 3. 选择 Python 环境并安装依赖

下面三种方式任选一种。不要同时执行三种。

### 3.1 方式A：直接使用当前 Python 环境

如果当前 Python 环境可以用于本项目，直接安装依赖：

```powershell
python -m pip install -r requirements.txt
```

这种方式最简单，但依赖会安装到当前 Python 环境。



### 3.2 方式B：使用 Python venv（可选）

不使用 Conda 时，也可以用 Python 自带的 venv：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止激活脚本，可以仅为当前 PowerShell 进程允许脚本：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 3.3 方式C：使用 Conda（可选）

已经安装 Anaconda 或 Miniconda 的用户，可以创建名为 `aliyun` 的独立环境：

```powershell
conda create -n aliyun python=3.11 -y
conda activate aliyun
python -m pip install -r requirements.txt
```

以后重新打开 PowerShell 运行本项目时，只需要先进入仓库根目录并激活环境：

```powershell
conda activate aliyun
```

Conda 只是可选的依赖隔离方式，不影响阿里云或字节产品的选择。

### 3.4 依赖版本

当前项目固定使用：

```text
e2b==2.31.0
e2b-code-interpreter==2.8.1
python-dotenv==1.2.2
volcengine-python-sdk==5.0.31
```

### 3.5 检查当前环境

```powershell
python --version
python -m pip show e2b e2b-code-interpreter python-dotenv volcengine-python-sdk
python -m pip check
```

`python -m pip check` 预期输出：

```text
No broken requirements found.
```

如果选择 Conda，提示符通常显示 `(aliyun)`；如果选择 venv，通常显示
`(.venv)`。直接使用当前 Python 时不一定显示环境名称。

```text
(aliyun) PS ...\仓库目录>
或
(.venv) PS ...\仓库目录>
```

---

## 4. 可选方式：创建并填写 .env

如果使用前面的 `run-config --config benchmark.ini` 配置模式，并已在对应产品段
填写所需凭证，可以跳过本节，不需要创建 `.env`。火山原生驱动也不读取这里的
E2B 变量。

直接使用 `smoke`、`plan`、`run` 等传统命令时，程序从 `.env` 读取连接参数。
GitHub 中保存的是不含真实密钥的普通文本模板 `env.template.txt`。配置模式也
可以将 `api_key` 留空，再通过 `api_key_env` 指定的环境变量读取密钥。

如果还没有 `.env`，执行：

```powershell
Copy-Item -LiteralPath env.template.txt -Destination .env
```

检查文件：

```powershell
Get-ChildItem -Force .env,env.template.txt
```

应该看到：

```text
.env
env.template.txt
```

打开 `.env`：

```powershell
notepad .env
```

至少填写下面三项。以北京地域为例：

```dotenv
E2B_API_KEY=e2b_你的完整APIKey
E2B_API_URL=https://api.cn-beijing.e2b.fc.aliyuncs.com
E2B_DOMAIN=cn-beijing.e2b.fc.aliyuncs.com

E2B_TEMPLATE=code-interpreter-v1
E2B_SANDBOX_TIMEOUT=300
```

各配置含义：

| 配置 | 含义 |
|---|---|
| `E2B_API_KEY` | 阿里云函数计算“云沙箱 → API Keys”中创建的完整密钥 |
| `E2B_API_URL` | 云沙箱 API 地址 |
| `E2B_DOMAIN` | Sandbox 实例连接域名 |
| `E2B_TEMPLATE` | 使用的沙箱模板 |
| `E2B_SANDBOX_TIMEOUT` | 单个 Sandbox 的存活超时，单位为秒；建议300，避免高负载下首条命令尚未完成就超时 |

`E2B_API_KEY`、`E2B_API_URL` 和 `E2B_DOMAIN` 必须属于同一地域。

`E2B_SANDBOX_TIMEOUT` 是单个 Sandbox 的最长存活时间，与
`--sandbox-counts`（本地客户端每个档位创建的数量）不是一回事。

如果不是北京地域，需要把 `cn-beijing` 换成实际地域。使用自定义域名时，
以阿里云项目提供的 API URL 和 Domain 为准。

不要把完整 API Key 写入 `env.template.txt`、聊天、截图或 Git 仓库。真实
密钥只保存在 `.env` 中；项目的 `.gitignore` 已忽略 `.env`。

### 4.1 检查环境变量是否读取成功

下面命令不会打印 API Key：

```powershell
python -c "from dotenv import load_dotenv; import os; load_dotenv(); print('API Key已读取:', bool(os.getenv('E2B_API_KEY'))); print('API URL已读取:', bool(os.getenv('E2B_API_URL'))); print('Domain已读取:', bool(os.getenv('E2B_DOMAIN')))"
```

预期输出：

```text
API Key已读取: True
API URL已读取: True
Domain已读取: True
```

---

## 5. 先做单个 Sandbox 连通性测试

正式发压前，先创建一个 Sandbox：

```powershell
python benchmark.py smoke --storage none
```

成功时重点检查：

```json
"api_success": true
"ready_success": true
"cleanup_success": true
```

其中：

- `api_success=true`：`Sandbox.create()` 成功返回；
- `ready_success=true`：第一条和第二条命令都成功执行；
- `cleanup_success=true`：测试后成功执行 `sandbox.kill()` 释放资源。

如果 smoke 失败，先检查 API Key、地域、API URL、Domain、模板和账号权限，
不要直接运行高并发测试。

---

## 6. 理解测试参数

正式命令格式：

```powershell
python benchmark.py run --rates <每秒启动数> --sandbox-counts <各档位沙箱数量> --storages none --confirm
```

### 6.1 `--rates`

表示每秒计划发起多少次 `Sandbox.create()`。

例如：

```text
--rates 50
```

表示每秒计划创建 50 个 Sandbox。

### 6.2 `--sandbox-counts`

表示每个 TPS 档位要创建多少个 Sandbox；数量必须与 `--rates` 中档位一一对应。

例如：

```text
--rates 50 --sandbox-counts 3000
```

表示：

```text
每秒发起 50 次创建
创建满 3000 个 Sandbox 后结束该档位
```

每个 Sandbox 的实际流程为：

```text
创建 → 第一条命令 → 第二条命令 → kill释放
```

即使60秒已经结束，程序仍会等待所有已经发出的请求完成、清理资源、计算
统计指标并写入结果文件，所以整条命令的运行时间通常超过60秒。

### 6.3 `--storages none`

表示无 OSS、无 NAS 挂载，只测试普通 Sandbox 启动速度。

### 6.4 `--confirm`

这是本地安全确认开关。只有加上它，程序才会真实创建可能计费的云资源。

不加 `--confirm` 时，程序只显示计划，不调用阿里云：

```powershell
python benchmark.py run --rates 50 --sandbox-counts 3000 --storages none
```

---

## 7. 正式测试前检查调用量

可以先使用 `plan` 查看调用量，它不会创建云资源：

```powershell
python benchmark.py plan --rates 1 --sandbox-counts 60 --storages none
python benchmark.py plan --rates 10 --sandbox-counts 600 --storages none
python benchmark.py plan --rates 50 --sandbox-counts 3000 --storages none
python benchmark.py plan --rates 100 --sandbox-counts 6000 --storages none
python benchmark.py plan --rates 200 --sandbox-counts 12000 --storages none
```

各档计划创建数量：

| 档位 | 持续时间 | 计划创建数量 |
|---:|---:|---:|
| 1次/秒 | 60秒 | 60 |
| 10次/秒 | 60秒 | 600 |
| 50次/秒 | 60秒 | 3000 |
| 100次/秒 | 60秒 | 6000 |
| 200次/秒 | 60秒 | 12000 |
| 合计 | - | 21660 |

这五档全部执行会创建21660个 Sandbox，可能产生较高费用，并可能触发账号
实例配额、并发创建限制或接口限流。执行前必须让阿里云账号管理员确认配额
和费用。

---

## 8. 逐档运行启动并发速度测试

每次只运行一个档位。上一档完成并确认资源已清理后，再运行下一档。

### 8.1 1次/秒，持续60秒

```powershell
python benchmark.py run --rates 1 --sandbox-counts 60 --storages none --confirm
```

计划创建60个 Sandbox。

### 8.2 10次/秒，持续60秒

```powershell
python benchmark.py run --rates 10 --sandbox-counts 600 --storages none --confirm
```

计划创建600个 Sandbox。

### 8.3 50次/秒，持续60秒

```powershell
python benchmark.py run --rates 50 --sandbox-counts 3000 --storages none --confirm
```

计划创建3000个 Sandbox。

### 8.4 100次/秒，持续60秒

```powershell
python benchmark.py run --rates 100 --sandbox-counts 6000 --storages none --confirm
```

计划创建6000个 Sandbox。

### 8.5 200次/秒，持续60秒

```powershell
python benchmark.py run --rates 200 --sandbox-counts 12000 --storages none --confirm
```

计划创建12000个 Sandbox。

不要同时打开多个 PowerShell 窗口运行这些命令，否则实际发压速度和资源消耗
会叠加。

---

## 9. 判断本地是否真的达到目标发压速度

脚本默认：

```text
--max-workers 1000
```

如果云端创建或命令执行耗时较长，1000个工作线程仍可能全部被占用，后续任务会
在本地排队。此时即使命令写的是 `100/s` 或 `200/s`，实际也可能达不到目标
速率。

必须查看：

```text
schedule_delay_p95_ms
```

它表示95%的请求相对计划发起时间的本地延误不超过多少毫秒：

- 几毫秒：本地发压调度通常正常；
- 数百毫秒：已经出现明显排队；
- 数千毫秒或持续增大：本地客户端没有达到目标速率，该档结果不能直接作为
  云沙箱在目标速率下的性能结论。

不要在没有确认本地线程能力、账号并发配额和费用的情况下盲目提高
`--max-workers`。

---

## 10. 结果目录和文件

配置模式使用 `[run]` 中的 `product` 选择产品，并从对应的
`[product.aliyun]` 或 `[product.vol]` 读取SDK连接参数。

每次测试会按照“测试内容 + 存储类型 + 档位 + 持续时间 + 时间戳”生成独立
目录。例如：

```text
results/
├── 全局测试结果.csv
├── 全局测试历史.csv
└── 20260804_120000_123_阿里云_启动并发速度_无挂载_50tps_持续60s/
    ├── 20260804_120000_123_阿里云_..._原始明细.csv
    ├── 20260804_120000_123_阿里云_..._汇总.csv
    ├── 20260804_120000_123_阿里云_..._汇总.json
    ├── 20260804_120000_123_阿里云_..._失败日志.csv
    └── 20260804_120000_123_阿里云_..._失败日志.txt
```

如果配置 `product = vol`，目录名中的产品名称会自动变为“字节”。

各文件用途：

| 文件 | 用途 |
|---|---|
| `原始明细.csv` | 每个 Sandbox 的独立延迟、成功状态、Sandbox ID 和错误信息 |
| `汇总.csv` | 当前档位的成功率、min、max、mean、P50、P90、P95、P99 |
| `汇总.json` | 与汇总 CSV 相同的 JSON 数据 |
| `失败日志.csv` | 只保留失败请求，方便 Excel 筛选和统计 |
| `失败日志.txt` | 失败阶段、错误信息和完整异常堆栈 |
| `results/全局测试结果.csv` | 跨厂商最新结果矩阵，档位列为 `1tps`、`10tps`、`50tps` 等 |
| `results/全局测试历史.csv` | 跨厂商完整历史；每行对应一次运行中的一个厂商、存储、TPS档位和持续时间 |

正式执行 `run` 后，当前测试的每个档位会先追加到
`results/全局测试历史.csv`。历史表中的 `run_id`、`completed_at_local` 和
`result_directory` 可以定位到原始测试目录，同一厂商重复测试同一档位不会覆盖。

随后脚本会根据历史表重建 `results/全局测试结果.csv`。矩阵每行由
`test_name + provider + product_name + driver + storage + sandbox_count + metric`
唯一确定，各档位分别
放在 `1tps`、`10tps`、`50tps`、`100tps`、`200tps` 等列中。例如：

```text
test_name,provider,product_name,driver,storage,sandbox_count,metric,unit,1tps,10tps,50tps
启动并发速度,aliyun,阿里云,e2b,none,3000,api_latency_p50_ms,ms,420.1,510.2,890.4
启动并发速度,vol,字节,volcengine_native,none,60,api_latency_p50_ms,ms,380.3,470.5,760.8
```

同一厂商、存储、持续时间和指标重复测试同一TPS时，矩阵采用最后一次测试值；
旧值仍完整保留在历史表中。只运行一个档位时，其他已测档位不会丢失。
`smoke` 连通性测试不会写入这两个全局表。

配置产品示例：

```ini
# 阿里云
product = aliyun

# 字节
product = vol
```

修改后统一运行 `python benchmark.py run-config --config benchmark.ini --confirm`。

即使本轮没有失败，也会生成失败日志：

- `失败日志.csv` 只有表头；
- `失败日志.txt` 写明“本轮无失败”。

---

## 11. 汇总 CSV 指标

### 11.1 基本字段

| 指标 | 含义 |
|---|---|
| `provider` | 厂商标识，例如 `aliyun`、`vol`、`ags` |
| `product_name` | 结果目录和表格使用的产品名称，例如“阿里云”“字节” |
| `driver` | 调用驱动；阿里云为 `e2b`，字节原生为 `volcengine_native` |
| `storage` | 存储场景，`none` 表示无挂载 |
| `target_rate_per_s` | 目标启动速率，单位为次/秒 |
| `rate_label` | 便于制作对比矩阵的档位标签，例如 `1tps`、`10tps`、`50tps` |
| `sandbox_count` | 当前 TPS 档位计划创建的沙箱数量 |
| `configured_max_workers` | 命令配置的线程上限，默认1000 |
| `effective_max_workers` | 当前档位实际建立的最大线程数，不超过请求总数 |
| `attempts` | 计划并实际安排的请求数量 |
| `mean_excludes_first_trial` | 是否将当前TPS档位的第1个沙箱排除在mean之外 |
| `*_mean_sample_count` | 对应mean实际使用的有效样本数 |

### 11.2 成功率

| 指标 | 含义 |
|---|---|
| `api_success_rate_pct` | 创建 API 成功返回且取得 Sandbox ID 的比例 |
| `ready_success_rate_pct` | E2B 两条命令均成功，或火山原生连续两次查询均为 Ready 的比例 |
| `cleanup_success_rate_pct` | E2B `sandbox.kill()` 或火山原生 `KillSandbox` 成功的比例 |

### 11.3 三段延迟

阿里云 E2B 驱动的时间点定义：

```text
T0：开始调用 Sandbox.create()
T1：Sandbox.create() 返回
T2：第一条命令执行完成
T3：第二条命令执行完成
```

对应关系：

```text
api_latency = T1 - T0
first_command_latency = T2 - T0
second_command_latency = T3 - T0
```

三项都是以 `Sandbox.create()` 开始调用的 T0 为起点，因此在同一个成功样本中
应满足：`api_latency <= first_command_latency <= second_command_latency`。
第一条和第二条命令延迟现在都是累计就绪时间，不再是相邻阶段自身的耗时。

字节/火山原生驱动通过状态查询判断就绪。为复用同一套 CSV、统计和全局矩阵，
字段名称保持不变，但里程碑定义为：

```text
T0：开始调用原生 CreateSandbox
T1：CreateSandbox 返回 Sandbox ID
T2：DescribeSandbox 第一次返回 Ready
T3：紧接着再次调用 DescribeSandbox，第二次确认 Ready

api_latency = T1 - T0
first_command_latency = T2 - T0   （原生驱动实际表示首次 Ready）
second_command_latency = T3 - T0  （原生驱动实际表示二次 Ready 确认）
```

火山原生模式不会为了填充这两个字段而执行沙箱内部命令。原始明细中的
`ready_poll_count` 是 DescribeSandbox 调用次数，`last_observed_status` 是最后
一次查询到的状态。无论使用哪种驱动，三个延迟都从同一个 T0 开始，成功样本
应满足 `api_latency <= first_command_latency <= second_command_latency`。

当配置 `exclude_first_from_mean = true` 时，每个TPS档位中 `trial_index = 1` 的
第一条记录不参与三项延迟的 `mean`。该记录仍然保留在原始明细中，并继续参与
`min`、`max`、P50、P90、P95、P99以及成功率统计。若某档只有1次有效请求，排除
后该档的mean为空，同时 `*_mean_sample_count` 为0。

三段延迟都输出：

```text
min、max、mean、P50、P90、P95、P99
```

含义：

| 统计值 | 含义 |
|---|---|
| `min` | 本轮有效样本中的最小延迟 |
| `max` | 本轮有效样本中的最大延迟 |
| `mean` | 有效样本的算术平均延迟 |
| `P50` | 50%的有效样本不超过该值，代表典型表现 |
| `P90` | 90%的有效样本不超过该值 |
| `P95` | 95%的有效样本不超过该值，常用于观察尾延迟 |
| `P99` | 99%的有效样本不超过该值 |

所有延迟单位均为毫秒（ms）。这些统计值已经按有效样本计算，不能再除以
`attempts` 或 `sandbox_count`。

完整可复制说明：

```text
指标说明.txt
```

Excel 指标字典：

```text
指标说明.csv
```

---

## 12. 失败日志

失败日志通过 `failure_phase` 区分阶段：

| 阶段 | 含义 |
|---|---|
| `api_create` | 创建 Sandbox 失败 |
| `first_command` | 第一条命令失败 |
| `second_command` | 第二条命令失败 |
| `ready_poll` | 火山原生 DescribeSandbox 等待 Ready 失败或超时 |
| `ready_confirm` | 火山原生第二次 Ready 确认失败 |
| 清理错误字段 | E2B `sandbox.kill()` 或火山原生 `KillSandbox` 失败 |

重点字段：

```text
trial_index
scheduled_at_utc
sandbox_id
api_success
ready_success
cleanup_success
failure_phase
error_type
error_message
error_traceback
cleanup_error_type
cleanup_error_message
cleanup_error_traceback
ready_poll_count
last_observed_status
```

提交阿里云工单时，可以提供失败时间、Sandbox ID、错误类型和错误信息，但
不要提供完整 API Key。

---

## 13. 推荐执行顺序

下面的流程同时适用于阿里云和字节/火山。首次下载后先安装依赖并复制配置模板：

```powershell
# 以下命令均在仓库根目录执行
python -m pip install -r requirements.txt
python -m pip check
Copy-Item benchmark.template.ini benchmark.ini
notepad benchmark.ini
```

在 `benchmark.ini` 中选择 `product`，填写对应厂商凭证，并设置共同的测试规模：

```ini
[run]
product = aliyun
rates = 1,10,50,100,200
sandbox_counts = 10,100,500,1000,2000
storages = none
max_workers = 1000
output = results
exclude_first_from_mean = true
```

建议先把 `rates = 1`、`sandbox_counts = 2` 做小规模连通性验证。先预览，确认
产品、档位和预计请求数无误后再正式执行：

```powershell
python benchmark.py run-config --config benchmark.ini
python benchmark.py run-config --config benchmark.ini --confirm
```

小规模测试成功后，再改回正式档位并重复上述两条命令。测试阿里云时设置
`product = aliyun`，测试字节/火山时只改为 `product = vol`；公平对比时不要改变
`rates`、`sandbox_counts`、`max_workers` 和 `exclude_first_from_mean`。

如果选择了 Conda，应在上述命令前先执行：

```powershell
conda activate aliyun
```

如果选择了 venv，应先执行：

```powershell
.\.venv\Scripts\Activate.ps1
```

如果直接使用当前 Python 环境，则不需要执行任何激活命令。

每档完成后，应检查：

1. `api_success_rate_pct`；
2. `ready_success_rate_pct`；
3. `cleanup_success_rate_pct`；
4. `schedule_delay_p95_ms`；
5. `失败日志.csv` 和 `失败日志.txt`；
6. 对应厂商控制台中是否仍有未释放 Sandbox。

---

## 14. 对象存储 OSS 启动测试

### 14.1 OSS 是什么

OSS 是阿里云对象存储服务。测试时，云沙箱会把一个 OSS Bucket 挂载到
Sandbox 内的目录，例如 `/mnt/oss`。

这不是模拟测试：必须事先存在一个真实的 OSS Bucket，并且函数计算必须有
权限访问该 Bucket。

### 14.2 OSS 测试需要哪些东西

无挂载测试使用的三项仍然必须存在：

```dotenv
E2B_API_KEY=
E2B_API_URL=
E2B_DOMAIN=
```

此外，需要向主管或云平台管理员获取三个实际值：

| 需要的值 | 示例 | 含义 |
|---|---|---|
| OSS Bucket 名称 | `company-sandbox-test` | 需要挂载的 OSS 存储桶 |
| OSS Endpoint | `http://oss-cn-beijing-internal.aliyuncs.com` | Sandbox 访问 Bucket 的地址 |
| RAM 角色 ARN | `acs:ram::1234567890123456:role/fc-sandbox-oss-role` | 允许函数计算访问 Bucket 的角色 |

虽然 `.env` 只填写两行，但第一行 JSON 同时包含 Bucket 名称和 Endpoint：

```text
E2B_OSS_CONFIG_JSON
├── bucketName：Bucket名称
└── endpoint：OSS Endpoint

E2B_ROLE_ARN
└── RAM角色ARN
```

### 14.3 OSS 资源要求

1. Bucket 与云沙箱最好位于同一地域；
2. 同地域访问应优先使用该地域的 OSS 内网 Endpoint；
3. RAM 角色必须信任函数计算服务；
4. RAM 角色必须具有目标 Bucket 的访问权限；
5. `readOnly=false` 时，角色需要读取和写入权限；
6. `readOnly=true` 时，只测试只读挂载；
7. Bucket、请求、存储和流量可能产生费用。

不要使用个人 AccessKey ID/AccessKey Secret 代替 RAM 角色 ARN。

### 14.4 在 .env 中填写 OSS 配置

打开 `.env`：

```powershell
notepad .env
```

在原有 E2B 配置后增加：

```dotenv
E2B_OSS_CONFIG_JSON={"mountPoints":[{"bucketName":"这里填Bucket名称","mountDir":"/mnt/oss","bucketPath":"/","endpoint":"这里填OSS Endpoint","readOnly":false}]}
E2B_ROLE_ARN=这里填RAM角色ARN
```

示例：

```dotenv
E2B_OSS_CONFIG_JSON={"mountPoints":[{"bucketName":"company-sandbox-test","mountDir":"/mnt/oss","bucketPath":"/","endpoint":"http://oss-cn-beijing-internal.aliyuncs.com","readOnly":false}]}
E2B_ROLE_ARN=acs:ram::1234567890123456:role/fc-sandbox-oss-role
```

示例中的 Bucket、账号 ID 和角色名称不能直接照抄。

JSON 字段解释：

| 字段 | 含义 |
|---|---|
| `bucketName` | 真实 OSS Bucket 名称 |
| `mountDir` | OSS 在 Sandbox 内的目录，建议 `/mnt/oss` |
| `bucketPath` | Bucket 内需要挂载的目录，`/` 表示根目录 |
| `endpoint` | OSS Endpoint，地域必须正确 |
| `readOnly` | `false` 表示读写，`true` 表示只读 |

JSON 必须保持在一行内，不要使用中文引号。

### 14.5 先做一个 OSS smoke 测试

```powershell
python benchmark.py smoke --storage oss
```

重点检查：

```json
"api_success": true
"ready_success": true
"cleanup_success": true
```

如果失败，查看当前结果目录中的：

```text
失败日志.csv
失败日志.txt
```

常见检查项：

- Bucket 名称是否正确；
- Endpoint 是否与地域匹配；
- RAM 角色 ARN 是否正确；
- RAM 角色是否有 Bucket 权限；
- API Key、API URL、Domain 是否属于同一地域。

### 14.6 OSS 全部档位命令

每条命令只运行一个档位，每档持续1秒。

#### OSS 1次/秒

```powershell
python benchmark.py run --rates 1 --sandbox-counts 1 --storages oss --confirm
```

#### OSS 10次/秒

```powershell
python benchmark.py run --rates 10 --sandbox-counts 10 --storages oss --confirm
```

#### OSS 50次/秒

```powershell
python benchmark.py run --rates 50 --sandbox-counts 50 --storages oss --confirm
```

#### OSS 100次/秒

```powershell
python benchmark.py run --rates 100 --sandbox-counts 100 --storages oss --confirm
```

#### OSS 200次/秒

```powershell
python benchmark.py run --rates 200 --sandbox-counts 200 --storages oss --confirm
```

OSS 五档合计计划创建：

```text
1 + 10 + 50 + 100 + 200 = 361 个 Sandbox
```

建议按 `1 → 10 → 50 → 100 → 200` 顺序执行。上一档失败时不要继续升档。

### 14.7 OSS 测试的重要限制

当前脚本会把 OSS 配置传给 `Sandbox.create(metadata=...)`，并执行两条 Python
命令检查 Sandbox 是否可用。正式提交“OSS 挂载成功率”结论时，还应确认
Sandbox 内的 `/mnt/oss` 确实存在并能按照只读/读写要求访问；仅有两条普通
Python 命令成功，不能单独证明 OSS 文件读写一定成功。

---

## 15. 文件存储 NAS 启动测试

### 15.1 NAS 是什么

NAS 是阿里云文件存储服务，可以理解为位于阿里云 VPC 内的共享文件系统。
挂载后，Sandbox 可以通过 `/mnt/nas` 等普通目录访问 NAS 文件。

### 15.2 NAS 测试需要哪些东西

除了 E2B API Key、API URL 和 Domain，还需要：

| 需要的值或资源 | 示例 | 含义 |
|---|---|---|
| 通用型 NAS 文件系统 | 由 NAS 控制台创建 | 需要挂载的共享文件系统 |
| NAS 挂载地址 | `xxx.cn-beijing.nas.aliyuncs.com:/` | NAS 服务端地址和远端目录 |
| VPC ID | `vpc-xxxx` | NAS 挂载点所在 VPC |
| vSwitch ID | `vsw-xxxx` | Sandbox 接入 VPC 使用的交换机 |
| 安全组 ID | `sg-xxxx` | Sandbox 的网络访问规则 |
| 执行角色 ARN | `acs:ram::...:role/...` | 允许函数计算访问 NAS 的角色 |
| Sandbox 模板 | 由项目方提供 | 模板需要配置相同 VPC 和执行角色 |
| E2B metadata key | 必须由阿里云项目方确认 | E2B SDK 传递 NAS 配置所用字段名 |

NAS 挂载点、Sandbox 模板必须使用匹配的 VPC、vSwitch 和安全组。

### 15.3 必须先确认 NAS 接口方式

当前脚本的 `--storages file` 使用：

```python
Sandbox.create(metadata={E2B_FILE_METADATA_KEY: E2B_FILE_CONFIG_JSON})
```

但是阿里云公开的 NAS 数据面示例使用请求体顶层的 `nasConfig`，公开文档没有
明确给出 E2B Python SDK 对应的 metadata key。

因此：

1. `E2B_FILE_METADATA_KEY` 不是 API Key；
2. 它不能从 NAS 控制台直接复制；
3. 不能自行猜测为某个字符串；
4. 必须由阿里云项目人员明确提供；
5. 如果项目方确认 E2B metadata 不支持 NAS，就需要把脚本改为调用官方
   CreateSandbox 数据面 API 的 `nasConfig`，当前 `file` 命令不能直接使用。

可以把下面的问题发给主管或阿里云项目人员：

```text
请提供用于云沙箱 NAS 测试的通用型 NAS 挂载地址、VPC ID、vSwitch ID、
安全组 ID、执行角色 ARN和 Sandbox 模板名称。另外请确认：
使用 E2B Python SDK 的 Sandbox.create(metadata=...) 时，
NAS 配置对应的 metadata key 和 JSON 格式是什么？
如果 E2B metadata 不支持 NAS，是否必须调用数据面 CreateSandbox API
并传入顶层 nasConfig？
```

未确认这项接口前，不要把 `--storages file` 的成功结果当作真实 NAS 挂载
性能结果。

### 15.4 在 .env 中填写 VPC

得到 VPC、vSwitch 和安全组后：

```dotenv
E2B_VPC_CONFIG_JSON={"vpcId":"vpc-xxxx","securityGroupId":"sg-xxxx","vSwitchIds":["vsw-xxxx"]}
```

字段解释：

| 字段 | 含义 |
|---|---|
| `vpcId` | NAS 挂载点所在 VPC |
| `securityGroupId` | Sandbox 使用的安全组 |
| `vSwitchIds` | Sandbox 使用的一个或多个 vSwitch |

### 15.5 在 .env 中填写 NAS 配置

只有项目方确认 E2B metadata key 后才能填写：

```dotenv
E2B_FILE_METADATA_KEY=这里填项目方提供的metadata key
E2B_FILE_CONFIG_JSON={"groupId":1000,"userId":1000,"mountPoints":[{"serverAddr":"这里填NAS挂载地址","mountDir":"/mnt/nas"}]}
```

字段解释：

| 字段 | 含义 |
|---|---|
| `groupId` | NAS 文件用户组 ID，默认可使用1000 |
| `userId` | NAS 文件用户 ID，默认可使用1000 |
| `serverAddr` | NAS 挂载地址和远端目录，通常以 `:/` 结尾 |
| `mountDir` | NAS 在 Sandbox 内的目录，例如 `/mnt/nas` |

上面的 JSON 只是常见 `nasConfig` 结构示例。项目方提供的 E2B metadata JSON
格式如果不同，必须以项目方提供的格式为准。

执行角色通常配置在 Sandbox 模板上，不是随意填写到
`E2B_FILE_CONFIG_JSON` 中。

### 15.6 先做一个 NAS smoke 测试

确认接口、模板、VPC 和 NAS 配置后：

```powershell
python benchmark.py smoke --storage file
```

如果失败，首先查看失败日志，然后检查：

- VPC、vSwitch、安全组是否匹配；
- NAS 挂载点是否属于同一 VPC；
- Sandbox 模板是否启用 VPC；
- 执行角色是否具有 NAS 权限；
- NAS `serverAddr` 是否正确；
- `E2B_FILE_METADATA_KEY` 和 JSON 格式是否由项目方确认。

### 15.7 NAS 全部档位命令

以下命令只有在 NAS metadata 接口已经确认且 smoke 成功后才能执行。

#### NAS 1次/秒

```powershell
python benchmark.py run --rates 1 --sandbox-counts 1 --storages file --confirm
```

#### NAS 10次/秒

```powershell
python benchmark.py run --rates 10 --sandbox-counts 10 --storages file --confirm
```

#### NAS 50次/秒

```powershell
python benchmark.py run --rates 50 --sandbox-counts 50 --storages file --confirm
```

#### NAS 100次/秒

```powershell
python benchmark.py run --rates 100 --sandbox-counts 100 --storages file --confirm
```

#### NAS 200次/秒

```powershell
python benchmark.py run --rates 200 --sandbox-counts 200 --storages file --confirm
```

NAS 五档合计计划创建：

```text
1 + 10 + 50 + 100 + 200 = 361 个 Sandbox
```

### 15.8 NAS 测试的重要限制

正式提交“NAS 挂载成功率”结论前，还应确认 Sandbox 内 `/mnt/nas`：

1. 目录真实存在；
2. 可以列出目录；
3. 可以按权限要求读取文件；
4. 读写场景下可以创建并读取测试文件。

仅有两条普通 Python 命令成功，不能单独证明 NAS 已挂载成功。

---

## 16. 三类测试所需配置汇总

| 测试 | 必需配置 |
|---|---|
| 无挂载 | API Key、API URL、Domain、模板、配额 |
| OSS | 无挂载全部配置 + Bucket 名称 + Endpoint + RAM角色ARN |
| NAS | 无挂载全部配置 + NAS地址 + VPC/vSwitch/安全组 + 执行角色 + 已确认的 NAS 接口格式 |

三类命令的调用量：

| 测试 | 每档持续时间 | 五档合计创建数量 |
|---|---:|---:|
| 无挂载 | 60秒 | 21660 |
| OSS | 1秒 | 361 |
| NAS | 1秒 | 361 |

不要并行运行无挂载、OSS 和 NAS 测试。每完成一档，都要检查成功率、失败日志、
`schedule_delay_p95_ms` 和阿里云控制台中的资源清理情况。
