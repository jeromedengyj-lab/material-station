# -*- coding: utf-8 -*-
import zipfile, os, time

src = r'D:\漫剧剪辑工具\素材准备站'
out = r'D:\素材准备站\素材准备站_绿色版.zip'
log = r'D:\素材准备站\zip_rebuild.log'
t0 = time.time()
n = 0
with open(log, 'w', encoding='utf-8') as lg:
    def w(msg):
        lg.write(msg + '\n')
        lg.flush()
    w(f'开始打包 {time.strftime("%H:%M:%S")}')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in ('.git', 'browser_profiles')]
            for f in files:
                p = os.path.join(root, f)
                arc = os.path.relpath(p, src).replace(os.sep, '/')
                z.write(p, arc)
                n += 1
                if n % 500 == 0:
                    w(f'{n} 条目 {time.time()-t0:.0f}s')
    w(f'写入完成 {n} 条目 {time.time()-t0:.0f}s')
    bad = z.testzip()
    w('CRC: ' + ('OK' if bad is None else bad))
    names = z.namelist()
    w('条目数: ' + str(len(names)))
    m = next((x for x in names if x.endswith('_internal/platform_adapter/task-platform-download.mjs')), None)
    w('mjs路径: ' + str(m))
    t = z.read(m).decode('utf-8')
    w('safeEv轮询: ' + str('try{state=await safeEv' in t))
    w('申词兜底: ' + str('submit_verified_via_ledger' in t))
    w('pending判定: ' + str("verify.state==='pending'||verify.state==='approved'" in t))
    w('平台配置: ' + str(any(x.endswith('data/platforms.json') for x in names)))
    w('发文类型: ' + str(any(x.endswith('data/post_type.json') for x in names)))
    w('完成')
