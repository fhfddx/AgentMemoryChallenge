# MASM v1.1 交接文档（2026-10-21 第二会话结束）

## 0. 一句话状态

线上 `masm-v11-final-candidate:a7b58d1`（healthy），官方最好成绩 **38.46**（`Retrieval 100` + `abstention 22.22` + `multi_session 0`），**剩余 Smoke 19 次**。目标 40 分的缺口是"再多命中 1 题 abstention 或 1 题 multi-session"。

## 1. 目标与权重

```
total = (3 × retrieval + 9 × abstention + 1 × relational) / 13
retrieval = (2 × atomic + direct) / 3
```

`abstention` 9 题制，1 题 = 11.11。到 40 需要：

- `Retrieval 100` + `abstention 22.22` + `multi_session ≥ 20` → 40.00，或
- `Retrieval 100` + `abstention 33.33` → 46.15。

**注意**：multi-session 单独满分也只有 38.46（已多次核算）。

## 2. 官方运行记录（本会话实测）

| 构建 | 关键改动 | R | direct | atomic | multi | abst | 总分 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `a01b708` | A 关系前缀 + B 槽位模板 + 桥接 | 100 | 100 | 100 | 0 | **22.22** | **38.46** |
| `162f5bc` | 同上 + 补召回 tokenizer 修复 | 100 | 100 | 100 | 0 | **22.22** | **38.46** |
| `a7b58d1` | 同上 + 关系形状扩宽（4 形状） | 100 | 100 | 100 | 0 | **22.22** | **38.46** |
| `96854e3` | + 值类型缺失判据 C | 100 | 100 | 100 | 0 | 11.11 | 30.77 |
| `a7cc9a8` | C 收紧 | 100 | 100 | 100 | 0 | **0.00** | 23.08 |
| `2ed70a9`/`07e8ee0` | + 最近邻追加 | 66.67 | **0** | 100 | 0 | 22.22 | 30.77 |
| `7c64c19` | + 召回加宽 3×→6× | 探针失败（`relation_absent` 返回 1） | | | | | 未跑 |
| `2224ea3`/`798f772` | 拒答前置到 planner 之前 | 100 | 100 | 100 | 0 | 11.11 | 30.77 |
| `e6f65c6` | + 空池显式拒答 | 100 | 100 | 100 | 0 | 11.11 | 30.77 |

## 3. 已验证的硬事实（不要再重复试错）

1. **direct 与 abstention 是 1:1 弹性**：五种不同"实体缺失"拒答形态都得到 30.77（`400/13`）。
2. **唯一不伤 direct 的拒答判据是关系前缀缺人**（`my sister Maya`、`X's sister`、`the sister named X`、`sister called X`）。因为用户从不用这种形式指代自己。
3. **`direct_recall` 对多余证据零容忍**：追加一条即 direct 归零（实测 `07e8ee0`）。⇒ 任何"追加证据"路线作废。
4. **改选择器内部 ⇒ 官方 abstention 掉一题**：`a7b58d1` 三次 22.22，其后五次改动（含空池显式化）都是 11.11。原因未明，但**规律稳定**。
5. **召回加宽会拖慢 planner** → 回退增多 → 拒答被跳过（`7c64c19` 探针失败）。
6. **拒答的真实来源是"候选池为空"**：`--case refusal` 曾显示 `graph_candidate_count=0`、`vetoed=false`，说明 A 判据在有候选时才生效，池为空时走的是早退分支。

## 4. 下一步：选择器之外加拒答预检（未实现，补丁已定稿）

**原则**：`a7b58d1` 的选择器逻辑一个字节都不改，只在它之前加一条"返回空结果"的路径。最坏 38.46，最好 46.15。

### 4.1 `src/masm/services/search_service.py`

```python
# 顶部
from collections.abc import Callable

# __init__ 参数（source_context 之后）
        refusal_gate: Callable[[str, str], bool] | None = None,

# __init__ 赋值（self._source_context = source_context 之后）
        self._refusal_gate = refusal_gate

# search() 内，selector_latency_ms = 0.0 之后、if self._graph_selector is not None: 之前
        if self._refusal_gate is not None and isinstance(request.query, str):
            if self._refusal_gate(request.user_id, request.query):
                return SearchResponse(data=[])
```

### 4.2 `src/masm/runtime.py`

```python
from masm.retrieval.evidence_graph_rules import relation_capitalized_names

def _relation_absent_gate(repository: MemoryRepository) -> Callable[[str, str], bool]:
    """选择器之外的拒答预检：问题点名了全库不存在的关系前缀人。"""

    def gate(user_id: str, question: str) -> bool:
        names = relation_capitalized_names(question)
        if not names:
            return False
        present = repository.present_tokens(user_id, sorted(names))
        return bool(names - present)

    return gate
```

构造 `SearchService(...)` 时加 `refusal_gate=_relation_absent_gate(repository),`。

### 4.3 测试

`tests/unit/services/test_search_service.py`：gate 返回 True 时 `response.data == []`；返回 False 时行为不变。

## 5. 验证协议（每次动额度前必须全绿）

