# DataOpsStudio 2.0 — dev task runner
# 详细见各 target 注释。三形态部署/打包等专项命令后续 Step 加入。

.PHONY: help install sync lock fmt lint typecheck test test-unit test-contract test-e2e \
        check-redlines pg-up pg-down pg-psql alembic-init alembic-up alembic-down clean

UV ?= uv

help:  ## 列出所有可用 target
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ─── 依赖 ───
install: sync  ## 同步依赖到 .venv(等价于 sync)
sync:  ## uv sync — 按 uv.lock 同步 .venv(包含 dev/test 组)
	$(UV) sync --all-groups

lock:  ## uv lock — 重新解析依赖,刷新 uv.lock
	$(UV) lock

# ─── 代码质量 ───
fmt:  ## ruff format
	$(UV) run ruff format app tests tools

lint:  ## ruff lint(含 R1/R3 banned-api)
	$(UV) run ruff check app tests tools

typecheck:  ## mypy strict
	$(UV) run mypy app

# ─── 红线检查(分层,见契约 §4) ───
check-redlines:  ## 跑全部红线检查(R1-R9)
	@echo "→ R1/R3 import 越界:ruff TID251 banned-api"
	$(UV) run ruff check app tests
	@echo "→ R2/R6 字段/调用模式:ast-grep"
	@command -v ast-grep >/dev/null 2>&1 && ast-grep scan --config tools/lint/sgconfig.yml || echo "  (ast-grep 未装,跳过 —— Step 1.7 在 CI 强制)"
	@echo "→ R8 配置文件明文 secret:gitleaks"
	@command -v gitleaks >/dev/null 2>&1 && gitleaks detect --no-banner --redact || echo "  (gitleaks 未装,跳过 —— Step 1.7 在 CI 强制)"
	@echo "→ R4/R5/R7 由单测覆盖:见 pytest"

# ─── 测试 ───
test:  ## 跑全部测试
	$(UV) run pytest

test-unit:  ## tests/unit/
	$(UV) run pytest tests/unit

test-contract:  ## tests/contract/(默认 skip,Codex 实现后启用)
	$(UV) run pytest tests/contract

test-e2e:  ## tests/e2e/(默认 xfail,2.0.0 骨架完成才转绿)
	$(UV) run pytest tests/e2e

# ─── 元数据 PG(dev) ───
# 启动前需要先 export POSTGRES_DEV_PASSWORD=$(openssl rand -base64 24)
# R8:不写明文密码到任何配置/脚本,docker-compose 用 env var,失败有提示
pg-up:  ## 起元数据 PG 容器(端口 127.0.0.1:15432,与 1.x 隔离)
	@test -n "$$POSTGRES_DEV_PASSWORD" || { \
		echo "POSTGRES_DEV_PASSWORD 未设置。请运行:"; \
		echo "  export POSTGRES_DEV_PASSWORD=\$$(openssl rand -base64 24)"; \
		exit 1; \
	}
	docker compose -f docker/dev-pg.yml up -d
	@echo "等待 PG ready..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		docker exec dataops-v2-pg pg_isready -U dataops -d dataops -q && exit 0; \
		sleep 1; \
	done; \
	echo "PG 启动超时"; exit 1

pg-down:  ## 停元数据 PG 容器(保留数据 volume)
	docker compose -f docker/dev-pg.yml down

pg-down-clean:  ## 停 + 删数据 volume(危险:dev 数据清零)
	docker compose -f docker/dev-pg.yml down -v

pg-psql:  ## 进入元数据 PG psql
	docker exec -it dataops-v2-pg psql -U dataops -d dataops

# ─── Alembic ───
# DATAOPS_DATABASE_URL 拼接,密码取自 POSTGRES_DEV_PASSWORD env(R8)
DEV_DB_URL = postgresql+psycopg://dataops:$(POSTGRES_DEV_PASSWORD)@127.0.0.1:15432/dataops

alembic-up:  ## 应用所有迁移到 head
	@test -n "$$POSTGRES_DEV_PASSWORD" || { echo "POSTGRES_DEV_PASSWORD 未设置"; exit 1; }
	DATAOPS_DATABASE_URL="$(DEV_DB_URL)" $(UV) run alembic upgrade head

alembic-down:  ## 回退一格
	@test -n "$$POSTGRES_DEV_PASSWORD" || { echo "POSTGRES_DEV_PASSWORD 未设置"; exit 1; }
	DATAOPS_DATABASE_URL="$(DEV_DB_URL)" $(UV) run alembic downgrade -1

alembic-current:  ## 显示当前 migration 状态
	@test -n "$$POSTGRES_DEV_PASSWORD" || { echo "POSTGRES_DEV_PASSWORD 未设置"; exit 1; }
	DATAOPS_DATABASE_URL="$(DEV_DB_URL)" $(UV) run alembic current

alembic-history:  ## migration 历史
	$(UV) run alembic history

alembic-check:  ## 校验 metadata 与当前 DB schema 是否一致
	@test -n "$$POSTGRES_DEV_PASSWORD" || { echo "POSTGRES_DEV_PASSWORD 未设置"; exit 1; }
	DATAOPS_DATABASE_URL="$(DEV_DB_URL)" $(UV) run alembic check

# ─── 清理 ───
clean:  ## 清理缓存
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
