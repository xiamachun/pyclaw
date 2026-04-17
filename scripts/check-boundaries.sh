#!/bin/bash
# ============================================================
# PyClaw 架构边界检查脚本
# Harness Engineering: 把架构文档里的规则变成可执行的自动化检查
# ============================================================

set -e

ERRORS=0

echo "🔍 PyClaw Architecture Boundary Check"
echo "========================================"

# -------------------------------------------------------
# 规则 1: Gateway 不直接导入 sqlite/aiosqlite
# Gateway 必须通过 SessionManager / MemoryManager 访问数据
# -------------------------------------------------------
echo -n "Rule 1: Gateway does not import sqlite directly... "
if grep -rn "import.*sqlite\|from.*sqlite" pyclaw/gateway/ 2>/dev/null | grep -v "__pycache__" | grep -v ".pyc"; then
    echo "❌ FAIL: Gateway directly imports sqlite"
    echo "  WHY: Gateway must access data through SessionManager/MemoryManager"
    echo "  FIX: Replace direct sqlite imports with calls to the appropriate manager"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS"
fi

# -------------------------------------------------------
# 规则 2: Channels 不直接导入 AgentRuntime
# Channels 必须通过 Gateway API (/v1/chat/completions) 调用 AI
# -------------------------------------------------------
echo -n "Rule 2: Channels does not import AgentRuntime directly... "
if grep -rn "from pyclaw.agents.runtime import\|from pyclaw.agents import.*runtime" pyclaw/channels/ 2>/dev/null | grep -v "__pycache__" | grep -v ".pyc"; then
    echo "❌ FAIL: Channels directly imports AgentRuntime"
    echo "  WHY: Channels must call AI through Gateway API (/v1/chat/completions)"
    echo "  FIX: Use httpx to call the Gateway API instead of importing AgentRuntime"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS"
fi

# -------------------------------------------------------
# 规则 3: Skills 不直接注册为可执行工具
# Skills 内容注入到系统提示，不直接调用 ToolRegistry
# -------------------------------------------------------
echo -n "Rule 3: Skills does not import ToolRegistry... "
if grep -rn "from pyclaw.agents.tools import\|from pyclaw.agents import.*tools" pyclaw/skills/ 2>/dev/null | grep -v "__pycache__" | grep -v ".pyc"; then
    echo "❌ FAIL: Skills directly imports ToolRegistry"
    echo "  WHY: Skills content is injected into system prompts, not registered as tools"
    echo "  FIX: Remove ToolRegistry imports from skills module"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS"
fi

# -------------------------------------------------------
# 规则 4: 配置中不出现 0.0.0.0 绑定地址
# Gateway 必须绑定回环地址
# -------------------------------------------------------
echo -n "Rule 4: No 0.0.0.0 bind address in config... "
if grep -rn '"0\.0\.0\.0"' pyclaw/config/ 2>/dev/null | grep -v "__pycache__" | grep -v ".pyc" | grep -v "test" | grep -v "validate"; then
    echo "❌ FAIL: Found 0.0.0.0 bind address in config"
    echo "  WHY: Gateway must bind to loopback address (127.0.0.1) for security"
    echo "  FIX: Change bind address to 127.0.0.1"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS"
fi

# -------------------------------------------------------
# 规则 5: 无调试代码残留
# 禁止 print() 调试语句（logging 除外）
# -------------------------------------------------------
echo -n "Rule 5: No debug print() statements in source... "
PRINT_COUNT=$(grep -rn "^\s*print(" pyclaw/ 2>/dev/null | grep -v "__pycache__" | grep -v ".pyc" | grep -v "test" | grep -v "# noqa" | wc -l | tr -d ' ')
if [ "$PRINT_COUNT" -gt 0 ]; then
    echo "⚠️  WARNING: Found $PRINT_COUNT print() statements"
    echo "  WHY: Use logging instead of print() for production code"
    echo "  FIX: Replace print() with logger.info() / logger.debug()"
    # Warning only, not a hard failure
else
    echo "✅ PASS"
fi

# -------------------------------------------------------
# 规则 6: 安全技能文件存在
# skills/security.md 必须存在
# -------------------------------------------------------
echo -n "Rule 6: Security skill file exists... "
if [ ! -f "skills/security.md" ]; then
    echo "❌ FAIL: skills/security.md not found"
    echo "  WHY: Security skill is mandatory and enforced at startup"
    echo "  FIX: Create skills/security.md with security rules"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS"
fi

# -------------------------------------------------------
# 规则 7: Harness 核心文件存在
# AGENTS.md, PROGRESS.md, Makefile, features.json 必须存在
# -------------------------------------------------------
echo -n "Rule 7: Harness core files exist... "
MISSING=""
for f in AGENTS.md PROGRESS.md Makefile features.json; do
    if [ ! -f "$f" ]; then
        MISSING="$MISSING $f"
    fi
done
if [ -n "$MISSING" ]; then
    echo "❌ FAIL: Missing harness files:$MISSING"
    echo "  WHY: These files are required by Harness Engineering standards"
    echo "  FIX: Create the missing files following the templates in AGENTS.md"
    ERRORS=$((ERRORS + 1))
else
    echo "✅ PASS"
fi

# -------------------------------------------------------
# 总结
# -------------------------------------------------------
echo ""
echo "========================================"
if [ $ERRORS -gt 0 ]; then
    echo "❌ $ERRORS boundary violation(s) found"
    exit 1
else
    echo "✅ All architecture boundaries respected"
    exit 0
fi
