#!/usr/bin/env bash
cd "$(dirname "$0")"
bash scripts/login.sh
read -r -p '按回车关闭…'
