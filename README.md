# 经营分析 Agent

FastAPI + Vue 3 的本地电商经营问数应用。支持版本化 Excel/CSV 数据接入、指标查询、期间比较、品类贡献、商家排名、履约/评价分析、复购、多轮条件继承、主动澄清、SQL 依据、数据质量和历史回放。

## 快速启动（Windows）

在项目根目录打开 PowerShell。依赖已安装且前端已构建时，直接运行：

```powershell
.\scripts\start.ps1
```

浏览器访问 **http://127.0.0.1:8000**。API 文档为 **http://127.0.0.1:8000/docs**。首次启动自动导入材料，原始文件保持不变。

重新安装需要 Python 3.11+ 和 Node.js 22+：

```powershell
.\scripts\setup.ps1 -Python python
.\scripts\start.ps1
```

如果 Python 不在 PATH，将 `-Python` 指定为本机 Python 可执行文件路径。安装脚本使用后端依赖锁和前端 package-lock.json。

跨平台手动启动：

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.lock.txt
cd frontend
npm ci
npm run build
cd ../backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端开发时，在 `frontend` 运行 `npm run dev`，Vite 将 `/api` 代理至 8000 端口；另开终端启动后端。

## 数据文件

默认从项目父目录递归查找以下精确文件名：

- `ecommerce_agent_demo.xlsx`：主版本 `excel-main`。
- `ecommerce_agent_demo_csv.zip`：独立小版本 `csv-demo`。

数据不混合，导入结果保存在项目 `data/`。manifest 记录源文件哈希、导入版本、各表行数和日期范围；查询、证据和回放绑定该版本。源文件或导入逻辑版本改变后重新生成数据库。参考答案不提供给规划器。

**两个数据集与参考答案的对应关系（重要）**：Excel 主版与 CSV 小版是两份不同数据，同一问题在两份数据上的答案不同。每条回答都会带【数据集名】前缀：

- `excel-main`（默认）：赛题 PDF 示例（2025 年 3/4 月对比、数码家电贡献）与 Excel 练习题参考答案对应此数据集；
- `csv-demo`：`practice_questions.csv`（材料包 CSV 目录内）的参考答案对应此数据集——提问前在界面右上角切到「小样本演示（CSV，2025上半年）」。

CSV 小版只覆盖 2025 年，裸月份问题会自动按 2025 年解释；Excel 主版跨两个年份，裸月份会先确认年份。

部署到其他位置时，复制 `.env.example` 为 `.env`，填写 `SOURCE_ROOT`。已登记数据集 ID 从服务端映射到文件，HTTP 请求不能指定任意文件路径。不要在服务运行时替换源文件或数据库；更新数据前停止服务再启动。

## 规则模式与模型模式

复制 `.env.example` 为 `.env` 即完成模型接入。规则模式无需 API 密钥：它将受支持的中文问法转换为统一分析计划，不是实时大模型回答；不支持的问题会解释限制。界面明确显示当前模式。

推荐配置（初赛评审跑测模型 Qwen3.8-Max，阿里云百炼 OpenAI 兼容接口）：

```dotenv
AGENT_MODE=llm
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=your-dashscope-key
LLM_MODEL=qwen3.8-max
```

任何兼容 Chat Completions 的接口均可接入。模型接收精简目录和规则，输出 Pydantic 校验的分析计划；指标值仍由数据库计算，模型不生成 SQL、不产出数字。未显式设置 `AGENT_MODE` 时，填齐 `LLM_*` 三项即自动启用模型模式，否则自动使用规则模式。

健壮性约定：服务若不接受 `response_format` 结构化输出，适配器自动降级重试（提示词仍要求纯 JSON）；模型服务异常或输出无效时，默认**回退规则解析并在结果中明确标注**（`LLM_FALLBACK_RULES=1`，设为 0 则直接失败）——回退绝不伪装成模型成功。密钥仅在后端读取，不发送至浏览器或查询子进程。

