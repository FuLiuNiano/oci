#!/usr/bin/env bash
# OCI Panel 安装/管理脚本（Linux）
# 用法: bash install.sh [start|stop|restart|status|log|uninstall]
set -e
umask 077
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$APP_DIR/.venv"
SVC=/etc/systemd/system/oci-panel.service

ensure_deps() {
  command -v python3 >/dev/null || { echo "请先安装 python3"; exit 1; }
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
}

start_systemd() {
  cat > "$SVC" <<EOF
[Unit]
Description=OCI Panel
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
UMask=0077
ExecStart="$VENV/bin/python" "$APP_DIR/main.py"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now oci-panel
  echo "已安装 systemd 服务并启动，访问 http://你的IP:9527"
}

case "${1:-start}" in
  install|start)
    if [ -f "$APP_DIR/panel.pid" ] && kill -0 "$(cat "$APP_DIR/panel.pid")" 2>/dev/null; then
      echo "面板已经运行，请使用 restart 重启"; exit 0
    fi
    ensure_deps
    if command -v systemctl >/dev/null 2>&1 && [ -w /etc/systemd/system ]; then
      start_systemd
    else
      cd "$APP_DIR"
      nohup "$VENV/bin/python" main.py > panel.log 2>&1 &
      echo $! > panel.pid
      echo "已后台启动 pid=$(cat panel.pid)，日志: panel.log，访问 http://你的IP:9527"
    fi
    ;;
  stop)
    if systemctl is-active --quiet oci-panel 2>/dev/null; then
      systemctl stop oci-panel
    elif [ -f "$APP_DIR/panel.pid" ]; then
      kill "$(cat "$APP_DIR/panel.pid")" 2>/dev/null || true
      rm -f "$APP_DIR/panel.pid"
    fi
    echo "已停止"
    ;;
  restart) bash "$0" stop; sleep 1; bash "$0" start ;;
  status)
    if systemctl is-active --quiet oci-panel 2>/dev/null; then echo "运行中 (systemd)"
    elif [ -f "$APP_DIR/panel.pid" ] && kill -0 "$(cat "$APP_DIR/panel.pid")" 2>/dev/null; then echo "运行中 (pid $(cat "$APP_DIR/panel.pid"))"
    else echo "未运行"; fi
    ;;
  log)
    if [ -f "$APP_DIR/panel.log" ]; then tail -n 200 -f "$APP_DIR/panel.log"
    elif [ -f "$APP_DIR/data/panel.log" ]; then tail -n 200 -f "$APP_DIR/data/panel.log"
    else journalctl -u oci-panel -f; fi
    ;;
  uninstall)
    systemctl disable --now oci-panel 2>/dev/null || true
    rm -f "$SVC"
    [ -f "$APP_DIR/panel.pid" ] && kill "$(cat "$APP_DIR/panel.pid")" 2>/dev/null || true
    rm -f "$APP_DIR/panel.pid"
    echo "服务已停止并卸载。数据目录 $APP_DIR/data 已保留，彻底删除请手动清理。"
    ;;
  *)
    echo "用法: bash install.sh [start|stop|restart|status|log|uninstall]"
    ;;
esac
