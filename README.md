# 多语种UI文本格式检测系统

基于多模态大模型的 Excel 多语种 UI 截图缺陷自动检测系统。上传测试记录表，系统逐行抽取内嵌截图，
对每个语种调用视觉大模型，自动判定 **截断 / 重叠 / 缺字** 三类 UI 缺陷，并通过 SSE 实时推送进度，
最终产出带缺陷截图的结果文档。

---

## 1. 模型配置

首次运行必须先编辑项目根目录的 `.env`（由 `.env.example` 改名而来）：

```
# OpenAI 官方
OPENAI_API_KEY=sk-xxxx
LLM_MODEL=gpt-4o  # 模型必须具备多模态输入能力

# 或任意兼容网关（Azure OpenAI / 阿里云百炼 / vLLM / One-API …）
OPENAI_BASE_URL=https://your-gateway.example.com/v1
LLM_MODEL=qwen-vl-max

LLM_CONCURRENCY=5      # 全局并发上限，防止触发网关限流
LLM_IMAGE_DETAIL=high  # 视觉精度；high 对 OCR 与截断判定更准，Token 更高
```

> 网关不支持 `response_format=json_schema` 时会自动降级为 `json_object` 并做容错解析，
> 无需手动切换。

### Token 预算

`LLM_MAX_TOKENS` 默认 2048。若使用**推理型模型**（返回 `reasoning_tokens` 的），
思考过程同样计入这个额度，取小了会出现「finish_reason=length 且正文为空」。
系统会显式识别这种截断并给出可读报错，而不是把它当成普通的解析失败。

---

## 2. 快速开始

Windows下：
双击 start.bat；或者在根目录下运行python start.py

macOS / Linux下：
在根目录下运行./start.sh

脚本会自动完成：创建虚拟环境 → 安装前后端依赖 → 从 `.env.example` 生成 `.env` → 同时拉起前后端。

| 服务 | 地址 |
| :--- | :--- |
| 前端页面 | http://127.0.0.1:5173 |
| 后端 API 文档 | http://127.0.0.1:8000/docs |

若只想先跑通链路、不消耗 Token：

```bash
# 在 .env 中设置 MOCK_LLM=true，或在启动前导出该环境变量
MOCK_LLM=true python start.py
```

其他用法：

```bash
python start.py --setup-only    # 只装依赖不启动
python start.py --backend       # 只起后端
python start.py --frontend      # 只起前端
python start.py --port 9000     # 指定后端端口
```

---

## 3. 输入文件要求

系统按以下约定解析 Excel（默认读取第一个 Sheet 的第 1 行为表头）：

| 列 | 内容 |
| :--- | :--- |
| A–D | `Module` / `String path in English` / `Software Version` / `Remarks` 等元信息（可选，会作为上下文喂给模型） |
| **英语列** | 表头含「英语」或 `English`（如 `英语(English)`）的列，作为**基准列** |
| 英语列之后 | 各语种列，表头形如 `韩语(한국어)`，每列内嵌该语种的 UI 截图 |

关键行为：

- **图片只认锚点单元格**，与表头文字无关，因此语种列的排列顺序可以自由调整。
- **某行某个语种没有截图 = 该语种未测试**，不会被当作缺陷；只有存在截图的单元格才会被检测。
- 数据行只要该行有任意一张截图就会被处理，不要求 A–D 列必须有值。
- 英语列通过加权匹配定位（`英语` / `English` 优先于 `String path in English` 这类含英文单词的表头），
  全部匹配不上时回退到 E 列。

以仓库自带的 `record_image.xlsx` 为例：表头 1 行，数据行 4 行，英语列 E，语种列 F–AS 共 40 个，
内嵌截图 115 张。

---

## 4. 检测口径

| 缺陷 | 判定依据 |
| :--- | :--- |
| **截断** | 文本未显示完整：尾部省略号（… / ...）、字符被容器边界切断、句子语义不完整 |
| **重叠** | 文本与相邻文字、图标或控件像素互相覆盖、压字 |
| **缺字** | 出现豆腐块 □、空心方框或乱码问号，通常是小语种字体缺失 |

Prompt 强制要求「只依据截图中可见的像素证据判断」，并明确排除了两类误报：
截图自身的裁切边界不算截断；截图之外的推测不算证据。

**英文列先于小语种执行**——它的 OCR 结果会作为对照文本传入小语种 Prompt。
小语种译文通常比英文长 30% 以上，是最主要的截断成因，有基准对照能显著降低漏判。

---

## 5. 输出文件

`data/results/{task_id}_result.xlsx`，布局如下：

| 列 | 内容 |
| :--- | :--- |
| A | **英文UI缺陷** —— 英语基准截图的缺陷描述 |
| B | **英文UI截图** |
| C | **非英文UI缺陷汇总** —— 形如 `韩语：截断、缺字（依据…）` 的逐条罗列 |
| D 起 | **动态语种列** —— 仅针对检出缺陷的语种创建一列，列头为语种名，单元格内嵌该语种截图 |

