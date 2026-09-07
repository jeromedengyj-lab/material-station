// 三端别名候选校验（纯函数，供任务 worker 与回归测试共用）
// 规则：
//   - 所有候选必须恰好 4 个中文字、互不重复（手动/自动统一强制，平台侧硬性要求）
//   - 自动生成候选：还必须与任务固定字（前缀/后缀）匹配
//   - 手动别名（文档/界面直接提供）：不受前缀/后缀模式限制，原样使用
//     —— 用户明确要求"文档里提供的别名不受开关影响，按提供的来"

const AFFIX_LABELS = {prefix: '以“{p}”开头', suffix: '以“{p}”结尾'};

function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function validateCandidates(aliases, aliasPrefix, aliasMode, manual) {
  const list = [...(aliases || [])].map((x) => String(x || '').trim()).filter(Boolean);
  if (list.length < 1) return {ok: false, message: '至少需要1个候选别名'};
  const badShape = list.find((x) => !/^[\u4e00-\u9fff]{4}$/.test(x));
  if (badShape) return {ok: false, message: `候选「${badShape}」必须恰好是4个中文汉字`};
  if (new Set(list).size !== list.length) return {ok: false, message: '候选必须互不重复'};
  // 手动别名：不受前缀/后缀限制，原样使用
  if (manual === true) return {ok: true};
  if (!/^[\u4e00-\u9fff]{2}$/.test(String(aliasPrefix || ''))) {
    return {ok: false, message: '别名固定字必须恰好是2个中文汉字'};
  }
  const p = escapeRegExp(aliasPrefix);
  const pattern = aliasMode === 'suffix'
    ? new RegExp(`^[\\u4e00-\\u9fff]{2}${p}$`)
    : new RegExp(`^${p}[\\u4e00-\\u9fff]{2}$`);
  const label = (AFFIX_LABELS[aliasMode] || AFFIX_LABELS.prefix).replace('{p}', aliasPrefix);
  const mismatch = list.find((x) => !pattern.test(x));
  if (mismatch) return {ok: false, message: `候选「${mismatch}」必须${label}且恰好四个汉字`};
  return {ok: true};
}
