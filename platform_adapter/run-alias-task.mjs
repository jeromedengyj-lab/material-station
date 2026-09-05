import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const taskFile=path.resolve(String(process.argv[2]||''));
if(!taskFile||!fs.existsSync(taskFile)){
  console.error(`三端别名任务文件不存在：${taskFile||'未提供'}`);
  process.exit(2);
}
let task;
try{task=JSON.parse(fs.readFileSync(taskFile,'utf8'))}
catch(error){console.error(`三端别名任务文件无效：${error.message}`);process.exit(2)}
const title=String(task.title||'').trim(),batch=String(task.batch_id||'').trim();
const rows=Array.isArray(task.candidate_rows)?task.candidate_rows:[];
if(!title||!batch||rows.length<1){
  console.error('三端别名任务缺少剧名、批次号或候选。');
  process.exit(2);
}
const toolDir=path.dirname(fileURLToPath(import.meta.url));
const worker=path.join(toolDir,'task-platform-download.mjs');
const run=(script,args)=>spawnSync(process.execPath,[...process.execArgv,script,...args],{stdio:'inherit',windowsHide:false}).status??1;
process.exit(run(worker,['--task-file',taskFile,'--triple-platform','--watch','--local-task-center']));
