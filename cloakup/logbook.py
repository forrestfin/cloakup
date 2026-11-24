"""本机结构化日志：每类一天一个 JSON Lines 文件，供日报和持续监控分析。

文件：<root>/logs/<类别>-YYYY-MM-DD.jsonl（Docker 中 /data/logs，绑定到本机 data/logs）。

类别：
- event   执行事件全量（与工作台记录同源），附运行状态、阶段、当前岗位
- state   状态变化：任务运行／暂停（谁、为什么）、执行器阶段切换与下次检查时间、会话选择失败诊断
- action  动作账本：占位、执行、核验、失败、结果未知
- model   模型调用：耗时、修正轮数、网关请求数、结果；决策暂缓的原因与异常
- error   异常与警告：类型、信息、调用位置（文件、函数、行号）

约束：
- 写日志永远不能影响执行：任何写入失败都吞掉。
- 凭据（模型密钥、浏览器许可证、飞书密钥）在落盘前替换为 [已隐藏]。
- 不写入候选人消息原文；候选人以编号标识，事件消息与工作台显示一致。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

CATEGORIES = ('event', 'state', 'action', 'model', 'error')
RETENTION_DAYS = 30
SECRET_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLOAKBROWSER_LICENSE_KEY', 'LARK_APP_SECRET')

_lock = threading.Lock()
_book = None


def _tz():
    from .daily_report import report_timezone
    return report_timezone()


def frames(exc):
    """异常调用位置：只记文件名、函数、行号，不记局部变量和源码。"""
    items = []
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        items.extend({'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                     for f in traceback.extract_tb(current.__traceback__))
        current = current.__cause__ or current.__context__
    return items[-20:]


class Logbook:
    def __init__(self, root, retention_days=RETENTION_DAYS):
        self.folder = Path(root) / 'logs'
        self.retention_days = retention_days
        self._pruned_day = None

    def path(self, category, day):
        return self.folder / f'{category}-{day.isoformat()}.jsonl'

    def write(self, category, record):
        now = record.get('at') or time.time()
        local = datetime.fromtimestamp(now, _tz())
        line = json.dumps({'at': now, 'time': local.isoformat(timespec='milliseconds'), 'category': category, **record},
                          ensure_ascii=False, default=str)
        for key in SECRET_ENV:
            value = os.environ.get(key)
            if value and len(value) >= 6:
                line = line.replace(value, '[已隐藏]')
        with _lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self.path(category, local.date()).open('a', encoding='utf-8') as f:
                f.write(line + '\n')
            if self._pruned_day != local.date():
                self._pruned_day = local.date()
                self.prune(local.date())

    def prune(self, today):
        cutoff = today - timedelta(days=self.retention_days)
        for path in self.folder.glob('*-*.jsonl'):
            category, _, stamp = path.stem.partition('-')
            if category not in CATEGORIES:
                continue
            try:
                day = datetime.strptime(stamp, '%Y-%m-%d').date()
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)


class _ErrorHandler(logging.Handler):
    """把 cloakup 日志器里的警告和异常写进 error 日志。"""

    def emit(self, record):
        entry = {'level': record.levelname, 'logger': record.name, 'message': record.getMessage()[:2000],
                 'source': {'file': Path(record.pathname).name, 'function': record.funcName, 'line': record.lineno}}
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            entry.update(exception=type(exc).__name__, detail=str(exc)[:1000], frames=frames(exc))
        log('error', **entry)


def configure(root, retention_days=RETENTION_DAYS):
    global _book
    _book = Logbook(root, retention_days)
    logger = logging.getLogger('cloakup')
    if not any(isinstance(h, _ErrorHandler) for h in logger.handlers):
        handler = _ErrorHandler(level=logging.WARNING)
        logger.addHandler(handler)
    return _book


def log(category, **fields):
    if _book is None or category not in CATEGORIES:
        return
    try:
        _book.write(category, fields)
    except Exception:
        pass


def exception(category, exc, **fields):
    log(category, exception=type(exc).__name__, detail=str(exc)[:1000], frames=frames(exc), **fields)
