#!/bin/sh
# 容器入口脚本。
#
# 解决的问题：容器以非 root 用户（dbsync）运行，但 docker-compose 挂载的
# 宿主机目录通常属于 root 或宿主机用户，UID 不一致导致容器内无法写入
# /app/data 与 /app/backups，启动时报 Permission denied。
#
# 处理方式：以 root 身份启动，把挂载目录的属主改为 dbsync，然后用
# gosu/su-exec 降权执行真正的服务进程。这样既保证可写，又避免服务
# 以 root 长期运行。
set -e

APP_UID=$(id -u dbsync)
APP_GID=$(id -g dbsync)

# 需要确保可写的挂载点
for dir in /app/data /app/backups; do
    [ -d "$dir" ] || continue
    # 已属于目标用户则跳过，避免每次启动都递归 chown 大目录
    current_owner=$(stat -c '%u:%g' "$dir" 2>/dev/null || echo "")
    if [ "$current_owner" != "$APP_UID:$APP_GID" ]; then
        echo "[entrypoint] 修正挂载目录属主: $dir → $APP_UID:$APP_GID"
        chown -R "$APP_UID:$APP_GID" "$dir" 2>/dev/null || \
            echo "[entrypoint] 警告：无法修改 $dir 属主，若启动失败请在宿主机执行 sudo chown -R $APP_UID:$APP_GID ./data ./backups"
    fi
done

# 降权执行服务
exec gosu "$APP_UID:$APP_GID" "$@"
