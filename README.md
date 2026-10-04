# 讲解内容版本发布

本项目维护讲解内容版本发布的领域约定、角色边界与样例数据，并提供可运行的 Python 后端服务（仅标准库），供接口和自动化验证统一使用。当前契约覆盖文博中心运营员、志愿者、监护人、场馆负责人，并明确内容依赖图、多级审核签署、场次版本冻结、影响范围分析等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/narration/`：后端服务（领域服务 + SQLite 持久化 + HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动后端服务。
- `tests/`：契约、领域规则、并发与 API 回归测试。

## 后端服务

### 领域规则

- **内容片段**：按血缘（lineage）修订。局部更正（`correction`，须填更正说明）与整体替代（`replacement`，须给完整标题正文）产生新修订，旧修订置为 `superseded`；撤回（`withdrawn`）后该血缘终止，不能再修订或加入草稿。
- **多级审核签署**：发布草稿需全部必需角色签署（默认：文博中心运营员、场馆负责人）。签署绑定草稿内容序号，草稿内容变更后旧签署全部失效；同角色重复签署冲突（409），同人同内容重签幂等；并发签署由唯一约束与串行写事务保证结果确定。
- **发布冻结**：发布时把片段与引用来源冻结为不可变快照，生成稳定版本号（`v1`、`v2`…）、内容指纹（SHA-256）与父版本指针，形成版本链。发布后再修改片段不影响已发布版本。
- **撤回**：版本可撤回（须填原因），未来场次不再选用；片段级紧急纠错走「撤回/替代片段 → 重新签署 → 发布新版本」链路。
- **场次版本解析**：已排场次按规则解析——指定版本（pin）优先且未撤回时生效，否则取开场时间前最新已发布且未撤回的版本；已完成场次永久冻结当时依据，后续撤回、更正、新发布均不改写历史服务记录。

### 运行

```bash
python3 tools/run_server.py --db data/narration.db --port 8000
# 或 PYTHONPATH=src python3 -m narration --db :memory: --port 8000
```

### API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/themes` `/api/audiences` `/api/sources` | 建主题、受众级别、引用来源 |
| POST | `/api/fragments` | 建内容片段（可关联来源） |
| POST | `/api/fragments/{id}/revise` | 局部更正 / 替代片段（`mode`） |
| POST | `/api/fragments/{id}/withdraw` | 撤回片段 |
| POST | `/api/drafts` | 建发布草稿（选定片段集合） |
| POST | `/api/drafts/{id}/items` | 替换草稿内容（旧签署失效） |
| POST | `/api/drafts/{id}/signoffs` | 审核签署（并发安全） |
| POST | `/api/drafts/{id}/publish` | 发布：冻结依赖，生成稳定版本 |
| GET | `/api/versions` | 版本链（含父版本与撤回状态） |
| GET | `/api/versions/{id}` | 版本冻结快照与新鲜度分析 |
| POST | `/api/versions/{id}/withdraw` | 撤回版本 |
| GET | `/api/versions/{id}/impact` | 受影响的未来场次 |
| GET | `/api/diff?from=&to=` | 两版本差异（片段增删改、来源变化） |
| POST | `/api/sessions` | 排场次（可指定版本） |
| GET | `/api/sessions/{id}` | 场次解析结果与已完成场次的冻结依据 |
| POST | `/api/sessions/{id}/complete` | 完成场次：冻结实际采用版本 |
| GET | `/api/audit` | 审计事件流 |

错误统一为 `{"error": {"code", "message"}}`，状态码：400 请求体非法 / 404 不存在 / 409 状态冲突 / 422 校验失败。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
