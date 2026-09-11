# -*- coding: utf-8 -*-
"""验证加密钥版产物 exe 是否真的含密钥逻辑，然后从该产物重打 zip。"""
import zipfile, os, time

install = r'D:\漫剧剪辑工具\material-station\build_station_install\素材准备站'
exe = os.path.join(install, '素材准备站.exe')

# 1) exe 二进制含密钥相关字符串（PyInstaller 打包后字符串仍在二进制里）
blob = open(exe, 'rb').read()
keys = ['授权密钥', '设备码', 'license.dat', 'licensed_mode.flag']
kb = [k.encode('utf-8') for k in keys]
found = {k: (b in blob) for k, b in zip(keys, kb)}
print('exe 密钥字符串:', found)
print('exe 大小:', len(blob))

# 2) 本机免激活版（build_station_portable）应不含 FORCE_LICENSE True 行为（保持 False 构建）
port_exe = r'D:\漫剧剪辑工具\material-station\build_station_portable\素材准备站\素材准备站.exe'
pblob = open(port_exe, 'rb').read()
print('便携版 exe 密钥字符串:', {k: (b in pblob) for k, b in zip(keys, kb)})

# 3) 从加密钥版产物重打 zip（不打包 data 里的运行状态文件，保留 platforms/post_type 配置）
src = install
out = r'D:\素材准备站\素材准备站_绿色版.zip'
t0 = time.time()
n = 0
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in ('.git', 'browser_profiles')]
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, src).replace(os.sep, '/')
            # 交付 zip 不带本机运行状态/激活标记，防止绕过密钥
            if rel.startswith('data/') and rel.split('/', 1)[1] not in ('platforms.json', 'post_type.json'):
                continue
            z.write(p, rel)
            n += 1
    print(f'写入 {n} 条目 {time.time()-t0:.0f}s')

z2 = zipfile.ZipFile(out)
bad = z2.testzip()
print('CRC:', 'OK' if bad is None else bad)
names = z2.namelist()
print('条目数:', len(names))
print('含 flag/license:', [x for x in names if 'licensed_mode.flag' in x or 'license.dat' in x])
m = next((x for x in names if x.endswith('_internal/platform_adapter/task-platform-download.mjs')), None)
t = z2.read(m).decode('utf-8')
print('mjs 等2秒+轮询4次:', 'await sleep(2000);\n let state;\n for(let i=0;i<4;i++){' in t)
print('mjs let submitted:', 'let submitted=await submitAlias(' in t)
print('zip exe 密钥字符串:', {k: (b in z2.read(next(x for x in names if x.endswith('素材准备站.exe')))) for k, b in zip(keys, kb)})
