#!/usr/bin/env python3
"""MemoryGraph Work Tracker — goal/task/result с типами и связями."""

import sqlite3, json, uuid, sys, os
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────
MEMORYGRAPH_DB = os.environ.get(
    "MEMORYGRAPH_DB",
    "/home/andreitrifonov/infra/docker/hermes-hybrid-memory/data/andrei/memorygraph/memorygraph_andrei.db"
)
AGENT_ID = os.environ.get("AGENT_ID", "andrei")

# ── DB ────────────────────────────────────────────────────────
def _conn():
    c = sqlite3.connect(MEMORYGRAPH_DB)
    c.row_factory = sqlite3.Row
    return c

# ── Node CRUD ─────────────────────────────────────────────────
def add_node(nodetype: str, title: str, content: str = "", status: str = "pending", meta: dict = None):
    """Добавить узел: goal, task или result."""
    conn = _conn()
    node_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    props = {"id": node_id, "type": nodetype, "title": title,
             "content": content, "status": status}
    if meta:
        props.update(meta)
    conn.execute(
        "INSERT INTO nodes (id, label, properties, created_at, updated_at) VALUES (?,?,?,?,?)",
        [node_id, "Memory", json.dumps(props), now, now])
    conn.commit()
    conn.close()
    print(f"[work_tracker] +{nodetype}: {title}")
    return node_id

