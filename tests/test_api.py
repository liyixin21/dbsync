"""
API 层端到端测试。

覆盖报告中标出的每一个 P0/P1 缺陷，逐条给出回归断言。
"""
import pytest


# ============================================================ 认证

class TestAuth:
    def test_login_success(self, anon_client, admin_user):
        res = anon_client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin123"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["access_token"]
        assert body["username"] == "admin"
        assert body["expires_in"] > 0

    def test_login_wrong_password(self, anon_client, admin_user):
        res = anon_client.post(
            "/api/auth/login", json={"username": "admin", "password": "nope"}
        )
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "UNAUTHORIZED"

    def test_login_rate_limited(self, anon_client, admin_user):
        """P1 修复：旧实现记录失败登录却从不据此限流，密码可被暴力尝试。"""
        from app.core.security import login_rate_limiter

        login_rate_limiter._hits.clear()

        # 配置为 3 次
        for _ in range(3):
            anon_client.post(
                "/api/auth/login", json={"username": "admin", "password": "bad"}
            )

        res = anon_client.post(
            "/api/auth/login", json={"username": "admin", "password": "bad"}
        )
        assert res.status_code == 429
        assert res.json()["error"]["code"] == "RATE_LIMITED"

        login_rate_limiter._hits.clear()

    def test_error_envelope_shape(self, anon_client):
        """所有错误必须是统一外壳，前端只处理一种形状。"""
        res = anon_client.get("/api/auth/me")
        assert res.status_code == 401
        body = res.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]

    def test_me_requires_auth(self, anon_client):
        assert anon_client.get("/api/auth/me").status_code == 401

    def test_me_with_token(self, client):
        res = client.get("/api/auth/me")
        assert res.status_code == 200
        assert res.json()["username"] == "admin"

    def test_token_version_invalidation(self, client, db_session):
        """改密后旧令牌必须失效。"""
        from app.models.database import User

        old_token = client.app_token
        res = client.put(
            "/api/auth/change-password",
            json={"old_password": "admin123", "new_password": "newpass123"},
        )
        assert res.status_code == 200
        new_token = res.json()["access_token"]

        user = db_session.query(User).filter(User.username == "admin").first()
        db_session.refresh(user)
        assert user.token_version == 1

        # 旧令牌失效
        stale = client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})
        assert stale.status_code == 401

        # 新令牌可用
        fresh = client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"})
        assert fresh.status_code == 200

    def test_change_password_wrong_old(self, client, admin_user):
        res = client.put(
            "/api/auth/change-password",
            json={"old_password": "wrong", "new_password": "newpass123"},
        )
        assert res.status_code == 400

    def test_change_password_same_as_old(self, client, admin_user):
        res = client.put(
            "/api/auth/change-password",
            json={"old_password": "admin123", "new_password": "admin123"},
        )
        assert res.status_code == 400


# ============================================================ 数据库管理

