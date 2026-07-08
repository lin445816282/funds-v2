#!/bin/bash
# funds-v2 DB 备份 — 保留最近 30 个备份
SRC=~/projects/funds-v2/backend/funds-v2.db
DIR=~/projects/funds-v2/backend/backups
mkdir -p "$DIR"

NAME="funds-v2-$(date +%Y%m%d_%H%M).db"
cp "$SRC" "$DIR/$NAME"

# 保留最近30个
cd "$DIR" && ls -t funds-v2-*.db | tail -n +31 | xargs rm -f 2>/dev/null
