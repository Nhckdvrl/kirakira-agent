/** 单一 HTML 转义实现；面板与交付形态分类共用，避免两份不一致的转义。 */
export const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