class TestDatabases:
    def test_requires_auth(self, anon_client):
        """P1 修复：连接测试此前完全没有认证。"""
        assert anon_client.get("/api/databases/").status_code == 401

    def test_test_connection_requires_auth(self, anon_client):
        """
        关键回归：旧版本的 POST /databases/test-connection 没有 Depends(get_current_user)，
        任何未登录者都能用它探测内网 MySQL。
        """
        res = anon_client.post(
            "/api/databases/test-connection",
            json={
                "host": "10.0.0.1", "port": 3306, "username": "root",
                "password": "x", "database_name": "mysql",
            },
        )
        assert res.status_code == 401, "连接测试端点未受保护"

    def test_create_and_list(self, client):
        res = client.post(
            "/api/databases/",
            json={
                "name": "prod", "host": "10.0.0.5", "port": 3306,
                "username": "root", "password": "pw", "database_name": "proddb",
            },
        )
        assert res.status_code == 200
        created = res.json()
        assert created["password_set"] is True
        assert "password" not in created, "响应泄露了密码字段"

        listed = client.get("/api/databases/").json()
        assert len(listed) == 1

    def test_password_is_encrypted_at_rest(self, client, db_session):
        from app.models.database import Database
        from app.core.crypto import is_encrypted

        client.post(
            "/api/databases/",
            json={
                "name": "enc", "host": "h", "port": 3306,
                "username": "u", "password": "plaintext-pw", "database_name": "d",
            },
        )
        record = db_session.query(Database).filter(Database.name == "enc").first()
        assert record.password != "plaintext-pw"
        assert is_encrypted(record.password)

    def test_duplicate_name_rejected(self, client, sample_database):
        res = client.post(
            "/api/databases/",
            json={
                "name": sample_database.name, "host": "h", "port": 3306,
                "username": "u", "password": "p", "database_name": "d",
            },
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "NAME_TAKEN"

    def test_delete_in_use_rejected(self, client, sample_database, second_database):
        task = client.post(
            "/api/sync-tasks/",
            json={
                "name": "t1",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        )
        assert task.status_code == 200

        res = client.delete(f"/api/databases/{sample_database.id}")
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "DATABASE_IN_USE"
        assert "同步任务" in res.json()["error"]["message"]

    def test_delete_unused(self, client, sample_database):
        res = client.delete(f"/api/databases/{sample_database.id}")
        assert res.status_code == 200

    def test_not_found(self, client):
        assert client.get("/api/databases/9999").status_code == 404

    def test_update_keeps_password_when_blank(self, client, db_session, sample_database):
        from app.models.database import Database

        before = db_session.query(Database).filter(
            Database.id == sample_database.id
        ).first().password

        res = client.put(
            f"/api/databases/{sample_database.id}",
            json={"name": "renamed", "password": ""},
        )
        assert res.status_code == 200

        db_session.expire_all()
        after = db_session.query(Database).filter(
            Database.id == sample_database.id
        ).first().password
        assert after == before, "空密码被误当成新密码写入"


# ============================================================ 同步任务

class TestSyncTasks:
    def test_create_requires_two_distinct_databases(self, client, sample_database, second_database):
        res = client.post(
            "/api/sync-tasks/",
            json={
                "name": "same",
                "source_db_id": sample_database.id,
                "target_db_id": sample_database.id,
            },
        )
        assert res.status_code == 400
        assert "不能相同" in res.json()["error"]["message"]

    def test_lifecycle_fields_present(self, client, sample_database, second_database):
        res = client.post(
            "/api/sync-tasks/",
            json={
                "name": "monitor",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        )
        task = res.json()
        # 新增的可观测字段
        for field in ("health", "applied_events", "dlq_count", "running"):
            assert field in task, f"缺少可观测字段 {field}"
        assert task["health"] == "unknown"

    def test_start_fails_without_reachable_source(self, client, sample_database, second_database):
        """源库不可达时必须给出明确的 502，而不是 500 堆栈。"""
        task = client.post(
            "/api/sync-tasks/",
            json={
                "name": "unreachable",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        ).json()

        res = client.post(f"/api/sync-tasks/{task['id']}/start")
        assert res.status_code == 502
        assert res.json()["error"]["code"] == "DATABASE_CONNECTION_FAILED"

    def test_stop_when_not_running_is_400(self, client, sample_database, second_database):
        task = client.post(
            "/api/sync-tasks/",
            json={
                "name": "idle",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        ).json()
        res = client.post(f"/api/sync-tasks/{task['id']}/stop")
        assert res.status_code == 400

    def test_tables_endpoint_empty_initially(self, client, sample_database, second_database):
        task = client.post(
            "/api/sync-tasks/",
            json={
                "name": "tbl",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        ).json()
        res = client.get(f"/api/sync-tasks/{task['id']}/tables")
        assert res.status_code == 200
        assert res.json() == []

    def test_errors_endpoint_paginated(self, client, sample_database, second_database):
        task = client.post(
            "/api/sync-tasks/",
            json={
                "name": "errq",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        ).json()
        res = client.get(f"/api/sync-tasks/{task['id']}/errors")
        assert res.status_code == 200
        body = res.json()
        assert body["data"] == []
        assert body["page"]["total"] == 0

    def test_update_blocked_while_running(self, client, sample_database, second_database):
        """运行中改绑数据库应当被拒绝。"""
        from app.services.sync import sync_manager

        task = client.post(
            "/api/sync-tasks/",
            json={
                "name": "running",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        ).json()

        # 伪造一个运行中的引擎
        class FakeEngine:
            task_id = task["id"]
            is_running = True

        sync_manager._engines[task["id"]] = FakeEngine()  # type: ignore[assignment]
        try:
            res = client.put(
                f"/api/sync-tasks/{task['id']}",
                json={"target_db_id": sample_database.id},
            )
            assert res.status_code == 400
            assert "运行中" in res.json()["error"]["message"]
        finally:
            sync_manager._engines.pop(task["id"], None)

    def test_delete_removes_task(self, client, sample_database, second_database):
        task = client.post(
            "/api/sync-tasks/",
            json={
                "name": "todrop",
                "source_db_id": sample_database.id,
                "target_db_id": second_database.id,
            },
        ).json()
        assert client.delete(f"/api/sync-tasks/{task['id']}").status_code == 200
        assert client.get(f"/api/sync-tasks/{task['id']}").status_code == 404


# ============================================================ 备份计划

class TestBackupPlans:
    def test_create_full_only(self, client, sample_database):
        """增量备份已移除：请求 incremental 应被拒绝。"""
        res = client.post(
            "/api/backup-plans/",
            json={
                "name": "p1",
                "database_id": sample_database.id,
                "backup_type": "incremental",
                "schedule_interval": 60,
            },
        )
        # backup_type 不在请求模型中，多余字段被忽略；关键是不能创建出增量计划
        assert res.status_code in (200, 422)
        if res.status_code == 200:
            assert res.json()["backup_type"] == "full"

    def test_created_plan_is_full(self, client, sample_database):
        res = client.post(
            "/api/backup-plans/",
            json={
                "name": "p2",
                "database_id": sample_database.id,
                "schedule_interval": 30,
                "retention_count": 10,
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["backup_type"] == "full"
        assert body["schedule_interval"] == 30
        assert body["retention_count"] == 10

    def test_interval_required(self, client, sample_database):
        res = client.post(
            "/api/backup-plans/",
            json={"name": "p3", "database_id": sample_database.id},
        )
        assert res.status_code == 422

    def test_invalid_interval_rejected(self, client, sample_database):
        res = client.post(
            "/api/backup-plans/",
            json={
                "name": "p4",
                "database_id": sample_database.id,
                "schedule_interval": 0,
            },
        )
        assert res.status_code == 422

    def test_toggle_active(self, client, sample_database):
        plan = client.post(
            "/api/backup-plans/",
            json={
                "name": "p5",
                "database_id": sample_database.id,
                "schedule_interval": 60,
                "is_active": True,
            },
        ).json()

        res = client.put(f"/api/backup-plans/{plan['id']}", json={"is_active": False})
        assert res.status_code == 200
        assert res.json()["is_active"] is False

    def test_delete_plan_removes_history(self, client, db_session, sample_database):
        from app.models.database import BackupHistory, BackupPlan, BackupStatus, BackupType

        plan = client.post(
            "/api/backup-plans/",
            json={
                "name": "p6",
                "database_id": sample_database.id,
                "schedule_interval": 60,
            },
        ).json()

        history = BackupHistory(
            backup_plan_id=plan["id"],
            status=BackupStatus.COMPLETED,
            backup_type=BackupType.FULL,
        )
        db_session.add(history)
        db_session.commit()

        assert client.delete(f"/api/backup-plans/{plan['id']}").status_code == 200
        db_session.expire_all()
        assert db_session.query(BackupHistory).filter(
            BackupHistory.backup_plan_id == plan["id"]
        ).count() == 0


# ============================================================ 备份历史

class TestBackupHistory:
    @pytest.fixture()
    def history(self, db_session, sample_database):
        from app.models.database import BackupHistory, BackupPlan, BackupStatus, BackupType

        plan = BackupPlan(
            name="hplan", database_id=sample_database.id,
            schedule_interval=60, retention_count=5,
        )
        db_session.add(plan)
        db_session.flush()

        rows = [
            BackupHistory(
                backup_plan_id=plan.id,
                status=BackupStatus.COMPLETED,
                backup_type=BackupType.FULL,
                file_path=f"/tmp/nonexistent-{i}.sql",
                file_size=1024 * (i + 1),
            )
            for i in range(3)
        ]
        db_session.add_all(rows)
        db_session.commit()
        return plan, rows

    def test_pagination_shape(self, client, history):
        res = client.get("/api/backup-history/?skip=0&limit=2")
        assert res.status_code == 200
        body = res.json()
        assert body["page"]["total"] == 3
        assert len(body["data"]) == 2
        assert body["page"]["limit"] == 2

    def test_statistics(self, client, history):
        res = client.get("/api/backup-history/statistics")
        assert res.status_code == 200
        body = res.json()
        assert body["total_count"] == 3
        assert body["total_size"] == 1024 + 2048 + 3072
        # 新增的磁盘一致性统计
        assert "disk_file_count" in body
        assert "orphan_file_count" in body

    def test_batch_delete_endpoint_reachable(self, client, history):
        """
        P0 回归：旧实现把 DELETE /batch 注册在 DELETE /{history_id} 之后，
        请求永远命中路径参数路由并返回 422，该端点完全不可用。
        新实现改为 POST /batch，从根本上避开路由顺序问题。
        """
        _, rows = history
        ids = [rows[0].id, rows[1].id]
        res = client.post(
            "/api/backup-history/batch", json={"ids": ids, "delete_files": False}
        )
        assert res.status_code == 200, f"批量删除不可用: {res.status_code} {res.text}"
        assert res.json()["affected"] == 2

    def test_batch_delete_empty_ids_rejected(self, client, history):
        res = client.post("/api/backup-history/batch", json={"ids": []})
        assert res.status_code == 422

    def test_clear_removes_records(self, client, history):
        res = client.delete("/api/backup-history/clear?delete_files=false")
        assert res.status_code == 200
        assert client.get("/api/backup-history/").json()["page"]["total"] == 0

    def test_delete_single(self, client, history):
        _, rows = history
        assert client.delete(
            f"/api/backup-history/{rows[0].id}?delete_files=false"
        ).status_code == 200

    def test_download_missing_file_is_404(self, client, history):
        _, rows = history
        res = client.get(f"/api/backup-history/{rows[0].id}/download")
        assert res.status_code == 404

    def test_static_route_precedence(self, client, history):
        """/statistics 和 /clear 必须优先于 /{history_id} 匹配。"""
        assert client.get("/api/backup-history/statistics").status_code == 200
        assert client.delete("/api/backup-history/clear?delete_files=false").status_code == 200


# ============================================================ 日志

class TestLogs:
    def test_removed_log_is_404_not_500(self, client):
        """
        P0 回归：旧 logs.py 使用了未导入的 HTTPException，
        删除不存在的记录时抛 NameError 并被兜成 500。
        """
        res = client.delete("/api/logs/operations/999999")
        assert res.status_code == 404, f"期望 404，实际 {res.status_code}"
        assert res.json()["error"]["code"] == "NOT_FOUND"

    def test_login_log_delete(self, client):
        res = client.delete("/api/logs/logins/999999")
        assert res.status_code == 404

    def test_run_log_delete(self, client):
        res = client.delete("/api/logs/runs/999999")
        assert res.status_code == 404

    def test_list_shapes(self, client):
        for kind in ("operations", "logins", "runs"):
            res = client.get(f"/api/logs/{kind}")
            assert res.status_code == 200
            body = res.json()
            assert "data" in body and "page" in body

    def test_statistics(self, client):
        res = client.get("/api/logs/statistics")
        assert res.status_code == 200
        body = res.json()
        assert set(body.keys()) == {"operations", "logins", "runs"}

    def test_clear_endpoints_reachable(self, client):
        """静态路径段必须优先于 /{log_id}。"""
        for kind in ("operations", "logins", "runs"):
            res = client.delete(f"/api/logs/{kind}-clear")
            assert res.status_code == 200, f"{kind}-clear 不可达"

    def test_search_filter(self, client):
        res = client.get("/api/logs/operations?search=nothing-matches")
        assert res.status_code == 200
        assert res.json()["page"]["total"] == 0


# ============================================================ 系统

class TestSystem:
    def test_status(self, client):
        res = client.get("/api/system/status")
        assert res.status_code == 200
        body = res.json()
        for field in (
            "app_name", "app_version", "database_count", "sync_tasks_count",
            "sync_running_count", "backup_plans_count", "backup_dir", "mysql_tools",
        ):
            assert field in body

    def test_theme_defaults(self, client):
        res = client.get("/api/system/theme")
        assert res.status_code == 200
        body = res.json()
        assert "primary_color" in body
        assert "dark_mode" in body
        # 已废弃的 MD3 字段不应出现
        assert "theme_scheme" not in body
        assert "contrast_level" not in body

    def test_theme_update(self, client):
        res = client.put(
            "/api/system/theme", json={"primary_color": "#ff0000", "dark_mode": True}
        )
        assert res.status_code == 200
        assert res.json()["theme"]["primary_color"] == "#ff0000"

        fetched = client.get("/api/system/theme").json()
        assert fetched["dark_mode"] is True

    def test_theme_rejects_invalid_color(self, client):
        res = client.put("/api/system/theme", json={"primary_color": "not-a-color"})
        assert res.status_code == 422

    def test_config_whitelist(self, client):
        """旧实现允许写任意 key，包括已废弃的 MD3 残留配置。"""
        res = client.put("/api/system/configs/theme_scheme", json={"value": "X"})
        assert res.status_code == 400
        assert "不允许" in res.json()["error"]["message"]

    def test_config_allowed_key(self, client):
        res = client.put("/api/system/configs/backup_dir", json={"value": "/data/bk"})
        assert res.status_code == 200
        assert client.get("/api/system/configs/backup_dir").json()["value"] == "/data/bk"

    def test_app_logs(self, client):
        res = client.get("/api/system/logs?lines=10")
        assert res.status_code == 200
        assert "logs" in res.json()


# ============================================================ 健康检查与首页

class TestPublicEndpoints:
    def test_health_no_auth(self, anon_client):
        res = anon_client.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "healthy"

    def test_index_served(self, anon_client):
        res = anon_client.get("/")
        assert res.status_code in (200, 503)

    def test_openapi_reachable(self, anon_client):
        assert anon_client.get("/api/openapi.json").status_code == 200


class TestSpaFallback:
    """
    SPA 前端路由回退。

    真实环境暴露的问题：前端用 history 模式，用户在 /databases 这类子页面
    直接刷新或粘贴链接时，浏览器会向服务端请求该路径。若服务端只注册了 "/"，
    就会返回 404 —— 用户看到一页 JSON 错误，而不是应用界面。
    """

    def test_spa_routes_serve_index(self, anon_client):
        for path in ("/databases", "/sync", "/backups", "/logs", "/settings"):
            res = anon_client.get(path)
            assert res.status_code in (200, 503), f"{path} 返回 {res.status_code}"
            if res.status_code == 200:
                assert "text/html" in res.headers.get("content-type", "")

    def test_root_serves_index(self, anon_client):
        res = anon_client.get("/")
        assert res.status_code in (200, 503)

    def test_api_paths_not_swallowed_by_fallback(self, anon_client):
        """/api 下的未知路径必须返回 404 JSON，不能回退成 HTML。"""
        res = anon_client.get("/api/definitely-not-a-route")
        assert res.status_code == 404
        assert "application/json" in res.headers.get("content-type", "")

    def test_missing_asset_returns_404_not_html(self, anon_client):
        """缺失的静态资源不能回退成 HTML，否则会引发 JS/CSS 解析错误。"""
        res = anon_client.get("/assets/does-not-exist.js")
        assert res.status_code == 404

    def test_health_not_swallowed(self, anon_client):
        res = anon_client.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "healthy"
