#!/usr/bin/env python3
"""
虎测平台 v2.0 - Bug 队列只读检查脚本
此脚本只查看记录，不执行飞书写入。飞书提交统一由 auto_submit.py 负责。

用法（在 Claude Code 中）：
  python3 /Users/hh/work/虎测平台/claude_tasks/process_bug.py <bug_id>
  或
  python3 /Users/hh/work/虎测平台/claude_tasks/process_bug.py --list
  或
  python3 /Users/hh/work/虎测平台/claude_tasks/process_bug.py --all

详细 Bug 的 AI 仅用于生成可编辑预览，不得创建或修改外部工作项。
"""
import json
import os
import sys
import glob

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_DIR = os.path.join(BASE_DIR, 'data', 'bug_queue')


def find_bug_files():
    """找到所有 bug JSON 文件"""
    return sorted(glob.glob(os.path.join(QUEUE_DIR, '*.json')))


def load_bug(bug_id_or_path):
    """加载单个 bug"""
    if os.path.exists(bug_id_or_path):
        path = bug_id_or_path
    elif os.path.exists(os.path.join(QUEUE_DIR, f'{bug_id_or_path}.json')):
        path = os.path.join(QUEUE_DIR, f'{bug_id_or_path}.json')
    else:
        # 模糊匹配
        matches = glob.glob(os.path.join(QUEUE_DIR, f'*{bug_id_or_path}*.json'))
        if not matches:
            return None
        path = matches[0]

    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_bug(data):
    """保存 bug JSON"""
    path = os.path.join(QUEUE_DIR, f'{data["id"]}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def list_pending():
    """列出所有待处理的 bug"""
    bugs = []
    for f in find_bug_files():
        with open(f, 'r', encoding='utf-8') as fp:
            b = json.load(fp)
            if b['status'] in ('draft', 'preview_ready', 'confirmed'):
                bugs.append(b)
    return bugs


def print_bug_summary(bug):
    """打印 bug 摘要"""
    print(f"""
╔══════════════════════════════════════════════════════╗
║  🐛 Bug: {bug['bug'].get('title', '未命名')[:52]}
╠══════════════════════════════════════════════════════╣
║  ID:       {bug['id']}
║  状态:     {bug['status']}
║  模块:     {bug['bug'].get('module', '')}
║  简述:     {bug['bug'].get('brief_description', '')}
║  经办人:   {bug['bug'].get('assignee', '')}
║  严重程度: {bug['bug'].get('severity', '')}
║  优先级:   {bug['bug'].get('priority', '')}
║  创建时间: {bug['created_at']}
║  附件:     {', '.join(bug['bug'].get('attachments', [])) or '无'}
╚══════════════════════════════════════════════════════╝
""")


def print_claude_instructions(bug):
    """打印 v2.0 的处理边界，不提供外部写入指令。"""
    print(f"""
📋 v2.0 处理方式：

- 当前记录只允许在平台页面中编辑和确认。
- 详细 Bug 可由 auto_process.py 使用 AI 整理预览。
- 问卷、简单提交和打点 Bug 使用固定规则生成预览。
- 飞书字段、经办人、附件、创建和校验只由 auto_submit.py 执行。
- 如果提交失败，请在平台中点击“重试”，不会创建第二条重复记录。
""")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ('--help', '-h'):
        print("用法: python3 process_bug.py [--list|--all|<bug_id>]")
        print("  --list  列出所有待处理 bug")
        print("  --all   批量显示所有待处理 bug 详情")
        print("  <bug_id> 查看单个 bug 详情和操作指南")
        return

    if sys.argv[1] == '--list':
        bugs = list_pending()
        if not bugs:
            print("✅ 队列为空，没有待处理的 bug")
            return
        print(f"\n📊 待处理 Bug ({len(bugs)}个):\n")
        for bug in bugs:
            status_icon = {'draft': '📝', 'preview_ready': '👁', 'confirmed': '✅'}.get(bug['status'], '❓')
            print(f"  {status_icon} [{bug['status']:14s}] {bug['id']}")
            print(f"     {bug['bug'].get('title', '无标题')}")
            print()
        print("查看详情: python3 process_bug.py <bug_id>")
        return

    if sys.argv[1] == '--all':
        bugs = list_pending()
        for bug in bugs:
            print_bug_summary(bug)
            print_claude_instructions(bug)
        return

    # 单个 bug 详情
    bug = load_bug(sys.argv[1])
    if bug is None:
        print(f"❌ 未找到 bug: {sys.argv[1]}")
        print(f"使用 --list 查看所有待处理 bug")
        return

    print_bug_summary(bug)
    if bug['status'] == 'submitted':
        print(f"  ✅ 已提交 (飞书ID: {bug.get('feishu_work_item_id', '未知')})")
        return
    print_claude_instructions(bug)


if __name__ == '__main__':
    main()
