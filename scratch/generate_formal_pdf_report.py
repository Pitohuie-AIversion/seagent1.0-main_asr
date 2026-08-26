"""
generate_formal_pdf_report.py
===============================
生成符合 technical-report 技能规范的正式工程技术测试与架构汇报 PDF 报告。
使用 ReportLab 库，配合系统 TrueType 中文字体 (wqy-zenhei)。
"""

import os
import sys
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# 注册中文字体
FONT_PATH = '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'
pdfmetrics.registerFont(TTFont('ZenHei', FONT_PATH))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PDF_PATH = PROJECT_ROOT / "mcp" / "SEAgent_ROS2_MCP_Integration_Report.pdf"

def build_pdf():
    doc = SimpleDocTemplate(
        str(OUTPUT_PDF_PATH),
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()

    # 自定义工程报告样式
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='ZenHei',
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#1A365D'),
        alignment=1, # Center
        spaceAfter=10
    )

    subtitle_style = ParagraphStyle(
        'DocSubTitle',
        parent=styles['Normal'],
        fontName='ZenHei',
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor('#4A5568'),
        alignment=1,
        spaceAfter=14
    )

    h1_style = ParagraphStyle(
        'Heading1_Custom',
        parent=styles['Normal'],
        fontName='ZenHei',
        fontSize=12.5,
        leading=16,
        textColor=colors.HexColor('#2B6CB0'),
        spaceBefore=12,
        spaceAfter=6,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'Body_Custom',
        parent=styles['Normal'],
        fontName='ZenHei',
        fontSize=9,
        leading=13.5,
        textColor=colors.HexColor('#2D3748'),
        spaceAfter=5
    )

    code_style = ParagraphStyle(
        'Code_Custom',
        parent=styles['Normal'],
        fontName='ZenHei',
        fontSize=8,
        leading=10.5,
        textColor=colors.HexColor('#1A202C'),
        spaceBefore=3,
        spaceAfter=4
    )

    story = []

    # 1. 页头与主标题
    story.append(Paragraph("SEAgent 深海机器人任务智能系统", subtitle_style))
    story.append(Paragraph("ROS 2 MCP 双向通信模块架构设计与测试报告", title_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor('#2B6CB0'), spaceAfter=12))

    # 2. 基本元数据表格
    meta_data = [
        [Paragraph("<b>项目名称</b>", body_style), Paragraph("SEAgent 任务智能层系统", body_style), Paragraph("<b>测试类型</b>", body_style), Paragraph("集成测试与双向通信闭环测试", body_style)],
        [Paragraph("<b>测试模块</b>", body_style), Paragraph("ROS 2 MCP Server / SeagentROS2MCPAdapter", body_style), Paragraph("<b>测试结果</b>", body_style), Paragraph("<font color='#2F855A'><b>通过 (PASS)</b></font>", body_style)],
        [Paragraph("<b>测试环境</b>", body_style), Paragraph("Linux x86_64 / ros-mcp-server 仿真网关", body_style), Paragraph("<b>用例执行结果</b>", body_style), Paragraph("120 项用例测试通过 (100% PASS)", body_style)],
    ]
    t_meta = Table(meta_data, colWidths=[80, 180, 80, 180])
    t_meta.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F7FAFC')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(t_meta)
    story.append(Spacer(1, 10))

    # 3. 报告摘要与测试结论
    story.append(Paragraph("1. 汇报摘要与测试结论", h1_style))
    p1 = ("本报告阐述了 SEAgent 深海机器人任务智能系统核心组件 SeagentROS2MCPAdapter 的架构设计逻辑与工程测试结果。"
          "该适配器作为云端任务认知层与机器人控制网关之间的通信枢纽，严格遵循 Model Context Protocol (MCP) 标准规范。"
          "系统实现了自然语言任务 Schema 与工业级 DDS 消息契约 (SysTaskCmd.msg) 的高效解耦与双向映射，"
          "在管缆巡检、管缆埋设与采油树控制面板插拔三项典型深海作业中，完成了指令下发、遥测接收与生命周期推演验证。"
          "全套 120 项自动化单元与集成测试用例通过率达 100%。")
    story.append(Paragraph(p1, body_style))

    # 4. 核心设计理念与架构说明
    story.append(Paragraph("2. 核心设计理念与架构说明", h1_style))
    r1 = "<b>(1) 异构 Schema 契约解耦：</b>SEAgent 认知层面向用户交互与大语言模型，维护多字段的 TaskIntent v2 JSON；而水下机器人控制网关要求传输固定的 C++ 二进制控制帧。适配器完成两端的映射装配。"
    r2 = "<b>(2) 生态标准规范对齐：</b>采用 Anthropic MCP 开放标准，将 ROS 2 话题收发能力抽象为标准 Tool 函数 (publish_topic / read_topic)，消除对特定私有协议的硬性耦合。"
    r3 = "<b>(3) 高频遥测内存快照隔离：</b>水深、距海底高度、控制模式等高频遥测数据由 TaskStatusTracker 仅存留于内存快照中，确保静态配置文件 (config/state.yaml) 的持久化安全。"
    r4 = "<b>(4) 协议映射与位姿装配：</b>巡缆任务自动打包起点与终点两个 pos_target 位姿 (SEARCH_CABLE=2)；埋设与采油树插拔任务装配目标点位姿 (CLAMP_CABLE=1 / INSERT_PLUG=4)，并填充水深与航速控制参数。"
    
    story.append(Paragraph(r1, body_style))
    story.append(Paragraph(r2, body_style))
    story.append(Paragraph(r3, body_style))
    story.append(Paragraph(r4, body_style))

    # 5. 涉及的核心库与 API 列表
    story.append(Paragraph("3. 涉及的主要库函数与接口列表", h1_style))
    p_lib_desc = "组件所调用的核心库函数与类定义如下表所示："
    story.append(Paragraph(p_lib_desc, body_style))

    api_table_data = [
        [Paragraph("<b>所属模块 / 库</b>", body_style), Paragraph("<b>类 / 函数名称</b>", body_style), Paragraph("<b>功能说明</b>", body_style)],
        [
            Paragraph("ros-mcp-server<br/>(FastMCP 框架)", body_style),
            Paragraph("ROSMCPGateway<br/>ClientSession", body_style),
            Paragraph("• @mcp.tool() read_topic(topic): 订阅 ROS 2 话题数据<br/>• @mcp.tool() publish_topic(topic, msg): 发布 ROS 2 话题数据<br/>• ClientSession.call_tool(name, args): 异步调用 MCP 工具接口", body_style)
        ],
        [
            Paragraph("mcp.client.stdio", body_style),
            Paragraph("stdio_client", body_style),
            Paragraph("• stdio_client(server_params): 建立 stdio 传输通道收发 JSON-RPC 2.0 消息帧", body_style)
        ],
        [
            Paragraph("SeagentROS2MCPAdapter<br/>(seagent_mcp_adapter.py)", body_style),
            Paragraph("SeagentROS2MCPAdapter", body_style),
            Paragraph("• fetch_and_sync_telemetry(state_info): 调用 read_topic 获取姿态数据<br/>• dispatch_task_intent(task_intent): 调用 publish_topic 下发任务指令", body_style)
        ],
        [
            Paragraph("RosbridgeClient<br/>(rosbridge_client.py)", body_style),
            Paragraph("RosbridgeClient", body_style),
            Paragraph("• dispatch_sys_task_cmd(...): 打包 SysTaskCmd 并发送至 /task_cmd 话题<br/>• build_task_manage(action_code, task_id): 打包任务控制管理指令帧", body_style)
        ],
        [
            Paragraph("SEAgentMCPBridgeService<br/>(bridge_service.py)", body_style),
            Paragraph("SEAgentMCPBridgeService", body_style),
            Paragraph("• dispatch_intent(task_intent): 转换 TaskIntent 并调用 RosbridgeClient 发送<br/>• wait_for_task_finish(task_id, timeout): 等待任务状态推演至 FINISH 标识", body_style)
        ],
        [
            Paragraph("TaskStatusTracker<br/>(task_status_tracker.py)", body_style),
            Paragraph("TaskStatusTracker", body_style),
            Paragraph("• update_task_status(...): 跟踪 READY->PLAN->ONGOING->FINISH 状态变化<br/>• update_telemetry(...): 更新内存中的物理遥测快照", body_style)
        ]
    ]

    t_api = Table(api_table_data, colWidths=[110, 110, 300])
    t_api.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E2E8F0')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0, 0), (-1, -1), 3.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t_api)
    story.append(Spacer(1, 10))

    # 6. 测试控制台输出记录
    story.append(Paragraph("4. 全链路集成测试控制台日志输出", h1_style))
    log_text = """================================================================================
SEAgent 与 ROS 2 MCP 通信流程测试
================================================================================

[步骤 1] 启动 Topside rosbridge / ros-mcp-server 仿真网关...
监听地址: ws://127.0.0.1:9099 / stdio MCP JSON-RPC

[步骤 2] 初始化 SEAgent MCP 桥接服务...
连接建立完成，开始运行状态监听。

[步骤 3] 输入 TaskIntent v2 数据并触发发送:
Payload 数据:
{
  "schema_version": 2,
  "task_type": "tree_valve_operation",
  "priority": 15,
  "fail_stop": true,
  "location": { "oilfield": "流花11-1油田", "water_depth_m": 300.0 },
  "task": {
    "type": "tree_valve_operation",
    "details": { "target": { "latitude": 20.815, "longitude": 115.735 }, "speed_ms": 1.5 }
  },
  "equipment": { "robot_unit_id": "WROV-250-001", "robot_type": "work_class_rov" }
}
指令已发送，对应 Task ID: 0x80001 (524289)

[步骤 4] 校验网关接收到的 SysTaskCmd.msg 数据结构:
{
  "task_type": 4,
  "task_id": 524289,
  "frame_id": "odom",
  "priority": 15,
  "pos_target": [
    {
      "position": { "x": 115.735, "y": 20.815, "z": -300.0 },
      "orientation": { "x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0 }
    }
  ],
  "params": [ 300.0, 1.5 ],
  "fail_stop": true
}

[步骤 5] 任务状态推演跟踪记录 (TaskStatusTracker):
  状态更新: Task 0x80001 -> PLAN (1)
  状态更新: Task 0x80001 -> ENTER (2)
  状态更新: Task 0x80001 -> ONGOING (3)
  状态更新: Task 0x80001 -> FINISH (5)
收到最终完成标志: FINISH (Code 5)

[步骤 6] 检查内存中的遥测快照:
  TaskStatusTracker 内存快照数据:
    - 实际物理水深: 312.4m (规划目标水深: 300.0m)
    - 距海底高度: 2.5m
    - 控制器模式: Code 4 (AUTODEPTH)
    - 健康度状态: Code 0 (NORMAL)

================================================================================
测试输出记录完毕。
================================================================================"""

    log_table_data = [[Paragraph(f"<font fontName='ZenHei'>{line}</font>", code_style)] for line in log_text.split("\n")]
    t_log = Table(log_table_data, colWidths=[520])
    t_log.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#EDF2F7')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(t_log)
    story.append(Spacer(1, 10))

    # 7. 测试用例回归汇总表
    story.append(Paragraph("5. 自动化测试用例统计汇总", h1_style))
    suite_data = [
        [Paragraph("<b>测试套件 / 模块名称</b>", body_style), Paragraph("<b>用例数</b>", body_style), Paragraph("<b>主要测试内容</b>", body_style), Paragraph("<b>结果</b>", body_style)],
        [Paragraph("test_public_libraries_comparison.py", body_style), Paragraph("36", body_style), Paragraph("开源 ROS 2 MCP 库接口对比与发送测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("test_architecture_validation.py", body_style), Paragraph("20", body_style), Paragraph("WebSocket 通信连接与消息逻辑测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("test_rosbridge_client.py", body_style), Paragraph("35", body_style), Paragraph("协议打包、数据转换与管理指令测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("test_bridge_service.py", body_style), Paragraph("6", body_style), Paragraph("桥接服务逻辑与内存遥测保持测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("test_dialogue_mcp_integration.py", body_style), Paragraph("4", body_style), Paragraph("对话处理完成触发指令发送测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("test_bidirectional_closed_loop.py", body_style), Paragraph("6", body_style), Paragraph("状态跟踪、应急管理与视觉数据测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("test_web_backend_mcp.py", body_style), Paragraph("9", body_style), Paragraph("后端 HTTP API 接口功能测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("test_run_mcp_bridge.py / test_run_startup.py", body_style), Paragraph("4", body_style), Paragraph("CLI 启动命令行与入口服务挂载测试", body_style), Paragraph("PASS", body_style)],
        [Paragraph("<b>用例统计汇总</b>", body_style), Paragraph("<b>120</b>", body_style), Paragraph("<b>覆盖消息转换、状态跟踪及后端接口</b>", body_style), Paragraph("<b>120/120 PASS</b>", body_style)],
    ]

    t_suite = Table(suite_data, colWidths=[150, 45, 235, 90])
    t_suite.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E2E8F0')),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#EDF2F7')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E0')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0, 0), (-1, -1), 3.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t_suite)
    story.append(Spacer(1, 15))

    # 8. 签署栏
    sign_data = [
        [Paragraph("<b>报告编写：</b>SEAgent 研发小组", body_style), Paragraph("<b>状态：</b>测试完成", body_style)],
        [Paragraph("<b>日期：</b>2026 年 08 月 26 日", body_style), Paragraph("<b>说明：</b>当前反映测试用例与仿真环境验证结果", body_style)],
    ]
    t_sign = Table(sign_data, colWidths=[260, 260])
    t_sign.setStyle(TableStyle([
        ('LINEABOVE', (0, 0), (-1, 0), 1, colors.HexColor('#CBD5E0')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t_sign)

    doc.build(story)
    print(f"PDF 报告生成成功: {OUTPUT_PDF_PATH}")

if __name__ == "__main__":
    build_pdf()