本地测试覆盖规则模式及模拟 HTTP 的模型适配器（含结构化输出降级与回退路径）；接入真实服务后，建议按 README 推荐演示复核一遍。

## 推荐演示

1. `2025年4月已送达订单商品销售额比3月变化多少？哪个品类贡献最大？能确定增长是促销带来的吗？`
2. `只看数码家电`
3. `再按商家拆开`
4. 新会话：`4月商品销售额是多少？`，选择年份后继续。
5. `2025年5月已送达订单不少于10笔的商家中，延迟率最高的是谁？`
6. `有评价的已送达订单中，延迟订单和准时订单平均评分分别是多少？`
7. `有多少自然客户完成了至少2笔已送达订单？`
8. 切换 CSV 小版本，查询 `2025年4月销售额`，核对版本差异。
9. `为什么直接把订单明细和支付表连接会重复计算销售额？`（回答带本数据集定量对照与两条 SQL 证据）
10. 点开结果的「查询依据」查看口径/SQL/参数/数据哈希；「历史记录」页对旧查询「按原计划重放」（不改变当前已确认条件）；「数据质量」页查看检查明细。

可查看每次运行的口径、SQL 参数、执行粒度、数据版本和校验依据。“回放”按保存的计划和原数据版本重新执行，不重新调用模型规划；不支持任意旧文件版本的自动恢复。

## 计算与执行边界

- 商品销售额只统计 `delivered` 明细 `price`，不含运费；经营月份默认按 `purchase_at`。
- 订单数去重；复购以 `customer_unique_id` 为自然客户键。
- 明细与支付分别按订单汇总，避免一对多交叉关联。商家履约按 `(seller_id, order_id)` 去重。
- 评分只以有效评价为样本；未知值不自动当作零分、零金额或准时。
- 金额使用 DECIMAL，API 返回精确十进制字符串；汇总和环比在后端完成。
- 运行通过 LangGraph 节点：计划 → 澄清/恢复 → 受控查询 → 结果核验。SQLite checkpoint 保存暂停点，业务状态成功后原子提交。
- SSE 事件先持久化再展示，支持序号恢复；分页读取固定结果，不触发重复执行。
- SQL 只由注册指标编译器生成，执行前通过 AST/表/函数白名单；DuckDB 只读并禁用外部读取与扩展加载。
- 查询在独立进程执行，环境变量使用白名单，支持超时与取消。独立进程不等于 OS 文件沙箱，因此 **任意模型 SQL/自由 SQL 执行接口未开放**。
- 本版本面向本机单用户演示，默认绑定 127.0.0.1。多用户部署还需接入身份认证和资源授权；不应直接公开无认证开发服务。

数据缺少成本、流量、活动及广告记录，对利润、转化率、促销因果等问题明确说明缺失证据。数据包含两个年份，裸月份在没有上下文时需要澄清。相对日期使用当前真实日期，不把“本月”悄悄替换成数据最新月份。

一次分析支持一个指标；同时问多个指标会明确提示分别提问（复杂问题自动拆解为后续演进方向）。平均口径支持整体汇总，按品类/商家分组需分别提问。

## 验证

```powershell
.\scripts\verify.ps1
```

或在 backend 执行 `python -m pytest -q`，在 frontend 执行 `npm run build`。测试覆盖原始材料的确定性计算、规划器、多表防重复、日期和筛选继承、澄清恢复、版本隔离、SSE、回放、取消/并发以及拒绝行为。实际执行记录见 `evaluation/verification.md`。

## 目录

```text
backend/app/     FastAPI、会话存储、LangGraph、规划器、数据与查询引擎
backend/tests/   后端测试
frontend/src/    Vue 工作台、Pinia 状态、SSE、表图和证据组件
scripts/        安装、启动、验证
evaluation/     核验记录
CONTRACTS.md    接口和模块契约
DESIGN.md       设计与适用范围
```

第三方项目调研见父目录中的 `DataAgent调研与FastAPI-Vue适配方案.md`；本项目依据其设计独立实现，第三方依赖与参考列于 `THIRD_PARTY_NOTICES.md`。