```bash
# 1) abstention 探针（9 用例：5 负例必须 0、2 正例必须命中）
docker exec -i masm-v11-api-1 python - --timeout 240 < /root/AgentMemoryChallenge/scripts/abstention_path_probe.py 2>&1 | tail -10

# 2) 桥接探针（bridged_question 必须 2 会话；连跑两次防抖）
docker exec -i masm-v11-api-1 python - --timeout 240 < /root/AgentMemoryChallenge/scripts/multi_session_bridge_probe.py 2>&1 | tail -5

# 3) 多会话回归（disconnected_chain 必须 0；该用例有抖动，失败时重跑一次）
docker exec -i masm-v11-api-1 python - --timeout 240 < /root/AgentMemoryChallenge/scripts/multi_session_path_probe.py 2>&1 | tail -4

# 4) 26 例 compare（必须 26/26 subset_ok、无 false 行、forbidden_total ≤ 5）
cd /root/AgentMemoryChallenge && rm -f /tmp/cmp.done /tmp/cmp.jsonl && nohup bash -c 'docker exec -i masm-v11-api-1 python - --compare < /root/AgentMemoryChallenge/scripts/evidence_graph_probe.py > /tmp/cmp.jsonl 2>/tmp/cmp.err; echo done > /tmp/cmp.done' >/dev/null 2>&1 &
for i in $(seq 1 30); do [ -f /tmp/cmp.done ] && break; sleep 5; done; grep -c '"subset_ok": true' /tmp/cmp.jsonl; grep '"subset_ok": false' /tmp/cmp.jsonl | head -3; grep compare_summary /tmp/cmp.jsonl
```

## 6. 服务器操作约定（重要）

- 部署：`docker build -t masm-v11-final-candidate:<sha> .` + 唯一的 override 文件名（`/opt/masm-v11/config/image.override.<sha>-<suffix>.yml`，**不要复用名字**），`up -d --no-deps --force-recreate api`；
- **不要重启或改动 `masm-v11-postgres-1`**（数据在其中）；
- 长命令会断线：单块 ≤60 秒；长任务 `nohup` + 轮询；重管道换 `grep`/`cut`；
- 探针脚本从宿主流入（镜像里没有 `scripts/`）：`docker exec -i masm-v11-api-1 python - --timeout 240 < /root/AgentMemoryChallenge/scripts/<probe>.py`；
- 推送需代理：`git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin codex/masm-v11-evidence-graph`。

## 7. 本机开发约定

- **禁止用 PowerShell 字符串批量替换改 Python 源码**（本会话三次把文件写坏，均为 `\`r\`n\`` 落入源码）；改用 `edit` 工具逐处修改，每次改完立刻 `ruff check` + `mypy`；
- 路径：工作树 `C:\Users\23952\.codex\worktrees\masm-v11-multihop\AgentMemoryChallenge`，虚拟环境 `.venv\Scripts\python.exe`；
- 全量测试需先起测试库：`docker run -d --rm --name masm-v11-evidence-graph-pg --tmpfs /var/lib/postgresql/data -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_USER=postgres -e POSTGRES_DB=masm_test -p 127.0.0.1:5433:5432 pgvector/pgvector:pg16`，`DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5433/masm_test`，`alembic upgrade head`，跑完 `docker stop`；
- 源码改动后必须刷新 pin：`scripts/evidence_graph_server_gate.py` 的 `source_digest`（用 gate 的 `_git_package_digest` 计算），否则 `test_default_gate_pins_the_platform_independent_package_digest` 会失败。

## 8. 关键文件

| 文件 | 作用 |
| --- | --- |
| `src/masm/retrieval/evidence_graph.py` | 选择器、planner、R11 桥接、R10 拒答、诊断 |
| `src/masm/retrieval/evidence_graph_rules.py` | `name_tokens` / `relation_capitalized_names` / `slot_template_conflict` / `asked_value_kind`（已弃用） |
| `src/masm/services/search_service.py` | 检索 → 选择 → 打包；**待加 refusal_gate** |
| `src/masm/runtime.py` | 组件装配（presence / frames / retrieve_more 都在这里注入） |
| `scripts/abstention_path_probe.py` | 9 用例拒答探针（含 `relation_absent`、`possessive_absent`） |
| `scripts/multi_session_bridge_probe.py` | 跨会话桥接探针（2 会话判据） |
| `scripts/graph_diagnostics_probe.py` | 进程内捕获搜索诊断（`--case lexical|semantic|refusal`） |
| `docs/competition/competition-final-retrospective-2026-10-21.md` | 总复盘（§1–§11，含本会话全部实测） |

## 9. 未采用但已验证的事实（避免重复研究）

- Qwen3-Rerank 混合排序：`forbidden` 4→5，`disconnected_chain` 返回 1 → 否决；
- 值类型缺失判据（金额/日期/数量/时长/颜色）：官方 abstention 11.11 → 0.00 → 否决；
- 标识符缺失判据：official direct 0 → 否决；
- 最近邻追加：official direct 0 → 否决；
- 召回加宽 6×：探针 `relation_absent` 失败 → 否决；
- 拒答前置到 planner 之前：官方 abstention 22.22 → 11.11（2 次）→ 否决。
