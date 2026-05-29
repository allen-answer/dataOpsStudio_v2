# DataOpsStudio 2.0 — dev task runner
# 详细见各 target 注释。三形态部署/打包等专项命令后续 Step 加入。

# Make 默认 /bin/sh,不读用户 PATH;uv 装在 ~/.local/bin(brew/curl 安装均如此),
# 需显式加进 PATH 让子进程找到。
SHELL := /bin/bash
export PATH := $(HOME)/.local/bin:$(PATH)

.PHONY: help install sync lock fmt lint typecheck test test-unit test-contract test-e2e \
        check-redlines pg-up pg-down pg-down-clean pg-psql \
        alembic-up alembic-down alembic-current alembic-history alembic-check clean

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
check-redlines:  ## 跑全部红线检查(R1-R9 本地版,CI 会再 enforce 一遍)
	@echo "→ R1/R3 import 越界:ruff TID251 banned-api"
	$(UV) run ruff check app tests tools
	@echo "→ ruff format check(防漏 format 进 CI)"
	$(UV) run ruff format --check app tests tools
	@echo "→ R2/R6 字段/调用模式:ast-grep(dev 组依赖,severity=error 自动 exit 1)"
	$(UV) run sg scan --config tools/lint/sgconfig.yml
	@echo "→ R8 配置文件明文 secret:gitleaks"
	@command -v gitleaks >/dev/null 2>&1 && gitleaks detect --no-banner --redact -c .gitleaks.toml || echo "  (gitleaks 未本地装,CI 会跑)"
	@echo "→ R4/R5/R6/R7 Python 层:pytest tests/unit/test_redlines.py + test_redaction.py + test_models.py"
	$(UV) run pytest tests/unit/test_redlines.py tests/unit/test_redaction.py tests/unit/test_models.py -q

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
# PG app password 统一来自 config/secrets/pg_app_password。
# 先运行: uv run python -m app.launcher bootstrap init
PG_APP_PASSWORD_FILE ?= config/secrets/pg_app_password

pg-up:  ## 起元数据 PG 容器(端口 127.0.0.1:15432,与 1.x 隔离)
	@test -r "$(PG_APP_PASSWORD_FILE)" || { \
		echo "$(PG_APP_PASSWORD_FILE) 不存在。请先运行:"; \
		echo "  uv run python -m app.launcher bootstrap init"; \
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
# DATABASE_URL 不含密码(避免 base64 特殊字符破坏 URL 解析);
# 密码走 PGPASSWORD env —— psycopg3 用 libpq,原生读 PGHOST/PGPORT/PGUSER/PGPASSWORD。
# R8:密码自始至终是 env 变量,不进任何文件。
DEV_DB_URL = postgresql+psycopg://dataops@127.0.0.1:15432/dataops

alembic-up:  ## 应用所有迁移到 head
	@test -r "$(PG_APP_PASSWORD_FILE)" || { echo "$(PG_APP_PASSWORD_FILE) 不存在"; exit 1; }
	PGPASSWORD="$$(cat "$(PG_APP_PASSWORD_FILE)")" \
		DATAOPS_DATABASE_URL="$(DEV_DB_URL)" \
		$(UV) run alembic upgrade head

alembic-down:  ## 回退一格
	@test -r "$(PG_APP_PASSWORD_FILE)" || { echo "$(PG_APP_PASSWORD_FILE) 不存在"; exit 1; }
	PGPASSWORD="$$(cat "$(PG_APP_PASSWORD_FILE)")" \
		DATAOPS_DATABASE_URL="$(DEV_DB_URL)" \
		$(UV) run alembic downgrade -1

alembic-current:  ## 显示当前 migration 状态
	@test -r "$(PG_APP_PASSWORD_FILE)" || { echo "$(PG_APP_PASSWORD_FILE) 不存在"; exit 1; }
	PGPASSWORD="$$(cat "$(PG_APP_PASSWORD_FILE)")" \
		DATAOPS_DATABASE_URL="$(DEV_DB_URL)" \
		$(UV) run alembic current

alembic-history:  ## migration 历史(不连库)
	$(UV) run alembic history

alembic-check:  ## 校验 metadata 与当前 DB schema 是否一致(无 drift)
	@test -r "$(PG_APP_PASSWORD_FILE)" || { echo "$(PG_APP_PASSWORD_FILE) 不存在"; exit 1; }
	PGPASSWORD="$$(cat "$(PG_APP_PASSWORD_FILE)")" \
		DATAOPS_DATABASE_URL="$(DEV_DB_URL)" \
		$(UV) run alembic check

# ─── 清理 ───
clean:  ## 清理缓存
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
