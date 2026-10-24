# MASM v1.1 服务器 runbook：拒答预检（2026-10-24）

本文件是交接文档 §10.6/§10.7 的可执行版，改动的源码见 §10.2。**顺序不可换**：门禁与探针没全绿之前，不要动官方 Smoke 额度。

适用前提（和上一会话一致）：

- 仓库：`/root/AgentMemoryChallenge`（宿主机），容器 `masm-v11-api-1` / `masm-v11-postgres-1`，compose 项目 `masm-v11`；
- **绝不重启、不改动 `masm-v11-postgres-1`**；
- 长命令会断线：单块 ≤ 60 秒，长任务 `nohup` + 轮询；
- 探针脚本只存在于宿主机，靠 stdin 流进容器（镜像里没有 `scripts/`）。

---

## 1. 同步到已推送的提交

```bash
cd /root/AgentMemoryChallenge
git status --porcelain          # 有输出 → 先停手，别 reset
git fetch origin
git checkout codex/masm-v11-evidence-graph
git merge --ff-only origin/codex/masm-v11-evidence-graph
SHA=$(git rev-parse --short=7 HEAD); echo "SHA=$SHA"
git log --oneline -5
```

期望 `git status --porcelain` 为空（门禁会检查工作树是否等于 HEAD）。

## 2. 构建镜像

```bash
cd /root/AgentMemoryChallenge
SHA=$(git rev-parse --short=7 HEAD)
docker build -t masm-v11-final-candidate:$SHA .
docker image inspect -f '{{.Id}} created={{.Created}}' masm-v11-final-candidate:$SHA
```

## 3. 写唯一的 override 文件

文件名必须唯一（带 sha），**不要复用旧名字**：

```bash
SHA=$(git -C /root/AgentMemoryChallenge rev-parse --short=7 HEAD)
mkdir -p /opt/masm-v11/config
cat > /opt/masm-v11/config/image.override.$SHA-gate.yml <<YML
services:
  api:
    image: masm-v11-final-candidate:$SHA
YML
cat /opt/masm-v11/config/image.override.$SHA-gate.yml
```

## 4. 重建 api（只动 api，不碰 postgres）

下面的块是**自包含**的：它自己从正在跑的容器里取出 compose 项目名与文件列表，再把新的 override 叠上去。不要凭记忆手写 `-f` 列表。

```bash
cd /opt/masm-v11
SHA=$(git -C /root/AgentMemoryChallenge rev-parse --short=7 HEAD)
PROJECT=$(docker inspect masm-v11-api-1 --format '{{index .Config.Labels "com.docker.compose.project"}}')
COMPOSE_FILES=$(docker inspect masm-v11-api-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
echo "project=$PROJECT"; echo "files=$COMPOSE_FILES"
[ -n "$PROJECT" ] && [ -n "$COMPOSE_FILES" ] || { echo "compose labels missing"; exit 1; }

ARGS=(); IFS=',' read -ra FILES <<< "$COMPOSE_FILES"
for f in "${FILES[@]}"; do ARGS+=(-f "$f"); done
ARGS+=(-f "/opt/masm-v11/config/image.override.$SHA-gate.yml")

# 先干跑一次，确认命令形状
docker compose -p "$PROJECT" "${ARGS[@]}" config --images
# 再重建
docker compose -p "$PROJECT" "${ARGS[@]}" up -d --no-deps --force-recreate api
docker inspect -f '{{.Config.Image}}' masm-v11-api-1
```

等健康：

```bash
for i in $(seq 1 40); do
  s=$(docker inspect -f '{{.State.Health.Status}}' masm-v11-api-1)
  [ "$s" = "healthy" ] && { echo "healthy"; break; }
  sleep 3
done
docker inspect -f '{{.State.Health.Status}}' masm-v11-api-1
```

## 5. 门禁自检（源码摘要 / 工作树 / 容器字节 / 运行时设置）

```bash
cd /root/AgentMemoryChallenge
python3 --version || python --version      # 门禁只用标准库，不需要 venv
python3 scripts/evidence_graph_server_gate.py --container masm-v11-api-1 --strict-exit
echo "gate_exit=$?"
```

期望这一屏（字段名以脚本实际输出为准）：

```
checkout_clean=yes
probe_blob_match=yes
probe_worktree_match=yes
source_manifest_match=yes
source_match=yes
MASM_RUNTIME_PROFILE=official-masm
runtime_settings=valid
candidate_modules=present
verification_exit_code=0
gate_exit=0
```

再加两条人工确认：

- 三个 provider 变量都必须是 `=set`（`MASM_LLM_API_KEY` / `MASM_EMBEDDING_API_KEY` / `MASM_API_KEYS` 一类），缺一个就是退出码 2；
- `graph_flag` 记下它的值。预检**不依赖** graph 开关（只看 `selector_enabled`），但线上图路径应当是开的。

它比对的是 `620669f4…`（已提交字节），与工作树 CRLF/LF 无关。

## 6. 确认新字段真的在镜像里

