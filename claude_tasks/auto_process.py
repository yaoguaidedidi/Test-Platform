#!/usr/bin/env python3
"""
虎测平台 - 详细 Bug AI 内容整理脚本
问卷、简单提交和脚本生成的 Bug 都使用固定规则，不调用 AI。
"""
import json
import os
import re
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_DIR = os.path.join(BASE_DIR, 'data', 'bug_queue')
CLAUDE = os.environ.get('CLAUDE_BIN', os.path.expanduser('~/.local/bin/claude'))

sys.path.insert(0, BASE_DIR)
from lib.feishu import search_test_cases as _drive_search_test_cases
from lib.bug_queue import build_survey_preview, build_simple_preview
from lib.bug_queue import lock_bug

PROCESS_PROMPT = """你是一个Bug分析工具，严格按以下规范输出。

输入：一个bug简述
输出：JSON（不要markdown包裹，不要其他文字）

规范：
- 如果提供了"参考测试用例"，必须优先从测试用例中提取/改写操作步骤和预期结果。
- 不要照搬与 bug 无关的用例；只使用和 bug 简述最相关的步骤。
- 如果参考测试用例不足，再根据 bug 简述补全缺失步骤，并保持步骤可执行。
- title: 【模块名】简述
- module: 根据关键词判断（图鉴/红点→图鉴系统，剧情/对话/引导→剧情系统，生成器→生成器系统，账号→账号系统，热更新→热更新）
- severity: "2"（重要）或 "1"（严重）或 "3"（一般）或 "5"（微小），默认"2"
- severity_label: 对应的中文
- priority: "0"P0 / "1"P1 / "2"P2 / "kvt5xvrgc"P3 / "99"待定，默认"1"
- priority_label: 对应的中文
- description: 格式如下（严格换行）：
【模块名】简述

操作步骤
1. 具体可执行的动作（不超过5步）
2. ...
n. 观察表现

实际结果
具体的错误表现（有数值写具体数值）

预期结果
正确的表现（有数值写具体数值）

Bug简述: {brief}
经办人: {assignee}
"""

