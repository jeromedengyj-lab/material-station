import path from 'node:path';

export function resolveSharedRoot() {
  const configured = String(process.env.MANJU_SHARED_ROOT || '').trim();
  if (configured) return configured;
  const toolRoot = String(process.env.MANJU_TOOL_ROOT || '').trim();
  if (!toolRoot) throw new Error('未提供 MANJU_TOOL_ROOT，无法确定本地数据目录');
  return path.join(toolRoot, 'data');
}
