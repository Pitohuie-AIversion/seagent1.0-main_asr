---
name: technical-report
description: Standardized engineering report writer. Use when creating technical documentation, test verification reports, architecture summaries, or synthesizing project findings into formal stakeholder-ready documents.
---

# Technical Report Generation Skill

This skill provides standardized procedures, structural guidelines, and tone rules for generating formal engineering, testing, and architecture reports for AI agent projects.

## When to Use

Trigger this skill whenever the user requests:
- Writing a formal engineering test report or verification document.
- Creating an architecture review or technical summary.
- Exporting project verification findings into Markdown and PDF formats.

---

## Key Principles & Tone Guidelines

1. **Strict Fact-Based Engineering Phrasing**:
   - Explicitly distinguish between:
     - Verified empirical facts (backed by actual test outputs or code diffs);
     - Inferences based on logs/tracebacks;
     - Unverified assumptions;
     - Recommended action items.
   - Do NOT use exaggerated or overly confident language (e.g., "完美解决", "证据确凿！", "绝对不", "准予交付", "100% 跑通").
   - Use humble, objective, neutral language (e.g., "目前在测试环境下完成双向通信功能验证，120 项单元测试全部执行通过").

2. **No Emoji & Informal Conversation Markers**:
   - Do NOT include markdown decorative emojis (e.g., 🚀, 📄, 📡, ⏱️, 📊, 🎉, ✅).
   - Maintain a clean, academic, professional technical report aesthetic.

3. **Standard Report Structure**:
   - **Header & Metadata Table**: Project name, module name, environment, test date, pass/fail result.
   - **Section 1: Executive Summary & Test Findings**: Concise overview of the scope and quantitative results.
   - **Section 2: Architecture & Design Rationale**: Decoupling, security, memory snapshot, and protocol alignment rationale.
   - **Section 3: Library & Function API Mapping**: Structured table listing modules, classes, and specific function APIs used.
   - **Section 4: Empirical Logs & Verification Details**: Verifiable, clean log outputs from actual execution scripts.
   - **Section 5: Automated Test Suite Breakdown**: Categorized table showing pass/fail status and case counts.

4. **Dual Output Formats**:
   - Export standard clean GitHub-Flavored Markdown report file.
   - Compile formal ReportLab PDF using system TrueType Chinese fonts (e.g. `wqy-zenhei.ttc`).
