"""
虎测平台 - 共享 xlsx 生成库
从 gen_generator_testcase.py 和 generate_*.py 提取公共逻辑
"""
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
import os
import time

# ============================================================
# 样式常量
# ============================================================
TITLE_FONT = Font(name='Calibri', size=14, bold=True, color='1F3864')
HEADER_FONT = Font(name='Calibri', size=11, bold=True, color='FFFFFF')
HEADER_FILL = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
SECTION_FONT = Font(name='Calibri', size=11, bold=True, color='1F3864')
SECTION_FILL = PatternFill(start_color='D6E4F0', end_color='D6E4F0', fill_type='solid')
NORMAL_FONT = Font(name='Calibri', size=11)
SMALL_FONT = Font(name='Calibri', size=10, color='808080')
ALT_FILL = PatternFill(start_color='F2F7FB', end_color='F2F7FB', fill_type='solid')
WHITE_FILL = PatternFill(start_color='FFFFFF', end_color='FFFFFF', fill_type='solid')
THIN_BORDER = Border(
    left=Side(style='thin', color='B4C6E7'),
    right=Side(style='thin', color='B4C6E7'),
    top=Side(style='thin', color='B4C6E7'),
    bottom=Side(style='thin', color='B4C6E7'),
)

# 对齐
WRAP_ALIGN = Alignment(horizontal='left', vertical='top', wrap_text=True)
CENTER_ALIGN = Alignment(horizontal='center', vertical='center', wrap_text=True)
VERT_CENTER = Alignment(horizontal='left', vertical='center', wrap_text=True)

# 列定义
HEADERS = ['编号', '一级模块', '二级模块', '三级模块', '四级模块', '五级模块',
           '操作步骤＆测试场景说明', '期望结果', '测试结果', '测试确认', '优先级', '备注']

COL_WIDTHS = {1: 8, 2: 20, 3: 18, 4: 18, 5: 14, 6: 14,
              7: 60, 8: 48, 9: 12, 10: 12, 11: 8, 12: 18}


def style_cell(cell, font=None, fill=None, alignment=None):
    if font: cell.font = font
    if fill: cell.fill = fill
    if alignment: cell.alignment = alignment
    cell.border = THIN_BORDER


