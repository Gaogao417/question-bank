/* Shared review predicates: OR within each checkbox group, AND across groups. */
"use strict";
const QuestionReview = Object.freeze({
  defaults: () => ({
    issues: true,
    clean: true,
    passed: false,
    pending: true,
    image: true,
    review_ai: true,
    ai: false,
  }),
  isPassed: (item) => (item.review_status || item.review_state) === "approved",
  hasIssues: (item) =>
    Boolean(
      item.has_open_issues ?? item.open_issues ?? item.open_blocking_issues,
    ),
  matches(item, filters, query = "") {
    const issues = this.hasIssues(item),
      passed = this.isPassed(item);
    return (
      (issues ? filters.issues : filters.clean) &&
      (passed ? filters.passed : filters.pending) &&
      (passed ||
        !item.review_task ||
        item.review_task === "review" ||
        filters[item.review_task]) &&
      `${item.question_number} ${item.item_id}`
        .toLowerCase()
        .includes(query.trim().toLowerCase())
    );
  },
  taskLabel(item) {
    return (
      { image: "待截图", ai: "AI 待处理", review_ai: "待查看 AI 修订" }[
        item.review_task
      ] || ""
    );
  },
  label(item) {
    const labels = {
      auto_approved: "系统自动通过",
      approved: "人工已通过",
      unreviewed: "未审阅",
      ai_revised: "AI 已修改，待复审",
      modified: "已修改，待复审",
      rejected: "需修改",
    };
    if (
      item.review_workflow === "approved" &&
      item.approval_source !== "human"
    ) {
      return item.approval_source === "ai" ? "AI 审核通过" : "系统已通过";
    }
    return (
      labels[item.review_workflow] ||
      {
        approved: "已通过",
        rejected: "需修改",
        stale: "已修改，待复审",
        pending: "未审阅",
      }[item.review_status || item.review_state] ||
      "未审阅"
    );
  },
});
