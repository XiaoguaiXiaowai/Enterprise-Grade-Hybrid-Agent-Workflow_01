"""提示词集中管理（整改⑧）：分类器 / 多 Agent / 上下文模板统一在此维护。

目的：便于版本管理与替换（配合 PROMPT_VERSION 单一常量）；业务代码不内嵌长文案。
模块划分：
- classifier.py：意图/风险规则表 + 相关性校验 / 分类 / 重写三段系统提示词
- agents.py：多 Agent（Triage / Knowledge / Identity / Change）的 system prompt
- context.py：上下文装配的约束文案与 <goal><state>… 渲染模板
"""