```bash
docker exec -i masm-v11-api-1 python - <<'PY'
import json, logging
from masm.retrieval.diagnostics import SearchDiagnostics, emit_search_diagnostics
logging.basicConfig(level=logging.INFO, format="%(message)s")
emit_search_diagnostics(SearchDiagnostics(
    request_tag="0" * 32, runtime_profile="official-masm", candidate_count=0,
    dedup_count=0, returned_count=0, response_bytes=0, latency_ms=0.0,
    status_code=200, channel_counts={}, refusal_gate=True,
))
PY
```

期望输出里同时有 `"refusal_gate": true` 和 `"graph_planner_latency_ms": 0.0`；出现 `KeyError`/`Unknown field` 说明镜像不是新构建的。

## 7. 四项探针 + 观测：一条命令

```bash
cd /root/AgentMemoryChallenge
git pull
python3 scripts/server_verify_refusal_gate.py
```

默认跑 `gate field bridge path compare observe`（abstention 探针上一轮已绿，默认不重复跑）。要单独跑：

```bash
python3 scripts/server_verify_refusal_gate.py abst        # 只跑 abstention 探针
python3 scripts/server_verify_refusal_gate.py all         # 七个阶段全跑
python3 scripts/server_verify_refusal_gate.py --since 6h  # 放大 observe 的日志窗口
python3 scripts/server_verify_refusal_gate.py --container masm-v11-api-1 gate
```

输出每阶段一行、最后一行是结论，退出码 0/1：

```
container=masm-v11-api-1 stages=gate,field,bridge,path,compare,observe repo=/root/AgentMemoryChallenge
[gate   ] PASS exit=0 providers=4/4 graph_flag=enabled
[field  ] PASS refusal_gate_field=present
[bridge1] PASS exit=0 bridged_sessions=2 bridged_returned=1 fail=0 failed_cases=none
[bridge2] PASS exit=0 bridged_sessions=2 bridged_returned=1 fail=0 failed_cases=none
[path   ] PASS exit=0 disconnected_returned=0 connected_returned=3 fail=0 failed_cases=none
[compare] PASS exit=0 subset_ok=26/26 false=0 forbidden_total=0 false_cases=none
[observe] INFO searches=87 gate_hits=3 gate_hits_with_evidence=0 selector_abstained=11
RESULT=PASS
```

红色行直接印出越界字段（`bridged_sessions=1`、`forbidden_total=6`、`positive_relation_returned=0`…），不用再翻日志。

脚本内置的判据（与交接文档 §5 一致）：

| 阶段 | 通过条件 |
| --- | --- |
| `gate` | `verification_exit_code=0`；`checkout_clean` / `probe_*` / `source_*` 全 `yes`；`official-masm` + `valid` + `present`；四个 provider 全 `set` |
| `field` | 走生产同一条脱敏链路后，日志行里仍有 `refusal_gate` 字段 |
| `abst` | 所有行 `passed=true`，且 `positive_relation.returned_count >= 1` |
| `bridge1/2` | `bridged_question.distinct_session_count = 2` 且 `passed`，连跑两次都过 |
| `path` | `disconnected_chain.returned_count = 0` 且 `connected_chain.passed` |
| `compare` | `subset_ok` 26/26、无 `false` 行、`forbidden_total <= 5` |
| `observe` | 只观测；`gate_hits_with_evidence` 必须为 0（命中却带回证据＝预检逻辑被破坏） |

`compare` 的唯一例外（已实测）：在**字节完全相同**的选择器上，同一条探针两次运行分别得到
26/26 与 25/26，翻转的永远是 `elliptical_confirmation_negative`（`baseline=2, merged=1,
forbidden=False`：v7 有时保留含 "Porto" 的引用，加法式把它正确剔除，于是"超集"不成立）。
这种单例翻转且 `forbidden=False` 时脚本输出 `WARN` 而不是 `FAIL`；任何**正例**掉证据仍然是
`FAIL`。

长探针用独立会话启动、脚本从宿主流进容器，SSH 断线不会打断；重连后重跑同一条命令即可。

`field` 与 `observe` 都依赖 `refusal_gate` 出现在**脱敏后**的日志里：该键已加入
`ALLOWED_LOG_FIELDS`，因此任何包含这条改动的提交都必须重新构建镜像，否则 `observe`
会报 `refusal_gate_field_missing`（这是唯一会因此变红的阶段，探针本身不受影响）。

## 8. 观测口径（`observe` 阶段已包含，这里是对照用）

```bash
python3 scripts/server_verify_refusal_gate.py observe                                  # 一行摘要
docker logs --since 60m masm-v11-api-1 2>&1 | grep '"refusal_gate":true' | tail -3    # 逐行
docker logs --since 60m masm-v11-api-1 2>&1 | grep '"selector_abstained":true' | tail -3
```

判定：

