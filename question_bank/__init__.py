"""题库录入与管理：数据库真源层（docs/question-bank-management-design.md）。

阶段 B（一题完整闭环）实现：
- SQLite 管理题目当前内容、修订、人工修改、审核与发布事实；
- AI 候选接收接口（幂等回执、按题定位错误）；
- Review UI 编辑闭环（三块正文 + 图片引用，base_revision 乐观并发）；
- 指定修订导出 teacher/student 与备份恢复。

内容模型为“正文文本 + 结构化管理字段”（设计文档 §9）：
题目/答案/解答三块正文，图片以 asset: 标记引用，不强制深结构。
"""

from question_bank.errors import QbError, ReceiptError

__all__ = ["QbError", "ReceiptError"]
