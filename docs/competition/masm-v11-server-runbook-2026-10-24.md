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

## 7. 四项探针（缺一不可，全绿才允许动额度）

### 7.1 abstention 9 用例

```bash
docker exec -i masm-v11-api-1 python - --timeout 240 \
  < /root/AgentMemoryChallenge/scripts/abstention_path_probe.py 2>&1 | tail -10
docker exec -i masm-v11-api-1 python - --timeout 240 \
  < /root/AgentMemoryChallenge/scripts/abstention_path_probe.py 2>&1 | grep positive_relation
```

判据：9 行全部 `"passed": true`；**特别是 `positive_relation` 必须 `returned_count >= 1`**（这是预检唯一可能误伤的正例）。

### 7.2 跨会话桥接（必须 2 会话）

```bash
docker exec -i masm-v11-api-1 python - --timeout 240 \
  < /root/AgentMemoryChallenge/scripts/multi_session_bridge_probe.py 2>&1 | tail -5
```

连跑两次防抖，两次都要 `bridged_question` 覆盖 2 个会话。

### 7.3 多会话路径（`disconnected_chain` 必须 0）

```bash
docker exec -i masm-v11-api-1 python - --timeout 240 \
  < /root/AgentMemoryChallenge/scripts/multi_session_path_probe.py 2>&1 | tail -4
```

该用例有抖动，失败先重跑一次再下结论。

### 7.4 26 例 compare

```bash
cd /root/AgentMemoryChallenge && rm -f /tmp/cmp.done /tmp/cmp.jsonl
nohup bash -c 'docker exec -i masm-v11-api-1 python - --compare \
  < /root/AgentMemoryChallenge/scripts/evidence_graph_probe.py \
  > /tmp/cmp.jsonl 2>/tmp/cmp.err; echo done > /tmp/cmp.done' >/dev/null 2>&1 &
for i in $(seq 1 30); do [ -f /tmp/cmp.done ] && break; sleep 5; done
grep -c '"subset_ok": true' /tmp/cmp.jsonl
grep '"subset_ok": false' /tmp/cmp.jsonl | head -3
grep compare_summary /tmp/cmp.jsonl
```

判据：`subset_ok` 26/26、无 `false` 行、`forbidden_total <= 5`。

## 8. 观测预检是否命中（决定要不要花额度）

```bash
# 命中了多少次
docker logs --since 60m masm-v11-api-1 2>&1 | grep -c '"refusal_gate":true'
# 命中的行（看 candidate_count 与 returned_count）
docker logs --since 60m masm-v11-api-1 2>&1 | grep '"refusal_gate":true' | tail -5
# 对照：走选择器的空响应长什么样
docker logs --since 60m masm-v11-api-1 2>&1 | grep '"selector_abstained":true' | tail -3
```

判定：

- `refusal_gate=true` **只**出现在拒答形状（问一个库里不存在的关系人）→ 预检按设计工作，可以花额度；
- `refusal_gate=true` 出现在本该有答案的 direct/atomic 问题上 → **立即回滚**，那种情况下总分只会 ≤ 38.46；
- 一条都没有 → 预检在这批题上没机会触发，本改动等价于没改，仍需回滚后再研究。

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