- `refusal_gate=true` **只**出现在拒答形状（问库里不存在的关系人）→ 预检按设计工作，可以花额度；
- `refusal_gate=true` 出现在本该有答案的 direct/atomic 上 → **立即回滚**，那种情况下总分只会 ≤ 38.46；
- `gate_hits=0` → 这批请求里预检没机会触发，本改动等价于没改，回滚后再研究。

## 8.1 取一次官方运行的指纹（`profile`）

```bash
python3 scripts/server_verify_refusal_gate.py profile --since 6h
```

窗口要足够大以覆盖那次官方运行。输出：

```
[profile] INFO searches=… empty=… gate_hits=… selector_abstained=… graph_enabled=…
[profile] INFO candidate_count={…}
[profile] INFO returned_count={…}
[profile] INFO graph_state={…}
[profile] INFO cluster1 start=HH:MM:SS searches=… empty=… gate_hits=… selector_abstained=… candidates={…}
[profile] INFO cluster2 start=HH:MM:SS searches=… empty=… gate_hits=… selector_abstained=… candidates={…}
```

`cluster*` 行按 ≥150 秒的间隔把日志切成簇：**探针流量与官方运行各成一簇**，不要把探针的
`gate_hits` 记到官方头上。读法：

- 官方那簇的 `gate_hits` 若为 0，说明官方题目里没有关系前缀形状（与 §10.9 的结论一致）；
- `empty` 等于「被拒答的题数」。若 `empty` 恰好等于 abstention 的命中数（如 2/9），说明失败
  的拒答题都**返回了证据**；若明显更大，说明另有题目（通常是 multi-session）在返回空；
- `candidates` 里大量 `<=0` = 检索层没给候选；集中在 `<=16` = 有近失配候选但被接受。

## 8.2 逐题明细（`detail`）

```bash
python3 scripts/server_verify_refusal_gate.py detail --since 6h
```

每个 `/search` 一行，只有计数与固定枚举、没有文本：

```
[detail ] INFO #7 16:41:03 cand=0 dedup=0 returned=0 gate=False sel_abst=True graph=insufficient ch={'lexical': 3, 'text_vector': 9}
```

用途：区分两种「空响应」——

| 形态 | 含义 |
| --- | --- |
| `ch` 非零、`cand=0` | 召回到了东西，但被相关性闸门全部滤掉（可考虑空池兜底） |
| `ch` 全零、`cand=0` | 检索层压根没召回（要换召回通道，不是调闸门） |

这两类的修法完全相反，所以在动判据之前必须先看一眼明细。

## 8.3 大库拒答探针（小库探针测不到的过答条件）

官方用户库是 110 条文本 + 21 张图；而 `abstention_path_probe` 只播 4 条记忆，相关性闸门会把
它们全部放进池子，因此那道探针在官方过答的情况下依然 10/10。噪声探针把库扩到几十条，用来在
本地复现官方条件（不消耗官方额度）：

```bash
docker exec -i masm-v11-api-1 python - --timeout 600 \
  < /root/AgentMemoryChallenge/scripts/abstention_noise_probe.py 2>&1 | tail -12
```

判据与 `abst` 一致：8 个用例全 `passed: true`、两个正例命中标记、`cleanup.complete=true`。
每个拒答形状都是一行 `"refusal_expected": true`，返回任何证据即该形状失败。

## 9. 回滚

同样自包含；把 `ROLLBACK_SHA` 换成你要回到的那个 tag（上一版最好成绩是 `a7b58d1`）：

```bash
cd /opt/masm-v11
ROLLBACK_SHA=a7b58d1
cat > /opt/masm-v11/config/image.override.$ROLLBACK_SHA-rollback.yml <<YML
services:
  api:
    image: masm-v11-final-candidate:ROLLBACK_TAG
YML
sed -i "s/ROLLBACK_TAG/$ROLLBACK_SHA/" /opt/masm-v11/config/image.override.$ROLLBACK_SHA-rollback.yml

PROJECT=$(docker inspect masm-v11-api-1 --format '{{index .Config.Labels "com.docker.compose.project"}}')
COMPOSE_FILES=$(docker inspect masm-v11-api-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
ARGS=(); IFS=',' read -ra FILES <<< "$COMPOSE_FILES"
for f in "${FILES[@]}"; do ARGS+=(-f "$f"); done
ARGS+=(-f "/opt/masm-v11/config/image.override.$ROLLBACK_SHA-rollback.yml")
docker compose -p "$PROJECT" "${ARGS[@]}" up -d --no-deps --force-recreate api
docker inspect -f '{{.Config.Image}}' masm-v11-api-1
```

回滚后立刻重跑 §7.1 的 `positive_relation` 与 §6 的字段检查（后者应报 `KeyError`），确认确实回到了旧镜像。

## 10. 本机已完成的验证（不要重复）

本地 `ruff` / `mypy src` / `pytest -q` 全绿：**1048 passed, 1 skipped（POSIX-only）**，测试库 `127.0.0.1:5433/masm_test`、alembic `0005 head`。源码只动 4 个文件（+41/−2），`evidence_graph.py` 与 `evidence_graph_rules.py` 一个字节未改。
