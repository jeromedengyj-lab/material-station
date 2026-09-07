// 剧目搜索匹配纯函数（供任务 worker 与回归测试共用）
// 定位策略：剧名（先精确后去标点模糊）→ 内容类型标签（网文/漫剧/短剧）→ 集数精确
// 解决：同名不同标签（网文/漫剧/短剧混在一起）、同名同标签不同集数（剧情68集 vs 科幻末世115集）

export const CONTENT_TYPE_LABELS = {manju: '漫剧', wangwen: '网文', duanju: '短剧'};

export function bookTypeLabels(b) {
  return [b?.content_tab_name, b?.tab_name, b?.content_type_name, b?.book_type_name, b?.type_name, b?.category_name]
    .map((x) => String(x ?? '').trim())
    .filter(Boolean);
}

export function bookMatchesType(b, typeKey) {
  if (!typeKey) return true; // 不限类型
  const label = CONTENT_TYPE_LABELS[typeKey];
  if (!label) return true; // 未知类型不拦截
  return bookTypeLabels(b).some((x) => x === label || x.includes(label));
}

export function bookEpisodes(b) {
  return Number(b?.chapter_num || b?.chapter_count || b?.total_chapter_num) || 0;
}

/**
 * 在搜索结果中挑选唯一目标书。
 * @param books 搜索接口返回的 book_list
 * @param title 目标剧名
 * @param opts {contentType?, expectedEpisodes?, normalize?}
 * @returns {byTitle, filtered, candidates}
 *   byTitle: 剧名精确/模糊命中（未过滤类型）
 *   filtered: 剧名命中 + 类型过滤 + 集数过滤
 *   candidates: filtered 的摘要（供报错信息展示）
 */
export function pickUniqueBook(books, title, opts = {}) {
  const norm = opts.normalize || ((s) => String(s ?? ''));
  const target = String(title ?? '').trim();
  const strict = books.filter((x) => String(x?.book_name || x?.name || '').trim() === target);
  const byTitle = strict.length ? strict : books.filter((x) => norm(x?.book_name || x?.name) === norm(target));
  let filtered = byTitle.filter((x) => bookMatchesType(x, opts.contentType));
  const wantEpisodes = Number(opts.expectedEpisodes) || 0;
  if (wantEpisodes > 0) {
    filtered = filtered.filter((x) => bookEpisodes(x) === wantEpisodes);
  }
  return {
    byTitle,
    filtered,
    candidates: filtered.map((x) => ({
      name: String(x?.book_name || x?.name || ''),
      episodes: bookEpisodes(x),
      book_id: String(x?.book_id || ''),
    })),
  };
}