def _save_bug(data):
    """原子写回 bug JSON。"""
    path = os.path.join(QUEUE_DIR, f'{data["id"]}.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _mark_failed(bug_id, message):
    """加锁复查后标记 AI 分析失败，避免覆盖并发修改。"""
    with lock_bug(bug_id, queue_dir=QUEUE_DIR):
        path = os.path.join(QUEUE_DIR, f'{bug_id}.json')
        if not os.path.exists(path):
            return
        with open(path, 'r', encoding='utf-8') as f:
            bug = json.load(f)
        if bug['status'] != 'draft':
            return
        bug['status'] = 'analysis_failed'
        bug['error'] = message
        _save_bug(bug)


def process_bug(bug_id):
    """分析 bug 并生成预览"""
    if not re.fullmatch(r'[\w\u4e00-\u9fff.,，。【】（）()：:+-]+', bug_id or ''):
        print(f'ERROR: 非法 bug id: {bug_id}')
        return

    # 读入 + 状态检查放在锁内，固定规则路径也直接在锁内完成
    with lock_bug(bug_id, queue_dir=QUEUE_DIR):
        path = os.path.join(QUEUE_DIR, f'{bug_id}.json')
        if not os.path.exists(path):
            print(f'ERROR: file not found {path}')
            return

        with open(path, 'r') as f:
            bug = json.load(f)

        if bug['status'] != 'draft':
            print(f'SKIP: already {bug["status"]}')
            return

        brief = bug['bug']['brief_description']
        assignee = bug['bug'].get('assignee', '萌虎')
        mode = bug.get('mode', 'detail')
        survey_desc = bug.get('survey_desc', '')
        survey_env = bug.get('survey_env', '')

        # 兼容历史非详细草稿/失败重试：固定规则生成预览，绝不调用 AI。
        if mode in ('survey', 'simple', 'scripted'):
            if mode == 'survey':
                preview = build_survey_preview(brief, survey_desc, survey_env)
            elif isinstance(bug.get('preview'), dict):
                preview = bug['preview']
            else:
                preview = build_simple_preview(brief, bug['bug'].get('module', ''))
            bug['status'] = 'preview_ready'
            bug['error'] = None
            bug['preview'] = preview
            bug['bug']['module'] = preview['module']
            bug['bug']['title'] = preview['title']
            bug['bug']['severity'] = preview['severity']
            bug['bug']['priority'] = preview['priority']
            bug['bug']['description'] = preview['description']
            _save_bug(bug)
            print(f'DONE: {bug_id} ({mode} preview without AI)')
            return

    # 详细 Bug 依赖 AI 整理：先确认本机 claude 可用，没有就直接标记失败，
    # 避免服务器上无 AI 时白等飞书搜索后再报底层 FileNotFoundError。
    if not shutil.which(CLAUDE):
        _mark_failed(bug_id, f'未检测到 AI（claude），详细提交需要安装 Claude Code')
        print(f'ERROR: {bug_id} claude not found: {CLAUDE}', flush=True)
        return

    # 从飞书测试用例存档搜索相关用例（不持锁，可能耗时）
    keywords = [kw for kw in re.split(r'[【】\s,.，。、：:；;]+', brief) if len(kw) >= 2]
    test_context = ''
    try:
        test_context = _drive_search_test_cases(keywords)
    except Exception:
        pass
    print(f'TEST_CONTEXT: keywords={keywords} chars={len(test_context)}', flush=True)

    tc = f'\n参考测试用例（来自飞书用例存档）：\n{test_context}\n' if test_context else ''

    prompt_template = PROCESS_PROMPT

    if tc:
        safe_tc = tc.replace('{', '{{').replace('}', '}}')
        prompt_template = prompt_template.replace(
            'Bug简述: {brief}',
            f'{safe_tc}Bug简述: {{brief}}'
        )

    prompt = prompt_template.format(
        brief=brief,
        assignee=assignee,
        survey_desc=survey_desc,
        survey_env=survey_env
    )

    result = None
    stderr_text = ''
    try:
        result = subprocess.run(
            [CLAUDE, '-p', prompt, '--max-turns', '5', '--allow-dangerously-skip-permissions'],
            capture_output=True, text=True, timeout=120,
            cwd=BASE_DIR
        )
        output = result.stdout.strip()
        stderr_text = result.stderr.strip()
        # 去掉可能的 markdown 包裹（``` 或 ```json）
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', output, re.DOTALL)
        if m:
            output = m.group(1).strip()
        elif output.startswith('```'):
            output = output[3:].strip()
        # 尝试提取 JSON 对象
        preview = None
        try:
            preview = json.loads(output)
        except json.JSONDecodeError:
            start = output.find('{')
            end = output.rfind('}')
            if start >= 0 and end > start:
                try:
                    preview = json.loads(output[start:end+1])
                except json.JSONDecodeError:
                    pass
        if preview is None:
            raise ValueError(f'No JSON found in output')
    except Exception as e:
        log_path = os.path.join(QUEUE_DIR, '..', 'auto_process.log')
        with open(log_path, 'a') as lf:
            lf.write(f'[{bug_id}] ERROR: {e}\n')
            lf.write(
                '  stdout: '
                + ((result.stdout[:500]) if result is not None and result.stdout else 'N/A')
                + '\n'
            )
            lf.write(f'  stderr: {stderr_text or "N/A"}\n')
        _mark_failed(bug_id, f'AI 分析失败: {e}')
        return

    # 写回前重新加锁并复查：文件可能已被删除或状态已被改动
    with lock_bug(bug_id, queue_dir=QUEUE_DIR):
        path = os.path.join(QUEUE_DIR, f'{bug_id}.json')
        if not os.path.exists(path):
            return
        with open(path, 'r') as f:
            bug = json.load(f)
        if bug['status'] != 'draft':
            print(f'SKIP: already {bug["status"]}')
            return

        bug['status'] = 'preview_ready'
        bug['error'] = None
        bug['preview'] = preview
        bug['bug']['module'] = preview.get('module', '')
        bug['bug']['title'] = preview.get('title', '')
        bug['bug']['severity'] = preview.get('severity', '2')
        bug['bug']['priority'] = preview.get('priority', '1')
        _save_bug(bug)

    print(f'DONE: {bug_id}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: auto_process.py <bug_id>')
        sys.exit(1)
    process_bug(sys.argv[1])
