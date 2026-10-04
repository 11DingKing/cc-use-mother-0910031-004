# 讲解内容版本发布

本项目维护讲解内容版本发布的领域约定、角色边界与样例数据，并提供可运行的 Python 后端：
内容编辑组维护**主题、受众级别、内容片段、引用来源**，经**多级审核签署**后发布；发布瞬间
冻结全部依赖并生成带 `sha256` 内容指纹的稳定版本。撤回、片段替代、局部更正与并发签署形成
清晰可追溯的版本链；已排场次按规则选择版本，已完成场次永久保留原始依据。

## 领域规则

- **内容依赖图**：提纲由有序片段组成；每个片段修订携带引用来源（含定位信息）。发布时对
  提纲依赖闭包做确定性快照（`snapshot_json`），快照不再随后续编辑改变，内容指纹排除时间戳，
  保证“同内容同指纹”。
- **多级审核签署**：签署链固定为 `内容审核 → 场馆负责人`，不得越级、不得重复；
  数据库 `UNIQUE(release_id, role)` 保证并发同角色签署只有一人成功，并支持
  `expected_row_version` 乐观并发控制。两级签齐后版本进入「已确认」。
- **版本链**：语义化版本号 `major.minor.patch`，按「主题 + 受众级别」独立编号。
  首版 `1.0.0`；局部更正（仅标题/正文）升补丁号；片段替代/提纲调整升次版本号；
  馆藏大改版显式升主版本号。每版记录 `parent_id`，可沿链回溯。
- **撤回与替代**：撤回有未来场次使用的版本需 `force` 确认，并给出影响场次；撤回后版本不可
  签署、不可被新场次选用。片段替代产生新 code 并保留 `replaces_code` 世系链。
- **场次版本冻结**：
  - 已排（未完成）场次按规则动态解析：**开讲时间之前最新「已确认」且未撤回/归档的版本**；
  - 场次完成瞬间把实际依据（版本号 + 内容指纹）固化到服务记录，此后任何纠错、撤回、归档
    都不可改动它，也不能重复完成——杜绝紧急纠错误改已完成服务记录。
- **影响范围分析**：可对任意版本比较差异（新增/删除/变更片段、来源变化、替代关系）并列出
  受影响的未来场次（将采用该版本 / 将回退到其他版本 / 将无可用版本）以及沿用该版本的
  已完成场次。

状态术语对应契约：发布版本 `待核验 / 已确认 / 已撤回 / 已归档`；
场次 `已排期 / 执行中 / 已完成`。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/guide_versioning/`：后端实现（store / service / api，纯标准库）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与领域规则回归测试（含并发签署、HTTP 端到端）。

## 运行

```bash
PYTHONPATH=src python3 -m guide_versioning --host 127.0.0.1 --port 8080 \
  --db guide_versioning.sqlite3
```

零第三方依赖，仅需 Python ≥ 3.11，数据存于 SQLite。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/themes` `/api/audiences` `/api/sources` | 主题 / 受众级别 / 引用来源 |
| POST | `/api/fragments` | 建片段（可带 `replaces_code` 替代、`source_refs` 引用） |
| POST | `/api/fragments/{code}/corrections` | 局部更正（仅 title/body，追加修订号） |
| POST/PUT | `/api/outlines` | 设置提纲片段顺序（内容依赖图） |
| POST | `/api/releases` | 发布冻结；`change_kind=correction/replacement/major` |
| POST | `/api/releases/{id}/sign` | 按链签署；可带 `expected_row_version` |
| POST | `/api/releases/{id}/withdraw` | 撤回（有未来场次时需 `force:true`） |
| GET | `/api/releases/{id}/chain` | 版本链（最新版沿 parent 回溯） |
| GET | `/api/releases/{a}/diff?other={b}` | 版本差异 |
| GET | `/api/releases/{id}/affected` | 受影响的未来场次与已完成场次 |
| POST | `/api/sessions` | 排期（校验当时存在可选已确认版本） |
| POST | `/api/sessions/{id}/start` `/complete` | 开讲 / 完成（完成即冻结依据） |
| GET | `/api/sessions` `/api/sessions/{id}` | 场次列表（含动态解析版本）/ 详情 |

错误统一返回 `{"error": {"code", "message"}}`，状态码 404/409/422 区分不存在、并发冲突与
规则违反（如撤回需确认、越级签署、无可用版本排期）。

## 验证

```bash
python3 -m unittest discover -s tests -v          # 17 项测试
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
