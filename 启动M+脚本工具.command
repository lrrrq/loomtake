#!/bin/zsh

set -u

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || {
  echo "无法进入 M+ 项目目录：$PROJECT_DIR"
  exit 1
}

exec "$PROJECT_DIR/mplus" ui "$@"