class TestCaseBuilder:
    """累积测试用例，生成标准格式 xlsx"""

    def __init__(self, title="", author="萌虎", reviewer=""):
        self.title = title
        self.author = author
        self.reviewer = reviewer
        self.cases = []  # 每项: (B, C, D, E, F, G, H, priority, remark)

    def add_case(self, module1, module2, module3="", module4="", module5="",
                 steps="", results="", priority=1, remark=""):
        """
        添加一条用例。
        results 可以是 \n 分隔的字符串（拆分多行），或单个字符串。
        """
        if isinstance(results, str):
            results_list = [r.strip() for r in results.split('\n') if r.strip()]
            if not results_list:
                results_list = [results]
        else:
            results_list = results

        for i, r in enumerate(results_list):
            self.cases.append((
                module1 if i == 0 else "",
                module2 if i == 0 else "",
                module3 if i == 0 else "",
                module4 if i == 0 else "",
                module5 if i == 0 else "",
                steps if i == 0 else "",
                r,
                priority if i == 0 else 0,
                remark if i == 0 else ""
            ))

    def add_cases_batch(self, case_list):
        """批量添加。每项: (B, C, D, E, F, G, H, priority, remark)"""
        for case in case_list:
            b, c, d, e, f, g, h, priority, remark = case
            self.add_case(b, c, d, e, f, g, h, priority, remark)

    def build_xlsx(self, output_path=None):
        """生成完整 8-Sheet xlsx，返回文件路径"""
        if output_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            output_path = os.path.join(
                base_dir, 'data', 'generated',
                f"{self.title}_{int(time.time())}.xlsx"
            )

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '测试内容'

        self._write_test_content(ws)
        self._write_info(wb.create_sheet('测试Case基本信息'))
        self._write_standards(wb.create_sheet('编写规范'))
        self._write_review(wb.create_sheet('评审流程'))
        self._write_checklist(wb.create_sheet('通用测试注意项'))
        self._write_tech(wb.create_sheet('技术备注'))
        self._write_dev(wb.create_sheet('策划程序新增Case'))
        self._write_options(wb.create_sheet('选项'))

        wb.save(output_path)
        return output_path

    # ---- 主内容 Sheet ----
    def _write_test_content(self, ws):
        # 表头
        for col, h in enumerate(HEADERS, 1):
            cell = ws.cell(row=1, column=col, value=h)
            style_cell(cell, HEADER_FONT, HEADER_FILL, CENTER_ALIGN)

        # 列宽
        for c, w in COL_WIDTHS.items():
            ws.column_dimensions[get_column_letter(c)].width = w

        if not self.cases:
            ws.freeze_panes = 'A2'
            return

        # 写入数据
        row = 2
        prev_g = None
        for case in self.cases:
            b, c, d, e, f, g, h, priority, remark = case

            ws.cell(row=row, column=1, value=row - 1)  # 自动编号
            if b: ws.cell(row=row, column=2, value=b)
            if c: ws.cell(row=row, column=3, value=c)
            if d: ws.cell(row=row, column=4, value=d)
            if e: ws.cell(row=row, column=5, value=e)
            if f: ws.cell(row=row, column=6, value=f)
            if g:
                ws.cell(row=row, column=7, value=g)
                prev_g = g
            elif prev_g:
                ws.cell(row=row, column=7, value=prev_g)
            if h: ws.cell(row=row, column=8, value=h)
            if priority: ws.cell(row=row, column=11, value=priority)
            if remark: ws.cell(row=row, column=12, value=remark)
            row += 1

        last_row = row - 1

        # 样式
        for r in range(2, last_row + 1):
            row_fill = ALT_FILL if (r % 2 == 0) else WHITE_FILL
            for col in range(1, 13):
                cell = ws.cell(row=r, column=col)
                if col in (1, 11):
                    style_cell(cell, NORMAL_FONT, row_fill, CENTER_ALIGN)
                elif col in (7, 8):
                    style_cell(cell, NORMAL_FONT, row_fill, WRAP_ALIGN)
                else:
                    style_cell(cell, NORMAL_FONT, row_fill, VERT_CENTER)

        # B/C/D/E/F 跨TC合并
        for col_idx in [2, 3, 4, 5, 6]:
            group_start = 2
            for r in range(3, last_row + 2):
                if r <= last_row and not ws.cell(row=r, column=col_idx).value:
                    continue
                if (r - 1 > group_start
                        and ws.cell(row=group_start, column=col_idx).value):
                    ws.merge_cells(start_row=group_start, start_column=col_idx,
                                   end_row=r - 1, end_column=col_idx)
                group_start = r

        # G列合并
        group_start = 2
        for r in range(3, last_row + 2):
            if r <= last_row and not ws.cell(row=r, column=7).value:
                continue
            if r - 1 > group_start:
                ws.merge_cells(start_row=group_start, start_column=7,
                               end_row=r - 1, end_column=7)
            group_start = r

        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = f'A1:L{last_row}'

    # ---- 基本信息 Sheet ----
    def _write_info(self, ws):
        info = [
            ('项目名', '二合游戏'),
            ('活动名称', self.title or '未命名'),
            ('Case状态', '编写中'),
            ('需求负责人', ''),
            ('Case负责人', self.author),
            ('评审人员', self.reviewer),
            ('评审会议纪要', ''),
        ]
        style_cell(ws.cell(row=1, column=1, value='测试Case基本信息'), TITLE_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.merge_cells('A1:B1')
        for i, (k, v) in enumerate(info, 2):
            style_cell(ws.cell(row=i, column=1, value=k), SECTION_FONT, SECTION_FILL, WRAP_ALIGN)
            style_cell(ws.cell(row=i, column=2, value=v), NORMAL_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.column_dimensions['A'].width = 20
        ws.column_dimensions['B'].width = 40

    # ---- 编写规范 Sheet ----
    def _write_standards(self, ws):
        rules = [
            ('编号', '新增=最大编号+1，废弃=置灰底色不删除，临时不用=双删除线'),
            ('模块', '一般不超过4级（B/C/D/E），复杂功能拆分页签'),
            ('步骤', '不超过5步，用换行符分隔。举具体例子，设具体参数值'),
            ('期望', '一行一结果，必须可量化/可观测'),
            ('优先级', '0=阻塞，1=核心逻辑，2=重要分支，3=一般场景，4=边缘场景'),
            ('操作步骤模式', '配置参数式 / 多章并列对比式 / 条件前置式 / 环境构造式 / 跨会话式'),
            ('期望结果模式', '配置值对应式 / UI显式正确式 / 逻辑推导式 / 公式计算式 / 边界分支式'),
        ]
        style_cell(ws.cell(row=1, column=1, value='编写规范'), TITLE_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.merge_cells('A1:C1')
        for col, h in enumerate(['规范项', '说明', ''], 1):
            style_cell(ws.cell(row=2, column=col, value=h), HEADER_FONT, HEADER_FILL, CENTER_ALIGN)
        for i, (k, v) in enumerate(rules, 3):
            style_cell(ws.cell(row=i, column=1, value=k), NORMAL_FONT, WHITE_FILL, WRAP_ALIGN)
            style_cell(ws.cell(row=i, column=2, value=v), NORMAL_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.column_dimensions['A'].width = 18
        ws.column_dimensions['B'].width = 80

    # ---- 评审流程 Sheet ----
    def _write_review(self, ws):
        steps = [
            ('预习', '评审前每人预习用例，标记疑问点'),
            ('发言', '按模块顺序逐条过，每人发言'),
            ('记录', '记录评审意见，当场修改或标记待办'),
            ('确认', '修改完成后发起人确认，通过后Case状态改为"已评审"'),
        ]
        style_cell(ws.cell(row=1, column=1, value='评审流程'), TITLE_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.merge_cells('A1:B1')
        for col, h in enumerate(['步骤', '说明'], 1):
            style_cell(ws.cell(row=2, column=col, value=h), HEADER_FONT, HEADER_FILL, CENTER_ALIGN)
        for i, (s, d) in enumerate(steps, 3):
            style_cell(ws.cell(row=i, column=1, value=s), SECTION_FONT, SECTION_FILL, CENTER_ALIGN)
            style_cell(ws.cell(row=i, column=2, value=d), NORMAL_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.column_dimensions['A'].width = 12
        ws.column_dimensions['B'].width = 60

    # ---- 通用测试注意项 Sheet ----
    def _write_checklist(self, ws):
        items = [
            ('屏幕适配', '异形屏/横竖屏/分屏切换，UI不重叠不错位'),
            ('特殊操作', '快速连续点击/多点触控/杀进程重进/网络切换/低内存'),
            ('数据打点', '关键操作的数据上报确认'),
            ('覆盖安装', '旧版本覆盖安装新版本，数据不丢失不异常'),
            ('多语言', '各语言文案不截断不对齐错位'),
            ('性能', '长时间运行内存不泄漏，帧率稳定'),
        ]
        style_cell(ws.cell(row=1, column=1, value='通用测试注意项'), TITLE_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.merge_cells('A1:B1')
        for col, h in enumerate(['类别', '检查项'], 1):
            style_cell(ws.cell(row=2, column=col, value=h), HEADER_FONT, HEADER_FILL, CENTER_ALIGN)
        for i, (c, t) in enumerate(items, 3):
            style_cell(ws.cell(row=i, column=1, value=c), SECTION_FONT, SECTION_FILL, CENTER_ALIGN)
            style_cell(ws.cell(row=i, column=2, value=t), NORMAL_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.column_dimensions['A'].width = 16
        ws.column_dimensions['B'].width = 60

    # ---- 技术备注 Sheet ----
    def _write_tech(self, ws):
        style_cell(ws.cell(row=1, column=1, value='技术备注'), TITLE_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.merge_cells('A1:B1')
        style_cell(ws.cell(row=2, column=1, value='（操作步骤/期望结果简要记录）'), SMALL_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.column_dimensions['A'].width = 40
        ws.column_dimensions['B'].width = 40

    # ---- 策划程序新增Case Sheet ----
    def _write_dev(self, ws):
        style_cell(ws.cell(row=1, column=1, value='策划程序新增Case'), TITLE_FONT, WHITE_FILL, WRAP_ALIGN)
        ws.merge_cells('A1:F1')
        for col, h in enumerate(['编号', '模块', '新增测试项', '是否程序自测', 'QA结果', '发起人'], 1):
            style_cell(ws.cell(row=2, column=col, value=h), HEADER_FONT, HEADER_FILL, CENTER_ALIGN)
        for c in range(1, 7):
            ws.column_dimensions[get_column_letter(c)].width = 18

    # ---- 选项 Sheet ----
    def _write_options(self, ws):
        style_cell(ws.cell(row=1, column=1, value='选项'), TITLE_FONT, WHITE_FILL, WRAP_ALIGN)
        opts = {
            '是否需要测试': ['是', '否'],
            'QA结果': ['通过', '失败', '阻塞', '跳过'],
            '测试结果': ['通过', '失败', '阻塞', '跳过'],
            '优先级': ['0', '1', '2', '3', '4'],
        }
        r = 2
        for cat, vals in opts.items():
            style_cell(ws.cell(row=r, column=1, value=cat), SECTION_FONT, SECTION_FILL, WRAP_ALIGN)
            r += 1
            for v in vals:
                style_cell(ws.cell(row=r, column=1, value=v), NORMAL_FONT, WHITE_FILL, WRAP_ALIGN)
                r += 1
            r += 1
        ws.column_dimensions['A'].width = 20