def link(from_id: str, to_id: str, rel_type: str, context: str = ""):
    """Создать связь: PART_OF, ACHIEVED_BY, FOLLOWS, BLOCKED_BY, LEADS_TO."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    rel_id = str(uuid.uuid4())
    props = {"strength": 0.9, "confidence": 0.9, "context": context,
             "created_at": now}
    conn.execute(
        "INSERT INTO relationships (id, from_id, to_id, rel_type, properties, created_at, valid_from) VALUES (?,?,?,?,?,?,?)",
        [rel_id, from_id, to_id, rel_type, json.dumps(props), now, now])
    conn.commit()
    conn.close()
    return rel_id

# ── Query ─────────────────────────────────────────────────────
def get_nodes(nodetype: str = None, status: str = None) -> list:
    """Получить узлы с фильтром по типу и статусу."""
    conn = _conn()
    q = "SELECT * FROM nodes WHERE 1=1"
    params = []
    if nodetype:
        q += " AND json_extract(properties, '$.type') = ?"
        params.append(nodetype)
    if status:
        q += " AND json_extract(properties, '$.status') = ?"
        params.append(status)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_relationships(from_id: str = None, rel_type: str = None) -> list:
    """Получить связи."""
    conn = _conn()
    q = "SELECT * FROM relationships WHERE 1=1"
    params = []
    if from_id:
        q += " AND from_id = ?"
        params.append(from_id)
    if rel_type:
        q += " AND rel_type = ?"
        params.append(rel_type)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Fix: retype existing nodes ────────────────────────────────
KNOWN_GOALS = {
    "SecureStore + мелочи": "goal",
    "Отказ от Encvoy → built-in auth (21 июня)": "goal",
    "Keenetic DNS + .home домен (21 июня)": "goal",
    "Ai-платформа: Open WebUI + SearXNG + Qdrant + Firecrawl (21 июня)": "goal",
    "CORS-прокси + streaming (21 июня)": "goal",
    "MemoryGraph Work Tracker (22 июня)": "goal",
}

KNOWN_RESULTS = {
    "SecureStore работает": "result",
    "device_id fix работает": "result",
    "20 июня заполнен": "result",
    "Built-in auth работает": "result",
    ".home DNS работает": "result",
    "Ai-платформа развёрнута": "result",
    "CORS-прокси + streaming работают": "result",
}

def retype_nodes():
    """Исправить типы существующих узлов."""
    conn = _conn()
    
    # Fix task→goal and task→result
    nodes = conn.execute(
        "SELECT id, properties FROM nodes WHERE json_extract(properties, '$.type') = 'task'"
    ).fetchall()
    
    fixed = 0
    for row in nodes:
        props = json.loads(row["properties"])
        title = props.get("title", "")

        if title in KNOWN_GOALS:
            props["type"] = "goal"
            props["status"] = "done"
            conn.execute("UPDATE nodes SET properties = ? WHERE id = ?",
                         [json.dumps(props), row["id"]])
            fixed += 1
        elif title in KNOWN_RESULTS:
            new_type = KNOWN_RESULTS[title]
            props["type"] = new_type
            props["status"] = "done"
            conn.execute("UPDATE nodes SET properties = ? WHERE id = ?",
                         [json.dumps(props), row["id"]])
            fixed += 1
    
    # Fix incorrectly typed goals→result
    bad_goals = conn.execute(
        "SELECT id, properties FROM nodes WHERE json_extract(properties, '$.type') = 'goal'"
    ).fetchall()
    for row in bad_goals:
        props = json.loads(row["properties"])
        title = props.get("title", "")
        if title in KNOWN_RESULTS:
            props["type"] = KNOWN_RESULTS[title]
            props["status"] = "done"
            conn.execute("UPDATE nodes SET properties = ? WHERE id = ?",
                         [json.dumps(props), row["id"]])
            fixed += 1

    conn.commit()
    
    # Mark tasks of done goals as done
    done_goals = conn.execute(
        "SELECT id FROM nodes WHERE json_extract(properties, '$.status') = 'done' AND json_extract(properties, '$.type') = 'goal'"
    ).fetchall()
    for (gid,) in done_goals:
        task_rows = conn.execute("""
            SELECT n.id, n.properties FROM nodes n
            JOIN relationships r ON r.from_id = n.id
            WHERE r.to_id = ? AND r.rel_type = 'OCCURS_IN'
              AND json_extract(n.properties, '$.type') = 'task'
        """, [gid]).fetchall()
        for tid, tprops_json in task_rows:
            tp = json.loads(tprops_json)
            if tp.get("status") != "done":
                tp["status"] = "done"
                conn.execute("UPDATE nodes SET properties = ? WHERE id = ?",
                             [json.dumps(tp), tid])
                fixed += 1
    
    conn.commit()
    conn.close()
    print(f"[work_tracker] Retyped {fixed} nodes")

# ── Summary ───────────────────────────────────────────────────
def summary():
    """Вывести сводку: цели → задачи → результаты."""
    retype_nodes()  # сначала исправить типы

    goals = get_nodes(nodetype="goal")
    tasks = get_nodes(nodetype="task")
    results = get_nodes(nodetype="result")

    done_goals = [g for g in goals if json.loads(g["properties"]).get("status") == "done"]
    active_goals = [g for g in goals if json.loads(g["properties"]).get("status") != "done"]

    done_tasks = [t for t in tasks if json.loads(t["properties"]).get("status") == "done"]
    pending_tasks = [t for t in tasks if json.loads(t["properties"]).get("status") == "pending"]

    print(f"🎯 Цели: {len(goals)} ({len(done_goals)} done, {len(active_goals)} active)")
    print(f"📋 Задачи: {len(tasks)} ({len(done_tasks)} done, {len(pending_tasks)} pending)")
    print(f"✅ Результаты: {len(results)}")
    print()

    for g in sorted(goals, key=lambda x: x["created_at"] or ""):
        props = json.loads(g["properties"])
        status = props.get("status", "?")
        icon = {"done": "✅", "in_progress": "🔄", "pending": "⏳"}.get(status, "❓")
        print(f"{icon} [{status:12s}] {props['title']}")

        # Найти задачи этой цели
        conn = _conn()
        subtasks = conn.execute("""
            SELECT n.properties, n.id FROM nodes n
            JOIN relationships r ON r.from_id = n.id
            WHERE r.to_id = ? AND r.rel_type = 'OCCURS_IN'
              AND json_extract(n.properties, '$.type') = 'task'
        """, [g["id"]]).fetchall()
        conn.close()

        for st in subtasks:
            sp = json.loads(st["properties"])
            s_status = sp.get("status", "?")
            s_icon = "  ✓" if s_status == "done" else "  ○"
            print(f"    {s_icon} {sp['title']}")

        print()

# ── CLI ───────────────────────────────────────────────────────
def add_goal(title: str, content: str = "", meta: dict = None):
    return add_node("goal", title, content, "pending", meta)

def add_task(title: str, content: str = "", parent_goal_id: str = None, meta: dict = None):
    tid = add_node("task", title, content, "pending", meta)
    if parent_goal_id:
        link(tid, parent_goal_id, "OCCURS_IN", f"Задача '{title}' в рамках цели")
    return tid

def add_result(title: str, content: str = "", parent_task_id: str = None, meta: dict = None):
    rid = add_node("result", title, content, "done", meta)
    if parent_task_id:
        link(parent_task_id, rid, "LEADS_TO", f"Задача привела к результату '{title}'")
    return rid

def mark_done(node_id: str):
    conn = _conn()
    props = json.loads(conn.execute("SELECT properties FROM nodes WHERE id=?", [node_id]).fetchone()["properties"])
    props["status"] = "done"
    conn.execute("UPDATE nodes SET properties=?, updated_at=? WHERE id=?",
                 [json.dumps(props), datetime.now(timezone.utc).isoformat(), node_id])
    conn.commit()
    conn.close()
    print(f"[work_tracker] ✓ done: {props['title']}")

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: work_tracker.py <command> [args]")
        print("  summary                      — сводка целей и задач")
        print("  retype                       — исправить типы существующих узлов")
        print("  goal <title>                 — добавить цель")
        print("  task <title> [goal_id]       — добавить задачу")
        print("  result <title> [task_id]     — добавить результат")
        print("  done <node_id>               — отметить выполненным")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "summary":
        summary()
    elif cmd == "retype":
        retype_nodes()
    elif cmd == "goal":
        if len(sys.argv) < 3:
            print("Usage: work_tracker.py goal <title>"); sys.exit(1)
        add_goal(sys.argv[2])
    elif cmd == "task":
        if len(sys.argv) < 3:
            print("Usage: work_tracker.py task <title> [goal_id]"); sys.exit(1)
        gid = sys.argv[3] if len(sys.argv) > 3 else None
        add_task(sys.argv[2], parent_goal_id=gid)
    elif cmd == "result":
        if len(sys.argv) < 3:
            print("Usage: work_tracker.py result <title> [task_id]"); sys.exit(1)
        tid = sys.argv[3] if len(sys.argv) > 3 else None
        add_result(sys.argv[2], parent_task_id=tid)
    elif cmd == "done":
        if len(sys.argv) < 3:
            print("Usage: work_tracker.py done <node_id>"); sys.exit(1)
        mark_done(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
