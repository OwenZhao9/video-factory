#!/bin/bash
# 双击这个文件启动「视频工厂」网页版，使用期间保持窗口开着
cd "$(dirname "$0")"
echo "正在启动视频工厂……"
/usr/bin/python3 webapp.py