首行冻结并启用筛选。截图按固定显示高度等比缩放后嵌入（原图 1200×2600+，
按原始尺寸嵌入会让结果文件膨胀到数百 MB 且无法浏览）。

---

## 6. 架构

```text
[ Vue 3 + Vite + Element Plus + Tailwind ]  前端 SPA
       |         ^
   (1) | POST    | (4) SSE 实时进度
Upload |         |
       v         |
[ FastAPI 网关 & 业务逻辑 ]  <--->  [ SQLite: tasks / row_results ]
       |
   (2) | asyncio 后台任务
       v
[ 核心处理引擎：openpyxl 解析 + 截图落盘 ]
       |
   (3) | Base64 图片 + 结构化 Prompt
       v
[ AI 代理层：AsyncOpenAI + Semaphore 限流 + Tenacity 退避重试 ]
       v
[ 多模态 LLM（任意 OpenAI 兼容网关）]
```

### 目录结构

```text
├── backend/app/
│   ├── main.py                  FastAPI 入口、CORS、生命周期、静态挂载
│   ├── config.py                .env 配置
│   ├── database.py              SQLite（aiosqlite），含断点续传所依赖的 row_results
│   ├── models.py                Pydantic V2 契约 + LLM 输出强校验
│   ├── api/tasks.py             上传 / SSE / 结果 / 下载 / 续跑
│   ├── core/events.py           进程内 SSE 事件总线（支持历史回放）
│   └── services/
│       ├── excel_parser.py      表头语种列识别 + 截图抽取落盘
│       ├── prompts.py           Prompt 模板 + JSON Schema
│       ├── ai_agent.py          并发限流 / 退避重试 / 容错 JSON 解析
│       ├── pipeline.py          全流程编排
│       └── result_builder.py    结果文档生成
├── frontend/src/
│   ├── api/                     接口封装与类型定义
│   ├── composables/useTaskStream.ts   SSE 订阅（含终态主动断开）
│   └── components/              上传 / 进度 / 结果 / 历史
├── data/                        运行期数据（已 gitignore）
│   ├── uploads/{task_id}/       原件 + 抽取的截图 + manifest.json
│   └── results/                 结果文件
├── tools/
│   ├── vision_probe.py          多模态能力探针（合成图 + 已知文字 + token 计量）
│   └── llm_check.py             真实截图端到端连通性自检
├── start.py / start.bat / start.sh
└── .env.example
```

### 接口一览

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| POST | `/api/v1/tasks/upload` | 上传 Excel，立即返回 `task_id`，后台自动开跑 |
| GET | `/api/v1/tasks/{id}/stream` | SSE 进度流（`text/event-stream`） |
| GET | `/api/v1/tasks` | 任务历史 |
| GET | `/api/v1/tasks/{id}` | 任务状态 |
| GET | `/api/v1/tasks/{id}/result` | 按语种聚合的缺陷统计 |
| GET | `/api/v1/tasks/{id}/findings` | 逐图检测结论 |
| POST | `/api/v1/tasks/{id}/resume` | 断点续跑 |
| GET | `/api/v1/tasks/{id}/download` | 下载结果文档 |

SSE 事件类型：`state`（连接即推的当前状态）、`started`、`progress`、`image_started`、
`finding`、`complete`、`error`、`closed`。每条事件带自增 `id`，服务端按
`Last-Event-ID` 回放漏掉的事件，因此刷新页面或断线重连都不会丢进度。

---

## 7. 工程要点

**并发与限流** — 行串行、行内各语种 `asyncio.gather` 并发，全局在途请求数由
`asyncio.Semaphore(LLM_CONCURRENCY)` 兜住。行串行保证了同一行的英文基准先于小语种拿到。

**重试策略** — 仅对瞬时错误（限流 / 超时 / 连接失败 / 5xx）做指数退避 + 抖动重试，
`400`、`401` 这类参数与鉴权错误立即抛出，不做无意义重试。单张截图重试耗尽后只记录该图的
错误并继续，不会拖垮整个任务。

**内存控制** — openpyxl 以非 `read_only` 模式加载（否则拿不到 `ws._images`）；
图片一旦写盘立刻释放引用，进程内不长期持有全部图像字节，流水线按需从磁盘读图。

**图片预处理** — Excel 内嵌截图常为 1200×2600+ 的超长图，直传既贵又易超限。
编码前按长边上限等比降采样，若仍超出字节预算则按 0.8 比例继续降采样。

**断点续传** — 每张截图的结论实时落库 `row_results`。进程重启时残留的
`PROCESSING` 任务会被标记为 `INTERRUPTED`，调用 `/resume` 即可跳过已成功的截图继续跑，
省下重复的 Token 与时间。实测中断于 63/115 后续跑耗时 5.1s（完整跑一遍 10.3s），
最终结果与完整跑完全一致且无重复记录。

**失败快速化** — 未配置 `OPENAI_API_KEY` 时任务直接失败并给出可读提示，
而不是逐张图重试后以「已完成但全部失败」的形式蒙混过关。

**单端口部署** — 前端 `npm run build` 后，后端启动时会自动把 `frontend/dist`
挂到根路径，无需再起 Node 进程。
