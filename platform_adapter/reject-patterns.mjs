// 三端别名申请：平台拒绝文案识别（纯函数，供任务 worker 与回归测试共用）
// 说明：平台在别名推广弹窗中，可能以红字提示形式拒绝候选（相似度高/重复申请等）。
// 这些提示必须被识别为“候选失败”，否则提交会被误判为成功，任务将卡在等待审核。
//
// 同一份拒绝文案规则需要同时作用于两处：
//   1. Node 侧（matchRejectText）：mjs 主流程与回归测试；
//   2. 浏览器页面侧（REJECT_MATCH_JS）：CDP Runtime.evaluate 注入到页面里检测弹窗红字。
// 因此这里以“字符串数组”为唯一事实来源，两个产物都由它生成。

export const REJECT_TEXT_SOURCES = [
  // 重复申请类（原有）
  '你已申请此别名',
  '已有相同书名存在',
  '别名已存在',
  '该别名已被使用',
  '该别名已被他人申请',
  '已被他人申请',
  '他人已申请',
  '此别名已被',
  '请勿重复申请',
  // 相似度过高/侵权类（本次新增：平台红字提示，未识别会导致误判提交成功）
  '与热门作品',
  '相似度高',
  '相似度过高',
  '涉嫌侵权',
  '侵权风险',
  '与已有作品相似',
];

export const REJECT_TEXT_PATTERNS = REJECT_TEXT_SOURCES.map((source) => new RegExp(source));

// 命中返回匹配到的正则（truthy），未命中返回 null
export function matchRejectText(text) {
  const hay = String(text || '');
  for (const pattern of REJECT_TEXT_PATTERNS) {
    if (pattern.test(hay)) return pattern;
  }
  return null;
}

// 浏览器页面侧内联检测函数源码：直接 new RegExp 从字符串构造，避免转义差异
export const REJECT_MATCH_JS = `(text)=>{const pats=${JSON.stringify(REJECT_TEXT_SOURCES)}.map(s=>new RegExp(s));for(const p of pats)if(p.test(text||''))return p;return null}`;
